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

"""Tests for combining per-task parallel runs into one batch run."""

import json

from devops_bench.results import (
    aggregate,
    build_manifests,
    dedupe_latest,
    discover_row_files,
    rebatch_rows,
)


def _row(**overrides):
    """One per-task row dict in the reporter's camelCase shape."""
    base = dict(
        setupId="m-h-mcp",
        model="m",
        harness="h",
        augmentation=["mcp"],
        runId="run_20260601_000001_taskA__m__h",
        t="2026-06-01T00:00:01Z",
        taskFolder="task-a",
        taskName="task-a",
        iteration=0,
        outcomeScore=1.0,
        toolScore=None,
        latencySec=1.0,
        inputTokens=10,
        outputTokens=5,
        status="success",
        validated=False,
    )
    base.update(overrides)
    return base


def _write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows), encoding="utf-8")


def test_rebatch_stamps_shared_run_id_and_t():
    rows = [
        _row(taskFolder="task-a", taskName="task-a", t="2026-06-01T00:00:01Z", runId="run_a"),
        _row(taskFolder="task-b", taskName="task-b", t="2026-06-01T00:00:09Z", runId="run_b"),
    ]
    out = rebatch_rows(rows, run_id="run_20260601_120000_42", t="2026-06-01T12:00:00Z")
    assert {r.run_id for r in out} == {"run_20260601_120000_42"}
    assert {r.t for r in out} == {"2026-06-01T12:00:00Z"}
    # Per-task identity is untouched, so the doc-id stays unique per task.
    assert [r.task_folder for r in out] == ["task-a", "task-b"]


def test_dedupe_keeps_latest_original_t():
    rows = [
        _row(taskFolder="task-a", t="2026-06-01T00:00:01Z", outcomeScore=0.0),
        _row(taskFolder="task-a", t="2026-06-01T05:00:00Z", outcomeScore=1.0),  # retry, newer
    ]
    kept = dedupe_latest(rows)
    assert len(kept) == 1
    assert kept[0]["outcomeScore"] == 1.0


def test_build_manifests_one_per_setup():
    rows = rebatch_rows(
        [
            _row(setupId="m-h-mcp", taskFolder="task-a", taskName="task-a"),
            _row(setupId="m-h-mcp", taskFolder="task-b", taskName="task-b"),
            _row(setupId="m-h", augmentation=[], taskFolder="task-a", taskName="task-a"),
        ],
        run_id="run_x",
        t="2026-06-01T12:00:00Z",
    )
    manifests = build_manifests(rows, run_id="run_x", t="2026-06-01T12:00:00Z")
    assert [m.setup_id for m in manifests] == ["m-h-mcp", "m-h"]
    assert all(m.run_id == "run_x" and m.t == "2026-06-01T12:00:00Z" for m in manifests)


def test_aggregate_collapses_per_task_runs_into_one_run(tmp_path):
    # Two tasks of one arm, run as separate processes (distinct runId + t).
    _write_rows(tmp_path / "run_1" / "rows.json", [_row(taskFolder="task-a", taskName="task-a")])
    _write_rows(
        tmp_path / "run_2" / "rows.json",
        [_row(taskFolder="task-b", taskName="task-b", runId="run_b", t="2026-06-01T00:00:09Z")],
    )
    files = discover_row_files(tmp_path)
    rows, manifests = aggregate(files, run_id="run_20260601_120000_7", t="2026-06-01T12:00:00Z")

    assert len(rows) == 2
    assert {r["runId"] for r in rows} == {"run_20260601_120000_7"}
    assert {r["t"] for r in rows} == {"2026-06-01T12:00:00Z"}
    assert {r["taskFolder"] for r in rows} == {"task-a", "task-b"}
    assert len(manifests) == 1  # one setup -> one manifest


def test_discover_excludes_given_outputs(tmp_path):
    _write_rows(tmp_path / "run_1" / "rows.json", [_row()])
    out = tmp_path / "rows.json"
    _write_rows(out, [_row()])
    files = discover_row_files(tmp_path, exclude=(out.resolve(),))
    assert out.resolve() not in {f.resolve() for f in files}
    assert (tmp_path / "run_1" / "rows.json").resolve() in {f.resolve() for f in files}
