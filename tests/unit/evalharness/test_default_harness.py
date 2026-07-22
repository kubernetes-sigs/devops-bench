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

"""Targeted unit tests for ``DefaultEvalHarness`` internals not covered elsewhere.

Tests in this file exercise the harness-level wiring beyond the agent /
scenario / metrics seams (those have their own files): the scenario-drain
timed-out path, the constructor-arg-driven deployment / namespace defaults,
the cached granted-skill-paths snapshot, and the narrowed builtin-agent
import behavior.
"""

from __future__ import annotations

import importlib
import threading
from pathlib import Path
from typing import Any

import pytest

from devops_bench.agents import AGENTS, AgentHarness
from devops_bench.agents.result import AgentResult, ToolCall
from devops_bench.core import ConfigError, MissingDependencyError
from devops_bench.evalharness import default as harness_default
from devops_bench.evalharness.default import DefaultEvalHarness
from devops_bench.tasks import Task


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip BENCH_* / AGENT_* env so the harness reads predictable defaults."""
    for var in (
        "BENCH_USE_MCP",
        "BENCH_AGENT_TYPE",
        "AGENT_MCP_SERVER",
        "AGENT_ALLOWED_TOOLS",
        "AGENT_SKILLS_PATHS",
        "AGENT_RULES_TEXT",
        "AGENT_TARGET",
        "AGENT_MODEL",
        "AGENT_PROVIDER",
        "TARGET_DEPLOYMENT_NAME",
        "NAMESPACE",
    ):
        monkeypatch.delenv(var, raising=False)


def test_parse_chaos_specs_raises_on_malformed_json(isolated_env: None) -> None:
    """A declared-but-unparseable chaos spec fails loudly rather than dropping to no chaos."""
    harness = DefaultEvalHarness(project_id="p", cluster_name="c")
    with pytest.raises(ConfigError, match="chaos_spec"):
        harness._parse_chaos_specs("{not valid json", "cluster")  # noqa: SLF001


def test_drain_scenario_stamps_timed_out_when_thread_still_alive(
    isolated_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scenario thread that outlives the join budget is flagged on the report."""
    harness = DefaultEvalHarness(project_id="p", cluster_name="c")

    # Drive the join budget to ~0 so the thread is "still alive" on return
    # without sleeping in the test. The harness reads the module global at
    # call time, so patching the attribute on the module flexes the path.
    monkeypatch.setattr(harness_default, "_SCENARIO_JOIN_SEC", 0.01)

    class _StuckScenario:
        """Stand-in scenario manager whose reports stay partial."""

        def get_reports(self) -> tuple[dict[str, Any], dict[str, Any]]:
            return ({"status": "initiated", "injected_fault": "x"}, {})

    stop_event = threading.Event()

    def _hang() -> None:
        # Hold the thread alive past the join budget so ``is_alive`` is True
        # on return; the event lets the test release the thread on cleanup.
        stop_event.wait(timeout=2.0)

    scenario_thread = threading.Thread(target=_hang, daemon=True)
    scenario_thread.start()
    try:
        chaos_report, perf_report = harness._drain_scenario(  # noqa: SLF001
            _StuckScenario(), scenario_thread
        )
        assert chaos_report["status"] == "timed_out"
        # Partial fields from the underlying scenario carry through so the
        # operator sees how far it got before the cutoff.
        assert chaos_report["injected_fault"] == "x"
        assert perf_report == {}
    finally:
        stop_event.set()
        scenario_thread.join(timeout=2.0)


def test_drain_scenario_returns_empty_when_no_scenario_scheduled(
    isolated_env: None,
) -> None:
    """No chaos for the task → both reports are empty dicts (legacy contract)."""
    harness = DefaultEvalHarness(project_id="p", cluster_name="c")
    chaos_report, perf_report = harness._drain_scenario(None, None)  # noqa: SLF001
    assert chaos_report == {} and perf_report == {}


def test_default_target_deployment_and_namespace_are_ctor_args(
    isolated_env: None,
) -> None:
    """A non-Hypercompute embedder can override the legacy defaults at ctor.

    No env vars set; the harness's overrides flow through to
    ``replace_placeholders`` so a task with
    ``{{TARGET_DEPLOYMENT_NAME}}``/``{{NAMESPACE}}`` resolves to the
    embedder's values rather than the legacy literals.
    """
    harness = DefaultEvalHarness(
        project_id="p",
        cluster_name="c",
        default_target_deployment="my-app",
        default_namespace="custom-ns",
    )
    resolved = harness.replace_placeholders(
        "deploy={{TARGET_DEPLOYMENT_NAME}} ns={{NAMESPACE}}",
        cluster_name="cl",
    )
    assert resolved == "deploy=my-app ns=custom-ns"


def test_granted_skill_paths_snapshot_captured_once(
    isolated_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_granted_skill_paths`` snapshots once at __init__, not per record.

    Removes the env-drift surface: a mid-run change to
    ``AGENT_SKILLS_PATHS`` must NOT show up in record records, because
    the harness is the single source of truth for what was granted.
    """
    monkeypatch.setenv("AGENT_SKILLS_PATHS", "/skills/a,/skills/b")
    harness = DefaultEvalHarness(project_id="p", cluster_name="c")
    assert harness._granted_skill_paths == ("/skills/a", "/skills/b")  # noqa: SLF001

    # A later env mutation must not move the snapshot — the captured
    # tuple is the authority for the rest of the run.
    monkeypatch.setenv("AGENT_SKILLS_PATHS", "/skills/x")
    assert harness._granted_skill_paths == ("/skills/a", "/skills/b")  # noqa: SLF001


def test_build_agent_config_returns_identical_snapshot_across_calls(
    isolated_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``build_agent_config`` is a pure accessor over the __init__ snapshot.

    Two back-to-back calls must return the **same object identity** so the
    agent the harness constructs cannot differ from the config the
    record's ``capabilities_granted`` was derived from. A previous version
    re-read ``AgentConfig.from_env()`` per call, opening a desync window
    that mid-batch env mutation could exploit.
    """
    monkeypatch.setenv("AGENT_SKILLS_PATHS", "/skills/a")
    monkeypatch.setenv("BENCH_USE_MCP", "true")
    harness = DefaultEvalHarness(project_id="p", cluster_name="c")

    a = harness.build_agent_config()
    b = harness.build_agent_config()
    # Identity — not just equality — pins the no-rebuild invariant.
    assert a is b
    assert a.capabilities.skills.paths == ("/skills/a",)


def test_capabilities_granted_matches_agent_config_even_after_env_mutation(
    isolated_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``capabilities_granted`` exactly mirrors the agent's actual config.

    This is the consistency invariant the senior reviewer flagged: env
    mutated AFTER ``DefaultEvalHarness(...)`` construction must not desync
    what the agent was built with from what the record claims it was
    built with. Both come from the single ``__init__`` snapshot.
    """
    monkeypatch.setenv("AGENT_SKILLS_PATHS", "/skills/granted")
    monkeypatch.setenv("AGENT_MCP_SERVER", "/path/to/mcp")
    monkeypatch.setenv("AGENT_ALLOWED_TOOLS", "tool_a")
    monkeypatch.setenv("BENCH_USE_MCP", "true")
    harness = DefaultEvalHarness(project_id="p", cluster_name="c")

    # Drift the env AFTER construction. The harness must still report
    # what was granted at construction, not what the env now says.
    monkeypatch.setenv("AGENT_SKILLS_PATHS", "/skills/leaked-after-init")
    monkeypatch.setenv("BENCH_USE_MCP", "false")
    monkeypatch.delenv("AGENT_MCP_SERVER", raising=False)

    config = harness.build_agent_config()
    task = Task.from_dict({"task_id": "t", "name": "demo", "prompt": "p"})
    success = harness._build_success_record(  # noqa: SLF001
        task=task,
        prompt="p",
        expected_output="e",
        agent_res=AgentResult(output="ok", trajectory=[]),
        chaos_report={},
        perf_report={},
    )
    failed = harness._build_failed_record(  # noqa: SLF001
        task, RuntimeError("boom")
    )

    # The record's ``skills`` and ``capabilities_granted.skills`` come
    # from the same snapshot the agent was built from. The post-init env
    # mutation must NOT leak through.
    expected_skills = list(config.capabilities.skills.paths)
    assert expected_skills == ["/skills/granted"]
    for record in (success, failed):
        assert record["skills"] == expected_skills
        assert record["capabilities_granted"]["skills"] == expected_skills
        # ``use_mcp`` was snapshotted True at __init__; the post-init
        # mutation to "false" must not flip it on the record.
        assert record["capabilities_granted"]["use_mcp"] is True
    # And the agent's actual MCP binding agrees — the post-init delenv
    # of AGENT_MCP_SERVER did NOT drop the binding the agent runs with.
    assert config.capabilities.mcp is not None


def test_run_one_returns_failed_record_when_get_deployer_raises(
    isolated_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A deployer-factory failure becomes a failed record, not a batch crash.

    ``get_deployer`` runs inside ``_run_one``'s try, so an unknown deployer
    type fails just this task (status ``failed``) instead of aborting the whole
    batch evaluation.
    """
    harness = DefaultEvalHarness(project_id="p", cluster_name="c")

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("unknown deployer type")

    monkeypatch.setattr(harness_default, "get_deployer", _boom)
    task = Task.from_dict({"task_id": "t", "name": "demo", "prompt": "p"})

    record = harness._run_one(task, tmp_path)  # noqa: SLF001

    assert record["status"] == "failed"
    assert "unknown deployer type" in record["error"]
    assert record["name"] == "demo"


class _WorkspaceWritingAgent(AgentHarness):
    """Stand-in agent that writes a file into whatever workspace it is given."""

    def _execute(self, prompt: str, workspace_path: Path | None = None) -> AgentResult:
        assert workspace_path is not None
        (workspace_path / "output.txt").write_text("agent wrote this")
        return AgentResult(output="wrote a file", trajectory=[])


def test_run_one_collects_files_the_agent_writes_to_its_workspace(
    isolated_env: None, tmp_path: Path
) -> None:
    """Generated-file collection diffs the agent's real workspace, not the launch cwd.

    Regression test: the harness used to snapshot/diff ``Path(os.getcwd())`` while
    each CLI agent wrote into its own private ``tempfile.TemporaryDirectory`` that
    was gone by the time artifacts were collected, so ``generated_files`` came back
    empty. The harness now owns a per-run workspace and threads it to the agent via
    ``RunContext.workspace_path``, so a file the agent writes there is collected.
    """
    AGENTS.register("fake-workspace-writer")(_WorkspaceWritingAgent)
    try:
        harness = DefaultEvalHarness(
            project_id="p", cluster_name="c", agent_type="fake-workspace-writer", no_infra=True
        )
        task = Task.from_dict({"task_id": "t", "name": "demo", "prompt": "p"})
        run_dir = tmp_path / "run_1"
        run_dir.mkdir()

        record = harness._run_one(task, run_dir)  # noqa: SLF001

        assert record["status"] == "success"
        generated = run_dir / "generated_files" / "output.txt"
        assert generated.exists()
        assert generated.read_text() == "agent wrote this"
    finally:
        AGENTS._items.pop("fake-workspace-writer", None)  # noqa: SLF001


def test_ensure_builtin_agents_swallows_only_import_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Optional-SDK absence is swallowed; real bugs re-raise.

    ``ImportError`` / ``MissingDependencyError`` are the narrow-catch
    classes — anything else (``SyntaxError``, ``RuntimeError`` at module
    top, etc.) must bubble out so the operator sees the real failure
    instead of a silent ``debug`` log.
    """

    # Case 1: ImportError is swallowed — function returns normally.
    def fake_import_missing_sdk(name: str) -> Any:
        raise ImportError("anthropic SDK not installed")

    monkeypatch.setattr(importlib, "import_module", fake_import_missing_sdk)
    harness_default._ensure_builtin_agents_registered()  # noqa: SLF001

    # Case 2: a non-import bug must NOT be silently swallowed.
    def fake_import_buggy_module(name: str) -> Any:
        raise SyntaxError("agent module is broken")

    monkeypatch.setattr(importlib, "import_module", fake_import_buggy_module)
    with pytest.raises(SyntaxError):
        harness_default._ensure_builtin_agents_registered()  # noqa: SLF001

    # Case 3: MissingDependencyError is also swallowed (its semantic class).
    def fake_missing_dep(name: str) -> Any:
        raise MissingDependencyError("optional-feature", "extras-marker")

    monkeypatch.setattr(importlib, "import_module", fake_missing_dep)
    harness_default._ensure_builtin_agents_registered()  # noqa: SLF001


def test_resolve_deployment_and_namespace_precedence_and_types(
    isolated_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1. Default case (no env, no task variables)
    harness = DefaultEvalHarness(
        project_id="p",
        cluster_name="c",
        default_target_deployment="my-default-dep",
        default_namespace="my-default-ns",
    )
    dep, ns = harness._resolve_deployment_and_namespace(None)  # noqa: SLF001
    assert dep == "my-default-dep"
    assert ns == "my-default-ns"

    # 2. Task variables override defaults
    task = Task.from_dict(
        {
            "task_id": "t",
            "name": "demo",
            "prompt": "p",
            "infrastructure": {
                "variables": {
                    "target_deployment_name": "task-dep",
                    "namespace": "task-ns",
                }
            },
        }
    )
    dep, ns = harness._resolve_deployment_and_namespace(task)  # noqa: SLF001
    assert dep == "task-dep"
    assert ns == "task-ns"

    # 3. Env variables override task variables and defaults
    monkeypatch.setenv("TARGET_DEPLOYMENT_NAME", "env-dep")
    monkeypatch.setenv("NAMESPACE", "env-ns")
    dep, ns = harness._resolve_deployment_and_namespace(task)  # noqa: SLF001
    assert dep == "env-dep"
    assert ns == "env-ns"

    # Clean up env for next assertions
    monkeypatch.delenv("TARGET_DEPLOYMENT_NAME")
    monkeypatch.delenv("NAMESPACE")

    # 4. Crash cases: empty variables (None)
    task_empty_vars = Task.from_dict(
        {"task_id": "t", "name": "demo", "prompt": "p", "infrastructure": {"variables": None}}
    )
    # Should not raise AttributeError, should fallback to defaults
    dep, ns = harness._resolve_deployment_and_namespace(task_empty_vars)  # noqa: SLF001
    assert dep == "my-default-dep"
    assert ns == "my-default-ns"

    # 5. Crash cases: non-string values (converted to string)
    task_non_str_vars = Task.from_dict(
        {
            "task_id": "t",
            "name": "demo",
            "prompt": "p",
            "infrastructure": {
                "variables": {
                    "target_deployment_name": 123,
                    "namespace": 456,
                }
            },
        }
    )
    # Should not raise TypeError, should convert to string
    dep, ns = harness._resolve_deployment_and_namespace(task_non_str_vars)  # noqa: SLF001
    assert dep == "123"
    assert ns == "456"


# --- results.json schema (Decision D3): _RECORD_KEYS is the source of truth ---

# Pinned symmetric key set. Every key must be present on *both* the success and
# failed record, so a downstream parser iterating one shape never KeyErrors on
# the other.
_RESULTS_JSON_REQUIRED_KEYS: frozenset[str] = frozenset(
    {
        "input",
        "output",
        "latency",
        "tokens",
        "tools",
        "trajectory",
        "skills",
        "name",
        "folder",
        "status",
        "error",
        "errors",
        "scores",
        "expected_output",
        "expected_output_raw",
        "retrieval_context",
        "chaos_spec",
        "verification_spec",
        "chaos_report",
        "perf_report",
        "documentation",
        "capabilities_granted",
        "verification_parse_errors",
        "generation_only",
        "validated",
    }
)


def _stub_task() -> Task:
    """Build a typed task with a few non-trivial fields the records carry."""
    return Task.from_dict(
        {
            "task_id": "demo-1",
            "name": "demo",
            "prompt": "do the thing",
            "expected_output": "exp",
            "retrieval_context": ["doc-a"],
            "chaos_spec": {"chaos": "yes"},
            "verification_spec": {"verify": "yes"},
        }
    )


def _stub_agent_result() -> AgentResult:
    """Build an agent result with output, trajectory, tokens, latency populated."""
    return AgentResult(
        output="done",
        trajectory=[
            ToolCall(
                name="run_command",
                args={"command": "kubectl get pods"},
                result="pod/web-app Running",
                status="completed",
            ).to_dict()
        ],
        tokens={"input": 10, "output": 5},
        latency=1.5,
    )


def test_record_keys_class_constant_matches_golden(isolated_env: None) -> None:
    """``_RECORD_KEYS`` is the authoritative schema; pin it against the golden set."""
    assert DefaultEvalHarness._RECORD_KEYS == _RESULTS_JSON_REQUIRED_KEYS  # noqa: SLF001


def test_success_record_keys_match_golden(isolated_env: None) -> None:
    """A real ``_build_success_record`` invocation emits exactly the golden keys."""
    harness = DefaultEvalHarness(project_id="p", cluster_name="c")
    record = harness._build_success_record(  # noqa: SLF001 - testing internals
        task=_stub_task(),
        prompt="resolved prompt",
        expected_output="resolved expected",
        agent_res=_stub_agent_result(),
        chaos_report={"status": "success"},
        perf_report={"uptime_percentage": 100.0},
    )
    assert set(record.keys()) == _RESULTS_JSON_REQUIRED_KEYS
    assert record["status"] == "success"
    assert record["output"] == "done"
    assert record["error"] is None
    assert record["errors"] == []
    assert record["scores"] == {}


def test_failed_record_keys_match_golden(isolated_env: None) -> None:
    """``_build_failed_record`` emits the SAME key set as the success record."""
    harness = DefaultEvalHarness(project_id="p", cluster_name="c")
    record = harness._build_failed_record(  # noqa: SLF001 - testing internals
        _stub_task(), RuntimeError("deployer.up() failed")
    )
    assert set(record.keys()) == _RESULTS_JSON_REQUIRED_KEYS
    assert record["status"] == "failed"
    assert record["error"] == "deployer.up() failed"
    assert record["errors"] == ["deployer.up() failed"]


def test_success_and_failed_records_have_identical_top_level_keys(isolated_env: None) -> None:
    """Direct invariant: the two record shapes carry the same top-level keys."""
    harness = DefaultEvalHarness(project_id="p", cluster_name="c")
    task = _stub_task()
    success = harness._build_success_record(  # noqa: SLF001
        task=task,
        prompt="p",
        expected_output="e",
        agent_res=_stub_agent_result(),
        chaos_report={},
        perf_report={},
    )
    failed = harness._build_failed_record(task, RuntimeError("boom"))  # noqa: SLF001
    assert set(success.keys()) == set(failed.keys()) == _RESULTS_JSON_REQUIRED_KEYS
