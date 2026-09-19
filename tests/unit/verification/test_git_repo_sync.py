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

"""Unit tests for ``GitRepoSyncVerifier``.

These build real repositories under ``tmp_path`` rather than stubbing git.
The verifier's whole job is to read what is committed, so faking ``git show``
would test the mock; a repo is two subprocess calls to create and keeps the
ref/root-commit behaviour honest.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from devops_bench.verification.verifiers import GitRepoSyncVerifier

_INGRESS_V1BETA1 = """\
apiVersion: networking.k8s.io/v1beta1
kind: Ingress
metadata:
  name: shop
---
apiVersion: policy/v1beta1
kind: PodDisruptionBudget
metadata:
  name: shop-pdb
"""

_INGRESS_V1 = _INGRESS_V1BETA1.replace("v1beta1", "v1")


def _run(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository seeded with one commit, as the task fixtures create it."""
    work = tmp_path / "manifests"
    work.mkdir()
    _run(work, "init", "-q", "-b", "main")
    _run(work, "config", "user.email", "bench@example.com")
    _run(work, "config", "user.name", "bench")
    (work / "app.yaml").write_text(_INGRESS_V1BETA1)
    _run(work, "add", "-A")
    _run(work, "commit", "-qm", "seed")
    return work


def _commit(repo: Path, content: str, message: str = "migrate") -> None:
    (repo / "app.yaml").write_text(content)
    _run(repo, "add", "-A")
    _run(repo, "commit", "-qm", message)


def _verifier(repo: Path, **kwargs: Any) -> GitRepoSyncVerifier:
    base: dict[str, Any] = {"type": "git_repo_sync", "repo_path": str(repo)}
    base.update(kwargs)
    return GitRepoSyncVerifier(**base)


_KIND_INGRESS = "$[?(@.kind='Ingress')].apiVersion"


# -- shape --------------------------------------------------------------


def test_a_value_op_requires_a_path(repo: Path) -> None:
    with pytest.raises(ValidationError, match="requires 'path'"):
        _verifier(repo, op="eq", value="networking.k8s.io/v1", file="app.yaml")


def test_a_path_requires_a_file_to_read_it_from(repo: Path) -> None:
    with pytest.raises(ValidationError, match="requires 'file'"):
        _verifier(repo, op="eq", value="x", path=_KIND_INGRESS)


def test_a_malformed_path_is_rejected_at_authoring_time(repo: Path) -> None:
    with pytest.raises(ValidationError, match="not a valid JSONPath"):
        _verifier(repo, op="exists", path="$[?(", file="app.yaml")


def test_absent_does_not_take_across_matches(repo: Path) -> None:
    with pytest.raises(ValidationError, match="does not take 'across_matches'"):
        _verifier(repo, op="absent", path=_KIND_INGRESS, file="app.yaml", across_matches="every")


def test_absent_requires_a_path(repo: Path) -> None:
    # Pathless `absent` is unsatisfiable here: a missing repo and a missing file
    # both fail closed, and a present one is reported as present, so no runtime
    # branch can pass it. Fail at load time rather than silently at grading time.
    with pytest.raises(ValidationError, match="requires 'path'"):
        _verifier(repo, op="absent", file="app.yaml")
    with pytest.raises(ValidationError, match="requires 'path'"):
        _verifier(repo, op="absent")


def test_absent_with_a_path_is_still_allowed(repo: Path) -> None:
    # The satisfiable form, kept working: it passes when the path resolves to
    # nothing, which is the whole point of the operator.
    assert _verifier(repo, op="absent", path=_KIND_INGRESS, file="app.yaml").op == "absent"


def test_a_value_op_requires_a_value(repo: Path) -> None:
    with pytest.raises(ValidationError, match="requires 'value'"):
        _verifier(repo, op="eq", path=_KIND_INGRESS, file="app.yaml")


# -- reading the committed document -------------------------------------


def test_the_committed_value_is_what_is_graded(repo: Path) -> None:
    _commit(repo, _INGRESS_V1)
    result = _verifier(
        repo, op="eq", value="networking.k8s.io/v1", path=_KIND_INGRESS, file="app.yaml"
    ).verify(0.0)
    assert result.success is True


def test_an_uncommitted_working_tree_edit_does_not_count(repo: Path) -> None:
    # The point of the verifier: an agent that fixes the file but never commits
    # has not kept the repository in sync, and must not score as if it had.
    (repo / "app.yaml").write_text(_INGRESS_V1)
    result = _verifier(
        repo, op="eq", value="networking.k8s.io/v1", path=_KIND_INGRESS, file="app.yaml"
    ).verify(0.0)
    assert result.success is False
    assert result.status == "fail"


def test_the_stale_value_fails(repo: Path) -> None:
    result = _verifier(
        repo, op="eq", value="networking.k8s.io/v1", path=_KIND_INGRESS, file="app.yaml"
    ).verify(0.0)
    assert result.success is False
    assert result.status == "fail"


def test_every_document_can_be_quantified_over(repo: Path) -> None:
    _commit(repo, _INGRESS_V1)
    result = _verifier(
        repo,
        op="matches",
        value=r"^(networking\.k8s\.io|policy)/v1$",
        path="$[*].apiVersion",
        across_matches="every",
        file="app.yaml",
    ).verify(0.0)
    assert result.success is True


def test_one_stale_document_fails_the_every_quantifier(repo: Path) -> None:
    half = _INGRESS_V1BETA1.replace("networking.k8s.io/v1beta1", "networking.k8s.io/v1")
    _commit(repo, half)
    result = _verifier(
        repo,
        op="matches",
        value=r"^(networking\.k8s\.io|policy)/v1$",
        path="$[*].apiVersion",
        across_matches="every",
        file="app.yaml",
    ).verify(0.0)
    assert result.success is False


def test_a_ref_other_than_head_can_be_read(repo: Path) -> None:
    _commit(repo, _INGRESS_V1)
    result = _verifier(
        repo,
        op="eq",
        value="networking.k8s.io/v1beta1",
        path=_KIND_INGRESS,
        file="app.yaml",
        ref="HEAD~1",
    ).verify(0.0)
    assert result.success is True


# -- require_new_commit -------------------------------------------------


def test_an_untouched_repository_has_not_committed_anything(repo: Path) -> None:
    result = _verifier(repo, op="exists", require_new_commit=True).verify(0.0)
    assert result.success is False
    assert result.status == "fail"
    assert "root commit" in result.reason


def test_a_second_commit_satisfies_require_new_commit(repo: Path) -> None:
    _commit(repo, _INGRESS_V1)
    result = _verifier(repo, op="exists", require_new_commit=True).verify(0.0)
    assert result.success is True


def test_require_new_commit_is_anded_with_the_path_check(repo: Path) -> None:
    # A commit was made, but it did not fix the thing being graded.
    _commit(repo, _INGRESS_V1BETA1 + "\n# touched\n")
    result = _verifier(
        repo,
        op="eq",
        value="networking.k8s.io/v1",
        path=_KIND_INGRESS,
        file="app.yaml",
        require_new_commit=True,
    ).verify(0.0)
    assert result.success is False


# -- failing closed -----------------------------------------------------


def test_a_missing_repository_fails_rather_than_erroring(tmp_path: Path) -> None:
    # The fixture created it before the agent started, so its absence is a
    # result about the run, not a broken harness.
    result = _verifier(tmp_path / "gone", op="exists").verify(0.0)
    assert result.success is False
    assert result.status == "fail"
    assert "does not exist" in result.reason


def test_a_deleted_manifest_fails_even_for_absent(repo: Path) -> None:
    # "The agent deleted the file" must not score the same as "the agent
    # removed the offending field from it".
    _run(repo, "rm", "-q", "app.yaml")
    _run(repo, "commit", "-qm", "drop")
    result = _verifier(repo, op="absent", path=_KIND_INGRESS, file="app.yaml").verify(0.0)
    assert result.success is False
    assert result.status == "fail"
    assert "not present at" in result.reason


def test_unparseable_yaml_is_an_observation_not_a_harness_failure(repo: Path) -> None:
    _commit(repo, "apiVersion: [unclosed\n")
    result = _verifier(
        repo, op="eq", value="networking.k8s.io/v1", path=_KIND_INGRESS, file="app.yaml"
    ).verify(0.0)
    assert result.success is False
    assert result.status == "fail"
    assert "not valid YAML" in result.reason


def test_a_path_resolving_to_nothing_fails_rather_than_passing_vacuously(repo: Path) -> None:
    result = _verifier(
        repo,
        op="eq",
        value="x",
        path="$[?(@.kind='CronJob')].apiVersion",
        file="app.yaml",
    ).verify(0.0)
    assert result.success is False


def test_an_unreadable_ref_is_an_error(repo: Path) -> None:
    result = _verifier(repo, op="exists", ref="refs/heads/nope").verify(0.0)
    assert result.success is False
    assert result.status == "error"


# -- bare repositories --------------------------------------------------


def test_a_bare_repository_is_readable(tmp_path: Path, repo: Path) -> None:
    # The fixtures seed bare repos, which git refuses to touch by path under
    # some safe.bareRepository settings; the verifier overrides that per call.
    bare = tmp_path / "manifests.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(repo), str(bare)],
        check=True,
        capture_output=True,
    )
    result = _verifier(
        bare, op="eq", value="networking.k8s.io/v1beta1", path=_KIND_INGRESS, file="app.yaml"
    ).verify(0.0)
    assert result.success is True
