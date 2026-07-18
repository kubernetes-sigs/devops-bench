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

"""Unit tests for the chaos base abstractions (``ChaosResult``, ABCs)."""

from __future__ import annotations

import pytest

from devops_bench.chaos.base import ChaosResult, Fault, Trigger


def test_chaos_result_defaults_are_minimal() -> None:
    res = ChaosResult(success=True, injected_fault="generate_load")
    assert res.success is True
    assert res.injected_fault == "generate_load"
    assert res.output == ""
    assert res.elapsed_time == 0.0
    assert res.error is None


def test_chaos_result_with_failure_carries_error_string() -> None:
    res = ChaosResult(
        success=False,
        injected_fault="generate_load",
        elapsed_time=1.5,
        error="RuntimeError: provider blew up",
    )
    assert res.success is False
    assert res.elapsed_time == 1.5
    assert res.error.startswith("RuntimeError")


def test_chaos_result_serialises_round_trip() -> None:
    res = ChaosResult(success=True, injected_fault="x", output="ok", elapsed_time=0.25)
    again = ChaosResult.model_validate(res.model_dump())
    assert again == res


def test_fault_is_abstract_and_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        Fault()  # type: ignore[abstract]


def test_trigger_is_abstract_and_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        Trigger()  # type: ignore[abstract]
