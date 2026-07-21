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

"""Anthropic (Claude) adapter for the LLM client interface.

Claude is reachable through three backends; this adapter selects one from the
environment so the ``claude`` provider key (alias ``anthropic``) names the model
family rather than a single transport:

- ``api`` — the first-party Anthropic API (``AsyncAnthropic``), the canonical
  default, selected when ``AGENT_API_KEY``/``ANTHROPIC_API_KEY`` is set.
- ``vertex`` — Claude on Google Vertex AI (``AsyncAnthropicVertex``), selected
  when ``GCP_PROJECT_ID`` is set, and the fallback when nothing else matches.
- ``bedrock`` — Claude on Amazon Bedrock (``AsyncAnthropicBedrock``), selected
  when ``AWS_REGION``/``AWS_DEFAULT_REGION`` is set.

Set ``ANTHROPIC_BACKEND`` to force a backend regardless of the inference above.
"""

from __future__ import annotations

from typing import Any

from devops_bench.core.config import first_env, get_env, get_int
from devops_bench.core.errors import ConfigError, MissingDependencyError
from devops_bench.core.logging import get_logger
from devops_bench.models.base import MODELS, LLMClient

try:
    from anthropic import AsyncAnthropic, AsyncAnthropicBedrock, AsyncAnthropicVertex
except ImportError:  # pragma: no cover - exercised only without the SDK
    AsyncAnthropic = None
    AsyncAnthropicBedrock = None
    AsyncAnthropicVertex = None

__all__ = ["ClaudeClientAdapter"]

_DEFAULT_MAX_TOKENS = 16000

# Per-backend default models. The model-id format is backend-specific, so each
# backend carries its own default; Bedrock ids are region/version-specific and
# have no safe universal default, so it requires AGENT_MODEL.
_DEFAULT_MODELS = {
    "api": "claude-sonnet-4-5",
    "vertex": "claude-sonnet-4-5@20250929",
}
_BACKENDS = frozenset({"api", "vertex", "bedrock"})

_log = get_logger("models.claude")


@MODELS.register("claude")
class ClaudeClientAdapter(LLMClient):
    """Adapter for the Anthropic SDK across its three backends.

    The backend (first-party API, Vertex AI, or Bedrock) is chosen from the
    environment; see the module docstring for the selection rules.

    Args:
        model_name: Model override; falls back to ``AGENT_MODEL`` and then a
            backend-specific default when omitted.
        max_tokens: Per-response output token cap; falls back to
            ``AGENT_MAX_TOKENS`` and then a sane default when omitted.
        backend: Backend hint from the provider contract (``"vertex"`` /
            ``"bedrock"``); overrides env inference. ``None`` infers from the
            environment (the bare ``anthropic`` provider).

    Raises:
        MissingDependencyError: If the ``anthropic`` SDK is not installed.
        ConfigError: If ``ANTHROPIC_BACKEND`` is unrecognized, or the ``bedrock``
            backend is selected without ``AGENT_MODEL`` set.
    """

    def __init__(
        self,
        model_name: str | None = None,
        max_tokens: int | None = None,
        *,
        backend: str | None = None,
    ) -> None:
        if AsyncAnthropic is None:
            raise MissingDependencyError("the Anthropic model adapter", "anthropic")

        # An explicit backend hint from the provider contract
        # (``anthropic-vertex`` -> ``vertex``, ``anthropic-bedrock`` -> ``bedrock``)
        # wins, decoupling Vertex/Bedrock auth from API-key presence; the bare
        # ``anthropic`` provider passes ``None`` and still infers from the env.
        backend = backend or self._select_backend()

        if not model_name:
            model_name = get_env("AGENT_MODEL", _DEFAULT_MODELS.get(backend))
        if not model_name:
            raise ConfigError(
                f"the Anthropic {backend!r} backend has no default model; set AGENT_MODEL"
            )

        self.client = self._build_client(backend)
        self.model_name = model_name
        self.max_tokens = max_tokens or get_int("AGENT_MAX_TOKENS", _DEFAULT_MAX_TOKENS)

    @staticmethod
    def _select_backend() -> str:
        """Resolve which backend to use from the environment.

        Returns:
            One of ``"api"``, ``"vertex"``, or ``"bedrock"``.

        Raises:
            ConfigError: If ``ANTHROPIC_BACKEND`` is set to an unknown value.
        """
        override = get_env("ANTHROPIC_BACKEND")
        if override:
            backend = override.lower()
            if backend not in _BACKENDS:
                raise ConfigError(
                    f"ANTHROPIC_BACKEND must be one of {sorted(_BACKENDS)}; got {override!r}"
                )
            return backend

        if first_env("AGENT_API_KEY", "ANTHROPIC_API_KEY"):
            return "api"
        if get_env("GCP_PROJECT_ID"):
            return "vertex"
        if first_env("AWS_REGION", "AWS_DEFAULT_REGION"):
            return "bedrock"

        _log.warning(
            "No Anthropic backend could be inferred from the environment; "
            "defaulting to Vertex. Set ANTHROPIC_BACKEND, AGENT_API_KEY, "
            "GCP_PROJECT_ID, or AWS_REGION to select one explicitly.",
        )
        return "vertex"

    @staticmethod
    def _build_client(backend: str) -> Any:
        """Construct the SDK client for the selected backend."""
        if backend == "api":
            return AsyncAnthropic(api_key=first_env("AGENT_API_KEY", "ANTHROPIC_API_KEY"))
        if backend == "bedrock":
            return AsyncAnthropicBedrock(aws_region=first_env("AWS_REGION", "AWS_DEFAULT_REGION"))
        # vertex
        return AsyncAnthropicVertex(
            region=get_env("GCP_VERTEX_LOCATION", "global"),
            project_id=get_env("GCP_PROJECT_ID"),
        )

    async def generate_content(
        self,
        contents: list[dict[str, Any]],
        tools: Any,
        system_instruction: str | None,
    ) -> Any:
        messages = self._convert_to_anthropic_messages(contents)
        kwargs: dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": self.max_tokens,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        if system_instruction:
            kwargs["system"] = system_instruction
        return await self.client.messages.create(**kwargs)

    def format_tools(self, mcp_tools: Any) -> Any:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.inputSchema if hasattr(tool, "inputSchema") else {},
            }
            for tool in mcp_tools
        ]

    def extract_function_calls(self, response: Any) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []
        if hasattr(response, "content"):
            for content in response.content:
                if hasattr(content, "type") and content.type == "tool_use":
                    calls.append({"name": content.name, "args": content.input, "id": content.id})
        return calls

    def get_text_content(self, response: Any) -> str:
        text = ""
        if hasattr(response, "content"):
            for content in response.content:
                if hasattr(content, "type") and content.type == "text":
                    text += content.text
        return text

    def _convert_to_anthropic_messages(
        self, contents: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        anthropic_messages: list[dict[str, Any]] = []
        for msg in contents:
            role = msg["role"]
            content = msg["content"]

            if role == "user":
                anthropic_messages.append({"role": "user", "content": content})
            elif role == "assistant":
                if "tool_calls" in msg:
                    content_blocks: list[dict[str, Any]] = []
                    if content:
                        content_blocks.append({"type": "text", "text": content})
                    for tc in msg["tool_calls"]:
                        content_blocks.append(
                            {
                                "type": "tool_use",
                                "id": tc.get("id"),
                                "name": tc.get("name"),
                                "input": tc.get("args"),
                            }
                        )
                    anthropic_messages.append({"role": "assistant", "content": content_blocks})
                else:
                    anthropic_messages.append({"role": "assistant", "content": content})
            elif role == "tool":
                # Group consecutive tool results into one user message so that
                # parallel tool calls do not produce back-to-back user turns,
                # which the Anthropic API rejects.
                tool_block = {
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id"),
                    "content": content,
                }
                if anthropic_messages and anthropic_messages[-1]["role"] == "user":
                    last = anthropic_messages[-1]["content"]
                    if isinstance(last, list):
                        last.append(tool_block)
                    else:
                        anthropic_messages[-1]["content"] = [
                            {"type": "text", "text": last},
                            tool_block,
                        ]
                else:
                    anthropic_messages.append({"role": "user", "content": [tool_block]})
        return anthropic_messages
