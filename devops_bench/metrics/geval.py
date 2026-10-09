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

"""Provider-agnostic DeepEval judge backed by the models layer."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from deepeval.models import DeepEvalBaseLLM

from devops_bench.core import get_env, get_logger
from devops_bench.core.model_identity import canonical_provider, judge_identity
from devops_bench.models import LLMClient, describe_client, get_model

__all__ = ["ModelLayerJudge", "describe_judge", "get_judge_model"]

_log = get_logger("metrics.geval")


class ModelLayerJudge(DeepEvalBaseLLM):
    """DeepEval judge that routes generation through the models layer.

    Routes generation through an :class:`~devops_bench.models.LLMClient`
    (the models layer), so the judge is provider-agnostic. Judge prompts are
    text-only: generation issues a single user turn with no tools and returns
    the response's text content.

    Args:
        client: Pre-built LLM client to wrap. When omitted, one is constructed
            from ``provider``/``model_name`` (or the ``JUDGE_PROVIDER`` /
            ``JUDGE_MODEL`` environment variables) via ``get_model``.
        provider: Provider key forwarded to ``get_model`` when ``client`` is not
            supplied. Falls back to the ``JUDGE_PROVIDER`` environment variable.
        model_name: Model override forwarded to ``get_model`` when ``client`` is
            not supplied. Falls back to the ``JUDGE_MODEL`` environment variable.
    """

    def __init__(
        self,
        client: LLMClient | None = None,
        *,
        provider: str | None = None,
        model_name: str | None = None,
    ) -> None:
        if client is None:
            configured = judge_identity()
            warn = model_name is None and get_env("JUDGE_MODEL") is None
            provider = provider or configured["provider"]
            model_name = model_name or configured["model"]
            client = get_model(provider=provider, model_name=model_name)
            if warn:
                # Name what was actually built: same model as the agent, or a foreign model id.
                _log.warning(
                    "JUDGE_MODEL unset; the judge resolved to %s/%s (agent: %s/%s)",
                    client.provider,
                    client.model_name,
                    canonical_provider(get_env("AGENT_PROVIDER")),
                    get_env("AGENT_MODEL"),
                )
        self.client = client
        # Explicit arguments win over the client's own values, for provider and model alike.
        #: Canonical provider id.
        self.provider = (
            canonical_provider(provider) if provider else getattr(client, "provider", None)
        )
        self._identity_model = model_name or getattr(client, "model_name", None)
        # DeepEval needs a non-empty label; the record never sees the placeholder.
        self._model_name = self._identity_model or "judge"

    @property
    def identity(self) -> dict[str, str | None]:
        """The ``{"provider", "model"}`` a run record stores for this judge."""
        return {"provider": self.provider, "model": self._identity_model}

    def load_model(self) -> LLMClient:
        """Return the wrapped LLM client (DeepEval contract)."""
        return self.client

    async def a_generate(self, prompt: str) -> str:
        """Generate judge text for ``prompt`` asynchronously.

        Args:
            prompt: The fully rendered judge prompt.

        Returns:
            The model's text response, or an empty string when none was produced.
        """
        response = await self.client.generate_content(
            contents=[{"role": "user", "content": prompt}],
            tools=None,
            system_instruction=None,
        )
        return self.client.get_text_content(response) or ""

    def generate(self, prompt: str) -> str:
        """Generate judge text for ``prompt`` synchronously.

        Runs the async :meth:`a_generate` to completion. This is loop-aware: when
        called from outside an event loop it uses :func:`asyncio.run`; when an
        event loop is already running on the calling thread (``asyncio.run`` would
        raise there) it runs the coroutine on a separate worker thread and blocks
        for the result.

        Args:
            prompt: The fully rendered judge prompt.

        Returns:
            The model's text response, or an empty string when none was produced.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.a_generate(prompt))

        # A loop is already running on this thread; offload to a worker thread
        # that owns its own loop so we never call asyncio.run() re-entrantly.
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(lambda: asyncio.run(self.a_generate(prompt))).result()

    def get_model_name(self) -> str:
        """Return the configured judge model name (DeepEval contract)."""
        return self._model_name


def describe_judge(judge: Any) -> dict[str, str | None]:
    """Return the ``{"provider", "model"}`` identity a run record stores for its judge."""
    own = getattr(judge, "identity", None)
    if isinstance(own, dict):
        return dict(own)
    # A foreign judge: read what it exposes.
    identity = describe_client(getattr(judge, "client", None))
    identity["provider"] = getattr(judge, "provider", None) or identity["provider"]
    return identity


def get_judge_model(provider: str | None = None, model_name: str | None = None) -> ModelLayerJudge:
    """Build the default judge model from configuration.

    Args:
        provider: Provider key; falls back to ``JUDGE_PROVIDER``.
        model_name: Model override; falls back to ``JUDGE_MODEL``.

    Returns:
        A :class:`ModelLayerJudge` wrapping the selected provider's client.
    """
    return ModelLayerJudge(provider=provider, model_name=model_name)
