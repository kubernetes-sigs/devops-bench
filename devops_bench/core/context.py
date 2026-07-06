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

"""Run context shared across the evaluation pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from devops_bench.core.config import get_env

__all__ = ["ClusterInfo", "RunContext"]

_DEFAULT_KUBECONFIG = "~/.kube/config"


def _resolve_kubeconfig(path: str | None = None) -> str:
    """Resolve a kubeconfig path.

    Args:
        path: Explicit path, if supplied.

    Returns:
        ``path`` if given, else ``KUBECONFIG``, else the expanded ``~/.kube/config``.
    """
    if path:
        return path
    return get_env("KUBECONFIG") or str(Path(_DEFAULT_KUBECONFIG).expanduser())


@dataclass(frozen=True)
class ClusterInfo:
    """Connection details for a provisioned cluster.

    Attributes:
        name: Cluster name.
        location: Cloud region or zone; None for local clusters.
        project: Cloud project; None for local clusters.
        kubeconfig_path: Kubeconfig path; resolved from ``KUBECONFIG`` or
            ``~/.kube/config`` when not supplied.
    """

    name: str
    location: str | None = None
    project: str | None = None
    kubeconfig_path: str = field(default_factory=_resolve_kubeconfig)

    @classmethod
    def from_dict(cls, info: dict[str, Any]) -> ClusterInfo:
        """Build a :class:`ClusterInfo` from a mapping of its fields.

        Args:
            info: Mapping with a required ``name`` and optional ``location``,
                ``project``, and ``kubeconfig_path``.

        Returns:
            The constructed instance, with ``kubeconfig_path`` resolved when absent.
        """
        return cls(
            name=info["name"],
            location=info.get("location"),
            project=info.get("project"),
            kubeconfig_path=_resolve_kubeconfig(info.get("kubeconfig_path")),
        )


@dataclass
class RunContext:
    """State threaded through a single benchmark task run.

    Attributes:
        task_id: Identifier of the task being evaluated.
        task_name: Human-readable task name.
        workspace_path: Working directory the agent operates in.
        cluster: Provisioned cluster details, if any.
        env: Extra environment variables to apply when running commands.
    """

    task_id: str
    task_name: str = ""
    workspace_path: Path | None = None
    cluster: ClusterInfo | None = None
    env: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.workspace_path is not None and not isinstance(self.workspace_path, Path):
            self.workspace_path = Path(self.workspace_path)

    @property
    def kubeconfig_path(self) -> str | None:
        """Cluster kubeconfig path, or None when no cluster is attached."""
        return self.cluster.kubeconfig_path if self.cluster else None
