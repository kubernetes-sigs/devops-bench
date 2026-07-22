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

"""Antigravity CLI agent harness driving the ``agy`` binary."""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import time
from typing import TYPE_CHECKING

from devops_bench import core
from devops_bench.agents import base
from devops_bench.agents import config as agents_config
from devops_bench.agents import result as agents_result
from devops_bench.agents.cli.antigravity import parsing
from devops_bench.agents.shared import cli_capabilities
from devops_bench.core import subprocess as devops_subprocess

if TYPE_CHECKING:
    from devops_bench.agents import capabilities

__all__ = ["AgyCliAgent"]

_log = core.get_logger("agents.cli.antigravity")

_GCLOUD_LOOKUP_TIMEOUT_SEC = 10

# agy flushes usage to the conversation DB asynchronously after process exit;
# poll briefly for the rows before giving up.
_DB_FLUSH_POLL_ATTEMPTS = 10
_DB_FLUSH_POLL_INTERVAL_SEC = 0.25
# Rows that decode to nothing mean schema drift, not an in-flight flush: allow
# one recheck, then stop instead of burning the full poll budget.
_UNDECODABLE_MAX_ATTEMPTS = 2


def _read_db_tokens(db_path: pathlib.Path) -> dict | None:
    """Read canonical token usage from the conversation DB.

    Polls for the async flush while the DB reports ``pending``.

    Args:
        db_path: Path to the ``conversations/<uuid>.db`` file.

    Returns:
        The canonical token dict, or ``None`` when usage never materializes
        (missing DB, or schema drift making the blobs undecodable).
    """
    undecodable_seen = 0
    for attempt in range(_DB_FLUSH_POLL_ATTEMPTS):
        state, tokens = parsing.db_token_state(db_path)
        if state == "ready":
            return tokens
        if state == "absent":
            return None
        if state == "undecodable":
            undecodable_seen += 1
            if undecodable_seen >= _UNDECODABLE_MAX_ATTEMPTS:
                return None
        if attempt < _DB_FLUSH_POLL_ATTEMPTS - 1:
            time.sleep(_DB_FLUSH_POLL_INTERVAL_SEC)
    return None


def _resolve_model_name(model: str) -> str:
    """Resolve a provider-qualified model id to the bare name ``agy`` expects.

    e.g. ``"google/gemini-3.5-flash"`` -> ``"gemini-3.5-flash"``.
    """
    return model.split("/")[-1]


def _build_settings(
    mcp_servers: tuple[capabilities.McpBinding, ...],
    model: str | None,
    project: str | None = None,
    location: str | None = None,
    *,
    skills_enabled: bool = False,
) -> dict:
    """Assemble the Antigravity ``settings.json`` payload for a run."""
    settings: dict = {}
    servers = cli_capabilities.build_mcp_servers(mcp_servers)
    if servers:
        settings["mcpServers"] = servers
    if skills_enabled:
        settings["experimental"] = {"skills": True}
    if model:
        settings["modelConfigs"] = {"defaultModel": _resolve_model_name(model)}

    # Add GCP block if project/location are provided (needed for GCA/GKE tools)
    if project or location:
        settings["gcp"] = {}
        if project:
            settings["gcp"]["project"] = project
        if location:
            settings["gcp"]["location"] = location

    return settings


def _build_env(config: agents_config.AgentConfig) -> dict[str, str]:
    """Build the env overlay for the Antigravity CLI subprocess.

    HOME must NOT be overridden to leverage cached OAuth/ADC credentials.
    """
    overlay: dict[str, str] = {
        # Trust workspace so it doesn't block on untrusted folder warnings
        "GEMINI_CLI_TRUST_WORKSPACE": "true",
        # Disable OTLP exporters to avoid hangs in headless environments
        "OTEL_TRACES_EXPORTER": "none",
        "OTEL_METRICS_EXPORTER": "none",
        "OTEL_LOGS_EXPORTER": "none",
        "OTEL_SDK_DISABLED": "true",
    }

    if config.api_key:
        overlay["GEMINI_API_KEY"] = config.api_key
        overlay["GOOGLE_API_KEY"] = config.api_key
    if config.model:
        overlay["GEMINI_MODEL"] = _resolve_model_name(config.model)

    if config.extra_env:
        overlay.update(config.extra_env)

    return overlay


def _get_gcloud_project() -> str | None:
    """Retrieve the default project from gcloud config if available."""
    try:
        result = devops_subprocess.run(
            ["gcloud", "config", "get-value", "project"],
            check=False,
            timeout=_GCLOUD_LOOKUP_TIMEOUT_SEC,
        )
    except (OSError, core.SubprocessError) as exc:
        _log.debug("gcloud project lookup failed: %s", exc)
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _get_gcloud_location() -> str | None:
    """Retrieve the default region from gcloud config if available."""
    try:
        result = devops_subprocess.run(
            ["gcloud", "config", "get-value", "compute/region"],
            check=False,
            timeout=_GCLOUD_LOOKUP_TIMEOUT_SEC,
        )
    except (OSError, core.SubprocessError) as exc:
        _log.debug("gcloud location lookup failed: %s", exc)
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


@base.AGENTS.register("antigravity")
class AgyCliAgent(base.AgentHarness):
    """Antigravity CLI agent harness driving the ``agy`` binary.

    Lays down capabilities (rules, MCP, skills) in the workspace
    directory and spawns the ``agy`` binary. It preserves the user's real
    HOME to leverage cached OAuth/ADC credentials. The trajectory is
    extracted by parsing the generated transcript JSONL log file.
    """

    def __init__(self, config: agents_config.AgentConfig | None = None) -> None:
        super().__init__(config)
        caps = self.config.capabilities
        self.mcp_servers = caps.mcp_servers
        self.skills = caps.skills
        self.rules = caps.rules

    def _resolve_binary(self) -> str:
        """Resolve the absolute path to the ``agy`` binary."""
        if self.config.target:
            return os.path.expanduser(self.config.target)
        # Default installation path for antigravity-cli
        candidate = os.path.expanduser("~/.local/bin/agy")
        if os.path.exists(candidate):
            return candidate
        return "agy"

    def _execute(
        self, prompt: str, workspace_path: pathlib.Path | None = None
    ) -> agents_result.AgentResult:
        caps = self.config.capabilities
        binary = self._resolve_binary()

        env_overlay = _build_env(self.config)

        with cli_capabilities.agent_workdir(workspace_path, prefix="agy-run-") as workdir:
            gemini_dir = workdir / ".gemini"
            # <gemini_dir>/antigravity-cli/ is the single directory agy reads
            # its config from and writes its state to (see the OAuth token,
            # conversations, and transcript paths below, and the global
            # default documented in
            # .agents/references/permission-configs/README.md).
            agy_config_dir = gemini_dir / "antigravity-cli"
            agy_config_dir.mkdir(parents=True, exist_ok=True)

            # Resolve project and location
            project = (
                os.environ.get("GOOGLE_CLOUD_PROJECT")
                or os.environ.get("GCP_PROJECT")
                or _get_gcloud_project()
            )
            if project:
                env_overlay["GOOGLE_CLOUD_PROJECT"] = project
                env_overlay["GCP_PROJECT"] = project

            location = (
                os.environ.get("GOOGLE_CLOUD_LOCATION")
                or os.environ.get("GCP_LOCATION")
                or _get_gcloud_location()
                or "us-central1"
            )
            if location:
                env_overlay["GOOGLE_CLOUD_LOCATION"] = location
                env_overlay["GCP_LOCATION"] = location

            # Explicit gemini_dir keeps agy on the workspace settings, not real HOME.
            argv = [
                binary,
                "--dangerously-skip-permissions",
                f"--gemini_dir={gemini_dir}",
            ]
            if project:
                argv.append(f"--project={project}")
            if self.config.model:
                argv.append(f"--model={_resolve_model_name(self.config.model)}")
            argv.append(f"--prompt={prompt}")

            # Write to both GEMINI.md (legacy) and .agents/AGENTS.md (modern)
            if caps.rules.text:
                (workdir / "GEMINI.md").write_text(caps.rules.text, encoding="utf-8")
                agents_dir = workdir / ".agents"
                agents_dir.mkdir(parents=True, exist_ok=True)
                (agents_dir / "AGENTS.md").write_text(caps.rules.text, encoding="utf-8")

            skill_names: list[str] = []
            if caps.skills.paths:
                skill_names = cli_capabilities.materialize_skills(
                    agy_config_dir / "skills", caps.skills.paths
                )

            settings = _build_settings(
                caps.mcp_servers,
                self.config.model,
                project,
                location,
                skills_enabled=bool(skill_names),
            )
            if settings:
                (agy_config_dir / "settings.json").write_text(
                    json.dumps(settings, indent=2), encoding="utf-8"
                )

            # Copy (not symlink) the OAuth token into the workspace: agy may
            # refresh it in place during a run, and a symlink shared across
            # concurrent runs would race on the one real file.
            real_home = pathlib.Path.home()
            real_token = real_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
            copied_token: pathlib.Path | None = None
            if real_token.exists():
                target_token = agy_config_dir / "antigravity-oauth-token"
                try:
                    shutil.copy2(real_token, target_token)
                    copied_token = target_token
                    _log.info("Copied OAuth token from %s to %s", real_token, target_token)
                except OSError as exc:
                    _log.warning("Failed to copy OAuth token: %s", exc)
            else:
                _log.warning("Real OAuth token not found at %s", real_token)

            completed: devops_subprocess.CompletedProcess | None = None
            timeout_exc: core.SubprocessError | None = None
            try:
                completed = devops_subprocess.run(
                    argv,
                    extra_env=env_overlay,
                    cwd=workdir,
                    check=False,
                    timeout=self.config.timeout_sec,
                )
            except core.SubprocessError as exc:
                # check=False means this can only be a timeout. agy may have
                # already written a partial transcript before being killed,
                # so fall through to recover it instead of returning early
                # and losing the workspace to the `with` block's cleanup.
                timeout_exc = exc
            except OSError as exc:
                return agents_result.AgentResult.errored(
                    f"antigravity-cli binary unavailable: {exc}"
                )
            finally:
                # agy only needs the token while running. Remove the copy once it
                # exits so the live credential never lingers in a workspace that
                # is deliberately retained for artifact collection.
                if copied_token is not None:
                    copied_token.unlink(missing_ok=True)

            # All logs and conversations land under agy_config_dir since we
            # passed --gemini_dir.
            conv_dir = agy_config_dir / "conversations"

            session_text = ""
            # Tokens live in the conversation DB, not the transcript; read them
            # here while the per-run workdir still exists.
            db_tokens: dict | None = None
            if conv_dir.exists():
                db_files = list(conv_dir.glob("*.db"))
                if db_files:
                    # Sort by modification time, newest first
                    db_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                    latest_uuid = db_files[0].stem
                    transcript_path = (
                        agy_config_dir
                        / "brain"
                        / latest_uuid
                        / ".system_generated"
                        / "logs"
                        / "transcript.jsonl"
                    )
                    if transcript_path.exists():
                        session_text = transcript_path.read_text(encoding="utf-8")
                    else:
                        _log.warning("Transcript file not found: %s", transcript_path)
                    db_tokens = _read_db_tokens(db_files[0])
                else:
                    _log.warning("No .db files found in %s", conv_dir)
            else:
                _log.warning("Conversations directory not found: %s", conv_dir)

            if not session_text:
                _log.warning("Failed to retrieve session log, falling back to empty")

        # Output + trajectory come from the transcript; tokens prefer the DB,
        # falling back to transcript-aggregated counts (old agy formats), else
        # all-None so the row reads "unavailable" rather than a fake 0.
        output, trajectory, transcript_tokens, parse_errors = parsing.parse_session_jsonl(
            session_text
        )
        metadata: dict = {}
        if db_tokens is not None:
            tokens = db_tokens
            metadata["token_source"] = "db"
        elif any(transcript_tokens.get(k) for k in ("input", "output", "cached")):
            # Old transcript shape: reasoning is folded into output and there is
            # no cache_write; map onto the canonical buckets.
            tokens = parsing.empty_tokens()
            tokens.update(
                input=transcript_tokens.get("input"),
                cached=transcript_tokens.get("cached"),
                output=transcript_tokens.get("output"),
                total=transcript_tokens.get("total"),
            )
            metadata["token_source"] = "transcript"
        else:
            tokens = parsing.empty_tokens()
            metadata["token_source"] = "unavailable"
            _log.warning("No token usage recovered from conversation DB or transcript")

        errors: list[str] = list(parse_errors)
        if timeout_exc is not None:
            errors.append(f"antigravity-cli subprocess error: {timeout_exc}")
            if not output:
                output = (timeout_exc.stdout or "").strip() or f"Error: {timeout_exc}"
        elif completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            errors.append(f"agy exited {completed.returncode}: {stderr or '<no stderr>'}")
            metadata["returncode"] = completed.returncode
            if not output:
                output = f"Error: agy exited {completed.returncode}"

        # Fall back to raw stdout when the transcript yielded no output.
        if not output and completed is not None and completed.stdout:
            output = completed.stdout.strip()

        return agents_result.AgentResult(
            output=output,
            trajectory=trajectory,
            tokens=tokens,
            errors=errors,
            metadata=metadata,
        )
