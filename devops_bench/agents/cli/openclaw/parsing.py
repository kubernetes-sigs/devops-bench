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

"""Parsers for OpenClaw ``oc sessions export-trajectory`` bundles.

Folds the bundle's ``events.jsonl`` (dotted-``type`` events with a nested
``data`` payload) into the canonical :class:`ToolCall` shape, locates the bundle
on disk, and picks the session key from ``oc sessions --json`` output.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import NamedTuple

from devops_bench.agents.result import ToolCall
from devops_bench.agents.shared.telemetry import note_model
from devops_bench.agents.shared.timing import merged_span_sec, parse_event_time
from devops_bench.core import get_logger

__all__ = ["TrajectoryExport", "parse_trajectory_export"]

_log = get_logger("agents.cli.openclaw.parsing")

# OpenClaw emits ANSI-colored debug logs to stdout. The escape codes add noise
# to the text the judge grades, so strip them before returning the output.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _join_text(content: object) -> str:
    """Join the ``text`` parts of an OpenClaw message ``content`` value.

    ``content`` is either a plain string or a list of typed parts
    (``{"type": "text", "text": ...}`` / ``{"type": "toolCall", ...}``); only
    text parts contribute, so tool-call blocks embedded in an assistant message
    are ignored here (they ride on the dedicated ``tool.call`` events instead).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part["text"]
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


def _accumulate_usage(acc: dict, usage: dict, *, top_level: bool = True) -> None:
    """Sum one turn's token usage into a running accumulator, in place.

    OpenClaw emits a ``model.completed`` event per turn (per model call), each
    carrying that turn's ``usage``, so the session total is the sum across turns
    rather than the value of any single ``model.completed``. Numeric fields are
    added; nested mappings (e.g. a ``cost`` breakdown) are summed recursively;
    booleans and other non-numeric values are ignored.

    The top-level ``cacheWrite`` is skipped even though today's rollup omits it,
    so the bucket keeps a single source (:func:`_accumulate_cache_write`) on any
    openclaw version rather than double-counting on one that starts reporting
    it. The skip is deliberately not applied inside nested mappings: a ``cost``
    breakdown itemizes cache-write *dollars*, which have no second source, so
    dropping that entry would leave the sub-buckets short of their own total.

    Args:
        acc: Accumulator mutated in place.
        usage: A single turn's usage mapping.
        top_level: Whether ``usage`` is the usage mapping itself rather than a
            nested breakdown inside it.
    """
    for key, value in usage.items():
        if (top_level and key == "cacheWrite") or isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            acc[key] = acc.get(key, 0) + value
        elif isinstance(value, dict):
            nested = acc.setdefault(key, {})
            if isinstance(nested, dict):
                _accumulate_usage(nested, value, top_level=False)


def _accumulate_cache_write(acc: dict, usage: object) -> None:
    """Add one model call's ``cacheWrite`` to the running usage accumulator.

    ``model.completed.usage`` omits ``cacheWrite``; the per-call
    ``assistant.message.usage`` is the only event that carries it. So this one
    bucket is summed from here and every other bucket from ``model.completed``,
    which otherwise reconciles to the token against the per-call events.

    Args:
        acc: Token accumulator mutated in place.
        usage: A single ``assistant.message`` usage mapping, or anything else
            (ignored).
    """
    if not isinstance(usage, dict):
        return
    written = usage.get("cacheWrite")
    if isinstance(written, (int, float)) and not isinstance(written, bool):
        acc["cacheWrite"] = acc.get("cacheWrite", 0) + written


def _fold_cache_write_into_total(acc: dict) -> None:
    """Add the recovered ``cacheWrite`` to the rollup total, in place.

    ``model.completed`` omits ``cacheWrite`` from both the buckets *and* the
    ``total`` it reports, so a total copied through verbatim understates the run
    by exactly the cache writes :func:`_accumulate_cache_write` recovered. The
    canonical contract is that ``total`` is the sum of every bucket (see
    :data:`~devops_bench.agents.result.TOKEN_BUCKETS`), and cache writes are
    billed above input on Anthropic, so leaving the gap would understate the
    priciest bucket on every openclaw run.

    Args:
        acc: Token accumulator mutated in place. Left untouched when no
            ``cacheWrite`` was recovered or the rollup reported no total.
    """
    written = acc.get("cacheWrite")
    if not isinstance(written, (int, float)):
        return
    for key in ("total", "totalTokens"):
        current = acc.get(key)
        if isinstance(current, (int, float)):
            acc[key] = current + written


class TrajectoryExport(NamedTuple):
    """What one ``events.jsonl`` yielded.

    Attributes:
        trajectory: ``ToolCall.to_dict()`` mappings, in issue order.
        tokens: Canonical usage summed across the run.
        output: The agent's final answer text; ``""`` when none was found.
        errors: Extraction failures worth surfacing.
        model_turns: Model round-trips, or ``None`` when the export carried no
            ``assistant.message`` events to count.
        tool_wait_sec: Wall-clock seconds inside tool calls, concurrent calls
            counted once; ``None`` when no call could be timed.
        served_models: Distinct model ids the provider answered with, in
            first-seen order.
    """

    trajectory: list[dict]
    tokens: dict
    output: str
    errors: list[str]
    model_turns: int | None
    tool_wait_sec: float | None
    served_models: list[str]

    @classmethod
    def empty(cls, errors: list[str]) -> TrajectoryExport:
        """Return an export that recovered nothing but ``errors``."""
        return cls([], {}, "", errors, None, None, [])


def parse_trajectory_export(jsonl_text: str) -> TrajectoryExport:
    """Parse an ``oc sessions export-trajectory`` ``events.jsonl`` into the canonical shape.

    The export bundle's ``events.jsonl`` is line-delimited JSON. Each line is an
    event with a dotted ``type`` and an event-specific ``data`` payload:

    - ``tool.call`` -> ``data.name`` / ``data.arguments`` / ``data.toolCallId``
      (+ the event's top-level ``ts``, paired with the result's to time the call)
    - ``tool.result`` -> ``data.message`` with ``toolCallId`` + ``content[].text``
      (+ ``isError`` / ``details.status``)
    - ``model.completed`` -> ``data.usage`` (tokens) + ``data.assistantTexts``
      (the agent's final answer)
    - ``assistant.message`` -> ``data.message.content[].text`` (fallback output)
      + ``data.message.model``, the model that actually answered the call
      + ``data.message.usage.cacheWrite``, the only place cache-write tokens
      appear (``model.completed`` omits that one bucket)

    Matching ``tool.call`` / ``tool.result`` pairs (keyed on ``toolCallId``) fold
    into one :class:`ToolCall` so the metrics layer sees the canonical trajectory
    other agents emit. An unpaired ``tool.result`` (no matching call seen) is
    **dropped** from the trajectory and reported on ``errors``, matching the API
    agent's ``_fold_with_extraction_errors`` and the Gemini ``parse_stream_json``
    policy.

    Args:
        jsonl_text: Raw contents of ``events.jsonl`` inside the export bundle.

    Returns:
        A :class:`TrajectoryExport`. ``tokens`` is the usage summed across every
        ``model.completed`` turn (not just the last), plus ``cacheWrite`` summed
        across the per-call ``assistant.message`` events and folded into the
        reported total, which omits it too. ``model_turns`` counts
        ``assistant.message`` events, one per model round-trip -- which is not
        ``len(trajectory)``, because a single message can carry several
        ``toolCall`` entries (seen live) and a text-only message carries none.

        There is no reasoning bucket: openclaw's usage payload carries none at
        any thinking level (checked live at ``off`` and ``high``), so
        ``reasoning`` normalizes to ``None`` rather than a fabricated ``0``.
    """
    tokens: dict = {}
    errors: list[str] = []
    output = ""
    fallback_output: list[str] = []
    pending: dict[str, ToolCall] = {}
    trajectory: list[ToolCall] = []
    model_turns = 0
    started_at: dict[str, float] = {}
    spans: list[tuple[float, float]] = []
    served_models: list[str] = []

    for lineno, raw in enumerate(jsonl_text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"events line {lineno} parse error: {exc}")
            continue
        if not isinstance(entry, dict):
            continue

        etype = entry.get("type") or entry.get("event")
        event_time = parse_event_time(entry.get("ts"))
        data = entry.get("data")
        if not isinstance(data, dict):
            data = {}

        if etype == "tool.call":
            call_id = data.get("toolCallId") or data.get("id") or ""
            args = data.get("arguments")
            call = ToolCall(
                name=data.get("name", ""),
                args=args if isinstance(args, dict) else {},
                status="called",
            )
            trajectory.append(call)
            if call_id:
                pending[str(call_id)] = call
                if event_time is not None:
                    started_at[str(call_id)] = event_time
        elif etype == "tool.result":
            msg = data.get("message") if isinstance(data.get("message"), dict) else data
            call_id = msg.get("toolCallId") or msg.get("id") or ""
            text = _join_text(msg.get("content"))
            details = msg.get("details") if isinstance(msg.get("details"), dict) else {}
            is_error = bool(msg.get("isError")) or (
                str(details.get("status", "")).lower() in ("error", "failed", "failure")
            )
            target = pending.pop(str(call_id), None) if call_id else None
            if target is None:
                # Drop the orphan from the trajectory but surface it on errors.
                # Synthesizing a free-floating result entry would break the
                # "every trajectory item is a real ToolCall the model issued"
                # invariant the metrics layer relies on; the API agent's
                # ``_fold_with_extraction_errors`` and the Gemini stream-json
                # parser both apply the same rule, so every agent feeds the
                # metrics seam an identical canonical shape.
                preview = text[:80].replace("\n", " ")
                errors.append(
                    f"events tool.result without matching call "
                    f"(id={call_id!r}, content={preview!r})"
                )
                continue
            target.result = text
            target.status = "error" if is_error else "completed"
            start = started_at.pop(str(call_id), None)
            if start is not None and event_time is not None and event_time >= start:
                spans.append((start, event_time))
        elif etype == "model.completed":
            usage = data.get("usage")
            if isinstance(usage, dict):
                _accumulate_usage(tokens, usage)
            texts = data.get("assistantTexts")
            if isinstance(texts, list):
                joined = "\n".join(t for t in texts if isinstance(t, str))
                if joined:
                    output = joined
        elif etype == "assistant.message":
            model_turns += 1
            msg = data.get("message") if isinstance(data.get("message"), dict) else {}
            note_model(served_models, msg.get("model"))
            _accumulate_cache_write(tokens, msg.get("usage"))
            txt = _join_text(msg.get("content"))
            if txt:
                fallback_output.append(txt)

    if not output and fallback_output:
        output = "\n".join(fallback_output)
    _fold_cache_write_into_total(tokens)

    return TrajectoryExport(
        trajectory=[call.to_dict() for call in trajectory],
        tokens=tokens,
        output=output,
        errors=errors,
        model_turns=model_turns or None,
        tool_wait_sec=merged_span_sec(spans),
        served_models=served_models,
    )


def _read_export_bundle(workspace: Path) -> tuple[str, list[str]]:
    """Locate and read ``events.jsonl`` inside an ``export-trajectory`` bundle.

    The bundle is written under
    ``<workspace>/.openclaw/trajectory-exports/openclaw-trajectory-<id>-<ts>/``;
    the trajectory itself is ``events.jsonl`` (siblings: ``manifest.json``,
    ``tools.json``, ``metadata.json``, ...). There is exactly one export per run,
    so a recursive glob suffices. The final answer + token usage are parsed out
    of ``events.jsonl`` (``model.completed`` / ``assistant.message``), so no
    separate output file is read.

    Args:
        workspace: Workspace dir handed to ``oc sessions export-trajectory --workspace``.

    Returns:
        A ``(events_jsonl, errors)`` tuple. ``events_jsonl`` is empty when the
        bundle or file is missing; the miss is recorded on ``errors``.
    """
    errors: list[str] = []
    export_root = workspace / ".openclaw" / "trajectory-exports"
    if not export_root.exists():
        errors.append(f"export-trajectory bundle missing: {export_root}")
        return "", errors

    event_files = sorted(export_root.rglob("events.jsonl"))
    if not event_files:
        errors.append(f"no events.jsonl under {export_root}")
        return "", errors
    try:
        return event_files[0].read_text(encoding="utf-8"), errors
    except OSError as exc:
        errors.append(f"failed to read {event_files[0]}: {exc}")
        return "", errors


def _pick_session_key(sessions_json: str) -> str | None:
    """Return the single session key from ``oc sessions --json`` output, or ``None``.

    The output may be a list of rows or a wrapper dict with a ``sessions``
    list (per ``docs/openclaw/sessions.md``). Because each run uses fresh
    isolated state, exactly one row is expected; if more than one is present
    the first is taken (with a debug log).

    Args:
        sessions_json: Raw stdout from ``oc sessions --agent <name> --json``.

    Returns:
        The ``key`` field of the chosen session, or ``None`` if parsing
        failed or no sessions were returned.
    """
    try:
        data = json.loads(sessions_json)
    except json.JSONDecodeError:
        return None
    rows = data.get("sessions") if isinstance(data, dict) else data
    if not isinstance(rows, list) or not rows:
        return None
    if len(rows) > 1:
        _log.debug("oc sessions returned %d rows; using the first", len(rows))
    first = rows[0]
    if not isinstance(first, dict):
        return None
    key = first.get("key")
    return key if isinstance(key, str) and key else None
