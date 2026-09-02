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

"""Tests for the task-supplied-argv cloud_resource_property verifier.

The subprocess runner is patched at the verifier module; no real gcloud,
no network. Argv assembly, the read-only verb guard, stderr
classification, and evaluation wiring are each covered separately.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from devops_bench.verification.verifiers.cloud_resource_property import (
    CloudResourcePropertyVerifier,
)

_MODULE = "devops_bench.verification.verifiers.cloud_resource_property"


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["gcloud"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _patched_run(completed: subprocess.CompletedProcess):
    return patch(f"{_MODULE}.run", return_value=completed)


@pytest.fixture(autouse=True)
def _no_ambient_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)


# --- argv assembly -----------------------------------------------------------


def test_appends_binary_and_format_json() -> None:
    with _patched_run(_completed(stdout=json.dumps({"name": "s"}))) as mock_run:
        CloudResourcePropertyVerifier(
            args=["compute", "networks", "subnets", "describe", "s", "--region", "us-central1"],
            path="name",
            op="eq",
            value="s",
        ).verify(timeout_sec=0)

    argv = mock_run.call_args.args[0]
    assert argv[0] == "gcloud"
    assert argv[1:8] == [
        "compute",
        "networks",
        "subnets",
        "describe",
        "s",
        "--region",
        "us-central1",
    ]
    assert argv[-1] == "--format=json"
    assert "--project" not in argv


def test_injects_project_from_env_when_not_in_args(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GCP_PROJECT_ID", "proj-123")
    with _patched_run(_completed(stdout="[]")) as mock_run:
        CloudResourcePropertyVerifier(
            args=["compute", "routers", "nats", "list", "--router", "r", "--region", "us-central1"],
            op="absent",
        ).verify(timeout_sec=0)

    argv = mock_run.call_args.args[0]
    assert argv[-3:] == ["--project", "proj-123", "--format=json"]


def test_explicit_project_in_args_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GCP_PROJECT_ID", "proj-123")
    with _patched_run(_completed(stdout="[]")) as mock_run:
        CloudResourcePropertyVerifier(
            args=["compute", "routers", "nats", "list", "--router", "r", "--project=other"],
            op="absent",
        ).verify(timeout_sec=0)

    argv = mock_run.call_args.args[0]
    assert "--project=other" in argv
    assert "proj-123" not in argv


# --- eager validation --------------------------------------------------------


def test_rejects_args_without_read_only_verb() -> None:
    with pytest.raises(ValidationError, match="read-only verb"):
        CloudResourcePropertyVerifier(
            args=["compute", "instances", "delete", "my-vm", "--zone", "us-central1-a"],
            op="exists",
        )


def test_read_verb_as_flag_value_is_rejected() -> None:
    with pytest.raises(ValidationError, match="read-only verb"):
        CloudResourcePropertyVerifier(
            args=["compute", "instances", "delete", "vm", "--zone", "list"],
            op="exists",
        )


def test_accepts_each_read_only_verb() -> None:
    for verb in ("list", "describe", "get-iam-policy"):
        CloudResourcePropertyVerifier(args=["something", verb], op="exists")


def test_rejects_author_supplied_format_flag() -> None:
    with pytest.raises(ValidationError, match="output format"):
        CloudResourcePropertyVerifier(
            args=["compute", "instances", "list", "--format=yaml"],
            op="exists",
        )
    with pytest.raises(ValidationError, match="output format"):
        CloudResourcePropertyVerifier(
            args=["compute", "instances", "list", "--format", "yaml"],
            op="exists",
        )


def test_rejects_invalid_jsonpath_at_load() -> None:
    with pytest.raises(ValidationError, match="not a valid JSONPath"):
        CloudResourcePropertyVerifier(args=["x", "list"], path="a[", op="eq", value=1)


def test_rejects_invalid_regex_at_load() -> None:
    with pytest.raises(ValidationError, match="invalid pattern"):
        CloudResourcePropertyVerifier(args=["x", "list"], path="a", op="matches", value="[")


def test_rejects_value_op_without_path() -> None:
    with pytest.raises(ValidationError, match="requires 'path'"):
        CloudResourcePropertyVerifier(args=["x", "list"], op="eq", value=1)


# --- result mapping ----------------------------------------------------------


def test_describe_object_payload_eq_pass() -> None:
    payload = {"name": "nat-1", "natIpAllocateOption": "AUTO_ONLY"}
    with _patched_run(_completed(stdout=json.dumps(payload))):
        result = CloudResourcePropertyVerifier(
            args=["compute", "routers", "nats", "describe", "nat-1", "--router", "r"],
            path="natIpAllocateOption",
            op="eq",
            value="AUTO_ONLY",
        ).verify(timeout_sec=0)

    assert result.success is True
    assert result.status == "pass"


def test_list_payload_across_matches_every() -> None:
    payload = [
        {"name": "a", "privateIpGoogleAccess": True},
        {"name": "b", "privateIpGoogleAccess": True},
    ]
    with _patched_run(_completed(stdout=json.dumps(payload))):
        result = CloudResourcePropertyVerifier(
            args=["compute", "networks", "subnets", "list"],
            path="privateIpGoogleAccess",
            op="eq",
            value=True,
            across_matches="every",
        ).verify(timeout_sec=0)

    assert result.status == "pass"


def test_matches_op_end_to_end() -> None:
    payload = {"name": "nat-router-prod", "natIpAllocateOption": "AUTO_ONLY"}
    with _patched_run(_completed(stdout=json.dumps(payload))):
        result = CloudResourcePropertyVerifier(
            args=["compute", "routers", "describe", "nat-router-prod"],
            path="name",
            op="matches",
            value=r"^nat-router-",
        ).verify(timeout_sec=0)

    assert result.status == "pass"


def test_empty_list_with_absent_passes() -> None:
    with _patched_run(_completed(stdout="[]")):
        result = CloudResourcePropertyVerifier(
            args=["compute", "routers", "nats", "list", "--router", "r"],
            op="absent",
        ).verify(timeout_sec=0)

    assert result.status == "pass"


def test_not_found_stderr_with_absent_passes() -> None:
    with _patched_run(_completed(returncode=1, stderr="ERROR: resource was not found")):
        result = CloudResourcePropertyVerifier(
            args=["compute", "networks", "subnets", "describe", "ghost"],
            op="absent",
        ).verify(timeout_sec=0)

    assert result.status == "pass"


def test_not_found_stderr_with_value_op_fails_not_errors() -> None:
    with _patched_run(_completed(returncode=1, stderr="ERROR: resource was not found")):
        result = CloudResourcePropertyVerifier(
            args=["compute", "networks", "subnets", "describe", "ghost"],
            path="name",
            op="eq",
            value="ghost",
        ).verify(timeout_sec=0)

    assert result.status == "fail"
    assert result.success is False


def test_permission_stderr_is_error_never_absence() -> None:
    with _patched_run(
        _completed(returncode=1, stderr="ERROR: PERMISSION_DENIED: caller forbidden")
    ):
        result = CloudResourcePropertyVerifier(
            args=["compute", "networks", "subnets", "describe", "s"],
            op="absent",
        ).verify(timeout_sec=0)

    assert result.status == "error"
    assert result.success is False


def test_not_found_wording_with_permission_clause_is_error() -> None:
    with _patched_run(
        _completed(
            returncode=1,
            stderr="ERROR: project ghost does not exist or you do not have permission to access it",
        )
    ):
        result = CloudResourcePropertyVerifier(
            args=["compute", "networks", "subnets", "describe", "ghost"],
            op="absent",
        ).verify(timeout_sec=0)

    assert result.status == "error"
    assert result.success is False


def test_empty_list_with_across_matches_none_is_vacuously_true() -> None:
    with _patched_run(_completed(stdout="[]")):
        result = CloudResourcePropertyVerifier(
            args=["compute", "instances", "list"],
            path="status",
            op="eq",
            value="RUNNING",
            across_matches="none",
        ).verify(timeout_sec=0)

    assert result.status == "pass"


def test_non_json_stdout_is_error() -> None:
    with _patched_run(_completed(stdout="not json")):
        result = CloudResourcePropertyVerifier(
            args=["compute", "instances", "list"],
            op="exists",
        ).verify(timeout_sec=0)

    assert result.status == "error"


def test_runner_exception_is_error() -> None:
    with patch(f"{_MODULE}.run", side_effect=OSError("gcloud not on PATH")):
        result = CloudResourcePropertyVerifier(
            args=["compute", "instances", "list"],
            op="exists",
        ).verify(timeout_sec=0)

    assert result.status == "error"


# --- stderr sanitization in reasons -------------------------------------------


def test_stderr_email_is_redacted_in_reason() -> None:
    with _patched_run(
        _completed(returncode=1, stderr="ERROR: forbidden for user jane.doe@example.com")
    ):
        result = CloudResourcePropertyVerifier(
            args=["compute", "networks", "subnets", "describe", "s"],
            op="absent",
        ).verify(timeout_sec=0)

    assert "jane.doe@example.com" not in result.reason
    assert "<redacted-email>" in result.reason


def test_long_stderr_is_truncated_in_reason() -> None:
    long_stderr = "ERROR: forbidden. " + ("detail " * 200)
    with _patched_run(_completed(returncode=1, stderr=long_stderr)):
        result = CloudResourcePropertyVerifier(
            args=["compute", "networks", "subnets", "describe", "s"],
            op="absent",
        ).verify(timeout_sec=0)

    assert len(result.reason) < len(long_stderr)
    assert result.reason.endswith("...(truncated)")


def test_stderr_whitespace_is_collapsed_in_reason() -> None:
    with _patched_run(
        _completed(returncode=1, stderr="ERROR:   forbidden\n\n  for   this   caller")
    ):
        result = CloudResourcePropertyVerifier(
            args=["compute", "networks", "subnets", "describe", "s"],
            op="absent",
        ).verify(timeout_sec=0)

    assert "  " not in result.reason
    assert "\n" not in result.reason
