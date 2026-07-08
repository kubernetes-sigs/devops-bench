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

"""Unit tests for devops_bench.agents.config."""

from devops_bench.agents.capabilities import (
    AgentRules,
    AllCapabilities,
    McpBinding,
    SkillBinding,
)
from devops_bench.agents.config import AgentConfig


def test_default_construction_uses_safe_defaults():
    cfg = AgentConfig()
    assert cfg.model is None
    assert cfg.provider is None
    assert cfg.api_key is None
    assert cfg.target is None
    assert cfg.timeout_sec == 600.0
    assert cfg.max_turns is None
    # Capability bindings default to "no capabilities granted" — a fresh
    # ``AgentConfig`` looks indistinguishable from "use built-in defaults".
    assert cfg.capabilities == AllCapabilities()
    assert cfg.capabilities.mcp_servers == ()
    assert cfg.capabilities.skills == SkillBinding()
    assert cfg.capabilities.rules == AgentRules()
    assert cfg.capabilities.allowed_tools == ()
    assert cfg.capabilities.tools_enabled is False
    assert cfg.capabilities.mcp is None
    assert dict(cfg.extra_env) == {}


def test_from_env_maps_each_field():
    env = {
        "AGENT_MODEL": "gemini-2.5-pro",
        "AGENT_PROVIDER": "gemini",
        "AGENT_API_KEY": "secret",
        "AGENT_TARGET": "/usr/local/bin/gemini",
        "AGENT_TIMEOUT_SEC": "42",
        "AGENT_MCP_SERVER": "uv run mcp-server",
        "AGENT_ALLOWED_TOOLS": "tool_a, tool_b ,tool_c",
        "AGENT_SKILLS_PATHS": "/a/skills, /b/skills",
        "AGENT_RULES_TEXT": "be careful",
        "AGENT_MAX_TURNS": "25",
    }
    cfg = AgentConfig.from_env(env)
    assert cfg.model == "gemini-2.5-pro"
    assert cfg.provider == "gemini"
    assert cfg.api_key == "secret"
    assert cfg.target == "/usr/local/bin/gemini"
    assert cfg.timeout_sec == 42.0
    assert cfg.max_turns == 25
    # MCP binding aggregates both the server command and the tool allow-list.
    assert cfg.capabilities.mcp_servers == (
        McpBinding(
            name="default",
            command=("uv", "run", "mcp-server"),
            tools=("tool_a", "tool_b", "tool_c"),
        ),
    )
    assert cfg.capabilities.allowed_tools == ("tool_a", "tool_b", "tool_c")
    assert cfg.capabilities.skills == SkillBinding(paths=("/a/skills", "/b/skills"))
    assert cfg.capabilities.rules == AgentRules(text="be careful")


def test_from_env_treats_unset_as_defaults():
    cfg = AgentConfig.from_env({})
    assert cfg.model is None
    assert cfg.provider is None
    assert cfg.timeout_sec == 600.0
    assert cfg.max_turns is None
    assert cfg.capabilities == AllCapabilities()


def test_from_env_blank_allowed_tools_yields_empty_tuple():
    cfg = AgentConfig.from_env({"AGENT_ALLOWED_TOOLS": ""})
    assert cfg.capabilities.mcp_servers == ()  # blank → no binding at all
    cfg = AgentConfig.from_env({"AGENT_ALLOWED_TOOLS": " , , "})
    assert cfg.capabilities.mcp_servers == ()


def test_from_env_allowed_tools_only_builds_mcp_binding_with_empty_command():
    """A CLI agent (Gemini) gets an MCP binding with no launch command — the
    binary launches MCP in-process — but still carries the tools allow-list."""
    cfg = AgentConfig.from_env({"AGENT_ALLOWED_TOOLS": "alpha,beta"})
    assert cfg.capabilities.mcp_servers == (
        McpBinding(name="default", command=(), tools=("alpha", "beta")),
    )
    assert cfg.capabilities.allowed_tools == ("alpha", "beta")
    assert cfg.capabilities.tools_enabled is True


def test_from_env_mcp_server_only_builds_binding_with_no_tools():
    """The API agent path: a server command but no pre-approved tool list."""
    cfg = AgentConfig.from_env({"AGENT_MCP_SERVER": "/usr/local/bin/mcp-gke"})
    assert cfg.capabilities.mcp_servers == (
        McpBinding(name="default", command=("/usr/local/bin/mcp-gke",), tools=()),
    )
    assert cfg.capabilities.allowed_tools == ()


def test_from_env_skips_mcp_binding_when_neither_command_nor_tools_set():
    """No MCP env at all → ``mcp_servers`` stays empty (default capabilities)."""
    cfg = AgentConfig.from_env({"AGENT_MODEL": "x"})
    assert cfg.capabilities.mcp_servers == ()


def test_from_env_blank_rules_text_yields_default_rules():
    cfg = AgentConfig.from_env({"AGENT_RULES_TEXT": ""})
    assert cfg.capabilities.rules == AgentRules()


def test_capabilities_constructor_is_harness_friendly():
    """The harness builds ``AllCapabilities`` directly with named bindings —
    not via env reads — and passes it to ``AgentConfig(capabilities=...)``."""
    caps = AllCapabilities(
        mcp_servers=(McpBinding(name="gke", command=("/bin/mcp",), tools=("list",)),),
        skills=SkillBinding(paths=("/opt/skills",)),
        rules=AgentRules(text="you are a sre"),
    )
    cfg = AgentConfig(model="m", provider="p", capabilities=caps)
    assert cfg.capabilities is caps
    assert cfg.capabilities.mcp.name == "gke"
    assert cfg.capabilities.mcp.command == ("/bin/mcp",)
    assert cfg.capabilities.allowed_tools == ("list",)
    assert cfg.capabilities.skills.paths == ("/opt/skills",)
    assert cfg.capabilities.rules.text == "you are a sre"
