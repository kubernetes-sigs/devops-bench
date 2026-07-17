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
    substring deduction from the stack name. The env var wins so a task can pin a
    default ``provider`` in its config while runs stay overridable from the
    environment (matching ``TARGET_DEPLOYMENT_NAME`` / ``NAMESPACE``). Deduction
    is only applied to in-repo (relative) stacks; an out-of-repo (absolute or
    ``~``) stack must name its provider explicitly rather than be guessed at.

    Args:
        infra_config: Task infrastructure config.
        stack: Resolved stack name or path.

    Returns:
        The selected provider name.

    Raises:
        ConfigError: If an absolute/external stack has no explicit provider.
    """
    explicit = (get_env("INFRA_PROVIDER", "") or infra_config.get("provider") or "").strip().lower()
    if explicit:
        return explicit
    if get_env("CLOUD_PROVIDER", ""):
        raise ConfigError(
            "CLOUD_PROVIDER environment variable has been renamed to INFRA_PROVIDER. "
            "Please use INFRA_PROVIDER instead."
        )
    if Path(stack).expanduser().is_absolute():
        raise ConfigError(
            f"external stack {stack!r} requires an explicit provider; set 'provider' in task "
            "config or the INFRA_PROVIDER env var (e.g. 'gcp' or 'kind')"
        )
    return "kind" if "kind" in stack else "gcp"


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

    Location precedence: ``global_location`` arg → ``GCP_LOCATION`` env →
    ``us-central1-a``.

    Args:
        infra_config: Task infrastructure config (``deployer``, ``provider``,
            ``stack``, ``variables``).
        global_project_id: Default project ID.
        global_cluster_name: Default cluster name.
        global_location: Explicit location override.

    Returns:
        A configured :class:`~devops_bench.deployers.base.Deployer`.

    Raises:
        ConfigError: If ``infra_config["deployer"]`` is anything other than
            ``tofu`` or ``noop``, if an external stack names no provider, or if
            the selected provider is unknown.
    """
    deployer_type = (infra_config.get("deployer") or "").lower()

    if get_bool("BENCH_NO_INFRA") or deployer_type == "noop":
        return NoOpDeployer(cluster_name=global_cluster_name, project_id=global_project_id)

    if deployer_type and deployer_type != "tofu":
        raise ConfigError(
            f"unsupported deployer {deployer_type!r}; use 'tofu', or 'noop' / "
            "BENCH_NO_INFRA=true to skip infra"
        )

    location = global_location or get_env("GCP_LOCATION", _DEFAULT_LOCATION)
    stack = infra_config.get("stack") or _DEFAULT_STACK
    custom_variables = infra_config.get("variables", {})

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
