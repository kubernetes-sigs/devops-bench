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

from collections.abc import Generator
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

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
    #
    # Resolve first: builtin harnesses self-register on the lazy import that
    # ``resolve_agent`` performs, so reading ``AGENTS`` ahead of it would pass
    # only when some earlier test happened to import the module.
    agent = harness.resolve_agent("gemini-cli")
    assert isinstance(agent, AGENTS.get("gemini"))


def test_claude_code_alias_normalizes_to_canonical_key() -> None:
    """``claude-code`` resolves to the canonical ``claude`` agent.

    The ``AGENTS`` registry has no alias mechanism; the alias lives in
    ``_AGENT_TYPE_ALIASES`` and is applied only by ``resolve_agent``, whose lazy
    import is also what registers the builtin — hence resolving before reading.
    """
    harness = DefaultEvalHarness(project_id="p", cluster_name="c")

    agent = harness.resolve_agent("claude-code")
    assert isinstance(agent, AGENTS.get("claude"))


def test_unknown_agent_type_raises_not_registered() -> None:
    """An agent key with no registration produces ``NotRegisteredError``."""
    harness = DefaultEvalHarness(project_id="p", cluster_name="c")

    with pytest.raises(NotRegisteredError):
        harness.resolve_agent("not-a-real-agent-key")


class _FakeEntryPoint:
    """Minimal stand-in for ``importlib.metadata.EntryPoint``."""

    def __init__(self, name: str, value: type) -> None:
        self.name = name
        self._value = value

    def load(self) -> type:
        return self._value


@pytest.fixture
def _pristine_entry_point_scan() -> Generator[None, None, None]:
    """Reset ``AGENTS``' one-time entry-point scan around a test.

    A registry miss anywhere in the suite (e.g. the unknown-agent test above)
    latches ``_entry_points_loaded`` session-wide, which would keep a mocked
    scan from firing. Reset on entry; on exit restore ``_items`` and the flag
    to their pre-test values (so a later unmocked miss cannot run a real scan
    on a host with a real ``devops_bench.agents`` package installed), and
    clear the dummy's captured config even when an assertion failed mid-test.
    """
    saved_items = dict(AGENTS._items)  # noqa: SLF001 - test-only isolation
    saved_loaded = AGENTS._entry_points_loaded  # noqa: SLF001
    AGENTS._entry_points_loaded = False  # noqa: SLF001
    try:
        yield
    finally:
        AGENTS._items.clear()  # noqa: SLF001
        AGENTS._items.update(saved_items)  # noqa: SLF001
        AGENTS._entry_points_loaded = saved_loaded  # noqa: SLF001
        _DummyAgent.last_config = None


def test_entry_point_agent_resolves_with_no_harness_edit(
    mocker: MockerFixture, _pristine_entry_point_scan: None
) -> None:
    """An agent shipped via the ``devops_bench.agents`` entry-point group flows
    through the orchestrator with no import of its module anywhere in this tree —
    the pip-installed-library integration path."""
    ep = _FakeEntryPoint("dummy-external", _DummyAgent)
    mock_eps = mocker.patch("devops_bench.core.registry.metadata.entry_points", return_value=[ep])
    harness = DefaultEvalHarness(project_id="p", cluster_name="c")

    agent = harness.resolve_agent("dummy-external")

    assert isinstance(agent, _DummyAgent)
    mock_eps.assert_called_once_with(group="devops_bench.agents")
    # The harness threaded its built config into the entry-point-loaded agent.
    assert isinstance(_DummyAgent.last_config, AgentConfig)


def test_manifest_records_the_canonical_harness_key(tmp_path: Path, mocker: MockerFixture) -> None:
    """An arm selected via an alias records the canonical key, not the alias.

    ``harness`` is contractually the canonical key (``ResultRow.harness``), and
    ``setup_id`` is derived from it, so recording the alias would split one arm
    into two dashboard setups. Nothing else asserts what ``_write_run_artifacts``
    writes.
    """
    harness = DefaultEvalHarness(project_id="p", cluster_name="c")
    harness.agent_type = "claude-code"
    harness._agent_config.model = "claude-opus-4-8"  # noqa: SLF001 - arm identity under test
    write_manifest = mocker.patch.object(harness.reporter, "write_manifest")
    mocker.patch.object(harness.reporter, "write_rows")

    harness._write_run_artifacts(tmp_path, [])  # noqa: SLF001 - the unit under test

    manifest = write_manifest.call_args.args[1]
    assert manifest["harness"] == "claude"
    assert manifest["setupId"].startswith("claude-opus-4-8-claude")
    # A harness with no tier concept leaves the id and the field alone.
    assert manifest["reasoningEffort"] is None
    assert "effort" not in manifest["setupId"]


def test_manifest_records_the_reasoning_effort_of_a_tiered_harness(
    tmp_path: Path, mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tier is part of the arm: it lands on the manifest and in ``setupId``.

    Without it, ``low`` and ``high`` runs of one model share a ``setupId`` and
    the aggregate's keep-newest dedupe lets a re-run at one tier replace rows
    scored at the other.
    """
    import devops_bench.agents.cli.antigravity.agent  # noqa: F401 - registers the harness

    monkeypatch.delenv("AGENT_MODEL_EFFORT", raising=False)
    harness = DefaultEvalHarness(project_id="p", cluster_name="c")
    harness.agent_type = "antigravity"
    harness._agent_config.model = "google/gemini-3.1-pro-preview-low"  # noqa: SLF001
    write_manifest = mocker.patch.object(harness.reporter, "write_manifest")
    mocker.patch.object(harness.reporter, "write_rows")

    harness._write_run_artifacts(tmp_path, [])  # noqa: SLF001 - the unit under test

    manifest = write_manifest.call_args.args[1]
    assert manifest["reasoningEffort"] == "low"
    assert manifest["setupId"].startswith("google-gemini-3-1-pro-preview-low-antigravity")
    assert manifest["setupId"].endswith("-effort-low")
