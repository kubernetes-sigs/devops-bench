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

"""Shared scratch-space helpers: mint directories, delete only what was minted.

Mint-don't-guard: rather than trying to make deletion of an arbitrary,
caller-supplied path safe, callers only ever delete directories this module
minted under its own root. Destructive helpers here assert that invariant
rather than acting as a general-purpose permission system.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

__all__ = [
    "scratch_root",
    "mint_dir",
    "remove_minted",
]


def _validate_root(root: Path) -> Path:
    """Reject a scratch root too broad to safely own, and return it resolved.

    An unvalidated root makes every containment check downstream vacuous:
    ``DEVOPS_BENCH_SCRATCH_ROOT=/`` would make :func:`remove_minted` willing
    to ``rmtree`` any absolute path, since everything is "under" ``/``. This
    is the same class of check ``remove_minted`` does for a minted path,
    applied to the root itself.

    Args:
        root: The candidate scratch root, resolved or not.

    Returns:
        ``root`` resolved to an absolute, symlink-free path.

    Raises:
        ValueError: If ``root`` is relative, resolves to the filesystem
            root, resolves to the home directory, or is fewer than two
            levels below the filesystem root.
    """
    if not root.is_absolute():
        raise ValueError(f"scratch root must be an absolute path, got {root!r}")
    resolved = root.resolve()
    home = Path.home().resolve()
    depth = len(resolved.parts) - 1
    if depth < 2:
        raise ValueError(
            f"scratch root must resolve at least two levels below the filesystem root, "
            f"got {root!r} (resolved: {resolved})"
        )
    if resolved == home:
        raise ValueError(f"scratch root must not resolve to the home directory, got {root!r}")
    return resolved


def scratch_root() -> Path:
    """Return the harness scratch root, creating it on first use.

    Defaults to ``<system temp dir>/devops-bench``. Overridable, as a root
    only, via the ``DEVOPS_BENCH_SCRATCH_ROOT`` environment variable. The
    bash-side stack (``tf/prebuilt/opa-remediation/scripts/setup.sh``) pins
    its own default to a fixed ``/tmp/devops-bench`` instead of the system
    temp dir, because that path is rendered into a static task prompt that
    must be identical across processes; this module has no such constraint,
    so it follows the system temp dir here.

    Returns:
        The scratch root directory, created if it did not already exist.

    Raises:
        ValueError: If the resolved root fails :func:`_validate_root`'s
            checks (see its docstring).
    """
    override = os.environ.get("DEVOPS_BENCH_SCRATCH_ROOT")
    root = Path(override) if override else Path(tempfile.gettempdir()) / "devops-bench"
    root = _validate_root(root)
    root.mkdir(parents=True, exist_ok=True)
    return root


def mint_dir(prefix: str) -> Path:
    """Mint a new unique directory under the scratch root.

    Args:
        prefix: Prefix for the minted directory's name.

    Returns:
        The path to the newly created directory.
    """
    return Path(tempfile.mkdtemp(prefix=prefix, dir=scratch_root()))


def remove_minted(path: Path) -> None:
    """Remove a directory tree previously minted by :func:`mint_dir`.

    This check is an assertion of the minting contract, not a permission
    system: it exists to catch a bug that passes in a path this module never
    minted, not to sanitize arbitrary input.

    Args:
        path: The minted directory to remove.

    Raises:
        ValueError: If ``path``, once resolved, is not strictly under the
            resolved scratch root (including if it is the root itself).
    """
    root = scratch_root().resolve()
    resolved = path.resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError(
            f"refusing to remove {path}: not a directory minted under the scratch root {root}"
        )
    shutil.rmtree(resolved)
