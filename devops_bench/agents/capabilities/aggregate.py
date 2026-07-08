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

"""Aggregate of the three capability bindings carried by :class:`AgentConfig`."""

from __future__ import annotations

from dataclasses import dataclass, field

from devops_bench.agents.capabilities.mcp import McpBinding
from devops_bench.agents.capabilities.rules import AgentRules
from devops_bench.agents.capabilities.skills import SkillBinding

__all__ = ["AllCapabilities"]


@dataclass(frozen=True)
class AllCapabilities:
    """Bundle of the three capability bindings granted to an agent for a run.

    Attributes:
        mcp_servers: MCP server bindings the agent may drive. Empty tuple
            disables MCP entirely.
        skills: Skill binding the agent may load. Default empty binding
            disables skills.
        rules: Operator-brief text. Default empty :class:`AgentRules` means
            "no preamble".
    """

    mcp_servers: tuple[McpBinding, ...] = ()
    skills: SkillBinding = field(default_factory=SkillBinding)
    rules: AgentRules = field(default_factory=AgentRules)

    @property
    def mcp(self) -> McpBinding | None:
        """Return the first MCP binding, or ``None`` when MCP is disabled.

        Gotcha:
            Only the first binding is honored; servers 2..N are dropped —
            iterate ``mcp_servers`` for all.
        """
        return self.mcp_servers[0] if self.mcp_servers else None

    @property
    def allowed_tools(self) -> tuple[str, ...]:
        """Aggregate of every MCP binding's ``tools`` in declared order.

        Returns:
            A flat tuple of tool names — what the Gemini CLI passes as
            ``--allowed-tools <name>`` arguments. Empty when no MCP server is
            bound.
        """
        return tuple(tool for server in self.mcp_servers for tool in server.tools)

    @property
    def tools_enabled(self) -> bool:
        """Convenience boolean: any MCP binding present (regardless of tools)."""
        return bool(self.mcp_servers)
