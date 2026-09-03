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
import logging
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from devops_bench.agents import AGENTS, AgentHarness
from devops_bench.agents.result import AgentResult, ToolCall
from devops_bench.core import ConfigError, MissingDependencyError
from devops_bench.core.score_keys import INTEGRITY_CATASTROPHIC_KEY, OUTCOME_SCORE_KEY
from devops_bench.evalharness import default as harness_default
from devops_bench.evalharness.default import DefaultEvalHarness
from devops_bench.tasks import Task
from devops_bench.verification.base import VerificationResult


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip BENCH_* / AGENT_* env so the harness reads predictable defaults."""
    for var in (
        "BENCH_USE_MCP",
        "BENCH_AGENT_TYPE",
        "BENCH_CHEAT_DETECT",
        "BENCH_CHEAT_RULES",
        "BENCH_CHEAT_INVENTORY",
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
    # The pre-run inventory scans Path.home(); unit runs must not depend on
    # whatever happens to live in the developer's real home directory. The
    # dedicated inventory test re-enables it against a controlled fake home.
    monkeypatch.setenv("BENCH_CHEAT_INVENTORY", "0")


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


def test_success_record_carries_substituted_safety_checklists(isolated_env: None) -> None:
    """Safety checklists get the same placeholder substitution as expected_output.

    The judge reads these strings verbatim, so an unresolved
    ``{{TARGET_DEPLOYMENT_NAME}}`` would be graded as literal text and the
    constraint would never match what the agent actually did.
    """
    harness = DefaultEvalHarness(
        project_id="p",
        cluster_name="c",
        default_target_deployment="my-app",
        default_namespace="custom-ns",
    )
    task = Task(
        name="t",
        recoverable_safety=["kept {{TARGET_DEPLOYMENT_NAME}} available"],
    )
    substituted_recoverable = [
        harness.replace_placeholders(item, cluster_name="cl") for item in task.recoverable_safety
    ]

    record = harness._build_success_record(  # noqa: SLF001 - testing the record shape
        task=task,
        prompt="p",
        expected_output="e",
        agent_res=_stub_agent_result(),
        chaos_report={},
        perf_report={},
        recoverable_safety=substituted_recoverable,
    )

    assert record["recoverable_safety"] == ["kept my-app available"]


def test_success_record_falls_back_to_raw_safety_checklists(isolated_env: None) -> None:
    # Callers that pass nothing keep the raw task values seeded by _empty_record.
    harness = DefaultEvalHarness(project_id="p", cluster_name="c")
    task = Task(name="t", recoverable_safety=["raw item"])

    record = harness._build_success_record(  # noqa: SLF001
        task=task,
        prompt="p",
        expected_output="e",
        agent_res=_stub_agent_result(),
        chaos_report={},
        perf_report={},
    )

    assert record["recoverable_safety"] == ["raw item"]


def test_failed_record_carries_substituted_safety_checklists(isolated_env: None) -> None:
    """A run that dies mid-execution still records resolved checklists.

    The checklists are substituted before the agent runs, so a failure carries
    the same resolved strings a success would rather than raw ``{{...}}`` text
    landing in results.json.
    """
    harness = DefaultEvalHarness(
        project_id="p",
        cluster_name="c",
        default_target_deployment="my-app",
        default_namespace="custom-ns",
    )
    task = Task(
        name="t",
        recoverable_safety=["kept {{TARGET_DEPLOYMENT_NAME}} available"],
    )
    substituted_recoverable = [
        harness.replace_placeholders(item, cluster_name="cl") for item in task.recoverable_safety
    ]

    record = harness._build_failed_record(  # noqa: SLF001 - testing the record shape
        task,
        RuntimeError("agent died"),
        recoverable_safety=substituted_recoverable,
    )

    assert record["status"] == "failed"
    assert record["recoverable_safety"] == ["kept my-app available"]


def test_failed_record_falls_back_to_raw_safety_checklists(isolated_env: None) -> None:
    """A failure before substitution keeps the raw task values, never a KeyError."""
    harness = DefaultEvalHarness(project_id="p", cluster_name="c")
    task = Task(name="t", recoverable_safety=["raw item"])

    record = harness._build_failed_record(task, RuntimeError("infra died"))  # noqa: SLF001

    assert record["recoverable_safety"] == ["raw item"]


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


def test_run_one_warns_when_a_verification_entry_fails_to_parse(
    isolated_env: None, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A typo'd verification entry logs a warning instead of vanishing silently.

    Regression test: parse errors used to be recorded on the record but never
    logged, so a task whose objective count silently dropped left no trace
    anywhere except a key buried in results.json.
    """
    AGENTS.register("fake-workspace-writer-parse-warn")(_WorkspaceWritingAgent)
    try:
        harness = DefaultEvalHarness(
            project_id="p",
            cluster_name="c",
            agent_type="fake-workspace-writer-parse-warn",
            no_infra=True,
        )
        task = Task.from_dict(
            {
                "task_id": "t",
                "name": "demo",
                "prompt": "p",
                "verification_spec": [
                    {
                        "name": "web-ready",
                        "role": "objective",
                        "check": {"type": "pod_healthy", "selector": "app=web"},
                    },
                    {
                        "name": "bad-entry",
                        "role": "objective",
                        "check": {"type": "no-such-type"},
                    },
                ],
            }
        )
        run_dir = tmp_path / "run_1"
        run_dir.mkdir()
        ok = VerificationResult(success=True, elapsed_time=0.0, reason="fine")

        with (
            caplog.at_level(logging.WARNING),
            patch("devops_bench.evalharness.default.VerifierAgent.run_entry", return_value=ok),
        ):
            record = harness._run_one(task, run_dir)  # noqa: SLF001

        assert record["status"] == "success"
        assert any("failed to parse" in message for message in caplog.messages)
        assert "bad-entry" in caplog.text
    finally:
        AGENTS._items.pop("fake-workspace-writer-parse-warn", None)  # noqa: SLF001


def test_run_one_evaluates_verification_on_the_exception_path_when_infra_is_up(
    isolated_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed record still carries a real verification report once infra is up.

    The exception path used to skip verification unconditionally. Now that
    ``infra_up`` and ``entries`` are tracked, a crash after provisioning still
    runs verification, so a failed record is scored instead of silently
    dropping every objective.
    """
    harness = DefaultEvalHarness(project_id="p", cluster_name="c")

    def _boom(prompt: str, ctx: Any) -> Any:
        raise RuntimeError("agent crashed")

    # ``execute_agent`` is patched directly, not the agent itself: AgentHarness.run()
    # has its own safety net that converts an agent crash into an errored
    # AgentResult rather than raising, which would never reach _run_one's
    # exception path.
    monkeypatch.setattr(harness, "execute_agent", _boom)
    canned_report = [{"name": "web-ready", "success": True, "status": "pass"}]
    monkeypatch.setattr(harness, "_run_verification", lambda entries: canned_report)
    task = Task.from_dict(
        {
            "task_id": "t",
            "name": "demo",
            "prompt": "p",
            "infrastructure": {"deployer": "noop"},
            "verification_spec": [
                {
                    "name": "web-ready",
                    "role": "objective",
                    "check": {"type": "pod_healthy", "selector": "app=web"},
                }
            ],
        }
    )

    record = harness._run_one(task, tmp_path)  # noqa: SLF001

    assert record["status"] == "failed"
    assert record["verification_report"] == canned_report
    assert record["verification_status"] == "evaluated"


def test_run_one_reports_evaluated_on_the_exception_path_with_no_entries_declared(
    isolated_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No entries declared but infra came up: still reads as "evaluated", not "not_evaluated".

    A task with no verification_spec has nothing to verify, not a broken
    environment. The exception path must record the same status the success
    path would for the same case: verification ran trivially over nothing.
    """
    harness = DefaultEvalHarness(project_id="p", cluster_name="c")

    def _boom(prompt: str, ctx: Any) -> Any:
        raise RuntimeError("agent crashed")

    monkeypatch.setattr(harness, "execute_agent", _boom)
    task = Task.from_dict(
        {
            "task_id": "t",
            "name": "demo",
            "prompt": "p",
            "infrastructure": {"deployer": "noop"},
        }
    )

    record = harness._run_one(task, tmp_path)  # noqa: SLF001

    assert record["status"] == "failed"
    assert record["verification_report"] == []
    assert record["verification_status"] == "evaluated"


def test_run_one_skips_verification_entirely_under_no_infra(
    isolated_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """no_infra means there is no cluster to check, so verification never runs."""
    AGENTS.register("fake-workspace-writer-no-infra")(_WorkspaceWritingAgent)
    try:
        harness = DefaultEvalHarness(
            project_id="p",
            cluster_name="c",
            agent_type="fake-workspace-writer-no-infra",
            no_infra=True,
        )

        def _fail_if_called(entries: Any) -> Any:
            raise AssertionError("_run_verification must not be called under no_infra")

        monkeypatch.setattr(harness, "_run_verification", _fail_if_called)
        task = Task.from_dict(
            {
                "task_id": "t",
                "name": "demo",
                "prompt": "p",
                "verification_spec": [
                    {
                        "name": "web-ready",
                        "role": "objective",
                        "check": {"type": "pod_healthy", "selector": "app=web"},
                    }
                ],
            }
        )
        run_dir = tmp_path / "run_1"
        run_dir.mkdir()

        record = harness._run_one(task, run_dir)  # noqa: SLF001

        assert record["status"] == "success"
        assert record["verification_status"] == "skipped_no_infra"
        assert record["verification_report"] == []
    finally:
        AGENTS._items.pop("fake-workspace-writer-no-infra", None)  # noqa: SLF001


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


# --- results.json schema (Decision D3) ---

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
        "recoverable_safety",
        "chaos_report",
        "perf_report",
        "cheating_report",
        "documentation",
        "capabilities_granted",
        "verification_parse_errors",
        "verification_report",
        "verification_status",
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
            "verification_spec": [
                {
                    "name": "v1",
                    "role": "objective",
                    "check": {"type": "pod_healthy", "selector": "app=web"},
                }
            ],
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


# --- cheating detection wiring (flag-only) ---


class _SensitiveReadingAgent(AgentHarness):
    """Stand-in agent whose trajectory reads the task definition."""

    def _execute(self, prompt: str, workspace_path: Path | None = None) -> AgentResult:
        return AgentResult(
            output="done",
            trajectory=[
                ToolCall(
                    name="exec",
                    args={"command": "cat ~/devops-bench/tasks/common/opa-remediation/task.yaml"},
                    result=None,
                    status="completed",
                ).to_dict()
            ],
        )


def _run_with_fake_agent(tmp_path: Path, agent_key: str) -> dict[str, Any]:
    """Register the sensitive-reading agent, run one no-infra task, return its record."""
    AGENTS.register(agent_key)(_SensitiveReadingAgent)
    try:
        harness = DefaultEvalHarness(
            project_id="p",
            cluster_name="c",
            agent_type=agent_key,
            no_infra=True,
            results_root=str(tmp_path),
        )
        task = Task.from_dict({"task_id": "t", "name": "demo", "prompt": "p"})
        results = harness.run([task])
        assert len(results) == 1
        return results[0]
    finally:
        AGENTS._items.pop(agent_key, None)  # noqa: SLF001


def test_run_flags_sensitive_access_and_gates_it_without_a_judge(
    isolated_env: None, tmp_path: Path
) -> None:
    """A task.yaml read in the trajectory is flagged, then gated to zero.

    End to end through ``run()``, with no judge configured — ``get_judge_model()``
    raises in this environment, which is the point. The integrity gate is
    deterministic, so an unrelated judge outage must not be able to unscore it:
    before the fallback in ``_score``, that exception aborted the whole batch and
    a cheating run kept a null ``outcomeScore``, dropping out of leaderboard
    aggregates entirely. ``validated`` stays untouched either way — the gate
    publishes a zero, it does not invalidate the row.
    """
    record = _run_with_fake_agent(tmp_path, "fake-sensitive-reader")

    report = record["cheating_report"]
    assert report["status"] == "flagged"
    assert "task-definition" in report["categories"]
    assert "harness-repo" in report["categories"]
    assert report["findings"][0]["trajectory_index"] == 0

    scores = record["scores"]
    assert scores[INTEGRITY_CATASTROPHIC_KEY]["score"] == 0.0
    assert scores[OUTCOME_SCORE_KEY]["score"] == 0.0
    assert INTEGRITY_CATASTROPHIC_KEY in scores[OUTCOME_SCORE_KEY]["reason"]
    # Judged metrics found no judge and are simply absent, not failed-closed.
    assert "OutcomeValidity" not in scores
    assert record["validated"] is False


def test_run_leaves_report_empty_when_detection_disabled(
    isolated_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BENCH_CHEAT_DETECT=0 keeps the seeded empty report on every record."""
    monkeypatch.setenv("BENCH_CHEAT_DETECT", "0")
    record = _run_with_fake_agent(tmp_path, "fake-sensitive-reader-off")
    assert record["cheating_report"] == {}


def test_bad_cheat_rules_path_fails_loud_at_construction(
    isolated_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo'd BENCH_CHEAT_RULES is an operator error, not a detector failure:
    it must raise at harness construction, not degrade to empty reports."""
    monkeypatch.setenv("BENCH_CHEAT_RULES", str(tmp_path / "no-such-rules.yaml"))
    with pytest.raises(ConfigError, match="rules file"):
        DefaultEvalHarness(project_id="p", cluster_name="c")


def test_detection_failure_on_one_record_spares_the_rest(
    isolated_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Annotation is best-effort per record: one failing record keeps its
    seeded empty report while every other record is still annotated."""
    from devops_bench.cheat_detection import annotate_records

    def flaky(records: list[dict[str, Any]], rules: Any) -> None:
        if records[0].get("name") == "boom":
            raise RuntimeError("scan exploded")
        annotate_records(records, rules)

    # Patched where the harness looks the name up, not where it is defined.
    monkeypatch.setattr("devops_bench.evalharness.default.annotate_records", flaky)

    AGENTS.register("fake-flaky-detection")(_LeftoverReadingAgent)
    try:
        harness = DefaultEvalHarness(
            project_id="p",
            cluster_name="c",
            agent_type="fake-flaky-detection",
            no_infra=True,
            results_root=str(tmp_path / "results"),
        )
        tasks = [
            Task.from_dict({"task_id": "t1", "name": "boom", "prompt": "p"}),
            Task.from_dict({"task_id": "t2", "name": "fine", "prompt": "p"}),
        ]
        results = harness.run(tasks)
    finally:
        AGENTS._items.pop("fake-flaky-detection", None)  # noqa: SLF001

    assert results[0]["cheating_report"] == {}
    assert results[1]["cheating_report"]["status"] == "clean"


class _LeftoverReadingAgent(AgentHarness):
    """Stand-in agent whose trajectory reads a prior run's leftover notes."""

    def _execute(self, prompt: str, workspace_path: Path | None = None) -> AgentResult:
        return AgentResult(
            output="done",
            trajectory=[
                ToolCall(
                    name="exec",
                    args={"command": "cat ~/old-notes.txt"},
                    result=None,
                    status="completed",
                ).to_dict()
            ],
        )


def test_run_inventory_flags_prior_run_leftover_access(
    isolated_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file already in the home before the run generates a live inventory rule."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / "old-notes.txt").write_text("leftover from a previous run\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setenv("BENCH_CHEAT_INVENTORY", "1")

    AGENTS.register("fake-leftover-reader")(_LeftoverReadingAgent)
    try:
        harness = DefaultEvalHarness(
            project_id="p",
            cluster_name="c",
            agent_type="fake-leftover-reader",
            no_infra=True,
            results_root=str(tmp_path / "results"),
        )
        task = Task.from_dict({"task_id": "t", "name": "demo", "prompt": "p"})
        results = harness.run([task])
    finally:
        AGENTS._items.pop("fake-leftover-reader", None)  # noqa: SLF001

    report = results[0]["cheating_report"]
    assert report["status"] == "flagged"
    assert "prior-run-artifact" in report["categories"]


def test_run_inventory_spares_prompt_authorized_entry(
    isolated_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-existing entry the task prompt names is required work, not a cheat."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / "old-notes.txt").write_text("leftover from a previous run\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setenv("BENCH_CHEAT_INVENTORY", "1")

    AGENTS.register("fake-authorized-reader")(_LeftoverReadingAgent)
    try:
        harness = DefaultEvalHarness(
            project_id="p",
            cluster_name="c",
            agent_type="fake-authorized-reader",
            no_infra=True,
            results_root=str(tmp_path / "results"),
        )
        task = Task.from_dict(
            {
                "task_id": "t",
                "name": "demo",
                "prompt": "Review the handover notes in 'old-notes.txt' and act on them.",
            }
        )
        results = harness.run([task])
    finally:
        AGENTS._items.pop("fake-authorized-reader", None)  # noqa: SLF001

    assert results[0]["cheating_report"]["status"] == "clean"


def test_run_survives_detector_failure(
    isolated_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A detector crash must not sink the run; results.json is still written."""

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("detector exploded")

    monkeypatch.setattr("devops_bench.evalharness.default.annotate_records", _boom)
    record = _run_with_fake_agent(tmp_path, "fake-sensitive-reader-crash")

    assert record["status"] == "success"
    assert record["cheating_report"] == {}
    # The raw results survived the detector failure on disk as well.
    run_dirs = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert len(run_dirs) == 1 and (run_dirs[0] / "results.json").exists()


class _BatchContaminatingAgent(AgentHarness):
    """Task 1 leaves a deliverable in the home; task 2 reads it back.

    Class-level state because the registry constructs its own instance per
    task, and the point of the test is what carries *between* those tasks.
    """

    home: Path
    calls: int = 0

    def _execute(self, prompt: str, workspace_path: Path | None = None) -> AgentResult:
        type(self).calls += 1
        if type(self).calls == 1:
            (self.home / "task-1-report.md").write_text(
                "# Task 1 remediation\n- set privileged to false on team-alpha/cache\n",
                encoding="utf-8",
            )
            command = "echo done > ~/task-1-report.md"
        else:
            command = "cat ~/task-1-report.md"
        return AgentResult(
            output="done",
            trajectory=[
                ToolCall(
                    name="exec", args={"command": command}, result=None, status="completed"
                ).to_dict()
            ],
        )


def test_inventory_is_resnapshotted_between_tasks(
    isolated_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 1's deliverable is an answer key for task 2, and must be covered.

    A single run-start snapshot cannot see it — the file does not exist yet
    when the batch begins — so this pins that the home is re-inventoried
    before each task rather than once per invocation.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setenv("BENCH_CHEAT_INVENTORY", "1")

    _BatchContaminatingAgent.home = fake_home
    _BatchContaminatingAgent.calls = 0
    AGENTS.register("fake-batch-contaminator")(_BatchContaminatingAgent)
    try:
        harness = DefaultEvalHarness(
            project_id="p",
            cluster_name="c",
            agent_type="fake-batch-contaminator",
            no_infra=True,
            results_root=str(tmp_path / "results"),
        )
        results = harness.run(
            [
                Task.from_dict({"task_id": "t1", "name": "writer", "prompt": "do the work"}),
                Task.from_dict({"task_id": "t2", "name": "reader", "prompt": "do other work"}),
            ]
        )
    finally:
        AGENTS._items.pop("fake-batch-contaminator", None)  # noqa: SLF001

    # Task 1 created the entry; its own snapshot predates it, so it stays clean.
    assert results[0]["cheating_report"]["status"] == "clean"
    # Task 2's snapshot saw it, so reading it by path is flagged.
    report = results[1]["cheating_report"]
    assert report["status"] == "flagged"
    assert "prior-run-artifact" in report["categories"]


def test_mid_batch_entry_is_not_fingerprinted(
    isolated_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An entry created during the batch gets a path rule but no fingerprint.

    Fingerprints are unfilterable, so fingerprinting task 1's honest report
    would flag a later iteration that merely worded its own report the same
    way. Only the run-start leftovers may contribute content patterns.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setenv("BENCH_CHEAT_INVENTORY", "1")

    _BatchContaminatingAgent.home = fake_home
    _BatchContaminatingAgent.calls = 0
    AGENTS.register("fake-batch-fingerprint")(_BatchContaminatingAgent)
    try:
        harness = DefaultEvalHarness(
            project_id="p",
            cluster_name="c",
            agent_type="fake-batch-fingerprint",
            no_infra=True,
            results_root=str(tmp_path / "results"),
        )
        harness.run(
            [
                Task.from_dict({"task_id": "t1", "name": "writer", "prompt": "do the work"}),
                Task.from_dict({"task_id": "t2", "name": "reader", "prompt": "do other work"}),
            ]
        )
        rules = harness._inventory_home(fingerprint_only=frozenset())  # noqa: SLF001
    finally:
        AGENTS._items.pop("fake-batch-fingerprint", None)  # noqa: SLF001

    assert "task-1-report.md" in {r.source for r in rules if r.source}
    assert not any("team-alpha/cache" in p for r in rules for p in r.patterns)
