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

"""Unit tests for ``ScalingCompleteVerifier``.

The poll function and kubectl primitives are stubbed; no real cluster work.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pydantic import ValidationError

from devops_bench.core import SubprocessError
from devops_bench.verification.verifiers import ScalingCompleteVerifier


def test_success_when_ready_replicas_meet_minimum() -> None:
    deployment = {"status": {"readyReplicas": 3}}
    with patch(
        "devops_bench.verification.verifiers.scaling_complete.get_resource",
        return_value=deployment,
    ):
        result = ScalingCompleteVerifier(deployment="web", min_replicas=2).verify(timeout_sec=5)

    assert result.success is True
    assert "Ready replicas (3) >= min replicas (2)" in result.reason
    assert result.raw["deployment"] == deployment


def test_failure_when_ready_replicas_below_minimum() -> None:
    # The poll runs once with a zero timeout, returns False, and we report the
    # last observed reason — replicas are below the threshold.
    deployment = {"status": {"readyReplicas": 1}}
    with patch(
        "devops_bench.verification.verifiers.scaling_complete.get_resource",
        return_value=deployment,
    ):
        result = ScalingCompleteVerifier(deployment="web", min_replicas=3).verify(timeout_sec=0)

    assert result.success is False
    assert "Ready replicas (1) < min replicas (3)" in result.reason


def test_null_status_does_not_crash_check() -> None:
    # ``status`` may be explicitly null before the deployment controller
    # populates it; the verifier must treat ready replicas as 0.
    deployment = {"status": None}
    with patch(
        "devops_bench.verification.verifiers.scaling_complete.get_resource",
        return_value=deployment,
    ):
        result = ScalingCompleteVerifier(deployment="web", min_replicas=1).verify(timeout_sec=0)

    assert result.success is False
    assert result.status == "fail"
    assert "Ready replicas (0) < min replicas (1)" in result.reason


def test_explicit_null_ready_replicas_does_not_crash_check() -> None:
    # ``status.readyReplicas`` may itself be explicitly null (present but
    # unset) rather than the key being absent; ``.get(..., 0)`` only covers
    # the latter and returns None for the former, which then blows up the
    # numeric comparisons below with a TypeError.
    deployment = {"status": {"readyReplicas": None}}
    with patch(
        "devops_bench.verification.verifiers.scaling_complete.get_resource",
        return_value=deployment,
    ):
        result = ScalingCompleteVerifier(deployment="web", min_replicas=1).verify(timeout_sec=0)

    assert result.success is False
    assert result.status == "fail"
    assert "Ready replicas (0) < min replicas (1)" in result.reason


def test_subprocess_error_is_reported_in_reason() -> None:
    with patch(
        "devops_bench.verification.verifiers.scaling_complete.get_resource",
        side_effect=SubprocessError(["kubectl"], returncode=1, stderr="not found"),
    ):
        result = ScalingCompleteVerifier(deployment="web", min_replicas=1).verify(timeout_sec=0)

    assert result.success is False
    assert result.status == "error"
    assert "Failed to get deployment" in result.reason


def test_success_when_ready_replicas_within_bounds() -> None:
    # With both bounds set, a count inside [min, max] succeeds — the scale-down /
    # optimization case where the deployment must shrink to a ceiling.
    deployment = {"status": {"readyReplicas": 2}}
    with patch(
        "devops_bench.verification.verifiers.scaling_complete.get_resource",
        return_value=deployment,
    ):
        result = ScalingCompleteVerifier(deployment="web", min_replicas=1, max_replicas=3).verify(
            timeout_sec=5
        )

    assert result.success is True
    assert "within bounds [1, 3]" in result.reason


def test_failure_when_ready_replicas_above_maximum() -> None:
    deployment = {"status": {"readyReplicas": 5}}
    with patch(
        "devops_bench.verification.verifiers.scaling_complete.get_resource",
        return_value=deployment,
    ):
        result = ScalingCompleteVerifier(deployment="web", min_replicas=1, max_replicas=3).verify(
            timeout_sec=0
        )

    assert result.success is False
    assert "Ready replicas (5) > max replicas (3)" in result.reason


def test_negative_min_replicas_raises() -> None:
    with pytest.raises(ValidationError, match="min_replicas must be >= 0"):
        ScalingCompleteVerifier(deployment="web", min_replicas=-1)


def test_negative_max_replicas_raises() -> None:
    with pytest.raises(ValidationError, match="max_replicas must be >= 0"):
        ScalingCompleteVerifier(deployment="web", min_replicas=0, max_replicas=-1)


def test_min_greater_than_max_raises() -> None:
    with pytest.raises(ValidationError, match="must be <="):
        ScalingCompleteVerifier(deployment="web", min_replicas=5, max_replicas=3)


@pytest.mark.parametrize(
    ("timeout_sec", "expected_timeout"), [(0.0, 30.0), (5.0, 5.0), (60.0, 60.0)]
)
def test_get_resource_is_called_with_a_floored_timeout(
    timeout_sec: float, expected_timeout: float
) -> None:
    # An assert-mode single_shot call passes timeout_sec=0.0, which as a
    # literal ``kubectl get`` subprocess timeout would mean "give up
    # immediately"; the floor is what actually bounds the underlying call.
    deployment = {"status": {"readyReplicas": 3}}
    with patch(
        "devops_bench.verification.verifiers.scaling_complete.get_resource",
        return_value=deployment,
    ) as mock_get:
        ScalingCompleteVerifier(deployment="web", min_replicas=1).verify(timeout_sec=timeout_sec)

    assert mock_get.call_args.kwargs["timeout"] == expected_timeout


def test_name_is_echoed_onto_result() -> None:
    deployment = {"status": {"readyReplicas": 5}}
    with patch(
        "devops_bench.verification.verifiers.scaling_complete.get_resource",
        return_value=deployment,
    ):
        result = ScalingCompleteVerifier(
            name="scale-to-two", deployment="web", min_replicas=2
        ).verify(timeout_sec=5)

    assert result.name == "scale-to-two"


def test_a_deployment_that_does_not_exist_is_a_fail_not_an_error() -> None:
    # A deployment the apiserver reports as NotFound has observably not scaled,
    # so it must count against correctness rather than dropping out of the
    # denominator the way an unobserved check does.
    exc = SubprocessError(
        ["kubectl", "get", "deployment", "web"],
        returncode=1,
        stderr='Error from server (NotFound): deployments.apps "web" not found',
    )
    with patch(
        "devops_bench.verification.verifiers.scaling_complete.get_resource",
        side_effect=exc,
    ):
        result = ScalingCompleteVerifier(deployment="web", min_replicas=1).verify(timeout_sec=0)

    assert result.status == "fail"
    assert result.success is False
    assert "not found" in result.reason
