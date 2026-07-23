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

"""API/MCP agent harness driving the shared :func:`run_tool_loop` primitive.

The concrete harness lives in :mod:`devops_bench.agents.api.agent` and
self-registers under ``"api"`` via ``@AGENTS.register`` — importing this
package triggers that registration (matching the CLI harness packages), so
``AGENTS.get("api")`` works after ``import devops_bench.agents.api``. Heavy
optional dependencies (the ``mcp`` SDK, ``deepeval``, provider SDKs) still
load only at call time.
"""

from __future__ import annotations

from devops_bench.agents.api.agent import ApiAgent

__all__ = ["ApiAgent"]
