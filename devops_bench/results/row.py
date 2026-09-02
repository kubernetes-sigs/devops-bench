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
#: v2 adds the scoring-framework v1 fields (``outcomeScore`` becomes the composite
#: score; ``correctnessScore`` / ``recoverableSafetyScore`` / ``catastrophic`` /
#: ``scoringVersion`` are added).
SCHEMA_VERSION = 2

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
        timeout_sec: The per-task wall-clock budget the agent ran under, or
            ``None`` when uncapped. "Timed out" and "used 90% of its budget"
            are both uninterpretable without it, and the budget is a run
            setting that no row can recover after the fact.
    """

    model_config = _MODEL_CONFIG

    schema_version: int
    run_id: str
    t: str
    setup_id: str
    model: str
    harness: str
    augmentation: list[str]
    timeout_sec: float | None = None

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
        served_model: The model that actually answered, when the harness
            reports it; ``""`` when it does not. ``model`` is only what the run
            *asked* for — a request for ``gemini-3-flash`` was served
            ``gemini-3-flash-preview``, and openclaw fails over to a different
            model mid-run — so a score attributed to ``model`` alone can name
            the wrong one. Comma-joined in first-seen order on the rare run
            that was served by more than one, which is itself the signal that
            a failover happened.
        augmentation: Capability tokens; matches :attr:`Manifest.augmentation`.
        run_id: Run directory suffix; matches :attr:`Manifest.run_id`.
        t: UTC ISO-8601 run timestamp; matches :attr:`Manifest.t`.
        task_folder: The task's directory name.
        task_name: The task's human-readable name (the spec ``name:`` field).
        iteration: Zero-based repeat index; always ``0`` until multi-iteration
            runs land.
        outcome_score: Composite scoring-framework score in ``[0, 1]``
            (``cat_v * sqrt(c * rec_v)``), or ``None`` when the run was unscored
            (e.g. a failed task). Continuous — never a precomputed pass flag.
        correctness_score: Correctness sub-score ``c`` in ``[0, 1]``, taken from
            the first available of the deterministic ``VerificationCorrectness``,
            the checklist score, or OutcomeValidity; ``None`` when unscored.
        recoverable_safety_score: The **raw** recoverable pass fraction in
            ``[0, 1]``, from the deterministic signal when present and the judged
            one otherwise. The ``[0.1, 1.0]`` rescale the outcome formula applies
            is deliberately not reflected here, because this layer maps and never
            scores, so this value will not reconcile by hand against
            ``outcome_score``. ``None`` when the task declared no recoverable
            safeguards.
        catastrophic: Whether a catastrophic tripwire fired (``cat_v = 0``); such
            a run has ``outcome_score = 0`` regardless of the other sub-scores.
        scoring_version: Scoring-framework version that produced ``outcome_score``
            (e.g. ``"v1"``); ``""`` for rows written before the framework landed.
        tool_score: Tool-invocation judge score in ``[0, 1]``, or ``None``.
        tool_calls: Number of tool calls in the run's trajectory, or ``None``
            when no trajectory was captured. The unit of agentic work: two
            models with the same score and the same wall clock can differ
            several-fold here, and the trajectory itself is too large to
            aggregate over at dashboard time.
        tool_errors: How many of those calls returned an error. A high count
            against a passing score means the model recovered; against a
            failing one it usually means the environment broke, not the model.
        model_turns: Model round-trips in the run, or ``None`` when the harness
            cannot delimit them. Not ``tool_calls``: one turn can issue several
            tool calls, and a text-only turn issues none.
        latency_sec: Agent wall-clock seconds for the iteration.
        tool_wait_sec: How much of ``latency_sec`` was spent waiting on tool
            calls, with concurrent calls counted once, or ``None`` when the
            harness reported no timings. Without it a run against a cold
            cluster and a genuinely slow model are the same number on a
            leaderboard that ranks latency lower-is-better.
        input_tokens: Non-cached prompt token count, or ``None`` when
            unreported. (Historical records that predate the canonical token
            schema may include cached tokens here.)
        output_tokens: Visible completion token count (excludes reasoning
            where the harness reports it), or ``None`` when unreported.
        cached_tokens: Cache-read token count, or ``None`` when the harness
            reports no cache telemetry.
        reasoning_tokens: Thinking-token count, or ``None`` when unreported
            (some providers bill thinking inside ``output_tokens``).
        cache_write_tokens: Cache-creation token count (billed at a premium by
            providers that report it), or ``None`` when unreported.
        total_tokens: Provider-reported or bucket-sum total, or ``None`` when
            unreported. Semantics vary for pre-canonical records.
        status: Terminal record status, ``"success"`` or ``"failed"``.
        terminal_reason: Why the *agent* stopped — ``"completed"``,
            ``"timeout"``, ``"error"``, or ``""`` when unreported. Distinct
            from ``status``, which describes the record: a run the harness
            killed at its wall-clock budget still reads ``status: "success"``.
        timeout_sec: The wall-clock budget this iteration ran under; matches
            :attr:`Manifest.timeout_sec`. Carried on the row because ingest
            uploads ``rows.json`` alone — the manifest is never read — so a
            run-level setting only reaches the dashboard by riding along.
            ``None`` when uncapped.
        validated: Whether the task is vetted as correct and eligible for the
            leaderboard; ingest gates promotion on this (default ``False``).
    """

    model_config = _MODEL_CONFIG

    setup_id: str
    model: str
    harness: str
    served_model: str = ""
    augmentation: list[str]
    run_id: str
    t: str
    task_folder: str
    task_name: str
    iteration: int
    outcome_score: float | None
    correctness_score: float | None = None
    recoverable_safety_score: float | None = None
    catastrophic: bool = False
    scoring_version: str = ""
    tool_score: float | None
    tool_calls: int | None = None
    tool_errors: int | None = None
    model_turns: int | None = None
    latency_sec: float
    tool_wait_sec: float | None = None
    input_tokens: int | None
    output_tokens: int | None
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None
    cache_write_tokens: int | None = None
    total_tokens: int | None = None
    status: str
    terminal_reason: str = ""
    timeout_sec: float | None = None
    validated: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable mapping written to ``rows.json``.

        Keys use the camelCase names of the dashboard ``ResultRow`` interface so
        the row round-trips into Firestore without a rename step.
        """
        return self.model_dump(by_alias=True)
