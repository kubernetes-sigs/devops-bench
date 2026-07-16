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

"""Tests for the LLM client interface and the provider factory."""

from __future__ import annotations

from typing import Any

import pytest

from devops_bench.core.errors import ConfigError, NotRegisteredError
from devops_bench.core.model_providers import ProviderSpec
from devops_bench.models import base
from devops_bench.models.base import MODELS, LLMClient, get_model


class _StubClient(LLMClient):
    """Minimal concrete adapter recording its constructor arguments."""

    def __init__(self, model_name: str | None = None, backend: str | None = None, **kwargs: Any):
        self.model_name = model_name
        self.backend = backend
        self.kwargs = kwargs

    async def generate_content(self, contents, tools, system_instruction):
        raise NotImplementedError

    def format_tools(self, mcp_tools):
        raise NotImplementedError

    def extract_function_calls(self, response):
        raise NotImplementedError

    def get_text_content(self, response):
        raise NotImplementedError


@pytest.fixture
def fake_family(monkeypatch):
    """Route provider resolution to a fake adapter family backed by ``_StubClient``.

    The family has no adapter module on disk, so ``get_model`` skips the import
    step and hits the registry directly — no collision with any real adapter,
    now or later.
    """

    def install(backend: str | None = None) -> None:
        spec = ProviderSpec(
            canonical="fake-provider",
            adapter_family="fake_family",
            oc_provider="fake-provider",
            api_key_envs=(),
            keyless_ok=True,
            backend=backend,
        )
        monkeypatch.setattr(base, "resolve_provider", lambda provider: spec)
        monkeypatch.setitem(MODELS._items, "fake_family", _StubClient)

    return install


def test_get_model_constructs_registered_adapter(fake_family):
    fake_family()

    client = get_model(provider="fake-provider", model_name="fake-model-1", timeout=7)

    assert isinstance(client, _StubClient)
    assert isinstance(client, LLMClient)
    assert client.model_name == "fake-model-1"
    assert client.backend is None
    assert client.kwargs == {"timeout": 7}


def test_get_model_forwards_backend_hint(fake_family):
    fake_family(backend="fake-backend")

    client = get_model(provider="fake-provider")

    assert isinstance(client, _StubClient)
    assert client.backend == "fake-backend"


def test_get_model_unknown_provider_raises():
    with pytest.raises(ConfigError):
        get_model(provider="does-not-exist")


def test_get_model_openai_resolves_but_has_no_adapter():
    # ``openai`` is a known provider in the contract but ships no adapter module,
    # so resolution succeeds and the registry lookup raises NotRegisteredError.
    with pytest.raises(NotRegisteredError):
        get_model(provider="openai")
