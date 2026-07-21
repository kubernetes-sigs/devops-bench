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
from unittest.mock import MagicMock

from pytest_mock import MockerFixture

from devops_bench.metrics import chaos_metrics
from devops_bench.metrics.chaos_metrics import evaluate_chaos_metrics


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


def test_chaos_defaults_fault_and_survives_eval_error(mocker: MockerFixture) -> None:
    captured = {}
    mocker.patch.object(
        chaos_metrics,
        "GEval",
        side_effect=lambda **kw: (
            captured.setdefault("criteria", []).append(kw["criteria"]) or MagicMock()
        ),
    )
    mocker.patch("deepeval.evaluate", side_effect=RuntimeError("judge down"))
    scores: dict = {}

    evaluate_chaos_metrics(MagicMock(), MagicMock(), {}, {}, scores)

    # Default fault used when none reported.
    assert any("pod deletion" in c for c in captured["criteria"])
    # Eval failure swallowed; perf keys still populated (as None here).
    assert "Workload_Uptime_Percentage" in scores
    assert scores["Workload_Uptime_Percentage"] is None
