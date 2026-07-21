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

from devops_bench.core import ConfigError, get_bool, get_env
from devops_bench.deployers.base import Deployer
from devops_bench.deployers.noop import NoOpDeployer
from devops_bench.deployers.tofu import TFDeployer
from devops_bench.providers import PROVIDERS, ResolveContext

__all__ = ["get_deployer"]

_DEFAULT_LOCATION = "us-central1-a"
_DEFAULT_STACK = "prebuilt/kind"


def _select_provider(infra_config: dict[str, Any], stack: str) -> str:
    """Determine the provider name for a tofu stack.

    Precedence: ``INFRA_PROVIDER`` env → explicit ``provider`` config key →
    ``kind`` deduced from an in-repo stack name. The env var wins so a task can
    pin a default ``provider`` in its config while runs stay overridable from
    the environment (matching ``TARGET_DEPLOYMENT_NAME`` / ``NAMESPACE``).
    Deduction is only applied to in-repo (relative) stacks named ``kind``; an
    out-of-repo (absolute or ``~``) stack, or any in-repo stack not named
    ``kind``, must name its provider explicitly — no cloud is assumed by
    default, so a new provider never silently inherits another's defaults.

    Args:
        infra_config: Task infrastructure config.
        stack: Resolved stack name or path.

    Returns:
        The selected provider name.

    Raises:
        ConfigError: If no explicit provider is given and the stack does not
            deduce to ``kind``.
    """
    explicit = (get_env("INFRA_PROVIDER", "") or infra_config.get("provider") or "").strip().lower()
    if explicit:
        return explicit
    stack_path = Path(stack).expanduser()
    if not stack_path.is_absolute() and stack_path.name == "kind":
        return "kind"
    raise ConfigError(
        f"stack {stack!r} requires an explicit provider; set 'provider' in task "
        "config or the INFRA_PROVIDER env var (e.g. 'gcp' or 'kind')"
    )


def get_deployer(
    infra_config: dict[str, Any],
    global_project_id: str,
    global_cluster_name: str,
    global_location: str | None = None,
) -> Deployer:
    """Instantiate the deployer selected by task config and environment.

    OpenTofu (``tofu``) is the sole provisioning engine; the provider (``gcp`` or
    ``kind``) only supplies credentials and stack variable defaults. Two layers
    can skip provisioning, with the env layer winning:

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
