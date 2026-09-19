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

"""Unit tests for devops_bench.agents.base."""

from collections.abc import Generator
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from devops_bench.agents import AGENTS, AgentConfig, AgentHarness, AgentResult
from devops_bench.core import Registry
from devops_bench.core.errors import AlreadyRegisteredError, InvalidKeyError, NotRegisteredError


def test_agents_registry_is_a_core_registry() -> None:
    assert isinstance(AGENTS, Registry)
    assert AGENTS.name == "agents"


def test_abstract_base_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        AgentHarness()  # type: ignore[abstract]


def test_subclass_run_returns_typed_result_with_latency() -> None:
    class _Stub(AgentHarness):
        def _execute(self, prompt: str, workspace_path=None) -> AgentResult:
            return AgentResult(output=f"echo:{prompt}", trajectory=[])

    result = _Stub().run("hi")
    assert isinstance(result, AgentResult)
    assert result.output == "echo:hi"
    assert result.latency > 0.0


def test_subclass_can_self_stamp_latency() -> None:
    class _Stub(AgentHarness):
        def _execute(self, prompt: str, workspace_path=None) -> AgentResult:
            # Subclasses with finer-grained timing may pre-fill latency; the
            # base must leave that value untouched.
            return AgentResult(output="x", trajectory=[], latency=99.0)

    assert _Stub().run("hi").latency == 99.0


def test_safety_net_converts_unexpected_exception_to_errored_result() -> None:
    class _Boom(AgentHarness):
        def _execute(self, prompt: str, workspace_path=None) -> AgentResult:
            raise RuntimeError("kaboom")

    result = _Boom().run("hi")
    assert isinstance(result, AgentResult)
    assert result.has_errors()
    assert "RuntimeError" in result.errors[0]
    assert "kaboom" in result.errors[0]
    assert result.output.startswith("Error:")
    assert result.latency >= 0.0


def test_safety_net_covers_a_none_result_from_execute() -> None:
    class _Forgetful(AgentHarness):
        def _execute(self, prompt: str, workspace_path=None) -> AgentResult:
            return None  # type: ignore[return-value]  # subclass bug: forgot to return

    result = _Forgetful().run("hi")
    assert isinstance(result, AgentResult)
    assert result.has_errors()
    assert "AttributeError" in result.errors[0]


def test_config_default_is_a_fresh_agent_config() -> None:
    class _Stub(AgentHarness):
        def _execute(self, prompt: str, workspace_path=None) -> AgentResult:
            return AgentResult(output="", trajectory=[])

    a = _Stub()
    b = _Stub()
    assert isinstance(a.config, AgentConfig)
    assert a.config is not b.config


def test_third_party_can_register_with_no_central_edit() -> None:
    """A dummy agent registers via @AGENTS.register and resolves via .get."""

    class _Dummy(AgentHarness):
        def _execute(self, prompt: str, workspace_path=None) -> AgentResult:
            return AgentResult(output="", trajectory=[])

    AGENTS.register("dummy-extension")(_Dummy)
    try:
        assert AGENTS.get("dummy-extension") is _Dummy
        assert "dummy-extension" in AGENTS
        # Re-registering the same key is rejected so the registry can never
        # silently shadow a builtin agent.
        with pytest.raises(AlreadyRegisteredError):
            AGENTS.register("dummy-extension")(_Dummy)
    finally:
        # The registry has no public unregister; touch the private dict to
        # leave the global clean for other tests.
        AGENTS._items.pop("dummy-extension", None)


def test_registry_miss_raises_not_registered() -> None:
    with pytest.raises(NotRegisteredError):
        AGENTS.get("definitely-not-registered")


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

    Any registry miss elsewhere in the suite (e.g. the miss test above) latches
    ``_entry_points_loaded`` for the whole session, which would keep a mocked
    scan from ever firing. Reset the flag on entry so this test's scan runs;
    on exit restore ``_items`` and the flag to their pre-test values, so
    nothing loaded here leaks and a later unmocked miss cannot trigger a real
    scan (which, on a host with a real ``devops_bench.agents`` package
    installed, would leak a live registration into the suite).
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


def test_registry_declares_the_agents_entry_point_group() -> None:
    """The registry is wired to the ``devops_bench.agents`` discovery group."""
    assert AGENTS._entry_point_group == "devops_bench.agents"  # noqa: SLF001


def test_external_harness_loads_via_entry_point(
    mocker: MockerFixture, _pristine_entry_point_scan: None
) -> None:
    """A harness shipped by another package resolves through the entry-point scan."""

    class _External(AgentHarness):
        def _execute(self, prompt: str, workspace_path: Path | None = None) -> AgentResult:
            return AgentResult(output="", trajectory=[])

    ep = _FakeEntryPoint("dummy-external", _External)
    mock_eps = mocker.patch("devops_bench.core.registry.metadata.entry_points", return_value=[ep])

    assert AGENTS.get("dummy-external") is _External
    mock_eps.assert_called_once_with(group="devops_bench.agents")


def test_uppercase_entry_point_is_skipped(
    mocker: MockerFixture, _pristine_entry_point_scan: None, caplog: pytest.LogCaptureFixture
) -> None:
    """An uppercase external key is dropped, not admitted as an unreachable entry.

    The harness lowercases the configured agent type, so ``Dummy-External``
    could never be looked up. Skipping it keeps the registry free of dead keys
    and surfaces the packaging mistake in the log instead.
    """

    class _External(AgentHarness):
        def _execute(self, prompt: str, workspace_path: Path | None = None) -> AgentResult:
            return AgentResult(output="", trajectory=[])

    bad = _FakeEntryPoint("Dummy-External", _External)
    good = _FakeEntryPoint("dummy-external", _External)
    mocker.patch("devops_bench.core.registry.metadata.entry_points", return_value=[bad, good])

    with caplog.at_level("WARNING"):
        # The valid sibling still loads — one bad key does not poison the scan.
        assert AGENTS.get("dummy-external") is _External
    assert "Dummy-External" not in AGENTS._items  # noqa: SLF001 - test-only assertion
    assert "Dummy-External" in caplog.text


def test_uppercase_explicit_registration_raises() -> None:
    """An in-tree key that breaks the lowercase contract fails loudly."""

    class _Dummy(AgentHarness):
        def _execute(self, prompt: str, workspace_path: Path | None = None) -> AgentResult:
            return AgentResult(output="", trajectory=[])

    with pytest.raises(InvalidKeyError) as exc_info:
        AGENTS.register("Dummy-Uppercase")(_Dummy)
    assert "lowercase" in str(exc_info.value)
    assert "Dummy-Uppercase" not in AGENTS._items  # noqa: SLF001 - test-only assertion


# --------------------------------------------------------------------------
# Multi-turn conversations
# --------------------------------------------------------------------------


def test_run_turns_on_a_single_turn_is_exactly_run() -> None:
    """One turn takes the plain ``_execute`` path, so no harness needs updating."""

    class _Stub(AgentHarness):
        def _execute(self, prompt: str, workspace_path=None) -> AgentResult:
            return AgentResult(output=f"echo:{prompt}", trajectory=[])

    result = _Stub().run_turns(["hi"])
    assert result.output == "echo:hi"
    assert result.latency > 0.0


def test_run_turns_errors_when_the_harness_cannot_hold_a_session() -> None:
    """A single-turn harness fails loudly rather than answering turn 1 only.

    Looping ``_execute`` would restart the conversation each turn, so the agent
    would answer turn n having forgotten 1..n-1 — a transcript that looks fine
    and means nothing.
    """

    class _Stub(AgentHarness):
        def _execute(self, prompt: str, workspace_path=None) -> AgentResult:
            return AgentResult(output=f"echo:{prompt}", trajectory=[])

    result = _Stub().run_turns(["one", "two"])
    assert result.has_errors()
    assert "single-turn" in result.errors[0]
    assert "2 turns" in result.errors[0]


def test_run_turns_errors_on_no_turns() -> None:
    class _Stub(AgentHarness):
        def _execute(self, prompt: str, workspace_path=None) -> AgentResult:
            return AgentResult(output="unused", trajectory=[])

    assert _Stub().run_turns([]).has_errors()


def test_run_turns_uses_the_override_when_a_harness_holds_a_session() -> None:
    """A session-holding harness sees every turn in one call."""

    class _Session(AgentHarness):
        def _execute(self, prompt: str, workspace_path=None) -> AgentResult:
            raise AssertionError("must not fall back to the single-turn path")

        def _execute_turns(self, prompts, workspace_path=None) -> AgentResult:
            return AgentResult(output="|".join(prompts), trajectory=[])

    result = _Session().run_turns(["one", "two", "three"])
    assert result.output == "one|two|three"
    assert result.latency > 0.0


def test_run_turns_keeps_the_safety_net() -> None:
    class _Boom(AgentHarness):
        def _execute(self, prompt: str, workspace_path=None) -> AgentResult:
            raise AssertionError("unused")

        def _execute_turns(self, prompts, workspace_path=None) -> AgentResult:
            raise RuntimeError("kaboom")

    result = _Boom().run_turns(["one", "two"])
    assert result.has_errors()
    assert "RuntimeError" in result.errors[0]
    assert "kaboom" in result.errors[0]
