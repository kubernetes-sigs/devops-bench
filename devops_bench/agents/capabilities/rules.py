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

"""Rules capability: binding data + agent-side Protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = ["AgentRules", "SupportsRules"]


@dataclass(frozen=True)
class AgentRules:
    """The operator brief delivered to an agent at run start.

    Attributes:
        text: Free-form rules / system-prompt text. An empty string means "no
            preamble" (the agent runs with only the task prompt).
    """

    text: str = ""


@runtime_checkable
class SupportsRules(Protocol):
    """Structural marker for an agent that can carry an :class:`AgentRules` text."""

    rules: AgentRules
