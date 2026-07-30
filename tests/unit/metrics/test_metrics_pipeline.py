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

"""Tests for the registry-driven batch scoring pipeline.

The first group registers stub evaluators into ``METRICS`` so the dispatch loop,
evaluator ordering, and per-metric exception isolation are exercised in
isolation; the ``registry`` fixture snapshots and restores the registry so the
stubs never leak into sibling tests. The remaining groups exercise the concrete
metric families (checklist, outcome-validity, tool-invocation, grounding, chaos)
end to end with ``deepeval`` mocked.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from devops_bench.core import Registry
from devops_bench.metrics import checklist, pipeline
from devops_bench.metrics.base import GEVAL_PASS_THRESHOLD, METRICS, MetricContext, MetricScore
from devops_bench.metrics.pipeline import (
    CHECKLIST_THRESHOLD,
    evaluate_metrics_batch,
    extract_checklist_items,
)

# --- pipeline dispatch loop (stub evaluators, no concrete families) -----------


class _StubEvaluator:
    """Always-applicable evaluator yielding one judged score."""

    name = "stub"

    def applies(self, ctx: MetricContext) -> bool:
        return True

    def evaluate(self, ctx: MetricContext) -> Iterable[MetricScore]:
        yield MetricScore(name="stub_score", score=1.0, success=True, reason="ok")


class _BoomEvaluator:
    """Applicable evaluator whose :meth:`evaluate` raises."""

    name = "boom"

    def applies(self, ctx: MetricContext) -> bool:
        return True

    def evaluate(self, ctx: MetricContext) -> Iterable[MetricScore]:
        raise RuntimeError("metric exploded")


class _SkippedEvaluator:
    """Non-applicable evaluator; :meth:`evaluate` must never be called."""

    name = "skipped"

    def applies(self, ctx: MetricContext) -> bool:
        return False

    def evaluate(self, ctx: MetricContext) -> Iterable[MetricScore]:
        raise AssertionError("evaluate() must not run when applies() is False")


def _make_stub(key: str) -> type:
    """Build a stub evaluator class that yields a bare score named after ``key``."""

    class _Stub:
        name = key

        def applies(self, ctx: MetricContext) -> bool:
            return True

        def evaluate(self, ctx: MetricContext) -> Iterable[MetricScore]:
            yield MetricScore(name=f"{key}_score", score=1.0)

    return _Stub


@pytest.fixture
def registry() -> Iterator[Registry[Any]]:
    """Yield the ``METRICS`` registry emptied for the test, then restored."""
    saved = dict(METRICS._items)
    METRICS._items.clear()
    try:
        yield METRICS
    finally:
        METRICS._items.clear()
        METRICS._items.update(saved)


def _result(name: str = "t1") -> dict[str, Any]:
    """Minimal execution-result dict the pipeline can build a context from."""
    return {"name": name, "input": "q", "output": "a", "expected_output": "a"}


def test_scores_written_from_registered_evaluator(registry: Registry[Any]) -> None:
    """A registered evaluator's scores land in the result's ``scores`` map."""
    registry.register("stub")(_StubEvaluator)
    results = [_result()]

    evaluate_metrics_batch(results, None, use_mcp=False)

    assert results[0]["scores"] == {"stub_score": {"score": 1.0, "success": True, "reason": "ok"}}


def test_one_failing_metric_does_not_abort_others(
    registry: Registry[Any], caplog: pytest.LogCaptureFixture
) -> None:
    """A raising metric is isolated and logged; siblings still record scores."""
    registry.register("boom")(_BoomEvaluator)
    registry.register("stub")(_StubEvaluator)
    results = [_result()]

    evaluate_metrics_batch(results, None, use_mcp=False)

    # The raising metric is isolated: the sibling metric still records a score.
    assert "stub_score" in results[0]["scores"]
    assert "boom" in caplog.text


def test_evaluate_skipped_when_applies_false(registry: Registry[Any]) -> None:
    """An evaluator whose ``applies`` returns False is never evaluated."""
    registry.register("skipped")(_SkippedEvaluator)
    results = [_result()]

    evaluate_metrics_batch(results, None, use_mcp=False)

    assert results[0]["scores"] == {}


def test_absent_builtin_keys_are_skipped(registry: Registry[Any]) -> None:
    """Unregistered builtin keys are skipped rather than instantiated."""
    # None of the pinned builtin keys are registered here; the batch must not
    # raise while building evaluators (the builtin-key filter guards this).
    registry.register("stub")(_StubEvaluator)
    results = [_result()]

    evaluate_metrics_batch(results, None, use_mcp=False)

    assert results[0]["scores"]["stub_score"]["score"] == 1.0


def test_builtin_keys_scored_before_third_party(registry: Registry[Any]) -> None:
    """Registered builtin keys are ordered ahead of third-party registrations."""
    # A registered builtin ("checklist") is ordered ahead of a third-party
    # metric even though the latter sorts earlier alphabetically.
    registry.register("checklist")(_make_stub("checklist"))
    registry.register("aaa_custom")(_make_stub("aaa_custom"))
    results = [_result()]

    evaluate_metrics_batch(results, None, use_mcp=False)

    assert list(results[0]["scores"].keys()) == ["checklist_score", "aaa_custom_score"]


def test_each_result_in_batch_is_scored(registry: Registry[Any]) -> None:
    """Every result in a multi-item batch is scored in place."""
    registry.register("stub")(_StubEvaluator)
    results = [_result("t1"), _result("t2"), _result("t3")]

    evaluate_metrics_batch(results, None, use_mcp=False)

    assert all(r["scores"].get("stub_score") for r in results)


# --- extract_checklist_items (pure logic) -------------------------------------


def test_checklist_extracts_critical_requirements_bullets() -> None:
    expected = (
        "Critical Requirements:\n- Deployment must use 3 replicas\n- Service exposes port 8080\n"
    )
    assert extract_checklist_items(expected, use_mcp=True) == [
        "Deployment must use 3 replicas",
        "Service exposes port 8080",
    ]


def test_checklist_stops_at_expected_manifest_marker() -> None:
    expected = (
        "Critical Requirements:\n"
        "- Keep replicas at 3\n"
        "Expected Manifest Generated:\n"
        "- apiVersion: apps/v1\n"
    )
    assert extract_checklist_items(expected, use_mcp=True) == ["Keep replicas at 3"]


def test_checklist_drops_tool_call_items_when_mcp_disabled() -> None:
    expected = "Critical Requirements:\n- Expected Tool Call: apply_manifest\n- App is reachable\n"
    assert extract_checklist_items(expected, use_mcp=False) == ["App is reachable"]
    assert extract_checklist_items(expected, use_mcp=True) == [
        "Expected Tool Call: apply_manifest",
        "App is reachable",
    ]


def test_checklist_empty_when_no_bullets() -> None:
    assert extract_checklist_items("Some prose without bullets", use_mcp=True) == []


def test_checklist_preserves_trailing_hyphen() -> None:
    # ``lstrip("- ")`` must not eat a trailing hyphen the way ``strip("- ")`` would.
    expected = "Critical Requirements:\n- Deploy to namespace staging-\n"
    assert extract_checklist_items(expected, use_mcp=True) == ["Deploy to namespace staging-"]


def test_checklist_preserves_leading_flag_double_dash() -> None:
    # Stripping the bullet must remove only the "- " marker, not a leading "--"
    # on a flag requirement (e.g. "- --dry-run flag must be set").
    expected = "Critical Requirements:\n- --dry-run flag must be set\n"
    assert extract_checklist_items(expected, use_mcp=True) == ["--dry-run flag must be set"]


def test_checklist_threshold_is_value_independent_of_tool_invocation() -> None:
    # Phase-0 fix #1: a dedicated constant so the checklist cutoff is not
    # silently coupled to ``TOOL_INVOCATION_THRESHOLD``.
    assert CHECKLIST_THRESHOLD == 0.8


# --- evaluate_metrics_batch (deepeval mocked) ---------------------------------


def _metric_result(name, score=1.0, success=True, reason="ok") -> SimpleNamespace:
    metric_data = SimpleNamespace(name=name, score=score, success=success, reason=reason)
    test_result = SimpleNamespace(metrics_data=[metric_data])
    return SimpleNamespace(test_results=[test_result])


def _evaluate_by_metric_name(successes=None):
    """Build an order-agnostic ``evaluate`` side effect.

    The pipeline always calls ``evaluate([tc], metrics=[m])`` with a single
    metric, so the result is derived from that metric's real ``name`` rather
    than call order. The reported name carries DeepEval's ``" [GEval]"`` suffix
    so the test actually exercises the suffix stripping. ``successes`` optionally
    maps a metric name (pre-suffix) to its success bool (default True).
    """
    successes = successes or {}

    def _side_effect(test_cases, metrics):
        metric = metrics[0]
        name = metric.name
        return _metric_result(f"{name} [GEval]", success=successes.get(name, True))

    return _side_effect


def _base_result(**overrides) -> dict[str, Any]:
    res = {
        "input": "deploy the app",
        "output": "done, applied to cluster",
        "trajectory": [{"tool": "apply"}],
        "expected_output": "App deployed",
        "latency": 1.0,
        "name": "case-1",
        "retrieval_context": [],
        "tools": ["apply"],
    }
    res.update(overrides)
    return res


def _patch_judges(mocker: MockerFixture) -> None:
    from devops_bench.metrics import outcome_validity, tool_invocation

    mocker.patch.object(
        outcome_validity,
        "build_outcome_validity_metric",
        return_value=SimpleNamespace(name="OutcomeValidity"),
    )
    mocker.patch.object(
        tool_invocation,
        "build_tool_invocation_metric",
        return_value=SimpleNamespace(name="ToolInvocation"),
    )


def test_batch_scores_outcome_and_tool(mocker: MockerFixture) -> None:
    _patch_judges(mocker)
    mocker.patch.object(pipeline, "LLMTestCase")
    # Order-agnostic: result keyed off the metric name with the [GEval] suffix
    # exercised through the strip path.
    mocker.patch("deepeval.evaluate", side_effect=_evaluate_by_metric_name())
    judge = MagicMock()
    results = [_base_result(expected_output="App deployed")]  # no bullets

    evaluate_metrics_batch(results, judge, use_mcp=True)

    scores = results[0]["scores"]
    assert "OutcomeValidity" in scores
    assert "ToolInvocation" in scores
    assert "OutcomeValidity [GEval]" not in scores
    assert "ToolInvocation [GEval]" not in scores
    assert "ChecklistScore" not in scores


def test_batch_skips_tool_when_mcp_disabled(mocker: MockerFixture) -> None:
    _patch_judges(mocker)
    mocker.patch.object(pipeline, "LLMTestCase")
    evaluate = mocker.patch("deepeval.evaluate", side_effect=_evaluate_by_metric_name())
    judge = MagicMock()
    results = [_base_result()]

    evaluate_metrics_batch(results, judge, use_mcp=False)

    # Only the outcome evaluation runs (ToolInvocation gated by ctx.use_mcp).
    assert evaluate.call_count == 1
    assert "OutcomeValidity" in results[0]["scores"]
    assert "ToolInvocation" not in results[0]["scores"]


def test_batch_use_mcp_falls_back_to_env(mocker: MockerFixture) -> None:
    _patch_judges(mocker)
    mocker.patch.object(pipeline, "LLMTestCase")
    mocker.patch("deepeval.evaluate", side_effect=_evaluate_by_metric_name())
    mocker.patch.object(pipeline, "get_bool", return_value=False)
    judge = MagicMock()
    results = [_base_result()]

    evaluate_metrics_batch(results, judge)  # use_mcp omitted → env fallback

    assert "ToolInvocation" not in results[0]["scores"]


def test_batch_computes_checklist_score(mocker: MockerFixture) -> None:
    _patch_judges(mocker)
    mocker.patch.object(pipeline, "LLMTestCase")
    # Give the dynamic GEval metric a stable name so success is matchable.
    mocker.patch.object(
        checklist, "GEval", side_effect=lambda **kw: SimpleNamespace(name=kw["name"])
    )
    mocker.patch("deepeval.evaluate", side_effect=_evaluate_by_metric_name())
    judge = MagicMock()
    results = [_base_result(expected_output="Critical Requirements:\n- replicas=3\n")]

    evaluate_metrics_batch(results, judge, use_mcp=True)

    scores = results[0]["scores"]
    # The dynamic check key is also stripped of the [GEval] suffix.
    assert "Check: replicas=3" in scores
    assert scores["ChecklistScore"]["score"] == 1.0
    assert scores["ChecklistScore"]["success"] is True


def test_checklist_per_item_geval_uses_explicit_pass_threshold(mocker: MockerFixture) -> None:
    # Per-item checklist GEvals must pass the explicit 0.8 cutoff rather than
    # inheriting deepeval's looser 0.5 default, since their success flags feed
    # the aggregate ChecklistScore.
    geval_cls = mocker.patch.object(
        checklist, "GEval", side_effect=lambda **kw: SimpleNamespace(name=kw["name"])
    )
    _patch_judges(mocker)
    mocker.patch.object(pipeline, "LLMTestCase")
    mocker.patch("deepeval.evaluate", side_effect=_evaluate_by_metric_name())
    judge = MagicMock()
    results = [_base_result(expected_output="Critical Requirements:\n- replicas=3\n")]

    evaluate_metrics_batch(results, judge, use_mcp=True)

    assert geval_cls.call_args.kwargs["threshold"] == GEVAL_PASS_THRESHOLD


def test_batch_invokes_grounding_and_chaos(mocker: MockerFixture) -> None:
    _patch_judges(mocker)
    mocker.patch.object(pipeline, "LLMTestCase")
    mocker.patch("deepeval.evaluate", side_effect=_evaluate_by_metric_name())

    from devops_bench.metrics import chaos_metrics, grounding

    grounding_call = mocker.patch.object(grounding, "evaluate_documentation_grounding")
    retrieval = mocker.patch.object(grounding, "calculate_doc_retrieval_rate", return_value=0.5)
    chaos = mocker.patch.object(chaos_metrics, "evaluate_chaos_metrics")
    judge = MagicMock()
    results = [
        _base_result(
            documentation=[{"doc_name": "g", "url": "u", "constraints": []}],
            chaos_spec=[{"fault": "kill"}],
            chaos_report={"injected_fault": "kill"},
            perf_report={"uptime_percentage": 99.9},
        )
    ]

    evaluate_metrics_batch(results, judge, use_mcp=True)

    grounding_call.assert_called_once()
    retrieval.assert_called_once()
    chaos.assert_called_once()
    assert results[0]["scores"]["DocRetrievalRate"] == 0.5


def test_batch_score_insertion_order_matches_legacy_results_json(mocker: MockerFixture) -> None:
    # D3: ``res["scores"]`` insertion order lands on disk; pin the legacy
    # ordering (outcome -> tool -> checklist -> grounding -> chaos) so
    # downstream results.json consumers do not see a reshuffle.
    _patch_judges(mocker)
    mocker.patch.object(pipeline, "LLMTestCase")
    mocker.patch.object(
        checklist, "GEval", side_effect=lambda **kw: SimpleNamespace(name=kw["name"])
    )
    mocker.patch("deepeval.evaluate", side_effect=_evaluate_by_metric_name())

    from devops_bench.metrics import chaos_metrics, grounding

    mocker.patch.object(
        grounding,
        "evaluate_documentation_grounding",
        side_effect=lambda docs, tc, judge, scores: scores.update(
            {
                "Doc Constraint: x": {"score": 1.0, "success": True, "reason": "r"},
                "GroundingAccuracy": {"score": 5.0, "success": True, "reason": "r"},
                "ParameterRecallAccuracy": 1.0,
            }
        ),
    )
    mocker.patch.object(grounding, "calculate_doc_retrieval_rate", return_value=0.5)
    mocker.patch.object(
        chaos_metrics,
        "evaluate_chaos_metrics",
        side_effect=lambda tc, judge, cr, pr, scores: scores.update(
            {
                "DiagnosisAccuracy": {"score": 5.0, "success": True, "reason": "r"},
                "GracefulRecovery": {"score": 4.0, "success": True, "reason": "r"},
                "Workload_Deployment_Time_Seconds": 12.0,
                "Workload_Uptime_Percentage": 99.5,
                "Resource_Utilization_Efficiency": 0.8,
            }
        ),
    )

    results = [
        _base_result(
            expected_output="Critical Requirements:\n- replicas=3\n",
            documentation=[{"doc_name": "g", "url": "u", "constraints": []}],
            chaos_spec=[{"fault": "kill"}],
            chaos_report={"injected_fault": "kill"},
            perf_report={"uptime_percentage": 99.5},
        )
    ]
    evaluate_metrics_batch(results, MagicMock(), use_mcp=True)

    keys = list(results[0]["scores"].keys())

    # Locate the section anchors and assert outcome -> tool -> checklist ->
    # grounding -> chaos. We pin the first-occurrence index of one canonical
    # key from each family so the assertion is robust to the per-family
    # internal ordering (grounding emits multiple keys, chaos likewise).
    def idx(name: str) -> int:
        return keys.index(name)

    assert (
        idx("OutcomeValidity")
        < idx("ToolInvocation")
        < idx("Check: replicas=3")
        < idx("ChecklistScore")
        < idx("GroundingAccuracy")
        < idx("DiagnosisAccuracy")
    )


# --- generation_only flow + MCP tool-name normalization -----------------------


def test_canonical_tool_name_strips_mcp_prefix() -> None:
    assert pipeline._canonical_tool_name("default__generate_manifest") == "generate_manifest"
    assert pipeline._canonical_tool_name("run_shell_command") == "run_shell_command"


def test_build_context_threads_generation_only(mocker: MockerFixture) -> None:
    mocker.patch.object(pipeline, "LLMTestCase", side_effect=lambda **kw: SimpleNamespace(**kw))
    ctx = pipeline._build_context(_base_result(generation_only=True), MagicMock(), True)
    assert ctx.generation_only is True
    # Absent key defaults to False.
    assert pipeline._build_context(_base_result(), MagicMock(), True).generation_only is False


def test_build_context_normalizes_tool_names_for_judge(mocker: MockerFixture) -> None:
    mocker.patch.object(pipeline, "LLMTestCase", side_effect=lambda **kw: SimpleNamespace(**kw))
    res = _base_result(
        tools=["default__generate_manifest"],
        trajectory=[{"name": "default__generate_manifest", "status": "completed"}],
    )
    ctx = pipeline._build_context(res, MagicMock(), True)
    # Judge sees the canonical name; the on-disk record keeps the raw prefix.
    assert "generate_manifest" in ctx.tool_case.actual_output
    assert "default__generate_manifest" not in ctx.tool_case.actual_output
    assert res["tools"] == ["default__generate_manifest"]
    assert res["trajectory"][0]["name"] == "default__generate_manifest"


def test_outcome_validity_override_only_when_generation_only(mocker: MockerFixture) -> None:
    from devops_bench.metrics import outcome_validity

    captured = {}
    mocker.patch.object(
        outcome_validity,
        "GEval",
        side_effect=lambda **kw: captured.update(kw) or SimpleNamespace(**kw),
    )
    outcome_validity.build_outcome_validity_metric(MagicMock(), generation_only=False)
    assert outcome_validity._GENERATION_ONLY_OVERRIDE not in captured["criteria"]
    outcome_validity.build_outcome_validity_metric(MagicMock(), generation_only=True)
    assert outcome_validity._GENERATION_ONLY_OVERRIDE in captured["criteria"]


def test_build_context_warns_on_empty_expected_output(
    caplog: pytest.LogCaptureFixture, mocker: MockerFixture
) -> None:
    mocker.patch.object(pipeline, "LLMTestCase", side_effect=lambda **kw: SimpleNamespace(**kw))
    import logging

    res = _base_result(expected_output="")
    with caplog.at_level(logging.WARNING):
        pipeline._build_context(res, MagicMock(), True)
    assert len(caplog.records) == 1
    assert "has an empty expected_output" in caplog.text


def test_build_context_no_warning_when_expected_output_set(
    caplog: pytest.LogCaptureFixture, mocker: MockerFixture
) -> None:
    mocker.patch.object(pipeline, "LLMTestCase", side_effect=lambda **kw: SimpleNamespace(**kw))
    import logging

    res = _base_result(expected_output="App deployed")
    with caplog.at_level(logging.WARNING):
        pipeline._build_context(res, MagicMock(), True)
    assert len(caplog.records) == 0


def test_build_context_missing_expected_output_defaults_and_warns(
    caplog: pytest.LogCaptureFixture, mocker: MockerFixture
) -> None:
    mocker.patch.object(pipeline, "LLMTestCase", side_effect=lambda **kw: SimpleNamespace(**kw))
    import logging

    res = _base_result()
    del res["expected_output"]
    with caplog.at_level(logging.WARNING):
        pipeline._build_context(res, MagicMock(), True)
    assert len(caplog.records) == 1
    assert "has an empty expected_output" in caplog.text


# --- the safety metric, driven through the batch pipeline --------------------


def test_safety_metric_scores_the_recoverable_checklist_through_the_batch(
    registry: Registry[Any], mocker: MockerFixture
) -> None:
    """The registered ``safety`` metric writes its sub-score onto the record.

    Covers the seam the per-metric tests cannot: that ``safety`` is a builtin
    key the batch actually builds and runs, and that its emitted score name
    lands in ``result["scores"]`` for the composite to read.
    """
    from devops_bench.metrics.safety import JUDGED_RECOVERABLE_SCORE_KEY, SafetyMetric

    # Stand in for GEval so construction skips DeepEval's judge-type validation.
    def _fake_geval(**kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(name=kwargs["name"])

    mocker.patch("devops_bench.metrics.safety.GEval", side_effect=_fake_geval)

    # Judge every constraint as satisfied: recoverable passes, no tripwire fires.
    def _run(case: Any, metrics: list[Any]) -> list[MetricScore]:
        return [MetricScore(name=metrics[0].name, score=1.0, success=True)]

    mocker.patch("devops_bench.metrics.safety.run_geval", side_effect=_run)
    registry.register("safety")(SafetyMetric)

    results = [_result()]
    results[0]["recoverable_safety"] = ["kept the service reachable"]

    evaluate_metrics_batch(results, MagicMock(), use_mcp=False)

    scores = results[0]["scores"]
    assert scores[JUDGED_RECOVERABLE_SCORE_KEY]["score"] == pytest.approx(1.0)
    # A registered metric runs either way, so pin the builtin membership too —
    # that is what orders safety ahead of third-party metrics in the batch.
    assert "safety" in pipeline._BUILTIN_METRIC_KEYS  # noqa: SLF001


def test_safety_metric_skipped_when_task_declares_no_constraints(
    registry: Registry[Any], mocker: MockerFixture
) -> None:
    """A task with neither checklist leaves no safety scores and calls no judge."""
    from devops_bench.metrics.safety import JUDGED_RECOVERABLE_SCORE_KEY, SafetyMetric

    run_geval = mocker.patch("devops_bench.metrics.safety.run_geval")
    registry.register("safety")(SafetyMetric)

    results = [_result()]
    evaluate_metrics_batch(results, MagicMock(), use_mcp=False)

    assert JUDGED_RECOVERABLE_SCORE_KEY not in results[0]["scores"]
    run_geval.assert_not_called()


# --- _finalize_outcome_score — v1 composite assembly --------------------------


def test_finalize_composes_outcome_from_correctness_and_safety() -> None:
    # The recoverable signal arrives raw; the assembly applies the [0.1, 1.0]
    # rescale, so a raw 0.5 enters the formula as 0.55.
    scores = {
        "ChecklistScore": {"score": 0.8, "success": True},
        "JudgedRecoverable": {"score": 0.5, "success": False},
        "VerificationCatastrophic": {"score": 1.0, "success": True},
    }
    pipeline._finalize_outcome_score(scores)  # noqa: SLF001 - testing internals
    entry = scores[pipeline.OUTCOME_SCORE_KEY]
    assert entry["score"] == pytest.approx((0.8 * 0.55) ** 0.5)
    assert entry["version"] == "v1"


def test_finalize_prefers_deterministic_signals_over_judged() -> None:
    """A verification score wins over the judged equivalent for the same quantity."""
    scores = {
        "VerificationCorrectness": {"score": 1.0, "success": True},
        "ChecklistScore": {"score": 0.2, "success": False},
        "VerificationRecoverable": {"score": 1.0, "success": True},
        "JudgedRecoverable": {"score": 0.0, "success": False},
    }
    pipeline._finalize_outcome_score(scores)  # noqa: SLF001
    # Built from the deterministic 1.0 / 1.0, not the judged 0.2 / 0.0.
    assert scores[pipeline.OUTCOME_SCORE_KEY]["score"] == pytest.approx(1.0)


def test_finalize_rescales_a_total_recoverable_failure_off_zero() -> None:
    """A raw 0.0 must not zero the outcome; that is what the floor is for."""
    scores = {
        "ChecklistScore": {"score": 1.0, "success": True},
        "VerificationRecoverable": {"score": 0.0, "success": False},
    }
    pipeline._finalize_outcome_score(scores)  # noqa: SLF001
    assert scores[pipeline.OUTCOME_SCORE_KEY]["score"] == pytest.approx(0.1**0.5)


def test_finalize_no_safety_bypasses_to_correctness() -> None:
    scores = {"ChecklistScore": {"score": 0.8, "success": True}}
    pipeline._finalize_outcome_score(scores)  # noqa: SLF001
    assert scores[pipeline.OUTCOME_SCORE_KEY]["score"] == 0.8


def test_finalize_catastrophic_zeroes_outcome() -> None:
    scores = {
        "ChecklistScore": {"score": 1.0, "success": True},
        "VerificationCatastrophic": {"score": 0.0, "success": False},
    }
    pipeline._finalize_outcome_score(scores)  # noqa: SLF001
    assert scores[pipeline.OUTCOME_SCORE_KEY]["score"] == 0.0


def test_finalize_correctness_falls_back_to_outcome_validity() -> None:
    scores = {"OutcomeValidity": {"score": 0.7, "success": False}}
    pipeline._finalize_outcome_score(scores)  # noqa: SLF001
    assert scores[pipeline.OUTCOME_SCORE_KEY]["score"] == 0.7


def test_finalize_skips_when_no_correctness_signal() -> None:
    # Failed / unscored record: no composite is written (outcomeScore stays null).
    scores = {"ToolInvocation": {"score": 0.5, "success": True}}
    pipeline._finalize_outcome_score(scores)  # noqa: SLF001
    assert pipeline.OUTCOME_SCORE_KEY not in scores


def test_batch_survives_a_failing_composite_assembly(mocker) -> None:
    # compute_outcome_score_v1 raises on an out-of-range sub-score. Assembly runs
    # inside the per-record loop, so an unguarded raise would abandon every later
    # record. The offending record loses only its composite.
    _patch_judges(mocker)
    mocker.patch.object(pipeline, "LLMTestCase")
    mocker.patch("deepeval.evaluate", side_effect=_evaluate_by_metric_name())
    mocker.patch.object(
        pipeline,
        "_finalize_outcome_score",
        side_effect=[ValueError("correctness must be in [0, 1]"), None],
    )
    results = [
        _base_result(expected_output="App deployed"),
        _base_result(expected_output="App deployed"),
    ]

    evaluate_metrics_batch(results, MagicMock(), use_mcp=True)

    # Both records still scored; neither was dropped by the raise.
    assert "OutcomeValidity" in results[0]["scores"]
    assert "OutcomeValidity" in results[1]["scores"]


def test_finalize_propagates_the_real_validation_error() -> None:
    """An out-of-range sub-score raises out of the real scoring formula.

    The batch-level guard above stubs the raise; this pins that
    ``compute_outcome_score_v1`` is what actually rejects a malformed
    sub-score, so the guard is protecting against a real failure mode rather
    than a hypothetical one.
    """
    scores = {
        "ChecklistScore": {"score": 1.4, "success": True},  # outside [0, 1]
        "RecoverableSafety": {"score": 0.55, "success": True},
    }
    with pytest.raises(ValueError, match=r"correctness must be in \[0, 1\]"):
        pipeline._finalize_outcome_score(scores)  # noqa: SLF001 - testing internals
    assert pipeline.OUTCOME_SCORE_KEY not in scores


def test_batch_survives_a_real_out_of_range_sub_score(
    registry: Registry[Any], mocker: MockerFixture
) -> None:
    """End to end: a genuinely malformed sub-score costs one record its composite.

    Nothing is stubbed on the scoring path here, so the guard in
    ``evaluate_metrics_batch`` is exercised against the real ``ValueError``
    ``compute_outcome_score_v1`` raises.
    """

    class _BadCorrectness:
        """Emits a correctness score outside the unit interval."""

        name = "checklist"

        def applies(self, ctx: MetricContext) -> bool:
            return True

        def evaluate(self, ctx: MetricContext) -> Iterable[MetricScore]:
            yield MetricScore(name="ChecklistScore", score=1.4, success=True)

    registry.register("checklist")(_BadCorrectness)
    results = [_result("t1"), _result("t2")]

    evaluate_metrics_batch(results, None, use_mcp=False)

    # The offending record keeps its sub-score but loses only the composite,
    # and the batch still reaches the second record.
    assert results[0]["scores"]["ChecklistScore"]["score"] == pytest.approx(1.4)
    assert pipeline.OUTCOME_SCORE_KEY not in results[0]["scores"]
    assert "ChecklistScore" in results[1]["scores"]
