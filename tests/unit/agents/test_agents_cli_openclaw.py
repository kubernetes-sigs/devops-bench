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
from typing import Any, NoReturn

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
from devops_bench.agents.cli.openclaw import parsing as parsing_mod
from devops_bench.agents.cli.openclaw.agent import (
    _build_env,
    _build_local_command,
    _build_model_override,
    _build_openclaw_config,
    _oc_model_id,
)
from devops_bench.agents.cli.openclaw.parsing import (
    _pick_session_key,
    _read_export_bundle,
    _strip_ansi,
)
from devops_bench.agents.sandbox import SandboxSpec
from devops_bench.core import is_placeholder_output
from devops_bench.core.errors import ConfigError, SandboxError, SubprocessError


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


# Mirrors the ``oc sessions export-trajectory`` events.jsonl schema: dotted ``type``
# with a nested ``data`` payload.
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
    """Token usage is summed across every model.completed, not just the last turn."""
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


def test_parse_trajectory_export_placeholder_final_text_falls_back() -> None:
    """A redacted final answer (live shape from run_20260911_172304) yields its assistant.message."""
    blob = _events(
        {
            "type": "assistant.message",
            "data": {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Deployed hello-app; 3/3 pods Ready."}],
                }
            },
        },
        {
            "type": "model.completed",
            "data": {
                "usage": {"input": 1, "output": 2},
                "assistantTexts": ["[Malformed diagnostic JSON redacted]"],
            },
        },
    )
    _trajectory, _tokens, output, errors = parse_trajectory_export(blob)
    assert errors == []
    assert output == "Deployed hello-app; 3/3 pods Ready."


def test_parse_trajectory_export_redacted_turn_recovers_its_own_message_not_an_earlier_turn() -> (
    None
):
    """A real earlier turn must not shadow the redacted final turn's assistant.message."""
    blob = _events(
        {"type": "model.completed", "data": {"assistantTexts": ["Applying the manifest now."]}},
        {
            "type": "assistant.message",
            "data": {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Deployed hello-app; 3/3 pods Ready."}],
                }
            },
        },
        {
            "type": "model.completed",
            "data": {"assistantTexts": ["[Malformed diagnostic JSON redacted]"]},
        },
    )
    _trajectory, _tokens, output, _errors = parse_trajectory_export(blob)
    assert output == "Deployed hello-app; 3/3 pods Ready."


def test_parse_trajectory_export_redacted_final_turn_does_not_promote_an_earlier_turn() -> None:
    """A redacted last turn with nothing of its own is missing, not the previous turn's narration."""
    blob = _events(
        {"type": "model.completed", "data": {"assistantTexts": ["Applying the manifest now."]}},
        {
            "type": "model.completed",
            "data": {"assistantTexts": ["[Oversized diagnostic JSON redacted]"]},
        },
    )
    _trajectory, _tokens, output, _errors = parse_trajectory_export(blob)
    assert output == "[Oversized diagnostic JSON redacted]"


def test_parse_trajectory_export_real_turn_after_a_redacted_one_wins() -> None:
    blob = _events(
        {
            "type": "model.completed",
            "data": {"assistantTexts": ["[Oversized diagnostic JSON redacted]"]},
        },
        {"type": "model.completed", "data": {"assistantTexts": ["Rollout complete."]}},
    )
    _trajectory, _tokens, output, _errors = parse_trajectory_export(blob)
    assert output == "Rollout complete."


def test_parse_trajectory_export_all_placeholders_returns_the_placeholder() -> None:
    """With nothing real to recover the placeholder stands, so the scoring layer withholds it."""
    blob = _events(
        {
            "type": "assistant.message",
            "data": {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "[Malformed diagnostic JSON redacted]"}],
                }
            },
        },
        {
            "type": "model.completed",
            "data": {"assistantTexts": ["[Malformed diagnostic JSON redacted]"]},
        },
    )
    _trajectory, _tokens, output, _errors = parse_trajectory_export(blob)
    assert output == "[Malformed diagnostic JSON redacted]"
    assert is_placeholder_output(output)


def test_parse_trajectory_export_filters_assistant_message_parts_individually() -> None:
    """Placeholder parts are dropped before the message's parts are joined."""
    blob = _events(
        {
            "type": "assistant.message",
            "data": {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "[Malformed diagnostic JSON redacted]"},
                        {"type": "text", "text": "Rollout complete."},
                        {"type": "text", "text": "[Oversized diagnostic JSON redacted]"},
                    ],
                }
            },
        },
        {"type": "model.completed", "data": {"assistantTexts": []}},
    )
    _trajectory, _tokens, output, _errors = parse_trajectory_export(blob)
    assert output == "Rollout complete."


def test_parse_trajectory_export_warns_once_per_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(parsing_mod._log, "warning", lambda msg, *args: warnings.append(msg % args))
    blob = _events(
        {
            "type": "model.completed",
            "data": {"assistantTexts": ["[Malformed diagnostic JSON redacted]"]},
        },
        {
            "type": "model.completed",
            "data": {"assistantTexts": ["[Oversized diagnostic JSON redacted]"]},
        },
    )
    parse_trajectory_export(blob)
    assert warnings == ["dropped 2 redaction placeholder(s) from the export bundle"]


def test_parse_trajectory_export_answer_quoting_a_placeholder_is_kept() -> None:
    """Only a whole-string placeholder is dropped; an answer about one survives."""
    text = "Retried after seeing '[Malformed diagnostic JSON redacted]' in the log; done."
    blob = _events({"type": "model.completed", "data": {"assistantTexts": [text]}})
    _trajectory, _tokens, output, _errors = parse_trajectory_export(blob)
    assert output == text


def test_parse_trajectory_export_surfaces_decode_errors() -> None:
    blob = "{not json}\n" + json.dumps(_tool_call("1", "x", {})) + "\n"
    trajectory, _tokens, _output, errors = parse_trajectory_export(blob)
    assert any("parse error" in m for m in errors)
    assert len(trajectory) == 1


def test_parse_trajectory_export_drops_unpaired_result_and_surfaces_error() -> None:
    """An orphan tool.result is dropped from the trajectory and recorded on errors, as
    the other agents do, so the metrics seam sees one shape."""
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


def test_effective_provider_follows_a_full_model_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no provider configured, every auth decision follows the id's provider segment."""
    monkeypatch.delenv("GOOGLE_CLOUD_API_KEY", raising=False)
    cfg = AgentConfig(model="google-vertex/gemini-3.8-flash")
    assert oc_mod._effective_provider(cfg) == "google-vertex"
    assert "GOOGLE_CLOUD_LOCATION" in _build_env(cfg)
    assert "paste-api-key --provider google-vertex" in oc_mod._build_local_command(
        cfg, "hi", "operator", "oc"
    )
    anthropic = _build_env(AgentConfig(model="anthropic/claude-opus-4-7", api_key="k"))
    assert anthropic == {"ANTHROPIC_API_KEY": "k"}


def test_effective_provider_accepts_an_agreeing_alias() -> None:
    cfg = AgentConfig(model="gemini/gemini-2.5-pro", provider="google")
    assert oc_mod._effective_provider(cfg) == "google"
    assert "GEMINI_API_KEY" not in _build_env(cfg)


def test_effective_provider_overrides_a_disagreeing_provider_with_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """oc routes by the id's provider, so the configured one cannot win; say so once."""
    monkeypatch.delenv("GOOGLE_CLOUD_API_KEY", raising=False)
    warnings: list[str] = []
    monkeypatch.setattr(oc_mod._log, "warning", lambda msg, *args: warnings.append(msg % args))
    cfg = AgentConfig(model="google-vertex/gemini-3.8-flash", provider="google")
    assert oc_mod._effective_provider(cfg) == "google-vertex"
    assert "GOOGLE_CLOUD_LOCATION" in _build_env(cfg)
    assert warnings == []
    OpenClawAgent(cfg)
    assert warnings == [
        "model 'google-vertex/gemini-3.8-flash' names provider 'google-vertex'; "
        "ignoring the configured provider 'google'"
    ]
    with pytest.raises(ConfigError, match="unknown agent provider"):
        _build_env(AgentConfig(model="google-vertex/gemini-3.8-flash", provider="bogus"))


def test_effective_provider_leaves_an_unknown_wire_to_the_configured_provider() -> None:
    cfg = AgentConfig(model="mystery/some-model", provider="google", api_key="k")
    assert oc_mod._effective_provider(cfg) == "google"
    assert _build_env(cfg)["GEMINI_API_KEY"] == "k"


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


def test_build_local_command_forwards_extra_flags() -> None:
    cfg = AgentConfig(extra_flags=("--flag1", "--opt=val", "--quoted=hello world"))
    cmd = _build_local_command(cfg, "prompt", "main", "/usr/local/bin/oc")
    assert "--flag1" in cmd
    assert "--opt=val" in cmd
    assert "'--quoted=hello world'" in cmd


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
    """Install a fake ``core.subprocess.run``: the bash agent turn goes to
    ``fake_bash(<command>)``, any other argv (the ``oc`` extraction call) to ``fake_core_run``."""
    core = fake_core_run or (lambda argv, **kwargs: _make_subprocess_result(json.dumps([]), "", 0))

    def dispatch(argv, **kwargs):
        if argv[0] == "/bin/bash":
            return fake_bash(argv[2], **kwargs)
        return core(argv, **kwargs)

    monkeypatch.setattr(oc_mod, "run", dispatch)


def _bundle_writer(events_jsonl: str) -> Callable[..., Any]:
    """Fake core-subprocess.run: ``oc sessions`` returns one row and export-trajectory
    writes ``events.jsonl`` into the bundle dir under the ``--workspace`` it was handed."""
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
    """Happy path: the final answer comes from the bundle, not the noisy bash stdout."""

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
    """The bundle's assistantTexts win over ``--log-level debug`` noise on stdout."""
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


def test_execute_keeps_the_placeholder_instead_of_falling_back_to_stdout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An all-redacted bundle must reach the record as the placeholder, not the debug stdout."""
    events = _events(
        _tool_call("1", "exec", {"command": "kubectl apply -f app.yaml"}),
        _tool_result("1", "deployment created"),
        {
            "type": "model.completed",
            "data": {"assistantTexts": ["[Malformed diagnostic JSON redacted]"]},
        },
    )
    _install_oc_run(
        monkeypatch,
        lambda *a, **k: _make_subprocess_result(stdout="[DEBUG] starting oc...\n", returncode=0),
        _bundle_writer(events),
    )

    result = OpenClawAgent(AgentConfig(target=str(tmp_path / "oc"))).run("p")
    assert is_placeholder_output(result.output)


def test_execute_keeps_the_export_bundle_in_a_harness_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The raw events.jsonl lands in the run workspace and outlives the run."""
    _install_oc_run(
        monkeypatch,
        lambda *a, **k: _make_subprocess_result("OK\n", "", 0),
        _bundle_writer(SAMPLE_EVENTS),
    )
    workspace = tmp_path / "run-ws"
    workspace.mkdir()

    agent = OpenClawAgent(AgentConfig(target=str(tmp_path / "oc"), timeout_sec=30.0))
    result = agent._execute("audit pods", workspace_path=workspace)

    assert result.output == "All pods healthy."
    bundles = list(workspace.glob("oc-export-*/.openclaw/trajectory-exports/*/events.jsonl"))
    assert len(bundles) == 1
    assert json.loads(bundles[0].read_text().splitlines()[0])["type"] == "tool.call"


def test_execute_ignores_a_bundle_the_agent_planted_in_its_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A forged events.jsonl written during the turn must not be scored."""
    workspace = tmp_path / "run-ws"
    workspace.mkdir()
    forged = _events({"type": "model.completed", "data": {"assistantTexts": ["FORGED"]}})

    def planting_turn(*_a: Any, **_k: Any) -> Any:
        planted = workspace / ".openclaw" / "trajectory-exports" / "a"
        planted.mkdir(parents=True)
        (planted / "events.jsonl").write_text(forged, encoding="utf-8")
        return _make_subprocess_result("OK\n", "", 0)

    _install_oc_run(monkeypatch, planting_turn, _bundle_writer(SAMPLE_EVENTS))

    agent = OpenClawAgent(AgentConfig(target=str(tmp_path / "oc"), timeout_sec=30.0))
    result = agent._execute("audit pods", workspace_path=workspace)

    assert result.output == "All pods healthy."
    assert [step["name"] for step in result.trajectory] == [
        "kubectl_get_pods",
        "kubectl_describe",
    ]
    assert (workspace / ".openclaw" / "trajectory-exports" / "a" / "events.jsonl").exists()


def test_read_export_bundle_refuses_more_than_one_events_file(tmp_path: Path) -> None:
    root = tmp_path / ".openclaw" / "trajectory-exports"
    for name in ("a", "openclaw-trajectory-x"):
        (root / name).mkdir(parents=True)
        (root / name / "events.jsonl").write_text(SAMPLE_EVENTS, encoding="utf-8")

    text, errors = _read_export_bundle(tmp_path)

    assert text == ""
    assert errors == [f"expected one events.jsonl under {root}, found 2"]


def test_oc_version_probes_parse_real_output_and_send_the_right_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both probes parse the one-line ``OpenClaw <ver> (<sha>)`` output, the host probe
    carries the nvm PATH overlay, and the image probe runs locked down."""
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append((argv, kwargs))
        if argv[0] == "docker":
            return _make_subprocess_result("OpenClaw 2026.9.6 (eb377ac)\n", "", 0)
        return _make_subprocess_result("OpenClaw 2026.9.1-beta.1 (1d96e5a)\n", "", 0)

    monkeypatch.setattr(oc_mod, "run", fake_run)
    monkeypatch.setattr(oc_mod, "_ensure_node_on_path", lambda env: {"PATH": "/nvm/bin"})
    assert oc_mod._host_oc_version("/usr/local/bin/oc") == "2026.9.1-beta.1"
    assert oc_mod._image_oc_version("img") == "2026.9.6"
    host_argv, host_kw = calls[0]
    assert host_argv == ["/usr/local/bin/oc", "--version"]
    assert host_kw["extra_env"] == {"PATH": "/nvm/bin"}
    image_argv, _ = calls[1]
    assert image_argv[:2] == ["docker", "run"]
    assert {"--network=none", "--cap-drop=ALL"} <= set(image_argv)
    assert image_argv[-4:] == ["--entrypoint", "oc", "img", "--version"]


def _raise_oserror(argv: list[str], **kwargs: Any) -> SimpleNamespace:
    raise OSError("docker not installed")


def test_oc_version_probe_is_inconclusive_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wrapper without ``--version``, or a docker failure, yields None rather than raising."""
    monkeypatch.setattr(
        oc_mod, "run", lambda argv, **kw: _make_subprocess_result("no version here", "", 1)
    )
    assert oc_mod._host_oc_version("oc-a") is None
    monkeypatch.setattr(oc_mod, "run", _raise_oserror)
    assert oc_mod._image_oc_version("img-a") is None


def test_oc_version_skew_names_both_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oc_mod, "_host_oc_version", lambda b: "2026.8.2")
    monkeypatch.setattr(oc_mod, "_image_oc_version", lambda i: "2026.9.1-beta.1")
    skew = oc_mod._oc_version_skew("oc", "img")
    assert skew is not None
    assert "2026.8.2" in skew
    assert "2026.9.1-beta.1" in skew


def test_oc_version_skew_silent_on_match_and_warns_on_an_inconclusive_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(oc_mod._log, "warning", lambda msg, *args: warnings.append(msg % args))
    monkeypatch.setattr(oc_mod, "_host_oc_version", lambda b: "2026.9.1-beta.1")
    monkeypatch.setattr(oc_mod, "_image_oc_version", lambda i: "2026.9.1-beta.1")
    assert oc_mod._oc_version_skew("oc", "img") is None
    assert warnings == []
    monkeypatch.setattr(oc_mod, "_host_oc_version", lambda b: None)
    assert oc_mod._oc_version_skew("oc", "img") is None
    assert warnings == ["oc version skew check skipped: the host probe was inconclusive"]


def test_oc_version_probe_anchors_on_the_product_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """A version-shaped token printed before oc's own line must not be taken for it."""
    monkeypatch.setattr(
        oc_mod,
        "run",
        lambda argv, **kw: _make_subprocess_result(
            "Node v24.21.0 is available\nOpenClaw 2026.9.6 (eb377ac)\n", "", 0
        ),
    )
    assert oc_mod._host_oc_version("oc") == "2026.9.6"
    monkeypatch.setattr(
        oc_mod, "run", lambda argv, **kw: _make_subprocess_result("wrapper 1.2.3\n", "", 0)
    )
    assert oc_mod._host_oc_version("oc") is None


def test_sandbox_preflight_refuses_a_skewed_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    """A skewed host/image pair raises ``SandboxError`` at batch start, so the harness refuses
    the batch unscored instead of provisioning a cluster per task to produce an empty trajectory."""
    seen: list[tuple[str, str]] = []

    def fake_skew(oc_bin: str, image: str) -> str:
        seen.append((oc_bin, image))
        return "oc version skew: boom"

    monkeypatch.setattr(oc_mod, "_oc_version_skew", fake_skew)
    config = AgentConfig(target="~/tools/oc", sandbox=SandboxSpec(image="img"))
    with pytest.raises(SandboxError, match="version skew"):
        OpenClawAgent.sandbox_preflight(config)
    assert seen == [(os.path.expanduser("~/tools/oc"), "img")]


def test_sandbox_preflight_skips_the_probe_when_unsandboxed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def never_probe(oc_bin: str, image: str) -> str | None:
        raise AssertionError("no sandbox image, so nothing to compare against")

    monkeypatch.setattr(oc_mod, "_oc_version_skew", never_probe)
    OpenClawAgent.sandbox_preflight(AgentConfig(target="oc"))
    OpenClawAgent.sandbox_preflight(AgentConfig(target="oc", sandbox=SandboxSpec(image="")))


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


def _timed_out_bash(cmd: str, **kwargs: Any) -> SimpleNamespace:
    # core.subprocess.run wraps TimeoutExpired in SubprocessError.
    raise SubprocessError(["/bin/bash", "-c", cmd], returncode=-1, stdout="", stderr="")


def test_execute_recovers_the_trajectory_on_a_bash_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The killed process leaves its session store behind; the host-side export still reads it."""
    _install_oc_run(monkeypatch, _timed_out_bash, _bundle_writer(SAMPLE_EVENTS))
    result = OpenClawAgent(AgentConfig(target=str(tmp_path / "oc"), timeout_sec=5.0)).run("p")
    assert result.errors == ["oc agent timed out after 5.0s"]
    assert result.metadata["returncode"] == -1
    assert [step["name"] for step in result.trajectory] == ["kubectl_get_pods", "kubectl_describe"]
    assert result.output == "All pods healthy."


def test_execute_records_a_bash_timeout_with_nothing_to_export(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_oc_run(monkeypatch, _timed_out_bash)
    result = OpenClawAgent(AgentConfig(target=str(tmp_path / "oc"), timeout_sec=5.0)).run("p")
    assert result.errors[0] == "oc agent timed out after 5.0s"
    assert any("no session key" in e for e in result.errors)
    assert result.trajectory == []
    # Same shape as the sibling CLI agents: the failure is the answer, so the judge still scores it.
    assert result.output == "Error: oc agent timed out after 5.0s"


def test_execute_treats_whitespace_only_stdout_as_nothing_captured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _timed_out_with_newline(cmd: str, **kwargs: Any) -> NoReturn:
        raise SubprocessError(["/bin/bash", "-c", cmd], returncode=-1, stdout="\n", stderr="")

    _install_oc_run(monkeypatch, _timed_out_with_newline)
    result = OpenClawAgent(AgentConfig(target=str(tmp_path / "oc"), timeout_sec=5.0)).run("p")
    assert result.output == "Error: oc agent timed out after 5.0s"


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


# Capability negotiation: OpenClaw wires MCP + skills via oc's native channels.


def test_openclaw_satisfies_mcp_skills_and_rules_protocols() -> None:
    """OpenClaw declares MCP, Skills and Rules: mcp.servers in an isolated config, managed
    skills under the state dir, the operator brief prepended to the prompt."""
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


# Rules delivery: the bound text reaches the spawned `oc` command.


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
    """The rules text lands inside the ``-m '<prompt>'`` segment of the spawned bash
    command, ahead of the prompt."""
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
    # The prompt is shlex-quoted and appears verbatim in the -m segment, with no
    # blank-line prefix.
    assert "-m 'just the prompt'" in cmd


# MCP server wiring: mcp.servers reach an isolated OPENCLAW_CONFIG_PATH.


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


def test_build_openclaw_config_threads_host_kubeconfig_into_mcp_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KUBECONFIG", "/home/op/.devops-bench/runs/r1/kubeconfig")
    cfg = _build_openclaw_config(AgentConfig(), (McpBinding(name="gke", command=("gke-mcp",)),))
    assert cfg["mcp"]["servers"]["gke"]["env"] == {
        "KUBECONFIG": "/home/op/.devops-bench/runs/r1/kubeconfig"
    }


def test_build_openclaw_config_uses_container_kubeconfig_when_sandboxed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The MCP server runs in the container, where the credential is at the bind path."""
    from devops_bench.agents import sandbox as sandbox_mod

    monkeypatch.setenv("KUBECONFIG", "/home/op/.devops-bench/runs/r1/kubeconfig")
    cfg = _build_openclaw_config(
        AgentConfig(sandbox=sandbox_mod.SandboxSpec(image="img")),
        (McpBinding(name="gke", command=("gke-mcp",)),),
    )
    assert cfg["mcp"]["servers"]["gke"]["env"] == {"KUBECONFIG": sandbox_mod.CONTAINER_KUBECONFIG}


# Model catalog override: ids oc does not ship get registered in the per-run config.


def test_model_override_empty_for_catalog_known_model() -> None:
    """A model already in oc's catalog needs no override."""
    assert _build_model_override(AgentConfig(model="gemini-3.1-pro-preview")) == {}


def test_model_override_empty_when_no_model() -> None:
    assert _build_model_override(AgentConfig()) == {}


def test_model_override_genai_pins_generative_ai_transport() -> None:
    """google-genai pins ``api: google-generative-ai`` (a per-run provider entry replaces
    oc's built-in one), needs no ``baseUrl``, and allowlists ``google/<model>``."""
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
    env = _build_env(AgentConfig(model="gemini-3.5-flash", provider="google-vertex"))
    assert "GOOGLE_CLOUD_API_KEY" not in env


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
    assert vertex["GOOGLE_CLOUD_API_KEY"] == "marker"
    assert "GEMINI_API_KEY" not in vertex


def test_build_env_unknown_provider_raises() -> None:
    """An unknown provider fails loud instead of silently writing a Gemini key."""
    with pytest.raises(ConfigError):
        _build_env(AgentConfig(api_key="k", provider="mystery"))


def test_build_env_unknown_provider_raises_even_when_keyless() -> None:
    """Validation is unconditional — a typoed provider fails loud on a keyless
    (Vertex/ADC) run too, not only when a key is set."""
    with pytest.raises(ConfigError):
        _build_env(AgentConfig(provider="google-vertyx"))


def test_model_override_raises_for_unpinned_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """An override without a transport pin fails loud; only reachable if the tables disagree."""
    monkeypatch.setitem(oc_mod._CATALOG_OVERRIDES, "mystery", frozenset({"gemini-3.5-flash"}))
    with pytest.raises(ConfigError):
        _build_model_override(AgentConfig(model="mystery/gemini-3.5-flash"))


@pytest.mark.parametrize("provider", ["anthropic", "anthropic-bedrock"])
def test_model_override_leaves_other_providers_catalog_alone(provider: str) -> None:
    """Unlisted providers resolve through oc's own catalog, as before the Claude ids."""
    assert _build_model_override(AgentConfig(model="claude-opus-5", provider=provider)) == {}
    assert _build_model_override(AgentConfig(model="mystery/gemini-3.5-flash")) == {}


def test_model_override_openai_without_base_url_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ``OPENAI_BASE_URL`` an ``openai`` model is left to oc's own catalog."""
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    assert _build_model_override(AgentConfig(model="gpt-5", provider="openai")) == {}


def test_model_override_openai_base_url_registers_served_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``OPENAI_BASE_URL`` registers the served model id on the completions transport."""
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:8000/v1/")
    override = _build_model_override(AgentConfig(model="qwen3.8-27b-fp8", provider="openai"))
    assert override["models"]["providers"]["openai"] == {
        "api": "openai-completions",
        "baseUrl": "http://localhost:8000/v1",
        "models": [{"id": "qwen3.8-27b-fp8", "name": "qwen3.8-27b-fp8"}],
    }
    assert override["agents"]["defaults"]["models"] == {"openai/qwen3.8-27b-fp8": {}}


def test_model_override_base_url_ignored_for_other_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``OPENAI_BASE_URL`` only applies to the ``openai`` provider."""
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:8000/v1")
    assert _build_model_override(AgentConfig(model="gemini-3.1-pro-preview")) == {}


def test_model_override_populates_optional_model_entry_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``AGENT_CONTEXT_WINDOW``, ``AGENT_MODEL_REASONING``, and ``AGENT_MAX_TOKENS`` populate the entry."""
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:8000/v1")
    monkeypatch.setenv("AGENT_CONTEXT_WINDOW", "262144")
    monkeypatch.setenv("AGENT_MODEL_REASONING", "true")
    monkeypatch.setenv("AGENT_MAX_TOKENS", "65536")
    override = _build_model_override(AgentConfig(model="qwen3.8-27b-fp8", provider="openai"))
    assert override["models"]["providers"]["openai"]["models"] == [
        {
            "id": "qwen3.8-27b-fp8",
            "name": "qwen3.8-27b-fp8",
            "contextWindow": 262144,
            "reasoning": True,
            "maxTokens": 65536,
        }
    ]


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("AGENT_CONTEXT_WINDOW", "not-an-int"),
        ("AGENT_MAX_TOKENS", "not-an-int"),
        ("AGENT_MODEL_REASONING", "maybe"),
    ],
)
def test_model_override_rejects_invalid_model_entry_env(
    monkeypatch: pytest.MonkeyPatch,
    var: str,
    value: str,
) -> None:
    """Invalid ``AGENT_CONTEXT_WINDOW`` / ``AGENT_MAX_TOKENS`` / ``AGENT_MODEL_REASONING`` raise ``ConfigError``."""
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:8000/v1")
    monkeypatch.setenv(var, value)
    with pytest.raises(ConfigError):
        _build_model_override(AgentConfig(model="qwen3.8-27b-fp8", provider="openai"))


def _empty_sessions_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
    """Core-subprocess.run stub: ``oc sessions`` returns no rows."""
    return _make_subprocess_result(stdout=json.dumps([]), returncode=0)


def test_execute_writes_mcp_servers_into_isolated_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A command-bearing MCP binding lands in ``<cwd>/openclaw.json`` via
    ``OPENCLAW_CONFIG_PATH`` (read inside the fake, before temp-dir cleanup)."""
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


def _runtime_call(call_id: str, args: dict) -> dict:
    """A ``source: runtime`` tool.call in the oc 2026.9.x shape."""
    return {
        "type": "tool.call",
        "source": "runtime",
        "data": {"toolCallId": call_id, "name": "exec", "args": args},
    }


def _runtime_result(call_id: str, text: str, *, success: bool = True) -> dict:
    """A ``source: runtime`` tool.result in the oc 2026.9.x shape."""
    return {
        "type": "tool.result",
        "source": "runtime",
        "data": {
            "toolCallId": call_id,
            "name": "exec",
            "success": success,
            "result": {"content": [{"type": "text", "text": text}]},
        },
    }


def test_parse_trajectory_export_dedupes_dual_source_events() -> None:
    """oc 2026.9.x logs each tool event twice; nested runtime-only calls are kept."""
    nested = "tool_search_code:call_1:exec:1"
    blob = _events(
        {**_tool_call("call_1", "exec", {"code": "run()"}), "source": "transcript"},
        _runtime_call("call_1", {"code": "***"}),
        _runtime_call(nested, {"command": "kubectl get pods"}),
        _runtime_result(nested, "pod-a Running"),
        _runtime_result("call_1", "done"),
        {**_tool_result("call_1", "stale duplicate"), "source": "transcript"},
        _runtime_call("call_2", {"code": "fail()"}),
        _runtime_result("call_2", "boom", success=False),
    )
    trajectory, _tokens, _output, errors = parse_trajectory_export(blob)
    assert errors == []
    assert trajectory == [
        {"name": "exec", "args": {"code": "run()"}, "result": "done", "status": "completed"},
        {
            "name": "exec",
            "args": {"command": "kubectl get pods"},
            "result": "pod-a Running",
            "status": "completed",
        },
        {"name": "exec", "args": {"code": "fail()"}, "result": "boom", "status": "error"},
    ]


def test_parse_trajectory_export_prefers_transcript_args_whichever_copy_comes_first() -> None:
    """The runtime copy is always redacted to ***; transcript args win regardless of order."""
    blob = _events(
        _runtime_call("call_1", {"code": "***"}),
        {**_tool_call("call_1", "exec", {"code": "run()"}), "source": "transcript"},
        _runtime_result("call_1", "done"),
        {**_tool_result("call_1", "done"), "source": "transcript"},
    )
    trajectory, _tokens, _output, errors = parse_trajectory_export(blob)
    assert errors == []
    assert trajectory == [
        {"name": "exec", "args": {"code": "run()"}, "result": "done", "status": "completed"},
    ]


def test_parse_trajectory_export_keeps_a_reused_call_id_from_the_same_source() -> None:
    """A repeat from the same source is a new call, not a duplicate, so nothing is dropped."""
    blob = _events(
        {**_tool_call("call_0", "exec", {"command": "ls"}), "source": "transcript"},
        _runtime_call("call_0", {"command": "***"}),
        _runtime_result("call_0", "a.txt"),
        {**_tool_result("call_0", "a.txt"), "source": "transcript"},
        {**_tool_call("call_0", "exec", {"command": "pwd"}), "source": "transcript"},
        _runtime_call("call_0", {"command": "***"}),
        _runtime_result("call_0", "/workspace"),
        {**_tool_result("call_0", "/workspace"), "source": "transcript"},
    )
    trajectory, _tokens, _output, errors = parse_trajectory_export(blob)
    assert errors == []
    assert trajectory == [
        {"name": "exec", "args": {"command": "ls"}, "result": "a.txt", "status": "completed"},
        {
            "name": "exec",
            "args": {"command": "pwd"},
            "result": "/workspace",
            "status": "completed",
        },
    ]


def test_native_openai_key_crosses_explicit_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    assert _build_env(AgentConfig(provider="openai"))["OPENAI_API_KEY"] == "test-key"


@pytest.mark.parametrize(
    "model,provider",
    [("gemini-3.7-flash", "google-vertex"), ("claude-sonnet-5", "anthropic-vertex")],
)
def test_fleet_models_registered(model: str, provider: str) -> None:
    assert (
        _build_model_override(AgentConfig(model=model, provider=provider))["models"]["providers"][
            provider
        ]["models"][0]["id"]
        == model
    )


def test_vertex_auth_profile_seeded_for_headless_run() -> None:
    command = oc_mod._build_local_command(
        AgentConfig(provider="anthropic-vertex"), "hi", "operator", "oc"
    )
    assert "models auth paste-api-key --provider anthropic-vertex --agent operator" in command
    assert oc_mod._VERTEX_CREDENTIALS_MARKER in command


def test_vertex_auth_profile_seeded_for_keyless_google_vertex() -> None:
    """oc's auth store gates every keyless Vertex provider, not just the plugin one."""
    command = oc_mod._build_local_command(
        AgentConfig(provider="google-vertex"), "hi", "operator", "oc"
    )
    assert "models auth paste-api-key --provider google-vertex --agent operator" in command


def test_vertex_auth_profile_skipped_for_a_keyed_run() -> None:
    """A real key is already in the config; the marker would shadow it."""
    command = oc_mod._build_local_command(
        AgentConfig(provider="google-vertex", api_key="k"), "hi", "operator", "oc"
    )
    assert "paste-api-key" not in command


@pytest.mark.parametrize(
    "env_key,api_key,seeded",
    [
        ("real-express-key", "", False),
        (oc_mod._VERTEX_CREDENTIALS_MARKER, "", True),
        ("", oc_mod._VERTEX_CREDENTIALS_MARKER, True),
        ("   ", "", True),
    ],
)
def test_vertex_auth_profile_follows_the_effective_key(
    monkeypatch: pytest.MonkeyPatch, env_key: str, api_key: str, seeded: bool
) -> None:
    """A key in the environment counts too; the ADC marker never counts as a key."""
    monkeypatch.setenv("GOOGLE_CLOUD_API_KEY", env_key)
    command = oc_mod._build_local_command(
        AgentConfig(provider="google-vertex", api_key=api_key), "hi", "operator", "oc"
    )
    assert ("paste-api-key --provider google-vertex" in command) is seeded


@pytest.mark.parametrize(
    "host_key,extra_key,seeded",
    [("", "real-express-key", False), ("real-express-key", "", True)],
)
def test_vertex_auth_profile_follows_the_key_extra_env_delivers(
    monkeypatch: pytest.MonkeyPatch, host_key: str, extra_key: str, seeded: bool
) -> None:
    """``extra_env`` outranks the host env in _build_env, so the seed decision follows it too."""
    monkeypatch.setenv("GOOGLE_CLOUD_API_KEY", host_key)
    cfg = AgentConfig(provider="google-vertex", extra_env={"GOOGLE_CLOUD_API_KEY": extra_key})
    assert _build_env(cfg)["GOOGLE_CLOUD_API_KEY"] == extra_key
    command = oc_mod._build_local_command(cfg, "hi", "operator", "oc")
    assert ("paste-api-key --provider google-vertex" in command) is seeded


def test_a_stray_key_on_a_keyless_vertex_provider_still_seeds_and_starts_the_emulator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """anthropic-vertex declares no key var, so a configured key never reaches oc."""
    cfg = _sandboxed(tmp_path, provider="anthropic-vertex", api_key="stray-gemini-key")
    command = oc_mod._build_local_command(cfg, "hi", "operator", "oc")
    assert "paste-api-key --provider anthropic-vertex" in command
    calls = _install_fake_emulator(monkeypatch)
    oc_mod._sandbox_provider_env(cfg, tmp_path)
    assert calls


def test_vertex_auth_profile_skipped_for_a_non_vertex_provider() -> None:
    command = oc_mod._build_local_command(AgentConfig(provider="google"), "hi", "operator", "oc")
    assert "paste-api-key" not in command


_FAKE_EMULATOR_ENV = {
    "GCE_METADATA_HOST": "host.docker.internal:41234",
    "GCE_METADATA_IP": "host.docker.internal:41234",
    "METADATA_SERVER_DETECTION": "assume-present",
}


def _sandboxed(tmp_path: Path, **fields: Any) -> AgentConfig:
    """An AgentConfig carrying a task-completed sandbox spec rooted at ``tmp_path``."""
    from devops_bench.agents import sandbox as sandbox_mod

    return AgentConfig(sandbox=sandbox_mod.SandboxSpec(image="img", workspace=tmp_path), **fields)


def _install_fake_emulator(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Stub the shared Vertex credential recipe; returns the calls it received."""
    calls: list[dict[str, Any]] = []

    def fake_credential_env(spec: Any, *, project: str | None = None, **kw: Any) -> dict[str, str]:
        calls.append({"backend": spec.backend, "project": project})
        return dict(_FAKE_EMULATOR_ENV)

    monkeypatch.setattr(oc_mod, "sandbox_credential_env", fake_credential_env)
    return calls


def test_sandbox_vertex_overlay_uses_metadata_without_host_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Metadata auth on AND pointed at the emulator, else the SDK reaches the real VM SA."""
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/private/host.json")
    calls = _install_fake_emulator(monkeypatch)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    overlay = oc_mod._sandbox_provider_env(
        _sandboxed(tmp_path, provider="anthropic-vertex"), state_dir
    )
    assert overlay["GOOGLE_CLOUD_PROJECT"] == "test-project"
    assert overlay["ANTHROPIC_VERTEX_USE_GCP_METADATA"] == "1"
    assert overlay["GOOGLE_CLOUD_API_KEY"] == "gcp-vertex-credentials"
    assert overlay["GCE_METADATA_HOST"] == "host.docker.internal:41234"
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in overlay
    assert calls == [{"backend": "vertex", "project": "test-project"}]


def test_sandbox_overlay_keeps_the_shim_out_of_the_artifact_diff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No new top-level workspace entry for the artifact diff; container path, not a literal."""
    _install_fake_emulator(monkeypatch)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    before = set(p.name for p in tmp_path.iterdir())
    overlay = oc_mod._sandbox_provider_env(
        _sandboxed(tmp_path, provider="google-vertex"), state_dir
    )
    assert set(p.name for p in tmp_path.iterdir()) == before
    assert (state_dir / "node-fetch-shim" / "register.mjs").is_file()
    assert overlay["NODE_OPTIONS"] == "--import=/workspace/state/node-fetch-shim/register.mjs"


def test_sandbox_overlay_refuses_to_run_without_a_completed_spec(tmp_path: Path) -> None:
    from devops_bench.core import SandboxError

    with pytest.raises(SandboxError):
        oc_mod._sandbox_provider_env(AgentConfig(provider="google-vertex"), tmp_path)


def test_sandbox_google_vertex_overlay_gets_the_same_credential_recipe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GCP_PROJECT_ID", "p-from-repo-var")
    calls = _install_fake_emulator(monkeypatch)
    overlay = oc_mod._sandbox_provider_env(_sandboxed(tmp_path, provider="google-vertex"), tmp_path)
    assert overlay["GCE_METADATA_HOST"] == "host.docker.internal:41234"
    assert "ANTHROPIC_VERTEX_USE_GCP_METADATA" not in overlay
    assert calls == [{"backend": "vertex", "project": "p-from-repo-var"}]


@pytest.mark.parametrize(
    "model,anthropic_marker",
    [("google-vertex/gemini-3.8-flash", False), ("anthropic-vertex/claude-opus-4-7", True)],
)
def test_sandbox_overlay_follows_a_full_model_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, model: str, anthropic_marker: bool
) -> None:
    """With no provider configured, the emulator and the Vertex markers follow the id."""
    monkeypatch.delenv("GOOGLE_CLOUD_API_KEY", raising=False)
    calls = _install_fake_emulator(monkeypatch)
    overlay = oc_mod._sandbox_provider_env(_sandboxed(tmp_path, model=model), tmp_path)
    assert overlay["GCE_METADATA_HOST"] == "host.docker.internal:41234"
    assert ("ANTHROPIC_VERTEX_USE_GCP_METADATA" in overlay) is anthropic_marker
    assert [c["backend"] for c in calls] == ["vertex"]


@pytest.mark.parametrize(
    "provider,api_key",
    [("google-vertex", "express-key"), ("google", "k"), ("anthropic", "k")],
)
def test_sandbox_overlay_skips_the_emulator_for_keyed_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, provider: str, api_key: str
) -> None:
    """A key crosses by value; the emulator is only for keyless Vertex."""
    calls = _install_fake_emulator(monkeypatch)
    config = _sandboxed(tmp_path, provider=provider, api_key=api_key)
    overlay = oc_mod._sandbox_provider_env(config, tmp_path)
    assert "GCE_METADATA_HOST" not in overlay
    assert calls == []


@pytest.mark.parametrize(
    "env,provider,api_key,expected",
    [
        ({}, "google-vertex", "", "global"),
        ({"GOOGLE_CLOUD_LOCATION": "us-east5"}, "google-vertex", "", "us-east5"),
        ({"GCP_VERTEX_LOCATION": "europe-west4"}, "google-vertex", "", "europe-west4"),
        ({}, "google-vertex", "k", "global"),
        ({}, "google", "k", None),
    ],
)
def test_build_env_pins_a_vertex_location(
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    provider: str,
    api_key: str,
    expected: str | None,
) -> None:
    """oc aborts on Vertex without a location, sandboxed or not; non-Vertex gets none."""
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)
    monkeypatch.delenv("GCP_VERTEX_LOCATION", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    overlay = _build_env(AgentConfig(provider=provider, api_key=api_key))
    assert overlay.get("GOOGLE_CLOUD_LOCATION") == expected


@pytest.mark.parametrize(
    "env_key,emulator",
    [("real-express-key", False), (oc_mod._VERTEX_CREDENTIALS_MARKER, True)],
)
def test_sandbox_overlay_reads_the_key_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env_key: str, emulator: bool
) -> None:
    """An env key crosses by value and needs no emulator; the env marker is not a key."""
    monkeypatch.setenv("GOOGLE_CLOUD_API_KEY", env_key)
    calls = _install_fake_emulator(monkeypatch)
    overlay = oc_mod._sandbox_provider_env(_sandboxed(tmp_path, provider="google-vertex"), tmp_path)
    assert ("GCE_METADATA_HOST" in overlay) is emulator
    assert bool(calls) is emulator


def test_execute_unsandboxed_vertex_asks_for_no_model_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unsandboxed runs use the host's own ADC; no emulator is started."""
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-a")

    def no_emulator(*args: Any, **kwargs: Any) -> NoReturn:
        pytest.fail("sandbox_credential_env called on an unsandboxed run")

    monkeypatch.setattr(oc_mod, "sandbox_credential_env", no_emulator)
    monkeypatch.delenv("GOOGLE_CLOUD_API_KEY", raising=False)
    captured: dict[str, Any] = {}

    def fake_bash(cmd: str, **kwargs: Any) -> SimpleNamespace:
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("extra_env") or {}
        return _make_subprocess_result(stdout="ok", returncode=0)

    _install_oc_run(monkeypatch, fake_bash, _empty_sessions_run)
    cfg = AgentConfig(target=str(tmp_path / "oc"), provider="google-vertex")
    OpenClawAgent(cfg).run("p")
    # The auth profile and the location are not sandbox concerns: both reach the host run.
    assert "paste-api-key --provider google-vertex" in captured["cmd"]
    assert captured["env"]["GOOGLE_CLOUD_LOCATION"]


@pytest.mark.parametrize(
    "model,provider,transport",
    [
        ("gemini-3.8-flash", "google", "google-generative-ai"),
        ("gemini-3.8-flash", "google-vertex", "google-vertex"),
        ("claude-fable-5-1", "anthropic-vertex", "anthropic-messages"),
    ],
)
def test_latest_models_have_per_run_catalog_and_transport(
    model: str, provider: str, transport: str
) -> None:
    override = _build_model_override(AgentConfig(model=model, provider=provider))
    entry = override["models"]["providers"][provider]
    assert entry["models"] == [{"id": model, "name": model}]
    assert entry["api"] == transport
    assert override["agents"]["defaults"]["models"] == {f"{provider}/{model}": {}}
