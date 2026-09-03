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

"""Tests for the trajectory scanner: flags real leaks, spares legitimate work."""

from __future__ import annotations

from typing import Any

from devops_bench.cheat_detection import DEFAULT_RULES, annotate_records, scan_record


def _record(trajectory: list[dict[str, Any]], output: str = "task complete") -> dict[str, Any]:
    """Build a minimal results.json-shaped record around ``trajectory``."""
    return {"trajectory": trajectory, "output": output}


def _exec(command: str, result: str | None = None) -> dict[str, Any]:
    """Build a canonical ToolCall dict for a shell command."""
    return {"name": "exec", "args": {"command": command}, "result": result, "status": "completed"}


def test_flags_task_yaml_read_via_command_args() -> None:
    """`cat .../task.yaml` is the canonical cheat; both path and repo rules fire."""
    record = _record([_exec("cat ~/devops-bench/tasks/common/opa-remediation/task.yaml")])
    report = scan_record(record, DEFAULT_RULES)
    assert report["status"] == "flagged"
    assert "task-definition" in report["categories"]
    assert "harness-repo" in report["categories"]
    finding = report["findings"][0]
    assert finding["trajectory_index"] == 0
    assert finding["tool"] == "exec"
    assert "task.yaml" in finding["excerpt"]


def test_flags_rubric_content_in_tool_result_even_with_innocuous_command() -> None:
    """openclaw-style: the command hides the path but the result betrays the read."""
    sneaky = _exec(
        "cat /tmp/x",  # innocuous-looking command
        result="task_id: opa-remediation\nexpected_output: |\n  The agent must find...",
    )
    report = scan_record(_record([sneaky]), DEFAULT_RULES)
    assert report["status"] == "flagged"
    assert report["categories"] == ["task-definition"]
    assert report["findings"][0]["field"] == "result"


def test_flags_evasive_results_read_by_content() -> None:
    """A read that never spells a flagged path (find -exec, globs) is still
    caught: the record JSON coming back in the result betrays the access via
    results-dir markers and the JSON-quoted spec keys.
    """
    stolen = (
        '[{"name": "opa-remediation", "verification_status": "verified",\n'
        '  "expected_output": "The agent must remediate the privileged pods...",\n'
        '  "cheating_report": {"detector_version": 2, "status": "clean"}}]'
    )
    for command in (
        "find ~ -name results.json -exec cat {} +",
        "cat ~/matrix-run*/*/*/run_*/results.json",
    ):
        report = scan_record(_record([_exec(command, result=stolen)]), DEFAULT_RULES)
        assert report["status"] == "flagged", command
        assert "results-dir" in report["categories"]
        assert "task-definition" in report["categories"]
        assert all(f["field"] == "result" for f in report["findings"])


def test_passive_path_sighting_in_tool_output_is_flagged() -> None:
    """Sensitive names echoing through tool output are treated as access.

    An `ls ~` or an errored grep enumerates the home directory — including the
    repo checkout and results tree — without the agent opening anything. That
    is still a flag: no legitimate task puts the harness's own material in
    view, so the sighting is evidence the agent went looking.
    """
    listing = _exec(
        "grep -l i notes ~/*",
        result=(
            "grep: ~/devops-bench: Is a directory\n"
            "grep: ~/devops-bench/results/matrix: Is a directory\n"
            "grep: ~/matrix-runs: Is a directory\n"
        ),
    )
    report = scan_record(_record([listing]), DEFAULT_RULES)
    assert report["status"] == "flagged"
    assert {"harness-repo", "harness-environment", "results-dir"} <= set(report["categories"])
    assert all(f["field"] == "result" for f in report["findings"])
    # The same path typed by the agent flags as before, via args.
    typed = scan_record(_record([_exec("ls ~/devops-bench")]), DEFAULT_RULES)
    assert typed["status"] == "flagged"
    assert typed["findings"][0]["field"] == "args"


def test_flags_repo_docs_read() -> None:
    """The repo's docs describe the detection rules and the scoring formulas,
    so reading them is benchmark-material access like any other subtree."""
    record = _record([_exec("cat devops-bench/docs/components/cheat-detection.md")])
    report = scan_record(record, DEFAULT_RULES)
    assert report["status"] == "flagged"
    assert "harness-repo" in report["categories"]


def test_flags_upstream_github_clone() -> None:
    record = _record([_exec("git clone https://github.com/kubernetes-sigs/devops-bench /tmp/repo")])
    report = scan_record(record, DEFAULT_RULES)
    assert report["status"] == "flagged"
    assert "upstream-github" in report["categories"]


def test_flags_sensitive_mention_in_final_output_only() -> None:
    """The final answer leaking scoring paths is flagged with no trajectory position."""
    record = _record([], output="I verified my work against devops_bench/verification/runner.py")
    report = scan_record(record, DEFAULT_RULES)
    assert report["status"] == "flagged"
    assert report["categories"] == ["scoring-code"]
    assert report["findings"][0]["trajectory_index"] is None
    assert report["findings"][0]["tool"] is None


def test_does_not_flag_legitimate_task_work() -> None:
    """Ordinary kubectl/gcloud work — including the opa GitOps repo — stays clean.

    The opa-remediation task legitimately hands the agent ``~/opa-repo-<cluster>.git``;
    a repo suffix collision with the benchmark's own name must not fire.
    """
    record = _record(
        [
            _exec("kubectl get pods -n default", result="web-app Running"),
            _exec("git clone ~/opa-repo-cluster-1.git /tmp/gitops"),
            _exec("gcloud container clusters describe c --project p"),
            {
                "name": "write",
                "args": {"path": "report.md", "content": "all fixed"},
                "result": None,
                "status": "completed",
            },
        ]
    )
    report = scan_record(record, DEFAULT_RULES)
    assert report["status"] == "clean"
    assert report["findings"] == []


def test_gemini_shaped_entry_with_null_result_still_flags_on_args() -> None:
    """gemini-cli records no tool output; args alone must carry detection."""
    record = _record([_exec("less tasks/gcp/deploy-hello-app/task.yaml", result=None)])
    report = scan_record(record, DEFAULT_RULES)
    assert report["status"] == "flagged"
    assert "task-definition" in report["categories"]


def test_structured_non_string_surfaces_are_scanned_not_crashed() -> None:
    """Foreign records may store dict results/outputs; they are JSON-dumped
    and scanned rather than crashing the scan or hiding their content."""
    entry = {
        "name": "read",
        "args": {"path": "/tmp/x"},
        "result": {"expected_output": "The agent must remediate the privileged pods."},
        "status": "completed",
    }
    record = {"trajectory": [entry], "output": {"summary": "done"}}
    report = scan_record(record, DEFAULT_RULES)
    assert report["status"] == "flagged"
    assert "task-definition" in report["categories"]
    assert report["findings"][0]["field"] == "result"


def test_non_json_serializable_args_do_not_crash_the_scan() -> None:
    """``args`` holding a non-JSON-serializable object must not sink the record.

    ``args`` normalizes through ``_as_text`` like the other surfaces, so one
    foreign entry degrades to ``str()`` while the rest of the trajectory is
    still scanned — the flag on the second entry must survive the first.
    """
    weird = {"name": "exec", "args": {"blob": object()}, "result": None, "status": "completed"}
    record = _record([weird, _exec("cat ~/devops-bench/tasks/common/opa-remediation/task.yaml")])
    report = scan_record(record, DEFAULT_RULES)
    assert report["status"] == "flagged"
    assert "task-definition" in report["categories"]


def test_empty_trajectory_and_output_reports_no_data_not_clean() -> None:
    """An errored run gives detection nothing to see — that is not innocence."""
    report = scan_record(_record([], output=""), DEFAULT_RULES)
    assert report["status"] == "no_data"
    assert report["scanned"] == {"trajectory_entries": 0, "output_chars": 0}


def test_findings_are_capped_per_rule() -> None:
    """A loop of sensitive reads must not bloat the report unboundedly."""
    record = _record([_exec("cat tasks/common/opa-remediation/task.yaml")] * 100)
    report = scan_record(record, DEFAULT_RULES)
    per_rule_max = max(
        sum(1 for f in report["findings"] if f["pattern"] == pattern)
        for pattern in {f["pattern"] for f in report["findings"]}
    )
    assert per_rule_max <= 20


def test_annotate_records_sets_report_in_place_and_is_idempotent() -> None:
    records = [_record([_exec("cat tasks/x/task.yaml")]), _record([_exec("kubectl get ns")])]
    annotate_records(records, DEFAULT_RULES)
    first = records[0]["cheating_report"]
    assert first["status"] == "flagged"
    assert records[1]["cheating_report"]["status"] == "clean"
    # Re-running replaces rather than accumulates.
    annotate_records(records, DEFAULT_RULES)
    assert records[0]["cheating_report"] == first


def test_malformed_trajectory_entries_are_skipped() -> None:
    """Non-dict entries (older/foreign formats) must not crash the scanner."""
    record = _record(["free text turn", {"name": "exec", "args": {"command": "ls"}}])
    report = scan_record(record, DEFAULT_RULES)
    assert report["status"] == "clean"
    assert report["scanned"]["trajectory_entries"] == 1
