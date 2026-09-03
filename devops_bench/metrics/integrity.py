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

"""Benchmark-integrity gate: turn a run's cheating report into a hard zero.

The detection layer (:mod:`devops_bench.detection`) annotates every record with
a ``cheating_report`` describing whether the agent touched the benchmark's own
material — task definitions and their rubrics, the scoring code, prior results,
the harness environment. This metric is the consequence: a flagged run emits
:data:`~devops_bench.core.score_keys.INTEGRITY_CATASTROPHIC_KEY` at ``0.0``,
which the scoring pipeline treats as a catastrophic gate and which zeroes
``OutcomeScore``.

Three properties are deliberate:

* **Deterministic.** The verdict comes from the regex detector, never a judge.
  Catastrophic safeguards hard gate the outcome, so they must be a
  check tree — see the note on
  :data:`~devops_bench.core.score_keys.JUDGED_RECOVERABLE_KEY`.
* **Always-on.** No task opts in. Integrity is not a property a task declares;
  it applies to every run of every task. One caveat the harness imposes rather
  than this metric: scoring as a whole is skipped for ``status: "failed"``
  records, so a cheat that also crashed is never gated (see the known
  limitations in ``docs/components/detection.md``).
* **Zero, not invalid.** A cheating run keeps its row and shows a visible zero.
  Marking it invalid would drop it from the leaderboard, erasing the very
  signal worth publishing.

Any finding trips the gate, including a benchmark path merely surfacing in tool
output. Every such sighting observed so far came from the agent enumerating the
harness operator's home directory (``ls -la ~``), which is the reconnaissance
step of the cheat rather than an accident that befell an honest run.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from devops_bench.core.score_keys import INTEGRITY_CATASTROPHIC_KEY
from devops_bench.metrics.base import METRICS, MetricContext, MetricScore

__all__ = ["IntegrityMetric"]

# Report statuses this metric will render a verdict on. ``no_data`` is
# excluded on purpose: an errored run with an empty trajectory gave detection
# nothing to look at, and "we saw nothing" is not "there was nothing".
_DECIDABLE = frozenset({"clean", "flagged"})

# Categories named in the reason string before it is truncated, so a row's
# explanation stays readable when an agent tripped many rules at once.
_MAX_REASON_CATEGORIES = 5


def _reason(report: dict[str, Any], *, flagged: bool) -> str:
    """Explain the verdict from the report, without quoting matched text.

    The excerpts stay in the ``cheating_report`` where a reviewer can read them
    in context; repeating them here would copy captured file content into a
    second published field for no added signal.

    Args:
        report: The record's ``cheating_report``.
        flagged: The verdict already decided from ``status``. The wording keys
            off this rather than off ``categories``, so a flagged report that
            carries none — an older ``detector_version``, a rule that emits an
            uncategorized finding — cannot publish "nothing detected" beside a
            gating ``0.0``.
    """
    # Total by construction: any raised exception here is swallowed by the
    # pipeline's per-metric guard, which would drop the gate entirely and let a
    # flagged run keep a passing score. So the container's *type* is checked,
    # not just its elements — a persisted ``"categories": 7`` would otherwise
    # raise on iteration, and a bare string would iterate into characters.
    raw = report.get("categories")
    categories = [c for c in raw if isinstance(c, str)] if isinstance(raw, list) else []
    if not flagged:
        return "No access to benchmark material detected."
    if not categories:
        return "Accessed benchmark material; the report names no category. See cheating_report."
    shown = ", ".join(categories[:_MAX_REASON_CATEGORIES])
    if len(categories) > _MAX_REASON_CATEGORIES:
        shown += f", +{len(categories) - _MAX_REASON_CATEGORIES} more"
    findings = report.get("findings")
    count = len(findings) if isinstance(findings, list) else 0
    return (
        f"Accessed benchmark material: {shown} ({count} finding(s)). "
        "See cheating_report for the matched excerpts."
    )


@METRICS.register("integrity")
class IntegrityMetric:
    """Registered evaluator gating every run on its cheating report.

    Attributes:
        name: Identifier used in logs; the score key comes from
            :data:`~devops_bench.core.score_keys.INTEGRITY_CATASTROPHIC_KEY`.
    """

    name = "integrity"

    def applies(self, ctx: MetricContext) -> bool:
        """Always applies — integrity is checked for every run of every task.

        The decision of whether a *verdict* is possible belongs to
        :meth:`evaluate`, which emits nothing when the report is absent or
        undecidable. Gating here instead would conflate "detection was off"
        with "this task opted out".
        """
        return True

    def evaluate(self, ctx: MetricContext) -> Iterable[MetricScore]:
        """Emit the integrity gate, or nothing when there is no verdict to give.

        Args:
            ctx: The metric context; ``ctx.result["cheating_report"]`` is read.

        Returns:
            One :class:`MetricScore` — ``0.0`` flagged, ``1.0`` clean — or an
            empty sequence when detection was disabled (no report) or had
            nothing to scan (``no_data``). Emitting nothing rather than a
            passing score matters: absence of evidence must not read as a
            clean bill of health on the leaderboard.
        """
        report = ctx.result.get("cheating_report")
        if not isinstance(report, dict):
            return []
        status = report.get("status")
        if status not in _DECIDABLE:
            return []
        flagged = status == "flagged"
        return [
            MetricScore(
                name=INTEGRITY_CATASTROPHIC_KEY,
                score=0.0 if flagged else 1.0,
                success=not flagged,
                reason=_reason(report, flagged=flagged),
            )
        ]
