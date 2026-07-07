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

"""The single source of truth for agent model-provider config.

Every harness consumes the same ``AGENT_PROVIDER`` / ``AGENT_MODEL`` /
``AGENT_API_KEY`` contract through :func:`resolve_provider`, so a config that
works for one harness behaves identically for the others. A raw provider alias
resolves to a :class:`ProviderSpec` carrying both axes the harnesses need: the
adapter family (which models-layer client to build) and the backend / transport
/ key-routing details (which oc wire-provider, which API-key env var(s), and
whether the backend authenticates without a key).

Lives in ``core`` (not ``models``/``agents``) so both the models layer and the
CLI harnesses import it without an import cycle and without pulling any provider
SDK. Named ``model_providers`` to avoid confusion with
:mod:`devops_bench.providers` (the cloud/infra OpenTofu providers).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from devops_bench.core.errors import ConfigError

__all__ = ["ProviderSpec", "resolve_provider", "known_providers"]


class ProviderSpec(BaseModel):
    """Resolved config contract for one agent model provider.

    Attributes:
        canonical: Normalized provider id (the ``_SPECS`` key).
        adapter_family: Models-layer adapter key for ``get_model`` /
            ``MODELS.get`` (e.g. ``gemini`` / ``claude`` / ``ollama``).
        oc_provider: openclaw wire-provider id used in ``provider/model`` and the
            per-run ``_PROVIDER_TRANSPORT`` lookup.
        api_key_envs: Env var name(s) a CLI harness sets from ``config.api_key``.
            Empty for ``anthropic-vertex`` / ``anthropic-bedrock`` / ``ollama``
            (no key is ever threaded). ``google-vertex`` is keyless-ok but still
            routes a *provided* key to ``GOOGLE_CLOUD_API_KEY``.
        keyless_ok: Whether the backend can authenticate without a key (Vertex
            ADC, Bedrock AWS creds, local ollama).
        backend: Adapter backend hint (``"vertex"`` / ``"bedrock"``), or ``None``
            to let the adapter infer the backend from the environment.
    """

    model_config = ConfigDict(frozen=True)

    canonical: str
    adapter_family: str
    oc_provider: str
    api_key_envs: tuple[str, ...]
    keyless_ok: bool
    backend: str | None = None


# Canonical provider id -> spec. Two axes are encoded here: ``adapter_family``
# (``google`` and ``google-vertex`` both build the ``gemini`` adapter) and the
# backend/transport/key-routing (which differ between them). Vertex and Bedrock
# are keyless-ok (ADC / AWS creds) and need no key; ``anthropic-vertex`` /
# ``anthropic-bedrock`` / ``ollama`` carry empty ``api_key_envs`` so a key is
# never forced onto them, while ``google-vertex`` still routes a provided key to
# ``GOOGLE_CLOUD_API_KEY`` (the vertex transport var, not the google-genai one).
_SPECS: dict[str, ProviderSpec] = {
    "google": ProviderSpec(
        canonical="google",
        adapter_family="gemini",
        oc_provider="google",
        api_key_envs=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        keyless_ok=False,
        backend=None,
    ),
    "google-vertex": ProviderSpec(
        canonical="google-vertex",
        adapter_family="gemini",
        oc_provider="google-vertex",
        api_key_envs=("GOOGLE_CLOUD_API_KEY",),
        keyless_ok=True,
        backend="vertex",
    ),
    "anthropic": ProviderSpec(
        canonical="anthropic",
        adapter_family="claude",
        oc_provider="anthropic",
        api_key_envs=("ANTHROPIC_API_KEY",),
        keyless_ok=False,
        backend=None,  # claude infers api/vertex/bedrock from the environment
    ),
    "anthropic-vertex": ProviderSpec(
        canonical="anthropic-vertex",
        adapter_family="claude",
        oc_provider="anthropic-vertex",
        api_key_envs=(),
        keyless_ok=True,
        backend="vertex",
    ),
    "anthropic-bedrock": ProviderSpec(
        canonical="anthropic-bedrock",
        adapter_family="claude",
        oc_provider="anthropic-bedrock",
        api_key_envs=(),
        keyless_ok=True,
        backend="bedrock",
    ),
    "openai": ProviderSpec(
        canonical="openai",
        adapter_family="openai",  # no adapter module today: get_model raises NotRegisteredError
        oc_provider="openai",
        api_key_envs=("OPENAI_API_KEY",),
        keyless_ok=False,
        backend=None,
    ),
    "ollama": ProviderSpec(
        canonical="ollama",
        adapter_family="ollama",
        oc_provider="ollama",
        api_key_envs=(),  # optional key handled by the adapter via AGENT_API_KEY
        keyless_ok=True,
        backend=None,
    ),
}

# Raw alias (lowercased) -> canonical id. Company/runtime names and underscore
# spellings map onto the canonical wire ids above.
_ALIASES: dict[str, str] = {
    "gemini": "google",
    "google": "google",
    "google-vertex": "google-vertex",
    "google_vertex": "google-vertex",
    "claude": "anthropic",
    "anthropic": "anthropic",
    "anthropic-vertex": "anthropic-vertex",
    "anthropic_vertex": "anthropic-vertex",
    "anthropic-bedrock": "anthropic-bedrock",
    "anthropic_bedrock": "anthropic-bedrock",
    "openai": "openai",
    "ollama": "ollama",
}


def known_providers() -> tuple[str, ...]:
    """Return the sorted raw provider aliases the contract accepts."""
    return tuple(sorted(_ALIASES))


def resolve_provider(provider: str | None, *, default: str = "google") -> ProviderSpec:
    """Resolve a raw provider alias to its :class:`ProviderSpec`.

    Matching is case-insensitive; a blank or unset value resolves to ``default``.

    Args:
        provider: Raw ``AGENT_PROVIDER`` value (or a per-call override). ``None``
            or blank resolves to ``default``.
        default: Alias used when ``provider`` is blank/unset.

    Returns:
        The :class:`ProviderSpec` for the resolved provider.

    Raises:
        ConfigError: If ``provider`` (or ``default``) is not a known alias.

    Example:
        >>> resolve_provider("gemini").canonical
        'google'
        >>> resolve_provider("google-vertex").backend
        'vertex'
    """
    raw = (provider or "").strip().lower() or default.strip().lower()
    canonical = _ALIASES.get(raw)
    if canonical is None:
        raise ConfigError(
            f"unknown agent provider {raw!r}; known providers: {', '.join(known_providers())}"
        )
    return _SPECS[canonical]
