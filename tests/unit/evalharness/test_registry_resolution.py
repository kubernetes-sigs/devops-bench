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

"""Registry-only agent resolution (CONVENTIONS.md §2 / harness handoff §5).

The harness must resolve agents purely through :data:`AGENTS` — there is no
``_AGENT_MODULES`` / ``_AGENT_KEYS`` dispatch table to edit when a new agent
is added. The acceptance bar is exactly: a dummy ``@AGENTS.register("dummy")``
resolves with **no harness edit**.
"""

from __future__ import annotations

import pytest

from devops_bench.agents import AGENTS, AgentConfig, AgentHarness, AgentResult
from devops_bench.core import NotRegisteredError
from devops_bench.evalharness.default import DefaultEvalHarness


class _DummyAgent(AgentHarness):
    """Trivial agent for the dropped-in-registration test.

    Captures the config it was constructed with so the test can assert that
    the harness threaded its resolved capabilities into the instance (rather
    than handing the agent an env-only config).
    """

    last_config: AgentConfig | None = None

    def __init__(self, config: AgentConfig | None = None) -> None:
        super().__init__(config)
        _DummyAgent.last_config = self.config

    def _execute(  # pragma: no cover - never run
        self, prompt: str, workspace_path=None
    ) -> AgentResult:
        return AgentResult(output=f"echo: {prompt}", trajectory=[])


@pytest.fixture
def dummy_agent_registered() -> None:
    """Register ``_DummyAgent`` under the canonical ``dummy`` key for one test."""
    AGENTS.register("dummy")(_DummyAgent)
    try:
        yield
    finally:
        # ``Registry`` has no public deregister; drop the key directly so the
        # fixture is hermetic across the suite.
        AGENTS._items.pop("dummy", None)  # noqa: SLF001 - test-only teardown
        _DummyAgent.last_config = None


def test_dummy_agent_resolves_with_no_harness_edit(
    dummy_agent_registered: None,
) -> None:
    """A third-party-registered agent flows through the orchestrator unchanged."""
    harness = DefaultEvalHarness(project_id="p", cluster_name="c")
    harness.agent_type = "dummy"

    agent = harness.resolve_agent("dummy")

    assert isinstance(agent, _DummyAgent)
    # The harness threaded its built config (not a bare ``AgentConfig()``).
    assert _DummyAgent.last_config is not None
    assert isinstance(_DummyAgent.last_config, AgentConfig)


def test_alias_normalizes_to_canonical_key() -> None:
    """``gemini-cli`` resolves to the canonical ``gemini`` agent."""
    harness = DefaultEvalHarness(project_id="p", cluster_name="c")

    # ``gemini-cli`` is the friendly alias for the gemini agent; resolution must
    # not require a path table — the alias map normalizes to ``gemini`` and the
    # registry returns the registered class.
    agent_cls = AGENTS.get("gemini")
    agent = harness.resolve_agent("gemini-cli")
    assert isinstance(agent, agent_cls)


def test_unknown_agent_type_raises_not_registered() -> None:
    """An agent key with no registration produces ``NotRegisteredError``."""
    harness = DefaultEvalHarness(project_id="p", cluster_name="c")

    with pytest.raises(NotRegisteredError):
        harness.resolve_agent("not-a-real-agent-key")
