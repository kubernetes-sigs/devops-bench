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

"""Pass@k and pass^k over result rows.

From ``n`` scored attempts with ``c`` passes (unbiased estimators):

    pass@k = 1 - C(n - c, k) / C(n, k)   (at least one of k passes)
    pass^k =     C(c, k)     / C(n, k)   (all k pass)

Both are ``None`` when ``n < k``. A pass is ``outcomeScore >= PASSK_THRESHOLD``.
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

#: Minimum ``outcomeScore`` counted as a pass.
PASSK_THRESHOLD = 1.0

DEFAULT_K = 5

#: How many decimal places each score is rounded to.
_SCORE_DECIMALS = 4


def _validate_counts(n: int, c: int, k: int) -> None:
    """Raise ``ValueError`` unless ``0 <= c <= n`` and ``k >= 1``."""
    if k < 1:
        raise ValueError(f"k must be >= 1, got {k}")
    if n < 0:
        raise ValueError(f"n must be >= 0, got {n}")
    if not 0 <= c <= n:
        raise ValueError(f"c must be in [0, n={n}], got {c}")


def pass_at_k(n: int, c: int, k: int) -> float | None:
    """Probability that at least one of ``k`` attempts passes, or ``None`` if ``n < k``."""
    _validate_counts(n, c, k)
    if n < k:
        return None
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


def pass_pow_k(n: int, c: int, k: int) -> float | None:
    """Probability that all ``k`` attempts pass, or ``None`` if ``n < k``."""
    _validate_counts(n, c, k)
    if n < k:
        return None
    return math.comb(c, k) / math.comb(n, k)


def _is_finite_number(value: object) -> bool:
    """Return whether ``value`` is a real, finite number (bools excluded)."""
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _dedupe_by_doc_id(rows: Iterable[dict]) -> list[dict]:
    """Drop duplicate ``(setupId, runId, taskFolder, iteration)`` rows, latest ``t`` wins.

    Unlike ``aggregate.dedupe_latest``, ``runId`` stays in the key: distinct runs
    are the attempts being sampled.
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
    """Round ``value``, passing ``None`` through."""
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
    """Compute per-task and per-setup pass@1 / pass@k / pass^k.

    Attempts are distinct ``(runId, iteration)`` rows per ``(setupId, taskFolder)``;
    unscored attempts are excluded. Setup values are the mean over tasks.

    Args:
        rows: ``rows.json`` row dicts, spanning any number of runs.
        k: The ``k`` in pass@k / pass^k.
        threshold: Minimum ``outcomeScore`` counted as a pass.

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


def _drop_combined(files: list[Path]) -> list[Path]:
    """Drop aggregate outputs (``manifests.json`` sibling) if any per-task rows are present."""
    per_task = [f for f in files if not (f.parent / "manifests.json").exists()]
    return per_task or files


def main(argv: list[str] | None = None) -> int:
    """CLI: write ``passk_summary.json`` for the ``rows.json`` files under a root."""
    # Lazy: keeps the estimators importable without pydantic.
    from devops_bench.results.aggregate import discover_row_files, load_rows

    parser = argparse.ArgumentParser(
        prog="python -m devops_bench.metrics.passk",
        description="Compute pass@k / pass^k from the rows.json files under a root.",
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

    files = _drop_combined(discover_row_files(args.root, exclude=(out_path,)))
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
