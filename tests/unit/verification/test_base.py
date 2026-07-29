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

"""Unit tests for the typed ``VerificationResult`` shape."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from devops_bench.verification import VerificationResult
from devops_bench.verification.base import single_call_timeout


def test_status_defaults_to_pass_when_success_is_true() -> None:
    result = VerificationResult(success=True, elapsed_time=0.0, reason="ok")
    assert result.status == "pass"


def test_status_defaults_to_fail_when_success_is_false() -> None:
    result = VerificationResult(success=False, elapsed_time=0.0, reason="nope")
    assert result.status == "fail"


def test_explicit_error_status_is_kept() -> None:
    result = VerificationResult(success=False, status="error", elapsed_time=0.0, reason="boom")
    assert result.status == "error"
    assert result.success is False


def test_status_pass_requires_success_true() -> None:
    with pytest.raises(ValidationError):
        VerificationResult(success=False, status="pass", elapsed_time=0.0, reason="mismatch")


def test_default_fields_match_contract():
    result = VerificationResult(success=True, elapsed_time=0.5, reason="ok")

    assert result.success is True
    assert result.elapsed_time == 0.5
    assert result.reason == "ok"
    assert result.name is None
    assert result.children == []
    assert result.raw is None


def test_compound_result_carries_children_not_raw():
    child = VerificationResult(success=True, elapsed_time=0.1, reason="leaf ok")
    compound = VerificationResult(
        success=True,
        elapsed_time=0.2,
        reason="all ok",
        name="group",
        children=[child],
    )

    assert compound.name == "group"
    assert compound.children == [child]
    assert compound.raw is None


def test_leaf_result_carries_raw_not_children():
    leaf = VerificationResult(
        success=False,
        elapsed_time=1.0,
        reason="kubectl failed",
        name="pods",
        raw={"items": []},
    )

    assert leaf.raw == {"items": []}
    assert leaf.children == []


# --- single_call_timeout (Change 4) -----------------------------------------


@pytest.mark.parametrize(
    ("timeout_sec", "expected"),
    [
        (0.0, 30.0),  # assert/zero path: floored for bounded I/O
        (0.5, 30.0),  # still below the runner's minimum effective leaf budget
        (5.0, 5.0),  # a real (if tight) converge budget: must not be raised
        (120.0, 120.0),  # a real, generous budget: unchanged
    ],
)
def test_single_call_timeout(timeout_sec: float, expected: float) -> None:
    assert single_call_timeout(timeout_sec) == expected


def test_no_details_field():
    # The legacy loose ``details`` field is replaced by ``children`` + ``raw``.
    fields = set(VerificationResult.model_fields.keys())

    assert "details" not in fields
    assert {"success", "status", "elapsed_time", "reason", "name", "children", "raw"} <= fields
