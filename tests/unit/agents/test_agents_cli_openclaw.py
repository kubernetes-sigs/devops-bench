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

"""Unit tests for devops_bench.agents.cli.openclaw."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from devops_bench.agents import AGENTS, AgentConfig
from devops_bench.agents.capabilities import (
    AgentRules,
    AllCapabilities,
    McpBinding,
    SkillBinding,
    SupportsMcp,
    SupportsRules,
    SupportsSkills,
)
from devops_bench.agents.cli.openclaw import OpenClawAgent, parse_trajectory_export
from devops_bench.agents.cli.openclaw import agent as oc_mod
from devops_bench.agents.cli.openclaw.agent import (
    _build_env,
    _build_local_command,
    _build_model_override,
    _build_openclaw_config,
    _oc_model_id,
)
from devops_bench.agents.cli.openclaw.parsing import _pick_session_key, _strip_ansi
from devops_bench.core.errors import ConfigError, SubprocessError


def _events(*entries: dict) -> str:
    return "\n".join(json.dumps(e) for e in entries) + "\n"


def _tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "type": "tool.call",
        "data": {"toolCallId": call_id, "name": name, "arguments": arguments},
    }


def _tool_result(
    call_id: str, text: str, *, is_error: bool = False, status: str = "completed"
) -> dict:
    return {
        "type": "tool.result",
        "data": {
            "message": {
                "toolCallId": call_id,
                "content": [{"type": "text", "text": text}],
                "details": {"status": status},
                "isError": is_error,
            }
        },
    }


# Mirrors the real ``oc sessions export-trajectory`` events.jsonl schema:
# dotted ``type`` with a nested ``data`` payload (captured from oc 2026.6.9).
SAMPLE_EVENTS = _events(
    _tool_call("1", "kubectl_get_pods", {"namespace": "default"}),
    _tool_result("1", "pod-a Running\n"),
    _tool_call("2", "kubectl_describe", {"resource": "pod/pod-a"}),
    _tool_result("2", "Phase: Running"),
    {
        "type": "model.completed",
        "data": {
            "usage": {"input": 5, "output": 10, "total": 15},
            "assistantTexts": ["All pods healthy."],
        },
    },
)


def test_parse_trajectory_export_folds_call_result_pairs() -> None:
    trajectory, tokens, output, errors = parse_trajectory_export(SAMPLE_EVENTS)
    assert errors == []
    assert tokens == {"input": 5, "output": 10, "total": 15}
    assert output == "All pods healthy."
    assert trajectory == [
        {
            "name": "kubectl_get_pods",
            "args": {"namespace": "default"},
            "result": "pod-a Running\n",
            "status": "completed",
        },
        {
            "name": "kubectl_describe",
            "args": {"resource": "pod/pod-a"},
            "result": "Phase: Running",
            "status": "completed",
        },
    ]


def test_parse_trajectory_export_sums_usage_across_turns() -> None:
    """Token usage is summed across every model.completed, not just the last.

    OpenClaw reports usage per turn (per model call); a multi-turn session that
    kept only the final ``model.completed`` would undercount to a single call.
    """
    blob = _events(
        {"type": "model.completed", "data": {"usage": {"input": 100, "output": 20, "total": 120}}},
        _tool_call("1", "kubectl_get_pods", {}),
        _tool_result("1", "pod-a Running"),
        {"type": "model.completed", "data": {"usage": {"input": 250, "output": 30, "total": 280}}},
        {
            "type": "model.completed",
            "data": {"usage": {"input": 75, "output": 15, "total": 90}, "assistantTexts": ["done"]},
        },
    )
    _trajectory, tokens, output, errors = parse_trajectory_export(blob)
    assert errors == []
    assert output == "done"
    assert tokens == {"input": 425, "output": 65, "total": 490}


def test_parse_trajectory_export_sums_nested_cost_breakdown() -> None:
    """Nested numeric mappings (e.g. a per-turn ``cost`` block) are summed too."""
    blob = _events(
        {"type": "model.completed", "data": {"usage": {"input": 10, "cost": {"total": 0.01}}}},
        {"type": "model.completed", "data": {"usage": {"input": 5, "cost": {"total": 0.02}}}},
    )
    _trajectory, tokens, _output, errors = parse_trajectory_export(blob)
    assert errors == []
    assert tokens["input"] == 15
    assert tokens["cost"]["total"] == pytest.approx(0.03)


def test_parse_trajectory_export_marks_failed_tool_result_as_error() -> None:
    """``isError`` (or ``details.status`` of error/failed) → status 'error'."""
    blob = _events(
        _tool_call("1", "exec", {"command": "false"}),
        _tool_result("1", "boom", is_error=True, status="error"),
    )
    trajectory, _tokens, _output, errors = parse_trajectory_export(blob)
    assert errors == []
    assert trajectory[0]["status"] == "error"


def test_parse_trajectory_export_output_falls_back_to_assistant_message() -> None:
    """When no model.completed.assistantTexts, output comes from assistant.message text."""
    blob = _events(
        {
            "type": "assistant.message",
            "data": {
                "message": {"role": "assistant", "content": [{"type": "text", "text": "done."}]}
            },
        },
        {"type": "model.completed", "data": {"usage": {"input": 1, "output": 2}}},
    )
    _trajectory, tokens, output, _errors = parse_trajectory_export(blob)
    assert tokens == {"input": 1, "output": 2}
    assert output == "done."


def test_parse_trajectory_export_surfaces_decode_errors() -> None:
    blob = "{not json}\n" + json.dumps(_tool_call("1", "x", {})) + "\n"
    trajectory, _tokens, _output, errors = parse_trajectory_export(blob)
    assert any("parse error" in m for m in errors)
    assert len(trajectory) == 1


def test_parse_trajectory_export_drops_unpaired_result_and_surfaces_error() -> None:
    """Orphan tool.result is dropped from trajectory, recorded on errors.

    Mirrors the API agent's ``_fold_with_extraction_errors`` policy and the
    Gemini ``parse_stream_json`` policy so every agent feeds the metrics seam
    one shape — only real ToolCalls the model issued ride on
    ``AgentResult.trajectory``; orphans are diagnostics, not trajectory entries.
    """
    blob = _events(_tool_result("ghost", "?"))
    trajectory, _tokens, _output, errors = parse_trajectory_export(blob)
    # Orphan must NOT appear in the canonical trajectory.
    assert trajectory == []
    # ...but MUST be surfaced on errors so the run is never silent-empty.
    assert any("without matching call" in m for m in errors)
    assert any("ghost" in m for m in errors)


def test_strip_ansi_removes_color_codes() -> None:
    assert _strip_ansi("\x1b[31mhello\x1b[0m") == "hello"


def test_oc_model_id_normalizes_provider_alias() -> None:
    assert _oc_model_id(AgentConfig(model="gemini-2.5-pro", provider="gemini")) == (
        "google/gemini-2.5-pro"
    )


def test_oc_model_id_preserves_full_id() -> None:
    assert _oc_model_id(AgentConfig(model="anthropic/claude-opus-4-7")) == (
        "anthropic/claude-opus-4-7"
    )


def test_oc_model_id_normalizes_full_id_provider_segment() -> None:
    # A full id whose wire is an alias is normalized through the contract.
    assert _oc_model_id(AgentConfig(model="gemini/gemini-2.5-pro")) == ("google/gemini-2.5-pro")


def test_oc_model_id_passes_through_unknown_full_id_wire() -> None:
    # An unrecognized wire is left for oc to validate, not rejected here.
    assert _oc_model_id(AgentConfig(model="mystery/some-model")) == "mystery/some-model"


def test_oc_model_id_returns_empty_when_no_model() -> None:
    assert _oc_model_id(AgentConfig()) == ""


def test_oc_model_id_defaults_to_google() -> None:
    assert _oc_model_id(AgentConfig(model="gemini-2.5-pro")) == "google/gemini-2.5-pro"


def test_build_local_command_quotes_inputs_and_passes_model_flag() -> None:
    cfg = AgentConfig(model="gemini-2.5-pro", provider="gemini")
    cmd = _build_local_command(cfg, "hi 'world'", "main", "/usr/local/bin/oc")
    # Prompt single-quote must be escaped, not break the shell line.
    assert "hi '\\''world'\\''" in cmd or "hi 'world'" not in cmd
    assert "NVM_DIR" in cmd  # nvm sourced for the node runtime
    # Per-run model override, not the global `oc models set` (no shared config write).
    assert "--model google/gemini-2.5-pro" in cmd
    assert "models set" not in cmd
    assert "agent --local" in cmd
    assert "--agent main" in cmd


def test_build_local_command_omits_model_flag_when_no_model_configured() -> None:
    cmd = _build_local_command(AgentConfig(), "prompt", "main", "/usr/local/bin/oc")
    assert "--model" not in cmd
    assert "models set" not in cmd


def test_pick_session_key_handles_top_level_list() -> None:
    payload = json.dumps([{"key": "agent:operator:abc", "model": "x"}])
    assert _pick_session_key(payload) == "agent:operator:abc"


def test_pick_session_key_handles_wrapper_dict() -> None:
    payload = json.dumps({"sessions": [{"key": "k1"}, {"key": "k2"}]})
    assert _pick_session_key(payload) == "k1"


def test_pick_session_key_returns_none_on_empty_or_invalid() -> None:
    assert _pick_session_key("") is None
    assert _pick_session_key("{}") is None
    assert _pick_session_key(json.dumps({"sessions": []})) is None
    assert _pick_session_key(json.dumps([{"no_key": "x"}])) is None


def test_openclaw_agent_registered_under_canonical_key() -> None:
    assert AGENTS.get("openclaw") is OpenClawAgent


def test_ensure_node_on_path_picks_numerically_newest_node(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """v18 must beat v8 even though "v18" < "v8" as strings."""
    for version in ("v8.9.0", "v18.17.0", "v9.11.2"):
        (tmp_path / "versions" / "node" / version / "bin").mkdir(parents=True)
    monkeypatch.setenv("NVM_DIR", str(tmp_path))
    monkeypatch.setattr(oc_mod.shutil, "which", lambda _cmd: None)

    merged = oc_mod._ensure_node_on_path({})

    first_path_entry = merged["PATH"].split(os.pathsep)[0]
    assert first_path_entry == str(tmp_path / "versions" / "node" / "v18.17.0" / "bin")


def _make_subprocess_result(
    stdout: str = "", stderr: str = "", returncode: int = 0
) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def _install_oc_run(
    monkeypatch: pytest.MonkeyPatch,
    fake_bash: Callable[..., Any],
    fake_core_run: Callable[..., Any] | None = None,
) -> None:
    """Install a fake ``core.subprocess.run`` on the agent module.

    The agent turn arrives as ``["/bin/bash", "-c", <command>]`` and is routed
    to ``fake_bash(<command>, **kwargs)`` so fakes keep asserting on the bash
    command string; any other argv is the direct ``oc`` extraction call and
    goes to ``fake_core_run`` (defaulting to an empty ``oc sessions``).
    """
    core = fake_core_run or (lambda argv, **kwargs: _make_subprocess_result(json.dumps([]), "", 0))

    def dispatch(argv, **kwargs):
        if argv[0] == "/bin/bash":
            return fake_bash(argv[2], **kwargs)
        return core(argv, **kwargs)

    monkeypatch.setattr(oc_mod, "run", dispatch)


def _bundle_writer(events_jsonl: str) -> Callable[..., Any]:
    """Build a fake core-subprocess.run that writes an export bundle's events.jsonl.

    ``oc sessions`` returns one session row; ``oc sessions export-trajectory``
    writes ``events.jsonl`` (the real trajectory filename) into the bundle dir
    under the ``--workspace`` it was handed.
    """
    sessions_payload = json.dumps(
        [{"key": "agent:operator:test", "model": "google/gemini-2.5-pro"}]
    )

    def fake_core_run(argv, **kwargs):
        if argv[1] == "sessions" and "export-trajectory" not in argv:
            return _make_subprocess_result(stdout=sessions_payload, returncode=0)
        if "export-trajectory" in argv:
            ws = Path(argv[argv.index("--workspace") + 1])
            export_root = ws / ".openclaw" / "trajectory-exports" / "openclaw-trajectory-x"
            export_root.mkdir(parents=True, exist_ok=True)
            (export_root / "events.jsonl").write_text(events_jsonl, encoding="utf-8")
            return _make_subprocess_result(stdout="exported", returncode=0)
        raise AssertionError(f"unexpected argv {argv}")

    return fake_core_run


def test_execute_happy_path_emits_canonical_trajectory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """oc agent succeeds, oc sessions yields one row, export-trajectory parses cleanly.

    The final answer comes from the bundle (``model.completed.assistantTexts``),
    not the noisy bash stdout.
    """

    def fake_bash(cmd, **kwargs):
        return _make_subprocess_result(stdout="OK\n", returncode=0)

    _install_oc_run(monkeypatch, fake_bash, _bundle_writer(SAMPLE_EVENTS))

    agent = OpenClawAgent(AgentConfig(target=str(tmp_path / "oc"), timeout_sec=30.0))
    result = agent.run("audit pods in default")
    assert result.errors == []
    assert len(result.trajectory) == 2
    assert result.trajectory[0]["name"] == "kubectl_get_pods"
    assert result.tokens == {"input": 5, "output": 10, "total": 15}
    assert result.output == "All pods healthy."


def test_execute_prefers_bundle_output_over_noisy_stdout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The agent's final answer (events.jsonl assistantTexts) must win over
    `oc --log-level debug` noise — otherwise the judge grades debug spew.
    """
    noisy_stdout = "[DEBUG] starting oc...\n[INFO] sessionFile=/tmp/.openclaw/...\n[DEBUG] turn 1\n"

    def fake_bash(cmd, **kwargs):
        return _make_subprocess_result(stdout=noisy_stdout, returncode=0)

    clean_answer = "All pods in `default` are Running."
    events = _events(
        _tool_call("1", "exec", {"command": "kubectl get pods"}),
        _tool_result("1", "pod-a Running"),
        {
            "type": "model.completed",
            "data": {"usage": {"input": 1, "output": 1}, "assistantTexts": [clean_answer]},
        },
    )
    _install_oc_run(monkeypatch, fake_bash, _bundle_writer(events))

    result = OpenClawAgent(AgentConfig(target=str(tmp_path / "oc"))).run("audit pods")
    assert result.output == clean_answer
    assert "[DEBUG]" not in result.output


def test_execute_falls_back_to_stdout_when_bundle_has_no_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No assistantTexts and no assistant.message in events → use stripped stdout."""
    events = _events(
        _tool_call("1", "exec", {"command": "ls"}),
        _tool_result("1", "file"),
        {"type": "model.completed", "data": {"usage": {"input": 1, "output": 1}}},
    )
    _install_oc_run(
        monkeypatch,
        lambda *a, **k: _make_subprocess_result(stdout="bare stdout answer", returncode=0),
        _bundle_writer(events),
    )

    result = OpenClawAgent(AgentConfig(target=str(tmp_path / "oc"))).run("p")
    assert result.output == "bare stdout answer"


def test_execute_records_when_sessions_returns_no_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_core_run(argv, **kwargs):
        # oc sessions returns empty.
        return _make_subprocess_result(stdout=json.dumps([]), returncode=0)

    _install_oc_run(
        monkeypatch, lambda *a, **k: _make_subprocess_result("ok", "", 0), fake_core_run
    )

    result = OpenClawAgent(AgentConfig(target=str(tmp_path / "oc"))).run("p")
    assert result.has_errors()
    assert any("no session key" in e for e in result.errors)


def test_execute_records_export_subprocess_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_core_run(argv, **kwargs):
        if "export-trajectory" in argv:
            raise SubprocessError(argv, returncode=1, stdout="", stderr="bad")
        return _make_subprocess_result(stdout=json.dumps([{"key": "k1"}]), returncode=0)

    _install_oc_run(
        monkeypatch, lambda *a, **k: _make_subprocess_result("ok", "", 0), fake_core_run
    )
    result = OpenClawAgent(AgentConfig(target=str(tmp_path / "oc"))).run("p")
    assert result.has_errors()
    assert any("export-trajectory failed" in e for e in result.errors)


def test_execute_records_bash_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_bash(cmd, **kwargs):
        # core.subprocess.run wraps TimeoutExpired in SubprocessError.
        raise SubprocessError(["/bin/bash", "-c", cmd], returncode=-1, stdout="", stderr="")

    _install_oc_run(monkeypatch, fake_bash)
    result = OpenClawAgent(AgentConfig(target=str(tmp_path / "oc"), timeout_sec=5.0)).run("p")
    assert result.has_errors()
    assert "timed out" in result.errors[0]
    assert result.trajectory == []


def test_execute_passes_timeout_to_bash(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict = {}

    def fake_bash(cmd, **kwargs):
        captured.update(kwargs)
        return _make_subprocess_result("ok", "", 0)

    _install_oc_run(monkeypatch, fake_bash)
    OpenClawAgent(AgentConfig(target=str(tmp_path / "oc"), timeout_sec=12.5)).run("p")
    assert captured["timeout"] == 12.5


# Tests for the now-deleted legacy surface — fail-fast if SSH transport returns.


def test_legacy_ssh_runner_is_gone() -> None:
    assert not hasattr(oc_mod, "run_openclaw_agent")


def test_legacy_local_runner_is_gone() -> None:
    assert not hasattr(oc_mod, "run_openclaw_agent_local")


# ---------------------------------------------------------------------------
# Capability negotiation: OpenClaw wires MCP + skills via oc's native channels
# ---------------------------------------------------------------------------


def test_openclaw_satisfies_mcp_skills_and_rules_protocols() -> None:
    """OpenClaw declares MCP, Skills and Rules: it writes ``mcp.servers`` into an
    isolated ``OPENCLAW_CONFIG_PATH``, materializes managed skills under
    ``<OPENCLAW_STATE_DIR>/skills``, and prepends the operator brief to the
    prompt."""
    agent = OpenClawAgent(AgentConfig())
    assert isinstance(agent, SupportsRules)
    assert isinstance(agent, SupportsMcp)
    assert isinstance(agent, SupportsSkills)


def test_openclaw_agent_mirrors_capability_bindings_onto_mixin_attributes() -> None:
    """The structural-Protocol attributes track the granted bindings."""
    binding = McpBinding(name="gke", command=("gke-mcp",), tools=("t",))
    skills = SkillBinding(paths=("/some/skills",))
    caps = AllCapabilities(
        mcp_servers=(binding,),
        skills=skills,
        rules=AgentRules(text="be a sre"),
    )
    agent = OpenClawAgent(AgentConfig(capabilities=caps))
    assert agent.mcp_servers == (binding,)
    assert agent.skills == skills
    assert agent.rules == AgentRules(text="be a sre")


def test_openclaw_agent_mirrors_rules_binding_onto_mixin_attribute() -> None:
    caps = AllCapabilities(rules=AgentRules(text="be precise"))
    agent = OpenClawAgent(AgentConfig(capabilities=caps))
    assert agent.rules == AgentRules(text="be precise")


# ---------------------------------------------------------------------------
# Rules delivery: the bound text actually reaches the spawned `oc` command.
# ---------------------------------------------------------------------------


def test_prepend_rules_passes_prompt_through_when_rules_empty() -> None:
    from devops_bench.agents.cli.openclaw.agent import _prepend_rules

    assert _prepend_rules("", "do the thing") == "do the thing"
    assert _prepend_rules("   \n  ", "do the thing") == "do the thing"


def test_prepend_rules_separates_brief_from_prompt_with_blank_line() -> None:
    from devops_bench.agents.cli.openclaw.agent import _prepend_rules

    assert _prepend_rules("be careful", "audit pods") == "be careful\n\naudit pods"


def test_execute_prepends_bound_rules_to_oc_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The rules text must land inside the bash command string the agent
    spawns — specifically inside the ``-m '<prompt>'`` segment that ``oc
    agent`` reads. We capture the command and assert both the original prompt
    and the rules text are present, with the rules ahead of the prompt."""
    captured: dict = {}

    def fake_bash(cmd, **kwargs):
        captured["cmd"] = cmd
        return _make_subprocess_result(stdout="ok", returncode=0)

    _install_oc_run(monkeypatch, fake_bash)

    caps = AllCapabilities(rules=AgentRules(text="you are a precise SRE"))
    OpenClawAgent(AgentConfig(target=str(tmp_path / "oc"), capabilities=caps)).run(
        "audit pods in default"
    )

    cmd = captured["cmd"]
    assert "you are a precise SRE" in cmd, "rules text must reach the spawned oc command"
    assert "audit pods in default" in cmd, "task prompt must still reach oc"
    # The rules brief must precede the task prompt — order matters because the
    # model reads it as the leading context.
    assert cmd.index("you are a precise SRE") < cmd.index("audit pods in default")


def test_execute_does_not_prepend_rules_when_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With default (empty) rules, the prompt reaches oc unchanged — no
    accidental blank-line prefix that would shift the model's attention."""
    captured: dict = {}

    def fake_bash(cmd, **kwargs):
        captured["cmd"] = cmd
        return _make_subprocess_result(stdout="ok", returncode=0)

    _install_oc_run(monkeypatch, fake_bash)

    OpenClawAgent(AgentConfig(target=str(tmp_path / "oc"))).run("just the prompt")
    cmd = captured["cmd"]
    # The agent shlex-quotes the prompt; "just the prompt" appears verbatim
    # inside single quotes in the `-m` segment — and no extra blank-line
    # prefix surrounds it.
    assert "-m 'just the prompt'" in cmd


# ---------------------------------------------------------------------------
# MCP server wiring: mcp.servers reach an isolated OPENCLAW_CONFIG_PATH.
# ---------------------------------------------------------------------------


def test_build_openclaw_config_wraps_servers_under_mcp() -> None:
    """A launchable binding renders under the ``mcp.servers`` config path."""
    cfg = _build_openclaw_config(AgentConfig(), (McpBinding(name="gke", command=("gke-mcp",)),))
    assert cfg == {"mcp": {"servers": {"gke": {"command": "gke-mcp"}}}}


def test_build_openclaw_config_empty_without_launchable_server_or_override() -> None:
    """No MCP binding and a catalog-known model → empty config (caller skips)."""
    assert _build_openclaw_config(AgentConfig(), ()) == {}
    assert (
        _build_openclaw_config(
            AgentConfig(model="gemini-3.1-pro-preview"),
            (McpBinding(name="b", command=(), tools=("t",)),),
        )
        == {}
    )


def test_build_openclaw_config_merges_mcp_and_model_override() -> None:
    """MCP servers and a model-catalog entry coexist (disjoint key spaces)."""
    cfg = _build_openclaw_config(
        AgentConfig(model="gemini-3.5-flash", provider="google"),
        (McpBinding(name="gke", command=("gke-mcp",)),),
    )
    assert cfg["mcp"] == {"servers": {"gke": {"command": "gke-mcp"}}}
    assert cfg["models"]["providers"]["google"]["models"] == [
        {"id": "gemini-3.5-flash", "name": "gemini-3.5-flash"}
    ]
    assert cfg["agents"]["defaults"]["models"] == {"google/gemini-3.5-flash": {}}


# ---------------------------------------------------------------------------
# Model catalog override: models oc doesn't ship by default get registered in
# the per-run isolated config, for both google-genai and google-vertex.
# ---------------------------------------------------------------------------


def test_model_override_empty_for_catalog_known_model() -> None:
    """A model already in oc's catalog needs no override."""
    assert _build_model_override(AgentConfig(model="gemini-3.1-pro-preview")) == {}


def test_model_override_empty_when_no_model() -> None:
    assert _build_model_override(AgentConfig()) == {}


def test_model_override_genai_pins_generative_ai_transport() -> None:
    """google-genai: the entry pins ``api: google-generative-ai`` so oc routes it
    through the google-genai transport (a per-run provider entry replaces oc's
    built-in one, so the transport must be carried) and needs no ``baseUrl``.
    Allowlists ``google/<model>``."""
    override = _build_model_override(AgentConfig(model="gemini-3.5-flash", provider="google"))
    google = override["models"]["providers"]["google"]
    assert google["api"] == "google-generative-ai"
    assert "baseUrl" not in google
    assert google["models"] == [{"id": "gemini-3.5-flash", "name": "gemini-3.5-flash"}]
    assert override["agents"]["defaults"]["models"] == {"google/gemini-3.5-flash": {}}


def test_model_override_gemini_alias_normalizes_to_google() -> None:
    """``provider=gemini`` resolves to the ``google`` provider (id alias)."""
    override = _build_model_override(AgentConfig(model="gemini-3.5-flash", provider="gemini"))
    assert "google" in override["models"]["providers"]
    assert override["agents"]["defaults"]["models"] == {"google/gemini-3.5-flash": {}}


def test_model_override_vertex_pins_transport_and_allowlists() -> None:
    """google-vertex: the entry pins ``api``/``baseUrl`` so oc uses the vertex
    transport (not the OpenAI fallback), and allowlists ``google-vertex/<model>``."""
    override = _build_model_override(
        AgentConfig(model="gemini-3.5-flash", provider="google-vertex")
    )
    vertex = override["models"]["providers"]["google-vertex"]
    assert vertex["api"] == "google-vertex"
    assert vertex["baseUrl"] == "https://{location}-aiplatform.googleapis.com"
    assert vertex["models"] == [{"id": "gemini-3.5-flash", "name": "gemini-3.5-flash"}]
    assert override["agents"]["defaults"]["models"] == {"google-vertex/gemini-3.5-flash": {}}


def test_model_override_vertex_needs_no_api_key() -> None:
    """The override is independent of ``api_key`` — a keyless ADC vertex run still
    gets its catalog entry (auth is the metadata-server ADC, set outside)."""
    override = _build_model_override(
        AgentConfig(model="gemini-3.5-flash", provider="google-vertex", api_key=None)
    )
    assert override["agents"]["defaults"]["models"] == {"google-vertex/gemini-3.5-flash": {}}
    assert _build_env(AgentConfig(model="gemini-3.5-flash", provider="google-vertex")) == {}


def test_build_env_threads_api_key_by_provider() -> None:
    """``config.api_key`` lands on the provider-specific env var(s)."""
    google = _build_env(AgentConfig(api_key="k", provider="google"))
    assert google["GEMINI_API_KEY"] == "k" and google["GOOGLE_API_KEY"] == "k"
    anthropic = _build_env(AgentConfig(api_key="k", provider="anthropic"))
    assert anthropic == {"ANTHROPIC_API_KEY": "k"}
    assert _build_env(AgentConfig()) == {}


def test_build_env_routes_vertex_key_to_cloud_api_key() -> None:
    """A ``google-vertex`` key reaches ``GOOGLE_CLOUD_API_KEY`` (the vertex
    transport's var), not ``GEMINI_API_KEY`` (the google-genai one)."""
    vertex = _build_env(AgentConfig(api_key="marker", provider="google-vertex"))
    assert vertex == {"GOOGLE_CLOUD_API_KEY": "marker"}


def test_build_env_unknown_provider_raises() -> None:
    """An unknown provider fails loud instead of silently writing a Gemini key."""
    with pytest.raises(ConfigError):
        _build_env(AgentConfig(api_key="k", provider="mystery"))


def test_build_env_unknown_provider_raises_even_when_keyless() -> None:
    """Validation is unconditional — a typoed provider fails loud on a keyless
    (Vertex/ADC) run too, not only when a key is set."""
    with pytest.raises(ConfigError):
        _build_env(AgentConfig(provider="google-vertyx"))


def test_model_override_raises_for_unpinned_transport() -> None:
    """A catalog-override model whose provider has no pinned transport fails loud
    rather than shipping a transport-less entry (which would 401 via the OpenAI
    fallback). A full-id with an unknown wire reaches this path."""
    with pytest.raises(ConfigError):
        _build_model_override(AgentConfig(model="mystery/gemini-3.5-flash"))


def _empty_sessions_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
    """Core-subprocess.run stub: ``oc sessions`` returns no rows."""
    return _make_subprocess_result(stdout=json.dumps([]), returncode=0)


def test_execute_writes_mcp_servers_into_isolated_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A command-bearing MCP binding lands in ``<cwd>/openclaw.json`` and
    ``OPENCLAW_CONFIG_PATH`` is pointed at it (read inside the fake to beat
    temp-dir cleanup)."""
    captured: dict = {}

    def fake_bash(cmd, **kwargs):
        env = kwargs.get("extra_env") or {}
        cfg_path = env.get("OPENCLAW_CONFIG_PATH")
        captured["cfg_path"] = cfg_path
        captured["config"] = (
            json.loads(Path(cfg_path).read_text()) if cfg_path and Path(cfg_path).exists() else None
        )
        return _make_subprocess_result(stdout="ok", returncode=0)

    _install_oc_run(monkeypatch, fake_bash, _empty_sessions_run)
    caps = AllCapabilities(
        mcp_servers=(McpBinding(name="gke", command=("gke-mcp",), tools=("mcp_gke_x",)),),
    )
    OpenClawAgent(AgentConfig(target=str(tmp_path / "oc"), capabilities=caps)).run("p")
    assert captured["cfg_path"], "OPENCLAW_CONFIG_PATH must be set when MCP is bound"
    assert captured["config"] == {"mcp": {"servers": {"gke": {"command": "gke-mcp"}}}}


def test_execute_writes_no_config_when_no_launchable_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No command-bearing MCP binding and a catalog-known model → no isolated
    config, env var unset."""
    captured: dict = {}

    def fake_bash(cmd, **kwargs):
        env = kwargs.get("extra_env") or {}
        captured["has_cfg"] = "OPENCLAW_CONFIG_PATH" in env
        return _make_subprocess_result(stdout="ok", returncode=0)

    _install_oc_run(monkeypatch, fake_bash, _empty_sessions_run)
    caps = AllCapabilities(
        mcp_servers=(McpBinding(name="builtin", command=(), tools=("alpha",)),),
    )
    OpenClawAgent(
        AgentConfig(
            target=str(tmp_path / "oc"),
            model="gemini-3.1-pro-preview",
            capabilities=caps,
        )
    ).run("p")
    assert captured["has_cfg"] is False


def test_execute_writes_model_override_config_without_mcp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A model absent from oc's catalog gets an isolated config + OPENCLAW_CONFIG_PATH
    even with no MCP server — and works keyless (vertex/ADC)."""
    for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_CLOUD_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    captured: dict = {}

    def fake_bash(cmd, **kwargs):
        env = kwargs.get("extra_env") or {}
        cfg_path = env.get("OPENCLAW_CONFIG_PATH")
        captured["cfg_path"] = cfg_path
        captured["config"] = (
            json.loads(Path(cfg_path).read_text()) if cfg_path and Path(cfg_path).exists() else None
        )
        # ADC path: no API key threaded into the subprocess env.
        captured["has_key"] = any(
            k in env for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_CLOUD_API_KEY")
        )
        return _make_subprocess_result(stdout="ok", returncode=0)

    _install_oc_run(monkeypatch, fake_bash, _empty_sessions_run)
    OpenClawAgent(
        AgentConfig(
            target=str(tmp_path / "oc"),
            model="gemini-3.5-flash",
            provider="google-vertex",
        )
    ).run("p")
    assert captured["cfg_path"], "OPENCLAW_CONFIG_PATH must be set for a catalog override"
    vertex = captured["config"]["models"]["providers"]["google-vertex"]
    assert vertex["api"] == "google-vertex"
    assert captured["config"]["agents"]["defaults"]["models"] == {
        "google-vertex/gemini-3.5-flash": {}
    }
    assert captured["has_key"] is False


def test_execute_isolates_state_dir_and_drops_global_session_wipe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``OPENCLAW_STATE_DIR`` points under the per-run cwd and the old global
    ``rm -rf ~/.openclaw/.../sessions`` wipe is gone (state is fresh per run)."""
    captured: dict = {}

    def fake_bash(cmd, **kwargs):
        env = kwargs.get("extra_env") or {}
        captured["state_dir"] = env.get("OPENCLAW_STATE_DIR")
        captured["cwd"] = kwargs.get("cwd")
        captured["cmd"] = cmd
        return _make_subprocess_result(stdout="ok", returncode=0)

    _install_oc_run(monkeypatch, fake_bash, _empty_sessions_run)
    OpenClawAgent(AgentConfig(target=str(tmp_path / "oc"))).run("p")
    assert captured["state_dir"] == os.path.join(captured["cwd"], "state")
    assert "rm -rf" not in captured["cmd"]


def test_execute_threads_api_key_into_subprocess_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The model API key reaches the spawned ``oc`` process env."""
    captured: dict = {}

    def fake_bash(cmd, **kwargs):
        captured["env"] = kwargs.get("extra_env") or {}
        return _make_subprocess_result(stdout="ok", returncode=0)

    _install_oc_run(monkeypatch, fake_bash, _empty_sessions_run)
    cfg = AgentConfig(target=str(tmp_path / "oc"), api_key="secret", provider="google")
    OpenClawAgent(cfg).run("p")
    assert captured["env"]["GEMINI_API_KEY"] == "secret"
    assert captured["env"]["GOOGLE_API_KEY"] == "secret"


def test_execute_materializes_skills_into_state_skills_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bound skill paths are copied to ``<cwd>/state/skills/<name>/SKILL.md``."""
    src = tmp_path / "skills" / "my-skill"
    src.mkdir(parents=True)
    skill_text = "---\nname: my-skill\ndescription: do things\n---\nbody\n"
    (src / "SKILL.md").write_text(skill_text)

    captured: dict = {}

    def fake_bash(cmd, **kwargs):
        cwd = kwargs.get("cwd")
        skill_path = os.path.join(cwd, "state", "skills", "my-skill", "SKILL.md")
        captured["exists"] = os.path.exists(skill_path)
        captured["text"] = Path(skill_path).read_text() if captured["exists"] else None
        return _make_subprocess_result(stdout="ok", returncode=0)

    _install_oc_run(monkeypatch, fake_bash, _empty_sessions_run)
    caps = AllCapabilities(skills=SkillBinding(paths=(str(tmp_path / "skills"),)))
    OpenClawAgent(AgentConfig(target=str(tmp_path / "oc"), capabilities=caps)).run("p")
    assert captured["exists"], "skill must be materialized before subprocess"
    assert captured["text"] == skill_text


def test_execute_warns_and_skips_missing_skill_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-existent skill path is skipped (no crash, empty skills root)."""
    captured: dict = {}

    def fake_bash(cmd, **kwargs):
        cwd = kwargs.get("cwd")
        skills_root = os.path.join(cwd, "state", "skills")
        captured["entries"] = sorted(os.listdir(skills_root)) if os.path.isdir(skills_root) else []
        return _make_subprocess_result(stdout="ok", returncode=0)

    _install_oc_run(monkeypatch, fake_bash, _empty_sessions_run)
    caps = AllCapabilities(skills=SkillBinding(paths=("/no/such/skills/dir",)))
    OpenClawAgent(AgentConfig(target=str(tmp_path / "oc"), capabilities=caps)).run("p")
    assert captured["entries"] == []


def test_execute_cleans_up_temp_working_dir_after_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The per-run temp dir (state, config, skills) is removed after _execute."""
    captured: dict = {}

    def fake_bash(cmd, **kwargs):
        captured["cwd"] = kwargs.get("cwd")
        return _make_subprocess_result(stdout="ok", returncode=0)

    _install_oc_run(monkeypatch, fake_bash, _empty_sessions_run)
    OpenClawAgent(AgentConfig(target=str(tmp_path / "oc"))).run("p")
    assert captured["cwd"] is not None
    assert not os.path.exists(captured["cwd"])
