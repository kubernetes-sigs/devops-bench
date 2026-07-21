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

"""Tests for the Gemini (google-genai) adapter."""

from __future__ import annotations

import asyncio
import base64
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pytest_mock import MockerFixture

from devops_bench.models import gemini
from devops_bench.models.base import MODELS, get_model
from devops_bench.models.gemini import GeminiClientAdapter, filter_schema_for_gemini


def _make_tool(name: str, description: str, input_schema: dict) -> SimpleNamespace:
    return SimpleNamespace(name=name, description=description, inputSchema=input_schema)


# --- filter_schema_for_gemini -------------------------------------------------


def test_filter_schema_upper_cases_type_and_drops_unsupported() -> None:
    schema = {
        "type": "object",
        "title": "ignored",
        "properties": {
            "count": {"type": "integer", "description": "a count"},
            "name": {"type": ["string", "null"]},
        },
        "required": ["count"],
    }

    result = filter_schema_for_gemini(schema)

    assert result["type"] == "OBJECT"
    assert "title" not in result
    assert result["properties"]["count"] == {"type": "INTEGER", "description": "a count"}
    # Nullable union collapses to nullable + the non-null type.
    assert result["properties"]["name"] == {"type": "STRING", "nullable": True}
    assert result["required"] == ["count"]


def test_filter_schema_bool_inputs() -> None:
    assert filter_schema_for_gemini(True) == {}
    assert filter_schema_for_gemini(False) is None


def test_filter_schema_normalizes_ref_and_defs_aliases() -> None:
    """``$defs``/``$ref`` are rewritten to ``defs``/``ref`` — google-genai's
    Schema rejects the ``$``-prefixed spellings with a validation error — and
    pointer values into the renamed container are rewritten to match
    (``ref`` resolves against the root ``defs`` per the Schema field docs)."""
    schema = {
        "type": "object",
        "properties": {"x": {"$ref": "#/$defs/item"}},
        "$defs": {"item": {"type": "string"}},
    }

    result = filter_schema_for_gemini(schema)

    assert result["defs"] == {"item": {"type": "STRING"}}
    assert result["properties"]["x"] == {"ref": "#/defs/item"}
    assert "$defs" not in result
    assert "$ref" not in result["properties"]["x"]


def test_filter_schema_collapses_oneof_into_anyof() -> None:
    """google-genai's Schema has no oneOf field at all; both spellings map to
    anyOf, and a oneOf merges into an anyOf already present."""
    schema = {
        "anyOf": [{"type": "string"}],
        "oneOf": [{"type": "integer"}],
        "one_of": [{"type": "boolean"}],
    }

    result = filter_schema_for_gemini(schema)

    assert "oneOf" not in result
    assert "one_of" not in result
    assert result["anyOf"] == [{"type": "STRING"}, {"type": "INTEGER"}, {"type": "BOOLEAN"}]


def test_filtered_schema_is_accepted_by_the_sdk() -> None:
    """Ground truth: the filtered output of an alias-heavy schema must construct
    a FunctionDeclaration without a pydantic validation error."""
    from google.genai import types

    schema = {
        "type": "object",
        "properties": {
            "target": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
            "item": {"$ref": "#/$defs/item"},
        },
        "$defs": {"item": {"type": "string"}},
    }

    types.FunctionDeclaration(name="t", parameters=filter_schema_for_gemini(schema))


# --- client selection by env --------------------------------------------------


def test_client_selection_api_key(mocker: MockerFixture) -> None:
    client_cls = mocker.patch.object(gemini.genai, "Client")
    mocker.patch.dict(os.environ, {"AGENT_API_KEY": "k", "GCP_PROJECT_ID": "p"}, clear=True)

    GeminiClientAdapter()

    client_cls.assert_called_once_with(api_key="k")


def test_client_selection_vertex(mocker: MockerFixture) -> None:
    client_cls = mocker.patch.object(gemini.genai, "Client")
    mocker.patch.dict(
        os.environ, {"GCP_PROJECT_ID": "proj", "GCP_VERTEX_LOCATION": "europe-west1"}, clear=True
    )

    GeminiClientAdapter()

    client_cls.assert_called_once_with(vertexai=True, project="proj", location="europe-west1")


def test_client_selection_default(mocker: MockerFixture) -> None:
    client_cls = mocker.patch.object(gemini.genai, "Client")
    mocker.patch.dict(os.environ, {}, clear=True)

    adapter = GeminiClientAdapter()

    client_cls.assert_called_once_with()
    assert adapter.model_name == "gemini-3.1-pro-preview"


def test_backend_vertex_forces_vertex_even_with_api_key(mocker: MockerFixture) -> None:
    client_cls = mocker.patch.object(gemini.genai, "Client")
    # An API key is present, but backend="vertex" (the google-vertex provider)
    # must still build the Vertex client — provider decides, not key presence.
    mocker.patch.dict(os.environ, {"AGENT_API_KEY": "k", "GCP_PROJECT_ID": "proj"}, clear=True)

    GeminiClientAdapter(backend="vertex")

    client_cls.assert_called_once_with(vertexai=True, project="proj", location="global")


def test_backend_none_keeps_inference(mocker: MockerFixture) -> None:
    client_cls = mocker.patch.object(gemini.genai, "Client")
    mocker.patch.dict(os.environ, {"AGENT_API_KEY": "k"}, clear=True)

    GeminiClientAdapter(backend=None)

    client_cls.assert_called_once_with(api_key="k")


# --- format_tools -------------------------------------------------------------


def test_format_tools_applies_filter(mocker: MockerFixture) -> None:
    mocker.patch.object(gemini.genai, "Client")
    adapter = GeminiClientAdapter()
    tool = _make_tool("do_thing", "does a thing", {"type": "object", "title": "drop"})

    result = adapter.format_tools([tool])

    decl = result.function_declarations[0]
    assert decl.name == "do_thing"
    assert decl.description == "does a thing"
    # filter_schema_for_gemini upper-cased the type and dropped unsupported keys.
    assert decl.parameters.type == "OBJECT"


# --- extract_function_calls ---------------------------------------------------


def test_extract_function_calls(mocker: MockerFixture) -> None:
    mocker.patch.object(gemini.genai, "Client")
    adapter = GeminiClientAdapter()

    part = SimpleNamespace(
        function_call=SimpleNamespace(name="fc", args={"x": 1}),
        thought_signature=b"sig",
    )
    response = SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))])

    calls = adapter.extract_function_calls(response)

    assert calls == [
        {
            "name": "fc",
            "args": {"x": 1},
            "id": None,
            "thought_signature": base64.b64encode(b"sig").decode("utf-8"),
        }
    ]


def test_extract_function_calls_no_candidates(mocker: MockerFixture) -> None:
    mocker.patch.object(gemini.genai, "Client")
    adapter = GeminiClientAdapter()

    assert adapter.extract_function_calls(SimpleNamespace(candidates=None)) == []


# --- get_text_content ---------------------------------------------------------


def test_get_text_content(mocker: MockerFixture) -> None:
    mocker.patch.object(gemini.genai, "Client")
    adapter = GeminiClientAdapter()

    assert adapter.get_text_content(SimpleNamespace(text="hi")) == "hi"
    assert adapter.get_text_content(SimpleNamespace(text=None)) == ""


# --- generate_content ---------------------------------------------------------


def test_generate_content_invokes_sdk(mocker: MockerFixture) -> None:
    client = mocker.patch.object(gemini.genai, "Client").return_value
    generate = AsyncMock(return_value="resp")
    client.aio.models.generate_content = generate

    adapter = GeminiClientAdapter()
    tools = adapter.format_tools([_make_tool("t", "d", {"type": "object", "properties": {}})])

    result = asyncio.run(
        adapter.generate_content([{"role": "user", "content": "hello"}], tools, "be helpful")
    )

    assert result == "resp"
    generate.assert_awaited_once()
    kwargs = generate.await_args.kwargs
    assert kwargs["model"] == adapter.model_name
    assert kwargs["contents"]  # converted gemini messages


class _FakeAPIError(Exception):
    """SDK-like error carrying an HTTP status ``code``."""

    def __init__(self, code: int, message: str = "") -> None:
        super().__init__(message or str(code))
        self.code = code


def test_generate_content_retries_on_429_then_succeeds(mocker: MockerFixture) -> None:
    client = mocker.patch.object(gemini.genai, "Client").return_value
    sleep = mocker.patch.object(gemini.asyncio, "sleep", new=AsyncMock())
    generate = AsyncMock(side_effect=[_FakeAPIError(429, "RESOURCE_EXHAUSTED"), "resp"])
    client.aio.models.generate_content = generate

    adapter = GeminiClientAdapter()
    result = asyncio.run(adapter.generate_content([{"role": "user", "content": "hi"}], None, None))

    assert result == "resp"
    assert generate.await_count == 2
    sleep.assert_awaited_once()


def test_generate_content_does_not_retry_non_retryable(mocker: MockerFixture) -> None:
    client = mocker.patch.object(gemini.genai, "Client").return_value
    sleep = mocker.patch.object(gemini.asyncio, "sleep", new=AsyncMock())
    generate = AsyncMock(side_effect=_FakeAPIError(400, "INVALID_ARGUMENT"))
    client.aio.models.generate_content = generate

    adapter = GeminiClientAdapter()
    try:
        asyncio.run(adapter.generate_content([{"role": "user", "content": "hi"}], None, None))
    except _FakeAPIError as exc:
        assert exc.code == 400
    else:  # pragma: no cover - the call must propagate
        raise AssertionError("expected the non-retryable error to propagate")

    assert generate.await_count == 1
    sleep.assert_not_awaited()


def test_generate_content_gives_up_after_max_retries(mocker: MockerFixture) -> None:
    client = mocker.patch.object(gemini.genai, "Client").return_value
    mocker.patch.object(gemini.asyncio, "sleep", new=AsyncMock())
    generate = AsyncMock(side_effect=_FakeAPIError(503, "UNAVAILABLE"))
    client.aio.models.generate_content = generate

    adapter = GeminiClientAdapter()
    try:
        asyncio.run(adapter.generate_content([{"role": "user", "content": "hi"}], None, None))
    except _FakeAPIError as exc:
        assert exc.code == 503
    else:  # pragma: no cover
        raise AssertionError("expected the error to propagate after retries")

    assert generate.await_count == gemini._MAX_RETRIES + 1


def test_generate_content_includes_system_instruction(mocker: MockerFixture) -> None:
    client = mocker.patch.object(gemini.genai, "Client").return_value
    captured: dict = {}

    def fake_config(**config_args):
        captured.update(config_args)
        return SimpleNamespace(**config_args)

    mocker.patch.object(gemini.types, "GenerateContentConfig", side_effect=fake_config)
    client.aio.models.generate_content = AsyncMock(return_value="resp")

    adapter = GeminiClientAdapter()
    asyncio.run(
        adapter.generate_content([{"role": "user", "content": "hello"}], None, "be helpful")
    )

    assert captured["system_instruction"] == "be helpful"


def test_generate_content_omits_system_instruction_when_none(mocker: MockerFixture) -> None:
    client = mocker.patch.object(gemini.genai, "Client").return_value
    captured: dict = {}

    def fake_config(**config_args):
        captured.update(config_args)
        return SimpleNamespace(**config_args)

    mocker.patch.object(gemini.types, "GenerateContentConfig", side_effect=fake_config)
    client.aio.models.generate_content = AsyncMock(return_value="resp")

    adapter = GeminiClientAdapter()
    asyncio.run(adapter.generate_content([{"role": "user", "content": "hello"}], None, None))

    assert "system_instruction" not in captured


# --- _convert_to_gemini_messages ----------------------------------------------


def test_convert_messages_groups_parallel_tool_results(mocker: MockerFixture) -> None:
    mocker.patch.object(gemini.genai, "Client")
    adapter = GeminiClientAdapter()

    contents = [
        {"role": "user", "content": "do it"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"name": "fc1", "args": {}},
                {"name": "fc2", "args": {}},
            ],
        },
        {"role": "tool", "name": "fc1", "content": "r1"},
        {"role": "tool", "name": "fc2", "content": "r2"},
    ]

    gemini_contents = adapter._convert_to_gemini_messages(contents)

    # The two tool results must collapse into a single trailing user Content
    # with two parts (no back-to-back user Contents).
    assert len(gemini_contents) == 3
    last = gemini_contents[-1]
    assert last.role == "user"
    assert len(last.parts) == 2
    assert last.parts[0].function_response.name == "fc1"
    assert last.parts[1].function_response.name == "fc2"


# --- registry / provider resolution -------------------------------------------


def test_registered_in_models_registry() -> None:
    assert MODELS.get("gemini") is GeminiClientAdapter


@pytest.mark.parametrize("provider", ["gemini", "google", "google-vertex", "google_vertex"])
def test_get_model_resolves_gemini_aliases(mocker: MockerFixture, provider: str) -> None:
    mocker.patch.object(gemini.genai, "Client")
    mocker.patch.dict(os.environ, {}, clear=True)

    client = get_model(provider=provider)

    assert isinstance(client, GeminiClientAdapter)
