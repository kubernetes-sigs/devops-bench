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

from typing import Union
from devops_bench.agents.verifier.base import VerificationResult
from devops_bench.agents.verifier.spec import VerificationSpec
from devops_bench.agents.verifier.utils import Timer

class VerifierAgent:
    """Uses kubectl to validate cluster state."""

    def wait_for_condition(
        self, spec: Union[VerificationSpec, dict], timeout_sec: int = 120
    ) -> VerificationResult:
        """Waits for condition using watch or polling.
        Supports structured specifications mapping spec IDs to individual verifiers.
        """
        if not isinstance(spec, VerificationSpec):
            spec = VerificationSpec(spec)

        root = spec.root
        timer = Timer()

        results = {}
        overall_success = True
        overall_reason = []
        for name, sub_spec in root.items():
            remaining_timeout = timeout_sec - timer.elapsed
            if remaining_timeout <= 0:
                overall_success = False
                overall_reason.append(f"{name} failed: Timeout reached")
                break
            sub_result = sub_spec.verify(timeout_sec=max(1, int(remaining_timeout)))
            results[name] = sub_result
            if not sub_result.success:
                overall_success = False
                overall_reason.append(f"{name} failed: {sub_result.reason}")
            else:
                overall_reason.append(f"{name} succeeded")
        return VerificationResult(
            success=overall_success,
            elapsed_time=timer.elapsed,
            reason="; ".join(overall_reason),
            details=results,
        )
