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

"""OutcomeValidity GEval metric and its checklist skill loading."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from deepeval.metrics import GEval
from deepeval.test_case import SingleTurnParams

from devops_bench.core import get_logger
from devops_bench.metrics._skills import load_skill_text
from devops_bench.metrics.base import (
    GEVAL_PASS_THRESHOLD,
    METRICS,
    MetricContext,
    MetricScore,
    run_geval,
)

__all__ = [
    "OUTCOME_SKILL_FILENAME",
    "OutcomeValidityMetric",
    "build_outcome_validity_metric",
    "load_outcome_criteria",
]

OUTCOME_SKILL_FILENAME = "outcome-validity-checklist.md"

# Appended to the criteria for generation-only tasks (``deployer: noop``), which
# provision no cluster. Without it the checklist's "Deployment Intent" /
# "Execution Confirmation" clauses wrongly fail a correct manifest for not being
# applied. None of the judge-visible test-case fields signal that this task is
# generation-only.
_GENERATION_ONLY_OVERRIDE = (
    "\n\n## Generation-Only Task Override\n"
    "This task is manifest-generation only: no live cluster is provisioned. "
    "Do NOT treat the absence of cluster application or execution confirmation "
    "as a failure. Criterion 1 follows the *Instructional Intent* path — a "
    "correct, complete manifest is full achievement — and Criterion 3 (Execution "
    "Confirmation) is skipped. Continue to grade manifest semantic integrity and "
    "every Expected Output requirement normally."
)

_log = get_logger("metrics.outcome_validity")


def load_outcome_criteria() -> str:
    """Read the outcome-validity checklist skill packaged with the project.

    Returns:
        The full markdown text used as the GEval criteria.

    Raises:
        FileNotFoundError: If the packaged skill file is missing.
    """
    return load_skill_text(OUTCOME_SKILL_FILENAME)


def build_outcome_validity_metric(model: Any, *, generation_only: bool = False) -> GEval:
    """Build the OutcomeValidity GEval metric.

    Args:
        model: A ``DeepEvalBaseLLM`` judge model.
        generation_only: When True (task provisions no cluster), append the
            generation-only override so the judge does not require cluster
            application/execution confirmation.

    Returns:
        A configured :class:`~deepeval.metrics.GEval` instance with the shared
        :data:`GEVAL_PASS_THRESHOLD` pass cutoff applied.
    """
    criteria = load_outcome_criteria()
    if generation_only:
        criteria += _GENERATION_ONLY_OVERRIDE
    return GEval(
        name="OutcomeValidity",
        criteria=criteria,
        threshold=GEVAL_PASS_THRESHOLD,
        evaluation_params=[
            SingleTurnParams.INPUT,
            SingleTurnParams.ACTUAL_OUTPUT,
            SingleTurnParams.EXPECTED_OUTPUT,
        ],
        model=model,
    )


@METRICS.register("outcome_validity")
class OutcomeValidityMetric:
    """Registered evaluator that scores OutcomeValidity for every result.

    Attributes:
        name: Score key written into ``res["scores"]``.
    """

    name = "OutcomeValidity"

    def applies(self, ctx: MetricContext) -> bool:
        """Always applies — every run is scored against outcome validity."""
        return True

    def evaluate(self, ctx: MetricContext) -> Iterable[MetricScore]:
        """Score the outcome-validity GEval against the outcome test case."""
        return run_geval(
            ctx.outcome_case,
            [build_outcome_validity_metric(ctx.judge, generation_only=ctx.generation_only)],
        )
