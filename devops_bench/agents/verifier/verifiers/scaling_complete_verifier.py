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

class ScalingCompleteVerifier(BaseVerifier):
    type: Literal["scaling_complete"] = "scaling_complete"
    deployment: str
    min_replicas: int = 1
    namespace: Optional[str] = None

    def verify(self, timeout_sec: int) -> VerificationResult:
        timer = Timer()
        delay = 2

        while timer.elapsed < timeout_sec:
            remaining = timeout_sec - timer.elapsed
            if remaining <= 0:
                break

            success, details = self._check_scaling(timeout=min(KUBECTL_DEFAULT_TIMEOUT, remaining))
            if success:
                return VerificationResult(
                    success=True,
                    elapsed_time=timer.elapsed,
                    reason=f"Scaling complete: {details.get('reason')}",
                    details=details,
                )
            
            sleep_time = min(delay, timeout_sec - timer.elapsed)
            if sleep_time > 0:
                time.sleep(sleep_time)

        # Last check on timeout
        remaining = timeout_sec - timer.elapsed
        success, details = self._check_scaling(timeout=max(1.0, remaining))
        return VerificationResult(
            success=success,
            elapsed_time=timer.elapsed,
            reason=f"Timeout reached: {details.get('reason')}",
            details=details,
        )

    def _check_scaling(self, timeout: float = KUBECTL_DEFAULT_TIMEOUT) -> tuple[bool, dict]:
        try:
            cmd = [
                "kubectl",
                "get",
                "deployment",
                self.deployment,
                "-o",
                "json",
            ]
            if self.namespace:
                cmd.extend(["-n", self.namespace])

            result = subprocess.run(
                cmd, capture_output=True, text=True, check=True, timeout=timeout
            )
            dep_data = json.loads(result.stdout)
            status = dep_data.get("status", {})
            metadata = dep_data.get("metadata", {})
            observed_generation = status.get("observedGeneration", 0)
            generation = metadata.get("generation", 0)
            ready_replicas = status.get("readyReplicas", 0)

            if observed_generation < generation:
                return False, {
                    "reason": f"Waiting for controller to observe generation {generation} (currently at {observed_generation})",
                    "deployment": dep_data,
                }

            success = ready_replicas >= self.min_replicas
            reason = (
                f"Ready replicas ({ready_replicas}) >= min replicas ({self.min_replicas})"
                if success
                else f"Ready replicas ({ready_replicas}) < min replicas ({self.min_replicas})"
            )
            return success, {"reason": reason, "deployment": dep_data}
        except subprocess.TimeoutExpired:
            return False, {"reason": "kubectl get deployment timed out"}
        except subprocess.CalledProcessError as e:
            return False, {
                "reason": f"Failed to get deployment: {e.stderr.strip()}"
            }
        except json.JSONDecodeError:
            return False, {"reason": "Failed to parse deployment JSON"}
