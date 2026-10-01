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

"""Tests for chaos-mode scoring."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from devops_bench.metrics import chaos_metrics
from devops_bench.metrics.base import MetricContext
from devops_bench.metrics.chaos_metrics import ChaosMetric, evaluate_chaos_metrics


def _chaos_result() -> SimpleNamespace:
    diag = SimpleNamespace(name="DiagnosisAccuracy [GEval]", score=5.0, success=True, reason="r")
    rec = SimpleNamespace(name="GracefulRecovery", score=4.0, success=True, reason="r")
    test_result = SimpleNamespace(metrics_data=[diag, rec])
    return SimpleNamespace(test_results=[test_result])


def test_chaos_records_geval_and_perf(mocker: MockerFixture) -> None:
    captured = {}

    def _fake_geval(**kwargs):
        captured.setdefault("names", []).append(kwargs["name"])
        captured["criteria"] = captured.get("criteria", []) + [kwargs["criteria"]]
        return MagicMock()

    mocker.patch.object(chaos_metrics, "GEval", side_effect=_fake_geval)
    mocker.patch("deepeval.evaluate", return_value=_chaos_result())
    scores: dict = {}

    evaluate_chaos_metrics(
        MagicMock(),
        MagicMock(),
        {"injected_fault": "node drain"},
        {
            "deployment_time_seconds": 12.0,
            "uptime_percentage": 99.5,
            "resource_utilization_efficiency": 0.8,
        },
        scores,
    )

    # GEval name suffix stripped on record (via shared run_geval).
    assert scores["DiagnosisAccuracy"]["score"] == 5.0
    assert scores["GracefulRecovery"]["success"] is True
    # Injected fault propagated into the diagnosis criteria.
    assert any("node drain" in c for c in captured["criteria"])
    # Performance numbers copied through verbatim.
    assert scores["Workload_Deployment_Time_Seconds"] == 12.0
    assert scores["Workload_Uptime_Percentage"] == 99.5
    assert scores["Resource_Utilization_Efficiency"] == 0.8


def test_chaos_skips_diagnosis_without_a_named_fault_and_survives_eval_error(
    mocker: MockerFixture,
) -> None:
    names: list[str] = []
    mocker.patch.object(
        chaos_metrics,
        "GEval",
        side_effect=lambda **kw: names.append(kw["name"]) or MagicMock(),
    )
    mocker.patch("deepeval.evaluate", side_effect=RuntimeError("judge down"))
    scores: dict = {}

    evaluate_chaos_metrics(MagicMock(), MagicMock(), {}, {}, scores)

    # No guessed fault: the agent is never judged on diagnosing a fault nobody named.
    assert names == ["GracefulRecovery"]
    # Eval failure swallowed; perf keys still populated (as None here).
    assert "Workload_Uptime_Percentage" in scores
    assert scores["Workload_Uptime_Percentage"] is None


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({}, False),
        ({"chaos_report": {"injected": True, "status": "success"}}, False),
        ({"chaos_spec": [{}], "chaos_report": {"injected": True, "status": "success"}}, True),
        ({"chaos_spec": [{}], "chaos_report": {"injected": True, "status": "timed_out"}}, True),
        ({"chaos_spec": [{}], "chaos_report": {"injected": False, "status": "failed"}}, False),
        ({"chaos_spec": [{}], "chaos_report": {"status": "success"}}, True),
        ({"chaos_spec": [{}], "chaos_report": {"status": "initiated"}}, False),
        ({"chaos_spec": [{}], "chaos_report": {}}, False),
        ({"chaos_spec": [{}]}, False),
    ],
    ids=[
        "no-chaos",
        "report-without-spec",
        "injected",
        "injected-then-drain-timed-out",
        "injection-failed",
        "legacy-success-status",
        "never-finished",
        "empty-report",
        "no-report",
    ],
)
def test_applies_only_when_declared_chaos_landed(result: dict, expected: bool) -> None:
    ctx = cast(MetricContext, SimpleNamespace(result=result))
    assert ChaosMetric().applies(ctx) is expected
