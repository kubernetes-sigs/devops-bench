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

"""Trajectory and token parsers for the Hermes ``state.db`` SQLite database."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from urllib.parse import quote

from devops_bench.agents.result import ToolCall, empty_tokens

__all__: list[str] = ["extract_tokens_from_db", "extract_trajectory_from_db"]

# Hermes ``sessions`` columns -> the canonical buckets in
# :data:`~devops_bench.agents.result.TOKEN_BUCKETS`. Hermes normalizes every
# provider's usage into its own ``CanonicalUsage`` before persisting, so
# ``input_tokens`` already excludes cache reads and writes.
_SESSION_TOKEN_COLUMNS: dict[str, str] = {
    "input_tokens": "input",
    "cache_read_tokens": "cached",
    "cache_write_tokens": "cache_write",
    "reasoning_tokens": "reasoning",
    "output_tokens": "output",
}


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    """Open ``db_path`` read-only, so a live ``state.db`` is never locked."""
    # quote() keeps a ``?`` or ``#`` in the path from being read as the URI's
    # query or fragment delimiter.
    return sqlite3.connect(f"file:{quote(str(db_path))}?mode=ro", uri=True)


def extract_tokens_from_db(db_path: Path) -> dict[str, int | None]:
    """Read canonical token usage from a Hermes ``state.db``.

    The ``sessions`` table carries per-session token counts (``input_tokens`` /
    ``output_tokens`` / ``reasoning_tokens`` / ``cache_read_tokens`` /
    ``cache_write_tokens``), summed across all sessions — the DB is run-scoped,
    and a run may write more than one session row. Never raises: an older
    Hermes schema (missing columns), a missing/corrupt DB, or any read failure
    yields all-``None`` buckets rather than a fabricated ``0``.

    Hermes stores ``output_tokens`` as the provider's full completion count with
    ``reasoning_tokens`` as a subset of it, whereas the canonical ``output``
    bucket excludes reasoning; reasoning is subtracted out here so ``total``
    counts it once and still matches Hermes's own prompt-plus-completion figure.

    Args:
        db_path: Path to the run's ``state.db``.

    Returns:
        The canonical token dict; ``total`` is the sum of the reported buckets,
        or ``None`` when nothing was reported.
    """
    tokens = empty_tokens()
    if not db_path.exists():
        return tokens
    select = ", ".join(f"SUM({column})" for column in _SESSION_TOKEN_COLUMNS)
    try:
        conn = _connect_ro(db_path)
        try:
            row = conn.execute(f"SELECT {select} FROM sessions").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return tokens
    if row is None:
        return tokens
    for bucket, value in zip(_SESSION_TOKEN_COLUMNS.values(), row, strict=True):
        # SUM yields int, float (REAL affinity), or NULL for an all-NULL column.
        if isinstance(value, int | float) and not isinstance(value, bool):
            tokens[bucket] = int(value)
    if tokens["output"] is not None and tokens["reasoning"]:
        tokens["output"] = max(0, tokens["output"] - tokens["reasoning"])
    reported = [
        value for bucket in _SESSION_TOKEN_COLUMNS.values() if (value := tokens[bucket]) is not None
    ]
    if reported:
        tokens["total"] = sum(reported)
    return tokens


def _parse_arguments(raw: object) -> dict:
    """Normalize a tool call's ``arguments`` field to a dict.

    Providers disagree on the encoding: OpenAI-style adapters send a JSON
    *string*, others send an already-decoded object. Anything that is neither is
    preserved verbatim under ``raw_args`` rather than dropped.
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {"raw_args": raw}
        return decoded if isinstance(decoded, dict) else {"raw_args": raw}
    return {"raw_args": raw}


def extract_trajectory_from_db(db_path: Path) -> tuple[list[dict], list[str]]:
    """Extract the canonical trajectory from a Hermes ``state.db``.

    Reads the ``messages`` rows of every session in the run-scoped DB: an
    ``assistant`` row's ``tool_calls`` JSON opens a
    :class:`~devops_bench.agents.result.ToolCall`, and the matching ``tool`` row
    (keyed on ``tool_call_id``) folds the result in. All sessions are read, not
    just the newest, so the trajectory covers the same rows
    :func:`extract_tokens_from_db` sums its usage over. Extraction misses are
    reported on the returned error list rather than yielding a silent-empty
    trajectory.

    Args:
        db_path: Path to the run's ``state.db``.

    Returns:
        A ``(trajectory, errors)`` tuple. ``trajectory`` is a list of
        ``ToolCall.to_dict()`` mappings in call order.
    """
    errors: list[str] = []
    trajectory: list[dict] = []

    if not db_path.exists():
        errors.append(f"State database not found at {db_path}")
        return [], errors

    try:
        conn = _connect_ro(db_path)
        try:
            cursor = conn.cursor()
            if not cursor.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]:
                errors.append("No session found in state database")
                return [], errors

            # Ordered by ``sessions.rowid``, not by ``id``: hermes session ids
            # are UUIDs, so lexical ordering interleaves sessions arbitrarily.
            # rowid is insertion order and exists on every rowid table, whatever
            # the schema version.
            cursor.execute(
                "SELECT m.role, m.content, m.tool_calls, m.tool_call_id, m.tool_name"
                " FROM messages m JOIN sessions s ON m.session_id = s.id"
                " ORDER BY s.rowid, m.id"
            )
            messages = cursor.fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        errors.append(f"Database error: {exc}")
        return [], errors

    calls_by_id: dict[str, dict] = {}
    for role, content, tool_calls_json, tool_call_id, tool_name in messages:
        if role == "assistant" and tool_calls_json:
            try:
                tool_calls = json.loads(tool_calls_json)
            except json.JSONDecodeError as exc:
                errors.append(f"Failed to parse tool calls JSON: {exc}")
                continue
            if not isinstance(tool_calls, list):
                errors.append(f"Tool calls JSON is not a list: {type(tool_calls).__name__}")
                continue
            for entry in tool_calls:
                if not isinstance(entry, dict):
                    errors.append(f"Skipped non-object tool call entry: {type(entry).__name__}")
                    continue
                function = entry.get("function")
                if not isinstance(function, dict):
                    function = {}
                args = _parse_arguments(function.get("arguments"))
                call = ToolCall(name=function.get("name") or "unknown", args=args, status="called")
                trajectory.append(call.to_dict())
                if entry.get("id"):
                    calls_by_id[entry["id"]] = trajectory[-1]
        elif role == "tool" and tool_call_id:
            pending = calls_by_id.get(tool_call_id)
            if pending is not None:
                pending["result"] = content
                pending["status"] = "completed"
            else:
                # An orphan result still belongs in the trajectory (the call row
                # may predate the session cut), but the gap is worth surfacing.
                call = ToolCall(
                    name=tool_name or "unknown", args={}, result=content, status="completed"
                )
                trajectory.append(call.to_dict())
                errors.append(f"Found tool response for unknown tool_call_id: {tool_call_id}")

    return trajectory, errors
