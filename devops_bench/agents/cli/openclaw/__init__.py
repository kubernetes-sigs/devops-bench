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

"""OpenClaw CLI agent harness driving the local ``oc`` binary.

The harness driver lives in :mod:`.agent` and the trajectory-bundle parsers in
:mod:`.parsing`. Importing this package self-registers the agent under the
``"openclaw"`` key via ``@AGENTS.register``.
"""

from __future__ import annotations

from devops_bench.agents.cli.openclaw.agent import OpenClawAgent
from devops_bench.agents.cli.openclaw.parsing import parse_trajectory_export

__all__ = ["OpenClawAgent", "parse_trajectory_export"]
