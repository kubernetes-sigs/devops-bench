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

"""Tests for the OpenAI-compatible adapter."""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pytest_mock import MockerFixture

from devops_bench.core.errors import ConfigError, MissingDependencyError
from devops_bench.models import openai
from devops_bench.models.base import MODELS, get_model
from devops_bench.models.openai import OpenAIClientAdapter


def _make_tool(name: str, description: str, input_schema: dict) -> SimpleNamespace:
    return SimpleNamespace(name=name, description=description, inputSchema=input_schema)


# --- construction -------------------------------------------------------------


def test_init_reads_env(mocker: MockerFixture) -> None:
    client_cls = mocker.patch.object(openai, "AsyncOpenAI")
    mocker.patch.dict(
        os.environ,
        {
            "AGENT_MODEL": "qwen3",
            "OPENAI_BASE_URL": "http://localhost:8000/v1",
            "AGENT_MAX_TOKENS": "4096",
        },
        clear=True,
    )

    adapter = OpenAIClientAdapter()

    client_cls.assert_called_once_with(base_url="http://localhost:8000/v1", api_key="unused")
    assert adapter.model_name == "qwen3"
    assert adapter.max_tokens == 4096


def test_init_defaults_to_openai_api_and_default_max_tokens(mocker: MockerFixture) -> None:
    client_cls = mocker.patch.object(openai, "AsyncOpenAI")
    mocker.patch.dict(os.environ, {"AGENT_MODEL": "gpt-5"}, clear=True)

    adapter = OpenAIClientAdapter()

    client_cls.assert_called_once_with(base_url=None, api_key="unused")
    assert adapter.max_tokens == 16000


def test_init_args_override_env(mocker: MockerFixture) -> None:
    client_cls = mocker.patch.object(openai, "AsyncOpenAI")
    mocker.patch.dict(
        os.environ,
        {"AGENT_MODEL": "qwen3", "OPENAI_BASE_URL": "http://env/v1", "AGENT_MAX_TOKENS": "1"},
        clear=True,
    )

    adapter = OpenAIClientAdapter(model_name="gemma", base_url="http://arg/v1", max_tokens=2)

    client_cls.assert_called_once_with(base_url="http://arg/v1", api_key="unused")
    assert adapter.model_name == "gemma"
    assert adapter.max_tokens == 2


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"AGENT_API_KEY": "agent", "OPENAI_API_KEY": "vendor"}, "agent"),
        ({"OPENAI_API_KEY": "vendor"}, "vendor"),
    ],
)
def test_init_api_key_precedence(mocker: MockerFixture, env: dict[str, str], expected: str) -> None:
    client_cls = mocker.patch.object(openai, "AsyncOpenAI")
    mocker.patch.dict(os.environ, env, clear=True)

    OpenAIClientAdapter(model_name="m")

    assert client_cls.call_args.kwargs["api_key"] == expected


def test_init_without_model_raises(mocker: MockerFixture) -> None:
    mocker.patch.object(openai, "AsyncOpenAI")
    mocker.patch.dict(os.environ, {}, clear=True)

    with pytest.raises(ConfigError, match="AGENT_MODEL"):
        OpenAIClientAdapter()


def test_init_without_sdk_raises(mocker: MockerFixture) -> None:
    mocker.patch.object(openai, "AsyncOpenAI", None)

    with pytest.raises(MissingDependencyError):
        OpenAIClientAdapter(model_name="m")


# --- format_tools -------------------------------------------------------------


def test_format_tools_shape(mocker: MockerFixture) -> None:
    mocker.patch.object(openai, "AsyncOpenAI")
    adapter = OpenAIClientAdapter(model_name="m")
    schema = {"type": "object", "properties": {}}

    result = adapter.format_tools([_make_tool("t", "d", schema)])

    assert result == [
        {"type": "function", "function": {"name": "t", "description": "d", "parameters": schema}}
    ]


# --- extract_function_calls ---------------------------------------------------


def test_extract_function_calls_parses_json_args(mocker: MockerFixture) -> None:
    mocker.patch.object(openai, "AsyncOpenAI")
    adapter = OpenAIClientAdapter(model_name="m")

    tool_call = SimpleNamespace(
        id="call-1", function=SimpleNamespace(name="fc", arguments='{"a": 1}')
    )
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[tool_call]))]
    )

    assert adapter.extract_function_calls(response) == [
        {"name": "fc", "args": {"a": 1}, "id": "call-1"}
    ]


def test_extract_function_calls_invalid_json_falls_back_to_empty(mocker: MockerFixture) -> None:
    mocker.patch.object(openai, "AsyncOpenAI")
    adapter = OpenAIClientAdapter(model_name="m")

    tool_call = SimpleNamespace(id="c", function=SimpleNamespace(name="fc", arguments="not-json"))
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[tool_call]))]
    )

    assert adapter.extract_function_calls(response) == [{"name": "fc", "args": {}, "id": "c"}]


def test_extract_function_calls_none(mocker: MockerFixture) -> None:
    mocker.patch.object(openai, "AsyncOpenAI")
    adapter = OpenAIClientAdapter(model_name="m")

    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=None))])

    assert adapter.extract_function_calls(response) == []


# --- get_text_content ---------------------------------------------------------


def test_get_text_content(mocker: MockerFixture) -> None:
    mocker.patch.object(openai, "AsyncOpenAI")
    adapter = OpenAIClientAdapter(model_name="m")

    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="hello"))])
    assert adapter.get_text_content(response) == "hello"


def test_get_text_content_empty(mocker: MockerFixture) -> None:
    mocker.patch.object(openai, "AsyncOpenAI")
    adapter = OpenAIClientAdapter(model_name="m")

    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None))])
    assert adapter.get_text_content(response) == ""


# --- generate_content ---------------------------------------------------------


def test_generate_content_passes_model_and_tools(mocker: MockerFixture) -> None:
    client = mocker.patch.object(openai, "AsyncOpenAI").return_value
    create = AsyncMock(return_value="resp")
    client.chat.completions.create = create

    adapter = OpenAIClientAdapter(model_name="m")
    tools = adapter.format_tools([_make_tool("t", "d", {"type": "object"})])

    result = asyncio.run(
        adapter.generate_content([{"role": "user", "content": "hi"}], tools, "be helpful")
    )

    assert result == "resp"
    kwargs = create.await_args.kwargs
    assert kwargs["model"] == adapter.model_name
    assert kwargs["max_completion_tokens"] == adapter.max_tokens
    assert kwargs["tools"] == tools
    assert kwargs["messages"][0] == {"role": "system", "content": "be helpful"}
    assert kwargs["messages"][1] == {"role": "user", "content": "hi"}


def test_generate_content_omits_tools_when_empty(mocker: MockerFixture) -> None:
    client = mocker.patch.object(openai, "AsyncOpenAI").return_value
    create = AsyncMock(return_value="resp")
    client.chat.completions.create = create

    adapter = OpenAIClientAdapter(model_name="m")
    asyncio.run(adapter.generate_content([{"role": "user", "content": "hi"}], [], None))

    assert "tools" not in create.await_args.kwargs


# --- message conversion -------------------------------------------------------


def test_convert_messages_tool_calls_and_results(mocker: MockerFixture) -> None:
    mocker.patch.object(openai, "AsyncOpenAI")
    adapter = OpenAIClientAdapter(model_name="m")

    contents = [
        {"role": "user", "content": "do it"},
        {
            "role": "assistant",
            "content": "thinking",
            "tool_calls": [{"id": "c1", "name": "fc", "args": {"k": "v"}}],
        },
        {"role": "tool", "content": "result", "tool_call_id": "c1"},
    ]

    messages = adapter._convert_to_openai_messages(contents, "sys")

    assert messages[0] == {"role": "system", "content": "sys"}
    assert messages[1] == {"role": "user", "content": "do it"}
    assert messages[2]["role"] == "assistant"
    assert messages[2]["content"] == "thinking"
    assert messages[2]["tool_calls"][0]["id"] == "c1"
    assert messages[2]["tool_calls"][0]["function"]["name"] == "fc"
    assert json.loads(messages[2]["tool_calls"][0]["function"]["arguments"]) == {"k": "v"}
    assert messages[3] == {"role": "tool", "tool_call_id": "c1", "content": "result"}


def test_convert_messages_synthesizes_tool_call_id(mocker: MockerFixture) -> None:
    mocker.patch.object(openai, "AsyncOpenAI")
    adapter = OpenAIClientAdapter(model_name="m")

    contents = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"name": "fc", "args": {}}],
        },
    ]

    messages = adapter._convert_to_openai_messages(contents, None)

    assert messages[0]["tool_calls"][0]["id"] == "call_0"


def test_convert_messages_no_system_when_absent(mocker: MockerFixture) -> None:
    mocker.patch.object(openai, "AsyncOpenAI")
    adapter = OpenAIClientAdapter(model_name="m")

    messages = adapter._convert_to_openai_messages([{"role": "user", "content": "hi"}], None)

    assert messages == [{"role": "user", "content": "hi"}]


# --- registry / provider resolution -------------------------------------------


def test_registered_in_models_registry() -> None:
    assert MODELS.get("openai") is OpenAIClientAdapter


def test_get_model_builds_openai_adapter(mocker: MockerFixture) -> None:
    mocker.patch.object(openai, "AsyncOpenAI")
    mocker.patch.dict(os.environ, {"AGENT_MODEL": "m"}, clear=True)

    client = get_model(provider="openai")

    assert isinstance(client, OpenAIClientAdapter)
