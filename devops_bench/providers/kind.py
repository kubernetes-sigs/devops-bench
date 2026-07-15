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

"""KinD provider: local clusters with no cloud identity."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from devops_bench.core import ClusterInfo, get_env
from devops_bench.providers.base import PROVIDERS, Provider, ResolveContext

__all__ = ["KindProvider"]

_DEFAULT_CLUSTER_NAME = "devops-bench-kind"
_LOCAL_PROJECT = "local-kind"


@PROVIDERS.register("kind")
class KindProvider(Provider):
    """Provider for local KinD clusters; no cloud account is involved."""

    def ensure_account_credentials(self) -> None:
        """No-op: local clusters require no cloud identity."""

    def ensure_cluster_credentials(
        self, cluster_name: str, location: str, variables: dict[str, Any]
    ) -> ClusterInfo:
        """Describe a local cluster; its kubeconfig is already on disk.

        Args:
            cluster_name: Cluster name from the stack outputs.
            location: Location from the stack outputs (typically ``"local"``).
            variables: OpenTofu input variables the cluster was provisioned with.

        Returns:
            The cluster's :class:`~devops_bench.core.ClusterInfo`; ``project``
            falls back to ``local-kind`` when none is set.
        """
        project = variables.get("project_id") or _LOCAL_PROJECT
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
        """Resolve default OpenTofu variables for local KinD stacks.

        Returns:
            A new mapping with ``cluster_name``, ``location`` (``"local"``), and
            ``kubeconfig_path`` filled in where not already set.
        """
        variables = custom_variables.copy()
        variables.setdefault("infra_provider", "kind")
        variables.setdefault("project_id", ctx.project_id or _LOCAL_PROJECT)
        variables.setdefault("cluster_name", ctx.cluster_name or _DEFAULT_CLUSTER_NAME)
        variables.setdefault("location", "local")
        kubeconfig_path = get_env("KUBECONFIG") or str(
            Path("~/.kube/config").expanduser().resolve()
        )
        variables.setdefault("kubeconfig_path", kubeconfig_path)
        return variables
