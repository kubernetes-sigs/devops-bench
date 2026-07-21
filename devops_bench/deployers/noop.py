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

"""Deployer that skips provisioning, for runs against existing or absent infra."""

from __future__ import annotations

from devops_bench.core import ClusterInfo, get_logger
from devops_bench.deployers.base import Deployer

__all__ = ["NoOpDeployer"]

_log = get_logger("deployers.noop")


class NoOpDeployer(Deployer):
    """Deployer that performs no provisioning.

    Useful when infrastructure already exists (or is intentionally absent) and a
    benchmark run should not create or destroy clusters.

    Args:
        cluster_name: Name reported by :meth:`get_cluster_info`.
        project_id: Optional project reported by :meth:`get_cluster_info`.
    """

    def __init__(self, cluster_name: str, project_id: str | None = None) -> None:
        self.cluster_name = cluster_name
        self.project_id = project_id

    def up(self) -> None:
        """Skip provisioning."""
        _log.info("BENCH_NO_INFRA/noop: skipping provisioning for %s", self.cluster_name)

    def down(self) -> None:
        """Skip teardown."""
        _log.info("BENCH_NO_INFRA/noop: skipping teardown for %s", self.cluster_name)

    def get_cluster_info(self) -> ClusterInfo:
        """Return connection details for the assumed cluster.

        Returns:
            A :class:`~devops_bench.core.ClusterInfo` with ``location`` set to
            ``"local"``; the kubeconfig is resolved from ``KUBECONFIG`` or
            ``~/.kube/config``.
        """
        return ClusterInfo.from_dict(
            {
                "name": self.cluster_name,
                "location": "local",
                "project": self.project_id,
            }
        )
