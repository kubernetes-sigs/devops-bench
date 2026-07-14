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

from typing import Any

from devops_bench.core import ClusterInfo, ConfigError, get_bool, get_env, get_logger
from devops_bench.core.subprocess import run
from devops_bench.providers.base import PROVIDERS, Provider, ResolveContext

__all__ = ["GcpProvider"]

_log = get_logger("providers.gcp")


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
        self, cluster_name: str, location: str, variables: dict[str, Any]
    ) -> ClusterInfo:
        """Configure ``kubectl`` for a GKE cluster via ``gcloud``.

        Args:
            cluster_name: Cluster name from the stack outputs.
            location: Cloud region or zone from the stack outputs.
            variables: OpenTofu input variables the cluster was provisioned with.

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

        context_name = f"gke_{project}_{location}_{cluster_name}"
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
            {"name": cluster_name, "location": location, "project": project}
        )

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
