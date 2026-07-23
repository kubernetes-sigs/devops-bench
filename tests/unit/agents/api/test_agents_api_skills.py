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

"""Unit tests for :mod:`devops_bench.agents.api.skills`."""

from __future__ import annotations

from pathlib import Path

import pytest

from devops_bench.agents.api.skills import (
    SkillToolInfo,
    discover_skill_tools,
    parse_skill_md,
)


def test_parse_skill_md_extracts_frontmatter(tmp_path: Path) -> None:
    f = tmp_path / "SKILL.md"
    f.write_text('---\nname: "my-skill"\ndescription: does things\n---\nbody text\n')
    name, description, content = parse_skill_md(str(f))
    assert name == "my-skill"
    assert description == "does things"
    assert "body text" in content


def test_parse_skill_md_strips_single_and_double_quotes(tmp_path: Path) -> None:
    f = tmp_path / "SKILL.md"
    f.write_text("---\nname: 'quoted'\ndescription: \"plain\"\n---\n")
    name, description, _ = parse_skill_md(str(f))
    assert name == "quoted"
    assert description == "plain"


def test_parse_skill_md_reads_multiline_block_scalar(tmp_path: Path) -> None:
    f = tmp_path / "SKILL.md"
    f.write_text(
        "---\n"
        "name: multi\n"
        "description: >-\n"
        "  use this skill when rotating\n"
        "  a secret across namespaces\n"
        "---\nbody\n"
    )
    name, description, _ = parse_skill_md(str(f))
    assert name == "multi"
    assert description == "use this skill when rotating a secret across namespaces"


def test_parse_skill_md_missing_file_returns_none() -> None:
    assert parse_skill_md("/nonexistent/SKILL.md") == (None, None, None)


def test_parse_skill_md_no_frontmatter_returns_none(tmp_path: Path) -> None:
    f = tmp_path / "SKILL.md"
    f.write_text("just body, no frontmatter")
    assert parse_skill_md(str(f)) == (None, None, None)


def test_discover_skill_tools_empty_paths_returns_empty_lists() -> None:
    tools, resources, names = discover_skill_tools(())
    assert tools == []
    assert resources == {}
    assert names == []


def test_discover_skill_tools_missing_path_is_skipped(tmp_path: Path) -> None:
    tools, resources, names = discover_skill_tools((str(tmp_path / "missing"),))
    assert tools == []
    assert resources == {}
    assert names == []


def test_discover_skill_tools_normalizes_dashes_to_underscores(tmp_path: Path) -> None:
    skill = tmp_path / "first" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    body = '---\nname: "my-cool-skill"\ndescription: ok\n---\nbody\n'
    skill.write_text(body)

    tools, resources, names = discover_skill_tools((str(tmp_path),))
    assert len(tools) == 1
    assert isinstance(tools[0], SkillToolInfo)
    assert tools[0].name == "skill_my_cool_skill"
    assert tools[0].description == "ok"
    # Resources carry the file *content* captured at discovery, so dispatch
    # serves exactly what was advertised with no re-read / TOCTOU window.
    assert resources == {"skill_my_cool_skill": body}
    assert names == ["my-cool-skill"]


def test_discover_skill_tools_falls_back_to_default_description(tmp_path: Path) -> None:
    """Missing description still loads the skill with a synthetic description."""
    skill = tmp_path / "SKILL.md"
    skill.write_text('---\nname: "noun"\n---\nbody\n')
    tools, _resources, _names = discover_skill_tools((str(tmp_path),))
    assert tools[0].description == "Exposes skill: noun"


def test_discover_skill_tools_walks_multiple_paths(tmp_path: Path) -> None:
    a = tmp_path / "a" / "SKILL.md"
    b = tmp_path / "b" / "SKILL.md"
    for path, name in [(a, "alpha"), (b, "beta")]:
        path.parent.mkdir(parents=True)
        path.write_text(f'---\nname: "{name}"\ndescription: d\n---\n')

    tools, _resources, names = discover_skill_tools((str(a.parent), str(b.parent)))
    assert sorted(t.name for t in tools) == ["skill_alpha", "skill_beta"]
    assert sorted(names) == ["alpha", "beta"]


def test_discover_skill_tools_skips_empty_path_strings(tmp_path: Path) -> None:
    # Empty / whitespace-only entries from CSV parsing must not be treated as
    # the current working directory.
    tools, _resources, _names = discover_skill_tools(("", str(tmp_path)))
    assert tools == []


def test_skill_tool_info_defaults_to_empty_object_schema() -> None:
    """``inputSchema`` must never be ``None`` — the Anthropic/OpenAI-style
    adapters forward the attribute verbatim and those APIs reject a null
    schema (regression for the review finding that broke every skills-enabled
    Anthropic run on turn 1)."""
    tool = SkillToolInfo(name="skill_x", description="d")
    assert tool.inputSchema == {"type": "object", "properties": {}}
    # Fresh dict per instance — no shared mutable default.
    assert SkillToolInfo(name="skill_y", description="d").inputSchema is not tool.inputSchema


def test_discover_skill_tools_expands_user_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``~/...`` skills path must work — the shared walk expanduser-expands it
    (previously the API agent warned-and-skipped where CLI agents loaded it)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    skill = tmp_path / "sk" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text('---\nname: "home-skill"\ndescription: d\n---\nbody\n')
    tools, _resources, names = discover_skill_tools(("~/sk",))
    assert [t.name for t in tools] == ["skill_home_skill"]
    assert names == ["home-skill"]


def test_discover_skill_tools_dedupes_normalized_name_collisions(tmp_path: Path) -> None:
    """Distinct raw names that normalize to the same tool name (``my-skill``
    vs ``my_skill``) must not advertise duplicate tools or silently overwrite
    the resources map — first wins, later collisions are warned and skipped."""
    for d, name, body in [("a", "my-skill", "first"), ("b", "my_skill", "second")]:
        f = tmp_path / d / "SKILL.md"
        f.parent.mkdir(parents=True)
        f.write_text(f'---\nname: "{name}"\ndescription: d\n---\n{body}\n')

    tools, resources, names = discover_skill_tools((str(tmp_path),))

    assert [t.name for t in tools] == ["skill_my_skill"]
    assert resources["skill_my_skill"].endswith("first\n")
    assert names == ["my-skill"]


def test_discover_skill_tools_dedupes_duplicate_names_first_wins(tmp_path: Path) -> None:
    """Duplicate skill names are first-wins in sorted order (shared-walk
    semantics) — previously both were advertised and the resources map was
    last-wins, nondeterministically."""
    for d, desc in [("a", "first"), ("b", "second")]:
        f = tmp_path / d / "SKILL.md"
        f.parent.mkdir(parents=True)
        f.write_text(f'---\nname: "dup"\ndescription: {desc}\n---\nbody-{d}\n')
    tools, resources, names = discover_skill_tools((str(tmp_path),))
    assert len(tools) == 1
    assert tools[0].description == "first"
    assert resources["skill_dup"].endswith("body-a\n")
    assert names == ["dup"]


def test_skills_module_pulls_no_heavy_dependencies() -> None:
    """``skills`` does only stdlib filesystem work — no SDK/deepeval/mcp."""
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent(
        """
        import sys
        import devops_bench.agents.api.skills  # noqa: F401
        forbidden = ("mcp", "deepeval", "anthropic", "google.genai", "openai")
        hits = sorted(
            m for m in sys.modules
            if any(m == p or m.startswith(p + ".") for p in forbidden)
        )
        if hits:
            sys.stderr.write("LEAKED:" + ",".join(hits))
            sys.exit(1)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
