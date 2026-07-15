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

"""Flattened, ingest-ready result rows bridging the harness and the dashboard.

The harness writes a nested, metric-keyed ``results.json`` per run. The
leaderboard instead consumes one flat row per ``(setup × task × run ×
iteration)`` carrying continuous scores. :class:`ResultRow` is that flat row and
:class:`Manifest` is the run-level identity shared by every row in a run; the
normalizer in :mod:`devops_bench.results.normalize` turns harness records into
``ResultRow`` instances. This module owns the on-disk shape only — it performs
no scoring and reads no environment.

Both models are frozen and serialize through a camelCase alias generator, so
``to_dict()`` emits exactly the field names of the dashboard's ``ResultRow`` /
manifest interfaces while the Python attributes stay snake_case.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

__all__ = ["SCHEMA_VERSION", "Manifest", "ResultRow"]

#: Version of the ``rows.json`` / ``manifest.json`` contract. Bump on any
#: breaking field change so a downstream ingest can detect a shape mismatch.
SCHEMA_VERSION = 1

# Frozen + camelCase aliases. ``populate_by_name`` keeps the snake_case
# attribute names usable as constructor kwargs (the normalizer builds rows that
# way), while ``to_dict`` dumps the camelCase aliases the dashboard expects.
_MODEL_CONFIG = ConfigDict(frozen=True, alias_generator=to_camel, populate_by_name=True)


class Manifest(BaseModel):
    """Run-level identity shared by every :class:`ResultRow` in a run.

    Attributes:
        schema_version: Contract version; mirrors :data:`SCHEMA_VERSION`.
        run_id: Run directory suffix, ``run_YYYYMMDD_HHMMSS``.
        t: Run timestamp as a UTC ISO-8601 string (e.g. ``2026-06-01T00:00:00Z``).
        setup_id: Deterministic id for the ``(model, harness, augmentation)``
            arm; the join key the dashboard groups rows by.
        model: Model identifier under test (e.g. ``AGENT_MODEL``).
        harness: Canonical agent/harness key (e.g. ``gemini`` / ``openclaw`` /
            ``api``).
        augmentation: Capability tokens active for the run (e.g.
            ``["mcp", "skills"]``); an empty list denotes the baseline arm.
    """

    model_config = _MODEL_CONFIG

    schema_version: int
    run_id: str
    t: str
    setup_id: str
    model: str
    harness: str
    augmentation: list[str]

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable mapping written to ``manifest.json``."""
        return self.model_dump(by_alias=True)


class ResultRow(BaseModel):
    """One flattened iteration row, the producer-side leaderboard contract.

    Mirrors the ``ResultRow`` TypeScript interface the dashboard reads field for
    field (the contract version lives once on the run :class:`Manifest`, not on
    every row). Scores and token counts are nullable because a failed or unscored
    iteration carries no judged value; ``outcome_score`` stays continuous (never
    a precomputed pass flag) so any pass threshold / pass@k formula remains
    computable downstream.

    Attributes:
        setup_id: Run arm id; matches :attr:`Manifest.setup_id`.
        model: Model identifier; matches :attr:`Manifest.model`.
        harness: Canonical harness key; matches :attr:`Manifest.harness`.
        augmentation: Capability tokens; matches :attr:`Manifest.augmentation`.
        run_id: Run directory suffix; matches :attr:`Manifest.run_id`.
        t: UTC ISO-8601 run timestamp; matches :attr:`Manifest.t`.
        task_folder: The task's directory name.
        task_name: The task's human-readable name (the spec ``name:`` field).
        iteration: Zero-based repeat index; always ``0`` until multi-iteration
            runs land.
        outcome_score: Outcome-validity judge score in ``[0, 1]``, or ``None``
            when the metric did not run (e.g. a failed task).
        tool_score: Tool-invocation judge score in ``[0, 1]``, or ``None``.
        latency_sec: Agent wall-clock seconds for the iteration.
        input_tokens: Prompt token count, or ``None`` when unreported.
        output_tokens: Completion token count, or ``None`` when unreported.
        status: Terminal record status, ``"success"`` or ``"failed"``.
        validated: Whether the task is vetted as correct and eligible for the
            leaderboard; ingest gates promotion on this (default ``False``).
    """

    model_config = _MODEL_CONFIG

    setup_id: str
    model: str
    harness: str
    augmentation: list[str]
    run_id: str
    t: str
    task_folder: str
    task_name: str
    iteration: int
    outcome_score: float | None
    tool_score: float | None
    latency_sec: float
    input_tokens: int | None
    output_tokens: int | None
    status: str
    validated: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable mapping written to ``rows.json``.

        Keys use the camelCase names of the dashboard ``ResultRow`` interface so
        the row round-trips into Firestore without a rename step.
        """
        return self.model_dump(by_alias=True)
