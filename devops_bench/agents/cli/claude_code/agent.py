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

"""Claude Code CLI agent harness driving the ``claude`` binary.

Runs ``claude`` in headless mode (``-p --output-format stream-json --verbose``)
and extracts the canonical trajectory from the official event stream on stdout
(see :mod:`~devops_bench.agents.cli.claude_code.parsing`) — no session-file
reads off disk.

Capability wiring uses Claude Code's native cwd-based channels, written into the
per-run working directory before invocation:

* **Rules** — ``config.capabilities.rules.text`` → ``CLAUDE.md``, auto-loaded
  from the cwd as the startup context.
* **Skills** — ``config.capabilities.skills.paths`` are materialized under
  ``<cwd>/.claude/skills/<name>/SKILL.md``, Claude Code's skill-discovery root.
* **MCP servers** — command-bearing bindings become a ``{"mcpServers": ...}``
  document at ``<cwd>/.claude/mcp-config.json``, passed via ``--mcp-config``.
  ``--strict-mcp-config`` is always set — including on a baseline arm with no
  bindings — so any stray project ``.mcp.json`` is ignored and no trust prompt
  fires. A bound run first checks the binary against
  :data:`_MCP_WAIT_MIN_VERSION`.

Auth is env-driven, matching the bench contract: ``config.api_key`` →
``ANTHROPIC_API_KEY`` for the direct API, or keyless Vertex / Bedrock via ADC /
AWS credentials. ``CLAUDE_CONFIG_DIR`` is redirected to a fresh per-run temp dir
so Claude Code's mutable global state never races across concurrent evals (see
:func:`_claude_config_dir` for the OAuth-debug escape hatch, which ``--parallel``
overrides).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import time
from collections.abc import Iterator
from functools import cache
from pathlib import Path

from devops_bench.agents.base import AGENTS, AgentHarness
from devops_bench.agents.cli.claude_code.parsing import parse_stream_json
from devops_bench.agents.config import AgentConfig
from devops_bench.agents.result import AgentResult
from devops_bench.agents.shared.cli_capabilities import (
    agent_workdir,
    build_mcp_servers,
    materialize_skills,
)
from devops_bench.core import SubprocessError, get_logger
from devops_bench.core.config import get_bool
from devops_bench.core.model_providers import resolve_provider
from devops_bench.core.subprocess import run

__all__ = ["ClaudeCodeAgent"]

# Auto-loaded from the cwd as the operator brief (native system-prompt analog).
_CLAUDE_RULES_FILE = "CLAUDE.md"
# Workspace config dir/files Claude Code reads from its cwd: ``skills/`` is the
# skill-discovery root and ``mcp-config.json`` is passed via ``--mcp-config``.
_CLAUDE_CONFIG_DIRNAME = ".claude"
_CLAUDE_SKILLS_DIR = "skills"
_CLAUDE_MCP_FILE = "mcp-config.json"
# Relocates Claude Code's mutable global state (see _claude_config_dir).
_CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"

_log = get_logger("agents.cli.claude_code")

# Child stderr is unbounded and reaches the persisted result record (both
# ``metadata`` and ``errors``), so every path clips it to the same tail.
_STDERR_TAIL_CHARS = 2000

# Under ``-p`` the CLI reads a piped prompt from stdin and waits out a 3s
# timeout before giving up when stdin stays open with nothing on it — which is
# what an inherited stdin looks like. Handing it an empty string closes the pipe
# immediately: ~3.7s saved per task, and no "no stdin data received" warning
# polluting ``metadata["stderr"]`` (and, on a non-zero exit, the error message,
# where it would displace the real cause).
_CLOSED_STDIN = ""


def _stderr_tail(stderr: str | None) -> str:
    """Stripped last :data:`_STDERR_TAIL_CHARS` characters of ``stderr``."""
    return (stderr or "").strip()[-_STDERR_TAIL_CHARS:]


# ``--mcp-config`` under ``-p`` only waits for still-pending servers to connect
# before the first turn from this version on. An older binary accepts the flag
# and starts the turn anyway, so an MCP-augmented arm can run with none of its
# tools attached and still be scored as augmented — the same silent
# contamination ``--strict-mcp-config`` prevents from the other direction.
_MCP_WAIT_MIN_VERSION = (2, 1, 221)
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


@cache
def _claude_version(target: str) -> tuple[int, int, int] | None:
    """Parse ``claude --version``, or ``None`` when it cannot be determined.

    An unreadable version is not an error: ``config.target`` may be a wrapper
    script with its own ``--version`` surface, and refusing to run on a probe
    that merely failed to parse would be worse than the risk it guards.

    Cached per target: a matrix run drives one binary across every task, and the
    version cannot change under a live process. An inconclusive probe caches too,
    so a wrapper without a ``--version`` surface is not re-spawned per task.
    """
    try:
        completed = run([target, "--version"], check=False, timeout=30)
    except (OSError, SubprocessError):
        return None
    match = _VERSION_RE.search(completed.stdout or "")
    if completed.returncode != 0 or not match:
        return None
    return (int(match[1]), int(match[2]), int(match[3]))


def _build_argv(
    target: str,
    prompt: str,
    *,
    model: str | None,
    max_turns: int | None,
    mcp_config_path: str | None,
) -> list[str]:
    """Build the ``claude`` headless invocation for ``prompt``.

    ``--verbose`` is mandatory (the CLI rejects ``stream-json`` under ``-p``
    without it) and ``--dangerously-skip-permissions`` keeps headless runs from
    blocking on confirmation prompts.

    ``--strict-mcp-config`` is passed unconditionally, not just alongside
    ``--mcp-config``: it pins the run to exactly the bound servers, and for a
    baseline arm that set is empty. Without it, a stray ``.mcp.json`` in the task
    workspace (the CLI's cwd) is loaded and silently grants MCP tools to the
    un-augmented arm, contaminating the very comparison the bench measures.

    The prompt is the trailing positional after ``--``. The CLI's option parser
    would otherwise read a prompt whose first token begins with ``-`` as a flag
    and abort the run.

    Args:
        target: Path to the ``claude`` binary (already user-expanded).
        prompt: Task prompt, passed as an argv value (never through a shell).
        model: Model id for ``--model``, or ``None`` to use the CLI default.
        max_turns: Cap for ``--max-turns``; ``None`` or non-positive uses the
            CLI default (the CLI rejects ``0``, so it is treated as unset).
        mcp_config_path: Absolute path to the MCP config document, or ``None``.

    Returns:
        The argv list ready to hand to ``core.subprocess.run``.
    """
    argv = [
        target,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--strict-mcp-config",
    ]
    if model:
        argv.extend(["--model", model])
    if max_turns is not None and max_turns > 0:
        argv.extend(["--max-turns", str(max_turns)])
    if mcp_config_path:
        argv.extend(["--mcp-config", mcp_config_path])
    argv.extend(["--", prompt])
    return argv


def _build_env(config: AgentConfig, *, config_dir: str | None) -> dict[str, str]:
    """Build the env overlay that makes the Claude Code run model-agnostic.

    ``config.api_key`` routes onto the provider's key env var(s) via the shared
    contract (default ``anthropic``); Vertex / Bedrock backends set their
    ``CLAUDE_CODE_USE_*`` switch, with Vertex mapping the repo's ambient
    ``GCP_PROJECT_ID`` / ``GCP_VERTEX_LOCATION`` onto the CLI's equivalents. The
    model is never set here — it flows through the ``--model`` argv flag.

    Args:
        config: Resolved :class:`AgentConfig` for this run.
        config_dir: Per-run ``CLAUDE_CONFIG_DIR`` path, or ``None`` when the
            operator exported their own (then the ambient value is left intact).

    Returns:
        A mapping suitable for ``core.subprocess.run``'s ``extra_env``.

    Raises:
        ConfigError: If ``config.provider`` is not a known provider.
    """
    # Resolve unconditionally so an unknown provider fails loud even on a keyless
    # (Vertex/Bedrock) run, not only when a key happens to be set.
    spec = resolve_provider(config.provider, default="anthropic")
    overlay: dict[str, str] = {
        # Headless hygiene: no background telemetry/error traffic, no autoupdate.
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
    }
    # Guard on truthiness: run() overlays extra_env onto os.environ, so writing
    # an empty key would clobber an ambient ANTHROPIC_API_KEY.
    if config.api_key:
        for var in spec.api_key_envs:
            overlay[var] = config.api_key
    if spec.backend == "vertex":
        overlay["CLAUDE_CODE_USE_VERTEX"] = "1"
        project = os.environ.get("GCP_PROJECT_ID")
        if project:
            overlay["ANTHROPIC_VERTEX_PROJECT_ID"] = project
        # Prefer the repo var, then an operator-set native CLOUD_ML_REGION, so we
        # only fall back to "global" when neither is set (never clobber it).
        region = os.environ.get("GCP_VERTEX_LOCATION") or os.environ.get("CLOUD_ML_REGION")
        overlay["CLOUD_ML_REGION"] = region or "global"
    elif spec.backend == "bedrock":
        overlay["CLAUDE_CODE_USE_BEDROCK"] = "1"
    if config_dir is not None:
        overlay[_CONFIG_DIR_ENV] = config_dir
    # Operator-supplied extra_env is applied last and deliberately wins over the
    # harness-set keys above (its escape hatch for backend/region overrides).
    if config.extra_env:
        overlay.update(config.extra_env)
    return overlay


@contextlib.contextmanager
def _claude_config_dir() -> Iterator[str | None]:
    """Yield a fresh per-run ``CLAUDE_CONFIG_DIR``, or ``None`` if operator-set.

    A non-empty ambient ``CLAUDE_CONFIG_DIR`` is the operator's escape hatch
    (e.g. to reuse a cached OAuth login for local debugging); it is left
    untouched and ``None`` is yielded so no per-run temp dir is created or
    injected. An empty ambient value is ignored so per-run isolation still
    applies (the CLI treats an empty var as unset and would race on ~/.claude).

    The hatch is refused under ``BENCH_PARALLEL``. Several benchmark processes
    on one host would then share a single mutable config dir — the collision
    class :mod:`devops_bench.core.run_env` exists to prevent — and the operator
    has already declared concurrency, so isolation outranks the login cache.
    """
    if os.environ.get(_CONFIG_DIR_ENV):
        if not get_bool("BENCH_PARALLEL", False):
            yield None
            return
        _log.warning(
            "ignoring ambient %s under BENCH_PARALLEL: concurrent runs would share "
            "one mutable Claude config dir; using a per-run dir instead",
            _CONFIG_DIR_ENV,
        )
    # ignore_cleanup_errors: Claude Code may leave straggler state/lock files or
    # MCP-server children; a cleanup OSError must not turn a completed run into
    # an errored one via the base safety net.
    with tempfile.TemporaryDirectory(prefix="claude-config-", ignore_cleanup_errors=True) as tmpdir:
        yield tmpdir


@AGENTS.register("claude")
class ClaudeCodeAgent(AgentHarness):
    """Claude Code CLI agent harness driving the ``claude`` binary.

    The binary path is resolved from ``config.target``, falling back to
    ``"claude"`` on ``$PATH``. Model / API key flow from ``config.model`` (via
    ``--model``) / ``config.api_key`` (via the env overlay) — never hardcoded.

    ``__init__`` assigns ``self.mcp_servers``, ``self.skills`` and ``self.rules``
    from the granted config bindings, which is what makes
    ``isinstance(agent, SupportsMcp / SupportsSkills / SupportsRules)`` return
    ``True`` for orchestrator-side capability negotiation (the Protocols are
    structural).

    """

    def __init__(self, config: AgentConfig | None = None) -> None:
        AgentHarness.__init__(self, config)
        caps = self.config.capabilities
        self.mcp_servers = caps.mcp_servers
        self.skills = caps.skills
        self.rules = caps.rules

    def _execute(self, prompt: str, workspace_path: Path | None = None) -> AgentResult:
        """Build argv, run the CLI, and parse the stream-json output.

        Capabilities are laid down in the run directory first via the cwd
        channels described in the module docstring. The temp working directory
        (when ``workspace_path`` is ``None``) and the per-run
        ``CLAUDE_CONFIG_DIR`` are cleaned up on return; a harness-supplied
        ``workspace_path`` is left for the harness to collect.
        """
        caps = self.config.capabilities
        target = os.path.expanduser(self.config.target or "claude")
        rules_text = caps.rules.text

        with agent_workdir(workspace_path, prefix="claude-run-") as workdir:
            if rules_text:
                (workdir / _CLAUDE_RULES_FILE).write_text(rules_text, encoding="utf-8")

            claude_dir = workdir / _CLAUDE_CONFIG_DIRNAME
            materialize_skills(claude_dir / _CLAUDE_SKILLS_DIR, caps.skills.paths)

            mcp_config_path: str | None = None
            servers = build_mcp_servers(caps.mcp_servers)
            if servers:
                claude_dir.mkdir(parents=True, exist_ok=True)
                mcp_path = claude_dir / _CLAUDE_MCP_FILE
                mcp_path.write_text(json.dumps({"mcpServers": servers}, indent=2), encoding="utf-8")
                # A binding's argv can carry a server credential, and this file
                # lands in the run workspace the harness later collects, so do
                # not leave it at the umask default (0o644 on most machines).
                mcp_path.chmod(0o600)
                mcp_config_path = str(mcp_path)
                version = _claude_version(target)
                if version is not None and version < _MCP_WAIT_MIN_VERSION:
                    return AgentResult.errored(
                        "claude "
                        + ".".join(str(part) for part in version)
                        + " predates the --mcp-config startup wait (needs "
                        + ".".join(str(part) for part in _MCP_WAIT_MIN_VERSION)
                        + "); an MCP-bound arm would run without its servers. "
                        "Upgrade the binary or drop the mcp_servers binding."
                    )

            argv = _build_argv(
                target,
                prompt,
                model=self.config.model,
                max_turns=self.config.max_turns,
                mcp_config_path=mcp_config_path,
            )
            with _claude_config_dir() as config_dir:
                env_overlay = _build_env(self.config, config_dir=config_dir)
                timed_out = False
                started = time.monotonic()
                try:
                    completed = run(
                        argv,
                        extra_env=env_overlay,
                        cwd=workdir,
                        check=False,
                        timeout=self.config.timeout_sec,
                        input=_CLOSED_STDIN,
                    )
                except SubprocessError as exc:
                    # Report a timeout as one rather than as an exit -1 that
                    # reads like a crash. str(exc) embeds the child's full
                    # stderr, so rebuild the message from the clipped tail rather
                    # than interpolating it. The timeout carries the partial
                    # stream-json captured before the kill; fall through so the
                    # trajectory is recovered rather than dropped.
                    timed_out = exc.timed_out
                    stderr = _stderr_tail(exc.stderr)
                    returncode = exc.returncode
                    stdout = exc.stdout or ""
                    reason = (
                        f"claude timed out after {self.config.timeout_sec}s"
                        if timed_out
                        else f"claude failed to run: exit {returncode}"
                    )
                    if stderr:
                        reason += f": {stderr}"
                except OSError as exc:
                    # Spawn failure core.subprocess.run does not wrap: usually a
                    # missing / non-executable binary, but also a vanished cwd.
                    return AgentResult.errored(
                        f"failed to spawn claude: {exc}", latency=time.monotonic() - started
                    )
                else:
                    stderr = _stderr_tail(completed.stderr)
                    returncode = completed.returncode
                    stdout = completed.stdout or ""
                    reason = (
                        None
                        if returncode == 0
                        else f"claude exited {returncode}: {stderr or '<no stderr>'}"
                    )
                agent_sec = time.monotonic() - started

        parsed = parse_stream_json(stdout)
        output = parsed.output
        errors: list[str] = list(parsed.errors)
        metadata: dict = {}
        if stderr:
            # Keep stderr for diagnosis even on a clean exit — e.g. MCP startup
            # warnings that leave the process returncode at 0.
            metadata["stderr"] = stderr
        if reason is not None:
            errors.append(reason)
            metadata["returncode"] = returncode
            output = output or f"Error: {reason}"
        return AgentResult(
            output=output,
            trajectory=parsed.trajectory,
            tokens=parsed.tokens,
            latency=agent_sec,
            errors=errors,
            # The timeout is the harness's own doing and outranks whatever the
            # killed process managed to write. Otherwise the stream's terminal
            # event wins: it is more specific than the exit code, and the CLI
            # exits 1 on a turn cap that the parser resolves to ``completed``.
            # The exit code governs only a stream that never reached its
            # terminal event -- a truncated pipe or a binary that died early.
            terminal_reason=(
                "timeout"
                if timed_out
                else parsed.terminal_reason or ("error" if returncode != 0 else "completed")
            ),
            tool_wait_sec=parsed.tool_wait_sec,
            served_models=parsed.served_models,
            model_turns=parsed.model_turns,
            metadata=metadata,
        )
