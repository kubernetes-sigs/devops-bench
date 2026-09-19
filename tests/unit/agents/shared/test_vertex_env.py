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

"""Tests for the shared Vertex location resolution."""

from __future__ import annotations

import pytest

from devops_bench.agents.shared.vertex_env import (
    DEFAULT_VERTEX_LOCATION,
    VERTEX_LOCATION_ENVS,
    vertex_location,
)


@pytest.fixture(autouse=True)
def _clear_location_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # An operator's ambient location env must not decide these assertions.
    for name in (*VERTEX_LOCATION_ENVS, "GCP_LOCATION"):
        monkeypatch.delenv(name, raising=False)


def test_defaults_to_global() -> None:
    # Not a region: the -preview model ids this benchmark runs are published
    # only on the global endpoint and 404 from a regional one.
    assert DEFAULT_VERTEX_LOCATION == "global"
    assert vertex_location() == "global"


def test_precedence_is_declaration_order(monkeypatch: pytest.MonkeyPatch) -> None:
    # Set every variable, then peel them off highest-first; each removal must
    # hand off to exactly the next name in the tuple.
    for index, name in enumerate(VERTEX_LOCATION_ENVS):
        monkeypatch.setenv(name, f"loc-{index}")
    for index, name in enumerate(VERTEX_LOCATION_ENVS):
        assert vertex_location() == f"loc-{index}"
        monkeypatch.delenv(name)
    assert vertex_location() == DEFAULT_VERTEX_LOCATION


def test_the_deployers_cluster_zone_is_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    # GCP_LOCATION belongs to the deployers and holds a cluster *zone*
    # (scripts/bastion/vm-setup.sh exports us-central1-a into the bastion
    # profile). A zone is never a valid Vertex location, so it must not leak
    # into model routing — the run falls through to the default instead.
    monkeypatch.setenv("GCP_LOCATION", "us-central1-a")
    assert vertex_location() == "global"
    assert "GCP_LOCATION" not in VERTEX_LOCATION_ENVS


def test_whitespace_only_value_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # Routing at " " yields an unresolvable endpoint; fall through instead.
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "   ")
    monkeypatch.setenv("GCP_VERTEX_LOCATION", "europe-west4")
    assert vertex_location() == "europe-west4"


def test_value_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", " europe-west4\n")
    assert vertex_location() == "europe-west4"


def test_fallback_runs_only_when_the_env_chain_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def _lookup() -> str | None:
        calls.append(1)
        return "asia-northeast1"

    monkeypatch.setenv("GCP_VERTEX_LOCATION", "europe-west4")
    assert vertex_location(fallback=_lookup) == "europe-west4"
    # The antigravity caller's fallback shells out to gcloud; a configured host
    # must not pay for that subprocess.
    assert calls == []

    monkeypatch.delenv("GCP_VERTEX_LOCATION")
    assert vertex_location(fallback=_lookup) == "asia-northeast1"
    assert calls == [1]


@pytest.mark.parametrize("returned", [None, "", "  "])
def test_empty_fallback_falls_through_to_the_default(returned: str | None) -> None:
    assert vertex_location(fallback=lambda: returned) == DEFAULT_VERTEX_LOCATION


def test_explicit_default_is_honored() -> None:
    assert vertex_location(default="us-east5") == "us-east5"
