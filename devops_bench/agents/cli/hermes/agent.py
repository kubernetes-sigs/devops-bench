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

"""Hermes CLI agent harness driving the ``hermes`` binary (local-only).

Capability wiring is delivered through hermes's native channels, laid down in
the run-scoped home directory ``$HERMES_HOME``:

* **State isolation** — ``HERMES_HOME`` points at ``<workspace>/.hermes``, so
  ``config.yaml`` and the ``state.db`` session store are per-run and never
  touch the user's ``~/.hermes``. The eval harness snapshots the workspace
  before the run, so ``.hermes`` is copied into the run's ``generated_files/``
  as a single directory rather than scattering hermes state across the diff.
* **MCP servers** — command-bearing bindings become ``mcp_servers`` entries in
  ``$HERMES_HOME/config.yaml``.
* **Skills** — ``config.capabilities.skills.paths`` are materialized under
  ``$HERMES_HOME/skills/<name>/SKILL.md``. Hermes's repo-local skill discovery
  is switched off and its bundled catalog is cut down to hermes's own essential
  ``hermes-agent`` skill, which always survives; beyond that one, the granted
  skills are all the agent is offered.
* **Rules** — ``config.capabilities.rules.text`` is prepended to the prompt
  (``hermes chat`` has no dedicated system-prompt flag).
* **Model auth** — ``config.api_key`` is threaded into the provider env vars
  the resolved :class:`~devops_bench.core.model_providers.ProviderSpec` names.

Trajectory and token usage are read back from the run's SQLite ``state.db``;
the parsers live in :mod:`~devops_bench.agents.cli.hermes.parsing`.

``__init__`` assigns ``self.rules``, ``self.mcp_servers`` and ``self.skills``
from the granted bindings, so the agent structurally satisfies
``SupportsRules`` / ``SupportsMcp`` / ``SupportsSkills``.
"""

from __future__ import annotations

import io
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from ruamel.yaml import YAML

from devops_bench.agents.base import AGENTS, AgentHarness
from devops_bench.agents.cli.hermes.parsing import (
    extract_tokens_from_db,
    extract_trajectory_from_db,
)
from devops_bench.agents.config import AgentConfig
from devops_bench.agents.result import AgentResult, empty_tokens
from devops_bench.agents.shared.cli_capabilities import (
    agent_workdir,
    build_mcp_servers,
    materialize_skills,
    mcp_isolation_env,
    prepend_rules,
)
from devops_bench.core import ConfigError, SubprocessError, get_logger
from devops_bench.core.model_providers import known_providers, resolve_provider
from devops_bench.core.subprocess import run

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from devops_bench.agents.capabilities import McpBinding

__all__ = ["HermesAgent"]

_log = get_logger("agents.cli.hermes.agent")

_HERMES_HOME_DIRNAME = ".hermes"
_CONFIG_FILE = "config.yaml"
_STATE_DB = "state.db"
_SKILLS_DIRNAME = "skills"

# Marker file hermes checks on startup: with it present the bundled skill sync
# is cut down to hermes's own essential skill instead of installing all 82.
# Without it every run is granted a skill catalog (``devops``, ``mlops``, ...)
# the capability matrix never granted, and the run's ``.hermes`` carries the
# ~6 MB of skill packs into the artifacts.
_NO_BUNDLED_SKILLS_MARKER = ".no-bundled-skills"

# Installation artifacts hermes materializes inside its home: a vendored binary,
# the models.dev catalog download, and its request cache. ~22 MB per task with
# nothing to say about what the agent did, so they are dropped before the
# harness snapshots the workspace. Run evidence (state.db, config, logs) stays.
_HOME_INSTALL_ARTIFACTS: tuple[str, ...] = (
    "bin",
    "cache",
    "models_dev_cache.json",
    "models_dev_cache.etag",
)

# ruamel's safe (non-round-trip) loader: block style keeps the generated config
# readable if a run is inspected after the fact.
_yaml = YAML(typ="safe")
_yaml.default_flow_style = False


# Canonical providers hermes can serve — the ones carrying a ``hermes_provider``
# on their :class:`ProviderSpec`. Only used to spell out the error below.
_HERMES_SUPPORTED: tuple[str, ...] = tuple(
    sorted(
        {
            spec.canonical
            for alias in known_providers()
            if (spec := resolve_provider(alias)).hermes_provider
        }
    )
)


def _hermes_provider(provider: str) -> str:
    """Map a bench provider alias onto the name ``hermes chat --provider`` takes.

    Args:
        provider: Raw ``AGENT_PROVIDER`` value.

    Returns:
        The hermes provider name.

    Raises:
        ConfigError: If hermes has no equivalent for the resolved provider.
    """
    spec = resolve_provider(provider)
    if spec.hermes_provider is None:
        # anthropic-vertex today: hermes's "vertex" serves Gemini only, so there
        # is no way to ask it for Claude on Vertex. Fail loudly rather than let
        # the run answer with a Gemini model instead.
        raise ConfigError(
            f"provider {spec.canonical!r} has no hermes equivalent; "
            f"supported: {', '.join(_HERMES_SUPPORTED)}"
        )
    return spec.hermes_provider


def _prune_install_artifacts(home: Path) -> None:
    """Drop hermes's own installation files from the run home.

    Called once the state DB has been read, so the workspace snapshot the eval
    harness takes carries the run's evidence rather than a copy of the hermes
    installation. Best-effort: a failed removal only costs disk.
    """
    for name in _HOME_INSTALL_ARTIFACTS:
        target = home / name
        try:
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
        except OSError as exc:
            _log.warning("Failed to prune %s from the hermes run home: %s", name, exc)


def _build_env(config: AgentConfig) -> dict[str, str]:
    """Build the subprocess env overlay carrying provider credentials.

    Raises:
        ConfigError: If ``config.provider`` names no known provider. Validation
            is unconditional so a typo fails here rather than on a keyless run
            that would otherwise reach hermes and answer from the wrong model.
    """
    overlay: dict[str, str] = {}
    if config.provider:
        spec = resolve_provider(config.provider)
        if config.api_key:
            for var in spec.api_key_envs:
                overlay[var] = config.api_key
    overlay.update(config.extra_env)
    return overlay


@AGENTS.register("hermes")
class HermesAgent(AgentHarness):
    """Hermes CLI agent harness driving the local ``hermes`` binary.

    The run-scoped home is never seeded from the user's ``~/.hermes``, so a
    benchmark run is reproducible and never picks up ambient developer state
    (nor the credentials in the user's ``.env``, which the run home would carry
    into the run's artifacts).

    Args:
        config: Agent configuration; ``target`` overrides the binary path.
    """

    def __init__(self, config: AgentConfig | None = None) -> None:
        AgentHarness.__init__(self, config)
        caps = self.config.capabilities
        self.rules = caps.rules
        self.mcp_servers = caps.mcp_servers
        self.skills = caps.skills

    def _resolve_hermes_bin(self) -> str:
        """Resolve the ``hermes`` binary path, preferring an explicit target."""
        if self.config.target:
            return os.path.expanduser(self.config.target)
        candidate = os.path.expanduser("~/.local/bin/hermes")
        return candidate if os.path.exists(candidate) else "hermes"

    def _prepare_config(self, run_dir: Path, mcp_servers: tuple[McpBinding, ...]) -> None:
        """Write the run-scoped ``config.yaml`` granting the run's MCP servers.

        The run home is created fresh per run and is never seeded from the
        user's ``~/.hermes`` — a benchmark run has to be reproducible and must
        not pick up ambient developer state — so the document is built outright
        rather than merged into whatever was already there.

        Also lays down the opt-out marker for hermes's bundled skill catalog, so
        the granted skills are the only ones the agent is offered beyond hermes's
        own essential ``hermes-agent`` skill, which the marker cannot remove.
        """
        (run_dir / _NO_BUNDLED_SKILLS_MARKER).touch()

        # Hermes otherwise sources ``<git root>/.hermes/skills`` and
        # ``<git root>/.agents/skills`` when the session starts inside a
        # checkout, which would hand the agent skills the run never granted.
        config_data: dict = {"skills": {"project_discovery": False}}

        servers = build_mcp_servers(mcp_servers)
        if servers:
            # MCP servers are spawned by hermes, so they inherit its env rather
            # than the harness's; pass the run's isolation vars through
            # explicitly or the servers fall back to the ambient ones.
            for key, value in mcp_isolation_env(self.config.extra_env).items():
                for entry in servers.values():
                    entry.setdefault("env", {})[key] = value
            config_data["mcp_servers"] = servers

        buffer = io.StringIO()
        _yaml.dump(config_data, buffer)
        (run_dir / _CONFIG_FILE).write_text(buffer.getvalue(), encoding="utf-8")

    def _build_command(self, prompt: str) -> list[str]:
        """Build the ``hermes chat`` argv for this run.

        Raises:
            ConfigError: If the resolved provider has no hermes equivalent.
        """
        # ``--query=<prompt>`` as one token: the prompt is attacker-influenced
        # task text, and a separate argv element starting with ``-`` would be
        # parsed as a flag.
        cmd = [self._resolve_hermes_bin(), "chat", f"--query={prompt}"]
        if self.config.model:
            cmd.extend(["-m", self.config.model])
        if self.config.provider:
            cmd.extend(["--provider", _hermes_provider(self.config.provider)])
        return cmd

    def _execute(self, prompt: str, workspace_path: Path | None = None) -> AgentResult:
        caps = self.config.capabilities

        with agent_workdir(workspace_path, prefix="hermes-run-") as workdir:
            hermes_home = workdir / _HERMES_HOME_DIRNAME
            hermes_home.mkdir(parents=True, exist_ok=True)
            self._prepare_config(hermes_home, caps.mcp_servers)
            if caps.skills.paths:
                materialize_skills(hermes_home / _SKILLS_DIRNAME, caps.skills.paths)

            env_overlay = _build_env(self.config)
            env_overlay["HERMES_HOME"] = str(hermes_home)
            db_path = hermes_home / _STATE_DB

            try:
                completed = run(
                    self._build_command(prepend_rules(caps.rules.text, prompt)),
                    check=False,
                    cwd=str(workdir),
                    timeout=self.config.timeout_sec,
                    extra_env=env_overlay,
                )
            except SubprocessError as exc:
                # With check=False the only SubprocessError left is the timeout.
                # The killed run still flushed whatever it completed, so report
                # that partial trajectory rather than discarding the run.
                trajectory, errors = extract_trajectory_from_db(db_path)
                tokens = extract_tokens_from_db(db_path)
                _prune_install_artifacts(hermes_home)
                return AgentResult(
                    output=(
                        f"Timeout expired.\n\n=== STDOUT ===\n{exc.stdout or ''}"
                        f"\n\n=== STDERR ===\n{exc.stderr or ''}"
                    ),
                    trajectory=trajectory,
                    tokens=tokens,
                    errors=[f"hermes agent timed out after {self.config.timeout_sec:g}s", *errors],
                    metadata={"timeout": True},
                )
            except OSError as exc:
                # Binary missing/not executable: same canonical all-None token
                # shape as every other path, so the row reads "unavailable".
                return AgentResult(
                    output=f"Error: hermes binary unavailable: {exc}",
                    trajectory=[],
                    tokens=empty_tokens(),
                    errors=[f"hermes binary unavailable: {exc}"],
                )

            errors: list[str] = []
            metadata: dict = {}
            if completed.returncode != 0:
                stderr = (completed.stderr or "").strip()
                errors.append(
                    f"hermes agent exited {completed.returncode}: {stderr or '<no stderr>'}"
                )
                metadata["returncode"] = completed.returncode

            trajectory, export_errors = extract_trajectory_from_db(db_path)
            errors.extend(export_errors)
            tokens = extract_tokens_from_db(db_path)
            if completed.returncode == 0 and not tokens["total"]:
                # hermes exits 0 on an unknown provider, a rejected API key, or
                # an API call whose retries all failed. Without this the run
                # looks like a genuinely bad answer instead of an infra failure,
                # because no model usage was ever recorded.
                stderr = (completed.stderr or "").strip()
                errors.append(
                    "hermes agent recorded no model usage despite exiting 0; "
                    f"the model was never reached: {stderr or '<no stderr>'}"
                )
            _prune_install_artifacts(hermes_home)

        return AgentResult(
            output=completed.stdout or "",
            trajectory=trajectory,
            tokens=tokens,
            errors=errors,
            metadata=metadata,
        )
