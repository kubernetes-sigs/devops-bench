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

"""Cloud provider abstraction: identity, cluster access, and OpenTofu variables."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from devops_bench.core import ClusterInfo, NetworkPlan, Registry, SandboxError

__all__ = ["PROVIDERS", "Provider", "ResolveContext"]

# The entry-point group lets out-of-tree providers register without code changes.
PROVIDERS: Registry[type[Provider]] = Registry(
    "providers", entry_point_group="devops_bench.providers"
)


@dataclass(frozen=True)
class ResolveContext:
    """Defaults available to a provider when resolving OpenTofu variables.

    Attributes:
        stack: Stack name or path being provisioned.
        project_id: Default cloud project ID.
        cluster_name: Default cluster name.
        location: Default cloud region or zone.
    """

    stack: str
    project_id: str
    cluster_name: str
    location: str


class Provider(ABC):
    """A cloud environment a benchmark task runs against.

    Splits credentials by scope: account-wide identity (needed by any task that
    calls cloud APIs, with or without a cluster) versus cluster access
    (kubeconfig). Local providers (e.g. KinD) implement the account methods as
    no-ops.
    """

    @abstractmethod
    def ensure_account_credentials(self) -> None:
        """Ensure account-wide cloud identity is active.

        Idempotent: safe to call repeatedly before provisioning or before a task
        calls cloud APIs. Local providers do nothing.
        """

    @abstractmethod
    def ensure_cluster_credentials(
        self,
        cluster_name: str,
        location: str,
        variables: dict[str, Any],
        outputs: dict[str, Any] | None = None,
    ) -> ClusterInfo:
        """Make a provisioned cluster reachable and describe it.

        Resolves the cluster's project and configures kubeconfig access (e.g. via
        ``gcloud container clusters get-credentials``) so ``kubectl`` can reach
        it.

        Args:
            cluster_name: Cluster name from the stack outputs.
            location: Cloud region/zone (or ``"local"``) from the stack outputs.
            variables: OpenTofu input variables the cluster was provisioned with.
            outputs: Optional OpenTofu output values from provisioning.

        Returns:
            The cluster's :class:`~devops_bench.core.ClusterInfo`.
        """

    @abstractmethod
    def resolve_variables(
        self, ctx: ResolveContext, custom_variables: dict[str, Any]
    ) -> dict[str, Any]:
        """Fill default OpenTofu variables for this provider.

        Args:
            ctx: Default project/cluster/location values.
            custom_variables: Task-specified variables; always preserved over
                defaults.

        Returns:
            A new mapping with provider defaults filled in where not already set.
        """

    def sandbox_network_plan(self, cluster_info: ClusterInfo) -> NetworkPlan:
        """Describe how a sandboxed agent container reaches this cluster.

        Every provider must override this with a plan pinned via
        ``kubectl_context`` to the context this cluster wrote; add a Docker
        network or in-network hostname only when the generic loopback rewrite
        cannot infer the endpoint.

        Raises:
            SandboxError: Always, from this default; an un-overridden provider
                cannot be sandboxed.
        """
        raise SandboxError(
            f"provider {type(self).__name__} does not implement sandbox_network_plan; a "
            f"sandboxed run needs a plan pinned to the context cluster {cluster_info.name!r} "
            "wrote — override it, or run this provider unsandboxed"
        )

    def sandbox_cloud_credential_env(
        self, cluster_info: ClusterInfo, *, lifetime_sec: int | None = None
    ) -> dict[str, str]:
        """Mint a ``lifetime_sec`` credential for the task's ``agent_cloud_identity``.

        The default mints nothing, so a named identity is a :class:`SandboxError`.
        """
        del lifetime_sec
        if cluster_info.agent_cloud_identity:
            raise SandboxError(
                f"provider {type(self).__name__} cannot mint a credential for the agent's "
                f"cloud identity {cluster_info.agent_cloud_identity!r}; refusing to run the "
                "agent without the credential its task needs"
            )
        return {}

    def cleanup(
        self,
        cluster_info: ClusterInfo,
        variables: dict[str, Any] | None = None,
        success: bool = True,
    ) -> None:
        """Perform provider-specific cleanup after cluster teardown.

        Args:
            cluster_info: The cluster info of the cluster that was destroyed.
            variables: Optional OpenTofu input variables used during provisioning.
            success: Whether the stack destroy completed successfully.
        """
        del cluster_info, variables, success
