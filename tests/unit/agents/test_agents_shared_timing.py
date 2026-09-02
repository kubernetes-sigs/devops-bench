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

"""Tests for the shared CLI-transcript timing helpers."""

from __future__ import annotations

from devops_bench.agents.shared.timing import merged_span_sec, parse_event_time


def test_parse_event_time_reads_the_z_suffixed_stamps_both_clis_emit() -> None:
    """Both transcripts stamp events with a ``Z``, not ``+00:00``."""
    a = parse_event_time("2026-08-24T17:44:45.862Z")
    b = parse_event_time("2026-08-24T17:44:46.081Z")
    assert a is not None and b is not None
    assert round(b - a, 3) == 0.219


def test_parse_event_time_returns_none_for_anything_unusable() -> None:
    """An older CLI that omits the field means "no timing", not a crash."""
    assert parse_event_time(None) is None
    assert parse_event_time("") is None
    assert parse_event_time(1724521485) is None
    assert parse_event_time("not a timestamp") is None


def test_merged_span_sec_counts_concurrent_calls_once() -> None:
    """A batch of calls dispatched together did not take the sum of its parts.

    Seen live: one openclaw message issued two tool calls stamped at the same
    millisecond. Summing them would report more time inside tools than the run
    itself took.
    """
    assert merged_span_sec([(0.0, 2.0), (0.5, 1.5)]) == 2.0
    assert merged_span_sec([(0.0, 2.0), (1.0, 3.0)]) == 3.0


def test_merged_span_sec_adds_disjoint_calls() -> None:
    assert merged_span_sec([(0.0, 1.0), (5.0, 6.5)]) == 2.5


def test_merged_span_sec_is_order_independent() -> None:
    """Transcript order is emission order, which need not be start order."""
    assert merged_span_sec([(5.0, 6.0), (0.0, 1.0)]) == 2.0


def test_merged_span_sec_reports_none_when_nothing_was_timed() -> None:
    """No timings is not the same as zero time inside tools.

    ``0.0`` is a real reading — tools that returned within the transcript's
    resolution — so a run with no usable stamps must stay out of an average
    rather than pull it toward zero.
    """
    assert merged_span_sec([]) is None
    assert merged_span_sec([(2.0, 1.0)]) is None  # clock skew, dropped
    assert merged_span_sec([(1.0, 1.0)]) == 0.0
