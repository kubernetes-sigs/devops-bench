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

"""Tests for the judged checklist metric.

What is pinned here is the line between "the agent failed" and "the judge could
not answer".
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from devops_bench.metrics import checklist as cl
from devops_bench.metrics.base import MetricScore

_EXPECTED = """critical requirements:
- alpha must hold
- beta must hold
- gamma must hold
"""


def _ctx() -> SimpleNamespace:
    """A context whose expected_output parses to three checklist items."""
    return SimpleNamespace(
        result={"expected_output": _EXPECTED},
        judge=None,
        use_mcp=False,
        outcome_case=None,
        tool_case=None,
        all_case=object(),
        generation_only=False,
    )


class _FakeGEval:
    """Stand-in for DeepEval's GEval: constructing the real one needs a key."""

    def __init__(self, name: str, **kwargs: object) -> None:
        self.name = name


def _stub_geval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cl, "GEval", _FakeGEval)


def _by_name(out: list[MetricScore]) -> dict[str, MetricScore]:
    return {ms.name: ms for ms in out}


def test_checklist_abstains_when_the_judge_evaluates_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing judged means a null aggregate and a null entry per item — not a zero, not silence."""
    _stub_geval(monkeypatch)

    def dead_judge(case: object, metrics: list[_FakeGEval]) -> list[MetricScore]:
        raise RuntimeError("404 models/gemini-3.1-pro is not found")

    monkeypatch.setattr(cl, "run_geval", dead_judge)

    scores = _by_name(list(cl.ChecklistMetric().evaluate(_ctx())))

    aggregate = scores["ChecklistScore"]
    assert aggregate.score is None and aggregate.success is None
    assert aggregate.reason == "None of 3 checks could be judged."
    items = [ms for name, ms in scores.items() if name.startswith("Check: ")]
    assert len(items) == 3
    assert all(ms.score is None and "404" in (ms.reason or "") for ms in items)


def test_checklist_scores_over_what_was_actually_judged(monkeypatch: pytest.MonkeyPatch) -> None:
    """A partial outage shrinks the denominator; the skipped item is recorded as null."""
    _stub_geval(monkeypatch)
    calls = {"n": 0}

    def flaky(case: object, metrics: list[_FakeGEval]) -> list[MetricScore]:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("judge blew up on this one")
        return [MetricScore(name=metrics[0].name, score=1.0, success=True)]

    monkeypatch.setattr(cl, "run_geval", flaky)

    scores = _by_name(list(cl.ChecklistMetric().evaluate(_ctx())))

    aggregate = scores["ChecklistScore"]
    assert aggregate.score == 1.0, "both judged items passed, so the ratio is over 2 not 3"
    assert aggregate.success is True
    assert aggregate.reason == "Passed 2 out of 2 evaluated checks (1 could not be judged)."
    skipped = scores["Check: beta must hold"]
    assert skipped.score is None and skipped.success is None
    assert "judge blew up" in (skipped.reason or "")


def test_a_fully_judged_checklist_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ordinary path keeps its previous meaning."""
    _stub_geval(monkeypatch)

    def judge(case: object, metrics: list[_FakeGEval]) -> list[MetricScore]:
        return [MetricScore(name=metrics[0].name, score=0.0, success=False)]

    monkeypatch.setattr(cl, "run_geval", judge)

    scores = _by_name(list(cl.ChecklistMetric().evaluate(_ctx())))

    assert scores["ChecklistScore"].score == 0.0
    assert scores["ChecklistScore"].reason == "Passed 0 out of 3 evaluated checks."
    assert all(ms.score == 0.0 for name, ms in scores.items() if name.startswith("Check: "))
