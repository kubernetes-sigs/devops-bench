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

"""OpenAI-compatible adapter for the LLM client interface.

Targets the OpenAI API itself or any server that speaks its chat-completions
wire format (SGLang, vLLM, Ollama, ...). The endpoint comes from
``OPENAI_BASE_URL``; when unset the SDK talks to the OpenAI API.
"""

from __future__ import annotations

import json
from typing import Any

from devops_bench.core.config import first_env, get_env, get_int
from devops_bench.core.errors import ConfigError, MissingDependencyError
from devops_bench.models.base import MODELS, LLMClient

try:
    from openai import AsyncOpenAI
except ImportError:  # pragma: no cover - exercised only without the SDK
    AsyncOpenAI = None

__all__ = ["OpenAIClientAdapter"]

_DEFAULT_MAX_TOKENS = 16000


@MODELS.register("openai")
class OpenAIClientAdapter(LLMClient):
    """Adapter for an OpenAI-compatible chat-completions endpoint.

    Args:
        model_name: Model override; falls back to ``AGENT_MODEL`` when omitted.
            There is no default: a self-hosted server's model ids are not
            guessable.
        base_url: Endpoint override; falls back to ``OPENAI_BASE_URL``, and to
            the OpenAI API when neither is set.
        max_tokens: Per-response output token cap; falls back to
            ``AGENT_MAX_TOKENS`` and then a sane default when omitted.
        backend: Accepted for a uniform adapter signature; ignored.

    Raises:
        MissingDependencyError: If the ``openai`` SDK is not installed.
        ConfigError: If no model is configured.
    """

    def __init__(
        self,
        model_name: str | None = None,
        base_url: str | None = None,
        max_tokens: int | None = None,
        *,
        backend: str | None = None,
    ) -> None:
        if AsyncOpenAI is None:
            raise MissingDependencyError("the OpenAI model adapter", "openai")

        model_name = model_name or get_env("AGENT_MODEL")
        if not model_name:
            raise ConfigError("the openai provider has no default model; set AGENT_MODEL")

        # The client requires a non-empty key even when the server ignores it.
        api_key = first_env("AGENT_API_KEY", "OPENAI_API_KEY") or "unused"
        self.client = AsyncOpenAI(base_url=base_url or get_env("OPENAI_BASE_URL"), api_key=api_key)
        self.model_name = model_name
        self.max_tokens = max_tokens or get_int("AGENT_MAX_TOKENS", _DEFAULT_MAX_TOKENS)

    async def generate_content(
        self,
        contents: list[dict[str, Any]],
        tools: Any,
        system_instruction: str | None,
    ) -> Any:
        messages = self._convert_to_openai_messages(contents, system_instruction)
        kwargs: dict[str, Any] = {
            "model": self.model_name,
            "max_completion_tokens": self.max_tokens,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        return await self.client.chat.completions.create(**kwargs)

    def format_tools(self, mcp_tools: Any) -> Any:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.inputSchema if hasattr(tool, "inputSchema") else {},
                },
            }
            for tool in mcp_tools
        ]

    def extract_function_calls(self, response: Any) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []
        message = response.choices[0].message
        if message.tool_calls:
            for tc in message.tool_calls:
                args = tc.function.arguments
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                calls.append({"name": tc.function.name, "args": args, "id": tc.id})
        return calls

    def get_text_content(self, response: Any) -> str:
        content = response.choices[0].message.content
        return content if content else ""

    def _convert_to_openai_messages(
        self, contents: list[dict[str, Any]], system_instruction: str | None
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        for msg in contents:
            role = msg["role"]
            content = msg["content"]

            if role == "user":
                messages.append({"role": "user", "content": content})
            elif role == "assistant":
                if "tool_calls" in msg:
                    tool_calls = [
                        {
                            "id": tc.get("id") or f"call_{i}",
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": (
                                    json.dumps(tc["args"])
                                    if isinstance(tc["args"], dict)
                                    else tc["args"]
                                ),
                            },
                        }
                        for i, tc in enumerate(msg["tool_calls"])
                    ]
                    messages.append(
                        {"role": "assistant", "content": content or "", "tool_calls": tool_calls}
                    )
                else:
                    messages.append({"role": "assistant", "content": content})
            elif role == "tool":
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg.get("tool_call_id", ""),
                        "content": content,
                    }
                )
        return messages
