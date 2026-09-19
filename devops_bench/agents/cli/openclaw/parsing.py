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

from devops_bench.agents.result import ToolCall
from devops_bench.agents.shared.telemetry import ParsedRun, note_model
from devops_bench.agents.shared.timing import merged_span_sec, parse_event_time
from devops_bench.core import get_logger

__all__ = ["parse_trajectory_export"]

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

    One ``model.completed`` per turn, so the session total is the sum of them.
    Top-level ``cacheWrite`` is left to :func:`_resolve_cache_write`, but not
    nested: a ``cost`` breakdown itemizes cache-write *dollars*, which have no
    second source.
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
    """Sum one event's ``cacheWrite`` into ``acc``, in place."""
    if not isinstance(usage, dict):
        return
    written = usage.get("cacheWrite")
    if isinstance(written, (int, float)) and not isinstance(written, bool):
        acc["cacheWrite"] = acc.get("cacheWrite", 0) + written


def _resolve_cache_write(acc: dict, rollup: dict, per_call: dict) -> None:
    """Settle the ``cacheWrite`` bucket on whichever event reported it.

    Today's rollup omits ``cacheWrite`` from its buckets *and* its total, so a
    per-call value is added into the total; a rollup value is taken as-is.
    """
    written = rollup.get("cacheWrite")
    if isinstance(written, (int, float)):
        acc["cacheWrite"] = written
        return
    written = per_call.get("cacheWrite")
    if not isinstance(written, (int, float)):
        return
    acc["cacheWrite"] = written
    for key in ("total", "totalTokens"):
        current = acc.get(key)
        if isinstance(current, (int, float)):
            acc[key] = current + written


def parse_trajectory_export(jsonl_text: str) -> ParsedRun:
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
        A :class:`~devops_bench.agents.shared.telemetry.ParsedRun`. ``tokens``
        sums every ``model.completed`` turn. Reasoning stays ``None``: openclaw
        reports none at any thinking level. ``model_turns`` counts
        ``assistant.message`` events, which is not ``len(trajectory)``.
    """
    tokens: dict = {}
    rollup_cache_write: dict = {}
    per_call_cache_write: dict = {}
    errors: list[str] = []
    output = ""
    fallback_output: list[str] = []
    # FIFO per id: reused ids match in emission order instead of overwriting.
    pending: dict[str, list[tuple[ToolCall, float | None]]] = {}
    trajectory: list[ToolCall] = []
    model_turns = 0
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
                name=str(data.get("name") or ""),
                args=args if isinstance(args, dict) else {},
                status="called",
            )
            trajectory.append(call)
            if call_id:
                pending.setdefault(str(call_id), []).append((call, event_time))
        elif etype == "tool.result":
            msg = data.get("message") if isinstance(data.get("message"), dict) else data
            call_id = msg.get("toolCallId") or msg.get("id") or ""
            text = _join_text(msg.get("content"))
            details = msg.get("details") if isinstance(msg.get("details"), dict) else {}
            is_error = bool(msg.get("isError")) or (
                str(details.get("status", "")).lower() in ("error", "failed", "failure")
            )
            queue = pending.get(str(call_id)) if call_id else None
            entry = queue.pop(0) if queue else None
            if entry is None:
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
            target, started = entry
            target.result = text
            target.status = "error" if is_error else "completed"
            if started is not None and event_time is not None:
                spans.append((started, event_time))
        elif etype == "model.completed":
            usage = data.get("usage")
            if isinstance(usage, dict):
                _accumulate_usage(tokens, usage)
                _accumulate_cache_write(rollup_cache_write, usage)
            texts = data.get("assistantTexts")
            if isinstance(texts, list):
                joined = "\n".join(t for t in texts if isinstance(t, str))
                if joined:
                    output = joined
        elif etype == "assistant.message":
            model_turns += 1
            msg = data.get("message") if isinstance(data.get("message"), dict) else {}
            note_model(served_models, msg.get("model"))
            _accumulate_cache_write(per_call_cache_write, msg.get("usage"))
            txt = _join_text(msg.get("content"))
            if txt:
                fallback_output.append(txt)

    if not output and fallback_output:
        output = "\n".join(fallback_output)
    _resolve_cache_write(tokens, rollup_cache_write, per_call_cache_write)

    return ParsedRun(
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
