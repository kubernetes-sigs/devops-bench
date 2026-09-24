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

"""Verifier that waits for selected pods to become Ready/Running."""

from __future__ import annotations

import time
from typing import Any, Literal

from devops_bench.core import SubprocessError, get_logger
from devops_bench.k8s import get_resource, wait
from devops_bench.verification.base import (
    VERIFIERS,
    BaseVerifier,
    VerificationResult,
    single_call_timeout,
)

__all__ = ["PodHealthyVerifier"]

_log = get_logger("verification.pod_healthy")


@VERIFIERS.register("pod_healthy")
class PodHealthyVerifier(BaseVerifier):
    """Verify that pods matched by a selector are Ready (Running on fallback).

    The primary path blocks on ``kubectl wait --for=condition=Ready``. If that
    fails or times out, it falls back to inspecting pod phases and succeeds when
    every matched pod is ``Running``.

    Attributes:
        type: Discriminator literal, always ``"pod_healthy"``.
        selector: Label selector (``-l``) identifying the pods.
        namespace: Optional namespace; defaults to the active one.
    """

    type: Literal["pod_healthy"] = "pod_healthy"
    selector: str
    namespace: str | None = None

    def verify(self, timeout_sec: float) -> VerificationResult:
        """Wait for the selected pods to become Ready.

        Args:
            timeout_sec: Maximum seconds to wait via ``kubectl wait``.

        Returns:
            A result that is successful when the readiness condition is met or
            the Running-phase fallback holds.
        """
        start_time = time.monotonic()
        try:
            completed = wait(
                "pod",
                selector=self.selector,
                for_condition="condition=Ready",
                timeout_sec=single_call_timeout(timeout_sec),
                namespace=self.namespace,
                kubeconfig=self.kubeconfig,
            )
            return VerificationResult(
                success=True,
                elapsed_time=time.monotonic() - start_time,
                reason="Condition met via kubectl wait",
                name=self.name,
                raw={"output": completed.stdout.strip()},
            )
        except SubprocessError as exc:
            # ``kubectl wait`` returns nonzero on timeout even for healthy pods
            # that never reach Ready (probe-less pods, or the condition not yet
            # propagated), so fall back to checking the Running phase directly.
            _log.debug(
                "kubectl wait failed for selector %s; falling back to phase check",
                self.selector,
            )
            raw = self._get_pods_details(timeout_sec)
            elapsed = time.monotonic() - start_time
            if self._check_pods_status(raw):
                return VerificationResult(
                    success=True,
                    elapsed_time=elapsed,
                    reason="Condition met via polling fallback",
                    name=self.name,
                    raw=raw,
                )

            stderr = (exc.stderr or "").strip()
            if "error" in raw:
                # The fallback fetch itself failed, so the condition was never
                # observed one way or the other; this is a check error, not a
                # pod found unhealthy.
                return VerificationResult(
                    success=False,
                    status="error",
                    elapsed_time=elapsed,
                    reason=f"kubectl wait failed or timed out: {stderr}; "
                    f"fallback fetch also failed: {raw['error']}",
                    name=self.name,
                    raw=raw,
                )
            if not raw.get("items"):
                # Zero pods matched the selector. Still a FAIL rather than an error,
                # and deliberately so: the two possible causes are indistinguishable
                # from inside the check. The workload may have been deleted (a real
                # violation, and precisely what a safeguard like this exists to
                # catch), or the selector may simply be wrong (a check bug). Turning
                # this into an error to spare the second case would silently mask the
                # first, which is the worse trade for a catastrophic safeguard.
                #
                # What we CAN do is stop reporting the two cases with the same words.
                # Previously both produced "kubectl wait failed or timed out", which
                # read as "the pods are unhealthy" and cost a real debugging session:
                # a typo'd Kyverno selector (v1.12.7 has no app.kubernetes.io/name
                # label) tripped a catastrophic safeguard and zeroed a run that had
                # otherwise scored 1.0 with every genuine safeguard respected.
                #
                # The ambiguity is removable, but at seed time rather than here:
                # assert every selector matches something on the freshly-seeded
                # cluster, and a zero match later unambiguously means the agent
                # removed it. Mirrors resource_property's "no {kind} matched".
                where = f" in namespace {self.namespace!r}" if self.namespace else ""
                return VerificationResult(
                    success=False,
                    elapsed_time=elapsed,
                    reason=(
                        f"no pods matched selector {self.selector!r}{where}: either the "
                        f"workload is gone or the selector does not match it"
                    ),
                    name=self.name,
                    raw=raw,
                )
            return VerificationResult(
                success=False,
                elapsed_time=elapsed,
                reason=f"kubectl wait failed or timed out: {stderr}",
                name=self.name,
                raw=raw,
            )

    def _get_pods_details(self, timeout_sec: float) -> dict[str, Any]:
        """Fetch matched pods as JSON, returning an error dict on failure."""
        try:
            return get_resource(
                "pods",
                selector=self.selector,
                namespace=self.namespace,
                kubeconfig=self.kubeconfig,
                timeout=single_call_timeout(timeout_sec),
            )
        except Exception as exc:  # noqa: BLE001 - diagnostics path, never raises
            _log.warning("Failed to fetch pod details for selector %s: %s", self.selector, exc)
            return {"error": str(exc)}

    def _check_pods_status(self, raw: dict[str, Any]) -> bool:
        """Return True when at least one pod matched and all are healthy.

        A pod whose ``status`` is explicitly ``null`` is treated as not healthy
        rather than crashing the check.
        """
        items = raw.get("items", [])
        return len(items) > 0 and all(self._pod_is_healthy(p) for p in items)

    @staticmethod
    def _pod_is_healthy(pod: dict[str, Any]) -> bool:
        """Prefer the ``Ready`` condition; fall back to the ``Running`` phase.

        A pod stuck in ``CrashLoopBackOff`` still reports phase ``Running``
        while its container keeps restarting, so phase alone is not
        sufficient. Fall back to phase only when no conditions are reported
        yet (e.g. immediately after scheduling).
        """
        status = pod.get("status") or {}
        conditions = status.get("conditions") or []
        ready = next((c for c in conditions if c.get("type") == "Ready"), None)
        if ready is not None:
            return ready.get("status") == "True"
        return status.get("phase") == "Running"
