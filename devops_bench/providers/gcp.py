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

"""GCP provider: identity, GKE cluster access, and stack variable defaults."""

from __future__ import annotations

import subprocess
from typing import Any

from devops_bench.core import (
    ClusterInfo,
    ConfigError,
    NetworkPlan,
    SandboxError,
    SubprocessError,
    get_bool,
    get_env,
    get_logger,
)
from devops_bench.core.subprocess import run
from devops_bench.providers.base import PROVIDERS, Provider, ResolveContext

__all__ = ["GcpProvider"]

_log = get_logger("providers.gcp")

_TOKEN_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
# Impersonation's ceiling without constraints/iam.allowServiceAccountCredentialLifetimeExtension.
_DEFAULT_TOKEN_LIFETIME_SEC = 3600
_MINT_TIMEOUT_SEC = 60


def _context_name(project: str, location: str, cluster_name: str) -> str:
    """Reconstruct the kubectl context name ``gcloud get-credentials`` writes."""
    return f"gke_{project}_{location}_{cluster_name}"


def _stderr_of(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or "").strip() or "<no stderr>"


def _print_access_token(identity: str, lifetime_sec: int) -> subprocess.CompletedProcess[str]:
    """Run the bounded impersonation mint; a hang or missing gcloud is a :class:`SandboxError`."""
    try:
        return run(
            [
                "gcloud",
                "auth",
                "print-access-token",
                f"--impersonate-service-account={identity}",
                f"--scopes={_TOKEN_SCOPE}",
                f"--lifetime={lifetime_sec}s",
            ],
            check=False,
            timeout=_MINT_TIMEOUT_SEC,
        )
    except (OSError, SubprocessError) as exc:
        raise SandboxError(
            f"could not mint an access token for the agent's cloud identity {identity!r}: "
            f"{exc} (the mint is bounded at {_MINT_TIMEOUT_SEC}s)"
        ) from exc


@PROVIDERS.register("gcp")
class GcpProvider(Provider):
    """Provider for GCP-hosted (GKE) clusters."""

    def ensure_account_credentials(self) -> None:
        """Ensure GCP application-default credentials are active.

        Currently a no-op: runs assume ambient credentials (ADC, a service
        account key, or workload identity) configured out of band.
        """
        _log.debug("GCP provider: assuming ambient application-default credentials")

    def ensure_cluster_credentials(
        self,
        cluster_name: str,
        location: str,
        variables: dict[str, Any],
        outputs: dict[str, Any] | None = None,
    ) -> ClusterInfo:
        """Configure ``kubectl`` for a GKE cluster via ``gcloud``.

        Args:
            cluster_name: Cluster name from the stack outputs.
            location: Cloud region or zone from the stack outputs.
            variables: OpenTofu input variables the cluster was provisioned with.
            outputs: Optional OpenTofu output values from provisioning.

        Returns:
            The cluster's :class:`~devops_bench.core.ClusterInfo`.

        Raises:
            ConfigError: If no project is resolvable from ``variables`` or the
                ``GCP_PROJECT_ID`` environment variable.
        """
        project = variables.get("project_id") or get_env("GCP_PROJECT_ID")
        if not project:
            raise ConfigError("Project ID not found in variables or environment (GCP_PROJECT_ID).")

        _log.info("Configuring kubectl for cluster: %s in %s...", cluster_name, location)
        run(
            [
                "gcloud",
                "container",
                "clusters",
                "get-credentials",
                cluster_name,
                "--location",
                location,
                "--project",
                project,
            ],
            capture=False,
        )

        context_name = _context_name(project, location, cluster_name)
        if get_bool("GCP_USE_ADC", False):
            _log.info(
                "Enabling application default credentials for auth plugin in context %s",
                context_name,
            )
            run(
                [
                    "kubectl",
                    "config",
                    "set-credentials",
                    context_name,
                    "--exec-arg=--use_application_default_credentials",
                ],
                capture=False,
            )
        else:
            _log.info(
                "Skipping GKE ADC credentials override (GCP_USE_ADC is false) for context %s",
                context_name,
            )

        return ClusterInfo.from_dict(
            {
                "name": cluster_name,
                "location": location,
                "project": project,
                # Only stacks whose task makes its own cloud API calls set this.
                "agent_cloud_identity": (outputs or {}).get("agent_cloud_identity"),
            }
        )

    def sandbox_network_plan(self, cluster_info: ClusterInfo) -> NetworkPlan:
        """Pin to this cluster's GKE context; the endpoint routes as-is.

        Raises:
            SandboxError: If project or location is unknown — an unpinned
                plan would mint the agent's credential on the ambient
                current-context, not necessarily this cluster.
        """
        if not (cluster_info.project and cluster_info.location):
            raise SandboxError(
                f"GKE cluster {cluster_info.name!r} reported no project/location, so the "
                "sandbox cannot pin to its kubectl context; refusing rather than "
                "provisioning the agent's credential against the ambient context, "
                "which may be a different cluster entirely"
            )
        return NetworkPlan(
            kubectl_context=_context_name(
                cluster_info.project, cluster_info.location, cluster_info.name
            )
        )

    def sandbox_cloud_credential_env(
        self, cluster_info: ClusterInfo, *, lifetime_sec: int | None = None
    ) -> dict[str, str]:
        """Mint a token by impersonating the task's identity host-side; raises when it cannot.

        Lifetimes past an hour need the lifetime-extension org policy, else an hour plus a warning.
        """
        identity = cluster_info.agent_cloud_identity
        if not identity:
            return {}
        lifetime = lifetime_sec or _DEFAULT_TOKEN_LIFETIME_SEC
        result = _print_access_token(identity, lifetime)
        if result.returncode != 0 and lifetime > _DEFAULT_TOKEN_LIFETIME_SEC:
            _log.warning(
                "could not mint a %ds credential for %s (%s); retrying at %ds, so it expires "
                "before the agent's token budget",
                lifetime,
                identity,
                _stderr_of(result),
                _DEFAULT_TOKEN_LIFETIME_SEC,
            )
            lifetime = _DEFAULT_TOKEN_LIFETIME_SEC
            result = _print_access_token(identity, lifetime)
        token = (result.stdout or "").strip()
        if result.returncode != 0 or not token:
            raise SandboxError(
                f"could not mint an access token for the agent's cloud identity "
                f"{identity!r} (gcloud exit {result.returncode}: {_stderr_of(result)}); the "
                "provisioning identity needs roles/iam.serviceAccountTokenCreator on it — "
                "refusing to run the agent without the credential its task needs"
            )
        _log.info("minted a %ds cloud credential for the sandboxed agent as %s", lifetime, identity)
        env = {
            "CLOUDSDK_AUTH_ACCESS_TOKEN": token,
            "GOOGLE_OAUTH_ACCESS_TOKEN": token,
        }
        if cluster_info.project:
            # Not GOOGLE_CLOUD_PROJECT: the agent overlay uses it to route model calls.
            env["CLOUDSDK_CORE_PROJECT"] = cluster_info.project
        return env

    def cleanup(
        self,
        cluster_info: ClusterInfo,
        variables: dict[str, Any] | None = None,
        success: bool = True,
    ) -> None:
        """No-op: GKE cluster cleanup is handled by stack teardown."""
        del success

    def resolve_variables(
        self, ctx: ResolveContext, custom_variables: dict[str, Any]
    ) -> dict[str, Any]:
        """Resolve default OpenTofu variables for GCP-based stacks.

        Returns:
            A new mapping with ``project_id``, ``cluster_name``, and ``location``
            filled in where not already set, plus ``namespace`` from the
            ``NAMESPACE`` environment variable when present.
        """
        variables = custom_variables.copy()
        variables.setdefault("infra_provider", "gcp")
        variables.setdefault("project_id", ctx.project_id)
        variables.setdefault("cluster_name", ctx.cluster_name)
        variables.setdefault("location", ctx.location)
        namespace = get_env("NAMESPACE")
        if namespace is not None:
            variables.setdefault("namespace", namespace)
        kubeconfig_path = get_env("KUBECONFIG")
        if kubeconfig_path:
            variables.setdefault("kubeconfig_path", kubeconfig_path)
        return variables
