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

"""Cloud providers selecting credentials and OpenTofu variables per cloud."""

from __future__ import annotations

# Import for their registration side effects so the registry is populated.
from devops_bench.providers import gcp as _gcp  # noqa: F401
from devops_bench.providers import kind as _kind  # noqa: F401
from devops_bench.providers.base import PROVIDERS, Provider, ResolveContext

__all__ = ["PROVIDERS", "Provider", "ResolveContext"]
