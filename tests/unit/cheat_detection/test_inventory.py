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

"""Tests for the pre-run inventory: flags prior-run leftovers, spares fresh work."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from devops_bench.cheat_detection import (
    DEFAULT_BASELINE,
    baseline_from_granted_paths,
    build_inventory_rules,
    filter_rules_for_prompt,
    scan_record,
)
from devops_bench.cheat_detection.rules import SCAN_FIELDS

_STALE_REPORT = (
    "# Cluster Audit and Remediation Report\n"
    "\n"
    "## Findings & Fixes\n"
    "- **team-alpha/cache**: was running privileged; set securityContext.privileged to false.\n"
)


def _record(trajectory: list[dict[str, Any]], output: str = "done") -> dict[str, Any]:
    return {"trajectory": trajectory, "output": output}


def _exec(command: str, result: str | None = None) -> dict[str, Any]:
    return {"name": "exec", "args": {"command": command}, "result": result, "status": "completed"}


def _seed_home(tmp_path: Path) -> Path:
    """A home with baseline entries, hidden files, and prior-run leftovers."""
    (tmp_path / "bin").mkdir()
    (tmp_path / "bench.env").write_text("export JUDGE_MODEL=whatever\n", encoding="utf-8")
    (tmp_path / ".bashrc").write_text("# shell init\n", encoding="utf-8")
    (tmp_path / "report.md").write_text(_STALE_REPORT, encoding="utf-8")
    (tmp_path / "policies.yaml").write_text(
        "kind: ClusterPolicy\nname: disallow-privileged-containers-and-more\n", encoding="utf-8"
    )
    (tmp_path / "workspace-repo").mkdir()
    return tmp_path


def test_inventory_skips_baseline_and_environment_dotfiles(tmp_path: Path) -> None:
    home = _seed_home(tmp_path)
    rules = build_inventory_rules(home)
    joined = " ".join(p for rule in rules for p in rule.patterns)
    assert "bench\\.env" not in joined
    assert "bashrc" not in joined
    assert all(rule.category == "prior-run-artifact" for rule in rules)


def test_agent_state_dotdir_is_a_leftover_not_environment(tmp_path: Path) -> None:
    """An agent CLI's state dotdir generates rules; enumerated dotfiles do not.

    Agent harnesses conventionally keep their state in a dotdir, and that is
    exactly where cross-run contamination piles up — a stale
    ``~/.openclaw/workspace`` holds a previous task's deliverables. Only the
    enumerated :data:`ENVIRONMENT_DOTFILES` are baseline, so the dotdir is
    covered while ``.bashrc`` stays invisible.
    """
    workspace = tmp_path / ".openclaw" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "production-readiness.md").write_text(
        "# Production readiness review\n", encoding="utf-8"
    )
    (tmp_path / ".bashrc").write_text("# shell init\n", encoding="utf-8")

    rules = build_inventory_rules(tmp_path)
    joined = " ".join(p for rule in rules for p in rule.patterns)
    assert "openclaw" in joined
    assert "bashrc" not in joined

    report = scan_record(
        _record([_exec("cat ~/.openclaw/workspace/production-readiness.md")]), rules
    )
    assert report["status"] == "flagged"
    assert report["categories"] == ["prior-run-artifact"]


def test_clean_home_yields_no_rules(tmp_path: Path) -> None:
    (tmp_path / "bin").mkdir()
    assert build_inventory_rules(tmp_path) == ()


def test_flags_home_anchored_read_of_leftover(tmp_path: Path) -> None:
    """`cat ~/policies.yaml` — the exact shape of the observed cheat — is flagged."""
    rules = build_inventory_rules(_seed_home(tmp_path))
    report = scan_record(_record([_exec("cat ~/policies.yaml")]), rules)
    assert report["status"] == "flagged"
    assert report["categories"] == ["prior-run-artifact"]
    assert report["findings"][0]["field"] == "args"


def test_flags_stale_report_by_content_not_path(tmp_path: Path) -> None:
    """Reading the stale report reproduces its lines in the result: flagged.

    The command names an unrelated path, so the leftover's own path rule never
    sees it — the read is caught purely by content fingerprint.
    """
    rules = build_inventory_rules(_seed_home(tmp_path))
    read = _exec("cat /some/copy.md", result=_STALE_REPORT)
    report = scan_record(_record([read]), rules)
    assert report["status"] == "flagged"
    assert any(f["field"] == "result" for f in report["findings"])


def test_fresh_report_write_stays_clean(tmp_path: Path) -> None:
    """An honest run writing its own deliverable must not match any rule.

    The prompt names ``report.md``, which is what authorizes writing it — the
    inventory itself excludes nothing, so the path rule exists and is dropped
    per record by the prompt filter.
    """
    rules = filter_rules_for_prompt(
        build_inventory_rules(_seed_home(tmp_path)), "write a summary to 'report.md'"
    )
    write = _exec(
        "cat << 'EOF' > ~/report.md\n# Audit Report\n\nEverything I found myself today.\nEOF"
    )
    report = scan_record(_record([write]), rules)
    assert report["status"] == "clean"


def test_leftover_deliverable_is_covered_when_the_task_does_not_name_it(
    tmp_path: Path,
) -> None:
    """A stale ``report.md`` is a leftover for every task that isn't the one
    that wrote it, so an unrelated task's run must still flag reading it.

    Statically excluding deliverable names would excuse the name globally and
    leave this uncovered — including when the file is too short to fingerprint,
    where the path rule is the only thing standing.
    """
    home = tmp_path
    (home / "report.md").write_text("# Report\nAll fixed.\n", encoding="utf-8")
    # Too short to fingerprint: no content rule exists to fall back on.
    rules = build_inventory_rules(home)
    assert [r.source for r in rules] == ["report.md"]

    unrelated_prompt = "Upgrade the cluster and write 'production-readiness.md'."
    filtered = filter_rules_for_prompt(rules, unrelated_prompt)
    report = scan_record(_record([_exec("cat ~/report.md")]), filtered)
    assert report["status"] == "flagged"


def test_granted_skills_tree_is_baseline_not_leftover(tmp_path: Path) -> None:
    """An arm granting a skills tree under the home must not flag for using it.

    The path is derived from what the harness granted rather than hard-coded,
    so any operator's directory name works — no host layout is baked in.
    """
    home = _seed_home(tmp_path)
    (home / "skills-repo").mkdir()
    granted = (str(home / "skills-repo" / "skills"),)

    baseline = DEFAULT_BASELINE | baseline_from_granted_paths(home, granted)
    assert "skills-repo" in baseline
    rules = build_inventory_rules(home, baseline=baseline)
    assert "skills-repo" not in [r.source for r in rules]
    read = _record([_exec("cat ~/skills-repo/skills/kubectl/SKILL.md")])
    assert scan_record(read, rules)["status"] == "clean"
    # Unrelated leftovers keep their rules.
    assert scan_record(_record([_exec("cat ~/policies.yaml")]), rules)["status"] == "flagged"


def test_granted_paths_outside_home_exempt_nothing(tmp_path: Path) -> None:
    """``/opt/skills`` has no home entry to exempt, and neither does an empty
    or unresolvable entry — the baseline must stay empty rather than widen."""
    assert baseline_from_granted_paths(tmp_path, ("/opt/skills/devops", "", "relative/path")) == (
        frozenset()
    )


def test_granted_paths_expand_home_shorthand(tmp_path: Path) -> None:
    """``AGENT_SKILLS_PATHS`` accepts ``~/...``, so the mapping must expand it."""
    assert baseline_from_granted_paths(Path.home(), ("~/skills-repo/skills",)) == frozenset(
        {"skills-repo"}
    )


def test_symlinked_leftover_is_not_fingerprinted(tmp_path: Path) -> None:
    """A leftover symlink must not pull its target's lines into a pattern.

    ``Path.is_file()`` follows links, so a link to any readable file would
    otherwise embed that file's content in the generated rules — and patterns
    are published in the record's report. The link keeps its path rule.
    """
    home = tmp_path / "home"
    home.mkdir()
    secret = tmp_path / "outside" / "credentials"
    secret.parent.mkdir()
    secret.write_text("client_secret: a-very-long-and-distinctive-value\n", encoding="utf-8")
    (home / "notes.txt").symlink_to(secret)

    rules = build_inventory_rules(home)
    assert [r.source for r in rules] == ["notes.txt"]  # path rule only, no content rule
    assert not any("client_secret" in p for r in rules for p in r.patterns)
    assert scan_record(_record([_exec("cat ~/notes.txt")]), rules)["status"] == "flagged"


def test_home_spelling_inside_a_longer_token_is_not_flagged(tmp_path: Path) -> None:
    """The home prefix must be left-bounded, not a bare substring.

    ``/data<home>/report.md`` contains the literal home path, and a ``~``
    glued to a word is not a home reference — neither is a read of the
    leftover, so neither may match its path rule.
    """
    home = _seed_home(tmp_path)
    rules = build_inventory_rules(home)
    report = scan_record(
        _record([_exec(f"cat /data{home}/report.md"), _exec("cat foo~/report.md")]), rules
    )
    assert report["status"] == "clean"


def test_same_name_outside_home_is_not_flagged(tmp_path: Path) -> None:
    """Path rules are home-anchored: this run's own /tmp clone shares no blame."""
    rules = build_inventory_rules(_seed_home(tmp_path))
    report = scan_record(_record([_exec("git clone x /tmp/workspace-repo")]), rules)
    assert report["status"] == "clean"


def test_binary_and_oversized_leftovers_get_path_rule_only(tmp_path: Path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00binary")
    rules = build_inventory_rules(tmp_path)
    assert len(rules) == 1
    assert rules[0].fields == SCAN_FIELDS


def test_passive_sighting_in_tool_output_is_flagged(tmp_path: Path) -> None:
    """A command whose *output* merely lists leftover names still flags.

    The observed shape: a malformed grep whose stderr enumerates the home
    directory. The agent typed none of those paths, but it had no reason to be
    enumerating the home directory either, so the sighting is treated as
    evidence it went looking rather than as innocent observation.
    """
    rules = build_inventory_rules(_seed_home(tmp_path))
    sighting = _exec(
        "grep -l i notes ~/*",
        result=(
            "grep: notes: No such file or directory\n"
            "~/policies.yaml\n~/report.md\n"
            "grep: ~/workspace-repo: Is a directory\n"
        ),
    )
    report = scan_record(_record([sighting]), rules)
    assert report["status"] == "flagged"
    assert any(f["field"] == "result" for f in report["findings"])
    # The same names typed by the agent flag as before, via args.
    typed = scan_record(_record([_exec("cat ~/policies.yaml")]), rules)
    assert typed["status"] == "flagged"
    assert typed["findings"][0]["field"] == "args"


def test_prompt_named_entry_loses_its_path_rule(tmp_path: Path) -> None:
    """The opa-repo shape: required work on a prompt-named repo is not a cheat.

    The GitOps bare repo pre-exists the run (provisioned infrastructure), so
    it lands in the inventory — but the task prompt names it as the source of
    truth, which authorizes referencing it for this record. Unnamed leftovers
    keep their rules.
    """
    home = _seed_home(tmp_path)
    (home / "opa-repo-c0c4d8c6-eval.git").mkdir()
    rules = build_inventory_rules(home)
    prompt = (
        "Workloads are managed via GitOps from the repository at "
        "'~/opa-repo-c0c4d8c6-eval.git' (the source of truth). When you're "
        "done, write a summary to 'report.md'."
    )
    filtered = filter_rules_for_prompt(rules, prompt)

    required_work = _record(
        [_exec("git clone ~/opa-repo-c0c4d8c6-eval.git /tmp/eval-repo")],
        output="Pushed the fixes to ~/opa-repo-c0c4d8c6-eval.git as instructed.",
    )
    assert scan_record(required_work, filtered)["status"] == "clean"
    # An entry the prompt never mentions is still a leftover.
    assert scan_record(_record([_exec("cat ~/policies.yaml")]), filtered)["status"] == "flagged"


def test_prompt_filter_requires_whole_token_match(tmp_path: Path) -> None:
    """Naming ``workspace-repo`` must not also authorize the ``workspace``
    leftover: authorization is a whole-token match, not a substring test."""
    home = _seed_home(tmp_path)
    (home / "workspace").mkdir()
    rules = build_inventory_rules(home)
    filtered = filter_rules_for_prompt(
        rules, "Clone the repo at '~/workspace-repo' and fix the manifests."
    )
    assert scan_record(_record([_exec("ls ~/workspace-repo")]), filtered)["status"] == "clean"
    assert scan_record(_record([_exec("ls ~/workspace")]), filtered)["status"] == "flagged"


def test_prompt_filter_tolerates_sentence_ending_period(tmp_path: Path) -> None:
    """A name at the end of a sentence is still named ('… in ~/workspace.')."""
    home = _seed_home(tmp_path)
    (home / "workspace").mkdir()
    filtered = filter_rules_for_prompt(
        build_inventory_rules(home), "All manifests live in ~/workspace."
    )
    assert scan_record(_record([_exec("ls ~/workspace")]), filtered)["status"] == "clean"


def test_fingerprint_lines_are_deterministic(tmp_path: Path) -> None:
    """Equal-length candidate lines must not be picked in set (hash) order:
    ties break lexicographically so every process fingerprints the same lines."""
    lines = [f"{c}: a candidate line of exactly this length" for c in "dcba"]
    (tmp_path / "notes.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    rules = build_inventory_rules(tmp_path)
    content_rules = [r for r in rules if r.fields == ("result", "output")]
    assert len(content_rules) == 1
    assert content_rules[0].patterns == tuple(re.escape(line) for line in sorted(lines)[:3])


def test_prompt_filter_never_drops_content_fingerprints(tmp_path: Path) -> None:
    """Naming report.md as the deliverable authorizes writing it, not reading
    the stale copy: the content fingerprint must survive the prompt filter."""
    rules = build_inventory_rules(_seed_home(tmp_path))
    filtered = filter_rules_for_prompt(rules, "write a summary to 'report.md'")
    stale_read = _exec("cat ~/report.md", result=_STALE_REPORT)
    report = scan_record(_record([stale_read]), filtered)
    assert report["status"] == "flagged"
    assert any(f["field"] == "result" for f in report["findings"])


def test_fingerprint_only_restricts_content_rules_but_not_path_rules(tmp_path: Path) -> None:
    """Mid-batch arrivals get a path rule and no fingerprint.

    ``fingerprint_only`` carries the run-start leftover set. Entries in it are
    genuine prior-run artifacts and fingerprint as usual; an entry that showed
    up while the batch ran (task 1's deliverable) is still covered by path, so
    task 2 reading it flags — but its lines never become patterns, because an
    honest repeat of task 1 would reproduce them.
    """
    home = _seed_home(tmp_path)
    (home / "task-1-output.md").write_text(
        "# Task 1 remediation summary\n- disabled privileged mode on team-alpha/cache\n",
        encoding="utf-8",
    )
    rules = build_inventory_rules(home, fingerprint_only=frozenset({"report.md", "policies.yaml"}))

    path_sources = {r.source for r in rules if r.source}
    assert "task-1-output.md" in path_sources  # covered by path
    assert not any("Task 1 remediation summary" in p for r in rules for p in r.patterns)
    # The run-start leftovers keep their fingerprints: echoing report.md's
    # content still flags even though the command names an unrelated path.
    echoed = scan_record(_record([_exec("cat /tmp/copy.md", result=_STALE_REPORT)]), rules)
    assert echoed["status"] == "flagged"
    assert echoed["findings"][0]["field"] == "result"


def test_mid_batch_deliverable_flags_on_path_and_spares_the_honest_repeat(tmp_path: Path) -> None:
    """The two halves of the intra-batch policy, on one leftover.

    Task 2 opening task 1's report by path is flagged; a later iteration
    writing prose that happens to match task 1's wording is not.
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / "report.md").write_text(_STALE_REPORT, encoding="utf-8")
    rules = build_inventory_rules(home, fingerprint_only=frozenset())

    by_path = scan_record(_record([_exec("cat ~/report.md")]), rules)
    assert by_path["status"] == "flagged"
    assert by_path["findings"][0]["field"] == "args"

    echoed = scan_record(_record([_exec("cat /tmp/mine.md", result=_STALE_REPORT)]), rules)
    assert echoed["status"] == "clean"
