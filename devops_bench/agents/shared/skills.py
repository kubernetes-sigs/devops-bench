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

"""Frontmatter parsing for ``SKILL.md`` files, shared by every agent.

Both the API agent (advertising skills as synthetic tools) and the CLI agents
(materializing skills into the launched CLI) need a skill file's ``name`` and
``description``; keeping the parser here lets either import it without reaching
into the other's package. Importing this module pulls no provider SDK.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import NamedTuple

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from devops_bench.core import get_logger

__all__ = ["SkillFile", "iter_skills", "parse_skill_md"]

_log = get_logger("agents.shared.skills")

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL | re.MULTILINE)

_SKILL_FILE = "SKILL.md"

# YAML 1.2 semantics (matching tasks/loader.py): only ``true``/``false`` are
# booleans, so a description like ``yes`` stays a plain string.
_yaml = YAML(typ="safe")


class SkillFile(NamedTuple):
    """One discovered skill: its frontmatter fields plus source location.

    Attributes:
        name: The ``name`` declared in the file's frontmatter.
        description: The ``description`` declared in the frontmatter, or ``None``.
        content: The full file text (frontmatter + body).
        path: Absolute or as-given path to the source ``SKILL.md``.
    """

    name: str
    description: str | None
    content: str
    path: str


def iter_skills(paths: Iterable[str]) -> Iterator[SkillFile]:
    """Discover ``SKILL.md`` files beneath ``paths`` with the shared semantics.

    Every harness discovers skills through this single walk so behavior cannot
    diverge per agent flavor:

    * each path is ``os.path.expanduser``-expanded (``~/skills`` works),
    * empty path strings are skipped, missing directories are warned and
      skipped,
    * files are visited in ``sorted(rglob)`` order for determinism,
    * names carrying a path separator, ``..``, or an absolute prefix are
      warned and skipped — consumers use the name as a directory or tool
      name, and such a name would escape the destination root,
    * duplicate skill names are first-wins with a warning,
    * files with no parseable ``name`` are skipped.

    Args:
        paths: Skill source directories to walk recursively.

    Yields:
        A :class:`SkillFile` per unique, parseable skill, in discovery order.
    """
    seen: set[str] = set()
    for raw_path in paths:
        if not raw_path:
            continue
        source = Path(os.path.expanduser(raw_path))
        if not source.exists():
            _log.warning("Skills directory not found: %s", source)
            continue
        for skill_file in sorted(source.rglob(_SKILL_FILE)):
            name, description, content = parse_skill_md(str(skill_file))
            if not name or content is None:
                continue
            # Path("..").name is ".." itself, so ".." needs its own rejection.
            if name == ".." or name != Path(name).name:
                _log.warning(
                    "Skipping skill %r from %s: name must not contain path separators, "
                    "'..', or an absolute prefix",
                    name,
                    skill_file,
                )
                continue
            if name in seen:
                _log.warning(
                    "Skipping duplicate skill %r from %s: already discovered from an "
                    "earlier source",
                    name,
                    skill_file,
                )
                continue
            seen.add(name)
            yield SkillFile(
                name=name, description=description, content=content, path=str(skill_file)
            )


def parse_skill_md(file_path: str) -> tuple[str | None, str | None, str | None]:
    """Parse a ``SKILL.md`` file's YAML frontmatter.

    The frontmatter block is parsed as safe YAML, so multi-line block scalars
    (e.g. a ``description: >-`` spanning several lines) are read in full
    rather than truncated to the first line.

    Args:
        file_path: Path to a skill markdown file.

    Returns:
        A ``(name, description, content)`` tuple. ``name``/``description`` are
        ``None`` when the field is absent; ``content`` is the full file text, or
        ``None`` when the file is unreadable or carries no frontmatter block.
    """
    try:
        with open(file_path, encoding="utf-8") as f:
            content = f.read()
    except OSError as exc:
        _log.warning("Error parsing skill file %s: %s", file_path, exc)
        return None, None, None

    match = _FRONTMATTER_RE.search(content)
    if not match:
        return None, None, None

    try:
        frontmatter = _yaml.load(match.group(1))
    except YAMLError as exc:
        _log.warning("Invalid YAML frontmatter in skill file %s: %s", file_path, exc)
        return None, None, content

    if not isinstance(frontmatter, dict):
        return None, None, content

    name = frontmatter.get("name")
    description = frontmatter.get("description")
    name = str(name).strip() if name is not None else None
    description = str(description).strip() if description is not None else None
    return name, description, content
