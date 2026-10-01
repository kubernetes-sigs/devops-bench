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

"""Abstract interface implemented by infrastructure deployers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from devops_bench.core import ClusterInfo

if TYPE_CHECKING:
    from devops_bench.providers.base import Provider

__all__ = ["Deployer"]


class Deployer(ABC):
    """Provisions and tears down a cluster for a benchmark run.

    Attributes:
        provider: Cloud provider backing the cluster, or ``None`` when no
            provider is involved (the no-op deployer). Declared on the base
            so callers can read it by contract instead of probing subclasses.
    """

    provider: Provider | None = None

    @abstractmethod
    def up(self) -> None:
        """Create the cluster."""

    @abstractmethod
    def down(self) -> None:
        """Tear down the cluster."""

    @abstractmethod
    def get_cluster_info(self) -> ClusterInfo:
        """Return connection details for the provisioned cluster.

        Returns:
            The cluster's :class:`~devops_bench.core.ClusterInfo`.
        """
