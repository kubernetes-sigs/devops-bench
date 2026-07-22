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
from devops_bench.verification.base import VERIFIERS, BaseVerifier, VerificationResult

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
                timeout_sec=timeout_sec,
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
            raw = self._get_pods_details()
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
            return VerificationResult(
                success=False,
                elapsed_time=elapsed,
                reason=f"kubectl wait failed or timed out: {stderr}",
                name=self.name,
                raw=raw,
            )

    def _get_pods_details(self) -> dict[str, Any]:
        """Fetch matched pods as JSON, returning an error dict on failure."""
        try:
            return get_resource(
                "pods",
                selector=self.selector,
                namespace=self.namespace,
                kubeconfig=self.kubeconfig,
            )
        except Exception as exc:  # noqa: BLE001 - diagnostics path, never raises
            _log.warning("Failed to fetch pod details for selector %s: %s", self.selector, exc)
            return {"error": str(exc)}

    def _check_pods_status(self, raw: dict[str, Any]) -> bool:
        """Return True when at least one pod matched and all are ``Running``.

        A pod whose ``status`` is explicitly ``null`` is treated as not Running
        rather than crashing the check.
        """
        items = raw.get("items", [])
        return len(items) > 0 and all(
            (p.get("status") or {}).get("phase") == "Running" for p in items
        )
