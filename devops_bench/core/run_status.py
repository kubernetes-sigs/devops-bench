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

"""When a run's end state cannot be attributed to the agent.

One definition, read by both the scoring layer (which withholds the composite)
and the row layer (which withholds the published correctness). Mirroring it
would let the leaderboard's correctness column disagree with the score beside
it, which is how a dead agent published a perfect 1.0 in the first place.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["UNSCOREABLE_RUN_STATUSES", "is_unscoreable_run"]

#: Statuses that say outright that the agent did not complete its turn.
#: Historical records use these; see :func:`is_unscoreable_run` for why the
#: status alone is not sufficient going forward.
UNSCOREABLE_RUN_STATUSES = frozenset({"agent_error"})


def is_unscoreable_run(record: Mapping[str, Any]) -> bool:
    """Whether ``record`` describes a run the agent never really performed.

    Two signals, because the status alone is not enough. Records produced by
    this lineage stamp ``status: "success"`` even when the agent call failed
    (a 429, an SDK fault, a timeout): the harness treats the *record* as
    successfully produced and marks the run un-``validated`` instead. So a
    status check alone would protect only artifacts from older builds while
    the next campaign reintroduced the same defect through a different door.

    The second signal is the one the harness itself uses for ``validated``:
    errors recorded and no trajectory at all means nothing the agent did can
    be read from the run, so whatever the cluster looks like now is not its
    doing. Errors *with* a trajectory are ordinary — an agent that hit a
    transient fault and carried on is still being graded on work it did.

    Args:
        record: One execution-result record.

    Returns:
        ``True`` when no composite score or published correctness may be
        derived from this record.
    """
    if str(record.get("status") or "") in UNSCOREABLE_RUN_STATUSES:
        return True
    return bool(record.get("errors")) and not record.get("trajectory")
