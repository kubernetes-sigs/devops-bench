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

"""Parser for the Claude Code ``--output-format stream-json`` event stream.

Folds the ``tool_use`` / ``tool_result`` content blocks into the canonical
:class:`ToolCall` list and pulls the final answer and token usage from the
terminal ``result`` event. ``thinking`` / ``redacted_thinking`` blocks are
dropped so the trajectory matches the tool-calls-only shape the other CLI
harnesses emit.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

from devops_bench.agents.result import TerminalReason, ToolCall, empty_tokens
from devops_bench.agents.shared.telemetry import ParsedRun, int_or_none, note_model
from devops_bench.agents.shared.timing import merged_span_sec, parse_event_time

__all__ = ["parse_stream_json"]


# Claude Code echoes the full body of every tool result into the stream, so an
# uncapped trajectory grows with the agent's file reads and command output. The
# whole trace is re-serialized into the LLM judge prompts downstream, where a
# multi-megabyte trace overruns the judge's context, so keep a head and tail
# slice — metric matching only needs the edges, not the middle.
_RESULT_HEAD_CHARS = 12000
_RESULT_TAIL_CHARS = 4000
_RESULT_MAX_CHARS = _RESULT_HEAD_CHARS + _RESULT_TAIL_CHARS


def _clip_result(text: str) -> str:
    """Head+tail slice ``text`` to :data:`_RESULT_MAX_CHARS`, marking the elision."""
    if len(text) <= _RESULT_MAX_CHARS:
        return text
    elided = len(text) - _RESULT_MAX_CHARS
    return (
        f"{text[:_RESULT_HEAD_CHARS]}\n... [{elided} chars elided] ...\n"
        f"{text[-_RESULT_TAIL_CHARS:]}"
    )


def _block_text(content: object) -> str | None:
    """Render a tool_result ``content`` payload to a string, or ``None``.

    Claude Code emits tool results either as a bare string or as a list of
    content blocks. Text blocks contribute their text; any other block (e.g. an
    ``image``) is JSON-encoded in place so nothing is dropped silently. The
    rendered text is clipped (see :func:`_clip_result`).
    """
    if content is None:
        return None
    if isinstance(content, str):
        return _clip_result(content)
    if isinstance(content, list):
        if not content:
            return None
        parts = [
            block["text"]
            if isinstance(block, dict) and isinstance(block.get("text"), str)
            else json.dumps(block, default=str)
            for block in content
        ]
        return _clip_result("".join(parts))
    return _clip_result(json.dumps(content, default=str))


# Claude Code namespaces MCP tools as ``mcp__<server>__<tool>``. The rest of the
# pipeline uses the ``<server>__<tool>`` convention (the metrics canonicalizer
# strips exactly one ``<server>__`` segment to recover the bare tool name), so
# drop the literal ``mcp__`` client prefix to stay consistent with the other
# harnesses. Built-in tools (``Bash``, ``Read``, ...) carry no prefix.
_MCP_TOOL_PREFIX = "mcp__"


def _normalize_tool_name(name: str) -> str:
    """Strip Claude Code's ``mcp__`` client prefix from an MCP tool name."""
    if name.startswith(_MCP_TOOL_PREFIX):
        return name[len(_MCP_TOOL_PREFIX) :]
    return name


# Terminal-failure MCP statuses in the ``init`` event. Transient ``pending`` /
# ``connecting`` states are excluded: a stdio server (e.g. gke-mcp) often reports
# them at init yet connects moments later, so flagging them would false-positive
# on a working run.
_MCP_FAILED_STATUSES = frozenset({"failed", "error", "disconnected", "needs-auth", "needs_auth"})


# The CLI's own loop-exit reasons; every other reason it emits is a failure.
_CLI_COMPLETED_REASON = "completed"
_CLI_TURN_CAP_REASON = "max_turns"
_CLI_TURN_CAP_SUBTYPE = "error_max_turns"


def _iter_events(stdout: str) -> Iterator[tuple[object, str | None]]:
    """Yield ``(event, error)`` pairs from the stream, one populated per item.

    The stream is normally newline-delimited JSON, but a rebuffered or truncated
    pipe can concatenate several objects onto one physical line. Each line is
    decoded with ``raw_decode`` in a loop so every object is recovered rather
    than lost to a single ``Extra data`` error. A malformed remainder yields one
    error and the rest of that line is abandoned.

    Splitting on ``"\\n"`` rather than :meth:`str.splitlines` is deliberate: the
    CLI leaves U+0085 (NEL) unescaped inside JSON strings, and ``splitlines``
    would treat it as a line break, shredding that event into two undecodable
    fragments.
    """
    decoder = json.JSONDecoder()
    for lineno, raw in enumerate(stdout.split("\n"), start=1):
        line = raw.strip()
        if not line:
            continue
        idx = 0
        while idx < len(line):
            while idx < len(line) and line[idx].isspace():
                idx += 1
            if idx >= len(line):
                break
            try:
                event, idx = decoder.raw_decode(line, idx)
            except json.JSONDecodeError as exc:
                yield None, f"stream-json line {lineno} parse error: {exc}"
                break
            yield event, None


def parse_stream_json(stdout: str) -> ParsedRun:
    """Parse a Claude Code ``--output-format stream-json`` stdout stream.

    The stream is newline-delimited JSON in the wrapped SDK form: each line is
    an envelope with a top-level ``type`` (``system`` / ``assistant`` / ``user``
    / ``result``) carrying a nested Anthropic ``message`` object. The parser is
    intentionally lenient (unknown event types are skipped) and surfaces both
    per-line JSON decode errors and unmatched ``tool_result`` blocks on the
    ``errors`` list rather than dropping them.

    | Event type    | Handling                                                  |
    |---------------|-----------------------------------------------------------|
    | ``system``    | ``init`` MCP statuses checked; other metadata ignored     |
    | ``assistant`` | ``tool_use`` → pending ToolCalls; ``text`` → output;      |
    |               | ``message.id`` → model turns; ``message.model`` → served; |
    |               | ``thinking`` / ``redacted_thinking`` dropped              |
    | ``user``      | ``tool_result`` blocks matched to pending ToolCalls       |
    | ``result``    | terminal: authoritative answer, token usage, failure flag,|
    |               | ``terminal_reason``                                       |

    ``tool_wait_sec`` pairs each ``tool_use`` with its ``tool_result`` by
    envelope ``timestamp``. The terminal ``duration_ms``/``duration_api_ms`` are
    *not* used: their difference goes negative on real runs (observed 4901ms
    wall against 6553ms of API time).

    The accumulated assistant ``text`` doubles as a fallback answer when no
    terminal ``result`` event arrives (a truncated pipe) or when it carries an
    empty answer (error subtypes emit ``""``). Likewise token usage falls back to
    the per-turn accumulator only when the terminal event reports no usage;
    per-turn usage is deduped by message id, since Claude Code repeats the same
    ``usage`` on every content-block envelope of one API message. That fallback
    recovers the prompt-side buckets only (see :data:`_ACC_USAGE_KEYS`).

    Args:
        stdout: Raw process stdout, possibly empty.

    Returns:
        A :class:`~devops_bench.agents.shared.telemetry.ParsedRun`.
        ``model_turns`` counts distinct assistant ``message.id`` values, not
        ``num_turns``, which the CLI raises once per content block rather than
        per model call. ``served_models`` comes from the assistant envelopes,
        not ``modelUsage``, whose keys include the CLI's own helper model.
    """
    text_parts: list[str] = []
    result_output: str | None = None
    tokens: dict = empty_tokens()
    result_usage_seen = False
    acc_usage: dict = {}
    # Doubles as the model-turn counter; one envelope per content block repeats the id.
    seen_message_ids: set[str] = set()
    errors: list[str] = []
    # FIFO per id: reused ids match in emission order instead of overwriting.
    pending: dict[str, list[tuple[ToolCall, float | None]]] = {}
    trajectory: list[ToolCall] = []
    spans: list[tuple[float, float]] = []
    served_models: list[str] = []
    terminal_reason: TerminalReason = ""

    for event, error in _iter_events(stdout):
        if error is not None:
            errors.append(error)
            continue
        if not isinstance(event, dict):
            continue

        etype = event.get("type")
        if etype == "system":
            # A failed MCP server leaves the run tool-less but still exits 0, so
            # surface terminal-failure statuses (see ``_MCP_FAILED_STATUSES``)
            # rather than scoring a silently-degraded run clean.
            if event.get("subtype") == "init":
                mcp_servers = event.get("mcp_servers")
                if not isinstance(mcp_servers, list):
                    continue
                for server in mcp_servers:
                    if not isinstance(server, dict):
                        continue
                    status = str(server.get("status", "")).lower()
                    if status in _MCP_FAILED_STATUSES:
                        name = server.get("name") or "<unknown>"
                        errors.append(f"mcp server {name!r} failed to connect at init: {status}")
        elif etype == "assistant":
            message = event.get("message")
            if not isinstance(message, dict):
                continue
            # A CLI-rendered error envelope carries a synthetic model and no real call.
            if not event.get("is_api_error_message"):
                # Per-turn usage backs a truncated stream; dedupe the repeated id.
                msg_id = message.get("id")
                identified = isinstance(msg_id, str) and bool(msg_id)
                if not (identified and msg_id in seen_message_ids):
                    if identified:
                        seen_message_ids.add(str(msg_id))
                    _add_usage(acc_usage, message.get("usage"))
                note_model(served_models, message.get("model"))
            event_time = parse_event_time(event.get("timestamp"))
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    if isinstance(block.get("text"), str):
                        text_parts.append(block["text"])
                elif btype in ("thinking", "redacted_thinking"):
                    continue  # not part of the tool-calls-only trajectory
                elif btype == "tool_use":
                    args = block.get("input")
                    raw_name = block.get("name")
                    call = ToolCall(
                        name=_normalize_tool_name(raw_name if isinstance(raw_name, str) else ""),
                        args=args if isinstance(args, dict) else {},
                        status="called",
                    )
                    trajectory.append(call)
                    call_id = block.get("id")
                    if call_id:
                        pending.setdefault(str(call_id), []).append((call, event_time))
        elif etype == "user":
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                # A user message with a bare-string content echoes the prompt.
                continue
            event_time = parse_event_time(event.get("timestamp"))
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                call_id = block.get("tool_use_id") or ""
                queue = pending.get(str(call_id)) if call_id else None
                entry = queue.pop(0) if queue else None
                if entry is None:
                    errors.append(
                        f"stream-json tool_result without matching tool_use (id={call_id!r})"
                    )
                    continue
                target, started = entry
                target.result = _block_text(block.get("content"))
                target.status = "error" if block.get("is_error") else "completed"
                if started is not None and event_time is not None:
                    spans.append((started, event_time))
        elif etype == "result":
            # Terminal event: ``result`` is the authoritative answer, ``usage``
            # holds token accounting, and failure shows up as either an
            # ``error_*`` subtype or an ``is_error`` flag. Guard against a later
            # degenerate ``result`` (empty answer / no usage) clobbering an
            # earlier good one.
            tail = event.get("result")
            if isinstance(tail, str) and not result_output:
                result_output = tail
            usage = event.get("usage")
            if isinstance(usage, dict) and _has_usage(usage):
                tokens = _usage_tokens(usage)
                result_usage_seen = True
            # First terminal event wins; a later one no longer reflects the reason.
            if terminal_reason:
                continue
            cli_reason = event.get("terminal_reason")
            cli_reason = cli_reason if isinstance(cli_reason, str) and cli_reason else None
            subtype = event.get("subtype")
            status = event.get("api_error_status")
            detail = f" (api status {status})" if status is not None else ""
            if cli_reason is not None and cli_reason != _CLI_COMPLETED_REASON:
                # Names the failure; the flags below only say one happened.
                note = f"stream-json result terminal_reason: {cli_reason}{detail}"
            elif isinstance(subtype, str) and subtype.startswith("error_"):
                note = f"stream-json result error: {subtype}"
            elif event.get("is_error"):
                # A failed call can still say ``subtype: "success"`` (observed: a 404).
                note = f"stream-json result flagged is_error{detail}"
            else:
                note = ""
            if note:
                errors.append(note)
            # ``terminal_reason`` is optional; a binary omitting it resolves from the flags.
            capped = cli_reason == _CLI_TURN_CAP_REASON or subtype == _CLI_TURN_CAP_SUBTYPE
            terminal_reason = "error" if note and not capped else "completed"

    # ``result_output`` may be an empty string (error subtypes emit ``""``); fall
    # back to the accumulated assistant text so a real partial answer survives.
    output = result_output or "".join(text_parts)
    # Only fall back to summed per-turn usage when the terminal event reported no
    # recognized usage — a terminal event that reported genuine zeros is trusted.
    if not result_usage_seen and acc_usage:
        tokens = _usage_tokens(acc_usage)
    return ParsedRun(
        output=output,
        trajectory=[call.to_dict() for call in trajectory],
        tokens=tokens,
        errors=errors,
        terminal_reason=terminal_reason,
        model_turns=len(seen_message_ids) or None,
        tool_wait_sec=merged_span_sec(spans),
        served_models=served_models,
    )


_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def _has_usage(usage: dict) -> bool:
    """True if ``usage`` has a recognized count, so a genuine zero is not a miss."""
    return any(int_or_none(usage.get(key)) is not None for key in _USAGE_KEYS)


# Per-turn ``output_tokens`` is a ``message_start`` placeholder (observed 3 vs 3069).
_ACC_USAGE_KEYS = tuple(key for key in _USAGE_KEYS if key != "output_tokens")


def _add_usage(acc: dict, usage: object) -> None:
    """Fold a per-turn ``usage`` block in; a stand-in for a truncated stream."""
    if not isinstance(usage, dict):
        return
    for key in _ACC_USAGE_KEYS:
        val = int_or_none(usage.get(key))
        if val is not None:
            acc[key] = acc.get(key, 0) + val


def _usage_tokens(usage: dict) -> dict[str, int | None]:
    """Normalize an Anthropic ``usage`` block onto :data:`TOKEN_BUCKETS`.

    Thinking is billed inside ``output_tokens`` and repeated under
    ``output_tokens_details``, so it is subtracted back out to keep ``output``
    free of ``reasoning``. ``total`` is filled only once ``output`` is known: a
    prompt-side-only sum would reach the dashboard as an undercount.
    """
    tokens = empty_tokens()
    output = int_or_none(usage.get("output_tokens"))
    details = usage.get("output_tokens_details")
    reasoning = int_or_none(details.get("thinking_tokens")) if isinstance(details, dict) else None
    if output is not None and reasoning is not None:
        output = max(0, output - reasoning)
    tokens.update(
        input=int_or_none(usage.get("input_tokens")),
        cached=int_or_none(usage.get("cache_read_input_tokens")),
        cache_write=int_or_none(usage.get("cache_creation_input_tokens")),
        reasoning=reasoning,
        output=output,
    )
    if output is not None:
        tokens["total"] = sum(v for k, v in tokens.items() if k != "total" and v is not None)
    return tokens
