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

"""Unit tests for the model-provider contract."""

import pytest
from pydantic import ValidationError

from devops_bench.core.errors import ConfigError
from devops_bench.core.model_providers import (
    known_providers,
    resolve_provider,
)

# (raw alias, canonical, adapter_family, oc_provider, api_key_envs, keyless_ok, backend)
_ROWS = [
    ("gemini", "google", "gemini", "google", ("GEMINI_API_KEY", "GOOGLE_API_KEY"), False, None),
    ("google", "google", "gemini", "google", ("GEMINI_API_KEY", "GOOGLE_API_KEY"), False, None),
    (
        "google-vertex",
        "google-vertex",
        "gemini",
        "google-vertex",
        ("GOOGLE_CLOUD_API_KEY",),
        True,
        "vertex",
    ),
    (
        "google_vertex",
        "google-vertex",
        "gemini",
        "google-vertex",
        ("GOOGLE_CLOUD_API_KEY",),
        True,
        "vertex",
    ),
    ("anthropic", "anthropic", "claude", "anthropic", ("ANTHROPIC_API_KEY",), False, None),
    ("claude", "anthropic", "claude", "anthropic", ("ANTHROPIC_API_KEY",), False, None),
    ("anthropic-vertex", "anthropic-vertex", "claude", "anthropic-vertex", (), True, "vertex"),
    ("anthropic_vertex", "anthropic-vertex", "claude", "anthropic-vertex", (), True, "vertex"),
    ("anthropic-bedrock", "anthropic-bedrock", "claude", "anthropic-bedrock", (), True, "bedrock"),
    ("anthropic_bedrock", "anthropic-bedrock", "claude", "anthropic-bedrock", (), True, "bedrock"),
    ("openai", "openai", "openai", "openai", ("OPENAI_API_KEY",), False, None),
    ("ollama", "ollama", "ollama", "ollama", (), True, None),
]


@pytest.mark.parametrize("raw,canonical,family,oc_provider,api_key_envs,keyless,backend", _ROWS)
def test_resolve_provider_table(
    raw, canonical, family, oc_provider, api_key_envs, keyless, backend
):
    spec = resolve_provider(raw)
    assert spec.canonical == canonical
    assert spec.adapter_family == family
    assert spec.oc_provider == oc_provider
    assert spec.api_key_envs == api_key_envs
    assert spec.keyless_ok is keyless
    assert spec.backend == backend


def test_resolve_provider_is_case_insensitive():
    assert resolve_provider("GEMINI").canonical == "google"
    assert resolve_provider("Google-Vertex").canonical == "google-vertex"


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_blank_resolves_to_default(blank):
    assert resolve_provider(blank).canonical == "google"
    assert resolve_provider(blank, default="anthropic").canonical == "anthropic"


def test_unknown_provider_raises_with_known_list():
    with pytest.raises(ConfigError) as exc:
        resolve_provider("mystery")
    msg = str(exc.value)
    assert "mystery" in msg
    assert "gemini" in msg and "anthropic" in msg


def test_only_vertex_bedrock_ollama_are_keyless():
    keyless = {raw for raw, *_ in _ROWS if resolve_provider(raw).keyless_ok}
    assert keyless == {
        "google-vertex",
        "google_vertex",
        "anthropic-vertex",
        "anthropic_vertex",
        "anthropic-bedrock",
        "anthropic_bedrock",
        "ollama",
    }


def test_provider_spec_is_frozen():
    spec = resolve_provider("google")
    with pytest.raises(ValidationError):  # frozen pydantic model rejects mutation
        spec.canonical = "other"


def test_known_providers_sorted_and_complete():
    known = known_providers()
    assert known == tuple(sorted(known))
    assert "google-vertex" in known and "ollama" in known
