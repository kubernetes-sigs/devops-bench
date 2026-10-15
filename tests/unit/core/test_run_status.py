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

"""Tests for the shared "the agent never really ran" rule."""

from __future__ import annotations

from devops_bench.core import is_unscoreable_run


def test_an_explicit_agent_error_status_is_unscoreable() -> None:
    # The shape older builds produced; two such runs published a perfect 1.0.
    assert is_unscoreable_run({"status": "agent_error", "trajectory": [{"name": "x"}]}) is True


def test_errors_with_no_trajectory_are_unscoreable_whatever_the_status_says() -> None:
    # This lineage stamps status "success" even when the agent call failed, so
    # a status check alone would protect only historical artifacts while the
    # next campaign reintroduced the defect through a different door.
    record = {"status": "success", "errors": ["429 RESOURCE_EXHAUSTED"], "trajectory": []}
    assert is_unscoreable_run(record) is True


def test_errors_alongside_real_work_are_ordinary() -> None:
    # An agent that hit a transient fault and carried on is still being graded
    # on work it actually did.
    record = {"status": "success", "errors": ["one 429"], "trajectory": [{"name": "kubectl"}]}
    assert is_unscoreable_run(record) is False


def test_a_clean_run_is_scoreable() -> None:
    assert is_unscoreable_run({"status": "success", "trajectory": [{"name": "kubectl"}]}) is False
