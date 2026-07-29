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

"""Unit tests for post-agent verification wiring in the default harness."""

import time
from unittest.mock import patch

import pytest

from devops_bench.evalharness.default import DefaultEvalHarness
from devops_bench.verification.base import MIN_LEAF_BUDGET_SECONDS, VerificationResult
from devops_bench.verification.spec import parse_entries

_SPEC = [
    {
        "name": "web-ready",
        "role": "objective",
        "weight": 3,
        "check": {"type": "pod_healthy", "selector": "app=web", "namespace": "shop"},
    },
    {
        "name": "nothing-in-default",
        "role": "safeguard",
        "severity": "catastrophic",
        "check": {
            "type": "resource_property",
            "kind": "deployment",
            "selector": "app=web",
            "namespace": "default",
            "op": "absent",
        },
    },
]


def _harness() -> DefaultEvalHarness:
    harness = DefaultEvalHarness.__new__(DefaultEvalHarness)
    # Minimal attributes replace_placeholders() reads unconditionally
    # (project_id, app_location) or falls back to when the caller's
    # target_deployment / namespace argument is falsy.
    harness.project_id = "proj"
    harness.app_location = "us-central1"
    harness.target_deployment = "web"
    harness.namespace = "shop"
    return harness


def test_report_carries_the_scoring_vocabulary_for_every_entry() -> None:
    entries, errors = parse_entries(_SPEC)
    assert errors == []
    ok = VerificationResult(success=True, elapsed_time=0.1, reason="fine")
    with patch("devops_bench.evalharness.default.VerifierAgent.run_entry", return_value=ok):
        report = _harness()._run_verification(entries)

    assert [r["name"] for r in report] == ["web-ready", "nothing-in-default"]
    assert report[0]["role"] == "objective"
    assert report[0]["weight"] == 3
    assert report[0]["mode"] == "converge"
    assert report[0]["severity"] is None
    assert report[1]["severity"] == "catastrophic"
    assert report[1]["mode"] == "assert"
    assert all(r["success"] is True for r in report)


def test_one_raising_entry_does_not_abort_the_rest() -> None:
    entries, _ = parse_entries(_SPEC)
    ok = VerificationResult(success=True, elapsed_time=0.1, reason="fine")
    with patch(
        "devops_bench.evalharness.default.VerifierAgent.run_entry",
        side_effect=[RuntimeError("cluster gone"), ok],
    ):
        report = _harness()._run_verification(entries)

    assert len(report) == 2
    assert report[0]["success"] is False
    assert "cluster gone" in report[0]["reason"]
    assert report[1]["success"] is True


def test_no_entries_yields_an_empty_report() -> None:
    assert _harness()._run_verification([]) == []


def test_child_results_are_serialised_onto_the_report() -> None:
    entries, _ = parse_entries(_SPEC[:1])
    child = VerificationResult(success=False, elapsed_time=0.0, reason="no pods", name="c")
    parent = VerificationResult(
        success=False, elapsed_time=0.2, reason="child failed", children=[child]
    )
    with patch("devops_bench.evalharness.default.VerifierAgent.run_entry", return_value=parent):
        report = _harness()._run_verification(entries)

    assert report[0]["children"][0]["reason"] == "no pods"


def test_the_report_feeds_rollup_directly() -> None:
    from devops_bench.verification.rollup import rollup

    entries, _ = parse_entries(_SPEC)
    ok = VerificationResult(success=True, elapsed_time=0.0, reason="fine")
    with patch("devops_bench.evalharness.default.VerifierAgent.run_entry", return_value=ok):
        report = _harness()._run_verification(entries)

    scores = rollup(report)
    assert scores.correctness == 1.0
    assert scores.catastrophic == 1.0
    assert scores.recoverable_safety is None


def test_converge_entries_get_the_min_of_per_entry_and_remaining_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tight total budget caps each converging entry below its per-entry timeout."""
    entries, errors = parse_entries(_SPEC[:1] + [{**_SPEC[0], "name": "web-ready-2"}])
    assert errors == []
    monkeypatch.setattr("devops_bench.evalharness.default.VERIFICATION_TOTAL_BUDGET_SEC", 5.0)

    seen_timeouts: list[float] = []

    def fake_run_entry(entry: object, timeout_sec: float = 120) -> VerificationResult:
        seen_timeouts.append(timeout_sec)
        return VerificationResult(success=True, elapsed_time=0.0, reason="ok")

    with patch(
        "devops_bench.evalharness.default.VerifierAgent.run_entry", side_effect=fake_run_entry
    ):
        _harness()._run_verification(entries, timeout_sec=120)

    assert len(seen_timeouts) == 2
    assert all(0 < t <= 5.0 for t in seen_timeouts)


def test_converge_entry_is_recorded_as_budget_exhausted_once_the_total_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, errors = parse_entries(_SPEC[:1] + [{**_SPEC[0], "name": "web-ready-2"}])
    assert errors == []
    # Above MIN_LEAF_BUDGET_SECONDS so the first entry clears the guard; the
    # sleep below then drops the remainder under it for the second entry.
    total_budget = MIN_LEAF_BUDGET_SECONDS * 1.2
    monkeypatch.setattr(
        "devops_bench.evalharness.default.VERIFICATION_TOTAL_BUDGET_SEC", total_budget
    )

    def fake_run_entry(entry: object, timeout_sec: float = 120) -> VerificationResult:
        sleep_sec = MIN_LEAF_BUDGET_SECONDS * 0.3  # outruns the tiny total budget
        time.sleep(sleep_sec)
        return VerificationResult(success=True, elapsed_time=sleep_sec, reason="ok")

    with patch(
        "devops_bench.evalharness.default.VerifierAgent.run_entry", side_effect=fake_run_entry
    ):
        report = _harness()._run_verification(entries, timeout_sec=120)

    assert report[0]["success"] is True
    assert report[1]["success"] is False
    assert report[1]["status"] == "error"
    assert report[1]["reason"] == "verification total budget exhausted before evaluation"


def test_converge_entry_is_recorded_as_budget_exhausted_in_the_sub_second_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A remaining budget under _MIN_LEAF_BUDGET_SECONDS must not reach run_entry.

    A remaining budget of, say, 0.4s is > 0 and so passed the old ``remaining
    <= 0`` guard straight into ``run_entry``, where the runner's own
    ``_MIN_LEAF_BUDGET_SECONDS`` short-circuit records "deadline exhausted
    before evaluation" as a FAIL, misrepresenting a never-observed entry as
    one that was. The harness guard must catch this window itself and record
    "error" without ever calling ``run_entry``.
    """
    entries, errors = parse_entries(_SPEC[:1] + [{**_SPEC[0], "name": "web-ready-2"}])
    assert errors == []
    # Above _MIN_LEAF_BUDGET_SECONDS so the first entry clears the guard; the
    # sleep below leaves a sub-second, but strictly positive, remainder.
    monkeypatch.setattr("devops_bench.evalharness.default.VERIFICATION_TOTAL_BUDGET_SEC", 1.2)

    calls: list[str] = []

    def fake_run_entry(entry: object, timeout_sec: float = 120) -> VerificationResult:
        calls.append(entry.name)  # type: ignore[attr-defined]
        time.sleep(0.9)  # leaves a sub-second, but positive, remaining budget
        return VerificationResult(success=True, elapsed_time=0.9, reason="ok")

    with patch(
        "devops_bench.evalharness.default.VerifierAgent.run_entry", side_effect=fake_run_entry
    ):
        report = _harness()._run_verification(entries, timeout_sec=120)

    assert calls == ["web-ready"]  # the second entry never reached run_entry
    assert report[1]["success"] is False
    assert report[1]["status"] == "error"
    assert report[1]["reason"] == "verification total budget exhausted before evaluation"


def test_assert_entry_still_evaluates_after_the_total_budget_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries, errors = parse_entries(_SPEC)  # [0] objective (converge), [1] safeguard (assert)
    assert errors == []
    # Above _MIN_LEAF_BUDGET_SECONDS so the first (converge) entry clears the
    # guard; the sleep below then drops the remainder under it before [1].
    monkeypatch.setattr("devops_bench.evalharness.default.VERIFICATION_TOTAL_BUDGET_SEC", 1.2)

    called: list[str] = []

    def fake_run_entry(entry: object, timeout_sec: float = 120) -> VerificationResult:
        called.append(entry.name)  # type: ignore[attr-defined]
        time.sleep(0.3)  # the first (converge) call alone outruns the tiny budget
        return VerificationResult(success=True, elapsed_time=0.3, reason="ok")

    with patch(
        "devops_bench.evalharness.default.VerifierAgent.run_entry", side_effect=fake_run_entry
    ):
        report = _harness()._run_verification(entries, timeout_sec=120)

    assert called == ["web-ready", "nothing-in-default"]
    assert report[1]["mode"] == "assert"
    assert report[1]["success"] is True


def test_scenario_resolves_a_verify_reference_to_the_entry() -> None:
    from devops_bench.evalharness.scenario import ScenarioManager

    entries, _ = parse_entries(_SPEC)
    manager = ScenarioManager(
        target_deployment="web",
        namespace="shop",
        verification_mapping={e.name: e for e in entries},
        skip_port_forward=True,
    )
    resolved = manager._resolve_verification("web-ready")
    assert resolved is entries[0]


def test_scenario_returns_none_for_an_unknown_verify_reference() -> None:
    from devops_bench.evalharness.scenario import ScenarioManager

    manager = ScenarioManager(
        target_deployment="web",
        namespace="shop",
        verification_mapping={},
        skip_port_forward=True,
    )
    assert manager._resolve_verification("nope") is None
    assert manager._resolve_verification(None) is None


def test_resolve_spec_placeholders_substitutes_inside_a_jsonpath_string() -> None:
    harness = _harness()
    raw = {
        "path": 'spec.template.spec.containers[?(@.image =~ "hello-app-{{CLUSTER_NAME}}")].image'
    }

    resolved = harness._resolve_spec_placeholders(raw, "prod-cluster", "web", "shop")

    assert resolved["path"] == (
        'spec.template.spec.containers[?(@.image =~ "hello-app-prod-cluster")].image'
    )


def test_resolve_spec_placeholders_substitutes_inside_a_value_string() -> None:
    harness = _harness()
    raw = {"value": "{{NAMESPACE}}-web"}

    resolved = harness._resolve_spec_placeholders(raw, "prod-cluster", "web", "shop")

    assert resolved["value"] == "shop-web"


def test_resolve_spec_placeholders_leaves_booleans_untouched() -> None:
    harness = _harness()

    resolved_false = harness._resolve_spec_placeholders(
        {"value": False}, "prod-cluster", "web", "shop"
    )
    resolved_true = harness._resolve_spec_placeholders(
        {"value": True}, "prod-cluster", "web", "shop"
    )

    assert resolved_false["value"] is False
    assert resolved_true["value"] is True


def test_resolve_spec_placeholders_leaves_numbers_untouched() -> None:
    harness = _harness()
    raw = {"replicas": 3, "threshold": 0.5}

    resolved = harness._resolve_spec_placeholders(raw, "prod-cluster", "web", "shop")

    assert resolved["replicas"] == 3
    assert isinstance(resolved["replicas"], int)
    assert resolved["threshold"] == 0.5
    assert isinstance(resolved["threshold"], float)


def test_resolve_spec_placeholders_leaves_none_untouched() -> None:
    harness = _harness()

    resolved = harness._resolve_spec_placeholders({"value": None}, "prod-cluster", "web", "shop")

    assert resolved["value"] is None


def test_resolve_spec_placeholders_recurses_through_nested_entries() -> None:
    harness = _harness()
    raw = [
        {
            "name": "combo",
            "role": "objective",
            "check": {
                "type": "sequence",
                "checks": [
                    {"type": "pod_healthy", "namespace": "{{NAMESPACE}}"},
                    {
                        "type": "resource_property",
                        "selector": "app={{TARGET_DEPLOYMENT_NAME}}",
                    },
                ],
            },
        }
    ]

    resolved = harness._resolve_spec_placeholders(raw, "prod-cluster", "web", "shop")

    checks = resolved[0]["check"]["checks"]
    assert checks[0]["namespace"] == "shop"
    assert checks[1]["selector"] == "app=web"
