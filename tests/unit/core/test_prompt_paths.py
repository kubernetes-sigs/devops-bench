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

"""Tests for the shared prompt-path helpers."""

from __future__ import annotations

import pytest

from devops_bench.core.prompt_paths import carries_cluster_token


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("cve-advisory-c1.json", True),
        ("c1_report.md", True),
        ("repo.c1.git", True),
        ("shared-report.md", False),
        ("c10-advisory.json", False),
        ("abc1-advisory.json", False),
    ],
)
def test_the_cluster_token_needs_a_delimiter_on_both_sides(name: str, expected: bool) -> None:
    assert carries_cluster_token(name, "c1") is expected


def test_no_cluster_name_means_nothing_carries_it() -> None:
    assert carries_cluster_token("cve-advisory-c1.json", None) is False
    assert carries_cluster_token("cve-advisory-c1.json", "") is False
