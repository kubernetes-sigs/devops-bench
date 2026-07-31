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

"""Unit tests for devops_bench.core.scratch."""

from pathlib import Path

import pytest

from devops_bench.core import scratch


def test_scratch_root_creates_default_under_system_tempdir(monkeypatch, tmp_path):
    monkeypatch.delenv("DEVOPS_BENCH_SCRATCH_ROOT", raising=False)
    monkeypatch.setattr(scratch.tempfile, "gettempdir", lambda: str(tmp_path))

    root = scratch.scratch_root()

    assert root == tmp_path / "devops-bench"
    assert root.is_dir()


def test_scratch_root_env_override(monkeypatch, tmp_path):
    override = tmp_path / "custom-root"
    monkeypatch.setenv("DEVOPS_BENCH_SCRATCH_ROOT", str(override))

    root = scratch.scratch_root()

    assert root == override
    assert root.is_dir()


def test_scratch_root_rejects_the_filesystem_root(monkeypatch):
    monkeypatch.setenv("DEVOPS_BENCH_SCRATCH_ROOT", "/")

    with pytest.raises(ValueError, match="filesystem root"):
        scratch.scratch_root()


def test_scratch_root_rejects_the_home_directory(monkeypatch):
    monkeypatch.setenv("DEVOPS_BENCH_SCRATCH_ROOT", str(Path.home()))

    with pytest.raises(ValueError, match="home directory"):
        scratch.scratch_root()


def test_scratch_root_rejects_a_relative_path(monkeypatch):
    monkeypatch.setenv("DEVOPS_BENCH_SCRATCH_ROOT", "relative/scratch-root")

    with pytest.raises(ValueError, match="absolute"):
        scratch.scratch_root()


def test_scratch_root_accepts_a_legitimate_deep_override(monkeypatch, tmp_path):
    deep = tmp_path / "a" / "b" / "devops-bench"
    monkeypatch.setenv("DEVOPS_BENCH_SCRATCH_ROOT", str(deep))

    root = scratch.scratch_root()

    assert root == deep
    assert root.is_dir()


def test_mint_dir_creates_a_directory_under_the_root(monkeypatch, tmp_path):
    monkeypatch.setenv("DEVOPS_BENCH_SCRATCH_ROOT", str(tmp_path))

    minted = scratch.mint_dir("workspace-")

    assert minted.is_dir()
    assert minted.parent == tmp_path
    assert minted.name.startswith("workspace-")


def test_remove_minted_removes_a_minted_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("DEVOPS_BENCH_SCRATCH_ROOT", str(tmp_path))
    minted = scratch.mint_dir("workspace-")
    (minted / "file.txt").write_text("hello")

    scratch.remove_minted(minted)

    assert not minted.exists()


def test_remove_minted_refuses_a_path_outside_the_root(monkeypatch, tmp_path):
    root = tmp_path / "scratch-root"
    root.mkdir()
    monkeypatch.setenv("DEVOPS_BENCH_SCRATCH_ROOT", str(root))
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    with pytest.raises(ValueError, match=str(outside)):
        scratch.remove_minted(outside)

    assert outside.exists()


def test_remove_minted_refuses_the_root_itself(monkeypatch, tmp_path):
    root = tmp_path / "scratch-root"
    root.mkdir()
    monkeypatch.setenv("DEVOPS_BENCH_SCRATCH_ROOT", str(root))

    with pytest.raises(ValueError, match=str(root)):
        scratch.remove_minted(root)

    assert root.exists()


def test_remove_minted_refuses_a_sibling_tmp_dir_with_a_shared_prefix(monkeypatch, tmp_path):
    root = tmp_path / "devops-bench"
    root.mkdir()
    monkeypatch.setenv("DEVOPS_BENCH_SCRATCH_ROOT", str(root))
    # Shares the root's path as a string prefix but is not a child of it, so a
    # naive string-prefix containment check would wrongly treat it as inside.
    sibling = tmp_path / "devops-bench-evil"
    sibling.mkdir()

    with pytest.raises(ValueError, match=str(sibling)):
        scratch.remove_minted(sibling)

    assert sibling.exists()


def test_remove_minted_error_names_the_path(monkeypatch, tmp_path):
    root = tmp_path / "scratch-root"
    root.mkdir()
    monkeypatch.setenv("DEVOPS_BENCH_SCRATCH_ROOT", str(root))
    outside = tmp_path / "outside-dir"
    outside.mkdir()

    with pytest.raises(ValueError) as exc_info:
        scratch.remove_minted(outside)

    assert str(outside) in str(exc_info.value)
