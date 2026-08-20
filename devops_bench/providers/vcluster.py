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

"""vcluster provider: virtual clusters running inside an existing GKE host."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from devops_bench.core import ClusterInfo, ConfigError, get_env, get_logger
from devops_bench.core.subprocess import run
from devops_bench.providers.base import PROVIDERS, Provider, ResolveContext

__all__ = ["VclusterProvider"]

_log = get_logger("providers.vcluster")


@PROVIDERS.register("vcluster")
class VclusterProvider(Provider):
    """Provider for loft-sh vcluster virtual clusters hosted on GKE.

    A vcluster is a virtual Kubernetes control plane running as a workload
    inside an existing GKE host cluster, provisioned in a couple of minutes
    instead of the ten-plus minutes a real GKE cluster takes. Access to the
    vcluster itself goes through the kubeconfig the OpenTofu module writes to
    disk, not through ``gcloud``; only reaching the *host* cluster (to run the
    vcluster's own control plane) may need ``gcloud`` credentials.
    """

    def ensure_account_credentials(self) -> None:
        """Ensure the host GKE cluster's context is available for kubectl/helm.

        If ``VCLUSTER_HOST_CLUSTER`` is set and a project is resolvable from
        ``GCP_PROJECT_ID``, runs ``gcloud container clusters get-credentials``
        for the host cluster so its context exists in kubeconfig. Otherwise a
        no-op: assumes the host context is already present in kubeconfig.

        Raises:
            ConfigError: If ``VCLUSTER_HOST_CLUSTER`` is set but no location is
                resolvable from ``VCLUSTER_HOST_LOCATION`` or ``GCP_LOCATION``.
        """
        host_cluster = get_env("VCLUSTER_HOST_CLUSTER")
        project = get_env("GCP_PROJECT_ID")
        if not host_cluster or not project:
            _log.debug(
                "vcluster provider: assuming host cluster context is already in kubeconfig"
            )
            return

        location = get_env("VCLUSTER_HOST_LOCATION") or get_env("GCP_LOCATION")
        if not location:
            raise ConfigError(
                "VCLUSTER_HOST_CLUSTER is set but no location is resolvable; "
                "set VCLUSTER_HOST_LOCATION or GCP_LOCATION"
            )
        _log.info("Configuring kubectl for host cluster: %s in %s...", host_cluster, location)
        run(
            [
                "gcloud",
                "container",
                "clusters",
                "get-credentials",
                host_cluster,
                "--location",
                location,
                "--project",
                project,
            ],
            capture=False,
        )

    def ensure_cluster_credentials(
        self, cluster_name: str, location: str, variables: dict[str, Any]
    ) -> ClusterInfo:
        """Describe a vcluster; its kubeconfig is already on disk.

        Args:
            cluster_name: Cluster name from the stack outputs.
            location: Location from the stack outputs (the host GKE region).
            variables: OpenTofu input variables the cluster was provisioned with.

        Returns:
            The cluster's :class:`~devops_bench.core.ClusterInfo`.
        """
        project = variables.get("project_id") or get_env("GCP_PROJECT_ID")
        return ClusterInfo.from_dict(
            {
                "name": cluster_name,
                "location": location,
                "project": project,
                "kubeconfig_path": variables.get("kubeconfig_path"),
            }
        )

    def resolve_variables(
        self, ctx: ResolveContext, custom_variables: dict[str, Any]
    ) -> dict[str, Any]:
        """Resolve default OpenTofu variables for vcluster stacks.

        Returns:
            A new mapping with ``project_id``, ``cluster_name``, ``location``,
            ``host_cluster_name``, ``host_context``, and ``kubeconfig_path``
            filled in where not already set.
        """
        variables = custom_variables.copy()
        variables.setdefault("infra_provider", "vcluster")
        variables.setdefault("project_id", ctx.project_id)
        variables.setdefault("cluster_name", ctx.cluster_name)
        variables.setdefault("location", ctx.location)
        variables.setdefault("host_cluster_name", get_env("VCLUSTER_HOST_CLUSTER") or "")
        host_context = get_env("VCLUSTER_HOST_CONTEXT")
        if host_context:
            variables.setdefault("host_context", host_context)
        cluster_name = variables["cluster_name"]
        kubeconfig_path = str(Path("~/.kube").expanduser() / f"vcluster-{cluster_name}.yaml")
        variables.setdefault("kubeconfig_path", kubeconfig_path)
        return variables
