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

class PodHealthyVerifier(BaseVerifier):
    type: Literal["pod_healthy"] = "pod_healthy"
    selector: str
    namespace: Optional[str] = None

    def verify(self, timeout_sec: int) -> VerificationResult:
        timer = Timer()
        event_received = threading.Event()
        
        def on_event(event: dict):
            # If the event targets a pod, signal the verifier to check status
            if event.get("kind") == "pod":
                if self.namespace and event.get("namespace") != self.namespace:
                    return
                event_received.set()

        watcher = KubeWatchService(on_event)
        try:
            watcher.start()
            
            # Initial check to see if condition is already satisfied
            details = self._get_pods_details()
            if self._check_pods_status(details):
                return VerificationResult(
                    success=True,
                    elapsed_time=timer.elapsed,
                    reason="All pods are healthy and ready (initial check)",
                    details=details,
                )
            # Block and wait for event-driven updates
            while timer.elapsed < timeout_sec:
                remaining = timeout_sec - timer.elapsed
                if remaining <= 0:
                    break
                
                # Sleep efficiently until notified of a pod event
                if event_received.wait(timeout=remaining):
                    event_received.clear()
                    details = self._get_pods_details()
                    if self._check_pods_status(details):
                        return VerificationResult(
                            success=True,
                            elapsed_time=timer.elapsed,
                            reason="All pods are healthy and ready (event-driven)",
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
            
        # Final fallback status check on timeout
        details = self._get_pods_details()
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
