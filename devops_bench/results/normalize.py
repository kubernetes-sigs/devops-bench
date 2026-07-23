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

"""Flatten nested harness records into :class:`ResultRow` rows.

Pure functions only: this module reads no environment and performs no scoring.
It maps the harness's metric-keyed records and a run-level :class:`Manifest`
onto the flat dashboard contract, normalizing the per-provider token shapes and
the per-metric score shapes along the way.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any, NamedTuple

from devops_bench.results.row import Manifest, ResultRow

__all__ = [
    "OUTCOME_SCORE_KEY",
    "TOOL_SCORE_KEY",
    "NormalizedTokens",
    "build_rows",
    "derive_augmentation",
    "extract_score",
    "normalize_tokens",
    "setup_id",
    "slugify",
]

#: ``res["scores"]`` keys the flat ``outcomeScore`` / ``toolScore`` are read
#: from. These match the ``MetricScore.name`` of the builtin outcome and tool
#: metrics.
OUTCOME_SCORE_KEY = "OutcomeValidity"
TOOL_SCORE_KEY = "ToolInvocation"

# Token usage aliases per provider, in lookup priority. The canonical keys
# (``input`` / ``cached`` / ``reasoning`` / ``output``; see
# ``devops_bench.agents.result.TOKEN_BUCKETS``) come first; the rest keep
# historical ``results.json`` records readable.
_INPUT_TOKEN_KEYS = ("input", "prompt_tokens", "prompt_token_count", "input_tokens")
_OUTPUT_TOKEN_KEYS = (
    "output",
    "candidates_tokens",
    "candidates_token_count",
    "completion_tokens",
    "output_tokens",
)
_CACHED_TOKEN_KEYS = ("cached", "cache_read_input_tokens", "cached_content_token_count")
_CACHE_WRITE_TOKEN_KEYS = ("cache_write", "cache_creation_input_tokens")
_REASONING_TOKEN_KEYS = ("reasoning", "thoughts_token_count", "reasoning_tokens")
_TOTAL_TOKEN_KEYS = ("total", "total_tokens", "total_token_count")

# Runs of characters outside ``[a-z0-9]`` collapse to a single ``-``. Mirrors the
# dashboard's ``catalog.mjs`` / seeder ``slugify`` so the model component of a
# setup id matches the model catalog doc key (``gemini-3.1-pro`` ->
# ``gemini-3-1-pro``, not ``gemini-31-pro``).
_DISALLOWED_ID_CHARS = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Reduce ``text`` to a document-key-safe slug.

    Lower-cases ``text``, collapses each run of characters outside ``[a-z0-9]``
    to a single ``-``, and strips leading/trailing dashes. This is byte-for-byte
    the dashboard's ``catalog.mjs`` / seeder ``slugify`` algorithm, so the same
    arm yields the same id across the producer, the seeder, and the catalog join
    (e.g. ``gemini-3.1-pro`` -> ``gemini-3-1-pro``).

    Args:
        text: Arbitrary identifier text.

    Returns:
        The sanitized slug (possibly empty).
    """
    return _DISALLOWED_ID_CHARS.sub("-", text.lower()).strip("-")


def setup_id(model: str, harness: str, augmentation: Iterable[str]) -> str:
    """Build the deterministic setup id for a ``(model, harness, augmentation)`` arm.

    The augmentation tokens are sorted so the id is independent of token order;
    the baseline arm (no tokens) yields ``"<model>-<harness>"`` with no trailing
    dash.

    Args:
        model: Model identifier.
        harness: Canonical harness key.
        augmentation: Capability tokens for the arm.

    Returns:
        The sanitized setup id.
    """
    parts = [model, harness]
    tokens = sorted(augmentation)
    if tokens:
        parts.append("-".join(tokens))
    return slugify("-".join(parts))


def derive_augmentation(capabilities_granted: Mapping[str, Any] | None) -> list[str]:
    """Map a record's ``capabilities_granted`` to sorted augmentation tokens.

    ``use_mcp`` contributes ``"mcp"``; a non-empty ``skills`` list contributes
    ``"skills"``. An arm with neither yields ``[]`` (baseline).

    Args:
        capabilities_granted: The record's ``capabilities_granted`` mapping
            (``{"use_mcp": bool, "skills": list}``), or ``None``.

    Returns:
        Sorted, de-duplicated capability tokens.
    """
    caps = capabilities_granted or {}
    tokens: set[str] = set()
    if caps.get("use_mcp"):
        tokens.add("mcp")
    if caps.get("skills"):
        tokens.add("skills")
    return sorted(tokens)


def _coerce_int(value: Any) -> int | None:
    """Return ``value`` as an int, or ``None`` if it is missing/non-numeric."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _first_token(tokens: Mapping[str, Any], keys: tuple[str, ...]) -> int | None:
    """Return the first integer-coercible value among ``keys`` in ``tokens``."""
    for key in keys:
        if key in tokens:
            coerced = _coerce_int(tokens[key])
            if coerced is not None:
                return coerced
    return None


class NormalizedTokens(NamedTuple):
    """Per-bucket token counts flattened from a provider ``tokens`` dict.

    Each field is an ``int`` count or ``None`` when the bucket was unreported
    (distinct from a genuine ``0``). Being a :class:`~typing.NamedTuple`, it
    still unpacks and compares as a plain 6-tuple, so existing positional
    callers keep working.
    """

    input: int | None
    output: int | None
    cached: int | None
    reasoning: int | None
    cache_write: int | None
    total: int | None


def normalize_tokens(tokens: Mapping[str, Any] | None) -> NormalizedTokens:
    """Flatten a token dict to per-bucket counts.

    Reads the first present alias for each bucket across the canonical shape
    and the known historical provider shapes; an unreported bucket yields
    ``None`` rather than ``0`` so the dashboard can distinguish "no data" from
    a genuine zero. ``total`` semantics vary for pre-canonical records (some
    eras exclude cached/reasoning); canonical records always report the full
    footprint.

    Args:
        tokens: The record's ``tokens`` mapping, or ``None``.

    Returns:
        A :class:`NormalizedTokens` of ``(input, output, cached, reasoning,
        cache_write, total)`` counts, each ``int`` or ``None``.
    """
    usage = tokens or {}
    return NormalizedTokens(
        input=_first_token(usage, _INPUT_TOKEN_KEYS),
        output=_first_token(usage, _OUTPUT_TOKEN_KEYS),
        cached=_first_token(usage, _CACHED_TOKEN_KEYS),
        reasoning=_first_token(usage, _REASONING_TOKEN_KEYS),
        cache_write=_first_token(usage, _CACHE_WRITE_TOKEN_KEYS),
        total=_first_token(usage, _TOTAL_TOKEN_KEYS),
    )


def extract_score(scores: Mapping[str, Any] | None, key: str) -> float | None:
    """Pull a single continuous metric score out of a record's ``scores`` map.

    Handles both score shapes produced by ``MetricScore.to_entry``: a bare
    number, or a ``{"score": ..., "success": ..., "reason": ...}`` dict.

    Args:
        scores: The record's ``scores`` mapping, or ``None``.
        key: Metric name to read (e.g. :data:`OUTCOME_SCORE_KEY`).

    Returns:
        The score as a float, or ``None`` when absent or non-numeric.
    """
    value: Any = (scores or {}).get(key)
    if isinstance(value, Mapping):
        value = value.get("score")
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def build_rows(records: Iterable[Mapping[str, Any]], manifest: Manifest) -> list[ResultRow]:
    """Flatten harness result records into :class:`ResultRow` rows for one run.

    Run-level identity comes from ``manifest``; per-task fields are read from
    each record. Every record yields exactly one row at ``iteration = 0``
    (single run per task today); failed records pass through with ``None`` scores
    so the failure stays visible downstream.

    Args:
        records: The harness's per-task result dicts (the ``results.json`` list).
        manifest: Run-level identity shared by every emitted row.

    Returns:
        One :class:`ResultRow` per record, in input order.
    """
    rows: list[ResultRow] = []
    for record in records:
        scores = record.get("scores")
        tokens = normalize_tokens(record.get("tokens"))
        rows.append(
            ResultRow(
                setup_id=manifest.setup_id,
                model=manifest.model,
                harness=manifest.harness,
                augmentation=list(manifest.augmentation),
                run_id=manifest.run_id,
                t=manifest.t,
                task_folder=record.get("folder", "") or "",
                task_name=record.get("name", "") or "",
                iteration=0,
                outcome_score=extract_score(scores, OUTCOME_SCORE_KEY),
                tool_score=extract_score(scores, TOOL_SCORE_KEY),
                latency_sec=float(record.get("latency") or 0.0),
                input_tokens=tokens.input,
                output_tokens=tokens.output,
                cached_tokens=tokens.cached,
                reasoning_tokens=tokens.reasoning,
                cache_write_tokens=tokens.cache_write,
                total_tokens=tokens.total,
                status=record.get("status", "") or "",
                validated=bool(record.get("validated", False)),
            )
        )
    return rows
