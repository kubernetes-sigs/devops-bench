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

"""Tests for documentation grounding metrics."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from devops_bench.metrics import grounding
from devops_bench.metrics.base import GEVAL_PASS_THRESHOLD
from devops_bench.metrics.grounding import (
    calculate_doc_retrieval_rate,
    evaluate_documentation_grounding,
)

# --- calculate_doc_retrieval_rate (pure logic) --------------------------------


def test_retrieval_rate_empty_docs():
    assert calculate_doc_retrieval_rate([], [{"step": 1}]) == 0.0


def test_retrieval_rate_matches_by_name():
    docs = [{"doc_name": "GuideA", "url": "http://a"}]
    trajectory = [{"action": "read guidea docs"}]
    assert calculate_doc_retrieval_rate(docs, trajectory) == 1.0


def test_retrieval_rate_matches_by_url():
    docs = [{"doc_name": "GuideA", "url": "http://example.com/a"}]
    trajectory = [{"href": "see http://example.com/a now"}]
    assert calculate_doc_retrieval_rate(docs, trajectory) == 1.0


def test_retrieval_rate_partial():
    docs = [
        {"doc_name": "GuideA", "url": "http://a"},
        {"doc_name": "GuideB", "url": "http://b"},
    ]
    trajectory = [{"action": "read guidea"}]
    assert calculate_doc_retrieval_rate(docs, trajectory) == 0.5


def test_retrieval_rate_no_match():
    docs = [{"doc_name": "GuideA", "url": "http://a"}]
    assert calculate_doc_retrieval_rate(docs, [{"action": "nothing here"}]) == 0.0


def test_retrieval_rate_tolerates_missing_name_and_url():
    docs = [
        {"url": "http://a"},  # no doc_name
        {"doc_name": None, "url": None},  # both None
        {"doc_name": "GuideC", "url": "http://c"},
    ]
    trajectory = [{"action": "read guidec via http://c"}]
    assert calculate_doc_retrieval_rate(docs, trajectory) == pytest.approx(1 / 3)


# --- evaluate_documentation_grounding (GEval mocked) --------------------------


def _metric_result(name, score, success, reason="ok"):
    metric_data = SimpleNamespace(name=name, score=score, success=success, reason=reason)
    test_result = SimpleNamespace(metrics_data=[metric_data])
    return SimpleNamespace(test_results=[test_result])


def _named_geval(mocker):
    """Patch grounding.GEval so each metric carries its real ``name``."""
    return mocker.patch.object(
        grounding, "GEval", side_effect=lambda **kw: SimpleNamespace(name=kw["name"])
    )


def _evaluate_by_outcome(mocker, outcomes):
    """Order-agnostic ``deepeval.evaluate`` keyed off the metric name.

    ``outcomes`` maps a metric name (as built by the grounding code, e.g.
    ``"Doc Constraint: use TLS"``) to a ``(score, success)`` pair. The reported
    name carries DeepEval's ``" [GEval]"`` suffix so the strip path is exercised.
    """

    def _side_effect(test_cases, metrics):
        name = metrics[0].name
        score, success = outcomes[name]
        return _metric_result(f"{name} [GEval]", score, success)

    return mocker.patch("deepeval.evaluate", side_effect=_side_effect)


def test_grounding_constraint_geval_uses_explicit_pass_threshold(mocker):
    # Per-constraint GEvals must pass the explicit 0.8 cutoff rather than
    # inheriting deepeval's looser 0.5 default, since their success flags drive
    # GroundingAccuracy / ParameterRecallAccuracy.
    geval_cls = _named_geval(mocker)
    mocker.patch("deepeval.evaluate", return_value=SimpleNamespace(test_results=[]))
    docs = [{"constraints": [{"text": "use TLS", "critical": True}]}]

    evaluate_documentation_grounding(docs, MagicMock(), MagicMock(), {})

    assert geval_cls.call_args.kwargs["threshold"] == GEVAL_PASS_THRESHOLD


def test_grounding_no_constraints_returns_early(mocker):
    evaluate = mocker.patch("deepeval.evaluate")
    scores: dict = {}

    evaluate_documentation_grounding([{"constraints": []}], MagicMock(), MagicMock(), scores)

    evaluate.assert_not_called()
    assert scores == {}


def test_grounding_all_applied_scores_full(mocker):
    _named_geval(mocker)
    docs = [
        {
            "constraints": [
                {"text": "use TLS", "critical": True},
                {"text": "set replicas", "critical": False},
            ]
        }
    ]
    scores: dict = {}
    _evaluate_by_outcome(
        mocker,
        {
            "Doc Constraint: use TLS": (5.0, True),
            "Doc Constraint: set replicas": (5.0, True),
        },
    )

    evaluate_documentation_grounding(docs, MagicMock(), MagicMock(), scores)

    assert scores["GroundingAccuracy"]["score"] == 5.0
    assert scores["GroundingAccuracy"]["success"] is True
    assert scores["ParameterRecallAccuracy"] == 1.0
    # Per-constraint keys recorded with the [GEval] suffix stripped.
    assert scores["Doc Constraint: use TLS"]["success"] is True


def test_grounding_critical_missing_scores_partial(mocker):
    _named_geval(mocker)
    docs = [
        {
            "constraints": [
                {"text": "use TLS", "critical": True},
                {"text": "set replicas", "critical": False},
            ]
        }
    ]
    scores: dict = {}
    _evaluate_by_outcome(
        mocker,
        {
            "Doc Constraint: use TLS": (0.0, False),
            "Doc Constraint: set replicas": (5.0, True),
        },
    )

    evaluate_documentation_grounding(docs, MagicMock(), MagicMock(), scores)

    # Critical applied (0) < critical total (1) => partial 2.5.
    assert scores["GroundingAccuracy"]["score"] == 2.5
    assert scores["GroundingAccuracy"]["success"] is False
    assert scores["ParameterRecallAccuracy"] == 0.5


def test_grounding_none_applied_scores_zero(mocker):
    _named_geval(mocker)
    docs = [{"constraints": [{"text": "use TLS", "critical": True}]}]
    scores: dict = {}
    _evaluate_by_outcome(mocker, {"Doc Constraint: use TLS": (0.0, False)})

    evaluate_documentation_grounding(docs, MagicMock(), MagicMock(), scores)

    assert scores["GroundingAccuracy"]["score"] == 0.0
    assert scores["ParameterRecallAccuracy"] == 0.0


def test_grounding_dedups_shared_constraint_text(mocker):
    # Two guides share the same constraint text; it must collapse to one metric
    # so a perfect 5.0 (applied == unique total) is reachable.
    _named_geval(mocker)
    docs = [
        {"constraints": [{"text": "use TLS", "critical": True}]},
        {"constraints": [{"text": "use TLS", "critical": True}]},
    ]
    scores: dict = {}
    evaluate = _evaluate_by_outcome(mocker, {"Doc Constraint: use TLS": (5.0, True)})

    evaluate_documentation_grounding(docs, MagicMock(), MagicMock(), scores)

    # Deduped: a single metric evaluated once, and the perfect score is reachable.
    assert evaluate.call_count == 1
    assert scores["GroundingAccuracy"]["score"] == 5.0
    assert scores["GroundingAccuracy"]["success"] is True
    assert scores["ParameterRecallAccuracy"] == 1.0
