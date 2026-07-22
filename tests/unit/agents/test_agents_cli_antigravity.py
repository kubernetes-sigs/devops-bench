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

"""Unit tests for devops_bench.agents.cli.antigravity."""

from __future__ import annotations

import json
import pathlib
import sqlite3
from types import SimpleNamespace
from unittest import mock

from devops_bench.agents import capabilities
from devops_bench.agents import config as agents_config
from devops_bench.agents.cli.antigravity import agent as agy_mod
from devops_bench.agents.cli.antigravity import parsing
from devops_bench.core import subprocess as devops_subprocess
from devops_bench.core.errors import SubprocessError


def _jsonl(*records: dict) -> str:
    """Render a list of records as a JSONL blob."""
    return "\n".join(json.dumps(record) for record in records) + "\n"


SAMPLE_SESSION = _jsonl(
    {"sessionId": "session-123"},
    {
        "id": "msg-1",
        "type": "user",
        "content": "List GKE clusters and get details of cluster-a",
    },
    {
        "id": "msg-2",
        "type": "gemini",
        "content": "",
        "toolCalls": [
            {
                "name": "mcp_gke_list_clusters",
                "args": {"project": "p1"},
                "result": [
                    {
                        "functionResponse": {
                            "name": "mcp_gke_list_clusters",
                            "response": {"output": "cluster-a, cluster-b"},
                        }
                    }
                ],
            }
        ],
        "tokens": {"input": 10, "output": 5},
    },
    {
        "id": "msg-3",
        "type": "gemini",
        "content": "I found cluster-a. Let me get its details.",
        "toolCalls": [
            {
                "name": "mcp_gke_get_cluster",
                "args": {"cluster": "cluster-a"},
                "result": [
                    {
                        "functionResponse": {
                            "name": "mcp_gke_get_cluster",
                            "response": {"output": "v1.30", "is_error": False},
                        }
                    }
                ],
            }
        ],
        "tokens": {"input": 15, "output": 8},
    },
    {
        "id": "msg-4",
        "type": "gemini",
        "content": "Done. Cluster-a is running v1.30.",
        "tokens": {"input": 20, "output": 10},
    },
)

SAMPLE_TRANSCRIPT = _jsonl(
    {
        "step_index": 0,
        "source": "USER_EXPLICIT",
        "type": "USER_INPUT",
        "status": "DONE",
        "content": "Configure redirect",
    },
    {
        "step_index": 2,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "tool_calls": [
            {
                "name": "run_command",
                "args": {"CommandLine": "pwd"},
            }
        ],
    },
    {
        "step_index": 3,
        "source": "MODEL",
        "type": "RUN_COMMAND",
        "status": "DONE",
        "content": "/workspace",
    },
    {
        "step_index": 5,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "content": "Done configuring redirect",
    },
)


def test_parse_session_jsonl_emits_canonical_trajectory():
    output, trajectory, tokens, errors = parsing.parse_session_jsonl(SAMPLE_SESSION)
    assert output == "I found cluster-a. Let me get its details.Done. Cluster-a is running v1.30."
    assert tokens == {"input": 45, "output": 23, "total": 68, "cached": 0}
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


def test_parse_old_session_total_includes_cached_tokens():
    # Regression: total must include cached (input + output + cached), matching
    # the DB-path convention, not input + output alone.
    session = _jsonl(
        {"sessionId": "s"},
        {
            "id": "m1",
            "type": "gemini",
            "content": "done",
            "tokens": {"input": 10, "output": 5, "cached": 100},
        },
    )
    _output, _trajectory, tokens, _errors = parsing.parse_session_jsonl(session)
    assert tokens == {"input": 10, "output": 5, "cached": 100, "total": 115}


def test_parse_transcript_total_includes_cached_tokens():
    # Same convention for the newer step_index transcript format.
    transcript = _jsonl(
        {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "hi"},
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "content": "done",
            "tokens": {"input": 10, "output": 5, "cached": 100},
        },
    )
    _output, _trajectory, tokens, _errors = parsing.parse_session_jsonl(transcript)
    assert tokens == {"input": 10, "output": 5, "cached": 100, "total": 115}


def test_parse_session_jsonl_handles_rewinds():
    session_with_rewind = _jsonl(
        {"sessionId": "session-123"},
        {"id": "msg-1", "type": "user", "content": "hello"},
        {
            "id": "msg-2",
            "type": "gemini",
            "content": "thought 1",
            "toolCalls": [{"name": "tool1", "args": {}}],
        },
        # Rewind back to msg-1 (effectively discarding msg-2)
        {"$rewindTo": "msg-1"},
        {
            "id": "msg-3",
            "type": "gemini",
            "content": "thought 2",
            "toolCalls": [{"name": "tool2", "args": {}, "result": "ok"}],
        },
    )
    output, trajectory, _tokens, errors = parsing.parse_session_jsonl(session_with_rewind)
    assert output == "thought 2"
    assert errors == []
    assert len(trajectory) == 1
    assert trajectory[0]["name"] == "tool2"


def test_parse_session_jsonl_handles_tool_errors():
    session_with_error = _jsonl(
        {
            "id": "msg-1",
            "type": "gemini",
            "toolCalls": [
                {
                    "name": "fail_tool",
                    "args": {},
                    "result": [
                        {
                            "functionResponse": {
                                "name": "fail_tool",
                                "response": {"output": "permission denied", "is_error": True},
                            }
                        }
                    ],
                }
            ],
        }
    )
    _, trajectory, _, _ = parsing.parse_session_jsonl(session_with_error)
    assert trajectory[0]["status"] == "error"
    assert trajectory[0]["result"] == "permission denied"


def test_parse_transcript_jsonl():
    output, trajectory, tokens, errors = parsing.parse_session_jsonl(SAMPLE_TRANSCRIPT)
    assert output == "Done configuring redirect"
    assert errors == []
    assert trajectory == [
        {
            "name": "run_command",
            "args": {"CommandLine": "pwd"},
            "result": "/workspace",
            "status": "completed",
        }
    ]


def test_parse_transcript_jsonl_marks_non_done_result_as_error():
    transcript = _jsonl(
        {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "go"},
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "tool_calls": [{"name": "run_command", "args": {"CommandLine": "false"}}],
        },
        {
            "step_index": 2,
            "source": "MODEL",
            "type": "RUN_COMMAND",
            "status": "FAILED",
            "content": "command not found",
        },
    )
    _, trajectory, _, _ = parsing.parse_session_jsonl(transcript)
    assert trajectory == [
        {
            "name": "run_command",
            "args": {"CommandLine": "false"},
            "result": "command not found",
            "status": "error",
        }
    ]


def test_parse_transcript_jsonl_marks_trailing_pending_calls_as_interrupted():
    transcript = _jsonl(
        {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "go"},
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "tool_calls": [{"name": "long_running_tool", "args": {}}],
        },
        # No matching result record follows: the run ended mid-tool-call.
    )
    _, trajectory, _, _ = parsing.parse_session_jsonl(transcript)
    assert trajectory == [
        {
            "name": "long_running_tool",
            "args": {},
            "result": None,
            "status": "interrupted",
        }
    ]


def test_build_settings_renders_mcp_and_model():
    mcp = capabilities.McpBinding(name="gke", command=("gke-mcp", "run"))
    settings = agy_mod._build_settings(
        (mcp,), "google/gemini-3.5-flash", "my-project", "us-east1", skills_enabled=True
    )

    assert settings["experimental"]["skills"] is True
    assert settings["modelConfigs"]["defaultModel"] == "gemini-3.5-flash"
    assert settings["mcpServers"]["gke"] == {
        "command": "gke-mcp",
        "args": ["run"],
    }
    assert settings["gcp"] == {
        "project": "my-project",
        "location": "us-east1",
    }


def test_build_settings_omits_skills_block_when_disabled():
    settings = agy_mod._build_settings((), "google/gemini-3.5-flash")

    assert "experimental" not in settings
    # No mcp servers, project, or location either: only modelConfigs remains.
    assert settings == {"modelConfigs": {"defaultModel": "gemini-3.5-flash"}}


def test_build_env_sets_auth_and_presets():
    config = agents_config.AgentConfig(
        model="gemini-3.5-flash",
        api_key="secret-key",
    )
    env = agy_mod._build_env(config)

    assert "HOME" not in env  # HOME must not be overridden
    assert env["GEMINI_CLI_TRUST_WORKSPACE"] == "true"
    assert env["GEMINI_API_KEY"] == "secret-key"
    assert env["GOOGLE_API_KEY"] == "secret-key"
    assert env["GEMINI_MODEL"] == "gemini-3.5-flash"
    assert env["OTEL_SDK_DISABLED"] == "true"


def test_build_env_resolves_provider_qualified_model_name():
    # GEMINI_MODEL must match the bare id used by --model= and modelConfigs,
    # not the raw "provider/model" form.
    config = agents_config.AgentConfig(model="google/gemini-3.5-flash")
    env = agy_mod._build_env(config)

    assert env["GEMINI_MODEL"] == "gemini-3.5-flash"


@mock.patch.object(pathlib.Path, "home")
@mock.patch.object(devops_subprocess, "run")
def test_agy_cli_agent_execute_flow(mock_run, mock_home, tmp_path):
    # Mock Path.home() to return a temp directory to avoid polluting real HOME
    mock_home.return_value = tmp_path

    mock_run.return_value = SimpleNamespace(
        args=["agy"],
        returncode=0,
        stdout="Success",
        stderr="",
    )

    # Mock the session file writing that agy would do in the mocked HOME
    def side_effect(*args, **kwargs):
        cwd = kwargs.get("cwd") or tmp_path
        root_dir = cwd / ".gemini" / "antigravity-cli"
        conv_dir = root_dir / "conversations"
        conv_dir.mkdir(parents=True, exist_ok=True)
        uuid = "test-uuid-123"
        (conv_dir / f"{uuid}.db").write_text("", encoding="utf-8")

        transcript_dir = root_dir / "brain" / uuid / ".system_generated" / "logs"
        transcript_dir.mkdir(parents=True, exist_ok=True)
        (transcript_dir / "transcript.jsonl").write_text(SAMPLE_SESSION, encoding="utf-8")
        return mock_run.return_value

    mock_run.side_effect = side_effect

    config = agents_config.AgentConfig(
        target="/bin/agy",
        model="gemini-3.5-flash",
        capabilities=capabilities.AllCapabilities(),
    )
    agent = agy_mod.AgyCliAgent(config)

    result = agent._execute("run task")

    assert (
        result.output
        == "I found cluster-a. Let me get its details.Done. Cluster-a is running v1.30."
    )
    assert len(result.trajectory) == 2
    assert result.errors == []
    assert mock_run.called

    # Verify argv
    args = mock_run.call_args[0][0]
    assert args[0] == "/bin/agy"
    assert "--dangerously-skip-permissions" in args
    assert "--prompt=run task" in args
    assert any(a.startswith("--gemini_dir=") for a in args)


def _write_sample_transcript(
    cwd: pathlib.Path, *, db_turns: list[bytes] | None = None, transcript: str | None = None
) -> None:
    """Lay down agy's on-disk session layout: transcript + conversation DB.

    ``db_turns`` populates a conversation DB with usage records; ``None`` writes
    an empty file (no usage). ``transcript`` defaults to ``SAMPLE_SESSION``.
    """
    root_dir = cwd / ".gemini" / "antigravity-cli"
    conv_dir = root_dir / "conversations"
    conv_dir.mkdir(parents=True, exist_ok=True)
    uuid = "test-uuid-123"
    if db_turns is None:
        (conv_dir / f"{uuid}.db").write_text("", encoding="utf-8")
    else:
        _make_conv_db(conv_dir / f"{uuid}.db", db_turns)
    transcript_dir = root_dir / "brain" / uuid / ".system_generated" / "logs"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    (transcript_dir / "transcript.jsonl").write_text(
        SAMPLE_SESSION if transcript is None else transcript, encoding="utf-8"
    )


@mock.patch.object(pathlib.Path, "home")
@mock.patch.object(devops_subprocess, "run")
def test_agy_cli_agent_execute_flow_nonzero_exit_records_error_and_metadata(
    mock_run, mock_home, tmp_path
):
    mock_home.return_value = tmp_path
    mock_run.return_value = SimpleNamespace(args=["agy"], returncode=1, stdout="", stderr="boom")

    def side_effect(*args, **kwargs):
        _write_sample_transcript(kwargs.get("cwd") or tmp_path)
        return mock_run.return_value

    mock_run.side_effect = side_effect

    config = agents_config.AgentConfig(
        target="/bin/agy",
        model="gemini-3.5-flash",
        capabilities=capabilities.AllCapabilities(),
    )
    result = agy_mod.AgyCliAgent(config)._execute("run task")

    assert result.errors == ["agy exited 1: boom"]
    assert result.metadata["returncode"] == 1
    # The transcript was still recovered even though the run failed.
    assert "Cluster-a is running v1.30" in result.output


@mock.patch.object(pathlib.Path, "home")
@mock.patch.object(devops_subprocess, "run")
def test_agy_cli_agent_execute_flow_missing_transcript_falls_back_to_stdout(
    mock_run, mock_home, tmp_path
):
    mock_home.return_value = tmp_path
    # No side_effect: agy writes no conversations/transcript this run.
    mock_run.return_value = SimpleNamespace(
        args=["agy"], returncode=0, stdout="raw agy stdout", stderr=""
    )

    config = agents_config.AgentConfig(
        target="/bin/agy",
        model="gemini-3.5-flash",
        capabilities=capabilities.AllCapabilities(),
    )
    result = agy_mod.AgyCliAgent(config)._execute("run task")

    assert result.output == "raw agy stdout"
    assert result.trajectory == []
    assert "Empty session log" in result.errors


def test_empty_tokens_all_none():
    assert parsing.empty_tokens() == {
        "input": None,
        "cached": None,
        "cache_write": None,
        "reasoning": None,
        "output": None,
        "total": None,
    }


# --- token usage from the conversation DB ----------------------------------


def _pb_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def _pb_vfield(field_num: int, val: int) -> bytes:
    return _pb_varint(field_num << 3) + _pb_varint(val)  # wire type 0


def _pb_lfield(field_num: int, payload: bytes) -> bytes:
    return _pb_varint(field_num << 3 | 2) + _pb_varint(len(payload)) + payload  # wire type 2


def _usage_blob(inp, cached, reasoning, output, *, f3=None):
    """Build a gen_metadata blob with a usage record at wire path .1.4.

    Fields: f2=input, f5=cached, f9=reasoning, f10=output, f3=f9+f10.
    Zero-valued scalars are omitted, mirroring proto3 wire encoding.
    """
    stats = b""
    if inp:
        stats += _pb_vfield(2, inp)
    if cached:
        stats += _pb_vfield(5, cached)
    if reasoning:
        stats += _pb_vfield(9, reasoning)
    if output:
        stats += _pb_vfield(10, output)
    stats += _pb_vfield(3, reasoning + output if f3 is None else f3)
    return _pb_lfield(1, _pb_lfield(4, stats))  # outer{1: mid{4: stats}} -> .1.4


def _make_conv_db(path, turns, *, with_gen_metadata=True):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE trajectory_meta (trajectory_id text)")
    con.execute("INSERT INTO trajectory_meta VALUES ('t')")
    if with_gen_metadata:
        con.execute("CREATE TABLE gen_metadata (idx integer, data blob, size integer)")
        for i, blob in enumerate(turns):
            con.execute("INSERT INTO gen_metadata VALUES (?, ?, ?)", (i, blob, len(blob)))
    con.commit()
    con.close()


def test_db_token_state_ready_single_turn(tmp_path):
    db = tmp_path / "conv.db"
    _make_conv_db(db, [_usage_blob(14882, 0, 0, 7)])
    state, tokens = parsing.db_token_state(db)
    assert state == "ready"
    assert tokens == {
        "input": 14882,
        "cached": 0,
        "cache_write": None,
        "reasoning": 0,
        "output": 7,
        "total": 14889,
    }


def test_db_token_state_sums_turns_with_cache_read(tmp_path):
    db = tmp_path / "conv.db"
    _make_conv_db(db, [_usage_blob(16592, 0, 234, 51), _usage_blob(4752, 12200, 352, 50)])
    state, tokens = parsing.db_token_state(db)
    assert state == "ready"
    assert tokens["input"] == 16592 + 4752
    assert tokens["cached"] == 12200  # f5
    assert tokens["reasoning"] == 234 + 352
    assert tokens["output"] == 51 + 50
    assert tokens["cache_write"] is None
    assert (
        tokens["total"]
        == tokens["input"] + tokens["cached"] + tokens["reasoning"] + tokens["output"]
    )


def test_db_token_state_counts_fully_cached_turn(tmp_path):
    # A turn served entirely from cache omits input_tokens (proto3 drops zero
    # fields); it must still be counted, not dropped.
    db = tmp_path / "conv.db"
    _make_conv_db(db, [_usage_blob(0, 12000, 30, 20)])
    state, tokens = parsing.db_token_state(db)
    assert state == "ready"
    assert tokens["input"] == 0
    assert tokens["cached"] == 12000
    assert tokens["reasoning"] == 30
    assert tokens["output"] == 20
    assert tokens["total"] == 12050


def test_db_token_state_counts_thinking_only_turn(tmp_path):
    # A turn with zero response tokens omits f10 (proto3); it must still be
    # counted via f3 == f9.
    db = tmp_path / "conv.db"
    _make_conv_db(db, [_usage_blob(8000, 0, 500, 0)])
    state, tokens = parsing.db_token_state(db)
    assert state == "ready"
    assert tokens["input"] == 8000
    assert tokens["reasoning"] == 500
    assert tokens["output"] == 0


def test_db_token_state_rejects_noise_record_with_zero_f3(tmp_path):
    # Config-shaped noise (f3 present but 0, no f9/f10) must not match.
    noise = _pb_lfield(1, _pb_lfield(4, _pb_vfield(1, 50) + _pb_vfield(3, 0) + _pb_vfield(5, 1)))
    db = tmp_path / "conv.db"
    _make_conv_db(db, [noise])
    assert parsing.db_token_state(db) == ("undecodable", None)


def test_db_token_state_undecodable_when_invariant_fails(tmp_path):
    # f3 != f9 + f10 -> schema drift, terminal.
    db = tmp_path / "conv.db"
    _make_conv_db(db, [_usage_blob(100, 0, 9, 5, f3=999)])
    assert parsing.db_token_state(db) == ("undecodable", None)


def test_db_token_state_pending_when_usage_not_flushed(tmp_path):
    # No gen_metadata table yet, or table exists with zero rows: flush pending.
    db1 = tmp_path / "conv1.db"
    _make_conv_db(db1, [], with_gen_metadata=False)
    assert parsing.db_token_state(db1) == ("pending", None)
    db2 = tmp_path / "conv2.db"
    _make_conv_db(db2, [])
    assert parsing.db_token_state(db2) == ("pending", None)


def test_db_token_state_pending_on_transient_read_error(tmp_path, monkeypatch):
    # A locked/half-written DB during agy's async post-exit flush raises a
    # transient sqlite error; it must be retryable ("pending"), not terminal
    # ("absent"), so the poll loop waits for the flush to finish.
    db = tmp_path / "conv.db"
    _make_conv_db(db, [_usage_blob(100, 0, 9, 5)])

    class _LockedCon:
        def execute(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        def close(self):
            pass

    monkeypatch.setattr(parsing.sqlite3, "connect", lambda *a, **k: _LockedCon())
    assert parsing.db_token_state(db) == ("pending", None)


def test_db_token_state_absent_for_missing_or_nonconversation_db(tmp_path):
    assert parsing.db_token_state(tmp_path / "nope.db") == ("absent", None)
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")  # 0-byte file -> valid empty sqlite, no agy tables
    assert parsing.db_token_state(empty) == ("absent", None)


@mock.patch.object(pathlib.Path, "home")
@mock.patch.object(devops_subprocess, "run")
def test_agy_cli_agent_execute_flow_timeout_recovers_partial_transcript(
    mock_run, mock_home, tmp_path
):
    # Regression test: core.subprocess.run raises SubprocessError on a timeout
    # even with check=False. The transcript agy wrote before being killed
    # must still be recovered instead of being lost with the tempdir.
    mock_home.return_value = tmp_path

    def side_effect(*args, **kwargs):
        cwd = kwargs.get("cwd")
        if cwd is None:
            return SimpleNamespace(args=["gcloud"], returncode=1, stdout="", stderr="")
        _write_sample_transcript(cwd)
        raise SubprocessError(["agy"], returncode=-1, stdout="", stderr="")

    mock_run.side_effect = side_effect

    config = agents_config.AgentConfig(
        target="/bin/agy",
        model="gemini-3.5-flash",
        capabilities=capabilities.AllCapabilities(),
        timeout_sec=1,
    )
    result = agy_mod.AgyCliAgent(config)._execute("run task")

    assert len(result.trajectory) == 2
    assert any("subprocess error" in e for e in result.errors)


@mock.patch.object(pathlib.Path, "home")
@mock.patch.object(devops_subprocess, "run")
def test_agy_cli_agent_execute_reads_tokens_from_db(mock_run, mock_home, tmp_path):
    # Tokens (incl. cached) come from the conversation DB; output + trajectory
    # from the transcript.
    mock_home.return_value = tmp_path
    result_ns = SimpleNamespace(args=["agy"], returncode=0, stdout="done", stderr="")

    def side_effect(*args, **kwargs):
        cwd = kwargs.get("cwd")
        if cwd is None:
            return SimpleNamespace(args=["gcloud"], returncode=1, stdout="", stderr="")
        _write_sample_transcript(cwd, db_turns=[_usage_blob(4752, 12200, 30, 20)])
        return result_ns

    mock_run.side_effect = side_effect

    config = agents_config.AgentConfig(
        target="/bin/agy",
        model="gemini-3.5-flash",
        capabilities=capabilities.AllCapabilities(),
    )
    result = agy_mod.AgyCliAgent(config)._execute("run task")

    assert result.tokens == {
        "input": 4752,
        "cached": 12200,
        "cache_write": None,
        "reasoning": 30,
        "output": 20,
        "total": 4752 + 12200 + 30 + 20,
    }
    assert result.metadata["token_source"] == "db"
    assert len(result.trajectory) == 2


@mock.patch.object(pathlib.Path, "home")
@mock.patch.object(devops_subprocess, "run")
def test_agy_cli_agent_execute_falls_back_to_transcript_tokens(mock_run, mock_home, tmp_path):
    # DB has no usage but the (old-format) transcript carries per-record tokens:
    # fall back to those instead of reporting all-None.
    mock_home.return_value = tmp_path
    plain = SimpleNamespace(args=["agy"], returncode=0, stdout="plain text output", stderr="")

    def side_effect(*args, **kwargs):
        cwd = kwargs.get("cwd")
        if cwd is None:
            return SimpleNamespace(args=["gcloud"], returncode=1, stdout="", stderr="")
        _write_sample_transcript(cwd)  # empty .db; SAMPLE_SESSION has tokens
        return plain

    mock_run.side_effect = side_effect

    config = agents_config.AgentConfig(
        target="/bin/agy",
        model="gemini-3.5-flash",
        capabilities=capabilities.AllCapabilities(),
    )
    result = agy_mod.AgyCliAgent(config)._execute("run task")

    # SAMPLE_SESSION sums: input 45, output 23, total 68, cached 0; reasoning and
    # cache_write are unknowable from the old shape.
    assert result.tokens == {
        "input": 45,
        "cached": 0,
        "cache_write": None,
        "reasoning": None,
        "output": 23,
        "total": 68,
    }
    assert result.metadata["token_source"] == "transcript"


@mock.patch.object(pathlib.Path, "home")
@mock.patch.object(devops_subprocess, "run")
def test_agy_cli_agent_execute_emits_none_tokens_when_no_source(mock_run, mock_home, tmp_path):
    # No usage in the DB and a token-less (new-format) transcript -> all-None
    # (never a fabricated zero), flagged as unavailable.
    mock_home.return_value = tmp_path
    plain = SimpleNamespace(args=["agy"], returncode=0, stdout="plain text output", stderr="")

    def side_effect(*args, **kwargs):
        cwd = kwargs.get("cwd")
        if cwd is None:
            return SimpleNamespace(args=["gcloud"], returncode=1, stdout="", stderr="")
        _write_sample_transcript(cwd, transcript=SAMPLE_TRANSCRIPT)  # no tokens anywhere
        return plain

    mock_run.side_effect = side_effect

    config = agents_config.AgentConfig(
        target="/bin/agy",
        model="gemini-3.5-flash",
        capabilities=capabilities.AllCapabilities(),
    )
    result = agy_mod.AgyCliAgent(config)._execute("run task")

    assert result.tokens == parsing.empty_tokens()
    assert result.metadata["token_source"] == "unavailable"
