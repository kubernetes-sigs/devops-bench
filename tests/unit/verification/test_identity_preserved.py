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

"""Unit tests for ``IdentityPreservedVerifier``.

``kubectl`` is stubbed at the module boundary; no cluster is contacted.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from devops_bench.core import SubprocessError
from devops_bench.verification.verifiers import IdentityPreservedVerifier

_GET = "devops_bench.verification.verifiers.identity_preserved.get_resource"
_UID_KEY = "devops-bench.io/original-uid"
_CREATED_KEY = "devops-bench.io/original-creation-timestamp"

_UID = "9f1c2e44-0d0e-4a6b-9a1f-2b3c4d5e6f70"
_CREATED = "2026-01-02T03:04:05Z"


def _deployment(
    uid: str = _UID,
    created: str = _CREATED,
    annotations: dict[str, str] | None = None,
) -> dict[str, Any]:
    if annotations is None:
        annotations = {_UID_KEY: _UID, _CREATED_KEY: _CREATED}
    return {
        "kind": "Deployment",
        "metadata": {
            "name": "orders-api",
            "namespace": "shop",
            "uid": uid,
            "creationTimestamp": created,
            "annotations": annotations,
        },
    }


def _verifier(**kwargs: Any) -> IdentityPreservedVerifier:
    base = {
        "type": "identity_preserved",
        "kind": "Deployment",
        "resource_name": "orders-api",
    }
    base.update(kwargs)
    return IdentityPreservedVerifier(**base)


# -- the object survived ------------------------------------------------


def test_an_object_updated_in_place_keeps_its_identity() -> None:
    with patch(_GET, return_value=_deployment()):
        result = _verifier().verify(0.0)
    assert result.success is True
    assert result.status == "pass"
    assert result.raw["live_uid"] == result.raw["baseline_uid"] == _UID


# -- the object was replaced --------------------------------------------


def test_a_recreated_object_is_caught_by_its_new_uid() -> None:
    # The whole point: delete-and-reapply produces a Deployment identical in
    # every field a spec check looks at, but the apiserver assigns a fresh uid
    # that no client can set.
    replaced = _deployment(uid="00000000-1111-2222-3333-444444444444")
    with patch(_GET, return_value=replaced):
        result = _verifier().verify(0.0)
    assert result.success is False
    assert result.status == "fail"
    assert "deleted and recreated" in result.reason


def test_a_moved_creation_timestamp_alone_is_enough_to_fail() -> None:
    with patch(_GET, return_value=_deployment(created="2026-06-30T12:00:00Z")):
        result = _verifier().verify(0.0)
    assert result.success is False
    assert result.status == "fail"


def test_an_object_rebuilt_from_a_manifest_has_no_baseline_to_match() -> None:
    # Recreating from the GitOps repo yields an object that never carried the
    # setup-time annotation, so there is nothing to compare against. That is a
    # failure, not a skip: the absence is itself the evidence of a rebuild.
    with patch(_GET, return_value=_deployment(annotations={})):
        result = _verifier().verify(0.0)
    assert result.success is False
    assert result.status == "fail"
    assert "no baseline identity annotation" in result.reason


def test_a_half_recorded_baseline_still_fails() -> None:
    with patch(_GET, return_value=_deployment(annotations={_UID_KEY: _UID})):
        result = _verifier().verify(0.0)
    assert result.success is False
    assert result.status == "fail"


def test_missing_metadata_does_not_crash_the_check() -> None:
    with patch(_GET, return_value={"kind": "Deployment"}):
        result = _verifier().verify(0.0)
    assert result.success is False
    assert result.status == "fail"


# -- configuration ------------------------------------------------------


def test_the_annotation_keys_are_overridable() -> None:
    obj = _deployment(annotations={"acme.io/uid": _UID, "acme.io/created": _CREATED})
    with patch(_GET, return_value=obj):
        result = _verifier(
            uid_annotation_key="acme.io/uid",
            creation_timestamp_annotation_key="acme.io/created",
        ).verify(0.0)
    assert result.success is True


def test_the_namespace_is_threaded_through_to_kubectl() -> None:
    with patch(_GET, return_value=_deployment()) as mock_get:
        _verifier(namespace="shop").verify(0.0)
    assert mock_get.call_args.kwargs["namespace"] == "shop"


# -- failure handling ---------------------------------------------------


def test_a_kubectl_failure_is_an_error_not_a_crash() -> None:
    with patch(_GET, side_effect=RuntimeError("connection refused")):
        result = _verifier().verify(0.0)
    assert result.success is False
    assert result.status == "error"
    assert "connection refused" in result.reason


def test_a_deleted_object_fails_rather_than_erroring() -> None:
    exc = SubprocessError(
        cmd=["kubectl", "get", "Deployment", "orders-api"],
        returncode=1,
        stderr='Error from server (NotFound): deployments.apps "orders-api" not found',
    )
    with patch(_GET, side_effect=exc):
        result = _verifier().verify(0.0)
    assert result.success is False
    assert result.status == "fail"
    assert "no longer exists" in result.reason
