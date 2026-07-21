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

"""Documentation grounding metrics: constraint GEval scoring and retrieval rate."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from deepeval.metrics import GEval
from deepeval.test_case import LLMTestCase, SingleTurnParams

from devops_bench.core import get_logger
from devops_bench.metrics.base import (
    GEVAL_PASS_THRESHOLD,
    METRICS,
    MetricContext,
    MetricScore,
    run_geval,
)

__all__ = [
    "GroundingMetric",
    "calculate_doc_retrieval_rate",
    "evaluate_documentation_grounding",
]

_log = get_logger("metrics.grounding")


def calculate_doc_retrieval_rate(
    documentation: list[dict[str, Any]], trajectory: list[Any]
) -> float:
    """Compute the fraction of mapped documentation guides seen in a trajectory.

    A guide counts as accessed when its ``doc_name`` or ``url`` (case-insensitive)
    appears anywhere in the JSON-serialized trajectory steps.

    Args:
        documentation: Mapped guides, each with ``doc_name`` and ``url`` keys.
        trajectory: Agent execution steps (any JSON-serializable objects).

    Returns:
        The accessed fraction in ``[0.0, 1.0]``; ``0.0`` when there is no
        documentation.
    """
    if not documentation:
        return 0.0

    # Serialize each step once up front rather than re-serializing the whole
    # trajectory for every documentation guide (it does not depend on the doc).
    step_strs = [json.dumps(step).lower() for step in trajectory]

    accessed_docs = set()
    for doc in documentation:
        doc_name = doc.get("doc_name") or ""
        doc_name_lower = doc_name.lower()
        url_lower = (doc.get("url") or "").lower()
        found_in_trajectory = False
        for step_str in step_strs:
            # Guard both substrings on truthiness so a missing name/url (now "")
            # does not spuriously match every step (``"" in s`` is always True).
            if (doc_name_lower and doc_name_lower in step_str) or (
                url_lower and url_lower in step_str
            ):
                found_in_trajectory = True
                break
        if found_in_trajectory:
            accessed_docs.add(doc_name)

    return len(accessed_docs) / len(documentation)


def evaluate_documentation_grounding(
    documentation: list[dict[str, Any]],
    all_test_case: LLMTestCase,
    judge_model: Any,
    scores: dict[str, Any],
) -> None:
    """Score documentation constraints via GEval and derive GroundingAccuracy.

    Each documented constraint becomes a GEval metric evaluated against
    ``all_test_case``. Per-constraint results, an aggregate ``GroundingAccuracy``
    (5.0/2.5/0.0 banded by critical-constraint coverage), and
    ``ParameterRecallAccuracy`` are written into ``scores`` in place.

    Args:
        documentation: Guides, each with a ``constraints`` list of
            ``{"text": str, "critical": bool}`` entries.
        all_test_case: The combined test case (text output + trace) to judge.
        judge_model: A ``DeepEvalBaseLLM`` judge model.
        scores: Mutable scores dict updated in place.
    """
    # Deduplicate constraints by text first so two guides sharing the same
    # constraint produce a single metric; otherwise total > unique and a
    # perfect 5.0 (applied == total) becomes unreachable.
    doc_constraints_map: dict[str, bool] = {}
    for doc in documentation:
        for constraint in doc.get("constraints", []):
            doc_constraints_map[constraint["text"]] = constraint["critical"]

    doc_metrics = [
        GEval(
            name=f"Doc Constraint: {c_text}",
            criteria=(
                "Verify that the actual output fulfills this specific"
                f" documentation constraint/requirement: {c_text}"
            ),
            threshold=GEVAL_PASS_THRESHOLD,
            evaluation_params=[SingleTurnParams.ACTUAL_OUTPUT],
            model=judge_model,
        )
        for c_text in doc_constraints_map
    ]

    if not doc_metrics:
        return

    _log.info(
        "Evaluating %d documentation constraint metrics sequentially...",
        len(doc_metrics),
    )
    for m in doc_metrics:
        try:
            _log.info("Evaluating doc metric: %s...", m.name)
            for ms in run_geval(all_test_case, [m]):
                scores[ms.name] = ms.to_entry()
        except Exception as e:  # noqa: BLE001 - one bad metric must not abort scoring
            _log.error("Error evaluating doc metric %s: %s", m.name, e)

    total_constraints = len(doc_metrics)
    applied_constraints = 0
    critical_total = sum(1 for crit in doc_constraints_map.values() if crit)
    critical_applied = 0

    for c_text, c_crit in doc_constraints_map.items():
        m_name = f"Doc Constraint: {c_text}"
        entry = scores.get(m_name)
        # ``entry`` is the judged-shape dict; bail safely on a missing/malformed
        # entry (the per-constraint scoring above logged the failure).
        if isinstance(entry, dict) and entry.get("success"):
            applied_constraints += 1
            if c_crit:
                critical_applied += 1

    # Banded grounding score: 5.0 (Success), 2.5 (Partial), 0.0 (Failure). The
    # early return above guarantees ``total_constraints > 0`` and
    # ``applied <= total``, and any unmet critical constraint takes the Partial
    # branch before non-critical analysis runs, so ``non_critical_total`` is
    # never zero in the final branch.
    if applied_constraints == total_constraints:
        grounding_score = 5.0
    elif applied_constraints == 0:
        grounding_score = 0.0
    elif critical_applied < critical_total:
        grounding_score = 2.5
    else:
        non_critical_total = total_constraints - critical_total
        non_critical_applied = applied_constraints - critical_applied
        grounding_score = 2.5 + 2.5 * (non_critical_applied / non_critical_total)

    recall_accuracy = applied_constraints / total_constraints

    scores["GroundingAccuracy"] = {
        "score": grounding_score,
        "success": grounding_score >= 4.0,
        "reason": (
            f"Applied {applied_constraints} out of {total_constraints} documented"
            f" constraints (Critical: {critical_applied}/{critical_total})."
        ),
    }
    scores["ParameterRecallAccuracy"] = recall_accuracy


@METRICS.register("grounding")
class GroundingMetric:
    """Registered evaluator for documentation grounding + retrieval rate.

    Runs only when the result carries mapped ``documentation``. Yields
    per-constraint judged scores, the aggregate ``GroundingAccuracy``, plus the
    bare-value ``ParameterRecallAccuracy`` and ``DocRetrievalRate`` passthroughs.

    Attributes:
        name: Identifier for logging; per-score keys come from each yielded
            :class:`MetricScore`.
    """

    name = "grounding"

    def applies(self, ctx: MetricContext) -> bool:
        """Run only when the harness recorded mapped documentation."""
        return bool(ctx.result.get("documentation"))

    def evaluate(self, ctx: MetricContext) -> Iterable[MetricScore]:
        """Score grounding constraints and derive the doc-retrieval rate."""
        documentation = ctx.result.get("documentation", [])
        scores: dict[str, Any] = {}
        evaluate_documentation_grounding(documentation, ctx.all_case, ctx.judge, scores)
        retrieval_rate = calculate_doc_retrieval_rate(
            documentation, ctx.result.get("trajectory", [])
        )

        out: list[MetricScore] = []
        for name, entry in scores.items():
            if isinstance(entry, dict):
                out.append(
                    MetricScore(
                        name=name,
                        score=entry.get("score"),
                        success=entry.get("success"),
                        reason=entry.get("reason"),
                    )
                )
            else:
                # Bare-value entry (e.g. ParameterRecallAccuracy).
                out.append(MetricScore(name=name, score=entry))
        out.append(MetricScore(name="DocRetrievalRate", score=retrieval_rate))
        return out
