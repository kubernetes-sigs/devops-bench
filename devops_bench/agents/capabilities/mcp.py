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

"""MCP capability: binding data + agent-side Protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = ["McpBinding", "SupportsMcp"]


@dataclass(frozen=True)
class McpBinding:
    """One MCP server an agent may drive during a run.

    A binding with an empty ``command`` is valid for CLI agents whose binary
    launches its own MCP infrastructure (e.g. Gemini's built-in MCP host); the
    binding still carries the ``tools`` list so the agent knows what to
    advertise / pre-approve.

    Attributes:
        name: Human-readable label for the server. Metadata only — never
            inspected to gate behavior.
        command: argv-style command launching the MCP server, or ``()`` when
            the agent runs MCP in-process. The API agent feeds this to
            :class:`~devops_bench.agents.api.mcp.MCPClient`.
        tools: Tool names this server exposes to the agent. The Gemini CLI
            passes these via ``--allowed-tools``; the API agent advertises
            whatever the live MCP server lists (this field acts as
            documentation / pre-approval there).
    """

    name: str = ""
    command: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()


@runtime_checkable
class SupportsMcp(Protocol):
    """Structural marker for an agent that can drive MCP servers.

    The orchestrator runs ``isinstance(agent, SupportsMcp)`` before granting
    an MCP binding so a task requiring MCP never silently runs against an
    agent that ignores it. Membership is structural: any class with a
    ``mcp_servers`` attribute typed as ``tuple[McpBinding, ...]`` satisfies it
    (concrete agents assign ``self.mcp_servers`` in ``__init__``).
    """

    mcp_servers: tuple[McpBinding, ...]
