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

Trajectory extraction reads the official ``--output-format stream-json`` event
stream from stdout (see :mod:`~devops_bench.agents.cli.gemini_cli.parsing`) — no
``~/.gemini/tmp/...`` disk reads, no session-id glob, no internal-schema parsing.

Capability wiring is delivered through the Gemini CLI's native workspace
mechanisms, written into the per-run working directory before invocation:

* **Tools** — ``config.capabilities.allowed_tools`` become ``--allowed-tools``
  arguments, pre-approving them in headless mode.
* **MCP servers** — command-bearing bindings become ``mcpServers`` entries in
  ``<cwd>/.gemini/settings.json``.
* **Skills** — ``config.capabilities.skills.paths`` are materialized under
  ``<cwd>/.gemini/skills/<name>/SKILL.md``.
* **Rules** — ``config.capabilities.rules.text`` is written to ``GEMINI.md``,
  auto-loaded as the startup context.
"""

from __future__ import annotations

import json
import os
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
from devops_bench.core import SubprocessError, get_logger
from devops_bench.core.model_providers import resolve_provider
from devops_bench.core.subprocess import run

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from devops_bench.agents.capabilities import McpBinding

__all__ = ["GeminiCliAgent"]

# Filename the Gemini CLI auto-loads from its working directory as the
# operator brief / startup context (its native equivalent of a system prompt).
_GEMINI_RULES_FILE = "GEMINI.md"
# Workspace config dir/files the CLI reads from its cwd. ``settings.json`` here
# overrides the user-level ``~/.gemini/settings.json``; ``skills/`` is the
# workspace skill-discovery root.
_GEMINI_CONFIG_DIR = ".gemini"
_GEMINI_SETTINGS_FILE = "settings.json"
_GEMINI_SKILLS_DIR = "skills"

_log = get_logger("agents.cli.gemini_cli")


def _build_settings(mcp_servers: tuple[McpBinding, ...], *, skills_enabled: bool) -> dict:
    """Assemble the Gemini ``settings.json`` payload for a run.

    Args:
        mcp_servers: Bindings to render into ``mcpServers`` (empty-command
            bindings are skipped by :func:`build_mcp_servers`).
        skills_enabled: Whether any workspace skill was materialized; gates the
            explicit ``skills.enabled`` flag so the benchmark does not depend on
            the user-level default.

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


def _build_argv(target: str, prompt: str, allowed_tools: tuple[str, ...]) -> list[str]:
    """Build the ``gemini`` invocation for ``prompt``.

    ``--approval-mode yolo`` is always passed so the CLI auto-approves every tool
    call (built-in *and* MCP) instead of blocking on interactive confirmation —
    without it, MCP tool calls hang until the run hits its timeout.

    When ``allowed_tools`` is empty, gemini *extensions* are disabled via
    ``--extensions=`` — this is orthogonal to MCP (servers come from
    ``settings.json`` and stay available). The short ``-e=`` / ``-e=""`` forms
    print help and exit non-zero on gemini >= 0.47 (the literal value, quotes
    included, reaches the parser since argv bypasses the shell), and ``-e none``
    loads an extension literally named "none" rather than disabling.

    Note: MCP servers only load when the workspace is *trusted*. The per-run temp
    cwd is untrusted by default, so the bastion sets
    ``security.folderTrust.enabled = false`` in the user-level
    ``~/.gemini/settings.json`` (``--skip-trust`` alone does not lift the MCP
    gate). See ``scripts/bastion/vm-setup.sh``.

    Args:
        target: Path to the ``gemini`` binary (already user-expanded).
        prompt: Task prompt.
        allowed_tools: Pre-approved tool names; each yields a separate
            ``--allowed-tools <name>`` pair (redundant under yolo).

    Returns:
        The argv list ready to hand to ``core.subprocess.run``.
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
    argv.extend(["-p", prompt])
    return argv


def _build_env(config: AgentConfig) -> dict[str, str]:
    """Build the env overlay that makes the Gemini CLI run model-agnostic.

    Maps the benchmark's neutral ``AGENT_*`` fields onto the variables the
    Gemini CLI expects: ``config.api_key`` is routed onto the provider's API-key
    env var(s) via the shared contract
    (:func:`~devops_bench.core.model_providers.resolve_provider`), and
    ``config.model`` onto ``GEMINI_MODEL``. OTLP telemetry exporters are disabled
    so they don't hang on broken endpoints. The model is never hardcoded; it
    flows from ``config.model``. A keyless backend (e.g. Vertex/ADC) writes no
    key.

    Args:
        config: Resolved :class:`AgentConfig` for this run.

    Returns:
        A mapping suitable for ``core.subprocess.run``'s ``extra_env``.

    Raises:
        ConfigError: If ``config.provider`` is not a known provider.
    """
    # Resolve unconditionally so an unknown provider fails loud even on a keyless
    # (Vertex/ADC) run, not only when a key happens to be set.
    spec = resolve_provider(config.provider)
    overlay: dict[str, str] = {
        # Disable the Gemini CLI's OTLP exporters; otherwise they block/hang
        # trying to reach an unreachable collector endpoint in headless runs.
        "OTEL_TRACES_EXPORTER": "none",
        "OTEL_METRICS_EXPORTER": "none",
        "OTEL_LOGS_EXPORTER": "none",
        "OTEL_SDK_DISABLED": "true",
    }
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

    The binary path is resolved from ``config.target``, falling back to
    ``"gemini"`` on ``$PATH``. Model / API key flow from
    ``config.model`` / ``config.api_key`` via the env overlay — never
    hardcoded. ``config.capabilities.allowed_tools`` (aggregated across every
    bound MCP server) selects between the ``--allowed-tools`` overlay and the
    ``--extensions=`` extensions-disabled path.

    ``__init__`` assigns ``self.mcp_servers``, ``self.skills`` and ``self.rules``
    from the granted config bindings, which is what makes
    ``isinstance(agent, SupportsMcp / SupportsSkills / SupportsRules)`` return
    ``True`` for orchestrator-side capability negotiation (the Protocols are
    structural).

    The full canonical trajectory is parsed from the official
    ``--output-format stream-json`` event stream; no session files are read
    from disk.
    """

    def __init__(self, config: AgentConfig | None = None) -> None:
        AgentHarness.__init__(self, config)
        caps = self.config.capabilities
        self.mcp_servers = caps.mcp_servers
        self.skills = caps.skills
        self.rules = caps.rules

    def _execute(self, prompt: str, workspace_path: Path | None = None) -> AgentResult:
        """Build argv, run the CLI, and parse the stream-json output.

        The agent runs the binary inside ``workspace_path`` when the harness
        supplies one, else its own per-run temp working directory, and lays
        down the granted capabilities there before invocation, using the
        CLI's native workspace channels (all auto-loaded from the cwd, so the
        user's ``~/.gemini`` stays untouched and concurrent runs never race):

        * ``GEMINI.md`` — the operator brief, when ``rules.text`` is set.
        * ``.gemini/settings.json`` — ``mcpServers`` for each command-bearing
          MCP binding, plus ``skills.enabled`` when skills were materialized.
        * ``.gemini/skills/<name>/SKILL.md`` — one per discovered skill.

        A temp working directory (used when ``workspace_path`` is ``None``) is
        cleaned up when ``_execute`` returns; a harness-supplied
        ``workspace_path`` is left for the harness to collect and clean up.
        """
        caps = self.config.capabilities
        target = os.path.expanduser(self.config.target or "gemini")
        argv = _build_argv(target, prompt, caps.allowed_tools)
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
            try:
                completed = run(
                    argv,
                    extra_env=env_overlay,
                    cwd=workdir,
                    check=False,
                    timeout=self.config.timeout_sec,
                )
            except SubprocessError as exc:
                return AgentResult.errored(f"gemini subprocess error: {exc}")
            except OSError as exc:
                # Missing / non-executable binary; core.subprocess.run does not wrap.
                return AgentResult.errored(f"gemini binary unavailable: {exc}")

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
            metadata=metadata,
        )
