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

"""Verify a subject's live authorization via ``kubectl auth can-i --as``.

Two shapes, both backed by :func:`devops_bench.k8s.can_i`:

* :class:`SubjectAccessReviewVerifier` -- a single (verb, resource[, name])
  point check against an expected allowed/denied outcome.
* :class:`SubjectAccessReviewSweepVerifier` -- a whole matrix of (verb,
  resource) pairs that must ALL match one expected outcome (``all_denied`` or
  ``all_allowed``). Every cell is checked and compared exactly: an unexpected
  ALLOW under ``all_denied`` fails the sweep just as much as an unexpected
  DENY under ``all_allowed`` would -- this is what makes the sweep catch an
  agent that "fixes" an access gap by over-granting instead of granting the
  exact permission asked for.

Both subjects are ServiceAccounts, qualified as
``system:serviceaccount:<namespace>:<name>`` for ``--as``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from devops_bench.core import SubprocessError, get_logger
from devops_bench.k8s import can_i
from devops_bench.verification.base import (
    VERIFIERS,
    BaseVerifier,
    VerificationResult,
    VerificationStatus,
    single_call_timeout,
)

__all__ = ["SubjectAccessReviewSweepVerifier", "SubjectAccessReviewVerifier"]

_log = get_logger("verification.subject_access_review")


def _as_subject(namespace: str, name: str) -> str:
    return f"system:serviceaccount:{namespace}:{name}"


def _describe_resource(resource: str, name: str | None) -> str:
    return f"{resource}/{name}" if name else resource


def _run_can_i(
    *,
    verb: str,
    resource: str,
    name: str | None,
    check_namespace: str,
    subject_namespace: str,
    subject: str,
    kubeconfig: str | None,
    context: str | None,
    timeout_sec: float,
) -> tuple[str | None, str | None]:
    """Run ``kubectl auth can-i`` and classify the outcome.

    Returns ``(observed, error_detail)``, exactly one of which is not None:
    ``observed`` (``"allowed"`` or ``"denied"``) on a clean yes/no answer, or
    ``error_detail`` when the call could not be interpreted as one (a real
    command error, e.g. a bad flag or an unreachable API server).
    """
    try:
        completed = can_i(
            verb,
            resource,
            subject=_as_subject(subject_namespace, subject),
            name=name,
            namespace=check_namespace,
            kubeconfig=kubeconfig,
            context=context,
            timeout=single_call_timeout(timeout_sec),
        )
    except SubprocessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip() or str(exc)
        return None, detail
    stdout = (completed.stdout or "").strip()
    if stdout == "yes":
        return "allowed", None
    if stdout == "no":
        return "denied", None
    stderr = (completed.stderr or "").strip()
    detail = stderr or stdout or f"unexpected output (exit {completed.returncode})"
    return None, detail


@VERIFIERS.register("subject_access_review")
class SubjectAccessReviewVerifier(BaseVerifier):
    """Verify a single point authorization check via ``kubectl auth can-i``.

    Confirms whether ``subject`` (a ServiceAccount in ``namespace``) is allowed
    or denied a single (verb, resource[, name]) operation in ``namespace``.

    Attributes:
        type: Discriminator literal, always ``"subject_access_review"``.
        subject: ServiceAccount name; its own namespace is ``namespace``.
        namespace: Namespace hosting ``subject``, and the namespace the
            (verb, resource) check runs against.
        verb: Verb to check, e.g. ``"get"``, ``"update"``.
        resource: Resource type to check, e.g. ``"deployments"``.
        name: Optional specific resource name.
        op: Comparison operator; only ``"eq"`` is supported.
        value: Expected outcome, ``"allowed"`` or ``"denied"``.
    """

    type: Literal["subject_access_review"] = "subject_access_review"
    subject: str
    namespace: str
    verb: str
    resource: str
    name: str | None = None
    op: Literal["eq"] = "eq"
    value: Literal["allowed", "denied"]

    def verify(self, timeout_sec: float) -> VerificationResult:
        """Poll until the check reports the expected outcome or time runs out."""
        return self._poll_to_result(lambda: self._check(timeout_sec), timeout_sec)

    def _check(self, timeout_sec: float) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        observed, error = _run_can_i(
            verb=self.verb,
            resource=self.resource,
            name=self.name,
            check_namespace=self.namespace,
            subject_namespace=self.namespace,
            subject=self.subject,
            kubeconfig=self.kubeconfig,
            context=self.context,
            timeout_sec=timeout_sec,
        )
        subject_desc = _as_subject(self.namespace, self.subject)
        resource_desc = _describe_resource(self.resource, self.name)
        if observed is None:
            _log.warning("can-i check failed for %s: %s", subject_desc, error)
            return (
                "error",
                f"can-i {self.verb} {resource_desc} -n {self.namespace} --as {subject_desc} "
                f"failed: {error}",
                None,
            )
        raw = {
            "subject": subject_desc,
            "verb": self.verb,
            "resource": resource_desc,
            "namespace": self.namespace,
            "expected": self.value,
            "observed": observed,
        }
        if observed == self.value:
            return (
                "pass",
                f"{subject_desc} is {observed} to {self.verb} {resource_desc} in "
                f"{self.namespace}, as expected",
                raw,
            )
        return (
            "fail",
            f"{subject_desc} expected {self.value} to {self.verb} {resource_desc} in "
            f"{self.namespace}, observed {observed}",
            raw,
        )


class _SweepPair(BaseModel):
    """One (verb, resource) cell of a :class:`SubjectAccessReviewSweepVerifier`."""

    model_config = ConfigDict(extra="forbid")

    verb: str
    resource: str
    namespace: str | None = None
    name: str | None = None


@VERIFIERS.register("subject_access_review_sweep")
class SubjectAccessReviewSweepVerifier(BaseVerifier):
    """Verify a whole matrix of authorization checks for one subject.

    Every (verb, resource) pair in ``pairs`` must match the single expected
    outcome ``op`` implies -- ``"all_denied"`` requires every pair to be
    denied, ``"all_allowed"`` requires every pair to be allowed. Every cell is
    checked and compared exactly, in both directions: under ``"all_denied"``,
    an unexpected ALLOW fails the sweep exactly like a missing DENY would.

    Attributes:
        type: Discriminator literal, always ``"subject_access_review_sweep"``.
        subject: ServiceAccount name; its own namespace is ``namespace``.
        namespace: Namespace hosting ``subject``. A pair without its own
            ``namespace`` is checked here.
        pairs: (verb, resource) pairs to check, each either a flat
            ``[verb, resource]`` 2-tuple or a
            ``{verb, resource, namespace?, name?}`` mapping.
        op: ``"all_denied"`` or ``"all_allowed"``.
    """

    type: Literal["subject_access_review_sweep"] = "subject_access_review_sweep"
    subject: str
    namespace: str
    pairs: list[_SweepPair] = Field(min_length=1)
    op: Literal["all_denied", "all_allowed"]

    @field_validator("pairs", mode="before")
    @classmethod
    def _normalize_pairs(cls, value: Any) -> Any:
        """Accept a flat ``[verb, resource]`` 2-tuple alongside a full mapping."""
        if not isinstance(value, list):
            return value
        normalized = []
        for pair in value:
            if isinstance(pair, (list, tuple)):
                if len(pair) != 2:
                    raise ValueError(
                        f"flat sweep pair must have exactly 2 elements [verb, resource]; "
                        f"got {pair!r}"
                    )
                normalized.append({"verb": pair[0], "resource": pair[1]})
            else:
                normalized.append(pair)
        return normalized

    def verify(self, timeout_sec: float) -> VerificationResult:
        """Poll until every pair matches the expected outcome or time runs out."""
        return self._poll_to_result(lambda: self._check(timeout_sec), timeout_sec)

    def _check(self, timeout_sec: float) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        expected = "allowed" if self.op == "all_allowed" else "denied"
        subject_desc = _as_subject(self.namespace, self.subject)
        cells: list[dict[str, Any]] = []
        for pair in self.pairs:
            check_namespace = pair.namespace or self.namespace
            resource_desc = _describe_resource(pair.resource, pair.name)
            observed, error = _run_can_i(
                verb=pair.verb,
                resource=pair.resource,
                name=pair.name,
                check_namespace=check_namespace,
                subject_namespace=self.namespace,
                subject=self.subject,
                kubeconfig=self.kubeconfig,
                context=self.context,
                timeout_sec=timeout_sec,
            )
            if observed is None:
                _log.warning("can-i sweep check failed for %s: %s", subject_desc, error)
                return (
                    "error",
                    f"can-i {pair.verb} {resource_desc} -n {check_namespace} "
                    f"--as {subject_desc} failed: {error}",
                    {"subject": subject_desc, "op": self.op, "cells": cells},
                )
            cells.append(
                {
                    "verb": pair.verb,
                    "resource": resource_desc,
                    "namespace": check_namespace,
                    "expected": expected,
                    "observed": observed,
                }
            )
        raw = {"subject": subject_desc, "op": self.op, "cells": cells}
        mismatches = [c for c in cells if c["observed"] != expected]
        if mismatches:
            described = "; ".join(
                f"{c['verb']} {c['resource']} in {c['namespace']} expected {c['expected']}, "
                f"observed {c['observed']}"
                for c in mismatches
            )
            return (
                "fail",
                f"{subject_desc}: {len(mismatches)}/{len(cells)} pair(s) mismatched: {described}",
                raw,
            )
        return (
            "pass",
            f"{subject_desc}: all {len(cells)} pair(s) {expected}, as expected",
            raw,
        )
