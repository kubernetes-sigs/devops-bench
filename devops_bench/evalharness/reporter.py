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

"""Result sink: write the per-run ``results.json`` and collect artifacts.

The harness depends on a :class:`ResultReporter` rather than calling
:func:`json.dump` inline, so output sinks (DB, dashboard, blob store) can be
added by substituting the reporter without editing the orchestrator.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from pathlib import Path
from typing import Any

from devops_bench.core import get_logger

__all__ = ["ResultReporter"]

_log = get_logger("evalharness.reporter")

_RESULTS_FILENAME = "results.json"
_ROWS_FILENAME = "rows.json"
_MANIFEST_FILENAME = "manifest.json"

# Characters not safe in a run-directory name are replaced with a dash.
_SANITIZE_RUN_ID_RE = re.compile(r"[^A-Za-z0-9_-]")


class ResultReporter:
    """Default reporter writing ``results.json`` to a timestamped run directory.

    Every value written through this reporter has already been routed through
    ``MetricScore.to_entry()`` / ``AgentResult.to_dict()`` /
    ``ChaosResult.model_dump()`` / ``VerificationResult.model_dump()`` by the
    orchestrator, so the reporter itself only knows how to JSON-encode the
    final dicts.

    Args:
        results_root: Root directory under which per-run subdirectories are
            created. Defaults to ``"results"``.
        run_dir_prefix: Prefix for the timestamped subdirectory name.
        run_id: Optional run identifier appended to the run-dir name. The
            timestamp alone has 1-second resolution, so two processes started
            in the same second would otherwise collide on one directory;
            appending the (process-unique) run id keeps concurrent runs apart.

    Attributes:
        last_run_dir: The most recent directory returned by
            :meth:`new_run_dir`, or ``None`` if none has been created yet.
    """

    def __init__(
        self,
        results_root: str | os.PathLike[str] = "results",
        *,
        run_dir_prefix: str = "run_",
        run_id: str | None = None,
    ) -> None:
        self.results_root = Path(results_root)
        self.run_dir_prefix = run_dir_prefix
        self.run_id = run_id
        self.last_run_dir: Path | None = None

    def new_run_dir(self) -> Path:
        """Create and return a fresh timestamped run directory under the root.

        The name carries a UTC timestamp. A supplied ``run_id`` is appended
        (filesystem-sanitized); without one, sub-second precision is added so two
        runs started in the same second still get distinct directories.

        Returns:
            The absolute path of the newly created directory.
        """
        now = datetime.datetime.now(datetime.UTC)
        name = f"{self.run_dir_prefix}{now.strftime('%Y%m%d_%H%M%S')}"
        if self.run_id:
            name = f"{name}_{_SANITIZE_RUN_ID_RE.sub('-', self.run_id)}"
        else:
            name = f"{name}_{now.strftime('%f')}"
        run_dir = (self.results_root / name).resolve()
        run_dir.mkdir(parents=True, exist_ok=False)
        self.last_run_dir = run_dir
        return run_dir

    def write(self, run_dir: str | os.PathLike[str], results: list[dict[str, Any]]) -> Path:
        """Write ``results`` to ``<run_dir>/results.json`` and return the path.

        Args:
            run_dir: Run directory previously returned by :meth:`new_run_dir`.
            results: Per-task result dicts already shaped to the on-disk schema.

        Returns:
            The path of the JSON file written.
        """
        path = Path(run_dir) / _RESULTS_FILENAME
        with open(path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)
        _log.info("wrote results.json with %d entries to %s", len(results), path)
        return path

    def write_rows(self, run_dir: str | os.PathLike[str], rows: list[dict[str, Any]]) -> Path:
        """Write flattened ingest rows to ``<run_dir>/rows.json`` and return the path.

        ``rows`` is the dashboard-facing ``ResultRow`` contract (one entry per
        ``(setup × task × run × iteration)``), already converted to dicts via
        ``ResultRow.to_dict()``.

        Args:
            run_dir: Run directory previously returned by :meth:`new_run_dir`.
            rows: Flattened result rows already shaped to the on-disk schema.

        Returns:
            The path of the JSON file written.
        """
        path = Path(run_dir) / _ROWS_FILENAME
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2)
        _log.info("wrote rows.json with %d rows to %s", len(rows), path)
        return path

    def write_manifest(self, run_dir: str | os.PathLike[str], manifest: dict[str, Any]) -> Path:
        """Write the run-level manifest to ``<run_dir>/manifest.json``.

        Args:
            run_dir: Run directory previously returned by :meth:`new_run_dir`.
            manifest: Run-level identity already converted via
                ``Manifest.to_dict()``.

        Returns:
            The path of the JSON file written.
        """
        path = Path(run_dir) / _MANIFEST_FILENAME
        with open(path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
        _log.info("wrote manifest.json to %s", path)
        return path
