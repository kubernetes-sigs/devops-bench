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

import json
from pathlib import Path

import pytest

from devops_bench.agents.capabilities import (
    AgentRules,
    AllCapabilities,
    McpBinding,
    SkillBinding,
)
from devops_bench.agents.config import AgentConfig
from devops_bench.core import ConfigError


def test_default_construction_uses_safe_defaults() -> None:
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


def test_from_env_maps_each_field() -> None:
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


def test_from_env_treats_unset_as_defaults() -> None:
    cfg = AgentConfig.from_env({})
    assert cfg.model is None
    assert cfg.provider is None
    assert cfg.timeout_sec == 600.0
    assert cfg.max_turns is None
    assert cfg.capabilities == AllCapabilities()


def test_from_env_blank_allowed_tools_yields_empty_tuple() -> None:
    cfg = AgentConfig.from_env({"AGENT_ALLOWED_TOOLS": ""})
    assert cfg.capabilities.mcp_servers == ()  # blank → no binding at all
    cfg = AgentConfig.from_env({"AGENT_ALLOWED_TOOLS": " , , "})
    assert cfg.capabilities.mcp_servers == ()


def test_from_env_allowed_tools_only_builds_mcp_binding_with_empty_command() -> None:
    """A CLI agent (Gemini) gets an MCP binding with no launch command — the
    binary launches MCP in-process — but still carries the tools allow-list."""
    cfg = AgentConfig.from_env({"AGENT_ALLOWED_TOOLS": "alpha,beta"})
    assert cfg.capabilities.mcp_servers == (
        McpBinding(name="default", command=(), tools=("alpha", "beta")),
    )
    assert cfg.capabilities.allowed_tools == ("alpha", "beta")
    assert cfg.capabilities.tools_enabled is True


def test_from_env_mcp_server_only_builds_binding_with_no_tools() -> None:
    """The API agent path: a server command but no pre-approved tool list."""
    cfg = AgentConfig.from_env({"AGENT_MCP_SERVER": "/usr/local/bin/mcp-server"})
    assert cfg.capabilities.mcp_servers == (
        McpBinding(name="default", command=("/usr/local/bin/mcp-server",), tools=()),
    )
    assert cfg.capabilities.allowed_tools == ()


def test_from_env_skips_mcp_binding_when_neither_command_nor_tools_set() -> None:
    """No MCP env at all → ``mcp_servers`` stays empty (default capabilities)."""
    cfg = AgentConfig.from_env({"AGENT_MODEL": "x"})
    assert cfg.capabilities.mcp_servers == ()


def test_from_env_blank_rules_text_yields_default_rules() -> None:
    cfg = AgentConfig.from_env({"AGENT_RULES_TEXT": ""})
    assert cfg.capabilities.rules == AgentRules()


def test_capabilities_constructor_is_harness_friendly() -> None:
    """The harness builds ``AllCapabilities`` directly with named bindings —
    not via env reads — and passes it to ``AgentConfig(capabilities=...)``."""
    caps = AllCapabilities(
        mcp_servers=(McpBinding(name="k8s", command=("/bin/mcp",), tools=("list",)),),
        skills=SkillBinding(paths=("/opt/skills",)),
        rules=AgentRules(text="you are a sre"),
    )
    cfg = AgentConfig(model="m", provider="p", capabilities=caps)
    assert cfg.capabilities is caps
    assert cfg.capabilities.mcp.name == "k8s"
    assert cfg.capabilities.mcp.command == ("/bin/mcp",)
    assert cfg.capabilities.allowed_tools == ("list",)
    assert cfg.capabilities.skills.paths == ("/opt/skills",)
    assert cfg.capabilities.rules.text == "you are a sre"


def test_from_env_extra_flags() -> None:
    cfg = AgentConfig.from_env({"AGENT_EXTRA_FLAGS": "--flag1 --opt=value --quoted='hello world'"})
    assert cfg.extra_flags == ("--flag1", "--opt=value", "--quoted=hello world")


def test_from_env_extra_flags_unset_or_empty() -> None:
    assert AgentConfig.from_env({}).extra_flags == ()
    assert AgentConfig.from_env({"AGENT_EXTRA_FLAGS": ""}).extra_flags == ()
    assert AgentConfig.from_env({"AGENT_EXTRA_FLAGS": "   "}).extra_flags == ()


def test_from_env_parses_inline_mcp_config_document() -> None:
    """``AGENT_MCP_CONFIG`` accepts the standard ``mcpServers`` document inline,
    so a one-off run needs no config file."""
    env = {
        "AGENT_MCP_CONFIG": json.dumps(
            {
                "mcpServers": {
                    "time": {"command": "uvx", "args": ["mcp-server-time"]},
                    "github": {
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-github"],
                        "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"},
                        "cwd": "/work",
                        "tools": ["create_issue"],
                    },
                }
            }
        )
    }
    cfg = AgentConfig.from_env(env)

    assert cfg.capabilities.mcp_servers == (
        McpBinding(name="time", command=("uvx", "mcp-server-time")),
        McpBinding(
            name="github",
            command=("npx", "-y", "@modelcontextprotocol/server-github"),
            env=(("GITHUB_TOKEN", "${GITHUB_TOKEN}"),),
            cwd="/work",
            tools=("create_issue",),
        ),
    )


def test_from_env_reads_mcp_config_from_a_path(tmp_path: Path) -> None:
    """A value that is not inline JSON is a path — the form the matrix uses,
    where a quoted JSON blob would not survive its ';'-joined, eval'd env."""
    config = tmp_path / "bench-mcp.json"
    config.write_text(json.dumps({"mcpServers": {"time": {"command": "uvx"}}}))

    cfg = AgentConfig.from_env({"AGENT_MCP_CONFIG": str(config)})

    assert cfg.capabilities.mcp_servers == (McpBinding(name="time", command=("uvx",)),)


def test_from_env_mcp_config_servers_inherit_allowed_tools() -> None:
    """A server declaring no ``tools`` inherits ``AGENT_ALLOWED_TOOLS``."""
    env = {
        "AGENT_MCP_CONFIG": '{"mcpServers": {"time": {"command": "uvx"}}}',
        "AGENT_ALLOWED_TOOLS": "a,b",
    }
    cfg = AgentConfig.from_env(env)

    assert cfg.capabilities.mcp_servers[0].tools == ("a", "b")


def test_from_env_mcp_config_takes_precedence_over_the_shorthand() -> None:
    """``AGENT_MCP_SERVER`` is the single-server shorthand; the document wins."""
    env = {
        "AGENT_MCP_CONFIG": '{"mcpServers": {"time": {"command": "uvx"}}}',
        "AGENT_MCP_SERVER": "/bin/gke-mcp",
    }
    cfg = AgentConfig.from_env(env)

    assert [b.name for b in cfg.capabilities.mcp_servers] == ["time"]


def test_from_env_blank_mcp_config_falls_back_to_the_shorthand() -> None:
    """An empty value is treated as unset, not as a malformed document."""
    env = {"AGENT_MCP_CONFIG": "  ", "AGENT_MCP_SERVER": "/bin/gke-mcp"}
    cfg = AgentConfig.from_env(env)

    assert cfg.capabilities.mcp_servers == (McpBinding(name="default", command=("/bin/gke-mcp",)),)


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ("{not json", "not valid JSON"),
        ('{"servers": {}}', "no 'mcpServers' key"),
        ('{"mcpServers": []}', "must be a JSON object"),
        ('{"mcpServers": {"a": "uvx"}}', "must be a JSON object"),
        ('{"mcpServers": {"a": {}}}', "command: Field required"),
        ('{"mcpServers": {"a": {"command": ""}}}', "command: String should have at least 1"),
        # An all-whitespace command clears min_length but splits to no argv at
        # all, which is the same "MCP arm with no MCP server" the empty case is
        # rejected for.
        (
            '{"mcpServers": {"a": {"command": "   "}}}',
            "command: String should match pattern",
        ),
        (
            '{"mcpServers": {"a": {"command": "uvx", "args": "x"}}}',
            "args: Input should be a valid list",
        ),
        (
            '{"mcpServers": {"a": {"command": "uvx", "env": {"K": 1}}}}',
            "env.K: Input should be a valid string",
        ),
        (
            '{"mcpServers": {"a": {"command": "uvx", "tools": "x"}}}',
            "tools: Input should be a valid list",
        ),
        (
            '{"mcpServers": {"a": {"command": "uvx", "cwd": ["/tmp"]}}}',
            "cwd: Input should be a valid string",
        ),
        # A falsy wrong type must raise rather than be coerced or fall back to
        # the default: dropping a declared env or argv leaves the server
        # launching without its credential, and the probe then reports the wrong
        # cause. Strict mode is what makes ``0`` an error instead of ``"0"``.
        (
            '{"mcpServers": {"a": {"command": "uvx", "args": 0}}}',
            "args: Input should be a valid list",
        ),
        (
            '{"mcpServers": {"a": {"command": "uvx", "env": 0}}}',
            "env: Input should be a valid dict",
        ),
        (
            '{"mcpServers": {"a": {"command": "uvx", "cwd": 0}}}',
            "cwd: Input should be a valid string",
        ),
        ('{"mcpServers": {}}', "is empty"),
        ("/nonexistent/bench-mcp.json", "unreadable"),
    ],
)
def test_from_env_malformed_mcp_config_fails_loud(raw: str, match: str) -> None:
    """A broken capability grant raises rather than running an MCP arm with no
    MCP server — the same validity concern the reachability probe addresses."""
    with pytest.raises(ConfigError, match=match):
        AgentConfig.from_env({"AGENT_MCP_CONFIG": raw})


def test_from_env_mcp_config_file_must_hold_a_json_object(tmp_path: Path) -> None:
    """Only the inline form is recognized by its leading ``{``; a file is read
    first and then validated, so a non-object document fails there."""
    config = tmp_path / "bench-mcp.json"
    config.write_text("[]")

    with pytest.raises(ConfigError, match="must be a JSON object"):
        AgentConfig.from_env({"AGENT_MCP_CONFIG": str(config)})


@pytest.mark.parametrize(
    "key",
    ["GITHUB_TOKEN", "API_KEY", "CLIENT_SECRET", "DB_PASSWORD", "GCP_CREDENTIALS"],
)
def test_from_env_rejects_a_literal_in_a_secret_named_mcp_env_value(key: str) -> None:
    """A pasted credential must fail the config, not ride into the artifacts.

    The declared env is written into the agent's workspace config and the
    harness collects that workspace wholesale into the run's results, so a
    literal secret is persisted. A ``${VAR}`` reference is resolved only for the
    server's own process.
    """
    raw = json.dumps({"mcpServers": {"gh": {"command": "uvx", "env": {key: "ghp_literal"}}}})

    with pytest.raises(ConfigError, match="looks like a credential"):
        AgentConfig.from_env({"AGENT_MCP_CONFIG": raw})


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("GITHUB_TOKEN", "${GITHUB_TOKEN}"),
        ("AUTH_HEADER_KEY", "Bearer ${GH_PAT}"),
        # Not credential-shaped: rejecting every literal would break ordinary
        # configuration, which is the common case.
        ("NODE_ENV", "production"),
        # An empty value carries nothing to leak.
        ("API_KEY", ""),
    ],
)
def test_from_env_accepts_references_and_non_secret_literals(key: str, value: str) -> None:
    """The guard fires on secret-named keys holding a literal, and nothing else."""
    raw = json.dumps({"mcpServers": {"gh": {"command": "uvx", "env": {key: value}}}})

    cfg = AgentConfig.from_env({"AGENT_MCP_CONFIG": raw})

    assert cfg.capabilities.mcp_servers[0].env == ((key, value),)


def test_from_env_undecodable_mcp_config_file_raises_config_error(tmp_path: Path) -> None:
    """``from_env`` documents ``ConfigError`` as its only failure, so a file that
    is not UTF-8 must not surface the raw ``UnicodeDecodeError``."""
    config = tmp_path / "bench-mcp.json"
    config.write_bytes(b'{"mcpServers": {"a": {"command": "\xff\xfe"}}}')

    with pytest.raises(ConfigError, match="not valid UTF-8"):
        AgentConfig.from_env({"AGENT_MCP_CONFIG": str(config)})
