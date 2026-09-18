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

"""Tests for the publishable wording of a cheating report."""

from __future__ import annotations

from typing import Any

from devops_bench.cheat_detection import DEFAULT_RULES, describe_findings, scan_record
from devops_bench.cheat_detection.summary import _MAX_LOCATIONS


def _finding(
    rule: str = "task-definition/path",
    *,
    material: str = "task definition",
    evidence: str = "the path",
    severity: str = "high",
    field: str = "args",
    index: int | None = 0,
    tool: str | None = "run_command",
) -> dict[str, Any]:
    return {
        "rule": rule,
        "category": rule.split("/")[0],
        "material": material,
        "evidence": evidence,
        "severity": severity,
        "pattern": "irrelevant",
        "field": field,
        "trajectory_index": index,
        "tool": tool,
        "excerpt": "cat tasks/x/task.yaml  # never republished",
    }


def _report(*findings: dict[str, Any], categories: list[str] | None = None) -> dict[str, Any]:
    return {
        "status": "flagged",
        "categories": categories if categories is not None else [],
        "findings": list(findings),
    }


def test_a_typed_read_names_every_tool_call_that_typed_it() -> None:
    verdicts = describe_findings(
        _report(
            _finding(index=3, tool="view_file"),
            _finding(index=13, tool="run_command"),
        )
    )
    assert verdicts == [
        {
            "name": "task-definition/path",
            "reason": "task definition: read by the agent — view_file at step 3, "
            "run_command at step 13",
        }
    ]


def test_a_passive_sighting_beside_a_typed_read_is_a_footnote() -> None:
    """Both in one verdict, not two: the agent typing the path is the claim."""
    verdicts = describe_findings(
        _report(
            _finding(index=3, tool="view_file"),
            _finding(field="result", index=5, tool="run_command"),
            _finding(field="result", index=9, tool="run_command"),
        )
    )
    assert verdicts[0]["reason"] == (
        "task definition: read by the agent — view_file at step 3 (+2 passive sightings)"
    )


def test_locations_past_the_cap_become_a_count() -> None:
    """Three tool calls establish a pattern; the twentieth adds nothing."""
    verdicts = describe_findings(
        _report(*(_finding(index=i, tool="run_command") for i in range(_MAX_LOCATIONS + 2)))
    )
    assert verdicts[0]["reason"].endswith("(+2 more reads)")


def test_a_sighting_with_no_typed_read_says_what_appeared_and_where() -> None:
    """The distinction the wording exists for: appearing is not accessing."""
    verdicts = describe_findings(
        _report(
            _finding(
                "task-definition/content",
                evidence="rubric and verification-spec keys",
                field="result",
                index=11,
                tool="manage_task",
            )
        )
    )
    assert verdicts[0]["reason"] == (
        "task definition: rubric and verification-spec keys appeared in "
        "manage_task output at step 11"
    )


def test_a_finding_on_the_records_final_output_has_no_step() -> None:
    # ``output`` is scanned once, outside the trajectory, so it carries no index.
    verdicts = describe_findings(
        _report(_finding(field="output", index=None, tool=None, evidence="the path"))
    )
    assert verdicts[0]["reason"] == "task definition: the path appeared in the run's final output"


def test_typed_access_outranks_a_higher_severity_sighting() -> None:
    verdicts = describe_findings(
        _report(
            _finding("scoring-code/path", material="harness scoring code", field="result", index=1),
            _finding("harness-repo/path", material="benchmark repo checkout", severity="medium"),
        )
    )
    assert [v["name"] for v in verdicts] == ["harness-repo/path", "scoring-code/path"]


def test_a_pre_v8_report_falls_back_to_bare_category_names() -> None:
    # Findings written before the detector stamped rule ids support no sentence;
    # the categories are the strongest statement left, with no reason at all.
    legacy = {
        "status": "flagged",
        "detector_version": 6,
        "categories": ["harness-repo", "task-definition"],
        "findings": [{"category": "harness-repo", "pattern": "x", "field": "args"}],
    }
    assert describe_findings(legacy) == [
        {"name": "harness-repo", "reason": ""},
        {"name": "task-definition", "reason": ""},
    ]


def test_a_clean_report_yields_nothing() -> None:
    assert describe_findings({"status": "clean", "categories": [], "findings": []}) == []


def test_no_excerpt_text_reaches_a_verdict() -> None:
    """The hard rule: excerpts hold captured file content and stay in results.json.

    Scanned end to end rather than on a crafted finding, because the excerpt is
    built by the detector and a wording change is exactly the kind of edit that
    would reintroduce it.
    """
    record = {
        "trajectory": [
            {
                "name": "exec",
                "args": {"command": "cat ~/devops-bench/tasks/x/task.yaml"},
                "result": "expected_output: |\n  the grader's answer key",
            }
        ],
        "output": "done",
    }
    report = scan_record(record, DEFAULT_RULES)

    assert report["status"] == "flagged"
    joined = " ".join(v["reason"] for v in describe_findings(report))
    assert "answer key" not in joined
    assert all(f["excerpt"] not in joined for f in report["findings"])


def test_a_malformed_severity_or_tool_does_not_sink_the_row() -> None:
    """A foreign harness can put a non-string in ``name``, which lands in ``tool``.

    ``build_rows`` reads a persisted report, so an odd scalar has to degrade to
    a verdict rather than raise out of the sort.
    """
    report = {
        "findings": [
            {"rule": "r1", "material": "creds.json", "severity": {}, "tool": ["bash"]},
            {"rule": "r2", "material": "task.yaml", "field": "args", "tool": "cat"},
        ]
    }

    assert [v["name"] for v in describe_findings(report)] == ["r2", "r1"]
