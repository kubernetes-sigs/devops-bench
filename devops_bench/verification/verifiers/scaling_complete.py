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

"""Verifier that waits for a deployment to converge to a ready replica target."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import model_validator

from devops_bench.core import SubprocessError, get_logger
from devops_bench.k8s import get_resource
from devops_bench.verification.base import VERIFIERS, BaseVerifier, VerificationResult

__all__ = ["ScalingCompleteVerifier"]

_log = get_logger("verification.scaling_complete")


@VERIFIERS.register("scaling_complete")
class ScalingCompleteVerifier(BaseVerifier):
    """Verify that a deployment has converged to a ready replica target.

    The deployment's ``status.readyReplicas`` is polled with exponential backoff
    (via :func:`devops_bench.k8s.poll_until`) until it falls within
    ``[min_replicas, max_replicas]`` or the timeout elapses. Leaving
    ``max_replicas`` unset checks only the lower bound (scale-up); setting it
    bounds the count from above as well, covering scale-down and optimization
    targets where the deployment must shrink to or stay within a ceiling.

    Attributes:
        type: Discriminator literal, always ``"scaling_complete"``.
        deployment: Name of the deployment to inspect.
        min_replicas: Minimum ready replicas required for success.
        max_replicas: Optional maximum ready replicas allowed for success; when
            ``None`` only the lower bound is enforced.
        namespace: Optional namespace; defaults to the active one.

    Raises:
        pydantic.ValidationError: If ``min_replicas`` or ``max_replicas`` is
            negative, or ``min_replicas`` exceeds ``max_replicas``.
    """

    type: Literal["scaling_complete"] = "scaling_complete"
    deployment: str
    min_replicas: int = 1
    max_replicas: int | None = None
    namespace: str | None = None

    @model_validator(mode="after")
    def _validate_replica_bounds(self) -> ScalingCompleteVerifier:
        if self.min_replicas < 0:
            raise ValueError(f"min_replicas must be >= 0, got {self.min_replicas}")
        if self.max_replicas is not None:
            if self.max_replicas < 0:
                raise ValueError(f"max_replicas must be >= 0, got {self.max_replicas}")
            if self.min_replicas > self.max_replicas:
                raise ValueError(
                    f"min_replicas ({self.min_replicas}) must be <= "
                    f"max_replicas ({self.max_replicas})"
                )
        return self

    def verify(self, timeout_sec: float) -> VerificationResult:
        """Poll the deployment until its ready replicas reach the target range.

        Args:
            timeout_sec: Maximum seconds to keep polling.

        Returns:
            A result reflecting the last observed scaling state; successful once
            ready replicas fall within ``[min_replicas, max_replicas]``.
        """
        return self._poll_to_result(lambda: self._check_scaling(timeout_sec), timeout_sec)

    def _check_scaling(self, timeout_sec: float) -> tuple[bool, str, dict[str, Any] | None]:
        """Read the deployment once and compare ready replicas to the target.

        Args:
            timeout_sec: Seconds before the ``kubectl get`` call is killed, so
                a single hung API request cannot block the whole poll.

        Returns:
            A ``(success, reason, raw)`` triple. ``raw`` carries the raw
            deployment document once one could be read, else ``None``.
        """
        try:
            dep_data = get_resource(
                "deployment",
                self.deployment,
                namespace=self.namespace,
                kubeconfig=self.kubeconfig,
                timeout=timeout_sec,
            )
        except SubprocessError as exc:
            stderr = (exc.stderr or "").strip()
            _log.warning("Failed to get deployment %s: %s", self.deployment, stderr)
            return False, f"Failed to get deployment: {stderr}", None
        except ValueError:
            _log.warning("Failed to parse deployment JSON for %s", self.deployment)
            return False, "Failed to parse deployment JSON", None

        # ``status`` may be explicitly null before the controller populates it.
        ready_replicas = (dep_data.get("status") or {}).get("readyReplicas", 0)
        raw = {"deployment": dep_data}
        if ready_replicas < self.min_replicas:
            reason = f"Ready replicas ({ready_replicas}) < min replicas ({self.min_replicas})"
            return False, reason, raw
        if self.max_replicas is not None and ready_replicas > self.max_replicas:
            reason = f"Ready replicas ({ready_replicas}) > max replicas ({self.max_replicas})"
            return False, reason, raw
        if self.max_replicas is None:
            reason = f"Ready replicas ({ready_replicas}) >= min replicas ({self.min_replicas})"
        else:
            reason = (
                f"Ready replicas ({ready_replicas}) within bounds "
                f"[{self.min_replicas}, {self.max_replicas}]"
            )
        return True, reason, raw
