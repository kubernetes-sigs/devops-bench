# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Factory selecting an infrastructure deployer from task config and env."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from devops_bench.core import ConfigError, get_bool, get_env, get_logger, resolve_tf_root
from devops_bench.deployers.base import Deployer
from devops_bench.deployers.noop import NoOpDeployer
from devops_bench.deployers.tofu import TFDeployer
from devops_bench.providers import PROVIDERS, ResolveContext

__all__ = ["get_deployer", "needs_cloud_project"]

_log = get_logger("deployers.factory")

_DEFAULT_LOCATION = "us-central1-a"
_DEFAULT_STACK = "prebuilt/kind"
_DEDUCIBLE_PROVIDERS = frozenset({"kind", "vcluster"})

# Providers that provision locally and bill nothing, so a run targeting only
# these needs no cloud project id.
_LOCAL_PROVIDERS = _DEDUCIBLE_PROVIDERS


def _select_provider(infra_config: dict[str, Any], stack: str) -> str:
    """Determine the provider name for a tofu stack.

    Precedence: ``INFRA_PROVIDER`` env → explicit ``provider`` config key →
    directory name deduction from a supported in-repository local stack name. The env
    var wins so a task can pin a default ``provider`` in its config while runs
    stay overridable from the environment (matching
    ``TARGET_DEPLOYMENT_NAME`` / ``NAMESPACE``). Deduction is only applied to
    in-repo (relative) stacks matching a local, non-billable provider name
    (``kind``, ``vcluster``); billable clouds or out-of-repo stacks must name
    their provider explicitly — no cloud is assumed by default, so a cloud provider
    is never silently selected or charged without explicit configuration.

    Args:
        infra_config: Task infrastructure config.
        stack: Resolved stack name or path.

    Returns:
        The selected provider name.

    Raises:
        ConfigError: If no explicit provider is given and the stack does not
            deduce to a deducible local provider directory name.
    """
    from_env = (get_env("INFRA_PROVIDER", "") or "").strip().lower()
    declared = (infra_config.get("provider") or "").strip().lower()
    if from_env and declared and from_env != declared:
        # Almost always a stale export rather than an intent: the variable
        # outlives the shell command that set it, and silently sending a task
        # to the wrong provider looks like a task bug, not a configuration one.
        _log.warning(
            "INFRA_PROVIDER=%r overrides the provider %r declared by the task; "
            "prefer the task's 'provider:' key and unset INFRA_PROVIDER",
            from_env,
            declared,
        )
    explicit = from_env or declared
    if explicit:
        return explicit
    stack_path = Path(stack).expanduser()
    if not stack_path.is_absolute():
        tf_root = resolve_tf_root()
        resolved = (tf_root / stack_path).resolve()
        is_in_repo = tf_root in resolved.parents or resolved == tf_root
        if is_in_repo and resolved.name in _DEDUCIBLE_PROVIDERS:
            return resolved.name
    raise ConfigError(
        f"stack {stack!r} requires an explicit provider; set 'provider' in task "
        "config or the INFRA_PROVIDER env var to a supported provider"
    )


def get_deployer(
    infra_config: dict[str, Any],
    global_project_id: str,
    global_cluster_name: str,
    global_location: str | None = None,
) -> Deployer:
    """Instantiate the deployer selected by task config and environment.

    OpenTofu (``tofu``) is the sole provisioning engine; the selected provider
    only supplies credentials and stack variable defaults. Two layers can skip
    provisioning, with the env layer winning:

    * ``deployer: noop`` (config) *declares* a task that needs no infrastructure.
    * ``BENCH_NO_INFRA=true`` (env) *overrides* any config to skip infra for a
      run (local smoke tests, CI plumbing, running against existing clusters).

    Location precedence: ``global_location`` arg → ``INFRA_LOCATION`` env →
    ``GCP_LOCATION`` env → ``us-central1-a``.

    Args:
        infra_config: Task infrastructure config (``deployer``, ``provider``,
            ``stack``, ``variables``).
        global_project_id: Default project ID.
        global_cluster_name: Default cluster name.
        global_location: Explicit location override.

    Returns:
        A configured :class:`~devops_bench.deployers.base.Deployer`.

    Raises:
        ConfigError: If ``infra_config["deployer"]`` is set to a value other
            than ``tofu`` or ``noop`` (unset/empty defaults to ``tofu``), if
            ``infra_config["variables"]`` is set but not a mapping, if the
            stack names no provider, or if the selected provider is unknown.
    """
    deployer_type = (infra_config.get("deployer") or "").lower()

    if get_bool("BENCH_NO_INFRA") or deployer_type == "noop":
        return NoOpDeployer(cluster_name=global_cluster_name, project_id=global_project_id)

    if deployer_type and deployer_type != "tofu":
        raise ConfigError(
            f"unsupported deployer {deployer_type!r}; use 'tofu', or 'noop' / "
            "BENCH_NO_INFRA=true to skip infra"
        )

    location = (
        global_location
        or get_env("INFRA_LOCATION", "")
        or get_env("GCP_LOCATION", _DEFAULT_LOCATION)
    )
    stack = infra_config.get("stack") or _DEFAULT_STACK
    custom_variables = infra_config.get("variables") or {}
    if not isinstance(custom_variables, dict):
        raise ConfigError(
            f"'variables' in task config must be a mapping, got {type(custom_variables).__name__}"
        )

    provider_name = _select_provider(infra_config, stack)
    if provider_name not in PROVIDERS:
        raise ConfigError(f"unknown provider {provider_name!r}; known: {sorted(PROVIDERS.keys())}")
    provider = PROVIDERS.get(provider_name)()

    ctx = ResolveContext(
        stack=stack,
        project_id=global_project_id,
        cluster_name=global_cluster_name,
        location=location,
    )
    variables = provider.resolve_variables(ctx, custom_variables)

    return TFDeployer(
        tf_dir=stack,
        provider=provider,
        variables=variables,
        custom_keys=set(custom_variables.keys()),
    )


def needs_cloud_project(infra_config: dict[str, Any]) -> bool:
    """Report whether provisioning this task requires a cloud project id.

    Resolves the task's provider the same way :func:`get_deployer` does, but
    without building a deployer or touching credentials, so a launcher can ask
    the question before a run starts.

    A task that provisions nothing (``deployer: noop``) needs no project, and
    neither does one that resolves to a local, non-billable provider. A config
    whose provider cannot be resolved also answers ``False``: demanding a
    project id would replace its real error -- "this stack names no provider" --
    with a misleading one, and ``get_deployer`` still raises that error before
    anything is applied.

    Args:
        infra_config: Task infrastructure config.

    Returns:
        ``True`` when the task targets a provider that bills to a cloud project.
    """
    if (infra_config.get("deployer") or "").strip().lower() == "noop":
        return False
    try:
        provider = _select_provider(infra_config, infra_config.get("stack") or _DEFAULT_STACK)
    except ConfigError:
        return False
    return provider not in _LOCAL_PROVIDERS
