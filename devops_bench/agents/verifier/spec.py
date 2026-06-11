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

from typing import Dict, Union
from pydantic import RootModel
from devops_bench.agents.verifier.verifiers.pod_healthy_verifier import PodHealthyVerifier
from devops_bench.agents.verifier.verifiers.scaling_complete_verifier import ScalingCompleteVerifier

# SingleVerificationSpec is a discriminated union of all supported checker types
SingleVerificationSpec = Union[PodHealthyVerifier, ScalingCompleteVerifier]

# Top-level VerificationSpec which parses a dict mapping spec IDs to single verification specs.
class VerificationSpec(RootModel[Dict[str, SingleVerificationSpec]]):
    """Represents a structured verification specification."""
    pass

