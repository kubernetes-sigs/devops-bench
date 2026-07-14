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

"""Combine per-task parallel runs into one batch run for dashboard ingest.

The matrix runs one task per process, emitting a ``rows.json`` per task with its
own ``runId``/``t``; the dashboard models a run as a batch of tasks sharing one
``runId``/``t`` (tasks kept distinct by ``taskFolder``). This rewrites the
per-task rows onto a single batch ``run_id`` + ``t`` and writes a combined
``rows.json`` + per-setup ``manifests.json``. The batch ``run_id`` carries a
unique suffix so repeated/concurrent runs never collide on the
``setupId__runId__taskFolder__iteration`` doc id.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
from collections.abc import Iterable
from pathlib import Path

from devops_bench.results.row import SCHEMA_VERSION, Manifest, ResultRow

__all__ = [
    "aggregate",
    "build_manifests",
    "dedupe_latest",
    "discover_row_files",
    "rebatch_rows",
]

# ``manifests.json`` is plural: the combined file may span several setups.
_ROWS_FILENAME = "rows.json"
_OUT_ROWS_FILENAME = "rows.json"
_OUT_MANIFESTS_FILENAME = "manifests.json"


def discover_row_files(root: str | os.PathLike[str], *, exclude: Iterable[Path] = ()) -> list[Path]:
    """Recursively collect per-task ``rows.json`` files under ``root``.

    Walks ``root`` in sorted order for deterministic aggregation and skips any
    path in ``exclude`` (e.g. a previously written combined output living under
    the same tree).

    Args:
        root: Directory to scan (e.g. the matrix results root).
        exclude: Absolute paths to skip.

    Returns:
        The discovered ``rows.json`` paths, path-sorted.
    """
    root_path = Path(root)
    skip = {Path(p).resolve() for p in exclude}
    out = [p for p in sorted(root_path.rglob(_ROWS_FILENAME)) if p.resolve() not in skip]
    return out


def _load_rows(files: Iterable[Path]) -> list[dict]:
    """Read and concatenate the ``ResultRow[]`` arrays from ``files``.

    Args:
        files: ``rows.json`` paths, each holding a JSON list of row dicts.

    Returns:
        The flattened row dicts, in file order.

    Raises:
        ValueError: If any file's top-level JSON is not a list.
    """
    rows: list[dict] = []
    for file in files:
        parsed = json.loads(Path(file).read_text(encoding="utf-8"))
        if not isinstance(parsed, list):
            raise ValueError(f"{file}: top level must be a ResultRow[] array")
        rows.extend(parsed)
    return rows


def dedupe_latest(rows: Iterable[dict]) -> list[dict]:
    """Drop duplicate ``(setupId, taskFolder, iteration)`` rows, keeping the latest.

    A retried task can appear more than once across per-task run dirs; since those
    three fields plus ``runId`` form the Firestore document id, two rows that
    collide after re-batching would overwrite each other. Resolve it here by
    keeping the row with the greatest original ``t`` (ISO timestamps sort
    lexicographically), so a retry's newer result wins deterministically.

    Args:
        rows: Per-task row dicts (still carrying their original ``t``).

    Returns:
        The de-duplicated rows, in first-seen key order.
    """
    chosen: dict[tuple[str, str, int], dict] = {}
    order: list[tuple[str, str, int]] = []
    for row in rows:
        key = (row.get("setupId", ""), row.get("taskFolder", ""), int(row.get("iteration", 0)))
        prev = chosen.get(key)
        if prev is None:
            order.append(key)
            chosen[key] = row
        elif str(row.get("t", "")) >= str(prev.get("t", "")):
            chosen[key] = row
    return [chosen[key] for key in order]


def rebatch_rows(rows: Iterable[dict], *, run_id: str, t: str) -> list[ResultRow]:
    """Stamp every row with one shared batch ``run_id`` and ``t``.

    Each input dict is validated against :class:`ResultRow` (camelCase aliases),
    so a malformed row fails loudly here rather than silently corrupting the
    leaderboard. The run-level identity is the only thing rewritten.

    Args:
        rows: Per-task row dicts (camelCase keys, as written by the reporter).
        run_id: Batch run id to apply to every row.
        t: Batch UTC ISO-8601 timestamp to apply to every row.

    Returns:
        Re-batched :class:`ResultRow` instances, input order preserved.

    Raises:
        pydantic.ValidationError: If any row does not match the schema.
    """
    return [
        ResultRow.model_validate(row).model_copy(update={"run_id": run_id, "t": t}) for row in rows
    ]


def build_manifests(rows: Iterable[ResultRow], *, run_id: str, t: str) -> list[Manifest]:
    """Build one :class:`Manifest` per distinct setup in ``rows``.

    Setups appear in first-seen order; each manifest takes the arm identity
    (model / harness / augmentation) from that setup's first row and shares the
    batch ``run_id`` / ``t``.

    Args:
        rows: Re-batched rows (all already carrying the batch ``run_id`` / ``t``).
        run_id: Batch run id, mirrored onto every manifest.
        t: Batch timestamp, mirrored onto every manifest.

    Returns:
        One manifest per setup, in first-seen order.
    """
    manifests: dict[str, Manifest] = {}
    for row in rows:
        if row.setup_id in manifests:
            continue
        manifests[row.setup_id] = Manifest(
            schema_version=SCHEMA_VERSION,
            run_id=run_id,
            t=t,
            setup_id=row.setup_id,
            model=row.model,
            harness=row.harness,
            augmentation=list(row.augmentation),
        )
    return list(manifests.values())


def aggregate(files: Iterable[Path], *, run_id: str, t: str) -> tuple[list[dict], list[dict]]:
    """Aggregate per-task ``rows.json`` files into one batch run.

    De-duplicates retried tasks (latest wins), re-batches the rows onto a single
    ``run_id`` / ``t``, and derives the per-setup manifests.

    Args:
        files: Per-task ``rows.json`` paths.
        run_id: Batch run id to stamp on every row/manifest.
        t: Batch UTC ISO-8601 timestamp to stamp on every row/manifest.

    Returns:
        A ``(rows, manifests)`` pair of JSON-serializable dict lists, ready to
        write as ``rows.json`` / ``manifests.json``.
    """
    deduped = dedupe_latest(_load_rows(files))
    rows = rebatch_rows(deduped, run_id=run_id, t=t)
    manifests = build_manifests(rows, run_id=run_id, t=t)
    return [row.to_dict() for row in rows], [m.to_dict() for m in manifests]


def _default_run_id() -> str:
    """Generate a batch run id ``run_YYYYMMDD_HHMMSS_<pid>``.

    The ``run_<ts>`` prefix keeps it human-sortable and PROTOCOL-shaped; the pid
    suffix makes it unique across concurrent matrix aggregations in the same
    second. Pass ``--run-id`` to use the matrix's own id instead.
    """
    now = datetime.datetime.now(datetime.UTC)
    return f"run_{now.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"


def _now_iso() -> str:
    """Return the current time as a UTC ISO-8601 ``...Z`` string."""
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: list[str] | None = None) -> int:
    """CLI: aggregate a matrix results tree into one combined run.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code (0 on success).
    """
    parser = argparse.ArgumentParser(
        prog="python -m devops_bench.results.aggregate",
        description="Combine per-task parallel run rows.json files into one batch "
        "run (shared runId/t) for dashboard ingest.",
    )
    parser.add_argument("root", help="Results root scanned recursively for per-task rows.json.")
    parser.add_argument(
        "-o",
        "--out-dir",
        default=None,
        help="Output directory for the combined rows.json/manifests.json (default: ROOT).",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Batch run id to stamp on every row (default: run_<ts>_<pid>).",
    )
    parser.add_argument(
        "--t",
        default=None,
        help="Batch UTC ISO-8601 timestamp to stamp on every row (default: now).",
    )
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir) if args.out_dir else Path(args.root)
    out_rows = (out_dir / _OUT_ROWS_FILENAME).resolve()
    out_manifests = (out_dir / _OUT_MANIFESTS_FILENAME).resolve()

    run_id = args.run_id or _default_run_id()
    t = args.t or _now_iso()

    # Exclude the outputs so re-running over the same tree is idempotent.
    files = discover_row_files(args.root, exclude=(out_rows, out_manifests))
    if not files:
        parser.error(f"no {_ROWS_FILENAME} files found under {args.root}")

    rows, manifests = aggregate(files, run_id=run_id, t=t)

    out_dir.mkdir(parents=True, exist_ok=True)
    out_rows.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    out_manifests.write_text(json.dumps(manifests, indent=2) + "\n", encoding="utf-8")

    print(
        f"aggregated {len(files)} file(s) -> {len(rows)} rows across "
        f"{len(manifests)} setup(s) | run_id={run_id} t={t}"
    )
    print(f"  {out_rows}")
    print(f"  {out_manifests}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
