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

"""Skills capability: binding data + agent-side Protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = ["SkillBinding", "SupportsSkills"]


@dataclass(frozen=True)
class SkillBinding:
    """Local skill directories an agent may load.

    Attributes:
        paths: Filesystem locations to walk for ``SKILL.md`` files. The agent
            walks each path recursively at run time; missing paths are warned
            and skipped. An empty tuple disables skills entirely —
            independently of MCP.
    """

    paths: tuple[str, ...] = ()


@runtime_checkable
class SupportsSkills(Protocol):
    """Structural marker for an agent that can load local skill files.

    The orchestrator runs ``isinstance(agent, SupportsSkills)`` before
    granting a :class:`SkillBinding` so a task requiring skills never silently
    runs against an agent that ignores them.
    """

    skills: SkillBinding
