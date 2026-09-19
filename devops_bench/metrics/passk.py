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

"""Pass@k and Pass^k reliability metrics over result rows.

Single-shot evaluation does not capture the reliability of non-deterministic
agents, so this module estimates, from ``n`` scored attempts of which ``c``
passed:

    pass@k = 1 - C(n - c, k) / C(n, k)   (at least one of k attempts passes)
    pass^k =     C(c, k)     / C(n, k)   (all k attempts pass)

Both are the standard unbiased estimators over the observed attempts; both are
``None`` when ``n < k`` (too few attempts to estimate without extrapolating).
An attempt "passes" when its ``outcomeScore`` reaches :data:`PASSK_THRESHOLD`
— by default a perfect ``1.0`` composite, per the leaderboard's definition of
a pass. ``pass@1`` — the ``k = 1`` case, which collapses to ``c / n`` — is
always reported alongside, so the whole pass family shares one threshold, one
pooling rule and one formula.

:func:`summarize_rows` aggregates the dashboard's ``rows.json`` contract
(camelCase :class:`~devops_bench.results.row.ResultRow` dicts): attempts are
the distinct ``(runId, iteration)`` observations per ``(setupId, taskFolder)``,
and the overall per-setup value is the mean over its tasks — mirroring how the
leaderboard averages per-task scores. Kept pure (no judge/SDK imports), like
:mod:`devops_bench.metrics.scoring`.

CLI: ``python -m devops_bench.metrics.passk RESULTS_ROOT`` scans for
``rows.json`` files (e.g. several runs of the same matrix) and writes a
``passk_summary.json``.
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Iterable
from pathlib import Path

__all__ = [
    "DEFAULT_K",
    "PASSK_THRESHOLD",
    "pass_at_k",
    "pass_pow_k",
    "summarize_rows",
]

#: Minimum ``outcomeScore`` for an attempt to count as a pass. ``1.0`` means a
#: perfect composite (full correctness, all safety checks, no catastrophic
#: action); scores are stored rounded, so a perfect run compares equal exactly.
PASSK_THRESHOLD = 1.0

#: Default ``k``: the benchmark reports pass@5 / pass^5.
DEFAULT_K = 5

#: Score fractions are rounded like ``outcomeScore`` itself (4 decimal places).
_SCORE_DECIMALS = 4


def _validate_counts(n: int, c: int, k: int) -> None:
    """Raise ``ValueError`` unless ``0 <= c <= n`` and ``k >= 1``.

    Args:
        n: Number of scored attempts.
        c: Number of passing attempts.
        k: Number of hypothetical samples.
    """
    if k < 1:
        raise ValueError(f"k must be >= 1, got {k}")
    if n < 0:
        raise ValueError(f"n must be >= 0, got {n}")
    if not 0 <= c <= n:
        raise ValueError(f"c must be in [0, n={n}], got {c}")


def pass_at_k(n: int, c: int, k: int) -> float | None:
    """Estimate the probability that at least one of ``k`` attempts passes.

    Unbiased estimator ``1 - C(n - c, k) / C(n, k)`` over ``n`` observed
    attempts with ``c`` passes.

    Args:
        n: Number of scored attempts.
        c: Number of passing attempts.
        k: Number of hypothetical samples.

    Returns:
        The estimate in ``[0, 1]``, or ``None`` when ``n < k`` (not enough
        attempts to estimate without extrapolating).

    Raises:
        ValueError: If ``c > n`` or any count is out of range.
    """
    _validate_counts(n, c, k)
    if n < k:
        return None
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


def pass_pow_k(n: int, c: int, k: int) -> float | None:
    """Estimate the probability that all ``k`` attempts pass.

    Unbiased estimator ``C(c, k) / C(n, k)`` over ``n`` observed attempts with
    ``c`` passes — the consistency counterpart to :func:`pass_at_k`.

    Args:
        n: Number of scored attempts.
        c: Number of passing attempts.
        k: Number of hypothetical samples.

    Returns:
        The estimate in ``[0, 1]``, or ``None`` when ``n < k``.

    Raises:
        ValueError: If ``c > n`` or any count is out of range.
    """
    _validate_counts(n, c, k)
    if n < k:
        return None
    return math.comb(c, k) / math.comb(n, k)


def _is_finite_number(value: object) -> bool:
    """Return whether ``value`` is a real, finite number (bools excluded)."""
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _dedupe_by_doc_id(rows: Iterable[dict]) -> list[dict]:
    """Drop duplicate ``(setupId, runId, taskFolder, iteration)`` rows, latest wins.

    This is the full Firestore document id — unlike
    :func:`devops_bench.results.aggregate.dedupe_latest`, ``runId`` stays in
    the key because distinct runs ARE the attempts pass@k samples over;
    collapsing them would erase the very repetitions being measured. Only a
    re-uploaded/retried copy of the same attempt is dropped (greatest ``t``
    wins, ISO timestamps sorting lexicographically).

    Args:
        rows: Row dicts in the camelCase ``rows.json`` contract.

    Returns:
        The de-duplicated rows, in first-seen key order.
    """
    chosen: dict[tuple[str, str, str, int], dict] = {}
    order: list[tuple[str, str, str, int]] = []
    for row in rows:
        key = (
            str(row.get("setupId", "")),
            str(row.get("runId", "")),
            str(row.get("taskFolder", "")),
            int(row.get("iteration", 0) or 0),
        )
        prev = chosen.get(key)
        if prev is None:
            order.append(key)
            chosen[key] = row
        elif str(row.get("t", "")) >= str(prev.get("t", "")):
            chosen[key] = row
    return [chosen[key] for key in order]


def _round_or_none(value: float | None) -> float | None:
    """Round a score fraction like ``outcomeScore``, passing ``None`` through."""
    return None if value is None else round(value, _SCORE_DECIMALS)


def _mean_or_none(values: list[float | None]) -> float | None:
    """Mean of the non-``None`` values, or ``None`` when there are none."""
    present = [v for v in values if v is not None]
    return round(sum(present) / len(present), _SCORE_DECIMALS) if present else None


def summarize_rows(
    rows: Iterable[dict],
    *,
    k: int = DEFAULT_K,
    threshold: float = PASSK_THRESHOLD,
) -> dict:
    """Compute per-task and per-setup pass@1 / pass@k / pass^k from result rows.

    Rows are grouped by ``(setupId, taskFolder)``; each distinct
    ``(runId, iteration)`` is one attempt. An attempt with a non-finite or
    missing ``outcomeScore`` is missing data — excluded from both ``n`` and
    ``c`` — mirroring how the leaderboard's pass rates treat unscored
    iterations. Every metric is the same estimator at a different ``k`` under
    the one shared threshold — ``passAt1`` is the ``k = 1`` case, which
    collapses to ``c / n`` — and each reports ``None`` below its own ``k``
    scored attempts. The setup-level value is the mean over the tasks that do
    report one.

    Args:
        rows: Row dicts in the camelCase ``rows.json`` contract, spanning any
            number of runs.
        k: The ``k`` in pass@k / pass^k (``passAt1`` is always reported too).
        threshold: Minimum ``outcomeScore`` counting as a pass.

    Returns:
        A JSON-serializable summary::

            {
              "k": 5, "threshold": 1.0,
              "setups": [
                {
                  "setupId": ..., "model": ..., "harness": ..., "augmentation": [...],
                  "tasks": [{"taskFolder", "taskName", "n", "c",
                             "passAt1", "passAtK", "passPowK"}, ...],
                  "overall": {"passAt1": ..., "passAtK": ..., "passPowK": ...}
                }, ...
              ]
            }
    """
    deduped = _dedupe_by_doc_id(rows)

    # Group attempts per (setupId, taskFolder), preserving first-seen order.
    by_setup: dict[str, dict[str, list[dict]]] = {}
    for row in deduped:
        setup_id = str(row.get("setupId", ""))
        if not setup_id:
            continue
        by_setup.setdefault(setup_id, {}).setdefault(str(row.get("taskFolder", "")), []).append(row)

    setups = []
    for setup_id, by_task in by_setup.items():
        head = next(iter(by_task.values()))[0]
        tasks = []
        for folder, attempts in by_task.items():
            scored = [
                r["outcomeScore"] for r in attempts if _is_finite_number(r.get("outcomeScore"))
            ]
            n = len(scored)
            c = sum(1 for s in scored if s >= threshold)
            tasks.append(
                {
                    "taskFolder": folder,
                    "taskName": attempts[0].get("taskName") or folder,
                    "n": n,
                    "c": c,
                    "passAt1": _round_or_none(pass_at_k(n, c, 1)),
                    "passAtK": _round_or_none(pass_at_k(n, c, k)),
                    "passPowK": _round_or_none(pass_pow_k(n, c, k)),
                }
            )
        setups.append(
            {
                "setupId": setup_id,
                "model": head.get("model"),
                "harness": head.get("harness"),
                "augmentation": list(head.get("augmentation") or []),
                "tasks": tasks,
                "overall": {
                    "passAt1": _mean_or_none([t["passAt1"] for t in tasks]),
                    "passAtK": _mean_or_none([t["passAtK"] for t in tasks]),
                    "passPowK": _mean_or_none([t["passPowK"] for t in tasks]),
                },
            }
        )

    return {"k": k, "threshold": threshold, "setups": setups}


_OUT_FILENAME = "passk_summary.json"


def main(argv: list[str] | None = None) -> int:
    """CLI: summarize pass@k / pass^k over a tree of ``rows.json`` files.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code (0 on success).
    """
    # Imported here so the pure estimators stay importable without the results
    # layer (and its pydantic dependency) on the path.
    from devops_bench.results.aggregate import discover_row_files, load_rows

    parser = argparse.ArgumentParser(
        prog="python -m devops_bench.metrics.passk",
        description="Compute pass@k / pass^k per task and per setup from the "
        "rows.json files under a results root (attempts = distinct runs).",
    )
    parser.add_argument("root", help="Results root scanned recursively for rows.json files.")
    parser.add_argument(
        "-o",
        "--out",
        default=None,
        help=f"Output path for the summary JSON (default: ROOT/{_OUT_FILENAME}).",
    )
    parser.add_argument(
        "--k", type=int, default=DEFAULT_K, help="The k in pass@k (default: %(default)s)."
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=PASSK_THRESHOLD,
        help="Minimum outcomeScore counting as a pass (default: %(default)s).",
    )
    args = parser.parse_args(argv)

    out_path = (Path(args.out) if args.out else Path(args.root) / _OUT_FILENAME).resolve()

    files = discover_row_files(args.root, exclude=(out_path,))
    if not files:
        parser.error(f"no rows.json files found under {args.root}")

    summary = summarize_rows(load_rows(files), k=args.k, threshold=args.threshold)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    n_tasks = sum(len(s["tasks"]) for s in summary["setups"])
    print(
        f"pass@{args.k} summary over {len(files)} file(s): "
        f"{len(summary['setups'])} setup(s), {n_tasks} task group(s)"
    )
    print(f"  {out_path}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
