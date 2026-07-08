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

"""Typed configuration for the agent harness."""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field

from devops_bench.agents.capabilities import (
    AgentRules,
    AllCapabilities,
    McpBinding,
    SkillBinding,
)
from devops_bench.core import get_env, get_int

__all__ = ["AgentConfig"]


def _parse_csv(raw: str | None) -> tuple[str, ...]:
    """Split a comma-separated env value into a tuple, skipping blanks."""
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _build_capabilities_from_env(env: Mapping[str, str] | None) -> AllCapabilities:
    """Build an :class:`AllCapabilities` aggregate from ``AGENT_*`` env vars.

    Reads ``AGENT_MCP_SERVER`` (shell-quoted argv) and ``AGENT_ALLOWED_TOOLS``
    (CSV) into a single :class:`McpBinding` when either is set;
    ``AGENT_SKILLS_PATHS`` (CSV) into a :class:`SkillBinding`; and
    ``AGENT_RULES_TEXT`` into :class:`AgentRules`. A missing variable yields
    the default (empty) shape, so the function never raises on unset values
    and a fully unset env produces a default :class:`AllCapabilities`.

    Args:
        env: Optional mapping read from (defaults to ``os.environ``).

    Returns:
        A populated :class:`AllCapabilities`.
    """
    mcp_command_raw = get_env("AGENT_MCP_SERVER", env=env) or ""
    mcp_command = tuple(shlex.split(mcp_command_raw)) if mcp_command_raw else ()
    allowed_tools = _parse_csv(get_env("AGENT_ALLOWED_TOOLS", env=env))

    mcp_servers: tuple[McpBinding, ...] = ()
    if mcp_command or allowed_tools:
        # ``name="default"`` is generic: env-driven from_env has no catalog to
        # pull a real name from, and the agent never inspects it.
        mcp_servers = (McpBinding(name="default", command=mcp_command, tools=allowed_tools),)

    skills_paths = _parse_csv(get_env("AGENT_SKILLS_PATHS", env=env))
    skills = SkillBinding(paths=skills_paths)

    rules_text = get_env("AGENT_RULES_TEXT", env=env) or ""
    rules = AgentRules(text=rules_text)

    return AllCapabilities(mcp_servers=mcp_servers, skills=skills, rules=rules)


@dataclass
class AgentConfig:
    """Typed configuration for an agent run.

    All optional fields default to ``None`` / empty so a bare ``AgentConfig()``
    represents "use the agent's built-in defaults". :meth:`from_env` is the only
    reader of ``AGENT_*`` environment variables.

    Attributes:
        model: Optional model id; flows from ``AGENT_MODEL``.
        provider: Optional provider key (``gemini``/``anthropic``/``ollama``/...);
            flows from ``AGENT_PROVIDER``.
        api_key: Optional API key delivered to provider-specific env vars by the
            concrete agent; flows from ``AGENT_API_KEY``.
        target: Optional CLI binary path for CLI agents (``gemini`` / ``oc``);
            flows from ``AGENT_TARGET``. The API agent does **not** consume
            this — its MCP server command rides on ``capabilities.mcp.command``.
        timeout_sec: Wall-clock seconds before an external call is aborted; the
            base harness threads this into every subprocess invocation. ``None``
            disables the timeout (use only for tests / local debug).
        max_turns: Safety cap on the API agent's tool-use loop turns; flows from
            ``AGENT_MAX_TURNS``. ``None`` uses the agent's built-in default.
        capabilities: Aggregate of the MCP / skills / rules bindings granted
            for the run. Constructed by the orchestrator from a benchmark
            catalog (no GKE-specific strings live in agent code).
        extra_env: Provider-agnostic env overlay forwarded to subprocess calls.
            Concrete agents may add their own provider-specific keys on top.
    """

    model: str | None = None
    provider: str | None = None
    api_key: str | None = None
    target: str | None = None
    timeout_sec: float | None = 600.0
    max_turns: int | None = None
    capabilities: AllCapabilities = field(default_factory=AllCapabilities)
    extra_env: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AgentConfig:
        """Build a config from the ``AGENT_*`` environment variables.

        Reads ``AGENT_MODEL`` / ``AGENT_PROVIDER`` / ``AGENT_API_KEY`` /
        ``AGENT_TARGET`` / ``AGENT_TIMEOUT_SEC`` / ``AGENT_MAX_TURNS`` and
        delegates capability construction (``AGENT_MCP_SERVER`` /
        ``AGENT_ALLOWED_TOOLS`` / ``AGENT_SKILLS_PATHS`` / ``AGENT_RULES_TEXT``)
        to :func:`_build_capabilities_from_env`. A missing variable yields the
        dataclass default — this method never raises on unset variables.

        Args:
            env: Optional mapping to read from (defaults to ``os.environ``).
                Tests inject a dict here to avoid mutating the process env.

        Returns:
            A populated :class:`AgentConfig`.
        """
        timeout = get_int("AGENT_TIMEOUT_SEC", env=env)
        max_turns = get_int("AGENT_MAX_TURNS", env=env)
        return cls(
            model=get_env("AGENT_MODEL", env=env),
            provider=get_env("AGENT_PROVIDER", env=env),
            api_key=get_env("AGENT_API_KEY", env=env),
            target=get_env("AGENT_TARGET", env=env),
            timeout_sec=float(timeout) if timeout is not None else 600.0,
            max_turns=max_turns,
            capabilities=_build_capabilities_from_env(env),
        )
