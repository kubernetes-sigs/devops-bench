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

"""Gemini CLI agent harness, named to distinguish it from the Gemini model.

The harness driver lives in :mod:`.agent` and the stream-json parser in
:mod:`.parsing`. Importing this package self-registers the agent under the
``"gemini"`` key via ``@AGENTS.register``.
"""

from __future__ import annotations

from devops_bench.agents.cli.gemini_cli.agent import GeminiCliAgent
from devops_bench.agents.cli.gemini_cli.parsing import parse_stream_json

__all__ = ["GeminiCliAgent", "parse_stream_json"]
