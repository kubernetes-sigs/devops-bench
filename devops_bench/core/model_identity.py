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

"""Configured model identities for the judge and the chaos driver, read from the environment."""

from __future__ import annotations

from devops_bench.core.config import first_env
from devops_bench.core.errors import ConfigError
from devops_bench.core.model_providers import resolve_provider

__all__ = ["canonical_provider", "configured_identity", "driver_identity", "judge_identity"]


def canonical_provider(raw: str | None) -> str | None:
    """Canonicalize a provider alias without raising; an unknown alias is returned as written."""
    try:
        return resolve_provider(raw).canonical
    except ConfigError:  # get_model raises on it later; identity reporting must not
        return raw


def configured_identity(
    provider_vars: tuple[str, ...], model_vars: tuple[str, ...]
) -> dict[str, str | None]:
    """Return the ``{"provider", "model"}`` the environment configures (model None = adapter default)."""
    return {
        "provider": canonical_provider(first_env(*provider_vars)),
        "model": first_env(*model_vars),
    }


def driver_identity() -> dict[str, str | None]:
    """The chaos driver's configured identity: ``CHAOS_*``, falling back to ``AGENT_*``."""
    return configured_identity(("CHAOS_PROVIDER", "AGENT_PROVIDER"), ("CHAOS_MODEL", "AGENT_MODEL"))


def judge_identity() -> dict[str, str | None]:
    """The judge's configured identity: ``JUDGE_*``, falling back to ``AGENT_*``."""
    return configured_identity(("JUDGE_PROVIDER", "AGENT_PROVIDER"), ("JUDGE_MODEL", "AGENT_MODEL"))
