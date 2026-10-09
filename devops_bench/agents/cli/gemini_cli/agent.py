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

"""Gemini CLI agent harness driving the ``gemini`` binary.

The trajectory is parsed from the ``--output-format stream-json`` stdout stream
(:mod:`~.parsing`), never from session files. Capabilities use the CLI's
workspace channels under the per-run cwd: ``--allowed-tools``, ``mcpServers`` in
``.gemini/settings.json``, skills under ``.gemini/skills/``, rules in ``GEMINI.md``.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from devops_bench.agents.base import AGENTS, AgentHarness
from devops_bench.agents.cli.gemini_cli.parsing import parse_stream_json
from devops_bench.agents.config import AgentConfig
from devops_bench.agents.result import AgentResult
from devops_bench.agents.shared.cli_capabilities import (
    agent_workdir,
    build_mcp_servers,
    materialize_skills,
)
from devops_bench.agents.shared.mcp_probe import (
    PROBE_TIMEOUT_SEC,
    McpUnreachableError,
    preflight_mcp,
)
from devops_bench.agents.shared.vertex_env import (
    VERTEX_PROJECT_ENVS,
    vertex_location,
    vertex_project,
)
from devops_bench.core import ConfigError, SubprocessError, get_logger
from devops_bench.core.model_providers import resolve_provider, sandbox_credential_env
from devops_bench.core.subprocess import run

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from devops_bench.agents.capabilities import McpBinding

__all__ = ["GeminiCliAgent"]

# Auto-loaded from the cwd as the startup context (the CLI's system-prompt channel).
_GEMINI_RULES_FILE = "GEMINI.md"
# Workspace config the CLI reads from its cwd; overrides the user-level settings.
_GEMINI_CONFIG_DIR = ".gemini"
_GEMINI_SETTINGS_FILE = "settings.json"
_GEMINI_SKILLS_DIR = "skills"

_log = get_logger("agents.cli.gemini_cli")

# The image ships its own gemini on PATH; a host binary path cannot exec inside.
_CONTAINER_GEMINI_BIN = "gemini"

_ANSI_SGR = re.compile(r"\x1b\[[0-9;]*m")
_MCP_CONNECTED = "connected"
_MCP_NOT_LISTED = "<not listed>"
_MCP_LIST_TIMEOUT_SEC = 120.0
_LISTING_TAIL_CHARS = 800


def _build_settings(mcp_servers: tuple[McpBinding, ...], *, skills_enabled: bool) -> dict:
    """Assemble the Gemini ``settings.json`` payload for a run.

    Args:
        mcp_servers: Bindings to render into ``mcpServers`` (empty-command
            bindings are skipped by :func:`build_mcp_servers`).
        skills_enabled: Whether any workspace skill was materialized; sets
            ``skills.enabled`` explicitly rather than relying on the user default.

    Returns:
        A settings mapping, possibly empty (caller skips the write when empty).
    """
    settings: dict = {}
    servers = build_mcp_servers(mcp_servers)
    if servers:
        settings["mcpServers"] = servers
    if skills_enabled:
        settings["skills"] = {"enabled": True}
    return settings


def _status_rows(listing: str, name: str) -> list[str]:
    """Return the status word of every ``gemini mcp list`` row for ``name``.

    Matches the exact escaped name followed by ``: `` so non-word characters work
    and ``k8s`` is not conflated with ``k8s:prod``.
    """
    row = re.compile(rf"^\W*{re.escape(name)}:\s.*\s-\s+(\S+)\s*$", re.MULTILINE)
    return [match.group(1) for match in row.finditer(listing)]


def _mcp_gate_failure(
    target: str,
    workdir: Path,
    expected: tuple[str, ...],
    *,
    env_overlay: dict[str, str],
    timeout: float = _MCP_LIST_TIMEOUT_SEC,
) -> str:
    """Return why the CLI cannot use ``expected`` in ``workdir``, or ``""``.

    Gemini gates MCP on folder trust and silently disables servers in untrusted
    workspaces; ``gemini mcp list`` is the only view that reports the post-trust
    connection state.

    Args:
        target: Path to the ``gemini`` binary.
        workdir: Workspace whose ``.gemini/settings.json`` is under test.
        expected: Server names that must report ``Connected``.
        env_overlay: Env overlay the run uses so ``${VAR}`` references resolve.
        timeout: Seconds before the listing is abandoned.

    Returns:
        A human-readable failure reason, or ``""`` when all servers are connected.
    """
    try:
        completed = run(
            [target, "mcp", "list"],
            extra_env=env_overlay,
            cwd=workdir,
            check=False,
            timeout=timeout,
        )
    except (SubprocessError, OSError) as exc:
        return f"could not run 'gemini mcp list': {exc}"

    # gemini 0.56 prints the listing on stderr; read both streams.
    listing = _ANSI_SGR.sub("", f"{completed.stdout or ''}\n{completed.stderr or ''}")

    if completed.returncode != 0:
        detail = listing.strip()[-_LISTING_TAIL_CHARS:]
        return f"'gemini mcp list' exited {completed.returncode}: {detail or '<no output>'}"

    broken: dict[str, str] = {}
    for name in expected:
        statuses = _status_rows(listing, name)
        if not statuses:
            broken[name] = _MCP_NOT_LISTED
        elif any(status.lower() != _MCP_CONNECTED for status in statuses):
            broken[name] = "/".join(sorted(set(statuses)))
    if not broken:
        return ""
    detail = ", ".join(f"{name} ({status})" for name, status in sorted(broken.items()))
    return f"the CLI does not report these servers as connected: {detail}"


def _probe_failure_detail(
    mcp_servers: tuple[McpBinding, ...],
    *,
    env_overlay: dict[str, str],
    workdir: Path,
    timeout: float = PROBE_TIMEOUT_SEC,
) -> str:
    """Run the stdio probe after ``gemini mcp list`` fails to report the root cause."""
    try:
        preflight_mcp(
            mcp_servers,
            base_env={**os.environ, **env_overlay},
            timeout=timeout,
            cwd=workdir,
        )
    except McpUnreachableError as exc:
        return f"; {exc}"
    return "; every server answered a direct stdio probe, so this is a CLI-side gate (folder trust)"


def _build_argv(
    target: str,
    prompt: str,
    allowed_tools: tuple[str, ...],
    extra_flags: tuple[str, ...] = (),
) -> list[str]:
    """Build the ``gemini`` invocation for ``prompt``.

    ``--approval-mode yolo`` auto-approves every tool call, or MCP calls hang
    until the timeout. With no ``allowed_tools``, extensions are disabled via
    ``--extensions=`` (MCP servers come from ``settings.json`` and stay available).
    MCP servers only load in a trusted workspace, so the host setup disables
    ``security.folderTrust`` in the user-level settings; ``--skip-trust`` alone
    does not lift that gate. :func:`_mcp_gate_failure` fails the run when it
    does not.

    Args:
        target: Path to the ``gemini`` binary (already user-expanded).
        prompt: Task prompt.
        allowed_tools: Pre-approved tool names, one ``--allowed-tools`` each.
        extra_flags: Optional extra CLI flags to forward to the binary.
    """
    argv = [target, "--output-format", "stream-json", "--skip-trust"]
    argv.extend(["--approval-mode", "yolo"])
    if allowed_tools:
        for tool in allowed_tools:
            argv.extend(["--allowed-tools", tool])
    else:
        # `--extensions=` disables extensions; `-e=`/`-e=""` print help + exit 1
        # on gemini >= 0.47, and `-e none` loads an extension named "none".
        argv.append("--extensions=")
    if extra_flags:
        argv.extend(extra_flags)
    argv.extend(["-p", prompt])
    return argv


def _build_env(config: AgentConfig) -> dict[str, str]:
    """Build the env overlay that makes the Gemini CLI run model-agnostic.

    Routes ``config.api_key`` and ``config.model`` onto the CLI's env vars and
    disables the OTLP exporters. A Vertex backend gets the google-genai routing
    vars; sandboxed and keyless it also gets the metadata-emulator vars from
    :func:`~devops_bench.core.model_providers.sandbox_credential_env`.

    Raises:
        ConfigError: Unknown provider, a Vertex run with neither project nor
            ``GOOGLE_API_KEY``, or a sandboxed keyless run with no credential recipe.
    """
    # Resolve unconditionally so an unknown provider fails loud even keyless.
    spec = resolve_provider(config.provider)
    overlay: dict[str, str] = {
        # The OTLP exporters hang on an unreachable collector in headless runs.
        "OTEL_TRACES_EXPORTER": "none",
        "OTEL_METRICS_EXPORTER": "none",
        "OTEL_LOGS_EXPORTER": "none",
        "OTEL_SDK_DISABLED": "true",
    }
    if spec.backend == "vertex":
        # Without the switch the embedded google-genai SDK ignores the Vertex routing.
        overlay["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
        project = vertex_project()
        if project:
            overlay["GOOGLE_CLOUD_PROJECT"] = project
        elif not os.environ.get("GOOGLE_API_KEY"):
            # gemini-cli refuses this combination; fail here naming the variables we read.
            raise ConfigError(
                "a Vertex run of the Gemini CLI needs a project: set one of "
                f"{', '.join(VERTEX_PROJECT_ENVS)} (or GOOGLE_API_KEY for Vertex "
                "express mode)"
            )
        overlay["GOOGLE_CLOUD_LOCATION"] = vertex_location()
        if config.sandbox is not None and not (config.api_key or os.environ.get("GOOGLE_API_KEY")):
            # A key crosses the boundary on its own; only a keyless (ADC) run needs the recipe.
            overlay.update(sandbox_credential_env(spec, project=project))
    else:
        # Pin off explicitly so an ambient GOOGLE_GENAI_USE_VERTEXAI=true can't reroute the run.
        overlay["GOOGLE_GENAI_USE_VERTEXAI"] = "false"
    if config.api_key:
        for var in spec.api_key_envs:
            overlay[var] = config.api_key
    if config.model:
        overlay["GEMINI_MODEL"] = config.model
    if config.extra_env:
        overlay.update(config.extra_env)
    return overlay


@AGENTS.register("gemini")
class GeminiCliAgent(AgentHarness):
    """Gemini CLI agent harness driving the ``gemini`` binary.

    The binary comes from ``config.target``, else ``gemini`` on ``PATH``; model
    and key flow through the env overlay. ``__init__`` assigns
    ``mcp_servers``/``skills``/``rules`` so the agent satisfies the
    ``Supports*`` protocols.
    """

    supports_sandbox = True

    def __init__(self, config: AgentConfig | None = None) -> None:
        AgentHarness.__init__(self, config)
        caps = self.config.capabilities
        self.mcp_servers = caps.mcp_servers
        self.skills = caps.skills
        self.rules = caps.rules

    def _execute(self, prompt: str, workspace_path: Path | None = None) -> AgentResult:
        """Build argv, run the CLI, and parse the stream-json output.

        Capabilities are laid down in ``workspace_path`` (or a temp dir this
        method owns and removes) as ``GEMINI.md``, ``.gemini/settings.json`` and
        ``.gemini/skills/``, all auto-loaded from the cwd.
        """
        caps = self.config.capabilities
        target = os.path.expanduser(self.config.target or "gemini")
        if self.config.sandbox is not None:
            target = _CONTAINER_GEMINI_BIN
        argv = _build_argv(target, prompt, caps.allowed_tools, self.config.extra_flags)
        env_overlay = _build_env(self.config)
        rules_text = caps.rules.text

        with agent_workdir(workspace_path, prefix="gemini-run-") as workdir:
            if rules_text:
                (workdir / _GEMINI_RULES_FILE).write_text(rules_text, encoding="utf-8")

            gemini_dir = workdir / _GEMINI_CONFIG_DIR
            skill_names = materialize_skills(gemini_dir / _GEMINI_SKILLS_DIR, caps.skills.paths)
            settings = _build_settings(caps.mcp_servers, skills_enabled=bool(skill_names))
            if settings:
                gemini_dir.mkdir(parents=True, exist_ok=True)
                (gemini_dir / _GEMINI_SETTINGS_FILE).write_text(
                    json.dumps(settings, indent=2), encoding="utf-8"
                )
            if self.config.sandbox is not None:
                # The container HOME is fresh, so seed the folder-trust disable there;
                # untrusted, the CLI drops settings, GEMINI.md and --approval-mode.
                user_gemini_dir = workdir / "home" / _GEMINI_CONFIG_DIR
                user_gemini_dir.mkdir(parents=True, exist_ok=True)
                (user_gemini_dir / _GEMINI_SETTINGS_FILE).write_text(
                    json.dumps({"security": {"folderTrust": {"enabled": False}}}, indent=2),
                    encoding="utf-8",
                )

            turn_timeout = self.config.timeout_sec
            expected = tuple(build_mcp_servers(caps.mcp_servers))
            if expected and self.config.sandbox is not None:
                _log.info("Skipping host MCP preflight for sandboxed run")
            elif expected:
                deadline = (
                    None
                    if self.config.timeout_sec is None
                    else time.monotonic() + self.config.timeout_sec
                )
                list_timeout = (
                    _MCP_LIST_TIMEOUT_SEC
                    if deadline is None
                    else min(_MCP_LIST_TIMEOUT_SEC, max(0.0, deadline - time.monotonic()))
                )
                reason = _mcp_gate_failure(
                    target,
                    workdir,
                    expected,
                    env_overlay=env_overlay,
                    timeout=list_timeout,
                )
                if reason:
                    probe_timeout = (
                        PROBE_TIMEOUT_SEC
                        if deadline is None
                        else min(PROBE_TIMEOUT_SEC, max(0.0, deadline - time.monotonic()))
                    )
                    detail = _probe_failure_detail(
                        caps.mcp_servers,
                        env_overlay=env_overlay,
                        workdir=workdir,
                        timeout=probe_timeout,
                    )
                    return AgentResult.errored(f"MCP preflight failed: {reason}{detail}")
                if deadline is not None:
                    turn_timeout = max(0.0, deadline - time.monotonic())

            turn_start = time.monotonic()
            try:
                completed = self.run_agent_cmd(
                    argv,
                    extra_env=env_overlay,
                    cwd=workdir,
                    check=False,
                    timeout=turn_timeout,
                    host_run=run,
                )
            except SubprocessError as exc:
                return AgentResult.errored(f"gemini subprocess error: {exc}")
            except OSError as exc:
                # Missing / non-executable binary; core.subprocess.run does not wrap.
                return AgentResult.errored(f"gemini binary unavailable: {exc}")
            turn_latency = time.monotonic() - turn_start

        output, trajectory, tokens, parse_errors = parse_stream_json(completed.stdout or "")
        errors: list[str] = list(parse_errors)
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            errors.append(f"gemini exited {completed.returncode}: {stderr or '<no stderr>'}")
            if not output:
                output = f"Error: gemini exited {completed.returncode}"
        metadata: dict = {}
        if completed.returncode != 0:
            metadata["returncode"] = completed.returncode
        return AgentResult(
            output=output,
            trajectory=trajectory,
            tokens=tokens,
            errors=errors,
            latency=turn_latency,
            metadata=metadata,
        )
