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

"""Agents under evaluation and the agent-selection registry.

``import devops_bench.agents`` is intentionally light: it pulls only the
template-method :class:`AgentHarness`, the typed :class:`AgentConfig` /
:class:`AgentResult` / :class:`ToolCall`, and the :data:`AGENTS` registry.

Each concrete harness lives in a sibling subpackage (``cli.gemini_cli`` /
``cli.openclaw``) and self-registers under its canonical key via
``@AGENTS.register``. Those subpackages pull in heavy optional dependencies
(``deepeval``, provider SDKs); they are imported only when the agent is
selected, never on package import. The harness consumer imports the builtin
subpackages at call time before resolving via :data:`AGENTS`.
"""

from __future__ import annotations

from devops_bench.agents.base import AGENTS, AgentHarness
from devops_bench.agents.config import AgentConfig
from devops_bench.agents.result import AgentResult, ToolCall

__all__ = [
    "AGENTS",
    "AgentConfig",
    "AgentHarness",
    "AgentResult",
    "ToolCall",
]
