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

"""Agent capabilities: binding data + structural Protocols for MCP, skills, and rules."""

from __future__ import annotations

from devops_bench.agents.capabilities.aggregate import AllCapabilities
from devops_bench.agents.capabilities.mcp import McpBinding, SupportsMcp
from devops_bench.agents.capabilities.rules import AgentRules, SupportsRules
from devops_bench.agents.capabilities.skills import (
    SkillBinding,
    SupportsSkills,
)

__all__ = [
    "AgentRules",
    "AllCapabilities",
    "McpBinding",
    "SkillBinding",
    "SupportsMcp",
    "SupportsRules",
    "SupportsSkills",
]
