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

"""Unit tests for :mod:`devops_bench.agents.capabilities`."""

from __future__ import annotations

import dataclasses

import pytest

from devops_bench.agents.capabilities import (
    AgentRules,
    AllCapabilities,
    McpBinding,
    SkillBinding,
    SupportsMcp,
    SupportsRules,
    SupportsSkills,
)

# ---------------------------------------------------------------------------
# Binding construction
# ---------------------------------------------------------------------------


def test_mcp_binding_defaults_to_empty_command_and_tools():
    binding = McpBinding()
    assert binding.name == ""
    assert binding.command == ()
    assert binding.tools == ()


def test_mcp_binding_is_frozen():
    """Frozen so a binding can be shared safely across agents in a run."""
    binding = McpBinding(name="gke", command=("/bin/mcp",), tools=("list",))
    with pytest.raises(dataclasses.FrozenInstanceError):
        binding.name = "other"  # type: ignore[misc]


def test_skill_binding_defaults_to_empty_paths_and_is_frozen():
    binding = SkillBinding()
    assert binding.paths == ()
    with pytest.raises(dataclasses.FrozenInstanceError):
        binding.paths = ("/x",)  # type: ignore[misc]


def test_agent_rules_defaults_to_empty_text_and_is_frozen():
    rules = AgentRules()
    assert rules.text == ""
    with pytest.raises(dataclasses.FrozenInstanceError):
        rules.text = "x"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# AllCapabilities aggregate
# ---------------------------------------------------------------------------


def test_agent_capabilities_defaults_are_empty_bindings():
    caps = AllCapabilities()
    assert caps.mcp_servers == ()
    assert caps.skills == SkillBinding()
    assert caps.rules == AgentRules()
    assert caps.allowed_tools == ()
    assert caps.tools_enabled is False
    assert caps.mcp is None


def test_agent_capabilities_aggregates_allowed_tools_across_servers():
    caps = AllCapabilities(
        mcp_servers=(
            McpBinding(name="a", tools=("t1", "t2")),
            McpBinding(name="b", tools=("t3",)),
        ),
    )
    assert caps.allowed_tools == ("t1", "t2", "t3")
    assert caps.tools_enabled is True
    # ``.mcp`` returns the first binding for the common single-server case.
    assert caps.mcp is not None and caps.mcp.name == "a"


def test_agent_capabilities_tools_enabled_reflects_any_mcp_binding():
    """A binding with no tools still counts — the agent has *some* MCP."""
    caps = AllCapabilities(mcp_servers=(McpBinding(name="x"),))
    assert caps.tools_enabled is True
    assert caps.allowed_tools == ()


def test_agent_capabilities_is_frozen():
    caps = AllCapabilities()
    with pytest.raises(dataclasses.FrozenInstanceError):
        caps.mcp_servers = (McpBinding(),)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Protocol membership (runtime_checkable Protocols — structural, no mixins)
# ---------------------------------------------------------------------------


class _OnlyMcp:
    """A bare type exposing ``mcp_servers`` to gain SupportsMcp."""

    mcp_servers: tuple[McpBinding, ...] = ()


class _OnlySkills:
    skills = SkillBinding()


class _OnlyRules:
    rules = AgentRules()


def test_mcp_attribute_grants_supports_mcp():
    assert isinstance(_OnlyMcp(), SupportsMcp)
    assert not isinstance(_OnlyMcp(), SupportsSkills)
    assert not isinstance(_OnlyMcp(), SupportsRules)


def test_skills_attribute_grants_supports_skills():
    assert isinstance(_OnlySkills(), SupportsSkills)
    assert not isinstance(_OnlySkills(), SupportsMcp)


def test_rules_attribute_grants_supports_rules():
    assert isinstance(_OnlyRules(), SupportsRules)
    assert not isinstance(_OnlyRules(), SupportsMcp)


def test_protocols_accept_duck_typed_classes():
    """Structural typing — any class exposing the attribute satisfies the
    Protocol, no inheritance required."""

    class Duck:
        mcp_servers: tuple = ()
        skills = SkillBinding()
        rules = AgentRules()

    duck = Duck()
    assert isinstance(duck, SupportsMcp)
    assert isinstance(duck, SupportsSkills)
    assert isinstance(duck, SupportsRules)


def test_defaults_keep_a_fresh_agent_disabled():
    """A class exposing the attribute without bindings reports the defaults
    (empty tuple) — the harness's structural check still passes but the agent
    runs with no capability granted."""
    instance = _OnlyMcp()
    assert instance.mcp_servers == ()
