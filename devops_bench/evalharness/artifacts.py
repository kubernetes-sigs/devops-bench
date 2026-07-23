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

"""Capture files an agent generates by diffing a directory before and after."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from devops_bench.core import get_logger

__all__ = ["snapshot_dir", "collect_generated_files"]

_log = get_logger("evalharness.artifacts")


def snapshot_dir(path: str | os.PathLike[str] = ".") -> set[str]:
    """Snapshot the immediate entries of a directory.

    Args:
        path: Directory to list; defaults to the current working directory.

    Returns:
        The set of entry names directly under ``path``. An empty set is
        returned when ``path`` does not exist, so the diff stays well-defined
        when the workspace is created lazily by the agent.
    """
    target = os.fspath(path)
    if not os.path.isdir(target):
        return set()
    return set(os.listdir(target))


def collect_generated_files(
    before: set[str],
    run_dir: str | os.PathLike[str],
    *,
    source_dir: str | os.PathLike[str] = ".",
) -> list[str]:
    """Copy entries created since ``before`` into the run's artifact directory.

    New files and directories (those present now but absent from ``before``) are
    copied into ``<run_dir>/generated_files/``. The destination directory is
    created only when there is at least one new entry to copy.

    Args:
        before: Entry names captured by :func:`snapshot_dir` prior to the run.
        run_dir: The run output directory; artifacts land under its
            ``generated_files`` subdirectory.
        source_dir: Directory the agent wrote into; defaults to the current
            working directory. The harness threads its harness-owned, per-run
            :attr:`~devops_bench.core.RunContext.workspace_path` here — the
            same directory the CLI agent wrapper executes in — so the
            artifact diff is bound to the per-task workspace, not the
            harness process's cwd.

    Returns:
        The names of the entries that were copied.
    """
    after = snapshot_dir(source_dir)
    new_entries = after - before
    if not new_entries:
        return []

    gen_files_dir = Path(run_dir) / "generated_files"
    gen_files_dir.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    src_root = os.fspath(source_dir)
    for name in new_entries:
        src = os.path.join(src_root, name)
        dst = os.fspath(gen_files_dir / name)
        # An agent could plant a symlink pointing outside the workspace; following
        # it would copy host files/credentials into the run artifacts. Skip links
        # at the top level and preserve (don't follow) any nested ones.
        if os.path.islink(src):
            _log.warning("skipping symlinked artifact %s", src)
            continue
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=True)
            copied.append(name)
        elif os.path.isfile(src):
            shutil.copy(src, dst)
            copied.append(name)

    _log.info("collected %d generated artifact(s) into %s", len(copied), gen_files_dir)
    return copied
