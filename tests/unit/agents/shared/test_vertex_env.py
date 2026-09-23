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
    VERTEX_PROJECT_ENVS,
    vertex_location,
    vertex_project,
)


@pytest.fixture(autouse=True)
def _clear_vertex_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # An operator's ambient project/location env must not decide these assertions.
    for name in (*VERTEX_LOCATION_ENVS, *VERTEX_PROJECT_ENVS, "GCP_LOCATION"):
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


def test_explicit_default_is_honored() -> None:
    assert vertex_location(default="us-east5") == "us-east5"


def test_project_precedence_is_declaration_order(monkeypatch: pytest.MonkeyPatch) -> None:
    for index, name in enumerate(VERTEX_PROJECT_ENVS):
        monkeypatch.setenv(name, f"proj-{index}")
    for index, name in enumerate(VERTEX_PROJECT_ENVS):
        assert vertex_project() == f"proj-{index}"
        monkeypatch.delenv(name)
    assert vertex_project() is None


def test_project_reads_the_repo_wide_spelling(monkeypatch: pytest.MonkeyPatch) -> None:
    # GCP_PROJECT_ID is what models/, providers/gcp.py and the claude_code
    # harness read; an operator who set only that must not be ignored here.
    monkeypatch.setenv("GCP_PROJECT_ID", "proj-repo")
    monkeypatch.setenv("GCP_PROJECT", "proj-legacy")
    assert vertex_project() == "proj-repo"


def test_whitespace_only_project_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "  ")
    assert vertex_project() is None
