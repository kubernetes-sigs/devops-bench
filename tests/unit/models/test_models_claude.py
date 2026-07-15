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

"""Tests for the Claude adapter and its backend selection."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pytest_mock import MockerFixture

from devops_bench.core.errors import ConfigError, MissingDependencyError
from devops_bench.models import claude
from devops_bench.models.base import MODELS, get_model
from devops_bench.models.claude import ClaudeClientAdapter


def _make_tool(name: str, description: str, input_schema: dict) -> SimpleNamespace:
    return SimpleNamespace(name=name, description=description, inputSchema=input_schema)


# --- backend selection / construction ----------------------------------------


def test_init_selects_vertex_from_project(mocker: MockerFixture) -> None:
    client_cls = mocker.patch.object(claude, "AsyncAnthropicVertex")
    mocker.patch.dict(
        os.environ, {"GCP_PROJECT_ID": "proj", "GCP_VERTEX_LOCATION": "us-east5"}, clear=True
    )

    adapter = ClaudeClientAdapter()

    client_cls.assert_called_once_with(region="us-east5", project_id="proj")
    assert adapter.model_name == "claude-sonnet-4-5@20250929"


def test_init_defaults_to_vertex_and_warns(mocker: MockerFixture) -> None:
    mocker.patch.object(claude, "AsyncAnthropicVertex")
    warn = mocker.patch.object(claude._log, "warning")
    mocker.patch.dict(os.environ, {}, clear=True)

    adapter = ClaudeClientAdapter()

    warn.assert_called_once()
    assert adapter.model_name == "claude-sonnet-4-5@20250929"


def test_init_selects_api_from_key(mocker: MockerFixture) -> None:
    client_cls = mocker.patch.object(claude, "AsyncAnthropic")
    mocker.patch.dict(os.environ, {"AGENT_API_KEY": "sk-test"}, clear=True)

    adapter = ClaudeClientAdapter()

    client_cls.assert_called_once_with(api_key="sk-test")
    assert adapter.model_name == "claude-sonnet-4-5"


def test_init_api_key_takes_precedence_over_project(mocker: MockerFixture) -> None:
    api_cls = mocker.patch.object(claude, "AsyncAnthropic")
    vertex_cls = mocker.patch.object(claude, "AsyncAnthropicVertex")
    mocker.patch.dict(
        os.environ, {"AGENT_API_KEY": "sk-test", "GCP_PROJECT_ID": "proj"}, clear=True
    )

    ClaudeClientAdapter()

    api_cls.assert_called_once()
    vertex_cls.assert_not_called()


def test_init_selects_bedrock_from_aws_region(mocker: MockerFixture) -> None:
    client_cls = mocker.patch.object(claude, "AsyncAnthropicBedrock")
    mocker.patch.dict(
        os.environ, {"AWS_REGION": "us-west-2", "AGENT_MODEL": "anthropic.claude-x"}, clear=True
    )

    adapter = ClaudeClientAdapter()

    client_cls.assert_called_once_with(aws_region="us-west-2")
    assert adapter.model_name == "anthropic.claude-x"


def test_init_bedrock_requires_model(mocker: MockerFixture) -> None:
    mocker.patch.object(claude, "AsyncAnthropicBedrock")
    mocker.patch.dict(os.environ, {"AWS_REGION": "us-west-2"}, clear=True)

    with pytest.raises(ConfigError):
        ClaudeClientAdapter()


def test_init_backend_override_forces_vertex(mocker: MockerFixture) -> None:
    mocker.patch.object(claude, "AsyncAnthropic")
    vertex_cls = mocker.patch.object(claude, "AsyncAnthropicVertex")
    mocker.patch.dict(
        os.environ, {"AGENT_API_KEY": "sk-test", "ANTHROPIC_BACKEND": "vertex"}, clear=True
    )

    ClaudeClientAdapter()

    vertex_cls.assert_called_once()


def test_init_backend_override_invalid_raises(mocker: MockerFixture) -> None:
    mocker.patch.dict(os.environ, {"ANTHROPIC_BACKEND": "nope"}, clear=True)

    with pytest.raises(ConfigError):
        ClaudeClientAdapter()


def test_init_backend_arg_forces_vertex_over_api_key(mocker: MockerFixture) -> None:
    api_cls = mocker.patch.object(claude, "AsyncAnthropic")
    vertex_cls = mocker.patch.object(claude, "AsyncAnthropicVertex")
    # anthropic-vertex provider passes backend="vertex"; a stray API key must not
    # flip it to the api backend (decoupling Vertex auth from the key).
    mocker.patch.dict(
        os.environ, {"AGENT_API_KEY": "sk-test", "AGENT_MODEL": "claude-x"}, clear=True
    )

    ClaudeClientAdapter(backend="vertex")

    vertex_cls.assert_called_once()
    api_cls.assert_not_called()


def test_init_backend_arg_forces_bedrock(mocker: MockerFixture) -> None:
    bedrock_cls = mocker.patch.object(claude, "AsyncAnthropicBedrock")
    mocker.patch.dict(
        os.environ, {"AWS_REGION": "us-west-2", "AGENT_MODEL": "anthropic.claude-x"}, clear=True
    )

    ClaudeClientAdapter(backend="bedrock")

    bedrock_cls.assert_called_once_with(aws_region="us-west-2")


def test_init_backend_none_still_infers(mocker: MockerFixture) -> None:
    api_cls = mocker.patch.object(claude, "AsyncAnthropic")
    mocker.patch.dict(os.environ, {"AGENT_API_KEY": "sk-test"}, clear=True)

    ClaudeClientAdapter(backend=None)

    api_cls.assert_called_once_with(api_key="sk-test")


def test_init_without_sdk_raises(mocker: MockerFixture) -> None:
    mocker.patch.object(claude, "AsyncAnthropic", None)

    with pytest.raises(MissingDependencyError):
        ClaudeClientAdapter()


# --- format_tools -------------------------------------------------------------


def test_format_tools_shape(mocker: MockerFixture) -> None:
    mocker.patch.object(claude, "AsyncAnthropicVertex")
    adapter = ClaudeClientAdapter()
    schema = {"type": "object", "properties": {}}

    result = adapter.format_tools([_make_tool("t", "d", schema)])

    assert result == [{"name": "t", "description": "d", "input_schema": schema}]


# --- extract_function_calls ---------------------------------------------------


def test_extract_function_calls(mocker: MockerFixture) -> None:
    mocker.patch.object(claude, "AsyncAnthropicVertex")
    adapter = ClaudeClientAdapter()

    response = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="ignored"),
            SimpleNamespace(type="tool_use", name="fc", input={"a": 1}, id="tool-1"),
        ]
    )

    assert adapter.extract_function_calls(response) == [
        {"name": "fc", "args": {"a": 1}, "id": "tool-1"}
    ]


# --- get_text_content ---------------------------------------------------------


def test_get_text_content_concatenates(mocker: MockerFixture) -> None:
    mocker.patch.object(claude, "AsyncAnthropicVertex")
    adapter = ClaudeClientAdapter()

    response = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="hello "),
            SimpleNamespace(type="tool_use", name="x", input={}, id="i"),
            SimpleNamespace(type="text", text="world"),
        ]
    )

    assert adapter.get_text_content(response) == "hello world"


# --- generate_content ---------------------------------------------------------


def test_generate_content_passes_max_tokens_and_system(mocker: MockerFixture) -> None:
    client = mocker.patch.object(claude, "AsyncAnthropicVertex").return_value
    create = AsyncMock(return_value="resp")
    client.messages.create = create
    mocker.patch.dict(os.environ, {}, clear=True)

    adapter = ClaudeClientAdapter()
    tools = adapter.format_tools([_make_tool("t", "d", {"type": "object"})])

    result = asyncio.run(
        adapter.generate_content([{"role": "user", "content": "hello"}], tools, "be helpful")
    )

    assert result == "resp"
    create.assert_awaited_once()
    kwargs = create.await_args.kwargs
    assert kwargs["max_tokens"] == 16000
    assert kwargs["model"] == adapter.model_name
    assert kwargs["system"] == "be helpful"
    assert kwargs["messages"] == [{"role": "user", "content": "hello"}]
    assert kwargs["tools"] == tools


def test_generate_content_default_max_tokens(mocker: MockerFixture) -> None:
    client = mocker.patch.object(claude, "AsyncAnthropicVertex").return_value
    create = AsyncMock(return_value="resp")
    client.messages.create = create
    mocker.patch.dict(os.environ, {}, clear=True)

    adapter = ClaudeClientAdapter()
    asyncio.run(adapter.generate_content([{"role": "user", "content": "hi"}], [], None))

    assert create.await_args.kwargs["max_tokens"] == 16000


def test_generate_content_env_overrides_max_tokens(mocker: MockerFixture) -> None:
    client = mocker.patch.object(claude, "AsyncAnthropicVertex").return_value
    create = AsyncMock(return_value="resp")
    client.messages.create = create
    mocker.patch.dict(os.environ, {"AGENT_MAX_TOKENS": "12345"}, clear=True)

    adapter = ClaudeClientAdapter()
    asyncio.run(adapter.generate_content([{"role": "user", "content": "hi"}], [], None))

    assert adapter.max_tokens == 12345
    assert create.await_args.kwargs["max_tokens"] == 12345


def test_generate_content_arg_overrides_env_max_tokens(mocker: MockerFixture) -> None:
    client = mocker.patch.object(claude, "AsyncAnthropicVertex").return_value
    create = AsyncMock(return_value="resp")
    client.messages.create = create
    mocker.patch.dict(os.environ, {"AGENT_MAX_TOKENS": "12345"}, clear=True)

    adapter = ClaudeClientAdapter(max_tokens=999)
    asyncio.run(adapter.generate_content([{"role": "user", "content": "hi"}], [], None))

    assert adapter.max_tokens == 999
    assert create.await_args.kwargs["max_tokens"] == 999


def test_generate_content_omits_system_when_empty(mocker: MockerFixture) -> None:
    client = mocker.patch.object(claude, "AsyncAnthropicVertex").return_value
    create = AsyncMock(return_value="resp")
    client.messages.create = create
    mocker.patch.dict(os.environ, {}, clear=True)

    adapter = ClaudeClientAdapter()
    asyncio.run(adapter.generate_content([{"role": "user", "content": "hi"}], [], None))

    assert "system" not in create.await_args.kwargs


def test_generate_content_omits_tools_when_empty(mocker: MockerFixture) -> None:
    client = mocker.patch.object(claude, "AsyncAnthropicVertex").return_value
    create = AsyncMock(return_value="resp")
    client.messages.create = create
    mocker.patch.dict(os.environ, {}, clear=True)

    adapter = ClaudeClientAdapter()
    asyncio.run(adapter.generate_content([{"role": "user", "content": "hi"}], [], "sys"))

    assert "tools" not in create.await_args.kwargs


def test_convert_messages_tool_calls_and_results(mocker: MockerFixture) -> None:
    mocker.patch.object(claude, "AsyncAnthropicVertex")
    adapter = ClaudeClientAdapter()

    contents = [
        {"role": "user", "content": "do it"},
        {
            "role": "assistant",
            "content": "thinking",
            "tool_calls": [{"id": "c1", "name": "fc", "args": {"k": "v"}}],
        },
        {"role": "tool", "content": "result", "tool_call_id": "c1"},
    ]

    messages = adapter._convert_to_anthropic_messages(contents)

    assert messages[0] == {"role": "user", "content": "do it"}
    assert messages[1]["content"][0] == {"type": "text", "text": "thinking"}
    assert messages[1]["content"][1] == {
        "type": "tool_use",
        "id": "c1",
        "name": "fc",
        "input": {"k": "v"},
    }
    assert messages[2]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "c1",
        "content": "result",
    }


def test_convert_messages_groups_parallel_tool_results(mocker: MockerFixture) -> None:
    mocker.patch.object(claude, "AsyncAnthropicVertex")
    adapter = ClaudeClientAdapter()

    contents = [
        {"role": "user", "content": "do it"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "name": "fc1", "args": {}},
                {"id": "c2", "name": "fc2", "args": {}},
            ],
        },
        {"role": "tool", "content": "r1", "tool_call_id": "c1"},
        {"role": "tool", "content": "r2", "tool_call_id": "c2"},
    ]

    messages = adapter._convert_to_anthropic_messages(contents)

    # The two tool results must collapse into a single trailing user message
    # with two tool_result blocks (no back-to-back user turns).
    assert len(messages) == 3
    assert messages[2]["role"] == "user"
    assert messages[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "c1", "content": "r1"},
        {"type": "tool_result", "tool_use_id": "c2", "content": "r2"},
    ]


# --- registry / provider resolution -------------------------------------------


def test_registered_in_models_registry() -> None:
    assert MODELS.get("claude") is ClaudeClientAdapter


@pytest.mark.parametrize(
    "provider", ["claude", "anthropic", "anthropic-vertex", "anthropic_vertex"]
)
def test_get_model_resolves_claude_aliases(mocker: MockerFixture, provider: str) -> None:
    mocker.patch.object(claude, "AsyncAnthropicVertex")
    mocker.patch.dict(os.environ, {"GCP_PROJECT_ID": "proj"}, clear=True)

    client = get_model(provider=provider)

    assert isinstance(client, ClaudeClientAdapter)
