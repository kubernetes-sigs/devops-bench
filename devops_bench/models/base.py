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

"""LLM client interface and provider selection for MCP-style agent runs."""

from __future__ import annotations

import importlib.util
from abc import ABC, abstractmethod
from typing import Any

from devops_bench.core.config import get_env
from devops_bench.core.model_providers import resolve_provider
from devops_bench.core.registry import Registry

__all__ = ["LLMClient", "MODELS", "get_model"]

MODELS: Registry[type[LLMClient]] = Registry("models")


class LLMClient(ABC):
    """Abstract base class for LLM clients supporting MCP tools.

    Concrete adapters wrap a provider SDK and translate between the agent
    runner's neutral message/tool shapes and the provider's API.
    """

    @abstractmethod
    async def generate_content(
        self,
        contents: list[dict[str, Any]],
        tools: Any,
        system_instruction: str | None,
    ) -> Any:
        """Generate content from the model.

        Args:
            contents: Neutral message dicts with ``role`` and ``content`` keys.
            tools: Provider-formatted tool spec from :meth:`format_tools`.
            system_instruction: Optional system prompt.

        Returns:
            The raw, provider-specific response object.
        """

    @abstractmethod
    def format_tools(self, mcp_tools: Any) -> Any:
        """Convert MCP tools to the format expected by the model.

        Args:
            mcp_tools: Iterable of MCP tool objects exposing ``name``,
                ``description``, and ``inputSchema`` (duck-typed).

        Returns:
            The provider-specific tool representation.
        """

    @abstractmethod
    def extract_function_calls(self, response: Any) -> list[dict[str, Any]]:
        """Extract function calls from the model's response.

        Args:
            response: The raw response from the model.

        Returns:
            A list of dicts, each containing ``name``, ``args``, and optionally
            ``id`` (and provider-specific extras such as ``thought_signature``).
        """

    @abstractmethod
    def get_text_content(self, response: Any) -> str:
        """Extract the text content from the model's response.

        Args:
            response: The raw response from the model.

        Returns:
            The concatenated text, or an empty string when there is none.
        """


def get_model(
    provider: str | None = None, model_name: str | None = None, **kwargs: Any
) -> LLMClient:
    """Construct the LLM client for a provider.

    When ``provider`` is omitted it is read from the ``AGENT_PROVIDER``
    environment variable, defaulting to ``"google"``. The provider is resolved
    through the shared contract (:func:`devops_bench.core.model_providers.resolve_provider`),
    which maps it to the adapter family and a backend hint forwarded to the
    adapter — the same contract the CLI harnesses use.

    Args:
        provider: Provider alias such as ``"gemini"``/``"google"``/
            ``"google-vertex"``, ``"claude"``/``"anthropic"``/
            ``"anthropic-vertex"``, ``"openai"``, or ``"ollama"``.
            Case-insensitive.
        model_name: Optional model override passed to the adapter; when omitted
            the adapter reads ``AGENT_MODEL`` (or its own default).
        **kwargs: Extra keyword arguments forwarded to the adapter constructor.

    Returns:
        An instantiated :class:`LLMClient` for the selected provider.

    Raises:
        ConfigError: If ``provider`` is not a known alias.
        NotRegisteredError: If the resolved adapter family has no registered
            adapter (e.g. ``openai``).
        MissingDependencyError: If the selected provider's SDK is not installed.
    """
    spec = resolve_provider(provider if provider is not None else get_env("AGENT_PROVIDER"))
    key = spec.adapter_family
    # Import only the resolved adapter family's module so it self-registers. An
    # unregistered family (e.g. ``openai``) has no module; leave it to surface as
    # NotRegisteredError from ``MODELS.get``.
    module = f"{__package__}.{key}"
    if importlib.util.find_spec(module) is not None:
        importlib.import_module(module)
    adapter_cls = MODELS.get(key)
    return adapter_cls(model_name=model_name, backend=spec.backend, **kwargs)
