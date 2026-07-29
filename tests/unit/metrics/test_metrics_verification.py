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
    # verification_report plus parse errors alone must still fail closed into
    # correctness, keep coverage a full 1.0 (nothing declared errored, since
    # nothing declared parsed), and omit the safeguard keys entirely.
    ctx = _ctx(
        {
            "verification_report": [],
            "verification_parse_errors": [{"error": "bad"}, {"error": "also bad"}],
        }
    )
    scores = {s.name: s.score for s in VerificationMetric().evaluate(ctx)}
    assert scores == {"VerificationCorrectness": 0.0, "VerificationCoverage": 1.0}
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


def test_correctness_reflects_fail_closed_parse_errors() -> None:
    ctx = _ctx(
        {
            "verification_report": [_item("objective", True)],
            "verification_parse_errors": [{"error": "bad"}, {"error": "also bad"}],
        }
    )
    scores = {s.name: s.score for s in VerificationMetric().evaluate(ctx)}
    assert scores["VerificationCorrectness"] == 1 / 3


def test_parse_errors_sink_correctness_but_do_not_count_against_coverage() -> None:
    # Parse errors and coverage measure different things and must not be
    # conflated. A parse error is a deterministic authoring bug (the spec is
    # malformed), not an environmental non-evaluation, so it fails closed into
    # VerificationCorrectness (an unparseable objective is scored as not met)
    # while leaving VerificationCoverage, which tracks whether declared checks
    # actually got to run, at a full 1.0: the one entry that did parse ran and
    # was observed cleanly.
    ctx = _ctx(
        {
            "verification_report": [_item("objective", True)],
            "verification_parse_errors": [{"error": "bad"}, {"error": "also bad"}],
        }
    )
    scores = {s.name: s.score for s in VerificationMetric().evaluate(ctx)}
    assert scores["VerificationCorrectness"] == 1 / 3
    assert scores["VerificationCoverage"] == 1.0


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
