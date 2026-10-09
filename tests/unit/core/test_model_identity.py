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

"""Tests for the configured judge / chaos-driver identities."""

from __future__ import annotations

import pytest

from devops_bench.core.model_identity import canonical_provider, driver_identity, judge_identity


def test_canonical_provider_never_raises() -> None:
    assert canonical_provider("gemini") == "google"
    assert canonical_provider(None) == "google"  # the contract's default
    assert canonical_provider("no-such-provider") == "no-such-provider"


def test_driver_identity_prefers_chaos_env_and_canonicalizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_PROVIDER", "anthropic")
    monkeypatch.setenv("AGENT_MODEL", "arm-model")
    monkeypatch.delenv("CHAOS_PROVIDER", raising=False)
    monkeypatch.delenv("CHAOS_MODEL", raising=False)
    assert driver_identity() == {"provider": "anthropic", "model": "arm-model"}

    monkeypatch.setenv("CHAOS_PROVIDER", "gemini")
    monkeypatch.setenv("CHAOS_MODEL", "chaos-x")
    assert driver_identity() == {"provider": "google", "model": "chaos-x"}


def test_driver_identity_reports_an_unknown_provider_as_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHAOS_PROVIDER", "no-such-provider")
    monkeypatch.setenv("CHAOS_MODEL", "chaos-x")
    assert driver_identity() == {"provider": "no-such-provider", "model": "chaos-x"}


def test_judge_identity_mirrors_the_driver_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_PROVIDER", "gemini")
    monkeypatch.setenv("AGENT_MODEL", "arm-model")
    monkeypatch.delenv("JUDGE_PROVIDER", raising=False)
    monkeypatch.delenv("JUDGE_MODEL", raising=False)
    assert judge_identity() == {"provider": "google", "model": "arm-model"}

    monkeypatch.setenv("JUDGE_PROVIDER", "anthropic")
    monkeypatch.setenv("JUDGE_MODEL", "judge-x")
    assert judge_identity() == {"provider": "anthropic", "model": "judge-x"}


def test_model_is_none_when_only_the_adapter_default_would_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in ("AGENT_MODEL", "CHAOS_MODEL", "JUDGE_MODEL"):
        monkeypatch.delenv(var, raising=False)
    assert driver_identity()["model"] is None
    assert judge_identity()["model"] is None
