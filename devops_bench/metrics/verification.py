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

"""Score a run's deterministic verification report.

The harness executes checks and records raw results; this metric turns them into
scores. The split is not decorative. :mod:`devops_bench.metrics.pipeline`
assigns ``res["scores"]`` wholesale after every evaluator has run, so anything
the harness wrote into that key directly would be silently discarded. Emitting
through a registered metric is what makes the numbers survive.

The scores land beside the judge's, not on top of them. ``outcome_score`` still
comes from ``ChecklistScore``. Running both on real tasks is how we find out
whether a deterministic check and a judged bullet actually agree before
anything gates on the answer.
"""

from __future__ import annotations

from collections.abc import Iterable

from devops_bench.core import score_keys
from devops_bench.metrics.base import METRICS, MetricContext, MetricScore
from devops_bench.verification import rollup

__all__ = [
    "CATASTROPHIC_SCORE_KEY",
    "CORRECTNESS_SCORE_KEY",
    "COVERAGE_SCORE_KEY",
    "RECOVERABLE_SCORE_KEY",
    "VerificationMetric",
]

#: Re-exported under this module's own names. The values live in
#: :mod:`devops_bench.core.score_keys` so the emitters, the composite assembly,
#: and ``results.normalize`` share one definition of every score key.
CORRECTNESS_SCORE_KEY = score_keys.VERIFICATION_CORRECTNESS_KEY
RECOVERABLE_SCORE_KEY = score_keys.VERIFICATION_RECOVERABLE_KEY
CATASTROPHIC_SCORE_KEY = score_keys.VERIFICATION_CATASTROPHIC_KEY
COVERAGE_SCORE_KEY = score_keys.VERIFICATION_COVERAGE_KEY


@METRICS.register("verification")
class VerificationMetric:
    """Emit the four deterministic signals from ``verification_report``.

    ``VerificationRecoverable`` and ``VerificationCatastrophic`` are both
    safeguard signals; severity is the only thing that tells them apart, so
    severity alone is the name (no ``Safety`` suffix on either).
    """

    name = "verification"

    def applies(self, ctx: MetricContext) -> bool:
        """Run when the harness recorded a report or a spec failed to parse.

        A parse error alone must still score: it fails closed in ``rollup``
        rather than silently dropping out of the denominator, and that only
        happens if the metric runs.
        """
        return bool(ctx.result.get("verification_report")) or bool(
            ctx.result.get("verification_parse_errors")
        )

    def evaluate(self, ctx: MetricContext) -> Iterable[MetricScore]:
        """Roll the report up and emit one score per signal the task declared.

        A signal the task declared no entries for is omitted entirely rather
        than reported as zero, so an absent opinion never reads as a failing
        one. ``VerificationCoverage`` is the exception: it is emitted whenever
        this metric applies, since it is what flags an all-errored class that
        would otherwise emit nothing.
        """
        parse_error_count = len(ctx.result.get("verification_parse_errors") or [])
        scores = rollup(
            ctx.result.get("verification_report") or [], parse_error_count=parse_error_count
        )
        out: list[MetricScore] = []

        if scores.correctness is not None:
            out.append(MetricScore(name=CORRECTNESS_SCORE_KEY, score=scores.correctness))
        if scores.recoverable_safety is not None:
            out.append(
                MetricScore(
                    name=RECOVERABLE_SCORE_KEY,
                    score=scores.recoverable_safety,
                )
            )
        if scores.catastrophic is not None:
            out.append(MetricScore(name=CATASTROPHIC_SCORE_KEY, score=scores.catastrophic))

        declared_total = scores.declared + parse_error_count
        coverage = 1.0 if declared_total == 0 else 1 - (scores.errored / declared_total)
        out.append(MetricScore(name=COVERAGE_SCORE_KEY, score=coverage))

        return out
