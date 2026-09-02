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

"""Turning CLI-transcript timestamps into the wall clock a run spent in tools."""

from __future__ import annotations

import datetime

__all__ = ["merged_span_sec", "parse_event_time"]


def parse_event_time(value: object) -> float | None:
    """Parse a transcript event timestamp into epoch seconds.

    Both CLI transcripts stamp events as ISO-8601 with a ``Z`` suffix
    (``2026-08-24T17:44:45.862Z``), which ``fromisoformat`` only accepts from
    Python 3.11 on; the explicit ``+00:00`` swap keeps the intent visible rather
    than resting on that.

    Args:
        value: The raw ``ts`` / ``timestamp`` field, or anything else.

    Returns:
        Epoch seconds, or ``None`` when the value is missing or unparseable. A
        transcript that drops the field is normal on older CLI versions, so this
        is a "no timing available" signal, not an error.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def merged_span_sec(intervals: list[tuple[float, float]]) -> float | None:
    """Return the wall-clock seconds covered by ``intervals``, overlaps counted once.

    Agents dispatch tool calls in batches: a single model turn can issue several
    calls stamped at the same millisecond that then run concurrently. Summing
    their durations would report more tool time than the run took in total, so
    overlapping intervals are merged before measuring.

    Args:
        intervals: ``(start, end)`` epoch-second pairs, in any order. Pairs whose
            end precedes their start are dropped as clock skew.

    Returns:
        Seconds of wall clock inside at least one interval, or ``None`` when no
        usable interval was supplied — distinct from ``0.0``, which means the
        tools ran and returned within the transcript's resolution.
    """
    usable = sorted((s, e) for s, e in intervals if e >= s)
    if not usable:
        return None
    total = 0.0
    cur_start, cur_end = usable[0]
    for start, end in usable[1:]:
        if start > cur_end:
            total += cur_end - cur_start
            cur_start, cur_end = start, end
        else:
            cur_end = max(cur_end, end)
    return total + (cur_end - cur_start)
