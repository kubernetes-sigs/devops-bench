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


def test_checklist_extracts_critical_requirements_bullets():
    expected = (
        "Critical Requirements:\n- Deployment must use 3 replicas\n- Service exposes port 8080\n"
    )
    assert extract_checklist_items(expected, use_mcp=True) == [
        "Deployment must use 3 replicas",
        "Service exposes port 8080",
    ]


def test_checklist_stops_at_expected_manifest_marker():
    expected = (
        "Critical Requirements:\n"
        "- Keep replicas at 3\n"
        "Expected Manifest Generated:\n"
        "- apiVersion: apps/v1\n"
    )
    assert extract_checklist_items(expected, use_mcp=True) == ["Keep replicas at 3"]


def test_checklist_drops_tool_call_items_when_mcp_disabled():
    expected = "Critical Requirements:\n- Expected Tool Call: apply_manifest\n- App is reachable\n"
    assert extract_checklist_items(expected, use_mcp=False) == ["App is reachable"]
    assert extract_checklist_items(expected, use_mcp=True) == [
        "Expected Tool Call: apply_manifest",
        "App is reachable",
    ]


def test_checklist_empty_when_no_bullets():
    assert extract_checklist_items("Some prose without bullets", use_mcp=True) == []


def test_checklist_preserves_trailing_hyphen():
    # ``lstrip("- ")`` must not eat a trailing hyphen the way ``strip("- ")`` would.
    expected = "Critical Requirements:\n- Deploy to namespace staging-\n"
    assert extract_checklist_items(expected, use_mcp=True) == ["Deploy to namespace staging-"]


def test_checklist_threshold_is_value_independent_of_tool_invocation():
    # Phase-0 fix #1: a dedicated constant so the checklist cutoff is not
    # silently coupled to ``TOOL_INVOCATION_THRESHOLD``.
    assert CHECKLIST_THRESHOLD == 0.8


# --- evaluate_metrics_batch (deepeval mocked) ---------------------------------


def _metric_result(name, score=1.0, success=True, reason="ok"):
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


def _base_result(**overrides):
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


def _patch_judges(mocker):
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


def test_batch_scores_outcome_and_tool(mocker):
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


def test_batch_skips_tool_when_mcp_disabled(mocker):
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


def test_batch_use_mcp_falls_back_to_env(mocker):
    _patch_judges(mocker)
    mocker.patch.object(pipeline, "LLMTestCase")
    mocker.patch("deepeval.evaluate", side_effect=_evaluate_by_metric_name())
    mocker.patch.object(pipeline, "get_bool", return_value=False)
    judge = MagicMock()
    results = [_base_result()]

    evaluate_metrics_batch(results, judge)  # use_mcp omitted → env fallback

    assert "ToolInvocation" not in results[0]["scores"]


def test_batch_computes_checklist_score(mocker):
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


def test_checklist_per_item_geval_uses_explicit_pass_threshold(mocker):
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


def test_batch_invokes_grounding_and_chaos(mocker):
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


def test_batch_score_insertion_order_matches_legacy_results_json(mocker):
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


def test_canonical_tool_name_strips_mcp_prefix():
    assert pipeline._canonical_tool_name("default__generate_manifest") == "generate_manifest"
    assert pipeline._canonical_tool_name("run_shell_command") == "run_shell_command"


def test_build_context_threads_generation_only(mocker):
    mocker.patch.object(pipeline, "LLMTestCase", side_effect=lambda **kw: SimpleNamespace(**kw))
    ctx = pipeline._build_context(_base_result(generation_only=True), MagicMock(), True)
    assert ctx.generation_only is True
    # Absent key defaults to False.
    assert pipeline._build_context(_base_result(), MagicMock(), True).generation_only is False


def test_build_context_normalizes_tool_names_for_judge(mocker):
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


def test_outcome_validity_override_only_when_generation_only(mocker):
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


def test_build_context_warns_on_empty_expected_output(caplog, mocker):
    mocker.patch.object(pipeline, "LLMTestCase", side_effect=lambda **kw: SimpleNamespace(**kw))
    import logging

    res = _base_result(expected_output="")
    with caplog.at_level(logging.WARNING):
        pipeline._build_context(res, MagicMock(), True)
    assert len(caplog.records) == 1
    assert "has an empty expected_output" in caplog.text


def test_build_context_no_warning_when_expected_output_set(caplog, mocker):
    mocker.patch.object(pipeline, "LLMTestCase", side_effect=lambda **kw: SimpleNamespace(**kw))
    import logging

    res = _base_result(expected_output="App deployed")
    with caplog.at_level(logging.WARNING):
        pipeline._build_context(res, MagicMock(), True)
    assert len(caplog.records) == 0


def test_build_context_missing_expected_output_defaults_and_warns(caplog, mocker):
    mocker.patch.object(pipeline, "LLMTestCase", side_effect=lambda **kw: SimpleNamespace(**kw))
    import logging

    res = _base_result()
    del res["expected_output"]
    with caplog.at_level(logging.WARNING):
        pipeline._build_context(res, MagicMock(), True)
    assert len(caplog.records) == 1
    assert "has an empty expected_output" in caplog.text
