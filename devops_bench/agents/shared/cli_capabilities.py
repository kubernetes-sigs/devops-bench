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
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from devops_bench.agents.shared.skills import iter_skills
from devops_bench.core import get_logger

if TYPE_CHECKING:
    from devops_bench.agents.capabilities import McpBinding

__all__ = ["agent_workdir", "build_mcp_servers", "materialize_skills"]

_log = get_logger("agents.shared.cli_capabilities")

_SKILL_FILE = "SKILL.md"


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

    Bindings with an empty ``command`` are skipped: a CLI needs a command to
    spawn a stdio MCP server, and an empty-command binding denotes a server the
    binary already hosts itself.

    If the command is path-like (contains a path separator) and exists on disk,
    it is resolved to its absolute path to prevent execution ambiguity in the
    agent's workspace. If it does not exist, a warning is logged.

    Args:
        mcp_servers: Bindings granted for the run.

    Returns:
        A ``{name: {"command": ..., "args": [...]}}`` mapping suitable for the
        agent's MCP-servers config section. Empty when no binding carries a
        command.
    """
    servers: dict[str, dict] = {}
    for index, binding in enumerate(mcp_servers):
        if not binding.command:
            continue
        name = binding.name or f"mcp{index}"
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
        servers[name] = entry
    return servers


def materialize_skills(skills_root: Path, paths: tuple[str, ...]) -> list[str]:
    """Copy discovered ``SKILL.md`` files into a CLI's workspace skills tree.

    For each ``SKILL.md`` found beneath ``paths`` (the same discovery the API
    agent performs), the file is written to ``skills_root/<name>/SKILL.md`` using
    the ``name`` from its frontmatter.

    Args:
        skills_root: The destination skills directory to populate.
        paths: Skill source directories, discovered via the shared
            :func:`~devops_bench.agents.shared.skills.iter_skills` walk
            (expanduser, sorted order, escaping/duplicate names warned and
            skipped, missing paths warned).

    Returns:
        The names of the skills materialized, in discovery order.
    """
    written: list[str] = []
    for skill in iter_skills(paths):
        dest_dir = skills_root / skill.name
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / _SKILL_FILE).write_text(skill.content, encoding="utf-8")
        written.append(skill.name)
        _log.info("Linked skill %s -> %s", skill.name, dest_dir)
    return written
