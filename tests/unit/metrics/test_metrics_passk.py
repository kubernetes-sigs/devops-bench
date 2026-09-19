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

"""Tests for the pass@k / pass^k estimators and row summarizer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from devops_bench.metrics.passk import (
    DEFAULT_K,
    PASSK_THRESHOLD,
    main,
    pass_at_k,
    pass_pow_k,
    summarize_rows,
)

# --- estimators — mathematical correctness ------------------------------------


@pytest.mark.parametrize(
    ("n", "c", "k", "expected"),
    [
        (5, 0, 5, 0.0),  # no passes -> no k-subset can contain one
        (5, 5, 5, 1.0),  # all pass -> every subset passes
        (5, 1, 5, 1.0),  # fewer than k failures -> the k-subset must hit a pass
        (10, 3, 5, 1.0 - 21 / 252),  # 1 - C(7,5)/C(10,5)
        (10, 1, 1, 0.1),  # pass@1 degenerates to the plain pass rate c/n
        (6, 2, 5, 1.0),  # n - c = 4 < 5
    ],
)
def test_pass_at_k_known_values(n: int, c: int, k: int, expected: float) -> None:
    assert pass_at_k(n, c, k) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("n", "c", "k", "expected"),
    [
        (5, 5, 5, 1.0),  # perfectly consistent
        (5, 4, 5, 0.0),  # one failure among n=5 -> the only 5-subset fails
        (5, 0, 5, 0.0),
        (10, 5, 5, 1 / 252),  # C(5,5)/C(10,5)
        (10, 10, 5, 1.0),
        (10, 1, 1, 0.1),  # pass^1 also degenerates to c/n
    ],
)
def test_pass_pow_k_known_values(n: int, c: int, k: int, expected: float) -> None:
    assert pass_pow_k(n, c, k) == pytest.approx(expected)


def test_estimators_complement_each_other_at_k_1() -> None:
    # With k=1, "at least one passes" and "all pass" are the same event.
    assert pass_at_k(8, 3, 1) == pytest.approx(pass_pow_k(8, 3, 1))


def test_too_few_attempts_yield_none_not_extrapolation() -> None:
    # n < k: refusing to estimate beats claiming pass@5 = 1.0 from one lucky run.
    assert pass_at_k(1, 1, 5) is None
    assert pass_pow_k(4, 4, 5) is None
    assert pass_at_k(0, 0, 5) is None


@pytest.mark.parametrize(("n", "c", "k"), [(5, 6, 5), (-1, 0, 5), (5, -1, 5), (5, 3, 0)])
def test_estimators_reject_invalid_counts(n: int, c: int, k: int) -> None:
    with pytest.raises(ValueError):
        pass_at_k(n, c, k)
    with pytest.raises(ValueError):
        pass_pow_k(n, c, k)


# --- summarize_rows — grouping, thresholding, missing data --------------------


def _row(
    *,
    setup_id: str = "modelx-harnessy",
    run_id: str = "run_20260901_000000",
    task_folder: str = "task-a",
    iteration: int = 0,
    outcome_score: float | None = 1.0,
    t: str = "2026-09-01T00:00:00Z",
) -> dict:
    """One rows.json-contract dict with the identity fields the summarizer reads."""
    return {
        "setupId": setup_id,
        "model": "model-x",
        "harness": "harness-y",
        "augmentation": ["mcp"],
        "runId": run_id,
        "t": t,
        "taskFolder": task_folder,
        "taskName": task_folder.title(),
        "iteration": iteration,
        "outcomeScore": outcome_score,
    }


def _five_runs(task_folder: str, scores: list[float | None]) -> list[dict]:
    """One row per run for ``task_folder``, one score per run."""
    return [
        _row(
            run_id=f"run_2026090{i}_000000",
            t=f"2026-09-0{i + 1}T00:00:00Z",
            task_folder=task_folder,
            outcome_score=score,
        )
        for i, score in enumerate(scores)
    ]


def test_summarize_groups_attempts_across_runs() -> None:
    # 5 runs of one task: 3 perfect, 2 not -> n=5, c=3.
    rows = _five_runs("task-a", [1.0, 1.0, 1.0, 0.9, 0.0])
    summary = summarize_rows(rows, k=5)

    assert summary["k"] == 5
    assert summary["threshold"] == PASSK_THRESHOLD
    (setup,) = summary["setups"]
    assert setup["setupId"] == "modelx-harnessy"
    (task,) = setup["tasks"]
    assert (task["n"], task["c"]) == (5, 3)
    assert task["passAt1"] == pytest.approx(0.6)  # the k=1 case: c/n
    assert task["passAtK"] == pytest.approx(1.0)  # n - c = 2 < 5
    assert task["passPowK"] == pytest.approx(0.0)  # not all 5 passed
    assert setup["overall"] == {"passAt1": 0.6, "passAtK": 1.0, "passPowK": 0.0}


def test_pass_requires_a_perfect_outcome_score() -> None:
    # 0.9999 is a near-miss, not a pass: the threshold is outcomeScore >= 1.0.
    rows = _five_runs("task-a", [1.0, 1.0, 1.0, 1.0, 0.9999])
    (setup,) = summarize_rows(rows, k=5)["setups"]
    assert setup["tasks"][0]["c"] == 4


def test_unscored_attempts_are_missing_data_not_failures() -> None:
    # A null outcomeScore drops out of n entirely, leaving n=4 < k=5 -> None
    # for the k=5 pair, while passAt1 (k=1) still reports from the 4 attempts.
    rows = _five_runs("task-a", [1.0, 1.0, 1.0, 1.0, None])
    (setup,) = summarize_rows(rows, k=5)["setups"]
    (task,) = setup["tasks"]
    assert (task["n"], task["c"]) == (4, 4)
    assert task["passAt1"] == pytest.approx(1.0)
    assert task["passAtK"] is None
    assert task["passPowK"] is None
    assert setup["overall"] == {"passAt1": 1.0, "passAtK": None, "passPowK": None}


def test_overall_is_the_mean_over_reporting_tasks() -> None:
    # task-a: all 5 pass; task-b: none pass; task-c: only 1 attempt -> excluded.
    rows = (
        _five_runs("task-a", [1.0] * 5)
        + _five_runs("task-b", [0.5] * 5)
        + [_row(task_folder="task-c", outcome_score=1.0)]
    )
    (setup,) = summarize_rows(rows, k=5)["setups"]
    assert setup["overall"]["passAtK"] == pytest.approx(0.5)
    assert setup["overall"]["passPowK"] == pytest.approx(0.5)


def test_iterations_within_a_run_count_as_attempts() -> None:
    # Once multi-iteration runs land, (runId, iteration) pairs are the samples.
    rows = [_row(iteration=i, outcome_score=1.0 if i < 2 else 0.4) for i in range(5)]
    (setup,) = summarize_rows(rows, k=5)["setups"]
    assert (setup["tasks"][0]["n"], setup["tasks"][0]["c"]) == (5, 2)


def test_retried_attempt_dedupes_to_latest_not_extra_sample() -> None:
    # Same (setupId, runId, taskFolder, iteration) re-uploaded with a newer t:
    # one attempt, and the newer score wins.
    rows = _five_runs("task-a", [1.0] * 5)
    retry = dict(rows[0], t="2026-09-09T00:00:00Z", outcomeScore=0.0)
    (setup,) = summarize_rows([*rows, retry], k=5)["setups"]
    (task,) = setup["tasks"]
    assert (task["n"], task["c"]) == (5, 4)


def test_distinct_runs_are_not_collapsed() -> None:
    # Different runIds share (setupId, taskFolder, iteration=0) — they must all
    # survive dedupe, because they ARE the repeated attempts.
    rows = _five_runs("task-a", [1.0] * 5)
    (setup,) = summarize_rows(rows, k=5)["setups"]
    assert setup["tasks"][0]["n"] == 5


def test_setups_are_summarized_independently() -> None:
    rows = _five_runs("task-a", [1.0] * 5) + [
        dict(r, setupId="other-setup") for r in _five_runs("task-a", [0.0] * 5)
    ]
    summary = summarize_rows(rows, k=5)
    by_id = {s["setupId"]: s for s in summary["setups"]}
    assert by_id["modelx-harnessy"]["overall"]["passAtK"] == pytest.approx(1.0)
    assert by_id["other-setup"]["overall"]["passAtK"] == pytest.approx(0.0)


def test_custom_k_and_threshold_are_respected() -> None:
    rows = _five_runs("task-a", [0.8, 0.8, 0.2, 0.2, 0.2])
    (setup,) = summarize_rows(rows, k=2, threshold=0.7)["setups"]
    (task,) = setup["tasks"]
    assert task["c"] == 2
    assert task["passAtK"] == pytest.approx(1.0 - 3 / 10)  # 1 - C(3,2)/C(5,2)
    assert task["passPowK"] == pytest.approx(1 / 10)  # C(2,2)/C(5,2)


# --- CLI ----------------------------------------------------------------------


def test_cli_writes_summary_json(tmp_path: Path) -> None:
    root = tmp_path / "results"
    for i in range(DEFAULT_K):
        run_dir = root / f"run_2026090{i}"
        run_dir.mkdir(parents=True)
        rows = [
            _row(
                run_id=f"run_2026090{i}_000000",
                t=f"2026-09-0{i + 1}T00:00:00Z",
                outcome_score=1.0 if i < 3 else 0.5,
            )
        ]
        (run_dir / "rows.json").write_text(json.dumps(rows), encoding="utf-8")

    assert main([str(root)]) == 0

    summary = json.loads((root / "passk_summary.json").read_text(encoding="utf-8"))
    (setup,) = summary["setups"]
    (task,) = setup["tasks"]
    assert (task["n"], task["c"]) == (5, 3)
    assert task["passAtK"] == pytest.approx(1.0)
    assert task["passPowK"] == pytest.approx(0.0)


def test_cli_output_is_excluded_from_rescans(tmp_path: Path) -> None:
    # Re-running over the same root must not ingest its own passk_summary.json;
    # rows.json discovery also must not pick the summary up by name.
    root = tmp_path / "results"
    run_dir = root / "run_20260901"
    run_dir.mkdir(parents=True)
    (run_dir / "rows.json").write_text(
        json.dumps(_five_runs("task-a", [1.0] * 5)), encoding="utf-8"
    )

    assert main([str(root)]) == 0
    first = (root / "passk_summary.json").read_text(encoding="utf-8")
    assert main([str(root)]) == 0
    assert (root / "passk_summary.json").read_text(encoding="utf-8") == first
