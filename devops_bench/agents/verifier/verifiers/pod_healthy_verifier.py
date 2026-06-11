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

import subprocess
import time
import json
from typing import Literal, Optional
from devops_bench.agents.verifier.base import BaseVerifier, VerificationResult
from devops_bench.agents.verifier.utils import Timer, KUBECTL_DEFAULT_TIMEOUT

class PodHealthyVerifier(BaseVerifier):
    type: Literal["pod_healthy"] = "pod_healthy"
    selector: str
    namespace: Optional[str] = None

    def verify(self, timeout_sec: int) -> VerificationResult:
        timer = Timer()
        delay = 1
        max_delay = 5
        last_details = {}

        while timer.elapsed < timeout_sec:
            remaining = timeout_sec - timer.elapsed
            if remaining <= 0:
                break

            # Try using kubectl wait
            cmd = [
                "kubectl",
                "wait",
                "--for=condition=Ready",
                "pod",
                "-l",
                self.selector,
                f"--timeout={int(remaining)}s",
            ]
            if self.namespace:
                cmd.extend(["-n", self.namespace])

            try:
                # Add Python-level timeout of remaining + 5 to prevent indefinite hanging
                result = subprocess.run(
                    cmd, capture_output=True, text=True, check=True, timeout=remaining + 5
                )
                return VerificationResult(
                    success=True,
                    elapsed_time=timer.elapsed,
                    reason="Condition met via kubectl wait",
                    details={"output": result.stdout.strip()},
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                # Catch timeout or error and fallback to polling check
                # Check status details
                details = self._get_pods_details(timeout=max(1.0, remaining))
                last_details = details
                if self._check_pods_status(details):
                    return VerificationResult(
                        success=True,
                        elapsed_time=timer.elapsed,
                        reason="Condition met via polling fallback",
                        details=details,
                    )
                
                # Sleep a bit and retry
                sleep_time = min(delay, timeout_sec - timer.elapsed)
                if sleep_time > 0:
                    time.sleep(sleep_time)
                delay = min(delay + 1, max_delay)

        # Last check on timeout
        remaining = timeout_sec - timer.elapsed
        details = self._get_pods_details(timeout=max(1.0, remaining))
        success = self._check_pods_status(details)
        return VerificationResult(
            success=success,
            elapsed_time=timer.elapsed,
            reason="Timeout reached",
            details=details,
        )

    def _get_pods_details(self, timeout: float = KUBECTL_DEFAULT_TIMEOUT) -> dict:
        try:
            cmd = ["kubectl", "get", "pods", "-l", self.selector, "-o", "json"]
            if self.namespace:
                cmd.extend(["-n", self.namespace])
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=True, timeout=timeout
            )
            return json.loads(result.stdout)
        except Exception as e:
            return {"error": str(e)}

    def _check_pods_status(self, details: dict) -> bool:
        items = details.get("items", [])
        if not items:
            return False
        for pod in items:
            status = pod.get("status", {})
            if status.get("phase") != "Running":
                return False
            conditions = status.get("conditions", [])
            # Must find a condition of type Ready with status True
            if not any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions):
                return False
        return True
