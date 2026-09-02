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

import json
import os
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from devops_bench.agents.capabilities import (
    AgentRules,
    AllCapabilities,
    McpBinding,
    SkillBinding,
)
from devops_bench.core import ConfigError, get_env, get_int

__all__ = ["AgentConfig"]


# ``${VAR}`` reference in a declared MCP env value — the same form
# :func:`~devops_bench.agents.shared.mcp_probe.expand_env` resolves for the probe
# and every supported CLI resolves for the server it launches.
_ENV_REF = re.compile(r"\$\{\w+\}")

# Env keys whose value must be a reference, never a literal. A binding's ``env``
# is written verbatim into the CLI's config file inside the agent's workspace,
# and the harness collects that workspace wholesale into the run's artifacts, so
# a pasted credential would be persisted with the results.
_SECRET_KEY_HINTS: tuple[str, ...] = ("TOKEN", "KEY", "SECRET", "PASSWORD", "CREDENTIAL")


def _reject_literal_secrets(name: str, env: dict[str, str]) -> None:
    """Fail when a secret-named env value carries a literal instead of ``${VAR}``.

    Args:
        name: The server name, for the error message.
        env: The server's declared ``env`` mapping.

    Raises:
        ConfigError: If a secret-named key holds a value with no ``${VAR}``
            reference in it.
    """
    for key, value in env.items():
        if not value or _ENV_REF.search(value):
            continue
        if any(hint in key.upper() for hint in _SECRET_KEY_HINTS):
            raise ConfigError(
                f"AGENT_MCP_CONFIG server {name!r} env {key!r} looks like a credential but "
                f"holds a literal; use a '${{{key}}}' reference so the value stays out of "
                "the run's artifacts"
            )


class _McpServerSpec(BaseModel):
    """One entry of the standard ``mcpServers`` document.

    Strict so a wrong JSON type fails the grant instead of being coerced: an
    ``args`` of ``0`` silently becoming ``["0"]`` would launch the server with a
    junk argument and report the resulting failure as unreachability.

    Attributes:
        command: The server binary. Required, and must hold a non-whitespace
            character — a binding with no command is "no MCP", which is a grant
            that scores as an MCP arm, and an all-whitespace one splits to the
            same nothing.
        args: Arguments appended to ``command``.
        env: Declared child env; secret-named values must be ``${VAR}``
            references, enforced by :func:`_reject_literal_secrets`.
        cwd: Working directory the server is launched in; ``""`` for inherit.
        tools: Tools to pre-approve, or ``None`` to inherit
            ``AGENT_ALLOWED_TOOLS``. The distinction matters: an explicit ``[]``
            grants none.
    """

    model_config = ConfigDict(strict=True)

    command: str = Field(min_length=1, pattern=r"\S")
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str = ""
    tools: list[str] | None = None

    @model_validator(mode="before")
    @classmethod
    def _drop_nulls(cls, data: Any) -> Any:
        """Treat an explicit ``null`` as absent so the field default applies."""
        if isinstance(data, dict):
            return {key: value for key, value in data.items() if value is not None}
        return data


def _parse_csv(raw: str | None) -> tuple[str, ...]:
    """Split a comma-separated env value into a tuple, skipping blanks."""
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _load_mcp_config(raw: str) -> dict:
    """Load ``AGENT_MCP_CONFIG`` from inline JSON or from a file path.

    A value starting with ``{`` is inline JSON; anything else is a path. Both
    forms exist because the two call sites differ: a one-off local run passes
    the document inline, while the matrix joins its env with ``;`` and evaluates
    it over ssh, where a quoted JSON blob does not survive.

    Args:
        raw: The variable's value, already known to be non-empty.

    Returns:
        The parsed document.

    Raises:
        ConfigError: If the path is missing, undecodable, or the JSON is
            malformed.
    """
    text = raw.strip()
    if not text.startswith("{"):
        path = Path(os.path.expanduser(text))
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"AGENT_MCP_CONFIG path is unreadable: {exc}") from exc
        except UnicodeError as exc:
            raise ConfigError(f"AGENT_MCP_CONFIG file is not valid UTF-8: {exc}") from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"AGENT_MCP_CONFIG is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise ConfigError("AGENT_MCP_CONFIG must be a JSON object")
    return document


def _parse_mcp_config(raw: str, default_tools: tuple[str, ...]) -> tuple[McpBinding, ...]:
    """Build MCP bindings from the standard ``mcpServers`` document.

    The schema is the one Claude Code (``--mcp-config``), Cursor, and the wider
    ecosystem already read, so a server config can be copied between them
    unchanged::

        {"mcpServers": {"github": {"command": "npx",
                                   "args": ["-y", "@modelcontextprotocol/server-github"],
                                   "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}}}}

    ``tools`` is an optional per-server extension (ignored by other readers)
    naming the tools to pre-approve; servers omitting it inherit
    ``default_tools`` from ``AGENT_ALLOWED_TOOLS``.

    Args:
        raw: The raw ``AGENT_MCP_CONFIG`` value (inline JSON or a path).
        default_tools: Tool names applied to servers that declare none.

    Returns:
        One binding per entry, in document order.

    A secret-named ``env`` value must be a ``${VAR}`` reference, not a literal —
    see :func:`_reject_literal_secrets`.

    Raises:
        ConfigError: If the document is malformed, an entry has no ``command``,
            or a secret-named ``env`` value holds a literal.
    """
    servers = _load_mcp_config(raw).get("mcpServers")
    if servers is None:
        raise ConfigError("AGENT_MCP_CONFIG has no 'mcpServers' key")
    if not isinstance(servers, dict):
        raise ConfigError("AGENT_MCP_CONFIG 'mcpServers' must be a JSON object")
    if not servers:
        # An arm that grants nothing leaves AGENT_MCP_CONFIG unset. Setting it to
        # an empty document instead yields a no-MCP run that still reports as an
        # MCP arm — the silent-pass this whole path exists to prevent.
        raise ConfigError("AGENT_MCP_CONFIG 'mcpServers' is empty; unset the variable instead")

    bindings: list[McpBinding] = []
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            raise ConfigError(f"AGENT_MCP_CONFIG server {name!r} must be a JSON object")
        try:
            spec = _McpServerSpec.model_validate(entry)
        except ValidationError as exc:
            detail = "; ".join(
                f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}"
                for err in exc.errors()
            )
            raise ConfigError(f"AGENT_MCP_CONFIG server {name!r} is invalid: {detail}") from exc
        _reject_literal_secrets(name, spec.env)
        bindings.append(
            McpBinding(
                name=name,
                command=(spec.command, *spec.args),
                env=tuple(spec.env.items()),
                cwd=spec.cwd,
                tools=tuple(spec.tools) if spec.tools is not None else default_tools,
            )
        )
    return tuple(bindings)


def _build_capabilities_from_env(env: Mapping[str, str] | None) -> AllCapabilities:
    """Build an :class:`AllCapabilities` aggregate from ``AGENT_*`` env vars.

    MCP servers come from ``AGENT_MCP_CONFIG`` (a standard ``mcpServers``
    document, inline or a path — see :func:`_parse_mcp_config`) which supports
    any number of servers with their own env and cwd. ``AGENT_MCP_SERVER``
    (shell-quoted argv) remains the single-server shorthand and is read only
    when ``AGENT_MCP_CONFIG`` is unset. ``AGENT_ALLOWED_TOOLS`` (CSV) supplies
    the tool list for servers that declare none. ``AGENT_SKILLS_PATHS`` (CSV)
    becomes a :class:`SkillBinding` and ``AGENT_RULES_TEXT`` becomes
    :class:`AgentRules`. A missing variable yields the default (empty) shape,
    so a fully unset env produces a default :class:`AllCapabilities`.

    Args:
        env: Optional mapping read from (defaults to ``os.environ``).

    Returns:
        A populated :class:`AllCapabilities`.

    Raises:
        ConfigError: If ``AGENT_MCP_CONFIG`` is set but malformed. A broken
            capability grant fails loud rather than running an MCP arm with no
            MCP server.
    """
    allowed_tools = _parse_csv(get_env("AGENT_ALLOWED_TOOLS", env=env))
    mcp_config_raw = (get_env("AGENT_MCP_CONFIG", env=env) or "").strip()

    mcp_servers: tuple[McpBinding, ...] = ()
    if mcp_config_raw:
        mcp_servers = _parse_mcp_config(mcp_config_raw, allowed_tools)
    else:
        mcp_command_raw = get_env("AGENT_MCP_SERVER", env=env) or ""
        mcp_command = tuple(shlex.split(mcp_command_raw)) if mcp_command_raw else ()
        if mcp_command or allowed_tools:
            # ``name="default"`` is generic: the shorthand carries no name and
            # the agent never inspects it.
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
            disables the timeout, reachable only via direct construction (use
            only for tests / local debug) — :meth:`from_env` always falls back
            to the 600s default since ``AGENT_TIMEOUT_SEC`` has no sentinel for
            "disabled".
        max_turns: Safety cap on the API agent's tool-use loop turns and on the
            Claude CLI's ``--max-turns``; flows from ``AGENT_MAX_TURNS``.
            ``None`` uses the agent's built-in default.
        capabilities: Aggregate of the MCP / skills / rules bindings granted
            for the run. Constructed by the orchestrator from a benchmark
            catalog (no GKE-specific strings live in agent code).
        extra_env: Provider-agnostic env overlay forwarded to subprocess calls.
            Concrete agents may add their own provider-specific keys on top.
        extra_flags: Optional CLI argument flags forwarded to the spawned agent
            subprocess; flows from ``AGENT_EXTRA_FLAGS``.
    """

    model: str | None = None
    provider: str | None = None
    api_key: str | None = None
    target: str | None = None
    timeout_sec: float | None = 600.0
    max_turns: int | None = None
    capabilities: AllCapabilities = field(default_factory=AllCapabilities)
    extra_env: Mapping[str, str] = field(default_factory=dict)
    extra_flags: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AgentConfig:
        """Build a config from the ``AGENT_*`` environment variables.

        Reads ``AGENT_MODEL`` / ``AGENT_PROVIDER`` / ``AGENT_API_KEY`` /
        ``AGENT_TARGET`` / ``AGENT_TIMEOUT_SEC`` / ``AGENT_MAX_TURNS`` /
        ``AGENT_EXTRA_FLAGS`` and delegates capability construction
        (``AGENT_MCP_CONFIG`` / ``AGENT_MCP_SERVER`` / ``AGENT_ALLOWED_TOOLS`` /
        ``AGENT_SKILLS_PATHS`` / ``AGENT_RULES_TEXT``) to
        :func:`_build_capabilities_from_env`. A missing variable yields the
        dataclass default — this method never raises on unset variables.

        Args:
            env: Optional mapping to read from (defaults to ``os.environ``).
                Tests inject a dict here to avoid mutating the process env.

        Returns:
            A populated :class:`AgentConfig`.

        Raises:
            ConfigError: If ``AGENT_MCP_CONFIG`` is set but malformed.
        """
        timeout = get_int("AGENT_TIMEOUT_SEC", env=env)
        max_turns = get_int("AGENT_MAX_TURNS", env=env)
        extra_flags_raw = get_env("AGENT_EXTRA_FLAGS", env=env) or ""
        extra_flags = tuple(shlex.split(extra_flags_raw)) if extra_flags_raw else ()
        return cls(
            model=get_env("AGENT_MODEL", env=env),
            provider=get_env("AGENT_PROVIDER", env=env),
            api_key=get_env("AGENT_API_KEY", env=env),
            target=get_env("AGENT_TARGET", env=env),
            timeout_sec=float(timeout) if timeout is not None else 600.0,
            max_turns=max_turns,
            capabilities=_build_capabilities_from_env(env),
            extra_flags=extra_flags,
        )
