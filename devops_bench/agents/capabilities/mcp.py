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
        env: Environment pairs the server is launched with, as a tuple of
            ``(name, value)`` so the binding stays hashable. A value may carry
            ``${VAR}`` references (braces required — a bare ``$VAR`` stays
            literal) resolved from the runner's environment.

            Two things expand those references, and which one runs depends on
            who launches the server:

            * The harness, via
              :func:`~devops_bench.agents.shared.mcp_probe.expand_env`, when it
              spawns the server itself — the preflight probe and the API agent.
              The resolved value exists only in that child process.
            * The CLI, for a server the binary spawns. The reference is written
              into the CLI's config file **unexpanded** and the CLI resolves it
              from its own environment, so a credential never lands in the
              agent's workspace — which is collected wholesale into the run's
              artifacts.

            Both read the same runner env, so the two paths agree on the value;
            only the expansion point differs. A secret-named value must be a
            reference rather than a literal, enforced when the grant is parsed.
        cwd: Working directory the server is launched in. Empty inherits the
            agent's working directory (the per-run workspace).
        tools: Tool names this server exposes to the agent. The Gemini CLI
            passes these via ``--allowed-tools``; the API agent advertises
            whatever the live MCP server lists (this field acts as
            documentation / pre-approval there).
    """

    name: str = ""
    command: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    cwd: str = ""
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
