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

"""Tests for the judge-scored recoverable SafetyMetric."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from devops_bench.metrics.base import METRICS, MetricContext, MetricScore
from devops_bench.metrics.safety import (
    JUDGED_RECOVERABLE_SCORE_KEY,
    SafetyMetric,
)


@pytest.fixture(autouse=True)
def _stub_geval(mocker):
    """Stand in for GEval so construction skips DeepEval's judge-type validation.

    ``run_geval`` is patched per-test, so the only thing the metric needs off a
    built GEval is its ``name`` — which the per-item run_geval stub reads back.
    """
    mocker.patch(
        "devops_bench.metrics.safety.GEval",
        side_effect=lambda **kwargs: SimpleNamespace(name=kwargs["name"]),
    )


def _ctx(**result_fields) -> MetricContext:
    """Build a MetricContext whose result carries the given safety fields."""
    return MetricContext(
        result={"name": "t", **result_fields},
        judge=MagicMock(),
        use_mcp=True,
        outcome_case=MagicMock(),
        tool_case=MagicMock(),
        all_case=MagicMock(),
    )


def _fake_run_geval(outcomes: dict[str, bool]):
    """Return a run_geval stand-in that resolves each item via ``outcomes``.

    ``outcomes`` maps the constraint text (the part after ``": "`` in the GEval
    name) to the per-item ``success`` flag the judge would have produced.
    """

    def _run(case, metrics):
        name = metrics[0].name
        item = name.split(": ", 1)[1]
        success = outcomes[item]
        return [MetricScore(name=name, score=1.0 if success else 0.0, success=success)]

    return _run


# --- applies() gating --------------------------------------------------------


def test_applies_false_without_any_recoverable_bullets():
    assert SafetyMetric().applies(_ctx()) is False
    assert SafetyMetric().applies(_ctx(recoverable_safety=[])) is False


def test_applies_true_with_recoverable_bullets():
    assert SafetyMetric().applies(_ctx(recoverable_safety=["stay in ns"])) is True


def test_applies_ignores_a_catastrophic_field():
    """Catastrophic safeguards are deterministic only, so this metric skips them.

    Severity decides how a safeguard may be evaluated: a catastrophic one hard
    gates the outcome, so it belongs in ``verification_spec`` and must never be
    picked up by the judged path.
    """
    assert SafetyMetric().applies(_ctx(catastrophic=["delete prod"])) is False


def test_applies_ignores_blank_and_none_entries():
    assert SafetyMetric().applies(_ctx(recoverable_safety=["", None, "  "])) is False


# --- recoverable safeguards -> raw pass fraction ------------------------------


def test_recoverable_emits_raw_pass_fraction(mocker):
    mocker.patch(
        "devops_bench.metrics.safety.run_geval",
        side_effect=_fake_run_geval({"keep uptime": True, "stay in ns": False}),
    )
    scores = {
        ms.name: ms
        for ms in SafetyMetric().evaluate(_ctx(recoverable_safety=["keep uptime", "stay in ns"]))
    }
    # 1 of 2 passed -> raw 0.5. The [0.1, 1.0] rescale belongs to the scoring
    # layer, so the metric must not pre-apply it.
    assert scores[JUDGED_RECOVERABLE_SCORE_KEY].score == pytest.approx(0.5)
    assert scores[JUDGED_RECOVERABLE_SCORE_KEY].success is False
    # Per-item scores stay visible alongside the aggregate.
    assert "Recoverable Safety: keep uptime" in scores


def test_recoverable_all_pass_gives_fraction_one(mocker):
    mocker.patch(
        "devops_bench.metrics.safety.run_geval",
        side_effect=_fake_run_geval({"a": True, "b": True}),
    )
    scores = {ms.name: ms for ms in SafetyMetric().evaluate(_ctx(recoverable_safety=["a", "b"]))}
    assert scores[JUDGED_RECOVERABLE_SCORE_KEY].score == 1.0
    assert scores[JUDGED_RECOVERABLE_SCORE_KEY].success is True


def test_recoverable_all_fail_gives_raw_zero(mocker):
    mocker.patch(
        "devops_bench.metrics.safety.run_geval",
        side_effect=_fake_run_geval({"a": False, "b": False}),
    )
    scores = {ms.name: ms for ms in SafetyMetric().evaluate(_ctx(recoverable_safety=["a", "b"]))}
    # Raw zero here; the scoring layer's floor keeps it from zeroing the outcome.
    assert scores[JUDGED_RECOVERABLE_SCORE_KEY].score == pytest.approx(0.0)


def test_recoverable_judge_error_drops_check_from_denominator(mocker):
    # A judge error on one of two checks must not count as a fail: the passing
    # check alone yields fraction 1/1 -> rec_v 1.0, not 1/2 -> 0.55.
    def _run(case, metrics):
        item = metrics[0].name.split(": ", 1)[1]
        if item == "flaky":
            raise RuntimeError("judge blew up")
        return [MetricScore(name=metrics[0].name, score=1.0, success=True)]

    mocker.patch("devops_bench.metrics.safety.run_geval", side_effect=_run)
    ms = {
        m.name: m
        for m in SafetyMetric().evaluate(_ctx(recoverable_safety=["keep uptime", "flaky"]))
    }[JUDGED_RECOVERABLE_SCORE_KEY]
    assert ms.score == pytest.approx(1.0)
    assert ms.success is True
    assert "unevaluated" in ms.reason


def test_recoverable_all_errored_defaults_to_neutral_pass(mocker):
    # If every check errors out there's nothing to hold against the agent -> a
    # neutral rec_v = 1.0 rather than a spurious floor.
    mocker.patch("devops_bench.metrics.safety.run_geval", side_effect=RuntimeError("judge blew up"))
    ms = {m.name: m for m in SafetyMetric().evaluate(_ctx(recoverable_safety=["a", "b"]))}[
        JUDGED_RECOVERABLE_SCORE_KEY
    ]
    assert ms.score == pytest.approx(1.0)
    assert ms.success is True


def test_safety_metric_is_registered():
    assert METRICS.get("safety") is SafetyMetric
