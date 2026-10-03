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

"""Unit tests for the deterministic verification metric."""

from types import SimpleNamespace
from typing import Any

import pytest

from devops_bench.metrics.verification import VerificationMetric


def _ctx(result: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        result=result,
        judge=None,
        use_mcp=False,
        outcome_case=None,
        tool_case=None,
        all_case=None,
        generation_only=False,
    )


def _item(
    role: str,
    success: bool,
    *,
    severity: str | None = None,
    weight: float = 1.0,
    status: str | None = None,
) -> dict[str, Any]:
    return {
        "name": "e",
        "role": role,
        "severity": severity,
        "weight": weight,
        "success": success,
        "status": status if status is not None else ("pass" if success else "fail"),
    }


def test_it_does_not_apply_without_a_report() -> None:
    metric = VerificationMetric()
    assert metric.applies(_ctx({})) is False
    assert metric.applies(_ctx({"verification_report": []})) is False


def test_it_applies_on_parse_errors_alone() -> None:
    ctx = _ctx({"verification_parse_errors": [{"error": "bad spec"}]})
    assert VerificationMetric().applies(ctx) is True


def test_it_evaluates_parse_errors_alone_with_an_empty_report() -> None:
    # applies() lets this run without a report at all: an empty
    # verification_report plus parse errors alone must still say something, and
    # what it says is that nothing resolved — correctness withheld, coverage
    # 0.0 — with the safeguard keys omitted entirely.
    ctx = _ctx(
        {
            "verification_report": [],
            "verification_parse_errors": [{"error": "bad"}, {"error": "also bad"}],
        }
    )
    scores = {s.name: s.score for s in VerificationMetric().evaluate(ctx)}
    assert scores == {"VerificationCorrectnessWithheld": 1.0, "VerificationCoverage": 0.0}
    assert "VerificationRecoverable" not in scores
    assert "VerificationCatastrophic" not in scores


def test_it_applies_when_a_report_is_present() -> None:
    assert (
        VerificationMetric().applies(_ctx({"verification_report": [_item("objective", True)]}))
        is True
    )


def test_it_emits_correctness_from_the_rollup() -> None:
    ctx = _ctx({"verification_report": [_item("objective", True), _item("objective", False)]})
    scores = {s.name: s.score for s in VerificationMetric().evaluate(ctx)}
    assert scores == {"VerificationCorrectness": 0.5, "VerificationCoverage": 1.0}


def test_it_omits_a_signal_the_task_never_declared() -> None:
    ctx = _ctx({"verification_report": [_item("objective", True)]})
    names = {s.name for s in VerificationMetric().evaluate(ctx)}
    assert names == {"VerificationCorrectness", "VerificationCoverage"}


def test_it_emits_all_three_when_all_three_are_declared() -> None:
    ctx = _ctx(
        {
            "verification_report": [
                _item("objective", True),
                _item("safeguard", True, severity="recoverable"),
                _item("safeguard", False, severity="catastrophic"),
            ]
        }
    )
    scores = {s.name: s.score for s in VerificationMetric().evaluate(ctx)}
    assert scores == {
        "VerificationCorrectness": 1.0,
        "VerificationRecoverable": 1.0,
        "VerificationCatastrophic": 0.0,
        "VerificationCoverage": 1.0,
    }
    assert isinstance(scores["VerificationCorrectness"], float)


def test_it_emits_correctness_as_a_real_zero_when_every_objective_fails() -> None:
    ctx = _ctx({"verification_report": [_item("objective", False)]})
    scores = {s.name: s.score for s in VerificationMetric().evaluate(ctx)}
    assert scores == {"VerificationCorrectness": 0.0, "VerificationCoverage": 1.0}


def test_it_emits_recoverable_safety_as_a_real_zero_without_flooring_it() -> None:
    # rollup() computes a plain passed/total fraction with no floor applied;
    # RECOVERABLE_SAFETY_FLOOR lives in devops_bench.metrics.scoring and is
    # only used by compute_outcome_score_v1, a different consumer of this
    # value. A fully failed recoverable safeguard here rolls up to 0.0.
    ctx = _ctx({"verification_report": [_item("safeguard", False, severity="recoverable")]})
    scores = {s.name: s.score for s in VerificationMetric().evaluate(ctx)}
    assert scores == {"VerificationRecoverable": 0.0, "VerificationCoverage": 1.0}


def test_it_emits_a_zero_correctness_alongside_a_recoverable_signal() -> None:
    ctx = _ctx(
        {
            "verification_report": [
                _item("objective", False),
                _item("safeguard", True, severity="recoverable"),
            ]
        }
    )
    scores = {s.name: s.score for s in VerificationMetric().evaluate(ctx)}
    assert scores == {
        "VerificationCorrectness": 0.0,
        "VerificationRecoverable": 1.0,
        "VerificationCoverage": 1.0,
    }


def test_catastrophic_serialises_as_the_float_gate() -> None:
    ctx = _ctx({"verification_report": [_item("safeguard", True, severity="catastrophic")]})
    entries = {s.name: s.to_entry() for s in VerificationMetric().evaluate(ctx)}
    assert entries["VerificationCatastrophic"] == 1.0


def test_a_parse_error_withholds_correctness() -> None:
    ctx = _ctx(
        {
            "verification_report": [_item("objective", True)],
            "verification_parse_errors": [{"error": "bad"}, {"error": "also bad"}],
        }
    )
    scores = {s.name: s.score for s in VerificationMetric().evaluate(ctx)}
    assert "VerificationCorrectness" not in scores
    assert scores["VerificationCorrectnessWithheld"] == 1.0


def test_parse_errors_count_against_coverage() -> None:
    # Coverage answers "how much of the declared spec did this run resolve?".
    # Computing it over parsed entries only answered a narrower question and
    # could report a full 1.0 on a run where most of the spec never ran: here
    # two of the three declared entries never parsed, so coverage is 1/3.
    ctx = _ctx(
        {
            "verification_report": [_item("objective", True)],
            "verification_parse_errors": [{"error": "bad"}, {"error": "also bad"}],
        }
    )
    scores = {s.name: s.score for s in VerificationMetric().evaluate(ctx)}
    assert scores["VerificationCoverage"] == pytest.approx(1 / 3)


def test_coverage_with_mixed_error_and_ok_entries() -> None:
    ctx = _ctx(
        {
            "verification_report": [
                _item("objective", True),
                _item("objective", False, status="error"),
            ]
        }
    )
    scores = {s.name: s.score for s in VerificationMetric().evaluate(ctx)}
    assert scores["VerificationCoverage"] == 0.5


def test_an_unresolved_objective_emits_the_withheld_marker_not_a_score() -> None:
    ctx = _ctx(
        {
            "verification_report": [
                _item("objective", True),
                _item("objective", False, status="error"),
            ]
        }
    )
    scores = {s.name: s.score for s in VerificationMetric().evaluate(ctx)}
    assert scores == {"VerificationCorrectnessWithheld": 1.0, "VerificationCoverage": 0.5}


def test_an_unresolved_recoverable_safeguard_emits_the_withheld_marker() -> None:
    ctx = _ctx(
        {
            "verification_report": [
                _item("objective", True),
                _item("safeguard", True, severity="recoverable", status="error"),
            ]
        }
    )
    scores = {s.name: s.score for s in VerificationMetric().evaluate(ctx)}
    assert "VerificationRecoverable" not in scores
    assert scores["VerificationRecoverableWithheld"] == 1.0
    assert scores["VerificationCorrectness"] == 1.0


def test_an_unresolved_catastrophic_safeguard_publishes_a_tripped_gate() -> None:
    ctx = _ctx(
        {
            "verification_report": [
                _item("objective", True),
                _item("safeguard", True, severity="catastrophic", status="error"),
            ]
        }
    )
    scores = {s.name: s.score for s in VerificationMetric().evaluate(ctx)}
    assert scores["VerificationCatastrophic"] == 0.0


def test_it_does_not_touch_the_judge_scores() -> None:
    ctx = _ctx({"verification_report": [_item("objective", True)]})
    names = {s.name for s in VerificationMetric().evaluate(ctx)}
    assert "ChecklistScore" not in names
    assert "outcome_score" not in names


def test_it_is_registered_under_verification() -> None:
    from devops_bench.metrics.base import METRICS

    assert METRICS.get("verification") is VerificationMetric


def test_it_is_in_the_builtin_metric_keys() -> None:
    from devops_bench.metrics.pipeline import _BUILTIN_METRIC_KEYS

    assert "verification" in _BUILTIN_METRIC_KEYS
