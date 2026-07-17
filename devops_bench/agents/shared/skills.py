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

import re

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from devops_bench.core import get_logger

__all__ = ["parse_skill_md"]

_log = get_logger("agents.shared.skills")

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL | re.MULTILINE)

# YAML 1.2 semantics (matching tasks/loader.py): only ``true``/``false`` are
# booleans, so a description like ``yes`` stays a plain string.
_yaml = YAML(typ="safe")


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
