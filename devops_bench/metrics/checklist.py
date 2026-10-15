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

"""Per-requirement checklist metric and its expected-output parser.

Scores each ``-`` bulleted "Critical Requirements" item from a task's expected
output with its own GEval and emits the aggregate ``ChecklistScore``. Registered
under the ``checklist`` key; the batch pipeline picks it up via :data:`METRICS`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from deepeval.metrics import GEval
from deepeval.test_case import SingleTurnParams

from devops_bench.core import get_logger
from devops_bench.metrics.base import (
    GEVAL_PASS_THRESHOLD,
    METRICS,
    MetricContext,
    MetricScore,
    not_judged,
    run_geval,
    withheld_aggregate,
)

__all__ = [
    "CHECKLIST_THRESHOLD",
    "ChecklistMetric",
    "extract_checklist_items",
]

_log = get_logger("metrics.checklist")

# Per-task checklist pass cutoff.
CHECKLIST_THRESHOLD = 0.8


def extract_checklist_items(expected_output: str, use_mcp: bool) -> list[str]:
    """Parse per-requirement checklist items from an expected-output string.

    Items are the ``-`` bulleted lines inside the "Critical Requirements" section
    (everything before an "Expected Manifest Generated" marker). When MCP is
    disabled, "expected tool call" requirements are dropped since the agent has
    no tools to invoke.

    Args:
        expected_output: The task's expected-output text.
        use_mcp: Whether the run used MCP tools.

    Returns:
        The cleaned list of requirement strings (bullet markers stripped).
    """
    reqs_section = expected_output
    if "critical requirements:" in reqs_section.lower():
        parts = re.split(r"(?i)critical requirements\s*:", reqs_section, maxsplit=1)
        if len(parts) > 1:
            reqs_section = parts[1]

    if "expected manifest generated:" in reqs_section.lower():
        parts = re.split(r"(?i)expected manifest generated\s*:", reqs_section, maxsplit=1)
        reqs_section = parts[0]

    # Strip a single leading "-" bullet marker (and the spaces after it). A
    # character-class strip like ``lstrip("- ")`` would also eat a leading flag
    # ("- --dry-run" -> "dry-run") or a trailing hyphen ("...staging-").
    raw_checklist_items = [
        re.sub(r"^-\s*", "", stripped)
        for line in reqs_section.split("\n")
        if (stripped := line.strip()).startswith("-")
    ]
    checklist_items = []
    for item in raw_checklist_items:
        if not use_mcp and "expected tool call" in item.lower():
            _log.info("Skipping Expected Tool Call criteria: '%s'", item)
            continue
        checklist_items.append(item)
    return checklist_items


@METRICS.register("checklist")
class ChecklistMetric:
    """Registered evaluator scoring per-requirement checklist items.

    For each parsed checklist item the evaluator builds a per-item ``Check: …``
    GEval, scores it, and emits the aggregate ``ChecklistScore`` using
    :data:`CHECKLIST_THRESHOLD` as the pass cutoff. An item the judge could not
    evaluate is recorded with a null score and withholds the aggregate.

    Attributes:
        name: Identifier for logging; per-score keys come from each yielded
            :class:`MetricScore`.
    """

    name = "checklist"
    needs_judge = True

    def applies(self, ctx: MetricContext) -> bool:
        """Run only when the result's ``expected_output`` carries bullets."""
        return bool(extract_checklist_items(ctx.result.get("expected_output", ""), ctx.use_mcp))

    def evaluate(self, ctx: MetricContext) -> Iterable[MetricScore]:
        """Score each requirement and emit the aggregate ChecklistScore."""
        items = extract_checklist_items(ctx.result.get("expected_output", ""), ctx.use_mcp)
        if not items:
            return []
        dynamic_metrics = [
            GEval(
                name=f"Check: {item}",
                criteria=(
                    f"Verify that the actual output fulfills this specific requirement: {item}"
                ),
                threshold=GEVAL_PASS_THRESHOLD,
                evaluation_params=[SingleTurnParams.ACTUAL_OUTPUT],
                model=ctx.judge,
            )
            for item in items
        ]

        out: list[MetricScore] = []
        passed = 0
        judged = 0
        total = len(dynamic_metrics)
        for m in dynamic_metrics:
            _log.info("Evaluating metric: %s...", m.name)
            try:
                entries = run_geval(ctx.all_case, [m])
            except Exception as e:  # noqa: BLE001 - keep scoring the rest
                _log.error("Error evaluating metric %s: %s", m.name, e)
                # Null score: the record shows the item was skipped, not failed.
                out.append(MetricScore(name=m.name, score=None, reason=not_judged(e)))
                continue
            out.extend(entries)
            judged += bool(entries)  # no verdict back is unjudged, not a pass
            passed += sum(1 for ms in entries if ms.success)

        withheld = withheld_aggregate(
            "ChecklistScore", passed=passed, judged=judged, total=total, noun="checks"
        )
        if withheld:
            out.append(withheld)
        else:
            ratio = passed / total
            out.append(
                MetricScore(
                    name="ChecklistScore",
                    score=ratio,
                    success=ratio >= CHECKLIST_THRESHOLD,
                    reason=f"Passed {passed} out of {total} checks.",
                )
            )
        return out
