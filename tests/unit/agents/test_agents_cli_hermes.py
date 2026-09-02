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

"""Unit tests for the Hermes CLI agent harness and its ``state.db`` parsers."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from ruamel.yaml import YAML

from devops_bench.agents.base import AGENTS
from devops_bench.agents.capabilities import AllCapabilities, McpBinding, SkillBinding
from devops_bench.agents.cli.hermes import agent as agent_mod
from devops_bench.agents.cli.hermes.agent import HermesAgent, _build_env
from devops_bench.agents.cli.hermes.parsing import (
    extract_tokens_from_db,
    extract_trajectory_from_db,
)
from devops_bench.agents.config import AgentConfig
from devops_bench.agents.result import TOKEN_BUCKETS, empty_tokens
from devops_bench.core.errors import ConfigError, SubprocessError
from devops_bench.results.normalize import normalize_tokens

_yaml = YAML(typ="safe")

_TOKEN_COLUMNS = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Path a test ``state.db`` is created at (the file itself is not made)."""
    return tmp_path / "state.db"


def _init_schema(path: Path) -> None:
    """Create the ``sessions`` / ``messages`` tables hermes writes."""
    token_columns = "".join(f", {column} INTEGER" for column in _TOKEN_COLUMNS)
    conn = sqlite3.connect(path)
    conn.execute(
        f"CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at TIMESTAMP{token_columns})"
    )
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,"
        " role TEXT, content TEXT, tool_calls TEXT, tool_call_id TEXT, tool_name TEXT)"
    )
    conn.commit()
    conn.close()


def _insert_session(path: Path, session_id: str, *counts: int | float | None) -> None:
    conn = sqlite3.connect(path)
    columns = ", ".join(_TOKEN_COLUMNS)
    placeholders = ", ".join("?" * len(_TOKEN_COLUMNS))
    conn.execute(
        f"INSERT INTO sessions (id, started_at, {columns}) VALUES (?, NULL, {placeholders})",
        (session_id, *counts),
    )
    conn.commit()
    conn.close()


def _insert_message(path: Path, session_id: str, role: str, **fields: str | None) -> None:
    conn = sqlite3.connect(path)
    keys = ["session_id", "role", *fields]
    placeholders = ", ".join("?" * len(keys))
    conn.execute(
        f"INSERT INTO messages ({', '.join(keys)}) VALUES ({placeholders})",
        (session_id, role, *fields.values()),
    )
    conn.commit()
    conn.close()


def _tool_calls_json(call_id: str | None, name: str | None, args: dict | str) -> str:
    function: dict = {"arguments": args if isinstance(args, str) else json.dumps(args)}
    if name is not None:
        function["name"] = name
    entry: dict = {"type": "function", "function": function}
    if call_id is not None:
        entry["id"] = call_id
    return json.dumps([entry])


# --- registration & configuration -------------------------------------------


def test_registers_under_the_canonical_key() -> None:
    """The eval harness resolves the agent by its lowercase registry key."""
    assert AGENTS.get("hermes") is HermesAgent


def test_build_env_routes_the_api_key_to_the_provider_vars() -> None:
    env = _build_env(
        AgentConfig(provider="google", api_key="test-key", extra_env={"FOO": "BAR"}),
    )

    assert env["GEMINI_API_KEY"] == "test-key"
    assert env["GOOGLE_API_KEY"] == "test-key"
    assert env["FOO"] == "BAR"


def test_build_env_omits_key_vars_when_no_key_is_configured() -> None:
    """A keyless (ADC / Vertex) run must not export an empty credential."""
    assert _build_env(AgentConfig(provider="google-vertex")) == {}


def test_resolve_hermes_bin_prefers_the_configured_target() -> None:
    agent = HermesAgent(AgentConfig(target="/custom/bin/hermes"))
    assert agent._resolve_hermes_bin() == "/custom/bin/hermes"


@patch("os.path.exists")
def test_resolve_hermes_bin_falls_back_to_path_lookup(mock_exists: MagicMock) -> None:
    agent = HermesAgent(AgentConfig(target=None))

    mock_exists.return_value = True
    assert agent._resolve_hermes_bin() == os.path.expanduser("~/.local/bin/hermes")

    mock_exists.return_value = False
    assert agent._resolve_hermes_bin() == "hermes"


def test_build_command_maps_vertex_onto_the_vertex_backend() -> None:
    cmd = HermesAgent(
        AgentConfig(target="/bin/hermes", model="gemini-3-pro", provider="google-vertex")
    )._build_command("hi")

    assert cmd == [
        "/bin/hermes",
        "chat",
        "--query=hi",
        "-m",
        "gemini-3-pro",
        "--provider",
        "vertex",
    ]


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("google", "gemini"),
        ("google-vertex", "vertex"),
        ("anthropic", "anthropic"),
        ("anthropic-bedrock", "bedrock"),
        ("openai", "openai-api"),
        ("ollama", "ollama"),
    ],
)
def test_build_command_maps_each_provider_onto_a_hermes_provider_name(
    provider: str, expected: str
) -> None:
    """The emitted name must be one hermes's own registry accepts."""
    cmd = HermesAgent(AgentConfig(target="/bin/hermes", provider=provider))._build_command("hi")

    assert cmd == ["/bin/hermes", "chat", "--query=hi", "--provider", expected]


def test_build_command_rejects_a_provider_hermes_cannot_serve() -> None:
    """Hermes's ``vertex`` is Gemini-only, so Claude-on-Vertex must not silently
    route to a Gemini model."""
    agent = HermesAgent(AgentConfig(target="/bin/hermes", provider="anthropic-vertex"))

    with pytest.raises(ConfigError, match="no hermes equivalent"):
        agent._build_command("hi")


def test_build_command_binds_a_dash_leading_prompt_to_the_query_flag() -> None:
    """A prompt starting with ``-`` must not be parsed as a hermes flag."""
    cmd = HermesAgent(AgentConfig(target="/bin/hermes"))._build_command("--help me")

    assert cmd == ["/bin/hermes", "chat", "--query=--help me"]


def test_prepare_config_writes_the_granted_mcp_servers(tmp_path: Path) -> None:
    agent = HermesAgent(AgentConfig())
    binding = McpBinding(name="k8s", command=("k8s-mcp", "--stdio"))

    agent._prepare_config(tmp_path, (binding,))

    data = _yaml.load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert data["mcp_servers"] == {"k8s": {"command": "k8s-mcp", "args": ["--stdio"]}}


def test_prepare_config_opts_out_of_the_skills_the_run_never_granted(tmp_path: Path) -> None:
    """Hermes installs 82 bundled skills and sources repo-local ones; either
    would hand the agent capabilities the matrix withheld."""
    HermesAgent(AgentConfig())._prepare_config(tmp_path, ())

    assert (tmp_path / ".no-bundled-skills").exists()
    data = _yaml.load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert data["skills"]["project_discovery"] is False


def test_prune_install_artifacts_keeps_the_run_evidence(tmp_path: Path) -> None:
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "tirith").write_bytes(b"\x00")
    (tmp_path / "cache").mkdir()
    (tmp_path / "models_dev_cache.json").write_text("{}", encoding="utf-8")
    (tmp_path / "state.db").write_bytes(b"\x00")
    (tmp_path / "config.yaml").write_text("{}", encoding="utf-8")

    agent_mod._prune_install_artifacts(tmp_path)

    assert not (tmp_path / "bin").exists()
    assert not (tmp_path / "cache").exists()
    assert not (tmp_path / "models_dev_cache.json").exists()
    assert (tmp_path / "state.db").exists()
    assert (tmp_path / "config.yaml").exists()


def test_prepare_config_forwards_the_run_isolation_env_to_mcp_servers(tmp_path: Path) -> None:
    """hermes filters an MCP child's env to an allowlist plus the declared keys,
    so the run's cluster and cloud config have to be named explicitly."""
    agent = HermesAgent(AgentConfig())

    with patch.dict(
        os.environ, {"KUBECONFIG": "/run/kubeconfig", "CLOUDSDK_CONFIG": "/run/gcloud"}
    ):
        agent._prepare_config(tmp_path, (McpBinding(name="k8s", command=("k8s-mcp",)),))

    data = _yaml.load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert data["mcp_servers"]["k8s"]["env"] == {
        "KUBECONFIG": "/run/kubeconfig",
        "CLOUDSDK_CONFIG": "/run/gcloud",
    }


def test_prepare_config_prefers_the_configured_isolation_override(tmp_path: Path) -> None:
    """``extra_env`` wins over the ambient value, as it does in ``_build_env``."""
    agent = HermesAgent(AgentConfig(extra_env={"KUBECONFIG": "/override/kubeconfig"}))

    with patch.dict(os.environ, {"KUBECONFIG": "/run/kubeconfig"}):
        agent._prepare_config(tmp_path, (McpBinding(name="k8s", command=("k8s-mcp",)),))

    data = _yaml.load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert data["mcp_servers"]["k8s"]["env"]["KUBECONFIG"] == "/override/kubeconfig"


def test_prepare_config_omits_the_env_block_without_isolation(tmp_path: Path) -> None:
    """A non-isolated run leaves the server on the ambient environment."""
    agent = HermesAgent(AgentConfig())

    with patch.dict(os.environ, {}, clear=True):
        agent._prepare_config(tmp_path, (McpBinding(name="k8s", command=("k8s-mcp",)),))

    data = _yaml.load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert "env" not in data["mcp_servers"]["k8s"]


def test_prepare_config_does_not_read_user_state_by_default(tmp_path: Path) -> None:
    """A benchmark run is reproducible: ``~/.hermes`` is not inherited."""
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    (home / ".hermes" / "SOUL.md").write_text("ambient", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with patch.dict(os.environ, {"HOME": str(home)}):
        HermesAgent(AgentConfig())._prepare_config(run_dir, ())

    assert not (run_dir / "SOUL.md").exists()


_DEFAULT_COUNTS: tuple[int, ...] = (2748, 11267, 152, 334987, 12000)


def _seed_state_db(home: Path, *, counts: tuple[int, ...] = _DEFAULT_COUNTS) -> None:
    _init_schema(home / "state.db")
    _insert_session(home / "state.db", "s1", *counts)


def _fake_run(
    *,
    returncode: int = 0,
    stdout: str = "ok",
    stderr: str = "",
    counts: tuple[int, ...] = _DEFAULT_COUNTS,
    seed: Callable[[Path], None] | None = None,
    seen: dict | None = None,
) -> Callable[..., SimpleNamespace]:
    """Build a ``core.subprocess.run`` stand-in that seeds the run's ``state.db``.

    Tolerant of extra keywords on purpose: pinning ``run``'s exact signature in
    every test means one new keyword there breaks all of them at once.

    Args:
        returncode: Exit status the fake reports.
        stdout: Captured stdout the fake reports.
        stderr: Captured stderr the fake reports.
        counts: Session token counts to seed, in ``_TOKEN_COLUMNS`` order.
        seed: Optional extra seeding (e.g. messages), called with the run home.
        seen: Optional dict the call's ``cmd`` / ``cwd`` / ``home`` are recorded in.
    """

    def fake_run(cmd: list[str], **kwargs) -> SimpleNamespace:
        home = Path(kwargs["extra_env"]["HERMES_HOME"])
        _seed_state_db(home, counts=counts)
        if seed is not None:
            seed(home)
        if seen is not None:
            seen.update(cmd=cmd, cwd=kwargs["cwd"], home=str(home))
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    return fake_run


def test_execute_reports_trajectory_and_tokens_from_the_state_db() -> None:
    """End-to-end wiring: the run-scoped DB reaches ``AgentResult``."""

    def seed(home: Path) -> None:
        _insert_message(
            home / "state.db",
            "s1",
            "assistant",
            tool_calls=_tool_calls_json("call_1", "kubectl_apply", {"manifest": "nginx.yaml"}),
        )
        _insert_message(home / "state.db", "s1", "tool", content="applied", tool_call_id="call_1")

    with patch.object(agent_mod, "run", side_effect=_fake_run(stdout="done", seed=seed)):
        result = HermesAgent(AgentConfig(target="/bin/hermes")).run("hello")

    assert result.output == "done"
    assert not result.has_errors()
    assert result.trajectory == [
        {
            "name": "kubectl_apply",
            "args": {"manifest": "nginx.yaml"},
            "result": "applied",
            "status": "completed",
        }
    ]
    assert result.tokens == {
        "input": 2748,
        "cached": 334987,
        "cache_write": 12000,
        "reasoning": 152,
        "output": 11267 - 152,
        "total": 2748 + 334987 + 12000 + 11267,
    }


def test_execute_runs_in_the_harness_workspace_when_given_one(tmp_path: Path) -> None:
    """hermes runs *in* the workspace but keeps its own state out of it."""
    seen: dict = {}

    with patch.object(agent_mod, "run", side_effect=_fake_run(seen=seen)):
        HermesAgent(AgentConfig(target="/bin/hermes")).run("hello", tmp_path)

    assert seen["cwd"] == str(tmp_path)
    assert seen["home"] == str(tmp_path / ".hermes")
    assert (tmp_path / ".hermes" / "config.yaml").exists()
    # Only the state home is added to the workspace, so the harness's
    # generated-file diff stays dominated by what the agent actually wrote.
    assert [entry.name for entry in tmp_path.iterdir()] == [".hermes"]


def test_execute_materializes_granted_skills_under_the_state_home(tmp_path: Path) -> None:
    src = tmp_path / "src" / "deploy"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("---\nname: deploy\n---\nsteps", encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()

    config = AgentConfig(
        target="/bin/hermes",
        capabilities=AllCapabilities(skills=SkillBinding(paths=(str(tmp_path / "src"),))),
    )
    with patch.object(agent_mod, "run", side_effect=_fake_run()):
        HermesAgent(config).run("hello", workspace)

    assert (workspace / ".hermes" / "skills" / "deploy" / "SKILL.md").exists()


def test_execute_records_a_nonzero_exit_without_losing_the_trajectory() -> None:
    fake_run = _fake_run(returncode=3, stdout="partial", stderr="boom")
    with patch.object(agent_mod, "run", side_effect=fake_run):
        result = HermesAgent(AgentConfig(target="/bin/hermes")).run("hello")

    assert result.metadata["returncode"] == 3
    assert any("hermes agent exited 3: boom" in err for err in result.errors)
    assert result.tokens["input"] == 2748


def test_execute_flags_a_zero_exit_that_never_reached_the_model() -> None:
    """hermes exits 0 on a rejected key or exhausted retries; an unflagged run
    would be scored as a bad answer instead of an infrastructure failure."""

    fake_run = _fake_run(stdout="", stderr="API key not valid", counts=(0, 0, 0, 0, 0))
    with patch.object(agent_mod, "run", side_effect=fake_run):
        result = HermesAgent(AgentConfig(target="/bin/hermes")).run("hello")

    assert result.has_errors()
    assert any("no model usage" in err and "API key not valid" in err for err in result.errors)


def test_execute_on_timeout_reports_what_the_killed_run_flushed() -> None:
    def fake_run(cmd: list[str], **kwargs) -> SimpleNamespace:
        home = Path(kwargs["extra_env"]["HERMES_HOME"])
        _seed_state_db(home)
        _insert_message(
            home / "state.db",
            "s1",
            "assistant",
            tool_calls=_tool_calls_json("call_1", "kubectl_get", {}),
        )
        raise SubprocessError(cmd, returncode=-1, stdout="out", stderr="err")

    with patch.object(agent_mod, "run", side_effect=fake_run):
        result = HermesAgent(AgentConfig(target="/bin/hermes", timeout_sec=5)).run("hello")

    assert result.metadata["timeout"] is True
    assert result.errors[0] == "hermes agent timed out after 5s"
    assert [call["name"] for call in result.trajectory] == ["kubectl_get"]
    assert result.tokens["input"] == 2748
    assert "out" in result.output and "err" in result.output


def test_execute_reports_canonical_none_tokens_when_the_binary_is_missing() -> None:
    with patch.object(agent_mod, "run", side_effect=OSError("no such file")):
        result = HermesAgent(AgentConfig(target="/bin/hermes")).run("hello")

    assert result.has_errors()
    assert result.trajectory == []
    assert result.tokens == empty_tokens()


# --- extract_trajectory_from_db ----------------------------------------------


def test_trajectory_missing_db_is_reported(db_path: Path) -> None:
    trajectory, errors = extract_trajectory_from_db(db_path)

    assert trajectory == []
    assert "State database not found" in errors[0]


def test_trajectory_non_database_file_is_reported(db_path: Path) -> None:
    db_path.write_text("not a database", encoding="utf-8")

    trajectory, errors = extract_trajectory_from_db(db_path)

    assert trajectory == []
    assert errors and errors[0].startswith("Database error:")


def test_trajectory_empty_db_reports_the_missing_table(db_path: Path) -> None:
    db_path.touch()

    trajectory, errors = extract_trajectory_from_db(db_path)

    assert trajectory == []
    assert "Database error: no such table: sessions" in errors[0]


def test_trajectory_no_sessions_is_reported(db_path: Path) -> None:
    _init_schema(db_path)

    trajectory, errors = extract_trajectory_from_db(db_path)

    assert trajectory == []
    assert "No session found in state database" in errors[0]


def test_trajectory_reads_every_session_in_insertion_order(db_path: Path) -> None:
    """All sessions are read, matching what the token sum already covers.

    Session ids are UUIDs, so ordering is insertion order, not id order — here
    the first-inserted session sorts *after* the second lexically.
    """
    _init_schema(db_path)
    _insert_session(db_path, "f3a9-uuid", *[0] * len(_TOKEN_COLUMNS))
    _insert_session(db_path, "0b21-uuid", *[0] * len(_TOKEN_COLUMNS))
    _insert_message(
        db_path, "f3a9-uuid", "assistant", tool_calls=_tool_calls_json("a", "first_call", {})
    )
    _insert_message(
        db_path, "0b21-uuid", "assistant", tool_calls=_tool_calls_json("b", "second_call", {})
    )

    trajectory, errors = extract_trajectory_from_db(db_path)

    assert errors == []
    assert [call["name"] for call in trajectory] == ["first_call", "second_call"]


def test_trajectory_pairs_calls_with_their_results(db_path: Path) -> None:
    _init_schema(db_path)
    _insert_session(db_path, "s1", *[0] * len(_TOKEN_COLUMNS))
    _insert_message(db_path, "s1", "user", content="Deploy app")
    _insert_message(
        db_path,
        "s1",
        "assistant",
        tool_calls=_tool_calls_json("call_1", "kubectl_apply", {"manifest": "nginx.yaml"}),
    )
    _insert_message(
        db_path,
        "s1",
        "tool",
        content="Successfully applied",
        tool_call_id="call_1",
        tool_name="kubectl_apply",
    )

    trajectory, errors = extract_trajectory_from_db(db_path)

    assert errors == []
    assert trajectory == [
        {
            "name": "kubectl_apply",
            "args": {"manifest": "nginx.yaml"},
            "result": "Successfully applied",
            "status": "completed",
        }
    ]


def test_trajectory_keeps_an_unanswered_call_as_called(db_path: Path) -> None:
    _init_schema(db_path)
    _insert_session(db_path, "s1", *[0] * len(_TOKEN_COLUMNS))
    _insert_message(
        db_path, "s1", "assistant", tool_calls=_tool_calls_json("call_1", "kubectl_get", {})
    )

    trajectory, errors = extract_trajectory_from_db(db_path)

    assert errors == []
    assert trajectory[0]["status"] == "called"
    assert trajectory[0]["result"] is None


def test_trajectory_malformed_tool_calls_json_is_reported(db_path: Path) -> None:
    _init_schema(db_path)
    _insert_session(db_path, "s1", *[0] * len(_TOKEN_COLUMNS))
    _insert_message(db_path, "s1", "assistant", tool_calls="[invalid json")

    trajectory, errors = extract_trajectory_from_db(db_path)

    assert trajectory == []
    assert "Failed to parse tool calls JSON" in errors[0]


def test_trajectory_malformed_json_does_not_drop_later_calls(db_path: Path) -> None:
    """One bad row must not truncate the rest of the session."""
    _init_schema(db_path)
    _insert_session(db_path, "s1", *[0] * len(_TOKEN_COLUMNS))
    _insert_message(db_path, "s1", "assistant", tool_calls="[invalid json")
    _insert_message(
        db_path, "s1", "assistant", tool_calls=_tool_calls_json("call_2", "kubectl_get", {})
    )

    trajectory, errors = extract_trajectory_from_db(db_path)

    assert len(errors) == 1
    assert [call["name"] for call in trajectory] == ["kubectl_get"]


def test_trajectory_non_json_arguments_are_preserved_raw(db_path: Path) -> None:
    _init_schema(db_path)
    _insert_session(db_path, "s1", *[0] * len(_TOKEN_COLUMNS))
    _insert_message(
        db_path,
        "s1",
        "assistant",
        tool_calls=_tool_calls_json("call_1", "bash", "kubectl get po"),
    )

    trajectory, errors = extract_trajectory_from_db(db_path)

    assert errors == []
    assert trajectory[0]["args"] == {"raw_args": "kubectl get po"}


def test_trajectory_decoded_object_arguments_are_used_as_is(db_path: Path) -> None:
    """Non-OpenAI adapters store ``arguments`` already decoded, not as a string."""
    _init_schema(db_path)
    _insert_session(db_path, "s1", *[0] * len(_TOKEN_COLUMNS))
    entry = {"id": "call_1", "function": {"name": "bash", "arguments": {"cmd": "kubectl get po"}}}
    _insert_message(db_path, "s1", "assistant", tool_calls=json.dumps([entry]))

    trajectory, errors = extract_trajectory_from_db(db_path)

    assert errors == []
    assert trajectory[0]["args"] == {"cmd": "kubectl get po"}


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        ('{"function": {}}', "Tool calls JSON is not a list"),
        ("[42]", "Skipped non-object tool call entry"),
    ],
)
def test_trajectory_malformed_tool_call_shapes_are_reported(
    db_path: Path, payload: str, expected_error: str
) -> None:
    """Valid JSON of the wrong shape must not crash the whole extraction."""
    _init_schema(db_path)
    _insert_session(db_path, "s1", *[0] * len(_TOKEN_COLUMNS))
    _insert_message(db_path, "s1", "assistant", tool_calls=payload)
    _insert_message(
        db_path, "s1", "assistant", tool_calls=_tool_calls_json("call_2", "kubectl_get", {})
    )

    trajectory, errors = extract_trajectory_from_db(db_path)

    assert expected_error in errors[0]
    assert [call["name"] for call in trajectory] == ["kubectl_get"]


def test_trajectory_non_object_function_falls_back_to_unknown(db_path: Path) -> None:
    _init_schema(db_path)
    _insert_session(db_path, "s1", *[0] * len(_TOKEN_COLUMNS))
    _insert_message(
        db_path, "s1", "assistant", tool_calls=json.dumps([{"id": "c1", "function": "bash"}])
    )

    trajectory, errors = extract_trajectory_from_db(db_path)

    assert errors == []
    assert trajectory == [{"name": "unknown", "args": {}, "result": None, "status": "called"}]


def test_trajectory_missing_tool_name_falls_back_to_unknown(db_path: Path) -> None:
    _init_schema(db_path)
    _insert_session(db_path, "s1", *[0] * len(_TOKEN_COLUMNS))
    _insert_message(
        db_path, "s1", "assistant", tool_calls=_tool_calls_json("call_1", None, {"a": 1})
    )

    trajectory, errors = extract_trajectory_from_db(db_path)

    assert errors == []
    assert trajectory[0]["name"] == "unknown"


def test_trajectory_orphan_tool_result_is_kept_and_reported(db_path: Path) -> None:
    _init_schema(db_path)
    _insert_session(db_path, "s1", *[0] * len(_TOKEN_COLUMNS))
    _insert_message(
        db_path,
        "s1",
        "tool",
        content="Some result",
        tool_call_id="call_unknown",
        tool_name="kubectl_delete",
    )

    trajectory, errors = extract_trajectory_from_db(db_path)

    assert "Found tool response for unknown tool_call_id: call_unknown" in errors[0]
    assert trajectory == [
        {"name": "kubectl_delete", "args": {}, "result": "Some result", "status": "completed"}
    ]


# --- extract_tokens_from_db --------------------------------------------------


def test_tokens_fill_every_canonical_bucket(db_path: Path) -> None:
    """The harness maps onto the shared schema — no bucket left unpopulated.

    Guards against ``TOKEN_BUCKETS`` gaining a bucket this parser silently skips.
    """
    _init_schema(db_path)
    _insert_session(db_path, "s1", 1, 2, 3, 4, 5)

    assert set(extract_tokens_from_db(db_path)) == set(TOKEN_BUCKETS)
    assert all(value is not None for value in extract_tokens_from_db(db_path).values())


def test_tokens_read_the_session_counts(db_path: Path) -> None:
    """``output`` drops the reasoning subset; ``total`` still counts it once.

    Hermes stores ``output_tokens`` as the full provider completion count and
    ``reasoning_tokens`` as a slice of it, so ``total`` here must equal Hermes's
    own ``prompt_tokens + output_tokens``.
    """
    _init_schema(db_path)
    _insert_session(db_path, "s1", 2748, 11267, 152, 334987, 12000)

    assert extract_tokens_from_db(db_path) == {
        "input": 2748,
        "cached": 334987,
        "cache_write": 12000,
        "reasoning": 152,
        "output": 11267 - 152,
        "total": 2748 + 334987 + 12000 + 11267,
    }


def test_tokens_reach_the_result_row_unchanged(db_path: Path) -> None:
    """Cross-layer guard: the emitted keys are the ones ``normalize_tokens`` reads.

    A bucket named differently here would silently normalize to ``None`` on the
    dashboard row rather than failing loudly.
    """
    _init_schema(db_path)
    _insert_session(db_path, "s1", 2748, 11267, 152, 334987, 12000)

    normalized = normalize_tokens(extract_tokens_from_db(db_path))

    assert normalized.input == 2748
    assert normalized.output == 11267 - 152
    assert normalized.cached == 334987
    assert normalized.cache_write == 12000
    assert normalized.reasoning == 152
    assert normalized.total == 2748 + 334987 + 12000 + 11267


def test_tokens_sum_across_sessions(db_path: Path) -> None:
    # The DB is run-scoped; a run may write several session rows (compaction,
    # sub-sessions), and session ids need not sort chronologically.
    _init_schema(db_path)
    _insert_session(db_path, "f3a9-uuid", 1, 1, 0, 0, 0)
    _insert_session(db_path, "0b21-uuid", 500, 40, 5, 900, 30)

    tokens = extract_tokens_from_db(db_path)

    assert tokens["input"] == 501
    assert tokens["output"] == 41 - 5
    assert tokens["cached"] == 900
    assert tokens["total"] == 501 + 41 + 900 + 30


def test_tokens_coerce_real_values(db_path: Path) -> None:
    # SQLite columns are dynamically typed; a REAL count must not be dropped.
    _init_schema(db_path)
    _insert_session(db_path, "s1", 100, 7, 0, 0, 12000.0)

    tokens = extract_tokens_from_db(db_path)

    assert tokens["cache_write"] == 12000
    assert tokens["input"] == 100


def test_tokens_null_columns_stay_none(db_path: Path) -> None:
    # NULL counts (e.g. a crashed run) must surface as None, not a fake 0.
    _init_schema(db_path)
    _insert_session(db_path, "s1", 100, 7, None, None, None)

    tokens = extract_tokens_from_db(db_path)

    assert tokens["input"] == 100
    assert tokens["output"] == 7
    assert tokens["reasoning"] is None
    assert tokens["cached"] is None
    assert tokens["cache_write"] is None
    assert tokens["total"] == 107


def test_tokens_reasoning_beyond_the_completion_total_clamps_at_zero(db_path: Path) -> None:
    """Defensive: a bad provider report must not yield a negative bucket."""
    _init_schema(db_path)
    _insert_session(db_path, "s1", 100, 7, 40, 0, 0)

    tokens = extract_tokens_from_db(db_path)

    assert tokens["output"] == 0
    assert tokens["total"] == 140


def test_tokens_survive_a_path_with_uri_delimiters(tmp_path: Path) -> None:
    """A ``?`` in the run path must not truncate the read-only URI."""
    odd_dir = tmp_path / "run?id=1"
    odd_dir.mkdir()
    db_path = odd_dir / "state.db"
    _init_schema(db_path)
    _insert_session(db_path, "s1", 100, 7, 0, 0, 0)

    assert extract_tokens_from_db(db_path)["input"] == 100


def test_tokens_no_session_rows_stay_none(db_path: Path) -> None:
    """SUM over an empty table yields NULL, which must not become 0."""
    _init_schema(db_path)

    assert extract_tokens_from_db(db_path) == empty_tokens()


def test_tokens_old_schema_without_token_columns(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at TIMESTAMP)")
    conn.commit()
    conn.close()

    assert extract_tokens_from_db(db_path) == empty_tokens()


def test_tokens_missing_or_invalid_db(db_path: Path) -> None:
    assert extract_tokens_from_db(db_path) == empty_tokens()  # no file
    db_path.write_text("not a database", encoding="utf-8")
    assert extract_tokens_from_db(db_path) == empty_tokens()
