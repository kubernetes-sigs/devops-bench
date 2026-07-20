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

"""Self-contained tests for the batch metric scoring pipeline.

Stub evaluators are registered into ``METRICS`` so the dispatch loop, evaluator
ordering, and per-metric exception isolation are exercised without importing any
concrete metric family that lands in a follow-up. The registry is snapshotted
and restored so the stubs never leak into sibling tests.
"""

from __future__ import annotations

from collections.abc import Iterable

import pytest

from devops_bench.metrics.base import METRICS, MetricContext, MetricScore
from devops_bench.metrics.pipeline import evaluate_metrics_batch


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
def registry():
    """Yield the ``METRICS`` registry emptied for the test, then restored."""
    saved = dict(METRICS._items)
    METRICS._items.clear()
    try:
        yield METRICS
    finally:
        METRICS._items.clear()
        METRICS._items.update(saved)


def _result(name: str = "t1") -> dict:
    """Minimal execution-result dict the pipeline can build a context from."""
    return {"name": name, "input": "q", "output": "a", "expected_output": "a"}


def test_scores_written_from_registered_evaluator(registry):
    registry.register("stub")(_StubEvaluator)
    results = [_result()]

    evaluate_metrics_batch(results, None, use_mcp=False)

    assert results[0]["scores"] == {"stub_score": {"score": 1.0, "success": True, "reason": "ok"}}


def test_one_failing_metric_does_not_abort_others(registry, caplog):
    registry.register("boom")(_BoomEvaluator)
    registry.register("stub")(_StubEvaluator)
    results = [_result()]

    evaluate_metrics_batch(results, None, use_mcp=False)

    # The raising metric is isolated: the sibling metric still records a score.
    assert "stub_score" in results[0]["scores"]
    assert "boom" in caplog.text


def test_evaluate_skipped_when_applies_false(registry):
    registry.register("skipped")(_SkippedEvaluator)
    results = [_result()]

    evaluate_metrics_batch(results, None, use_mcp=False)

    assert results[0]["scores"] == {}


def test_absent_builtin_keys_are_skipped(registry):
    # None of the pinned builtin keys are registered here; the batch must not
    # raise while building evaluators (the builtin-key filter guards this).
    registry.register("stub")(_StubEvaluator)
    results = [_result()]

    evaluate_metrics_batch(results, None, use_mcp=False)

    assert results[0]["scores"]["stub_score"]["score"] == 1.0


def test_builtin_keys_scored_before_third_party(registry):
    # A registered builtin ("checklist") is ordered ahead of a third-party
    # metric even though the latter sorts earlier alphabetically.
    registry.register("checklist")(_make_stub("checklist"))
    registry.register("aaa_custom")(_make_stub("aaa_custom"))
    results = [_result()]

    evaluate_metrics_batch(results, None, use_mcp=False)

    assert list(results[0]["scores"].keys()) == ["checklist_score", "aaa_custom_score"]


def test_each_result_in_batch_is_scored(registry):
    registry.register("stub")(_StubEvaluator)
    results = [_result("t1"), _result("t2"), _result("t3")]

    evaluate_metrics_batch(results, None, use_mcp=False)

    assert all(r["scores"].get("stub_score") for r in results)
