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

"""Shared fixtures for the agent-harness unit tests."""

from __future__ import annotations

import pytest

_PREFLIGHT_MODULES = (
    "devops_bench.agents.cli.antigravity.agent",
    "devops_bench.agents.cli.gemini_cli.agent",
    "devops_bench.agents.cli.openclaw.agent",
)


@pytest.fixture(autouse=True)
def stub_mcp_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize the MCP reachability probe for every agent unit test.

    The probe launches each bound server as a real subprocess, which these
    tests deliberately do not provide (their bindings name fake binaries like
    ``gke-mcp``). Tests that exercise the probe itself patch the symbol back to
    a raising stub, or call
    :mod:`devops_bench.agents.shared.mcp_probe` directly.
    """
    for module in _PREFLIGHT_MODULES:
        monkeypatch.setattr(f"{module}.preflight_mcp", lambda *a, **kw: {})
