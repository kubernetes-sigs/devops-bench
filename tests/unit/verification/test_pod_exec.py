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

"""Unit tests for ``PodExecVerifier``.

``kubectl`` is stubbed at the module boundary; no pod is ever exec'd.
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from devops_bench.verification.verifiers import PodExecVerifier

_EXEC = "devops_bench.verification.verifiers.pod_exec.exec_pod"
_GET = "devops_bench.verification.verifiers.pod_exec.get_resource"


def _completed(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout)


def _pods(*names: str) -> dict[str, Any]:
    return {"items": [{"metadata": {"name": n}} for n in names]}


def _verifier(**kwargs: Any) -> PodExecVerifier:
    base = {"type": "pod_exec", "command": ["cat", "/etc/version"], "op": "eq"}
    base.update(kwargs)
    return PodExecVerifier(**base)


# -- shape --------------------------------------------------------------


def test_a_target_is_required() -> None:
    with pytest.raises(ValidationError, match="exactly one of"):
        _verifier(value="v2")


def test_naming_a_pod_and_a_selector_at_once_is_rejected() -> None:
    with pytest.raises(ValidationError, match="exactly one of"):
        _verifier(resource_name="prober", selector="app=prober", value="v2")


def test_a_value_op_requires_a_value() -> None:
    with pytest.raises(ValidationError, match="requires 'value'"):
        _verifier(resource_name="prober")


def test_an_empty_command_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _verifier(resource_name="prober", value="v2", command=[])


# -- behaviour ----------------------------------------------------------


def test_output_matching_the_expected_value_passes() -> None:
    with patch(_EXEC, return_value=_completed("v2\n")):
        result = _verifier(resource_name="prober", value="v2").verify(0.0)
    assert result.success is True
    assert result.status == "pass"
    assert result.raw == {"pod": "prober", "command": ["cat", "/etc/version"], "output": "v2"}


def test_surrounding_whitespace_is_not_part_of_the_comparison() -> None:
    # Nearly every command a probe runs ends in a newline; comparing against it
    # verbatim would make every eq check fail for a reason no task author means.
    with patch(_EXEC, return_value=_completed("  v2  \n")):
        result = _verifier(resource_name="prober", value="v2").verify(0.0)
    assert result.success is True


def test_output_differing_from_the_expected_value_fails() -> None:
    with patch(_EXEC, return_value=_completed("v1")):
        result = _verifier(resource_name="prober", value="v2").verify(0.0)
    assert result.success is False
    assert result.status == "fail"
    assert "prober" in result.reason


def test_contains_matches_a_substring_of_the_output() -> None:
    with patch(_EXEC, return_value=_completed("Server: nginx/1.27.1")):
        result = _verifier(resource_name="prober", op="contains", value="nginx/1.27").verify(0.0)
    assert result.success is True


def test_matches_applies_a_regex_to_the_output() -> None:
    with patch(_EXEC, return_value=_completed("HTTP/1.1 200 OK")):
        result = _verifier(resource_name="prober", op="matches", value=r"^HTTP/1\.1 2\d\d").verify(
            0.0
        )
    assert result.success is True


# -- target resolution --------------------------------------------------


def test_a_selector_resolves_to_the_first_pod_by_name() -> None:
    # Sorted rather than in apiserver order so a rerun probes the same pod and
    # the check does not flap between replicas.
    with (
        patch(_GET, return_value=_pods("prober-zz", "prober-aa")),
        patch(_EXEC, return_value=_completed("v2")) as mock_exec,
    ):
        result = _verifier(selector="app=prober", value="v2").verify(0.0)
    assert result.success is True
    assert mock_exec.call_args.args[0] == "prober-aa"


def test_a_selector_matching_nothing_is_an_error() -> None:
    with patch(_GET, return_value=_pods()):
        result = _verifier(selector="app=prober", value="v2").verify(0.0)
    assert result.success is False
    assert result.status == "error"
    assert "no pod matched" in result.reason


def test_naming_a_pod_skips_the_lookup_entirely() -> None:
    with patch(_GET) as mock_get, patch(_EXEC, return_value=_completed("v2")):
        _verifier(resource_name="prober", value="v2").verify(0.0)
    mock_get.assert_not_called()


# -- failure handling ---------------------------------------------------


def test_a_failed_pod_lookup_is_an_error_not_a_crash() -> None:
    with patch(_GET, side_effect=RuntimeError("connection refused")):
        result = _verifier(selector="app=prober", value="v2").verify(0.0)
    assert result.success is False
    assert result.status == "error"
    assert "connection refused" in result.reason


def test_a_failed_exec_is_an_error_not_a_crash() -> None:
    # The pod exists but the command could not run (no shell, container
    # restarting). That is a failure to observe, not an observed mismatch.
    with patch(_EXEC, side_effect=RuntimeError("container not running")):
        result = _verifier(resource_name="prober", value="v2").verify(0.0)
    assert result.success is False
    assert result.status == "error"
    assert "container not running" in result.reason


def test_the_container_is_threaded_through_to_kubectl() -> None:
    with patch(_EXEC, return_value=_completed("v2")) as mock_exec:
        _verifier(resource_name="prober", value="v2", container="sidecar", namespace="shop").verify(
            0.0
        )
    assert mock_exec.call_args.kwargs["container"] == "sidecar"
    assert mock_exec.call_args.kwargs["namespace"] == "shop"
