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

import pytest

from devops_bench.agents import AGENTS, AgentConfig, AgentHarness, AgentResult
from devops_bench.core import Registry
from devops_bench.core.errors import AlreadyRegisteredError, NotRegisteredError


def test_agents_registry_is_a_core_registry():
    assert isinstance(AGENTS, Registry)
    assert AGENTS.name == "agents"


def test_abstract_base_cannot_be_instantiated():
    with pytest.raises(TypeError):
        AgentHarness()  # type: ignore[abstract]


def test_subclass_run_returns_typed_result_with_latency():
    class _Stub(AgentHarness):
        def _execute(self, prompt: str) -> AgentResult:
            return AgentResult(output=f"echo:{prompt}", trajectory=[])

    result = _Stub().run("hi")
    assert isinstance(result, AgentResult)
    assert result.output == "echo:hi"
    assert result.latency > 0.0


def test_subclass_can_self_stamp_latency():
    class _Stub(AgentHarness):
        def _execute(self, prompt: str) -> AgentResult:
            # Subclasses with finer-grained timing may pre-fill latency; the
            # base must leave that value untouched.
            return AgentResult(output="x", trajectory=[], latency=99.0)

    assert _Stub().run("hi").latency == 99.0


def test_safety_net_converts_unexpected_exception_to_errored_result():
    class _Boom(AgentHarness):
        def _execute(self, prompt: str) -> AgentResult:
            raise RuntimeError("kaboom")

    result = _Boom().run("hi")
    assert isinstance(result, AgentResult)
    assert result.has_errors()
    assert "RuntimeError" in result.errors[0]
    assert "kaboom" in result.errors[0]
    assert result.output.startswith("Error:")
    assert result.latency >= 0.0


def test_config_default_is_a_fresh_agent_config():
    class _Stub(AgentHarness):
        def _execute(self, prompt: str) -> AgentResult:
            return AgentResult(output="", trajectory=[])

    a = _Stub()
    b = _Stub()
    assert isinstance(a.config, AgentConfig)
    assert a.config is not b.config


def test_third_party_can_register_with_no_central_edit():
    """A dummy agent registers via @AGENTS.register and resolves via .get."""

    class _Dummy(AgentHarness):
        def _execute(self, prompt: str) -> AgentResult:
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


def test_registry_miss_raises_not_registered():
    with pytest.raises(NotRegisteredError):
        AGENTS.get("definitely-not-registered")
