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

Capabilities use openclaw's native channels under a per-run dir: ``OPENCLAW_STATE_DIR``
(``<run>/state``, sessions + skills), ``mcp.servers`` and catalog entries for
models oc does not ship in ``<run>/openclaw.json`` (``OPENCLAW_CONFIG_PATH``),
rules prepended to the prompt, and the model key threaded into the provider env var.

Trajectory extraction runs ``oc sessions --json`` then ``oc sessions
export-trajectory`` and parses the bundle (:mod:`~.parsing`); an extraction miss
lands on ``AgentResult.errors``, never a silently empty trajectory.
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

from devops_bench.agents import sandbox
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
from devops_bench.agents.shared.vertex_env import vertex_location, vertex_project
from devops_bench.core import SubprocessError, get_logger
from devops_bench.core.errors import ConfigError, SandboxError
from devops_bench.core.model_providers import ProviderSpec, resolve_provider, sandbox_credential_env
from devops_bench.core.subprocess import run

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from devops_bench.agents.capabilities import McpBinding

__all__ = ["OpenClawAgent"]


def _node_version_key(bin_path: str) -> tuple[int, ...]:
    """Numeric sort key for an nvm bin path; non-numeric segments sort lowest."""
    version = os.path.basename(os.path.dirname(bin_path)).lstrip("v")
    return tuple(int(chunk) if chunk.isdigit() else -1 for chunk in version.split("."))


def _ensure_node_on_path(env_overlay: dict[str, str]) -> dict[str, str]:
    """Return ``env_overlay`` with the nvm Node bin dir prepended to ``PATH``.

    The extraction calls run ``oc`` without a shell, so on an nvm-managed host
    they would exit 127 and empty the trajectory. No-op when Node is on ``PATH``.
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

# The image ships its own oc on PATH; a host binary path means nothing inside.
_CONTAINER_OC_BIN = "oc"
# Per-run layout: ``state`` is oc's state root, ``openclaw.json`` the isolated config.
_OPENCLAW_STATE_DIRNAME = "state"
_OPENCLAW_SKILLS_DIRNAME = "skills"
_OPENCLAW_CONFIG_FILE = "openclaw.json"

# Bare model ids absent from oc's built-in catalog, per oc provider; registered
# per-run by :func:`_build_model_override`. Other providers pass through to oc.
# TODO(deferred): supported-model-name maintenance is tracked separately (#147).
_GEMINI_CATALOG_OVERRIDES = frozenset({"gemini-3.5-flash", "gemini-3.7-flash", "gemini-3.8-flash"})
_CATALOG_OVERRIDES: dict[str, frozenset[str]] = {
    "google": _GEMINI_CATALOG_OVERRIDES,
    "google-vertex": _GEMINI_CATALOG_OVERRIDES,
    "anthropic-vertex": frozenset(
        {"claude-fable-5-1", "claude-sonnet-5", "claude-fable-5", "claude-opus-5"}
    ),
}

# Satisfies oc's auth-profile gate on keyless Vertex; never sent, ADC carries the real credential.
_VERTEX_CREDENTIALS_MARKER = "gcp-vertex-credentials"

# A per-run provider entry replaces oc's built-in one, so it must pin ``api`` or
# oc falls back to the OpenAI transport. oc expands ``{location}`` itself.
_PROVIDER_TRANSPORT: dict[str, dict[str, str]] = {
    "google": {
        "api": "google-generative-ai",
    },
    "google-vertex": {
        "api": "google-vertex",
        "baseUrl": "https://{location}-aiplatform.googleapis.com",
    },
    "anthropic-vertex": {
        "api": "anthropic-messages",
        "baseUrl": "https://aiplatform.googleapis.com",
        "apiKey": _VERTEX_CREDENTIALS_MARKER,
    },
}
# node-fetch->native-fetch loader shim (see :func:`_write_node_fetch_shim`), under
# the state dir so the artifact diff does not attribute it to the agent.
_NODE_FETCH_SHIM_DIRNAME = "node-fetch-shim"

_NODE_FETCH_REGISTER_MJS = (
    "import { register } from 'node:module';\nregister('./hooks.mjs', import.meta.url);\n"
)

_NODE_FETCH_HOOKS_MJS = (
    "export async function resolve(specifier, context, next) {\n"
    "  if (specifier === 'node-fetch') {\n"
    "    return { url: new URL('./fetch.mjs', import.meta.url).href, shortCircuit: true };\n"
    "  }\n"
    "  return next(specifier, context);\n"
    "}\n"
)

_NODE_FETCH_FETCH_MJS = (
    "const f = (...a) => globalThis.fetch(...a);\n"
    "export default f;\n"
    "export const Headers = globalThis.Headers;\n"
    "export const Request = globalThis.Request;\n"
    "export const Response = globalThis.Response;\n"
)


def _write_node_fetch_shim(state_dir: Path) -> Path:
    """Write the node-fetch->native-fetch ESM loader shim under ``state_dir``.

    gaxios 7.3.1 (google-auth-library) dynamically imports ``node-fetch`` to reach
    the metadata server and throws because the image lacks it; a module hook
    (``NODE_OPTIONS``) resolves that one specifier to a shim over native ``fetch``.
    World-readable because the container's ``--user`` may not match this process.
    """
    shim_dir = state_dir / _NODE_FETCH_SHIM_DIRNAME
    shim_dir.mkdir(exist_ok=True)
    shim_dir.chmod(0o755)
    for name, content in (
        ("register.mjs", _NODE_FETCH_REGISTER_MJS),
        ("hooks.mjs", _NODE_FETCH_HOOKS_MJS),
        ("fetch.mjs", _NODE_FETCH_FETCH_MJS),
    ):
        path = shim_dir / name
        path.write_text(content)
        path.chmod(0o644)
    return shim_dir


def _oc_model_id(config: AgentConfig) -> str:
    """Resolve the canonical ``provider/model`` id ``oc agent --model`` expects.

    ``""`` when no model is configured. The provider segment is normalized via
    :func:`~devops_bench.core.model_providers.resolve_provider`; an unknown wire
    passes through for oc to validate.

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

    Written into the per-run ``openclaw.json`` rather than oc's shared config,
    which would race across runs. The provider comes from the resolved model id,
    so one id works on any pinned backend; auth flows from the env, not from here.

    Returns an empty dict when no model is set or oc already knows it.
    """
    model_id = _oc_model_id(config)
    if not model_id:
        return {}
    provider, _, bare = model_id.partition("/")
    if bare not in _CATALOG_OVERRIDES.get(provider, frozenset()):
        return {}
    # Fail loud rather than ship a transport-less entry (see _PROVIDER_TRANSPORT).
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

    Merges ``mcp.servers`` (command-bearing bindings) with the model catalog
    override; the key spaces are disjoint. Each MCP server gets the run's
    ``KUBECONFIG`` as explicit env, in container spelling when sandboxed.

    Returns:
        The config mapping, or an empty dict when nothing needs writing.
    """
    payload: dict = {}
    servers = build_mcp_servers(mcp_servers)
    if servers:
        kubeconfig = os.environ.get("KUBECONFIG")
        if config.sandbox is not None:
            kubeconfig = sandbox.CONTAINER_KUBECONFIG
        if kubeconfig:
            for entry in servers.values():
                entry.setdefault("env", {})["KUBECONFIG"] = kubeconfig
        payload["mcp"] = {"servers": servers}
    payload.update(_build_model_override(config))
    return payload


def _vertex_key_present(config: AgentConfig, spec: ProviderSpec) -> bool:
    """Whether the agent receives a real Vertex key; the ADC marker never counts as one."""
    env = {**os.environ, **config.extra_env}  # the precedence _build_env applies
    # A provider with no key var gets config.api_key nowhere, so it cannot count.
    configured = (config.api_key or "") if spec.api_key_envs else ""
    candidates = (configured, *(env.get(var, "") for var in spec.api_key_envs))
    return any(c.strip() and c.strip() != _VERTEX_CREDENTIALS_MARKER for c in candidates)


def _build_env(config: AgentConfig) -> dict[str, str]:
    """Build the env overlay that gives ``oc agent --local`` its model API key.

    ``config.api_key`` lands on the provider's key var(s) from
    :func:`~devops_bench.core.model_providers.resolve_provider`; keyless (ambient
    credential) backends get no key. A Vertex backend also gets its location,
    sandboxed or not. The caller adds the ``OPENCLAW_*`` paths.

    Raises:
        ConfigError: If ``config.provider`` is not a known provider.
    """
    # Resolve unconditionally so an unknown provider fails loud even keyless.
    spec = resolve_provider(config.provider)
    overlay: dict[str, str] = {
        var: os.environ[var] for var in spec.api_key_envs if os.environ.get(var)
    }
    if config.api_key:
        for var in spec.api_key_envs:
            overlay[var] = config.api_key
    if spec.backend == "vertex":
        # oc aborts without a location; the shared chain ends at "global".
        overlay["GOOGLE_CLOUD_LOCATION"] = vertex_location()
    if config.extra_env:
        overlay.update(config.extra_env)
    return overlay


def _sandbox_provider_env(config: AgentConfig, state_dir: Path) -> dict[str, str]:
    """Forward provider routing and give the container a model credential.

    Needs the task-completed ``config.sandbox``. A keyless Vertex run gets the
    metadata-emulator recipe (``sandbox_credential_env``, as gemini_cli does); a
    key crosses by value.
    """
    overlay = {
        name: os.environ[name]
        for name in (
            "GOOGLE_CLOUD_PROJECT",
            "GOOGLE_CLOUD_LOCATION",
            "GOOGLE_GENAI_USE_VERTEXAI",
            "GCP_PROJECT_ID",
            "GCP_VERTEX_LOCATION",
            "GOOGLE_CLOUD_API_KEY",
        )
        if name in os.environ
    }
    sandbox_spec = config.sandbox
    if sandbox_spec is None or sandbox_spec.workspace is None:
        raise SandboxError("_sandbox_provider_env needs the task-completed sandbox spec")
    register = _write_node_fetch_shim(state_dir) / "register.mjs"
    overlay["NODE_OPTIONS"] = f"--import={sandbox.container_path(sandbox_spec.workspace, register)}"
    spec = resolve_provider(config.provider)
    if spec.backend == "vertex" and not _vertex_key_present(config, spec):
        overlay.update(sandbox_credential_env(spec, project=vertex_project()))
    if spec.oc_provider == "anthropic-vertex":
        overlay["ANTHROPIC_VERTEX_USE_GCP_METADATA"] = "1"
        overlay.setdefault("GOOGLE_CLOUD_API_KEY", _VERTEX_CREDENTIALS_MARKER)
    return overlay


def _oc_model_flag(config: AgentConfig) -> str:
    """Return ``--model <id> `` for ``oc agent``, or ``""`` when no model is set.

    Per-run ``--model`` rather than the global ``oc models set``, which writes
    oc's shared config and races across runs; an invalid id fails via oc's exit.
    """
    model_id = _oc_model_id(config)
    if not model_id:
        return ""
    return f"--model {shlex.quote(model_id)} "


def _vertex_auth_profile_provider(config: AgentConfig) -> str | None:
    """The oc provider id a keyless Vertex run must seed a headless auth profile for, else ``None``.

    In current openclaw releases a per-agent auth-profile store gates the Vertex
    providers before ADC resolution, so a keyless run fails with ``ProviderAuthError``
    until ``oc models auth paste-api-key`` registers :data:`_VERTEX_CREDENTIALS_MARKER`.
    A real key, configured or in the environment, gets no profile: it would be shadowed.
    """
    try:
        spec = resolve_provider(config.provider)
    except ConfigError:
        return None
    if spec.backend != "vertex" or _vertex_key_present(config, spec):
        return None
    return spec.oc_provider


def _prepend_rules(rules_text: str, prompt: str) -> str:
    """Return ``prompt`` with ``rules_text`` prepended; blank rules pass it through."""
    if not rules_text or not rules_text.strip():
        return prompt
    return f"{rules_text.rstrip()}\n\n{prompt}"


def _build_local_command(config: AgentConfig, prompt: str, agent_name: str, oc_bin: str) -> str:
    """Build the ``bash -c`` command that runs ``oc agent --local``.

    Every interpolated value is ``shlex.quote``d; bash is needed so ``nvm.sh``
    can be sourced before ``oc`` runs.
    """
    quoted_oc = shlex.quote(oc_bin)
    auth_setup = ""
    auth_provider = _vertex_auth_profile_provider(config)
    if auth_provider:
        auth_setup = (
            f"printf '%s\\n' {shlex.quote(_VERTEX_CREDENTIALS_MARKER)} | "
            f"{quoted_oc} models auth paste-api-key "
            f"--provider {shlex.quote(auth_provider)} --agent {shlex.quote(agent_name)}; "
        )
    extra_flags_str = (
        " ".join(shlex.quote(f) for f in config.extra_flags) + " " if config.extra_flags else ""
    )
    return (
        # Source nvm for oc's Node runtime; an inherited NVM_DIR wins.
        'export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"; '
        '[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"; '
        f"{auth_setup}{quoted_oc} --log-level debug agent --local "
        f"--agent {shlex.quote(agent_name)} {_oc_model_flag(config)}"
        f"{extra_flags_str}-m {shlex.quote(prompt)}"
    )


@AGENTS.register("openclaw")
class OpenClawAgent(AgentHarness):
    """OpenClaw CLI agent harness driving the local ``oc`` binary.

    The binary comes from ``config.target``, then ``~/bin/oc``, then ``oc`` on
    ``PATH``; model and provider flow through the per-run ``oc agent --model``.
    ``__init__`` assigns ``rules``/``mcp_servers``/``skills`` so the agent
    satisfies the ``Supports*`` protocols.

    Args:
        config: Typed :class:`AgentConfig`; defaults are used when omitted.
        agent_name: ``oc`` agent profile; ``"main"`` exists in every config.
    """

    # The agent turn goes through run_agent_cmd. The post-run ``oc sessions`` /
    # export calls stay on the host: the state dir is on the bind mount, so the
    # host reads the same bytes without keeping a container alive.
    supports_sandbox = True

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

        Capabilities are laid down under ``workspace_path`` (or a temp dir this
        method owns) first: ``state/`` with skills, and ``openclaw.json`` for MCP.
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

            # Two overlays: the agent needs container spellings of the paths in
            # its env, the host-side extraction needs host spellings; same bytes,
            # the state dir is on the bind mount. Unsandboxed they are identical.
            agent_env = dict(env_overlay)
            agent_oc_bin = oc_bin
            spec = self.config.sandbox
            if spec is not None and spec.workspace is not None:
                agent_env = {**_sandbox_provider_env(self.config, state_dir), **agent_env}
                agent_env["OPENCLAW_STATE_DIR"] = sandbox.container_path(spec.workspace, state_dir)
                if "OPENCLAW_CONFIG_PATH" in agent_env:
                    agent_env["OPENCLAW_CONFIG_PATH"] = sandbox.container_path(
                        spec.workspace, agent_env["OPENCLAW_CONFIG_PATH"]
                    )
                agent_oc_bin = _CONTAINER_OC_BIN

            command = _build_local_command(self.config, final_prompt, self.agent_name, agent_oc_bin)

            # TODO(follow-up): a timeout SIGKILLs only the bash child and orphans the
            # oc/kubectl/MCP tree; run in its own process group and killpg instead.
            try:
                # bash -c as argv (never shell=True); every value is shlex.quoted.
                completed = self.run_agent_cmd(
                    ["/bin/bash", "-c", command],
                    cwd=str(workdir),
                    extra_env=agent_env,
                    check=False,
                    timeout=self.config.timeout_sec,
                    host_run=run,
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
        """Run ``oc sessions`` + ``export-trajectory`` against the run's state and parse the bundle.

        Returns:
            ``(trajectory, tokens, output_text, errors)``; ``output_text`` is the
            final answer from the bundle, ``""`` when absent.
        """
        errors: list[str] = []
        # These calls run oc without a shell, so make nvm's Node discoverable.
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
