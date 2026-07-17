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

"""Tests for the CLI capability helpers shared by the Gemini/openclaw agents."""

from __future__ import annotations

from pathlib import Path

import pytest

from devops_bench.agents.capabilities import McpBinding
from devops_bench.agents.shared.cli_capabilities import (
    agent_workdir,
    build_mcp_servers,
    materialize_skills,
)


def test_build_mcp_servers_maps_command_to_command_and_args() -> None:
    """``command[0]`` → ``command``; the remainder → ``args``."""
    servers = build_mcp_servers(
        (McpBinding(name="gke", command=("gke-mcp", "--flag", "v"), tools=("t",)),)
    )
    assert servers == {"gke": {"command": "gke-mcp", "args": ["--flag", "v"]}}


def test_build_mcp_servers_omits_args_for_bare_command() -> None:
    """A single-element command yields no ``args`` key."""
    servers = build_mcp_servers((McpBinding(name="gke", command=("gke-mcp",)),))
    assert servers == {"gke": {"command": "gke-mcp"}}


def test_build_mcp_servers_skips_command_less_bindings() -> None:
    """Empty-command bindings (in-process/built-in servers) are not launched."""
    servers = build_mcp_servers((McpBinding(name="builtin", command=(), tools=("t",)),))
    assert servers == {}


def test_build_mcp_servers_names_unnamed_bindings_by_index() -> None:
    """A binding with no name falls back to a positional ``mcp<index>`` key."""
    servers = build_mcp_servers((McpBinding(name="", command=("srv",)),))
    assert servers == {"mcp0": {"command": "srv"}}


def test_materialize_skills_writes_named_skill_files(tmp_path: Path) -> None:
    """Each discovered ``SKILL.md`` is copied under ``<root>/<name>/SKILL.md``."""
    src = tmp_path / "src"
    (src / "rotate").mkdir(parents=True)
    (src / "rotate" / "SKILL.md").write_text(
        "---\nname: rotate-secret\ndescription: rotate\n---\nbody\n",
        encoding="utf-8",
    )
    dest = tmp_path / "dest"

    written = materialize_skills(dest, (str(src),))

    assert written == ["rotate-secret"]
    copied = dest / "rotate-secret" / "SKILL.md"
    assert "body" in copied.read_text(encoding="utf-8")


def test_materialize_skills_rejects_malicious_names(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Frontmatter names that would escape ``skills_root`` are warned and skipped."""
    src = tmp_path / "src"
    abs_escape = tmp_path / "abs-escape"
    bad_names = ("../rel-escape", str(abs_escape), "nested/name", "..")
    for index, bad_name in enumerate(bad_names):
        skill_dir = src / f"skill{index}"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {bad_name}\ndescription: d\n---\nbody\n", encoding="utf-8"
        )
    good = src / "zz-good"
    good.mkdir(parents=True)
    good_skill = "---\nname: good-skill\ndescription: d\n---\nbody\n"
    (good / "SKILL.md").write_text(good_skill, encoding="utf-8")
    dest = tmp_path / "skills" / "dest"

    written = materialize_skills(dest, (str(src),))

    assert written == ["good-skill"]
    assert not (tmp_path / "skills" / "rel-escape").exists()
    assert not abs_escape.exists()
    assert sorted(p.name for p in dest.iterdir()) == ["good-skill"]
    assert "Skipping skill '../rel-escape'" in caplog.text


def test_materialize_skills_warns_and_keeps_first_on_duplicate_names(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A second SKILL.md with an already-seen name is skipped, not overwritten."""
    first = tmp_path / "src1" / "dup"
    second = tmp_path / "src2" / "dup"
    for source_dir, body in ((first, "first body"), (second, "second body")):
        source_dir.mkdir(parents=True)
        (source_dir / "SKILL.md").write_text(
            f"---\nname: dup-skill\ndescription: d\n---\n{body}\n", encoding="utf-8"
        )
    dest = tmp_path / "dest"

    written = materialize_skills(dest, (str(tmp_path / "src1"), str(tmp_path / "src2")))

    assert written == ["dup-skill"]
    assert "first body" in (dest / "dup-skill" / "SKILL.md").read_text(encoding="utf-8")
    assert "Skipping duplicate skill 'dup-skill'" in caplog.text


def test_materialize_skills_skips_missing_paths(tmp_path: Path) -> None:
    """A non-existent source path is warned and skipped, not fatal."""
    assert materialize_skills(tmp_path / "dest", (str(tmp_path / "nope"),)) == []


def test_build_mcp_servers_resolves_path_like_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Path-like commands that exist on disk are absolutized; bare commands are not."""
    monkeypatch.chdir(tmp_path)

    # 1. Path-like command that exists -> resolved to absolute
    local_cmd = tmp_path / "my-local-mcp"
    local_cmd.touch()
    servers = build_mcp_servers((McpBinding(name="gke", command=("./my-local-mcp",)),))
    assert servers["gke"]["command"] == str(local_cmd.resolve())

    # 2. Bare command exists as a file in cwd -> NOT resolved (stays bare)
    dummy_node = tmp_path / "node"
    dummy_node.touch()
    servers = build_mcp_servers((McpBinding(name="node_srv", command=("node",)),))
    assert servers["node_srv"]["command"] == "node"

    # 3. Path-like command that does NOT exist -> NOT resolved
    servers = build_mcp_servers((McpBinding(name="missing", command=("./missing-mcp",)),))
    assert servers["missing"]["command"] == "./missing-mcp"
    assert (
        "Path-like MCP command './missing-mcp' not found relative to harness; passing unchanged"
        in caplog.text
    )


def test_agent_workdir_yields_supplied_path_without_cleanup(tmp_path: Path) -> None:
    """A supplied ``workspace_path`` is yielded as-is and left in place afterward."""
    supplied = tmp_path / "workspace"
    supplied.mkdir()

    with agent_workdir(supplied, prefix="ignored-") as workdir:
        assert workdir == supplied
        (workdir / "marker.txt").write_text("artifact", encoding="utf-8")

    assert supplied.exists()
    assert (supplied / "marker.txt").read_text(encoding="utf-8") == "artifact"


def test_agent_workdir_creates_and_cleans_up_temp_dir_when_no_path_supplied() -> None:
    """``None`` falls back to a prefixed temp dir that is removed on exit."""
    with agent_workdir(None, prefix="agent-workdir-test-") as workdir:
        created = workdir
        assert workdir.is_dir()
        assert workdir.name.startswith("agent-workdir-test-")

    assert not created.exists()
