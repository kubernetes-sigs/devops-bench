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

"""Tests for the promised-fixture pre-flight check."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from devops_bench.evalharness.fixtures import (
    REQUIRE_FIXTURES_ENV,
    check_prompt_fixtures,
    prompt_fixture_paths,
)

# The real shapes, verbatim from the five fixture-bearing task prompts.
_CVE_PROMPT = (
    "The cluster 'c1' is managed via the repository at '~/cve-repo-c1.git'.\n"
    "A critical CVE advisory has just been delivered to '~/cve-advisory-c1.json'."
)
_SPOT_PROMPT = "A rightsizing report has been delivered to '~/rightsizing-report-c1.json'."


def test_it_extracts_every_promised_path(tmp_path: Path) -> None:
    assert prompt_fixture_paths(_CVE_PROMPT, tmp_path) == [
        tmp_path / "cve-repo-c1.git",
        tmp_path / "cve-advisory-c1.json",
    ]


def test_it_handles_the_dollar_home_spelling(tmp_path: Path) -> None:
    assert prompt_fixture_paths("read $HOME/report.json now", tmp_path) == [
        tmp_path / "report.json"
    ]


def test_it_strips_sentence_punctuation_from_a_trailing_path(tmp_path: Path) -> None:
    # "...at ~/opa-repo-c1.git." must not look for a file named "…git.".
    assert prompt_fixture_paths("manifests live at ~/opa-repo-c1.git.", tmp_path) == [
        tmp_path / "opa-repo-c1.git"
    ]


def test_it_deduplicates_a_path_the_prompt_mentions_twice(tmp_path: Path) -> None:
    prompt = "clone ~/repo-c1.git then push back to ~/repo-c1.git"
    assert prompt_fixture_paths(prompt, tmp_path) == [tmp_path / "repo-c1.git"]


def test_a_prompt_with_no_fixture_yields_nothing(tmp_path: Path) -> None:
    assert prompt_fixture_paths("scale the deployment to 3 replicas", tmp_path) == []


def test_it_passes_when_every_fixture_is_present(tmp_path: Path) -> None:
    (tmp_path / "rightsizing-report-c1.json").write_text("{}")
    assert check_prompt_fixtures(_SPOT_PROMPT, "spot-rebalancing", tmp_path) == []


def test_it_raises_when_a_promised_fixture_was_never_seeded(tmp_path: Path) -> None:
    # multi-region-failover: the repo was never written to disk in any of the
    # four runs that attempted the task.
    with pytest.raises(RuntimeError, match="does not exist"):
        check_prompt_fixtures("clone ~/app-repo-e-c1.git", "multi-region-failover", tmp_path)


def test_it_raises_when_a_fixture_exists_but_is_unreadable(tmp_path: Path) -> None:
    # Eric's batch: fixtures seeded into /root at 0700 while the agent ran as
    # uid 2000. Present on disk, invisible to the agent.
    report = tmp_path / "rightsizing-report-c1.json"
    report.write_text("{}")
    report.chmod(0o000)
    try:
        if os.access(report, os.R_OK):  # pragma: no cover - root ignores mode bits
            pytest.skip("running as root: mode bits do not restrict reads")
        with pytest.raises(RuntimeError, match="not readable"):
            check_prompt_fixtures(_SPOT_PROMPT, "spot-rebalancing", tmp_path)
    finally:
        report.chmod(0o644)


def test_it_names_the_task_and_every_missing_path(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError) as excinfo:
        check_prompt_fixtures(_CVE_PROMPT, "cve-remediation", tmp_path)
    message = str(excinfo.value)
    assert "cve-remediation" in message
    assert "cve-repo-c1.git" in message
    assert "cve-advisory-c1.json" in message
    assert REQUIRE_FIXTURES_ENV in message


def test_the_env_escape_hatch_downgrades_to_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A deliberately degraded arm is a different experiment, not a broken run.
    monkeypatch.setenv(REQUIRE_FIXTURES_ENV, "0")
    problems = check_prompt_fixtures(_SPOT_PROMPT, "spot-rebalancing", tmp_path)
    assert len(problems) == 1


def test_a_mounted_sandbox_skips_the_host_side_check(tmp_path: Path) -> None:
    # The fixtures went in through a bind mount, so host paths say nothing
    # about what the agent can see.
    assert check_prompt_fixtures(_SPOT_PROMPT, "spot", tmp_path, mounted=True) == []


def test_an_unsubstituted_placeholder_is_not_reported_as_missing(tmp_path: Path) -> None:
    # Raw task text, before the harness substitutes {{CLUSTER_NAME}}. The name
    # captured here would be the truncated "cve-repo-", so checking it would
    # report a fixture nobody ever promised.
    raw = "the repository at '~/cve-repo-{{CLUSTER_NAME}}.git'"
    assert prompt_fixture_paths(raw, tmp_path) == []
    assert check_prompt_fixtures(raw, "cve-remediation", tmp_path) == []
