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
import json
import threading
from typing import Literal, Optional
from devops_bench.agents.verifier.base import BaseVerifier, VerificationResult
from devops_bench.agents.verifier.watcher import KubeWatchService
from devops_bench.agents.verifier.utils import Timer, KUBECTL_DEFAULT_TIMEOUT

class ScalingCompleteVerifier(BaseVerifier):
    type: Literal["scaling_complete"] = "scaling_complete"
    deployment: str
    min_replicas: int = 1
    namespace: Optional[str] = None

    def verify(self, timeout_sec: int) -> VerificationResult:
        timer = Timer()
        event_received = threading.Event()
        
        def on_event(event: dict):
            # Check if event targets our deployment name
            if event.get("kind") == "deployment" and event.get("name") == self.deployment:
                if self.namespace and event.get("namespace") != self.namespace:
                    return
                event_received.set()

        watcher = KubeWatchService(on_event)
        try:
            watcher.start()
            
            # Initial check
            success, details = self._check_scaling()
            if success:
                return VerificationResult(
                    success=True,
                    elapsed_time=timer.elapsed,
                    reason=f"Scaling complete: {details.get('reason')} (initial check)",
                    details=details,
                )

            # Event-driven waiting loop
            while timer.elapsed < timeout_sec:
                remaining = timeout_sec - timer.elapsed
                if remaining <= 0:
                    break
                
                # Sleep efficiently until deployment event is received
                if event_received.wait(timeout=remaining):
                    event_received.clear()
                    success, details = self._check_scaling()
                    if success:
                        return VerificationResult(
                            success=True,
                            elapsed_time=timer.elapsed,
                            reason=f"Scaling complete: {details.get('reason')} (event-driven)",
                            details=details,
                        )
        except Exception as e:
            return VerificationResult(
                success=False,
                elapsed_time=timer.elapsed,
                reason=f"Error during kubewatch: {str(e)}",
                details={"error": str(e)},
            )
        finally:
            watcher.stop()
            
        # Final fallback check on timeout
        success, details = self._check_scaling()
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
