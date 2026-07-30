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
    "CATASTROPHIC_KEY",
    "CHECKLIST_SCORE_KEY",
    "OUTCOME_SCORE_KEY",
    "OUTCOME_VALIDITY_KEY",
    "RECOVERABLE_SAFETY_KEY",
    "TOOL_INVOCATION_KEY",
]

#: The v1 composite assembled from the sub-scores below; the leaderboard row's
#: ``outcomeScore`` reads this.
OUTCOME_SCORE_KEY = "OutcomeScore"

#: Correctness, and its fallback for tasks that author no checklist.
CHECKLIST_SCORE_KEY = "ChecklistScore"
OUTCOME_VALIDITY_KEY = "OutcomeValidity"

#: Safety sub-scores. ``RECOVERABLE_SAFETY_KEY`` carries ``rec_v`` already
#: rescaled onto ``[0.1, 1.0]``; ``CATASTROPHIC_KEY`` carries the ``cat_v``
#: gate, ``0.0`` when a tripwire fired and ``1.0`` otherwise.
RECOVERABLE_SAFETY_KEY = "RecoverableSafety"
CATASTROPHIC_KEY = "Catastrophic"

#: Tool-invocation score, carried on the row beside the composite.
TOOL_INVOCATION_KEY = "ToolInvocation"
