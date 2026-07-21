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

"""Type-safe verification engine for validating cluster state.

This package exposes the verification spec model
(:class:`VerificationSpec`, :class:`SequenceSpec`, :class:`ParallelSpec`), its
typed outcome (:class:`VerificationResult`), the registry-driven extension
surface (:data:`VERIFIERS`, :func:`parse_node`), and the deadline-based
:class:`VerifierAgent` that evaluates them. Importing the package pulls no
heavy SDKs — concrete leaf verifiers register via this package's submodules
only.
"""

from devops_bench.verification.base import VERIFIERS, BaseVerifier, VerificationResult
from devops_bench.verification.runner import VerifierAgent
from devops_bench.verification.spec import (
    ParallelSpec,
    SequenceSpec,
    VerificationNode,
    VerificationSpec,
    json_schema,
    parse_node,
)

__all__ = [
    "BaseVerifier",
    "ParallelSpec",
    "SequenceSpec",
    "VERIFIERS",
    "VerificationNode",
    "VerificationResult",
    "VerificationSpec",
    "VerifierAgent",
    "json_schema",
    "parse_node",
]
