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

"""Local skill discovery for the API agent (decoupled from MCP).

A *skill* is a ``SKILL.md`` file under one of the configured paths whose
frontmatter declares a ``name`` and ``description``. Each discovered skill is
advertised to the model as a synthetic tool — invoking the tool returns the
file's contents. Skills are independent of MCP: an agent may have skills
without an MCP server (and vice versa); the API agent gates each on its own
config field.

This module performs only blocking filesystem I/O; importing it pulls no
provider SDK, ``mcp``, or ``deepeval``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from devops_bench.agents.shared.skills import iter_skills, parse_skill_md
from devops_bench.core import get_logger

__all__ = [
    "SkillToolInfo",
    "parse_skill_md",
    "discover_skill_tools",
]

_log = get_logger("agents.api.skills")


class SkillToolInfo:
    """Duck-typed stand-in for an MCP tool used to advertise a local skill.

    Mirrors the attribute surface (``name``, ``description``, ``inputSchema``)
    the provider adapters' ``format_tools`` reads from an MCP tool object, so a
    skill can be added to the same tool list as MCP tools without a separate
    formatting path.

    Attributes:
        name: Tool name as exposed to the model (prefixed ``skill_``).
        description: Free-form description shown to the model.
        inputSchema: JSON schema for arguments. Defaults to an empty object
            schema (skills take no arguments today) — never ``None``, because
            the Anthropic/OpenAI-style adapters forward the attribute verbatim
            and those APIs reject a null schema.
    """

    def __init__(self, name: str, description: str, inputSchema: Any = None) -> None:  # noqa: N803 - matches MCP tool attr
        self.name = name
        self.description = description
        self.inputSchema = (
            inputSchema if inputSchema is not None else {"type": "object", "properties": {}}
        )


def discover_skill_tools(
    paths: Iterable[str],
) -> tuple[list[SkillToolInfo], dict[str, str], list[str]]:
    """Discover skills under ``paths`` and build tool descriptors.

    Discovery goes through the shared
    :func:`~devops_bench.agents.shared.skills.iter_skills` walk, so semantics
    (expanduser, sorted order, first-wins dedupe, missing paths warned) are
    identical across harnesses. The file content is captured at discovery time
    so invoking a skill tool later serves the exact text that was advertised —
    no re-read, no window for the file to change or vanish mid-run.

    Performs blocking filesystem I/O; callers in async contexts should run this
    via :func:`asyncio.to_thread` to keep the event loop responsive.

    Args:
        paths: Filesystem locations to search (each is walked recursively for
            ``SKILL.md`` files).

    Returns:
        A ``(tools, resources, names)`` tuple:

        * ``tools`` — :class:`SkillToolInfo` descriptors ready to merge into the
          MCP tool list before formatting.
        * ``resources`` — map of normalized tool name to the skill file's full
          content, consumed by the agent's dispatch closure.
        * ``names`` — discovered skill names (the unnormalized values from each
          file's frontmatter); useful for diagnostics.
    """
    tools: list[SkillToolInfo] = []
    resources: dict[str, str] = {}
    names: list[str] = []

    for skill in iter_skills(paths):
        normalized = "skill_" + skill.name.replace("-", "_")
        tools.append(
            SkillToolInfo(
                name=normalized,
                description=skill.description or f"Exposes skill: {skill.name}",
            )
        )
        resources[normalized] = skill.content
        names.append(skill.name)
        _log.info("Loaded local skill as tool: %s -> %s", normalized, skill.path)

    return tools, resources, names
