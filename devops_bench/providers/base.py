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

from devops_bench.core import ClusterInfo, Registry

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
        self, cluster_name: str, location: str, variables: dict[str, Any]
    ) -> ClusterInfo:
        """Make a provisioned cluster reachable and describe it.

        Resolves the cluster's project and configures kubeconfig access (e.g. via
        ``gcloud container clusters get-credentials``) so ``kubectl`` can reach
        it.

        Args:
            cluster_name: Cluster name from the stack outputs.
            location: Cloud region/zone (or ``"local"``) from the stack outputs.
            variables: OpenTofu input variables the cluster was provisioned with.

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
