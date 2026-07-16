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

"""LLM provider clients and the provider-selection factory.

Adapter modules are named by model family and register themselves in
``MODELS`` on import. A provider's SDK is imported only when its adapter is
constructed, so a missing SDK surfaces as :class:`MissingDependencyError` at
construction time, not on import.
"""

from __future__ import annotations

from devops_bench.models.base import MODELS, LLMClient, get_model

__all__ = ["LLMClient", "MODELS", "get_model"]
