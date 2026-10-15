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

"""Tests for the score-entry shape rule shared by every layer."""

from __future__ import annotations

from typing import Any

import pytest

from devops_bench.core import score_keys


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (0.5, 0.5),
        (0, 0.0),
        ({"score": 0.83, "success": True}, 0.83),
        ({"score": None, "reason": "withheld"}, None),
        (True, None),
        ({"score": False}, None),
        ("0.5", None),
        (None, None),
    ],
)
def test_score_value_reads_both_entry_shapes_and_never_a_bool(
    entry: Any, expected: float | None
) -> None:
    assert score_keys.score_value(entry) == expected
