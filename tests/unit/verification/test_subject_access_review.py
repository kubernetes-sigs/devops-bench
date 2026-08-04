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

"""Unit tests for ``SubjectAccessReviewVerifier`` and its sweep sibling.

``kubectl auth can-i`` is faked via a stubbed ``can_i``; no real cluster work.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from devops_bench.core import SubprocessError
from devops_bench.verification.verifiers import (
    SubjectAccessReviewSweepVerifier,
    SubjectAccessReviewVerifier,
)

_MODULE = "devops_bench.verification.verifiers.subject_access_review"


def _completed(stdout: str, returncode: int, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# --- point checks -----------------------------------------------------------


def test_allowed_and_expected_allowed_passes() -> None:
    with patch(f"{_MODULE}.can_i", return_value=_completed("yes", 0)):
        result = SubjectAccessReviewVerifier(
            subject="deployer",
            namespace="apps",
            verb="update",
            resource="deployments",
            value="allowed",
        ).verify(timeout_sec=0)

    assert result.success is True
    assert result.status == "pass"
    assert "allowed to update deployments" in result.reason
    assert result.raw["observed"] == "allowed"


def test_allowed_but_expected_denied_fails() -> None:
    # The "beyond grant" case: an agent over-grants instead of granting the
    # exact permission asked for, so an allow where a deny was expected fails.
    with patch(f"{_MODULE}.can_i", return_value=_completed("yes", 0)):
        result = SubjectAccessReviewVerifier(
            subject="deployer",
            namespace="apps",
            verb="delete",
            resource="secrets",
            value="denied",
        ).verify(timeout_sec=0)

    assert result.success is False
    assert result.status == "fail"
    assert "expected denied" in result.reason
    assert "observed allowed" in result.reason


def test_denied_and_expected_denied_passes() -> None:
    with patch(f"{_MODULE}.can_i", return_value=_completed("no", 1)):
        result = SubjectAccessReviewVerifier(
            subject="deployer",
            namespace="apps",
            verb="delete",
            resource="secrets",
            value="denied",
        ).verify(timeout_sec=0)

    assert result.success is True
    assert result.status == "pass"
    assert "denied to delete secrets" in result.reason


def test_denied_but_expected_allowed_fails() -> None:
    with patch(f"{_MODULE}.can_i", return_value=_completed("no", 1)):
        result = SubjectAccessReviewVerifier(
            subject="deployer",
            namespace="apps",
            verb="update",
            resource="deployments",
            value="allowed",
        ).verify(timeout_sec=0)

    assert result.success is False
    assert result.status == "fail"
    assert "expected allowed" in result.reason
    assert "observed denied" in result.reason


def test_command_error_is_status_error_not_fail() -> None:
    with patch(
        f"{_MODULE}.can_i",
        side_effect=SubprocessError(
            ["kubectl"], returncode=2, stderr="the server doesn't have a resource type"
        ),
    ):
        result = SubjectAccessReviewVerifier(
            subject="deployer",
            namespace="apps",
            verb="update",
            resource="deployments",
            value="allowed",
        ).verify(timeout_sec=0)

    assert result.success is False
    assert result.status == "error"
    assert "failed" in result.reason
    assert result.raw is None


def test_unexpected_stdout_is_status_error() -> None:
    # Not a raised SubprocessError (kubectl can still exit 0/1 while printing
    # something other than "yes"/"no" on a malformed invocation), but still
    # not a clean answer -- must not be silently treated as "denied".
    with patch(f"{_MODULE}.can_i", return_value=_completed("", 1, stderr="error: unreachable")):
        result = SubjectAccessReviewVerifier(
            subject="deployer",
            namespace="apps",
            verb="update",
            resource="deployments",
            value="allowed",
        ).verify(timeout_sec=0)

    assert result.success is False
    assert result.status == "error"
    assert "unreachable" in result.reason


def test_service_account_subject_formatting() -> None:
    with patch(f"{_MODULE}.can_i", return_value=_completed("yes", 0)) as mock_can_i:
        SubjectAccessReviewVerifier(
            subject="deployer",
            namespace="apps",
            verb="update",
            resource="deployments",
            value="allowed",
        ).verify(timeout_sec=0)

    assert mock_can_i.call_args.kwargs["subject"] == "system:serviceaccount:apps:deployer"
    assert mock_can_i.call_args.kwargs["namespace"] == "apps"
    assert mock_can_i.call_args.args == ("update", "deployments")


def test_resource_name_is_appended_to_resource_arg() -> None:
    with patch(f"{_MODULE}.can_i", return_value=_completed("yes", 0)) as mock_can_i:
        SubjectAccessReviewVerifier(
            subject="inventory-sync",
            namespace="warehouse",
            verb="get",
            resource="secrets",
            name="inventory-sync-creds",
            value="allowed",
        ).verify(timeout_sec=0)

    assert mock_can_i.call_args.kwargs["name"] == "inventory-sync-creds"


# --- sweep --------------------------------------------------------------


def test_sweep_all_denied_passes_when_every_pair_denied() -> None:
    with patch(f"{_MODULE}.can_i", return_value=_completed("no", 1)):
        result = SubjectAccessReviewSweepVerifier(
            subject="deployer",
            namespace="apps",
            pairs=[["delete", "deployments"], ["create", "deployments"]],
            op="all_denied",
        ).verify(timeout_sec=0)

    assert result.success is True
    assert result.status == "pass"
    assert "all 2 pair(s) denied" in result.reason


def test_sweep_all_denied_fails_on_unexpected_allow() -> None:
    # no-access-beyond-grant's exact intent: an agent that over-grants (e.g.
    # cluster-admin) instead of the precise gap must fail this sweep even
    # though every OTHER pair is correctly denied.
    def fake_can_i(verb, resource, **kwargs):
        allowed = verb == "delete" and resource == "deployments"
        return _completed("yes" if allowed else "no", 0 if allowed else 1)

    with patch(f"{_MODULE}.can_i", side_effect=fake_can_i):
        result = SubjectAccessReviewSweepVerifier(
            subject="deployer",
            namespace="apps",
            pairs=[["delete", "deployments"], ["create", "deployments"]],
            op="all_denied",
        ).verify(timeout_sec=0)

    assert result.success is False
    assert result.status == "fail"
    assert "1/2 pair(s) mismatched" in result.reason
    assert "delete deployments" in result.reason
    assert "expected denied, observed allowed" in result.reason


def test_sweep_all_allowed_fails_on_unexpected_deny() -> None:
    def fake_can_i(verb, resource, **kwargs):
        allowed = verb != "get"
        return _completed("yes" if allowed else "no", 0 if allowed else 1)

    with patch(f"{_MODULE}.can_i", side_effect=fake_can_i):
        result = SubjectAccessReviewSweepVerifier(
            subject="inventory-sync",
            namespace="warehouse",
            pairs=[
                {"verb": "list", "resource": "pods"},
                {"verb": "get", "resource": "secrets", "name": "inventory-sync-creds"},
            ],
            op="all_allowed",
        ).verify(timeout_sec=0)

    assert result.success is False
    assert result.status == "fail"
    assert "1/2 pair(s) mismatched" in result.reason
    assert "get secrets/inventory-sync-creds" in result.reason
    assert "expected allowed, observed denied" in result.reason


def test_sweep_dict_pair_namespace_overrides_subject_home_namespace() -> None:
    with patch(f"{_MODULE}.can_i", return_value=_completed("no", 1)) as mock_can_i:
        SubjectAccessReviewSweepVerifier(
            subject="inventory-sync",
            namespace="warehouse",
            pairs=[{"verb": "get", "resource": "pods", "namespace": "kube-system"}],
            op="all_denied",
        ).verify(timeout_sec=0)

    # The check namespace is the pair's own, but the subject stays qualified
    # against its own home namespace.
    assert mock_can_i.call_args.kwargs["namespace"] == "kube-system"
    assert (
        mock_can_i.call_args.kwargs["subject"] == "system:serviceaccount:warehouse:inventory-sync"
    )


def test_sweep_command_error_is_status_error_not_fail() -> None:
    with patch(
        f"{_MODULE}.can_i",
        side_effect=SubprocessError(["kubectl"], returncode=2, stderr="server unreachable"),
    ):
        result = SubjectAccessReviewSweepVerifier(
            subject="deployer",
            namespace="apps",
            pairs=[["delete", "deployments"]],
            op="all_denied",
        ).verify(timeout_sec=0)

    assert result.success is False
    assert result.status == "error"
    assert "server unreachable" in result.reason


def test_sweep_flat_pair_normalizes_to_two_element_form() -> None:
    verifier = SubjectAccessReviewSweepVerifier(
        subject="deployer",
        namespace="apps",
        pairs=[["get", "deployments"]],
        op="all_denied",
    )
    assert verifier.pairs[0].verb == "get"
    assert verifier.pairs[0].resource == "deployments"
    assert verifier.pairs[0].namespace is None
    assert verifier.pairs[0].name is None


def test_sweep_flat_pair_wrong_length_raises() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="exactly 2 elements"):
        SubjectAccessReviewSweepVerifier(
            subject="deployer",
            namespace="apps",
            pairs=[["get", "deployments", "extra"]],
            op="all_denied",
        )
