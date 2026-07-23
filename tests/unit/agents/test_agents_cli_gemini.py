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

"""Unit tests for devops_bench.agents.cli.gemini_cli."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

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
from devops_bench.agents.cli.gemini_cli import GeminiCliAgent, parse_stream_json
from devops_bench.agents.cli.gemini_cli import agent as gemini_mod
from devops_bench.agents.cli.gemini_cli.agent import (
    _build_argv,
    _build_env,
    _build_settings,
)
from devops_bench.core.errors import ConfigError, SubprocessError


def _stream(*events: dict) -> str:
    """Render a list of events as a stream-json stdout blob."""
    return "\n".join(json.dumps(event) for event in events) + "\n"


SAMPLE_STREAM = _stream(
    {"type": "init", "session_id": "abc-123", "model": "gemini-2.5-pro"},
    {
        "type": "tool_use",
        "id": "call-1",
        "name": "mcp_gke_list_clusters",
        "input": {"project": "p1"},
    },
    {
        "type": "tool_result",
        "tool_use_id": "call-1",
        "content": "cluster-a, cluster-b",
    },
    {
        "type": "tool_use",
        "id": "call-2",
        "name": "mcp_gke_get_cluster",
        "input": {"cluster": "cluster-a"},
    },
    {
        "type": "tool_result",
        "tool_use_id": "call-2",
        "content": "v1.30",
        "is_error": False,
    },
    {
        "type": "result",
        "output": "Done.",
        "tokens": {"prompt_token_count": 10, "candidates_token_count": 20},
    },
)


def test_parse_stream_json_emits_canonical_trajectory() -> None:
    output, trajectory, tokens, errors = parse_stream_json(SAMPLE_STREAM)
    assert output == "Done."
    assert tokens == {"prompt_token_count": 10, "candidates_token_count": 20}
    assert errors == []
    assert trajectory == [
        {
            "name": "mcp_gke_list_clusters",
            "args": {"project": "p1"},
            "result": "cluster-a, cluster-b",
            "status": "completed",
        },
        {
            "name": "mcp_gke_get_cluster",
            "args": {"cluster": "cluster-a"},
            "result": "v1.30",
            "status": "completed",
        },
    ]


def test_parse_stream_json_records_json_decode_errors_on_errors_list() -> None:
    blob = "{not json}\n" + json.dumps({"type": "result", "output": "ok"}) + "\n"
    output, trajectory, _tokens, errors = parse_stream_json(blob)
    assert output == "ok"
    assert trajectory == []
    assert len(errors) == 1
    assert "parse error" in errors[0]


def test_parse_stream_json_records_unmatched_tool_results() -> None:
    blob = _stream({"type": "tool_result", "tool_use_id": "ghost", "content": "?"})
    _output, trajectory, _tokens, errors = parse_stream_json(blob)
    # Unpaired result must surface; canonical trajectory is empty.
    assert trajectory == []
    assert any("without matching tool_use" in msg for msg in errors)


def test_parse_stream_json_records_error_events() -> None:
    blob = _stream({"type": "error", "message": "rate limit"})
    _output, _trajectory, _tokens, errors = parse_stream_json(blob)
    assert errors == ["stream-json error event: rate limit"]


def test_parse_stream_json_marks_failed_tool_results_as_error_status() -> None:
    blob = _stream(
        {"type": "tool_use", "id": "c", "name": "x", "input": {}},
        {"type": "tool_result", "tool_use_id": "c", "content": "oops", "is_error": True},
    )
    _output, trajectory, _tokens, _errors = parse_stream_json(blob)
    assert trajectory[0]["status"] == "error"


def test_parse_stream_json_empty_input_returns_empty() -> None:
    assert parse_stream_json("") == ("", [], {}, [])


def test_build_argv_disables_extensions_when_no_allowed_tools() -> None:
    argv = _build_argv("/bin/gemini", "hi", ())
    assert "--output-format" in argv and "stream-json" in argv
    assert "--extensions=" in argv
    assert "--allowed-tools" not in argv
    # Headless auto-approve so MCP/built-in tool calls never block on a prompt.
    assert argv[argv.index("--approval-mode") + 1] == "yolo"
    assert argv[-2:] == ["-p", "hi"]


def test_build_argv_emits_one_allowed_tools_pair_per_tool() -> None:
    argv = _build_argv("/bin/gemini", "hi", ("a", "b"))
    pairs = [(argv[i], argv[i + 1]) for i in range(len(argv) - 1) if argv[i] == "--allowed-tools"]
    assert pairs == [("--allowed-tools", "a"), ("--allowed-tools", "b")]
    assert "--extensions=" not in argv
    assert argv[argv.index("--approval-mode") + 1] == "yolo"


def test_build_env_threads_api_key_and_model_into_gemini_vars() -> None:
    cfg = AgentConfig(model="gemini-2.5-pro", api_key="abc", extra_env={"X": "y"})
    env = _build_env(cfg)
    assert env["GOOGLE_API_KEY"] == "abc"
    assert env["GEMINI_API_KEY"] == "abc"
    assert env["GEMINI_MODEL"] == "gemini-2.5-pro"
    assert env["OTEL_SDK_DISABLED"] == "true"
    assert env["X"] == "y"


def test_build_env_vertex_key_routes_to_cloud_api_key() -> None:
    # google-vertex routes a provided key to GOOGLE_CLOUD_API_KEY (the vertex
    # transport var), not the google-genai vars.
    env = _build_env(AgentConfig(model="gemini-2.5-pro", api_key="abc", provider="google-vertex"))
    assert env["GOOGLE_CLOUD_API_KEY"] == "abc"
    assert "GEMINI_API_KEY" not in env and "GOOGLE_API_KEY" not in env


def test_build_env_keyless_writes_no_key_var() -> None:
    # No api_key (Vertex/ADC): no key env var is written regardless of provider.
    env = _build_env(AgentConfig(model="gemini-2.5-pro", provider="google-vertex"))
    assert not {"GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_CLOUD_API_KEY"} & env.keys()


def test_build_env_unknown_provider_raises_even_when_keyless() -> None:
    # Validation is unconditional — a typoed provider fails loud on a keyless run.
    with pytest.raises(ConfigError):
        _build_env(AgentConfig(model="gemini-2.5-pro", provider="google-vertyx"))


def test_gemini_agent_registered_under_canonical_key() -> None:
    assert AGENTS.get("gemini") is GeminiCliAgent


def test_execute_returns_typed_result_with_trajectory(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["timeout"] = kwargs.get("timeout")
        captured["extra_env"] = kwargs.get("extra_env")
        return SimpleNamespace(stdout=SAMPLE_STREAM, stderr="", returncode=0)

    monkeypatch.setattr(gemini_mod, "run", fake_run)
    agent = GeminiCliAgent(AgentConfig(target="gemini-x", timeout_sec=30.0))
    result = agent.run("ping")
    assert result.output == "Done."
    assert len(result.trajectory) == 2
    assert result.errors == []
    assert result.tokens == {"prompt_token_count": 10, "candidates_token_count": 20}
    assert captured["timeout"] == 30.0
    assert captured["argv"][0].endswith("gemini-x")
    assert "--output-format" in captured["argv"]
    assert "stream-json" in captured["argv"]
    assert captured["argv"][-2:] == ["-p", "ping"]


def test_execute_records_non_zero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv, **kwargs):
        return SimpleNamespace(stdout="", stderr="boom", returncode=2)

    monkeypatch.setattr(gemini_mod, "run", fake_run)
    result = GeminiCliAgent(AgentConfig(target="gemini")).run("p")
    assert result.has_errors()
    assert any("exited 2" in e for e in result.errors)
    assert result.metadata.get("returncode") == 2


def test_execute_handles_subprocess_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv, **kwargs):
        raise SubprocessError(argv, returncode=-1, stdout="", stderr="timeout")

    monkeypatch.setattr(gemini_mod, "run", fake_run)
    result = GeminiCliAgent(AgentConfig(target="gemini")).run("p")
    assert result.has_errors()
    assert "subprocess error" in result.errors[0]
    assert result.trajectory == []


def test_execute_handles_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv, **kwargs):
        raise OSError("not found")

    monkeypatch.setattr(gemini_mod, "run", fake_run)
    result = GeminiCliAgent(AgentConfig(target="gemini")).run("p")
    assert result.has_errors()
    assert "binary unavailable" in result.errors[0]


def test_execute_passes_timeout_to_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(gemini_mod, "run", fake_run)
    GeminiCliAgent(AgentConfig(target="gemini", timeout_sec=15.5)).run("p")
    assert captured["timeout"] == 15.5


def test_execute_wires_extra_env_into_subprocess_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Call-site wiring: GEMINI_MODEL / GOOGLE_API_KEY actually reach `run(...)`."""
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["extra_env"] = kwargs.get("extra_env")
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(gemini_mod, "run", fake_run)
    cfg = AgentConfig(target="gemini", model="gemini-2.5-pro", api_key="abc")
    GeminiCliAgent(cfg).run("p")
    env = captured["extra_env"]
    assert env is not None
    assert env["GEMINI_MODEL"] == "gemini-2.5-pro"
    assert env["GOOGLE_API_KEY"] == "abc"
    assert env["GEMINI_API_KEY"] == "abc"
    # OTLP exporters must be disabled to avoid the CLI hanging on a broken
    # telemetry endpoint when no AGENT_API_KEY/AGENT_MODEL is configured.
    assert env["OTEL_SDK_DISABLED"] == "true"


def test_parse_stream_json_accepts_tool_use_id_field_for_call_id() -> None:
    """Some CLI builds key the tool_use on `tool_use_id`, not `id`."""
    blob = _stream(
        {
            "type": "tool_use",
            "tool_use_id": "alt-1",
            "name": "list",
            "input": {},
        },
        {"type": "tool_result", "tool_use_id": "alt-1", "content": "ok"},
    )
    _output, trajectory, _tokens, errors = parse_stream_json(blob)
    assert errors == []
    assert trajectory == [
        {"name": "list", "args": {}, "result": "ok", "status": "completed"},
    ]


def test_parse_stream_json_accepts_args_field_when_input_absent() -> None:
    """Older CLI builds emit `args` instead of `input` on tool_use."""
    blob = _stream(
        {"type": "tool_use", "id": "c1", "name": "x", "args": {"k": "v"}},
        {"type": "tool_result", "id": "c1", "output": "ok"},
    )
    _output, trajectory, _tokens, errors = parse_stream_json(blob)
    assert errors == []
    assert trajectory[0]["args"] == {"k": "v"}
    assert trajectory[0]["result"] == "ok"


def test_parse_stream_json_real_cli_schema() -> None:
    """The live Gemini CLI schema: ``tool_name``/``tool_id``/``parameters`` on
    tool_use, the answer streamed across ``message`` (role=assistant) events,
    and token usage under ``result.stats`` — none of which the legacy field
    names cover. Captured from gemini-cli 0.47 + gke-mcp 0.13.
    """
    blob = _stream(
        {"type": "init", "session_id": "s1", "model": "gemini-3.1-pro-preview"},
        {"type": "message", "role": "user", "content": "list files"},
        {
            "type": "tool_use",
            "tool_name": "list_directory",
            "tool_id": "list_directory__abc",
            "parameters": {"dir_path": "/work"},
        },
        {"type": "tool_result", "tool_id": "list_directory__abc", "status": "success"},
        {"type": "message", "role": "assistant", "content": "I found "},
        {"type": "message", "role": "assistant", "content": "one file."},
        {
            "type": "result",
            "status": "success",
            "stats": {
                "total_tokens": 31489,
                "input_tokens": 31225,
                "output_tokens": 35,
                "cached": 12173,
            },
        },
    )
    output, trajectory, tokens, errors = parse_stream_json(blob)
    assert output == "I found one file."
    assert errors == []
    # The live tool_result carries only a status (no payload), so result stays
    # unset (None) — the stream simply doesn't include tool output text.
    assert trajectory == [
        {
            "name": "list_directory",
            "args": {"dir_path": "/work"},
            "result": None,
            "status": "completed",
        },
    ]
    # Canonical buckets: input excludes the cached subset, and reasoning is
    # derived from the total gap (total - full_input - output = thinking).
    assert tokens == {
        "input": 31225 - 12173,
        "cached": 12173,
        "cache_write": None,
        "reasoning": 31489 - 31225 - 35,
        "output": 35,
        "total": 31489,
    }


def test_parse_stream_json_clamps_input_when_cached_exceeds_full_input() -> None:
    """A ``cached`` count larger than ``input_tokens`` must not push canonical
    ``input`` negative — the difference clamps at 0."""
    blob = _stream(
        {
            "type": "result",
            "status": "success",
            "stats": {"input_tokens": 100, "cached": 250, "output_tokens": 10},
        },
    )
    _, _, tokens, errors = parse_stream_json(blob)
    assert errors == []
    assert tokens["input"] == 0
    assert tokens["cached"] == 250


def test_parse_stream_json_marks_failed_tool_result_status_field() -> None:
    """A tool_result with ``status="error"`` (and no is_error flag) is failed."""
    blob = _stream(
        {"type": "tool_use", "tool_name": "x", "tool_id": "t1", "parameters": {}},
        {"type": "tool_result", "tool_id": "t1", "status": "error"},
    )
    _output, trajectory, _tokens, errors = parse_stream_json(blob)
    assert errors == []
    assert trajectory[0]["status"] == "error"


def test_run_does_not_invoke_subprocess_when_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity: parse_stream_json must not shell out (catches an import-time hazard)."""

    def boom(*_a, **_kw):
        raise AssertionError("parse_stream_json should not run subprocess")

    monkeypatch.setattr(subprocess, "run", boom)
    parse_stream_json(SAMPLE_STREAM)


# Tests for the now-deleted legacy surface — these documents what is gone for
# good and will fail-fast if the dead modules are ever reintroduced.


def test_legacy_run_cli_agent_is_gone() -> None:
    assert not hasattr(gemini_mod, "run_cli_agent")


def test_legacy_session_glob_is_gone() -> None:
    assert not hasattr(gemini_mod, "extract_trajectory_from_session")


# ---------------------------------------------------------------------------
# PR3 — capability negotiation and binding consumption
# ---------------------------------------------------------------------------


def test_gemini_agent_satisfies_mcp_skills_and_rules_protocols() -> None:
    """Gemini declares MCP, Skills and Rules: it writes ``mcpServers`` into a
    workspace ``settings.json``, materializes workspace skills under
    ``.gemini/skills``, and auto-loads ``GEMINI.md``."""
    agent = GeminiCliAgent(AgentConfig())
    assert isinstance(agent, SupportsMcp)
    assert isinstance(agent, SupportsSkills)
    assert isinstance(agent, SupportsRules)


def test_execute_pulls_allowed_tools_from_capabilities(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--allowed-tools`` argv comes from ``capabilities.allowed_tools``."""
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(gemini_mod, "run", fake_run)
    caps = AllCapabilities(
        mcp_servers=(McpBinding(name="t", command=(), tools=("alpha", "beta")),),
    )
    GeminiCliAgent(AgentConfig(target="gemini", capabilities=caps)).run("p")
    argv = captured["argv"]
    pairs = [(argv[i], argv[i + 1]) for i in range(len(argv) - 1) if argv[i] == "--allowed-tools"]
    assert pairs == [("--allowed-tools", "alpha"), ("--allowed-tools", "beta")]
    assert "--extensions=" not in argv  # tools present → extensions enabled


def test_execute_disables_extensions_when_capabilities_have_no_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No MCP binding (no allowed tools) → ``--extensions=`` argv (extensions off)."""
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(gemini_mod, "run", fake_run)
    GeminiCliAgent(AgentConfig(target="gemini")).run("p")
    assert "--extensions=" in captured["argv"]
    assert "--allowed-tools" not in captured["argv"]


def test_gemini_agent_mirrors_capability_bindings_onto_mixin_attributes() -> None:
    """The structural-Protocol attributes track the granted bindings."""
    binding = McpBinding(name="x", command=(), tools=("t",))
    skills = SkillBinding(paths=("/some/skills",))
    caps = AllCapabilities(
        mcp_servers=(binding,),
        skills=skills,
        rules=AgentRules(text="be a sre"),
    )
    agent = GeminiCliAgent(AgentConfig(capabilities=caps))
    assert agent.mcp_servers == (binding,)
    assert agent.skills == skills
    assert agent.rules == AgentRules(text="be a sre")


# ---------------------------------------------------------------------------
# Rules delivery: GEMINI.md actually reaches the binary's working directory.
# ---------------------------------------------------------------------------


def test_execute_writes_gemini_md_with_rules_text_before_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When rules.text is bound, GEMINI.md exists with that text in the cwd
    handed to the subprocess — at the *moment* `run` is called.

    Snapshotting inside the fake `run` is load-bearing: the agent uses a
    `TemporaryDirectory` context manager whose cleanup runs after `_execute`
    returns, so a post-hoc filesystem check on the cwd would race with
    cleanup. Reading the file from inside `run` proves the binary would see
    it on startup, which is what the CLI's auto-load relies on.
    """
    captured: dict = {}

    def fake_run(argv, **kwargs):
        from pathlib import Path

        cwd = kwargs.get("cwd")
        captured["cwd"] = cwd
        gemini_md = Path(cwd) / "GEMINI.md" if cwd else None
        captured["gemini_md_exists"] = bool(gemini_md and gemini_md.exists())
        captured["gemini_md_text"] = (
            gemini_md.read_text(encoding="utf-8") if captured["gemini_md_exists"] else None
        )
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(gemini_mod, "run", fake_run)
    caps = AllCapabilities(rules=AgentRules(text="you are a precise SRE"))
    GeminiCliAgent(AgentConfig(target="gemini", capabilities=caps)).run("p")

    assert captured["cwd"] is not None, "agent must set cwd so GEMINI.md is auto-loaded"
    assert captured["gemini_md_exists"], "GEMINI.md must exist in cwd before subprocess"
    assert captured["gemini_md_text"] == "you are a precise SRE"


def test_execute_skips_writing_gemini_md_when_rules_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty rules.text → no GEMINI.md created (do not write a blank file)."""
    captured: dict = {}

    def fake_run(argv, **kwargs):
        cwd = kwargs.get("cwd")
        captured["cwd"] = cwd
        gemini_md = os.path.join(cwd, "GEMINI.md") if cwd else None
        captured["gemini_md_exists"] = bool(gemini_md and os.path.exists(gemini_md))
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(gemini_mod, "run", fake_run)
    GeminiCliAgent(AgentConfig(target="gemini")).run("p")  # default empty rules
    assert captured["gemini_md_exists"] is False


def test_execute_cleans_up_temp_working_dir_after_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-run temp working dir is cleaned up after `_execute` returns;
    the bound GEMINI.md leaves no trail on the user's filesystem."""
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["cwd"] = kwargs.get("cwd")
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(gemini_mod, "run", fake_run)
    caps = AllCapabilities(rules=AgentRules(text="any rules"))
    GeminiCliAgent(AgentConfig(target="gemini", capabilities=caps)).run("p")
    # cwd was a real path during the run; after _execute returns it is gone.
    assert captured["cwd"] is not None
    assert not os.path.exists(captured["cwd"])


# ---------------------------------------------------------------------------
# MCP server wiring: settings.json mcpServers reach the binary's cwd.
# ---------------------------------------------------------------------------


def test_build_settings_combines_mcp_servers_and_skills_flag() -> None:
    """Both knobs render; skills flag is gated on ``skills_enabled``."""
    binding = McpBinding(name="gke", command=("gke-mcp",))
    assert _build_settings((binding,), skills_enabled=True) == {
        "mcpServers": {"gke": {"command": "gke-mcp"}},
        "skills": {"enabled": True},
    }
    assert _build_settings((), skills_enabled=False) == {}


def test_execute_writes_mcp_servers_into_workspace_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A command-bearing MCP binding lands in ``<cwd>/.gemini/settings.json``."""
    captured: dict = {}

    def fake_run(argv, **kwargs):
        settings_path = os.path.join(kwargs["cwd"], ".gemini", "settings.json")
        captured["exists"] = os.path.exists(settings_path)
        if captured["exists"]:
            with open(settings_path) as f:
                captured["settings"] = json.load(f)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(gemini_mod, "run", fake_run)
    caps = AllCapabilities(
        mcp_servers=(McpBinding(name="gke", command=("gke-mcp",), tools=("mcp_gke_x",)),),
    )
    GeminiCliAgent(AgentConfig(target="gemini", capabilities=caps)).run("p")

    assert captured["exists"], "settings.json must exist in cwd before subprocess"
    assert captured["settings"]["mcpServers"] == {"gke": {"command": "gke-mcp"}}


def test_execute_writes_no_settings_when_no_command_and_no_skills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No launchable MCP server and no skills → no settings.json is written."""
    captured: dict = {}

    def fake_run(argv, **kwargs):
        settings_path = os.path.join(kwargs["cwd"], ".gemini", "settings.json")
        captured["exists"] = os.path.exists(settings_path)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(gemini_mod, "run", fake_run)
    # Binding carries tools (→ --allowed-tools) but no launch command.
    caps = AllCapabilities(
        mcp_servers=(McpBinding(name="builtin", command=(), tools=("alpha",)),),
    )
    GeminiCliAgent(AgentConfig(target="gemini", capabilities=caps)).run("p")
    assert captured["exists"] is False


def test_execute_materializes_skills_into_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bound skill paths are copied to ``<cwd>/.gemini/skills/<name>/SKILL.md``
    and ``skills.enabled`` is set in settings.json."""
    src = tmp_path / "skills" / "my-skill"
    src.mkdir(parents=True)
    skill_text = "---\nname: my-skill\ndescription: do things\n---\nbody\n"
    (src / "SKILL.md").write_text(skill_text)

    captured: dict = {}

    def fake_run(argv, **kwargs):
        skill_path = os.path.join(kwargs["cwd"], ".gemini", "skills", "my-skill", "SKILL.md")
        captured["skill_exists"] = os.path.exists(skill_path)
        if captured["skill_exists"]:
            with open(skill_path) as f:
                captured["skill_text"] = f.read()
        else:
            captured["skill_text"] = None
        settings_path = os.path.join(kwargs["cwd"], ".gemini", "settings.json")
        with open(settings_path) as f:
            captured["settings"] = json.load(f)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(gemini_mod, "run", fake_run)
    caps = AllCapabilities(skills=SkillBinding(paths=(str(tmp_path / "skills"),)))
    GeminiCliAgent(AgentConfig(target="gemini", capabilities=caps)).run("p")

    assert captured["skill_exists"], "skill must be materialized before subprocess"
    assert captured["skill_text"] == skill_text
    assert captured["settings"]["skills"] == {"enabled": True}


def test_execute_warns_and_skips_missing_skill_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-existent skill path is skipped (no settings.json, no crash)."""
    captured: dict = {}

    def fake_run(argv, **kwargs):
        settings_path = os.path.join(kwargs["cwd"], ".gemini", "settings.json")
        captured["exists"] = os.path.exists(settings_path)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(gemini_mod, "run", fake_run)
    caps = AllCapabilities(skills=SkillBinding(paths=("/no/such/skills/dir",)))
    GeminiCliAgent(AgentConfig(target="gemini", capabilities=caps)).run("p")
    assert captured["exists"] is False


# ---------------------------------------------------------------------------
# Parallel isolation: each run gets its own throwaway cwd, never a shared one.
# This is what makes concurrent gemini runs safe on a single host — the binary
# reads/writes its workspace `.gemini` from cwd, so two runs sharing a cwd would
# clobber each other's settings/skills. The refactored arm also parses the
# trajectory from stdout (see parse_stream_json tests), so it never touches the
# shared `~/.gemini/tmp/.../chats` dir the legacy arm relies on.
# ---------------------------------------------------------------------------


def test_execute_runs_in_isolated_temp_cwd_not_user_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cwd is a fresh temp dir (prefix ``gemini-run-``), not the process cwd
    and not under the user's ``~/.gemini``."""
    import tempfile

    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["cwd"] = kwargs.get("cwd")
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(gemini_mod, "run", fake_run)
    GeminiCliAgent(AgentConfig(target="gemini")).run("p")

    cwd = captured["cwd"]
    assert cwd is not None
    assert os.path.basename(cwd).startswith("gemini-run-")
    # Sandboxed under the OS temp root, and distinct from the process cwd.
    assert os.path.realpath(cwd).startswith(os.path.realpath(tempfile.gettempdir()))
    assert os.path.realpath(cwd) != os.path.realpath(os.getcwd())
    # The user-level gemini config dir must never be used as the workspace.
    assert os.path.expanduser("~/.gemini") not in os.path.realpath(cwd)


def test_execute_uses_distinct_cwd_per_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two runs never share a working directory — the core parallel-safety
    invariant. Each ``_execute`` mints its own ``TemporaryDirectory``."""
    cwds: list[str] = []

    def fake_run(argv, **kwargs):
        cwds.append(kwargs.get("cwd"))
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(gemini_mod, "run", fake_run)
    # Two separate agents and a re-run of one — all must get unique cwds.
    GeminiCliAgent(AgentConfig(target="gemini")).run("p")
    agent = GeminiCliAgent(AgentConfig(target="gemini"))
    agent.run("p")
    agent.run("p")

    assert len(cwds) == 3
    assert all(c is not None for c in cwds)
    assert len(set(cwds)) == 3, f"cwds must be unique per run, got {cwds}"
