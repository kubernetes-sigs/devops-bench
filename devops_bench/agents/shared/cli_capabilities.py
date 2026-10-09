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

"""Capability materialization shared by the CLI agents (Gemini, openclaw).

Both CLI agents render granted MCP bindings into a ``{name: {command, args}}``
launch map and copy discovered ``SKILL.md`` files into the binary's workspace
skills tree. Importing this module pulls no provider SDK.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from devops_bench.agents.shared.skills import iter_skills
from devops_bench.core import ConfigError, get_logger

if TYPE_CHECKING:
    from devops_bench.agents.capabilities import McpBinding

__all__ = ["agent_workdir", "build_mcp_servers", "materialize_skills"]

_log = get_logger("agents.shared.cli_capabilities")


@contextlib.contextmanager
def agent_workdir(workspace_path: Path | None, *, prefix: str) -> Iterator[Path]:
    """Yield the directory a CLI agent subprocess should run in.

    When the harness supplies ``workspace_path`` (its own per-run workspace,
    kept alive across the run so artifact collection can diff it afterward),
    that directory is yielded as-is and is NOT cleaned up here — the harness
    owns its lifecycle. Otherwise a throwaway ``TemporaryDirectory`` is
    created and removed on exit, preserving each CLI agent's standalone
    behavior (e.g. a direct unit-test invocation with no harness workspace).

    Args:
        workspace_path: The harness-owned workspace directory, or ``None``.
        prefix: Prefix for the fallback temp directory's name.

    Yields:
        The directory the CLI agent subprocess should run in.
    """
    if workspace_path is not None:
        yield workspace_path
        return
    with tempfile.TemporaryDirectory(prefix=prefix) as tmpdir:
        yield Path(tmpdir)


def build_mcp_servers(mcp_servers: tuple[McpBinding, ...]) -> dict[str, dict]:
    """Map MCP bindings with a launch command to a CLI ``servers`` mapping.

    Bindings with an empty ``command`` are skipped (they denote in-process CLI
    servers). Path-like commands that exist on disk are resolved to absolute
    paths. ``env`` values are rendered verbatim so ``${VAR}`` references are
    resolved by the CLI at launch rather than written into workspace artifacts.

    Args:
        mcp_servers: Bindings granted for the run.

    Returns:
        A ``{name: {"command": ..., "args": [...], "env": {...}, "cwd": ...}}``
        mapping carrying only the keys a binding populates.

    Raises:
        ConfigError: If two bindings resolve to the same name.
    """
    servers: dict[str, dict] = {}
    for index, binding in enumerate(mcp_servers):
        if not binding.command:
            continue
        name = binding.name or f"mcp{index}"
        if name in servers:
            raise ConfigError(f"two granted MCP servers resolve to the name {name!r}")
        cmd = binding.command[0]
        if os.sep in cmd:
            if os.path.exists(cmd):
                cmd = os.path.abspath(cmd)
            else:
                _log.warning(
                    "Path-like MCP command '%s' not found relative to harness; passing unchanged",
                    cmd,
                )
        entry: dict = {"command": cmd}
        if len(binding.command) > 1:
            entry["args"] = list(binding.command[1:])
        if binding.env:
            entry["env"] = dict(binding.env)
        if binding.cwd:
            entry["cwd"] = binding.cwd
        servers[name] = entry
    return servers


def _ignore_escaping_links(
    bundle: Path,
) -> Callable[[str | os.PathLike[str], list[str]], set[str]]:
    """Return a :func:`shutil.copytree` ``ignore`` callback dropping escaping or broken links."""
    root = bundle.resolve()

    def _ignore(dirpath: str | os.PathLike[str], names: list[str]) -> set[str]:
        skipped: set[str] = set()
        for name in names:
            entry = Path(dirpath) / name
            if not entry.is_symlink():
                continue
            try:
                target = entry.resolve(strict=True)
            except (OSError, RuntimeError):
                skipped.add(name)
                continue
            if not target.is_relative_to(root):
                _log.warning("Skipping skill link %s: resolves outside the bundle", entry)
                skipped.add(name)
        return skipped

    return _ignore


def materialize_skills(skills_root: Path, paths: tuple[str, ...]) -> list[str]:
    """Copy discovered skill bundles into a CLI's workspace skills tree.

    For each ``SKILL.md`` found beneath ``paths``, its containing directory is
    copied to ``skills_root/<name>/`` so sibling files (``references/``,
    ``templates/``, ``scripts/``) remain available. In-bundle symlinks are
    recreated; broken or escaping links are dropped. A ``SKILL.md`` at a
    discovery root that also contains child skills is skipped to avoid nesting
    sibling skills inside it.

    Args:
        skills_root: The destination skills directory to populate.
        paths: Skill source directories discovered via :func:`iter_skills`.

    Returns:
        The names of the skills materialized, in discovery order.
    """
    roots = {Path(os.path.expanduser(path)).resolve() for path in paths if path}
    discovered = list(iter_skills(paths))
    bundles = [Path(skill.path).parent.resolve() for skill in discovered]
    written: list[str] = []
    for skill, resolved_bundle in zip(discovered, bundles, strict=True):
        bundle = Path(skill.path).parent
        if resolved_bundle in roots and any(
            other != resolved_bundle and other.is_relative_to(resolved_bundle) for other in bundles
        ):
            _log.warning(
                "Skipping skill %r: its SKILL.md sits at the discovery root %s, so its "
                "bundle would be every other skill in that tree",
                skill.name,
                bundle,
            )
            continue
        dest_dir = skills_root / skill.name
        dest_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            bundle,
            dest_dir,
            dirs_exist_ok=True,
            symlinks=True,
            ignore=_ignore_escaping_links(bundle),
        )
        written.append(skill.name)
        _log.info("Linked skill %s -> %s", skill.name, dest_dir)
    return written
