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

"""OpenClaw CLI agent harness driving the ``oc`` binary (local-only).

Capability wiring is delivered through openclaw's native channels, laid down in
a per-run temp dir:

* **State isolation** — ``OPENCLAW_STATE_DIR`` points at ``<run>/state`` so
  sessions and the skills root live in the per-run dir.
* **MCP servers** — command-bearing bindings become ``mcp.servers`` entries in
  ``<run>/openclaw.json``, selected via ``OPENCLAW_CONFIG_PATH``.
* **Skills** — ``config.capabilities.skills.paths`` are materialized under
  ``<OPENCLAW_STATE_DIR>/skills/<name>/SKILL.md``.
* **Rules** — ``config.capabilities.rules.text`` is prepended to the prompt
  (the ``oc`` build has no dedicated system-prompt flag).
* **Model catalog** — models openclaw doesn't ship in its built-in catalog
  (e.g. ``gemini-3.5-flash``) are registered in the same per-run
  ``<run>/openclaw.json`` so a run never depends on a global
  ``oc models``/``configure-oc.sh`` step. The entry is written for whichever
  Google backend ``config.provider`` selects — ``google`` (google-genai) or
  ``google-vertex`` (Vertex AI).
* **Model auth** — ``config.api_key`` is threaded into the provider env var
  (``GEMINI_API_KEY``/``GOOGLE_CLOUD_API_KEY``/``ANTHROPIC_API_KEY``/...) that
  ``oc agent --local`` reads.

Trajectory extraction uses the official session commands documented in
``docs/openclaw/sessions.md``: run ``oc agent --local``, locate the single
session with ``oc sessions --json``, export it with
``oc sessions export-trajectory``, and parse the bundle. On any extraction miss
the failure is recorded on ``AgentResult.errors`` rather than returning a
silent-empty trajectory.

The bundle parsing lives in :mod:`~devops_bench.agents.cli.openclaw.parsing`.

``__init__`` assigns ``self.rules``, ``self.mcp_servers`` and ``self.skills``
from the granted bindings, so the agent structurally satisfies
``SupportsRules`` / ``SupportsMcp`` / ``SupportsSkills``.
"""

from __future__ import annotations

import glob
import json
import os
import shlex
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from devops_bench.agents.base import AGENTS, AgentHarness
from devops_bench.agents.cli.openclaw.parsing import (
    _pick_session_key,
    _read_export_bundle,
    _strip_ansi,
    parse_trajectory_export,
)
from devops_bench.agents.config import AgentConfig
from devops_bench.agents.result import AgentResult
from devops_bench.agents.shared.cli_capabilities import (
    agent_workdir,
    build_mcp_servers,
    materialize_skills,
)
from devops_bench.core import SubprocessError, get_logger
from devops_bench.core.errors import ConfigError
from devops_bench.core.model_providers import resolve_provider
from devops_bench.core.subprocess import run

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from devops_bench.agents.capabilities import McpBinding

__all__ = ["OpenClawAgent"]


def _node_version_key(bin_path: str) -> tuple[int, ...]:
    """Numeric sort key for an nvm bin path (``.../node/v18.17.0/bin`` -> ``(18, 17, 0)``).

    A plain string sort puts ``v18...`` before ``v8...``; comparing numeric
    tuples picks the truly newest version. Non-numeric segments (aliases,
    partial installs) sort lowest.
    """
    version = os.path.basename(os.path.dirname(bin_path)).lstrip("v")
    return tuple(int(chunk) if chunk.isdigit() else -1 for chunk in version.split("."))


def _ensure_node_on_path(env_overlay: dict[str, str]) -> dict[str, str]:
    """Return ``env_overlay`` with the nvm Node bin dir prepended to ``PATH``.

    The agent *turn* runs ``oc`` through a bash command that sources nvm, but the
    ``oc sessions`` / ``export-trajectory`` extraction calls run ``oc`` as a direct
    argv subprocess (``run()`` never uses a shell). On an nvm-managed host Node is
    not on the inherited ``PATH``, so those calls fail with
    ``exit 127: /usr/bin/env: 'node': No such file or directory`` and the
    trajectory comes back **silently empty** (deflating every tool/trajectory
    score). Prepend the nvm Node bin dir so the direct subprocess finds Node too.

    No-op when Node is already discoverable on ``PATH`` or nvm is absent.
    """
    if shutil.which("node"):
        return env_overlay
    nvm_dir = os.path.expanduser(os.environ.get("NVM_DIR") or "~/.nvm")
    bins = glob.glob(os.path.join(nvm_dir, "versions", "node", "*", "bin"))
    if not bins:
        return env_overlay
    node_bin = max(bins, key=_node_version_key)  # newest installed Node version
    merged = dict(env_overlay)
    existing = merged.get("PATH") or os.environ.get("PATH", "")
    merged["PATH"] = f"{node_bin}{os.pathsep}{existing}" if existing else node_bin
    return merged


_log = get_logger("agents.cli.openclaw.agent")

# Per-run layout under the temp working dir. ``state`` is openclaw's state root
# (sessions + the managed skills tree); ``openclaw.json`` is the isolated config
# carrying ``mcp.servers``.
_OPENCLAW_STATE_DIRNAME = "state"
_OPENCLAW_SKILLS_DIRNAME = "skills"
_OPENCLAW_CONFIG_FILE = "openclaw.json"

# Bare model ids (the part after ``provider/``) absent from openclaw's built-in
# catalog; the harness registers these per-run (see :func:`_build_model_override`).
# TODO(deferred): supported-model-name maintenance is tracked separately (#147).
_CATALOG_OVERRIDES: frozenset[str] = frozenset({"gemini-3.5-flash"})

# Transport each per-run provider entry must pin: such an entry *replaces* oc's
# built-in provider rather than merging, so without ``api`` oc falls back to the
# OpenAI transport and 401s. ``google`` needs no ``baseUrl``; for ``google-vertex``
# ``{location}`` is expanded by oc, so it stays literal here.
_PROVIDER_TRANSPORT: dict[str, dict[str, str]] = {
    "google": {
        "api": "google-generative-ai",
    },
    "google-vertex": {
        "api": "google-vertex",
        "baseUrl": "https://{location}-aiplatform.googleapis.com",
    },
}


def _oc_model_id(config: AgentConfig) -> str:
    """Resolve the canonical ``provider/model`` id ``oc agent --model`` expects.

    Returns ``""`` when no model is configured (oc's stored default is left
    untouched). The provider is normalized through the shared contract
    (:func:`~devops_bench.core.model_providers.resolve_provider`) so the openclaw
    id matches the rest of the pipeline (e.g. ``gemini`` → ``google``). A full id
    (``provider/model``) has its provider segment normalized too; an unknown wire
    passes through unchanged for oc to validate.

    >>> _oc_model_id(AgentConfig(model="gemini-2.5-pro", provider="gemini"))
    'google/gemini-2.5-pro'
    >>> _oc_model_id(AgentConfig(model="gemini/gemini-2.5-pro"))
    'google/gemini-2.5-pro'
    >>> _oc_model_id(AgentConfig(model="anthropic/claude-opus-4"))
    'anthropic/claude-opus-4'
    >>> _oc_model_id(AgentConfig(model="gpt-5", provider="openai"))
    'openai/gpt-5'
    >>> _oc_model_id(AgentConfig())
    ''
    """
    model = (config.model or "").strip()
    if not model:
        return ""
    if "/" in model:  # already a full oc id; normalize the provider segment
        wire, _, bare = model.partition("/")
        try:
            wire = resolve_provider(wire, default=wire).oc_provider
        except ConfigError:
            return model  # unknown wire: leave as-is, let oc validate
        return f"{wire}/{bare}"
    return f"{resolve_provider(config.provider).oc_provider}/{model}"


def _build_model_override(config: AgentConfig) -> dict:
    """Register a catalog entry for a model openclaw doesn't ship by default.

    openclaw resolves ``--model provider/id`` against its built-in catalog;
    ids absent from it (e.g. ``gemini-3.5-flash``) abort the run unless the
    provider's catalog is patched. Rather than depend on a global
    ``oc models``/``configure-oc.sh`` step — which races across parallel runs
    and mutates the operator's shared config — the harness writes the catalog
    entry into the per-run isolated ``openclaw.json`` selected via
    ``OPENCLAW_CONFIG_PATH``.

    The entry is provider-agnostic across Google's two backends: the provider is
    read from the resolved oc model id, so the *same* model id works on
    ``google`` (google-genai, API key) or ``google-vertex`` (Vertex AI, ADC) by
    flipping ``config.provider`` alone. Both pin their transport (see
    :data:`_PROVIDER_TRANSPORT`) because a per-run provider entry replaces oc's
    built-in one rather than merging into it.

    No auth is written here — it flows from the env: an API key
    (``GEMINI_API_KEY``/``GOOGLE_API_KEY``) for google-genai, or, when no key is
    set, the Vertex **ADC** path (the ``GOOGLE_CLOUD_API_KEY=gcp-vertex-credentials``
    marker → metadata-server credentials). So the override stands on its own for
    a keyless ADC run.

    Returns an empty dict when no model is configured or the model is already in
    oc's catalog (caller then writes no ``models``/``agents`` sections).
    """
    model_id = _oc_model_id(config)
    if not model_id:
        return {}
    provider, _, bare = model_id.partition("/")
    if bare not in _CATALOG_OVERRIDES:
        return {}
    # A per-run provider entry *replaces* oc's built-in one, so it must pin a
    # transport; without one oc falls back to the OpenAI transport and 401s. Fail
    # loud rather than ship a broken (transport-less) entry for a provider we have
    # not pinned.
    if provider not in _PROVIDER_TRANSPORT:
        raise ConfigError(
            f"openclaw catalog override {model_id!r} has no pinned transport for "
            f"provider {provider!r}; add it to _PROVIDER_TRANSPORT (known: "
            f"{', '.join(sorted(_PROVIDER_TRANSPORT))})"
        )
    provider_entry: dict = dict(_PROVIDER_TRANSPORT[provider])
    provider_entry["models"] = [{"id": bare, "name": bare}]
    return {
        "models": {"providers": {provider: provider_entry}},
        # Allowlist ``provider/id`` for the agent's per-run ``--model`` override.
        "agents": {"defaults": {"models": {model_id: {}}}},
    }


def _build_openclaw_config(config: AgentConfig, mcp_servers: tuple[McpBinding, ...]) -> dict:
    """Assemble the isolated ``openclaw.json`` payload for a run.

    Merges the two per-run config concerns the harness owns: command-bearing MCP
    bindings (``mcp.servers``) and a catalog entry for a model openclaw doesn't
    ship by default (``models``/``agents``; see :func:`_build_model_override`).
    Their key spaces are disjoint, so either, both, or neither may be present.

    Args:
        config: Resolved :class:`AgentConfig` (drives the model-catalog entry).
        mcp_servers: Bindings to render into ``mcp.servers`` (empty-command
            bindings are skipped by :func:`build_mcp_servers`).

    Returns:
        A config mapping, or an empty dict when there is neither a launchable MCP
        binding nor a model needing a catalog entry (caller then skips the config
        write and leaves ``OPENCLAW_CONFIG_PATH`` unset).

    Each MCP server entry inherits the run's ``KUBECONFIG`` (set by ``RunEnv``) as
    an explicit ``env`` so the MCP server (e.g. gke-mcp) reads the run-scoped
    cluster credentials directly instead of forcing the agent to re-fetch them.
    """
    payload: dict = {}
    servers = build_mcp_servers(mcp_servers)
    if servers:
        kubeconfig = os.environ.get("KUBECONFIG")
        if kubeconfig:
            for entry in servers.values():
                entry.setdefault("env", {})["KUBECONFIG"] = kubeconfig
        payload["mcp"] = {"servers": servers}
    payload.update(_build_model_override(config))
    return payload


def _build_env(config: AgentConfig) -> dict[str, str]:
    """Build the env overlay that gives ``oc agent --local`` its model API key.

    ``oc agent --local`` reads the model provider's API key from the shell. The
    benchmark's neutral ``config.api_key`` is mapped onto the provider-specific
    variable(s) openclaw expects; the model itself is never hardcoded (it flows
    from ``config.model`` via ``oc agent --model``).

    The key is routed onto the provider's API-key env var(s) from the shared
    contract (:func:`~devops_bench.core.model_providers.resolve_provider`). When
    ``config.api_key`` is unset the overlay carries no key — the **Vertex / ADC**
    path: ``google-vertex`` resolves credentials from the metadata-server
    Application Default Credentials at request time (the matrix exports the
    ``GOOGLE_CLOUD_API_KEY=gcp-vertex-credentials`` ADC marker), and the contract
    marks such backends keyless (empty ``api_key_envs``) so no key is ever forced
    onto them. A provided ``google-vertex`` key lands on ``GOOGLE_CLOUD_API_KEY``
    (the vertex transport), not ``GEMINI_API_KEY`` (google-genai).

    Args:
        config: Resolved :class:`AgentConfig` for this run.

    Returns:
        A mapping suitable for the subprocess environment overlay. The caller
        adds ``OPENCLAW_STATE_DIR`` / ``OPENCLAW_CONFIG_PATH`` on top.

    Raises:
        ConfigError: If ``config.provider`` is not a known provider.
    """
    # Resolve unconditionally so an unknown provider fails loud even on a keyless
    # (Vertex/ADC) run, not only when a key happens to be set.
    spec = resolve_provider(config.provider)
    overlay: dict[str, str] = {}
    if config.api_key:
        for var in spec.api_key_envs:
            overlay[var] = config.api_key
    if config.extra_env:
        overlay.update(config.extra_env)
    return overlay


def _oc_model_flag(config: AgentConfig) -> str:
    """Return ``--model <id> `` for ``oc agent``, or ``""`` when no model is set.

    The model is selected per-run with ``oc agent --model`` rather than the
    global ``oc models set``: the latter writes oc's *shared* config whenever no
    isolated ``OPENCLAW_CONFIG_PATH`` is in play (e.g. MCP off), which both races
    across concurrent runs and pollutes the operator's default model. An invalid
    id still aborts the run via ``oc agent``'s own non-zero exit, which surfaces
    on ``AgentResult.errors``.
    """
    model_id = _oc_model_id(config)
    if not model_id:
        return ""
    return f"--model {shlex.quote(model_id)} "


def _prepend_rules(rules_text: str, prompt: str) -> str:
    """Return ``prompt`` with ``rules_text`` prepended as an operator brief.

    Empty / whitespace-only rules pass the prompt through unchanged so a default
    :class:`AgentRules` is indistinguishable from "no preamble". A non-empty
    brief is separated from the prompt by a blank line.

    Args:
        rules_text: The bound rules text (``capabilities.rules.text``).
        prompt: The task prompt for this run.

    Returns:
        The combined string to hand to ``oc agent -m``.
    """
    if not rules_text or not rules_text.strip():
        return prompt
    return f"{rules_text.rstrip()}\n\n{prompt}"


def _build_local_command(config: AgentConfig, prompt: str, agent_name: str, oc_bin: str) -> str:
    """Build the bash command that sets the model and runs ``oc agent --local``.

    Every interpolated value is ``shlex.quote``d so prompts/agent names
    containing single quotes, spaces, or newlines neither break parsing nor
    inject commands. ``bash -c`` is required so ``nvm.sh`` can be sourced to
    expose the right Node toolchain before invoking ``oc`` (a no-op when Node is
    installed system-wide). Session state is isolated via ``OPENCLAW_STATE_DIR``
    (set by the caller's env overlay).

    Args:
        config: Resolved :class:`AgentConfig`.
        prompt: Task prompt for the agent (rules already prepended).
        agent_name: ``oc`` agent profile (e.g. ``"operator"``).
        oc_bin: Path to the ``oc`` binary.

    Returns:
        A single bash command string ready to run via ``/bin/bash -c`` through
        ``core.subprocess.run``.
    """
    quoted_oc = shlex.quote(oc_bin)
    return (
        # Source nvm so the Node-based oc binary's runtime is available. An
        # inherited NVM_DIR (custom install path) wins over the default.
        'export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"; '
        '[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"; '
        f"{quoted_oc} --log-level debug agent --local "
        f"--agent {shlex.quote(agent_name)} {_oc_model_flag(config)}-m {shlex.quote(prompt)}"
    )


@AGENTS.register("openclaw")
class OpenClawAgent(AgentHarness):
    """OpenClaw CLI agent harness driving the local ``oc`` binary.

    The binary path is resolved from ``config.target``, falling back to
    ``~/bin/oc`` and then ``"oc"`` on ``$PATH``. Model /
    provider flow from ``config.model`` / ``config.provider`` via the per-run
    ``oc agent --model`` flag — never hardcoded, never the global
    ``oc models set``.

    Capabilities are delivered through openclaw's native channels: MCP servers
    via an isolated ``OPENCLAW_CONFIG_PATH`` (``mcp.servers``), skills via
    ``<OPENCLAW_STATE_DIR>/skills``, and rules prepended to the prompt.
    ``__init__`` assigns ``self.rules``, ``self.mcp_servers`` and
    ``self.skills``, so the agent structurally satisfies ``SupportsRules`` /
    ``SupportsMcp`` / ``SupportsSkills``.

    Args:
        config: Typed :class:`AgentConfig`; defaults are used when omitted.
        agent_name: ``oc`` agent profile (defaults to ``"main"``, openclaw's
            built-in default agent, which exists in every config — including the
            per-run isolated one written for MCP).
    """

    def __init__(self, config: AgentConfig | None = None, *, agent_name: str = "main") -> None:
        AgentHarness.__init__(self, config)
        self.agent_name = agent_name
        caps = self.config.capabilities
        self.rules = caps.rules
        self.mcp_servers = caps.mcp_servers
        self.skills = caps.skills

    def _resolve_oc_bin(self) -> str:
        """Pick the ``oc`` binary path from config or fall back."""
        if self.config.target:
            return os.path.expanduser(self.config.target)
        candidate = os.path.expanduser("~/bin/oc")
        return candidate if os.path.exists(candidate) else "oc"

    def _execute(self, prompt: str, workspace_path: Path | None = None) -> AgentResult:
        """Run ``oc agent --local`` with the granted capabilities and extract the trajectory.

        The granted capabilities are laid down in ``workspace_path`` (the
        harness-owned per-run workspace, when supplied, else a throwaway temp
        dir this method owns) before invocation:

        * ``<run>/state`` — ``OPENCLAW_STATE_DIR`` (sessions + skills root).
        * ``<run>/state/skills/<name>/SKILL.md`` — one per discovered skill.
        * ``<run>/openclaw.json`` — ``mcp.servers`` for each command-bearing MCP
          binding, selected via ``OPENCLAW_CONFIG_PATH``.

        ``rules.text`` is prepended to the prompt; the model API key is threaded
        into the provider env var.
        """
        caps = self.config.capabilities
        oc_bin = self._resolve_oc_bin()
        final_prompt = _prepend_rules(caps.rules.text, prompt)

        with agent_workdir(workspace_path, prefix="oc-run-") as workdir:
            state_dir = workdir / _OPENCLAW_STATE_DIRNAME
            state_dir.mkdir(parents=True, exist_ok=True)

            materialize_skills(state_dir / _OPENCLAW_SKILLS_DIRNAME, caps.skills.paths)

            env_overlay = _build_env(self.config)
            env_overlay["OPENCLAW_STATE_DIR"] = str(state_dir)

            config_payload = _build_openclaw_config(self.config, caps.mcp_servers)
            if config_payload:
                config_path = workdir / _OPENCLAW_CONFIG_FILE
                config_path.write_text(json.dumps(config_payload, indent=2))
                env_overlay["OPENCLAW_CONFIG_PATH"] = str(config_path)

            command = _build_local_command(self.config, final_prompt, self.agent_name, oc_bin)

            # TODO(follow-up): on timeout this SIGKILLs only the bash child,
            # orphaning the oc/gcloud/kubectl/MCP process tree (which keeps
            # consuming Vertex quota). Run in its own process group
            # (start_new_session=True) and os.killpg(...) on timeout. Tracked as a
            # separate, more intrusive change to generalize across all CLI agents.
            try:
                # bash -c (as argv, never shell=True) so nvm.sh can be sourced;
                # every value interpolated into `command` is shlex.quoted.
                completed = run(
                    ["/bin/bash", "-c", command],
                    cwd=str(workdir),
                    extra_env=env_overlay,
                    check=False,
                    timeout=self.config.timeout_sec,
                )
            except SubprocessError:
                # With check=False the only SubprocessError here is a timeout.
                return AgentResult.errored(f"oc agent timed out after {self.config.timeout_sec}s")
            except OSError as exc:
                return AgentResult.errored(f"oc binary unavailable: {exc}")

            stdout_text = _strip_ansi(completed.stdout or "")
            errors: list[str] = []
            metadata: dict = {}

            if completed.returncode != 0:
                stderr = (completed.stderr or "").strip()
                errors.append(f"oc agent exited {completed.returncode}: {stderr or '<no stderr>'}")
                metadata["returncode"] = completed.returncode

            trajectory, tokens, bundle_output, export_errors = self._extract_trajectory(
                oc_bin, env_overlay
            )
            errors.extend(export_errors)

        # Bundle text is clean; bash stdout carries debug noise — fall back only if empty.
        output = bundle_output if bundle_output else stdout_text

        return AgentResult(
            output=output,
            trajectory=trajectory,
            tokens=tokens,
            errors=errors,
            metadata=metadata,
        )

    def _extract_trajectory(
        self, oc_bin: str, env_overlay: dict[str, str]
    ) -> tuple[list[dict], dict, str, list[str]]:
        """Run ``oc sessions`` + ``export-trajectory`` and parse the bundle.

        ``env_overlay`` carries ``OPENCLAW_STATE_DIR`` (and ``OPENCLAW_CONFIG_PATH``
        when MCP is configured) so the session commands read from the same
        isolated state the agent turn wrote to.

        Returns:
            A ``(trajectory, tokens, output_text, errors)`` tuple. ``output_text``
            is the agent's final answer parsed from the bundle's ``events.jsonl``
            (``model.completed.assistantTexts``) when present, else ``""``; the
            caller falls back to the ansi-stripped subprocess stdout when empty.
        """
        errors: list[str] = []
        # The agent turn sources nvm inside a bash command, but these extraction
        # calls run oc as a direct argv subprocess (no shell), so on nvm-managed
        # hosts Node isn't on PATH -> `oc sessions` exits 127 and the trajectory
        # is silently emptied. Make Node discoverable for the direct calls.
        env_overlay = _ensure_node_on_path(env_overlay)
        try:
            sessions = run(
                [oc_bin, "sessions", "--agent", self.agent_name, "--json"],
                check=False,
                timeout=self.config.timeout_sec,
                extra_env=env_overlay,
            )
        except SubprocessError as exc:
            errors.append(f"oc sessions failed: {exc}")
            return [], {}, "", errors
        except OSError as exc:
            errors.append(f"oc sessions: binary unavailable: {exc}")
            return [], {}, "", errors

        if sessions.returncode != 0:
            stderr = (sessions.stderr or "").strip()
            errors.append(f"oc sessions exited {sessions.returncode}: {stderr or '<no stderr>'}")
            return [], {}, "", errors

        key = _pick_session_key(sessions.stdout or "")
        if key is None:
            errors.append("oc sessions returned no session key")
            return [], {}, "", errors

        with tempfile.TemporaryDirectory(prefix="oc-export-") as tmpdir:
            workspace = Path(tmpdir)
            try:
                export = run(
                    [
                        oc_bin,
                        "sessions",
                        "export-trajectory",
                        "--session-key",
                        key,
                        "--workspace",
                        str(workspace),
                        "--json",
                    ],
                    check=False,
                    timeout=self.config.timeout_sec,
                    extra_env=env_overlay,
                )
            except SubprocessError as exc:
                errors.append(f"oc export-trajectory failed: {exc}")
                return [], {}, "", errors
            except OSError as exc:
                errors.append(f"oc export-trajectory: binary unavailable: {exc}")
                return [], {}, "", errors

            if export.returncode != 0:
                stderr = (export.stderr or "").strip()
                errors.append(
                    f"oc export-trajectory exited {export.returncode}: {stderr or '<no stderr>'}"
                )
                return [], {}, "", errors

            events_text, read_errors = _read_export_bundle(workspace)
            errors.extend(read_errors)
            if not events_text:
                return [], {}, "", errors

            trajectory, tokens, output_text, parse_errors = parse_trajectory_export(events_text)
            errors.extend(parse_errors)
            return trajectory, tokens, output_text, errors
