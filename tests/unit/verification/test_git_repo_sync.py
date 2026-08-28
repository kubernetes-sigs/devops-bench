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

"""Unit tests for the git_repo_sync verifier.

Runs against real, temporary bare git repos under ``tmp_path`` (no ``git``
mocking): more reliable than faking argv/stdout for a subprocess-heavy
verifier. The verifier polls via ``_poll_to_result``; a single immediate
result needs no sleep.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from devops_bench.core import SubprocessError
from devops_bench.verification import VerificationSpec
from devops_bench.verification.verifiers.git_repo_sync import GitRepoSyncVerifier

_GIT_CFG = [
    "-c",
    "commit.gpgsign=false",
    "-c",
    "init.defaultBranch=main",
    "-c",
    "safe.bareRepository=all",
    "-c",
    "user.email=devops-bench-test@example.com",
    "-c",
    "user.name=devops-bench-test",
]

_WEB_SEED = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
spec:
  replicas: 2
  template:
    spec:
      containers:
        - name: web
          image: nginx:1.21.6
"""

_WEB_FIXED = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
spec:
  replicas: 2
  template:
    spec:
      containers:
        - name: web
          image: nginx:1.27.4
"""

_APP_SEED = """\
apiVersion: networking.k8s.io/v1beta1
kind: Ingress
metadata:
  name: web
spec:
  rules: []
---
apiVersion: policy/v1beta1
kind: PodDisruptionBudget
metadata:
  name: web
spec:
  minAvailable: 1
"""

_APP_FIXED = """\
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: web
spec:
  rules: []
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: web
spec:
  minAvailable: 1
"""


def _run(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *_GIT_CFG, *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout


@dataclass
class GitFixture:
    """A bare repo with a seed commit and a follow-up "fix" commit."""

    bare: str
    seed_sha: str


def _init_repo(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    work = tmp_path / "work"
    _run(tmp_path, "init", "--bare", str(bare))
    _run(tmp_path, "clone", str(bare), str(work))
    _run(work, "checkout", "-B", "main")
    return work


def _build_git_fixture(tmp_path: Path) -> GitFixture:
    work = _init_repo(tmp_path)
    (work / "workloads").mkdir()
    (work / "workloads" / "web.yaml").write_text(_WEB_SEED)
    (work / "app.yaml").write_text(_APP_SEED)
    _run(work, "add", "-A")
    _run(work, "commit", "-m", "seed")
    seed_sha = _run(work, "rev-parse", "HEAD").strip()
    _run(work, "push", "-u", "origin", "main")

    (work / "workloads" / "web.yaml").write_text(_WEB_FIXED)
    (work / "app.yaml").write_text(_APP_FIXED)
    _run(work, "add", "-A")
    _run(work, "commit", "-m", "fix")
    _run(work, "push")

    return GitFixture(bare=str(work.parent / "origin.git"), seed_sha=seed_sha)


def test_registered_via_spec(tmp_path: Path) -> None:
    node = VerificationSpec(
        {"type": "git_repo_sync", "repo_path": str(tmp_path), "op": "exists"}
    ).root
    assert isinstance(node, GitRepoSyncVerifier)


def test_repo_path_tilde_expanded() -> None:
    v = GitRepoSyncVerifier.model_validate(
        {"type": "git_repo_sync", "repo_path": "~/nonexistent-xyz.git", "op": "exists"}
    )
    assert not v.repo_path.startswith("~")


def test_image_matches_after_fix(tmp_path: Path) -> None:
    fixture = _build_git_fixture(tmp_path)
    v = GitRepoSyncVerifier.model_validate(
        {
            "type": "git_repo_sync",
            "repo_path": fixture.bare,
            "file": "workloads/web.yaml",
            "path": "$[?(@.kind=='Deployment')].spec.template.spec.containers[0].image",
            "op": "matches",
            "value": r"nginx:1\.(2[7-9]|[3-9][0-9])",
        }
    )
    result = v.verify(0)
    assert result.success is True
    assert result.status == "pass"


def test_image_matches_fails_on_seed_ref(tmp_path: Path) -> None:
    fixture = _build_git_fixture(tmp_path)
    v = GitRepoSyncVerifier.model_validate(
        {
            "type": "git_repo_sync",
            "repo_path": fixture.bare,
            "ref": fixture.seed_sha,
            "file": "workloads/web.yaml",
            "path": "$[?(@.kind=='Deployment')].spec.template.spec.containers[0].image",
            "op": "matches",
            "value": r"nginx:1\.(2[7-9]|[3-9][0-9])",
        }
    )
    result = v.verify(0)
    assert result.success is False
    assert result.status == "fail"


# Neither test below combines `require_new_commit` with `file`: both check
# the repo/ref itself (`op: exists`, no `file`), which is exactly the shape
# `test_require_new_commit_with_file_is_rejected` requires everyone to use.


def test_require_new_commit_true_after_fix(tmp_path: Path) -> None:
    fixture = _build_git_fixture(tmp_path)
    v = GitRepoSyncVerifier.model_validate(
        {
            "type": "git_repo_sync",
            "repo_path": fixture.bare,
            "require_new_commit": True,
            "op": "exists",
        }
    )
    result = v.verify(0)
    assert result.success is True
    assert result.status == "pass"


def test_require_new_commit_false_on_seed_only_repo(tmp_path: Path) -> None:
    work = _init_repo(tmp_path)
    (work / "seed.txt").write_text("seed\n")
    _run(work, "add", "-A")
    _run(work, "commit", "-m", "seed")
    _run(work, "push", "-u", "origin", "main")

    v = GitRepoSyncVerifier.model_validate(
        {
            "type": "git_repo_sync",
            "repo_path": str(work.parent / "origin.git"),
            "require_new_commit": True,
            "op": "exists",
        }
    )
    result = v.verify(0)
    assert result.success is False
    assert result.status == "fail"


def test_require_new_commit_with_file_is_rejected() -> None:
    # Footgun guard: `_check` resolves the seed commit before it reads any
    # file, so combining `require_new_commit` with a content assertion in
    # the same check would let a transient `git rev-list` failure mask the
    # content assertion's own pass/fail. Reject the combination up front.
    with pytest.raises(ValidationError, match="does not combine"):
        GitRepoSyncVerifier.model_validate(
            {
                "type": "git_repo_sync",
                "repo_path": "/tmp/does-not-matter.git",
                "require_new_commit": True,
                "file": "workloads/web.yaml",
                "op": "exists",
            }
        )


def test_invalid_jsonpath_rejected_at_parse_time() -> None:
    with pytest.raises(ValidationError, match="invalid JSONPath"):
        GitRepoSyncVerifier.model_validate(
            {
                "type": "git_repo_sync",
                "repo_path": "/tmp/does-not-matter.git",
                "file": "workloads/web.yaml",
                "path": "$[?(",
                "op": "exists",
            }
        )


def test_absent_with_across_matches_rejected() -> None:
    with pytest.raises(ValidationError, match="already asserts emptiness"):
        GitRepoSyncVerifier.model_validate(
            {
                "type": "git_repo_sync",
                "repo_path": "/tmp/does-not-matter.git",
                "file": "workloads/web.yaml",
                "path": "$[*].kind",
                "op": "absent",
                "across_matches": "none",
            }
        )


def test_pathless_exists_with_across_matches_rejected() -> None:
    with pytest.raises(ValidationError, match="applies to the file/ref itself"):
        GitRepoSyncVerifier.model_validate(
            {
                "type": "git_repo_sync",
                "repo_path": "/tmp/does-not-matter.git",
                "op": "exists",
                "across_matches": "every",
            }
        )


def test_value_op_without_file_rejected() -> None:
    with pytest.raises(ValidationError, match="requires 'file'"):
        GitRepoSyncVerifier.model_validate(
            {
                "type": "git_repo_sync",
                "repo_path": "/tmp/does-not-matter.git",
                "op": "eq",
                "value": "banana",
            }
        )


def test_value_op_without_value_rejected() -> None:
    with pytest.raises(ValidationError, match="requires 'value'"):
        GitRepoSyncVerifier.model_validate(
            {
                "type": "git_repo_sync",
                "repo_path": "/tmp/does-not-matter.git",
                "file": "workloads/web.yaml",
                "op": "eq",
            }
        )


def test_multidoc_ingress_apiversion_migrated(tmp_path: Path) -> None:
    fixture = _build_git_fixture(tmp_path)
    v = GitRepoSyncVerifier.model_validate(
        {
            "type": "git_repo_sync",
            "repo_path": fixture.bare,
            "file": "app.yaml",
            "path": "$[?(@.kind=='Ingress')].apiVersion",
            "op": "eq",
            "value": "networking.k8s.io/v1",
        }
    )
    result = v.verify(0)
    assert result.success is True
    assert result.status == "pass"


def test_multidoc_v1beta1_absent_on_fixed_present_on_seed(tmp_path: Path) -> None:
    fixture = _build_git_fixture(tmp_path)
    spec = {
        "type": "git_repo_sync",
        "repo_path": fixture.bare,
        "file": "app.yaml",
        "path": "$[?(@.apiVersion=='networking.k8s.io/v1beta1')]",
        "op": "absent",
    }

    v_fixed = GitRepoSyncVerifier.model_validate(spec)
    result_fixed = v_fixed.verify(0)
    assert result_fixed.success is True
    assert result_fixed.status == "pass"

    v_seed = GitRepoSyncVerifier.model_validate({**spec, "ref": fixture.seed_sha})
    result_seed = v_seed.verify(0)
    assert result_seed.success is False
    assert result_seed.status == "fail"


def test_across_matches_every_and_none(tmp_path: Path) -> None:
    fixture = _build_git_fixture(tmp_path)

    v_every = GitRepoSyncVerifier.model_validate(
        {
            "type": "git_repo_sync",
            "repo_path": fixture.bare,
            "file": "app.yaml",
            "path": "$[*].kind",
            "op": "ne",
            "value": "ServiceAccount",
            "across_matches": "every",
        }
    )
    result_every = v_every.verify(0)
    assert result_every.success is True
    assert result_every.status == "pass"

    v_none_fails = GitRepoSyncVerifier.model_validate(
        {
            "type": "git_repo_sync",
            "repo_path": fixture.bare,
            "file": "app.yaml",
            "path": "$[*].kind",
            "op": "ne",
            "value": "ServiceAccount",
            "across_matches": "none",
        }
    )
    result_none_fails = v_none_fails.verify(0)
    assert result_none_fails.success is False
    assert result_none_fails.status == "fail"

    v_none_passes = GitRepoSyncVerifier.model_validate(
        {
            "type": "git_repo_sync",
            "repo_path": fixture.bare,
            "file": "app.yaml",
            "path": "$[*].apiVersion",
            "op": "eq",
            "value": "banana",
            "across_matches": "none",
        }
    )
    result_none_passes = v_none_passes.verify(0)
    assert result_none_passes.success is True
    assert result_none_passes.status == "pass"

    v_every_fails = GitRepoSyncVerifier.model_validate(
        {
            "type": "git_repo_sync",
            "repo_path": fixture.bare,
            "file": "app.yaml",
            "path": "$[*].apiVersion",
            "op": "eq",
            "value": "banana",
            "across_matches": "every",
        }
    )
    result_every_fails = v_every_fails.verify(0)
    assert result_every_fails.success is False
    assert result_every_fails.status == "fail"


def test_repo_path_nonexistent_dir_is_an_error(tmp_path: Path) -> None:
    v = GitRepoSyncVerifier.model_validate(
        {
            "type": "git_repo_sync",
            "repo_path": str(tmp_path / "does-not-exist.git"),
            "op": "exists",
        }
    )
    result = v.verify(0)
    assert result.success is False
    assert result.status == "error"


def test_file_not_found_at_ref_is_a_fail_not_an_error(tmp_path: Path) -> None:
    fixture = _build_git_fixture(tmp_path)
    v = GitRepoSyncVerifier.model_validate(
        {
            "type": "git_repo_sync",
            "repo_path": fixture.bare,
            "file": "workloads/does-not-exist.yaml",
            "op": "exists",
        }
    )
    result = v.verify(0)
    assert result.success is False
    assert result.status == "fail"


def test_yaml_parse_failure_is_a_fail_not_an_error(tmp_path: Path) -> None:
    # The file itself was found (the ref resolved and `git show` succeeded);
    # unparsable content is an observed fact about the repo, not a verifier
    # error, so this reports "fail" the same way a missing file does.
    work = _init_repo(tmp_path)
    (work / "broken.yaml").write_text("key: [unterminated\n")
    _run(work, "add", "-A")
    _run(work, "commit", "-m", "seed")
    _run(work, "push", "-u", "origin", "main")

    v = GitRepoSyncVerifier.model_validate(
        {
            "type": "git_repo_sync",
            "repo_path": str(work.parent / "origin.git"),
            "file": "broken.yaml",
            "path": "$[*].key",
            "op": "exists",
        }
    )
    result = v.verify(0)
    assert result.success is False
    assert result.status == "fail"
    assert "failed to parse YAML" in result.reason


def test_absent_on_missing_file_is_true(tmp_path: Path) -> None:
    fixture = _build_git_fixture(tmp_path)
    v = GitRepoSyncVerifier.model_validate(
        {
            "type": "git_repo_sync",
            "repo_path": fixture.bare,
            "file": "workloads/does-not-exist.yaml",
            "op": "absent",
        }
    )
    result = v.verify(0)
    assert result.success is True
    assert result.status == "pass"


# -- tri-state and across_matches edge cases (Task V3 additions) --------


def test_git_command_failure_is_an_error(tmp_path: Path) -> None:
    v = GitRepoSyncVerifier.model_validate(
        {"type": "git_repo_sync", "repo_path": str(tmp_path), "op": "exists"}
    )
    with patch.object(
        v,
        "_git",
        side_effect=SubprocessError(["git", "rev-parse", "HEAD"], returncode=128, stderr="boom"),
    ):
        result = v.verify(0)
    assert result.success is False
    assert result.status == "error"
    assert "boom" in result.reason


def test_multiple_matches_without_across_matches_fails_naming_them(tmp_path: Path) -> None:
    fixture = _build_git_fixture(tmp_path)
    v = GitRepoSyncVerifier.model_validate(
        {
            "type": "git_repo_sync",
            "repo_path": fixture.bare,
            "file": "app.yaml",
            "path": "$[*].kind",
            "op": "ne",
            "value": "ServiceAccount",
        }
    )
    result = v.verify(0)
    assert result.success is False
    assert result.status == "fail"
    assert "across_matches" in result.reason
    assert "Ingress" in result.reason
    assert "PodDisruptionBudget" in result.reason
    # The matched paths render without jsonpath_ng's wrapping parens noise.
    assert "[0].kind" in result.reason
    assert "[1].kind" in result.reason
    assert "([0]" not in result.reason
    assert "([1]" not in result.reason


def test_multiple_matches_deep_path_renders_clean_dotted_reason(tmp_path: Path) -> None:
    # A deep path (3+ segments through the wildcard) exercises the general
    # `_render_path` recursion, not just the single-segment `.kind` case
    # above: jsonpath_ng nests one paren pair per segment in `full_path`'s
    # own `str()`, so a shallow "strip the outer pair" fix would still leave
    # nested parens on a path like this one.
    fixture = _build_git_fixture(tmp_path)
    v = GitRepoSyncVerifier.model_validate(
        {
            "type": "git_repo_sync",
            "repo_path": fixture.bare,
            "file": "app.yaml",
            "path": "$[*].metadata.name",
            "op": "eq",
            "value": "web",
        }
    )
    result = v.verify(0)
    assert result.success is False
    assert result.status == "fail"
    assert "[0].metadata.name" in result.reason
    assert "[1].metadata.name" in result.reason
    assert "((" not in result.reason


def test_empty_match_set_with_across_matches_none_passes(tmp_path: Path) -> None:
    fixture = _build_git_fixture(tmp_path)
    v = GitRepoSyncVerifier.model_validate(
        {
            "type": "git_repo_sync",
            "repo_path": fixture.bare,
            "file": "app.yaml",
            "path": "$[*].apiVersion",
            "op": "eq",
            "value": "banana",
            "across_matches": "none",
        }
    )
    result = v.verify(0)
    assert result.success is True
    assert result.status == "pass"


# -- element-wise across_matches (PR 47 follow-up) -----------------------
#
# JSONPath drops elements that fail to resolve the target field, so a
# value-wise reduction over the flattened matches silently degrades "every
# element has X" to "at least one has X". These assert the fix quantifies
# over the elements the last wildcard-like path segment selects instead:
# `app.yaml`'s Ingress doc has no `spec.minAvailable` and its
# PodDisruptionBudget doc has no `spec.rules`, so either field read across
# both docs must FAIL under `every` by naming the doc that never resolved
# the suffix.


def test_across_matches_every_fails_on_element_missing_field(tmp_path: Path) -> None:
    fixture = _build_git_fixture(tmp_path)
    v = GitRepoSyncVerifier.model_validate(
        {
            "type": "git_repo_sync",
            "repo_path": fixture.bare,
            "file": "app.yaml",
            "path": "$[*].spec.rules",
            "op": "exists",
            "across_matches": "every",
        }
    )
    result = v.verify(0)
    assert result.success is False
    assert result.status == "fail"
    assert "did not resolve" in result.reason
    assert "[1]" in result.reason


def test_across_matches_every_value_op_fails_on_element_missing_field(tmp_path: Path) -> None:
    fixture = _build_git_fixture(tmp_path)
    v = GitRepoSyncVerifier.model_validate(
        {
            "type": "git_repo_sync",
            "repo_path": fixture.bare,
            "file": "app.yaml",
            "path": "$[*].spec.minAvailable",
            "op": "eq",
            "value": 1,
            "across_matches": "every",
        }
    )
    result = v.verify(0)
    assert result.success is False
    assert result.status == "fail"
    assert "did not resolve" in result.reason
    assert "[0]" in result.reason


def test_across_matches_none_element_wise(tmp_path: Path) -> None:
    fixture = _build_git_fixture(tmp_path)

    v_absent_everywhere = GitRepoSyncVerifier.model_validate(
        {
            "type": "git_repo_sync",
            "repo_path": fixture.bare,
            "file": "app.yaml",
            "path": "$[*].spec.selector",
            "op": "eq",
            "value": "anything",
            "across_matches": "none",
        }
    )
    result_absent = v_absent_everywhere.verify(0)
    assert result_absent.success is True
    assert result_absent.status == "pass"

    v_present_and_passing = GitRepoSyncVerifier.model_validate(
        {
            "type": "git_repo_sync",
            "repo_path": fixture.bare,
            "file": "app.yaml",
            "path": "$[*].spec.minAvailable",
            "op": "eq",
            "value": 1,
            "across_matches": "none",
        }
    )
    result_present = v_present_and_passing.verify(0)
    assert result_present.success is False
    assert result_present.status == "fail"
