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

"""Parser for the Gemini CLI ``--output-format stream-json`` event stream.

Folds ``tool_use``/``tool_result`` events into the canonical :class:`ToolCall`
list and pulls the final text and aggregated token usage from ``result`` events.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import NamedTuple

from devops_bench.agents.result import ToolCall, empty_tokens
from devops_bench.agents.shared.telemetry import note_model
from devops_bench.agents.shared.timing import merged_span_sec, parse_event_time

__all__: list[str] = ["StreamParse", "parse_stream_json"]


def _int_or_none(value: object) -> int | None:
    """Coerce to ``int``, rejecting ``bool`` (a JSON ``true`` is not a count)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _canonical_tokens(stats: Mapping[str, object]) -> dict[str, int | None]:
    """Map the terminal ``result.stats`` block onto the canonical token buckets.

    The CLI reports ``input_tokens`` as the *full* prompt with ``cached`` as a
    subset, and rolls thinking tokens into ``total_tokens`` only — so canonical
    ``input`` is the difference, and ``reasoning`` is derived from the total gap
    (``total - full_input - output``) when every part is reported. Both
    subtractions are clamped at ``0`` so an over-reported ``cached`` (or a
    rounding quirk) can never yield a negative bucket.
    """
    full_input = _int_or_none(stats.get("input_tokens"))
    output = _int_or_none(stats.get("output_tokens"))
    total = _int_or_none(stats.get("total_tokens"))
    cached = _int_or_none(stats.get("cached"))
    inp = (
        max(full_input - cached, 0) if full_input is not None and cached is not None else full_input
    )
    reasoning = None
    if full_input is not None and output is not None and total is not None:
        gap = total - full_input - output
        if gap >= 0:
            reasoning = gap
    tokens = empty_tokens()
    tokens.update(input=inp, cached=cached, reasoning=reasoning, output=output, total=total)
    return tokens


class StreamParse(NamedTuple):
    """What one ``--output-format stream-json`` stdout stream yielded.

    Attributes:
        output: Concatenated assistant text.
        trajectory: ``ToolCall.to_dict()`` mappings, ordered as emitted.
        tokens: Canonical token buckets from the terminal ``result.stats``.
        errors: Decode failures, unmatched ``tool_result`` events, and the
            stream's own ``error`` events (e.g. a provider rate limit), which
            can accompany an otherwise clean exit.
        tool_wait_sec: Wall-clock seconds inside tool calls, concurrent calls
            counted once; ``None`` when no call could be timed.
        served_models: Distinct model ids the CLI actually used, in first-seen
            order -- ``init.model`` plus every key of ``result.stats.models``,
            since the requested id can be an alias (``gemini-3-flash`` resolved
            to ``gemini-3-flash-preview`` in a live run).
    """

    output: str
    trajectory: list[dict]
    tokens: dict
    errors: list[str]
    tool_wait_sec: float | None
    served_models: list[str]


def parse_stream_json(stdout: str) -> StreamParse:
    """Parse a Gemini ``--output-format stream-json`` stdout stream.

    The stream is newline-delimited JSON events. The parser is intentionally
    lenient (an unknown event type is skipped) and surfaces both per-line JSON
    decode errors and unmatched ``tool_result`` events on the ``errors`` list
    rather than silently dropping them.

    | Event type      | Fields read                                              |
    |-----------------|---------------------------------------------------------|
    | ``init``        | ``model`` (the id the CLI resolved to)                  |
    | ``message``     | ``role`` (assistant text accumulated into the output)   |
    | ``tool_use``    | ``tool_name``, ``tool_id``, ``parameters``, ``timestamp``|
    | ``tool_result`` | ``tool_id``, ``status``, ``timestamp`` (no payload)     |
    | ``error``       | recorded on the errors list                             |
    | ``result``      | ``output``/``response``, ``stats`` (tokens, ``models``) |

    Args:
        stdout: Raw process stdout, possibly empty.

    Returns:
        A :class:`StreamParse`. Every event carries a ``timestamp``, so pairing
        ``tool_use`` with its ``tool_result`` gives each call a real duration
        and the run a total tool wait -- without which a slow cluster and a slow
        model are the same number on the leaderboard.
    """
    output_parts: list[str] = []
    tokens: dict = {}
    errors: list[str] = []
    pending: dict[str, ToolCall] = {}
    trajectory: list[ToolCall] = []
    started_at: dict[str, float] = {}
    spans: list[tuple[float, float]] = []
    served_models: list[str] = []

    for lineno, raw in enumerate(stdout.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"stream-json line {lineno} parse error: {exc}")
            continue
        if not isinstance(event, dict):
            continue

        etype = event.get("type")
        event_time = parse_event_time(event.get("timestamp"))
        if etype == "init":
            note_model(served_models, event.get("model"))
        elif etype == "message":
            # ``role="user"`` echoes the prompt and is skipped.
            if event.get("role") in ("assistant", "model"):
                content = event.get("content")
                if isinstance(content, str):
                    output_parts.append(content)
                elif isinstance(content, list):
                    # Defensive: a parts-list shape (``[{"text": ...}]``).
                    for part in content:
                        if isinstance(part, dict) and isinstance(part.get("text"), str):
                            output_parts.append(part["text"])
        elif etype == "tool_use":
            call_id = event.get("tool_id") or event.get("id") or event.get("tool_use_id") or ""
            args = event.get("parameters")
            if args is None:
                args = event.get("input")
            if args is None:
                args = event.get("args")
            call = ToolCall(
                name=event.get("tool_name") or event.get("name", ""),
                args=args if isinstance(args, dict) else {},
                status="called",
            )
            trajectory.append(call)
            if call_id:
                pending[str(call_id)] = call
                if event_time is not None:
                    started_at[str(call_id)] = event_time
        elif etype == "tool_result":
            call_id = event.get("tool_id") or event.get("tool_use_id") or event.get("id") or ""
            target = pending.pop(str(call_id), None) if call_id else None
            if target is None:
                errors.append(f"stream-json tool_result without matching tool_use (id={call_id!r})")
                continue
            # tool_result carries only a status; accept a content/output
            # payload as a fallback when present.
            content = event.get("content")
            if content is None:
                content = event.get("output")
            if content is not None:
                target.result = (
                    content if isinstance(content, str) else json.dumps(content, default=str)
                )
            status = str(event.get("status", "")).lower()
            failed = bool(event.get("is_error")) or status in ("error", "failed", "failure")
            target.status = "error" if failed else "completed"
            start = started_at.pop(str(call_id), None)
            if start is not None and event_time is not None and event_time >= start:
                spans.append((start, event_time))
        elif etype == "error":
            msg = event.get("message") or event.get("error") or str(event)
            errors.append(f"stream-json error event: {msg}")
        elif etype == "result":
            # Terminal event: answer streams via ``message`` events and token
            # usage rides under ``stats``; accept ``output``/``response`` and
            # ``tokens``/``usage`` as fallbacks.
            tail = event.get("output") or event.get("response")
            if isinstance(tail, str) and tail:
                output_parts.append(tail)
            stats = event.get("stats")
            usage = event.get("tokens") or event.get("usage")
            if isinstance(stats, dict):
                tokens = _canonical_tokens(stats)
                # ``stats.models`` is keyed by the model that served each slice
                # of the usage, so a mid-run switch shows up as a second key.
                per_model = stats.get("models")
                if isinstance(per_model, Mapping):
                    for name in per_model:
                        note_model(served_models, name)
            elif isinstance(usage, dict):
                tokens = usage

    return StreamParse(
        output="".join(output_parts),
        trajectory=[call.to_dict() for call in trajectory],
        tokens=tokens,
        errors=errors,
        tool_wait_sec=merged_span_sec(spans),
        served_models=served_models,
    )
