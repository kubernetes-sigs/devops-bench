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

"""Canonical ``result["scores"]`` key names.

These strings are a contract between three layers that deliberately do not
import one another: the metric families emit them, the scoring pipeline reads
them to assemble the composite, and the results normalizer reads them to build
the flat leaderboard row. Declaring them here keeps one definition without
creating a ``metrics`` <-> ``results`` edge.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = [
    "CATASTROPHIC_SCORE_KEYS",
    "CHECKLIST_SCORE_KEY",
    "GATE_FAILED_CLOSED_PREFIX",
    "INTEGRITY_CATASTROPHIC_KEY",
    "JUDGED_RECOVERABLE_KEY",
    "OUTCOME_SCORE_KEY",
    "OUTCOME_VALIDITY_KEY",
    "TOOL_INVOCATION_KEY",
    "VERIFICATION_CATASTROPHIC_KEY",
    "VERIFICATION_CORRECTNESS_KEY",
    "VERIFICATION_COVERAGE_KEY",
    "VERIFICATION_RECOVERABLE_KEY",
    "failed_closed",
    "is_withheld",
    "parse_error_class",
    "score_value",
]

#: The v1 composite assembled from the sub-scores below; the leaderboard row's
#: ``outcomeScore`` reads this.
OUTCOME_SCORE_KEY = "OutcomeScore"

# --- deterministic signals, from a task's ``verification_spec`` ---------------
#: Weighted objective pass fraction, plus the two safeguard signals keyed by
#: severity. ``VERIFICATION_RECOVERABLE_KEY`` carries a **raw** fraction; the
#: ``[0.1, 1.0]`` rescale is applied by the scoring layer, not the emitter.
#: A signal the task declared but could not fully resolve is **withheld**: the
#: key is still written, with a ``null`` score and a reason, so a reader can
#: tell "not measured" from "not declared" (key absent). Both score chains stop
#: on a present-but-null key rather than falling through to a judged reading.
VERIFICATION_CORRECTNESS_KEY = "VerificationCorrectness"
VERIFICATION_RECOVERABLE_KEY = "VerificationRecoverable"
VERIFICATION_CATASTROPHIC_KEY = "VerificationCatastrophic"
VERIFICATION_COVERAGE_KEY = "VerificationCoverage"

#: Reason prefix on a ``VerificationCatastrophic`` zero that no safeguard
#: tripped: the gate failed closed because one could not be read.
GATE_FAILED_CLOSED_PREFIX = "Gate failed closed"

# --- judged signals, from prose checklists on the task ------------------------
#: Correctness, and its fallback for tasks that author no checklist.
CHECKLIST_SCORE_KEY = "ChecklistScore"
OUTCOME_VALIDITY_KEY = "OutcomeValidity"
#: Judge-scored recoverable safeguards, also a raw fraction. Catastrophic
#: safeguards have no judged form: they hard gate the outcome, so they must be a
#: deterministic check tree. :data:`INTEGRITY_CATASTROPHIC_KEY` is a second
#: source of that gate and obeys the same rule — it reads a regex detector's
#: verdict, never a judge's.
JUDGED_RECOVERABLE_KEY = "JudgedRecoverable"

# --- integrity signals, applied to every task ---------------------------------
#: Benchmark-integrity gate. ``0.0`` when the run's ``cheating_report`` flagged
#: access to the benchmark's own material. Deliberately distinct from
#: :data:`VERIFICATION_CATASTROPHIC_KEY` rather than reusing it: the scores map
#: is last-write-wins, so a clean integrity check sharing that key would erase a
#: real task catastrophic. Keeping them separate also means the key name *is*
#: the failure type, which is what lets a row report which gate fired.
INTEGRITY_CATASTROPHIC_KEY = "IntegrityCatastrophic"

#: Every key that hard gates the outcome. Not a preference chain: *any* of them
#: at ``0.0`` forces ``cat_v = 0``. Shared rather than mirrored per layer,
#: unlike the correctness and recoverable chains each consumer keeps locally.
#: The asymmetry is deliberate — drift in those chains only means a row reports
#: a slightly different component than the composite used, while drift here
#: means the row's ``catastrophic`` flag contradicts the zero the pipeline
#: already applied to ``outcomeScore``, so the agreement has to be structural.
CATASTROPHIC_SCORE_KEYS: tuple[str, ...] = (
    VERIFICATION_CATASTROPHIC_KEY,
    INTEGRITY_CATASTROPHIC_KEY,
)

#: Tool-invocation score, carried on the row beside the composite.
TOOL_INVOCATION_KEY = "ToolInvocation"


def score_value(entry: Any) -> float | None:
    """The number in a scores entry (bare, or under ``"score"``); a bool or anything else is ``None``."""
    if isinstance(entry, Mapping):
        entry = entry.get("score")
    if isinstance(entry, bool):
        return None
    return float(entry) if isinstance(entry, (int, float)) else None


def is_withheld(entry: Any) -> bool:
    """A present key with a ``null`` score was withheld: declared, but not measured."""
    return entry is None or (isinstance(entry, Mapping) and entry.get("score") is None)


def failed_closed(entry: Any) -> bool:
    """A gate entry that scored zero because a safeguard was unread, not tripped."""
    reason = entry.get("reason") if isinstance(entry, Mapping) else None
    return isinstance(reason, str) and reason.startswith(GATE_FAILED_CLOSED_PREFIX)


def parse_error_class(err: Any) -> str:
    """Route a ``verification_parse_errors`` item to the signal it withholds or gates.

    Returns ``"objective"``, ``"recoverable"`` or ``"catastrophic"``. A
    safeguard without a valid severity is catastrophic, so an unreadable
    tripwire fails closed; anything else unusable is an objective. Shared so
    the rollup and the row route an entry alike.
    """
    role = err.get("role") if isinstance(err, Mapping) else None
    if role != "safeguard":
        return "objective"
    return "recoverable" if err.get("severity") == "recoverable" else "catastrophic"
