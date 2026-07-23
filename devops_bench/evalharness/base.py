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

"""Harness interface: the engine that runs the end-to-end evaluation pipeline."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from devops_bench.core import RunContext
from devops_bench.tasks import Task

__all__ = ["Harness"]


class Harness(ABC):
    """Abstract engine orchestrating a full benchmark evaluation.

    A harness sits above every component (deployers, agents, chaos, verification,
    metrics) and threads them together for each task: provision infrastructure,
    optionally start a background chaos scenario, run the agent under test,
    collect artifacts, tear down, then score and report. It owns the
    :class:`~devops_bench.core.RunContext` carried across these phases.

    Concrete harnesses implement :meth:`run` to execute a batch of typed
    :class:`~devops_bench.tasks.Task` objects; :meth:`make_context` is a helper
    for building the per-task context.
    """

    @abstractmethod
    def run(self, tasks: list[Task]) -> list[dict[str, Any]]:
        """Run the full pipeline over a batch of typed tasks.

        Args:
            tasks: The :class:`~devops_bench.tasks.Task` objects to evaluate
                in order. Each task is provisioned, run, scored, and torn
                down independently.

        Returns:
            The detailed per-task result dicts, scored in place, in the
            ``results.json`` schema produced by routing every typed component
            result through ``to_dict()``/``to_entry()``.
        """

    def make_context(
        self,
        task: Task,
        *,
        cluster: Any | None = None,
        workspace_path: Path | None = None,
    ) -> RunContext:
        """Build the per-task run context carried across pipeline phases.

        Args:
            task: The task being evaluated.
            cluster: Provisioned cluster details, if any.
            workspace_path: Working directory the agent operates in; ``None``
                lets the caller leave the field unset until it builds a
                concrete per-run workspace.

        Returns:
            A :class:`~devops_bench.core.RunContext` seeded with task metadata.
        """
        return RunContext(
            task_id=str(task.id or task.name or ""),
            task_name=task.name,
            workspace_path=workspace_path,
            cluster=cluster,
        )
