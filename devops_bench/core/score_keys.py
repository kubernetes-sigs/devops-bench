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

__all__ = [
    "CATASTROPHIC_SCORE_KEYS",
    "CHECKLIST_SCORE_KEY",
    "INTEGRITY_CATASTROPHIC_KEY",
    "JUDGED_RECOVERABLE_KEY",
    "OUTCOME_SCORE_KEY",
    "OUTCOME_VALIDITY_KEY",
    "TOOL_INVOCATION_KEY",
    "VERIFICATION_CATASTROPHIC_KEY",
    "VERIFICATION_CORRECTNESS_KEY",
    "VERIFICATION_COVERAGE_KEY",
    "VERIFICATION_RECOVERABLE_KEY",
]

#: The v1 composite assembled from the sub-scores below; the leaderboard row's
#: ``outcomeScore`` reads this.
OUTCOME_SCORE_KEY = "OutcomeScore"

# --- deterministic signals, from a task's ``verification_spec`` ---------------
#: Weighted objective pass fraction, plus the two safeguard signals keyed by
#: severity. ``VERIFICATION_RECOVERABLE_KEY`` carries a **raw** fraction; the
#: ``[0.1, 1.0]`` rescale is applied by the scoring layer, not the emitter.
VERIFICATION_CORRECTNESS_KEY = "VerificationCorrectness"
VERIFICATION_RECOVERABLE_KEY = "VerificationRecoverable"
VERIFICATION_CATASTROPHIC_KEY = "VerificationCatastrophic"
VERIFICATION_COVERAGE_KEY = "VerificationCoverage"

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
