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

"""Unit tests for ``PodHealthyVerifier``.

The underlying ``kubectl`` calls are stubbed via ``unittest.mock.patch`` so the
verifier can be exercised without a real cluster.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from devops_bench.core import SubprocessError
from devops_bench.verification.verifiers import PodHealthyVerifier


def test_kubectl_wait_success_returns_raw_output() -> None:
    completed = SimpleNamespace(stdout="pod/web condition met\n")
    with patch(
        "devops_bench.verification.verifiers.pod_healthy.wait",
        return_value=completed,
    ):
        result = PodHealthyVerifier(selector="app=web").verify(timeout_sec=10)

    assert result.success is True
    assert result.raw == {"output": "pod/web condition met"}
    assert result.children == []


def test_polling_fallback_when_kubectl_wait_fails_and_pods_running() -> None:
    fake_pods = {
        "items": [
            {"status": {"phase": "Running"}},
            {"status": {"phase": "Running"}},
        ]
    }
    with (
        patch(
            "devops_bench.verification.verifiers.pod_healthy.wait",
            side_effect=SubprocessError(["kubectl"], returncode=1, stderr="timeout"),
        ),
        patch(
            "devops_bench.verification.verifiers.pod_healthy.get_resource",
            return_value=fake_pods,
        ),
    ):
        result = PodHealthyVerifier(selector="app=web").verify(timeout_sec=10)

    assert result.success is True
    assert "polling fallback" in result.reason
    assert result.raw == fake_pods


def test_fallback_reports_failure_when_no_pods_match() -> None:
    with (
        patch(
            "devops_bench.verification.verifiers.pod_healthy.wait",
            side_effect=SubprocessError(["kubectl"], returncode=1, stderr="no match"),
        ),
        patch(
            "devops_bench.verification.verifiers.pod_healthy.get_resource",
            return_value={"items": []},
        ),
    ):
        result = PodHealthyVerifier(selector="app=ghost").verify(timeout_sec=5)

    assert result.success is False
    assert "no match" in result.reason


def test_null_status_does_not_crash_phase_check() -> None:
    # A pod returned with an explicitly null ``status`` field would crash a
    # naive ``p["status"]["phase"]`` lookup. The verifier must null-guard.
    fake_pods = {
        "items": [
            {"status": None},
            {"status": {"phase": "Running"}},
        ]
    }
    with (
        patch(
            "devops_bench.verification.verifiers.pod_healthy.wait",
            side_effect=SubprocessError(["kubectl"], returncode=1, stderr="wait failed"),
        ),
        patch(
            "devops_bench.verification.verifiers.pod_healthy.get_resource",
            return_value=fake_pods,
        ),
    ):
        result = PodHealthyVerifier(selector="app=mixed").verify(timeout_sec=5)

    # Not all pods are Running, so it fails — but it must fail cleanly, not
    # raise on the null status.
    assert result.success is False
    assert result.raw == fake_pods


def test_elapsed_time_includes_fallback_fetch_duration() -> None:
    # A plain monotonic() mock can't distinguish call ordering, so drive a fake
    # clock that advances inside the fetch itself: elapsed_time must reflect
    # that advance, which only holds if it's computed after the fetch returns.
    clock = {"t": 100.0}

    def fake_monotonic() -> float:
        return clock["t"]

    def fake_get_resource(*args: object, **kwargs: object) -> dict:
        clock["t"] += 5.0
        return {"items": [{"status": {"phase": "Running"}}]}

    with (
        patch(
            "devops_bench.verification.verifiers.pod_healthy.wait",
            side_effect=SubprocessError(["kubectl"], returncode=1, stderr="timeout"),
        ),
        patch(
            "devops_bench.verification.verifiers.pod_healthy.get_resource",
            side_effect=fake_get_resource,
        ),
        patch(
            "devops_bench.verification.verifiers.pod_healthy.time.monotonic",
            side_effect=fake_monotonic,
        ),
    ):
        result = PodHealthyVerifier(selector="app=web").verify(timeout_sec=10)

    assert result.elapsed_time >= 5.0


def test_name_is_echoed_onto_result() -> None:
    completed = SimpleNamespace(stdout="ok")
    with patch(
        "devops_bench.verification.verifiers.pod_healthy.wait",
        return_value=completed,
    ):
        result = PodHealthyVerifier(name="web-pods", selector="app=web").verify(timeout_sec=10)

    assert result.name == "web-pods"
