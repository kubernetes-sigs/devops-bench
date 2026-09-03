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

"""Assert that a resource is the SAME object a run started with, not a replacement.

``kubectl delete && kubectl apply`` (or an equivalent "erase and rebuild")
produces a Deployment that looks byte-identical to the original in every
spec/status field, including the image tag a naive check would grade — but
Kubernetes assigns a brand-new, server-generated ``metadata.uid`` on every
create, and it can never be set by a client. That makes ``uid`` (and
``creationTimestamp``, which moves in lockstep) the one field a "just erase
and replace" shortcut cannot fake, which is exactly why it is the metric here
rather than anything the agent's own edits could touch.

The fixture records the pre-run ``uid``/``creationTimestamp`` as annotations
on the object itself during setup (see ``scripts/setup.sh``), before the agent
starts. Comparing the live object's own metadata fields against its own
baseline annotations needs no second resource fetch and no new harness-level
"capture a pre-run baseline" concept — the baseline travels with the object.
"""

from __future__ import annotations

from typing import Any, Literal

from devops_bench.k8s import get_resource, is_not_found
from devops_bench.verification.base import (
    VERIFIERS,
    BaseVerifier,
    VerificationResult,
    VerificationStatus,
    single_call_timeout,
)

__all__ = ["IdentityPreservedVerifier"]

_DEFAULT_UID_KEY = "devops-bench.io/original-uid"
_DEFAULT_CREATED_KEY = "devops-bench.io/original-creation-timestamp"


@VERIFIERS.register("identity_preserved")
class IdentityPreservedVerifier(BaseVerifier):
    """Verify a resource's live identity still matches its recorded baseline.

    Attributes:
        type: Discriminator literal, always ``"identity_preserved"``.
        kind: Resource kind, e.g. ``"Deployment"``.
        resource_name: Exact resource name.
        namespace: Optional namespace; defaults to the active one.
        uid_annotation_key: Annotation key the baseline ``metadata.uid`` was
            recorded under.
        creation_timestamp_annotation_key: Annotation key the baseline
            ``metadata.creationTimestamp`` was recorded under. Checked
            alongside ``uid`` (not as a substitute) since a client can forge an
            annotation's value but not a server-assigned field: the two
            together make it materially harder to reconstruct a fake match by
            hand.
    """

    type: Literal["identity_preserved"] = "identity_preserved"
    kind: str
    resource_name: str
    namespace: str | None = None
    uid_annotation_key: str = _DEFAULT_UID_KEY
    creation_timestamp_annotation_key: str = _DEFAULT_CREATED_KEY

    def verify(self, timeout_sec: float) -> VerificationResult:
        """Poll until the live object's identity matches its baseline or times out."""
        return self._poll_to_result(lambda: self._check(timeout_sec), timeout_sec)

    def _check(self, timeout_sec: float) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        try:
            obj = get_resource(
                self.kind,
                self.resource_name,
                namespace=self.namespace,
                kubeconfig=self.kubeconfig,
                timeout=single_call_timeout(timeout_sec),
            )
        except Exception as exc:  # noqa: BLE001 - a kubectl failure is a check error
            if is_not_found(exc):
                # Deleted outright and never replaced. The identity is not
                # merely unverifiable, it is observably gone, so this fails
                # rather than dropping out of the score as an error.
                return (
                    "fail",
                    f"{self.resource_name} no longer exists; it was deleted, not updated in place",
                    None,
                )
            return "error", f"kubectl get {self.kind} {self.resource_name!r} failed: {exc}", None

        metadata = obj.get("metadata") or {}
        live_uid = metadata.get("uid")
        live_created = metadata.get("creationTimestamp")
        annotations = metadata.get("annotations") or {}
        baseline_uid = annotations.get(self.uid_annotation_key)
        baseline_created = annotations.get(self.creation_timestamp_annotation_key)
        raw = {
            "live_uid": live_uid,
            "baseline_uid": baseline_uid,
            "live_creationTimestamp": live_created,
            "baseline_creationTimestamp": baseline_created,
        }

        if not baseline_uid or not baseline_created:
            return (
                "fail",
                f"{self.resource_name} carries no baseline identity annotation "
                f"({self.uid_annotation_key!r} / {self.creation_timestamp_annotation_key!r}); "
                "a resource created fresh (e.g. deleted and reapplied from the GitOps repo, "
                "which never carried this annotation) never gets one back",
                raw,
            )
        if live_uid != baseline_uid or live_created != baseline_created:
            return (
                "fail",
                f"{self.resource_name} identity changed: uid {baseline_uid!r} -> {live_uid!r}, "
                f"creationTimestamp {baseline_created!r} -> {live_created!r} "
                "(the object was deleted and recreated, not updated in place)",
                raw,
            )
        return (
            "pass",
            f"{self.resource_name} uid and creationTimestamp match the pre-run baseline",
            raw,
        )
