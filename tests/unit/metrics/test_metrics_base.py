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

"""Tests for the metrics extension primitives: MetricScore, run_geval, registry."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from devops_bench.metrics.base import (
    METRICS,
    MetricContext,
    MetricEvaluator,
    MetricScore,
    run_geval,
)

# --- MetricScore.to_entry() — D3 legacy-shape preservation -------------------


def test_metric_score_to_entry_bare_value_for_rate_metrics():
    # Rate / perf passthroughs: no success or reason → entry is the raw value.
    ms = MetricScore(name="DocRetrievalRate", score=0.42)
    assert ms.to_entry() == 0.42


def test_metric_score_to_entry_dict_for_judged_metrics():
    # Judged metrics: success + reason flip the entry to the legacy dict shape.
    ms = MetricScore(name="OutcomeValidity", score=0.9, success=True, reason="solid")
    assert ms.to_entry() == {"score": 0.9, "success": True, "reason": "solid"}


def test_metric_score_to_entry_round_trips_through_either_shape():
    bare = MetricScore(name="X", score=0.5)
    judged = MetricScore(name="Y", score=0.5, success=False, reason="meh")
    assert isinstance(bare.to_entry(), float)
    assert isinstance(judged.to_entry(), dict)


def test_metric_score_to_entry_preserves_none_score():
    ms = MetricScore(name="Workload_Uptime_Percentage", score=None)
    assert ms.to_entry() is None


# --- run_geval — strips DeepEval's " [GEval]" suffix from score names ---------


def test_run_geval_strips_geval_suffix_exactly_once(mocker):
    # DeepEval appends " [GEval]" to GEval metric names; run_geval must strip it
    # and emit the bare name.
    test_result = SimpleNamespace(
        metrics_data=[
            SimpleNamespace(name="OutcomeValidity [GEval]", score=0.7, success=True, reason="ok")
        ]
    )
    mocker.patch("deepeval.evaluate", return_value=SimpleNamespace(test_results=[test_result]))

    out = run_geval(MagicMock(), [MagicMock()])

    assert len(out) == 1
    assert out[0].name == "OutcomeValidity"
    assert out[0].score == 0.7
    assert out[0].success is True
    assert out[0].reason == "ok"


def test_run_geval_leaves_non_geval_names_alone(mocker):
    # Metric data that does not carry the suffix passes through unchanged.
    test_result = SimpleNamespace(
        metrics_data=[SimpleNamespace(name="GracefulRecovery", score=4.0, success=True, reason="r")]
    )
    mocker.patch("deepeval.evaluate", return_value=SimpleNamespace(test_results=[test_result]))

    out = run_geval(MagicMock(), [MagicMock()])

    assert out[0].name == "GracefulRecovery"


# --- MetricContext + use_mcp flow --------------------------------------------


def test_metric_context_carries_use_mcp_and_judge():
    # use_mcp arrives via MetricContext (CONVENTIONS.md §7): metrics never
    # self-read BENCH_USE_MCP.
    judge = MagicMock()
    ctx = MetricContext(
        result={"name": "t"},
        judge=judge,
        use_mcp=False,
        outcome_case=MagicMock(),
        tool_case=MagicMock(),
        all_case=MagicMock(),
    )
    assert ctx.use_mcp is False
    assert ctx.judge is judge


# --- METRICS registry — extension axis ---------------------------------------


def test_metric_evaluator_protocol_is_runtime_checkable():
    class Dummy:
        name = "dummy"

        def applies(self, ctx):
            return True

        def evaluate(self, ctx):
            return []

    assert isinstance(Dummy(), MetricEvaluator)


def test_metrics_registry_records_decorated_class():
    # A new metric is one decorator away — no orchestrator surgery. We register
    # under a unique key, then deregister so the global registry stays clean.
    @METRICS.register("test_metric_base_dummy")
    class _Dummy:
        name = "TestMetricBaseDummy"

        def applies(self, ctx):
            return True

        def evaluate(self, ctx):
            yield MetricScore(name="TestMetricBaseDummy", score=1.0)

    try:
        assert METRICS.get("test_metric_base_dummy") is _Dummy
        assert "test_metric_base_dummy" in METRICS
    finally:
        # ``Registry`` exposes no delete; reach into the private dict so the
        # cleanup does not leak into sibling tests.
        METRICS._items.pop("test_metric_base_dummy", None)
