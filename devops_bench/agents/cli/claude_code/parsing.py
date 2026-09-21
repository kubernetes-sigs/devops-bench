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

Calls a delegated subagent made are attributed to it rather than merged into
the top-level agent's, via the ``parent_tool_use_id`` the CLI tags them with,
and named from the ``subagent_type`` it stamps alongside (see
:data:`_SUBAGENT_TYPE_FIELD` for the three naming sources and their authority
order, and :func:`_attribute_actors` for how an unnameable delegate is handled).
"""

from __future__ import annotations

import json
from collections.abc import Iterator

from devops_bench.agents.result import ROOT_ACTOR, SUBAGENT_ACTOR, ToolCall, empty_tokens

__all__ = ["parse_stream_json"]


def _int_or_none(val: object) -> int | None:
    """Return ``val`` if it is a real ``int`` (``bool`` rejected), else ``None``.

    Mirrors ``results/normalize._coerce_int``'s bool rejection so a stray JSON
    ``true`` in a usage field never surfaces as a token count.
    """
    return val if isinstance(val, int) and not isinstance(val, bool) else None


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


# Claude Code interleaves a delegated subagent's turns into the same stream as
# the top-level agent's, tagging every envelope from inside a delegation with
# ``parent_tool_use_id`` — the id of the spawning tool call. Nothing else marks
# them, so without reading that field a subagent's tool calls land in the
# trajectory indistinguishable from the top-level agent's own, and a metric
# cannot tell which agent in the fleet did what.
_PARENT_ID_FIELD = "parent_tool_use_id"

# The delegate's role name. It reaches the stream three ways, in descending
# order of authority:
#
# 1. Stamped by the CLI on each delegated envelope, beside ``parent_tool_use_id``.
# 2. Stamped by the CLI on the ``task_*`` lifecycle events, keyed by the
#    spawning call's ``tool_use_id``.
# 3. Supplied by the *model* as an argument on the spawning call (``Task``'s
#    ``subagent_type``).
#
# The first two are the CLI's own bookkeeping and agree with each other; the
# third is model-authored tool input and is only consulted last, since a label
# the agent under test chose is the one an agent could misreport.
_SUBAGENT_TYPE_FIELD = "subagent_type"

# ``tool_use_id`` on a ``task_*`` lifecycle event points at the spawning call,
# which is what a delegated entry's ``parent_id`` holds.
_TOOL_USE_ID_FIELD = "tool_use_id"


def _label_from(event: dict) -> str | None:
    """Return the stripped :data:`_SUBAGENT_TYPE_FIELD` on ``event``, if usable."""
    label = event.get(_SUBAGENT_TYPE_FIELD)
    return label.strip() if isinstance(label, str) and label.strip() else None


def _attribute_actors(
    trajectory: list[ToolCall],
    stamped_labels: dict[str, str],
    arg_labels: dict[str, str],
) -> None:
    """Stamp :attr:`ToolCall.actor` on every entry of a delegated run, in place.

    A run with no delegation is left completely untouched — including the
    ``call_id`` / ``parent_id`` collected along the way, which are cleared. That
    keeps a single-agent trajectory serializing byte-identically to what this
    parser emitted before attribution existed, so scores for the many runs that
    never delegate cannot move (see :class:`~devops_bench.agents.result.ToolCall`).

    Args:
        trajectory: Parsed calls in emission order, mutated in place.
        stamped_labels: Spawning ``call_id`` -> delegate role name, as stamped by
            the CLI. Authoritative.
        arg_labels: Spawning ``call_id`` -> delegate role name, as supplied by the
            model on the spawning call. Consulted only where the CLI stamped none.
    """
    if not any(call.parent_id for call in trajectory):
        for call in trajectory:
            call.call_id = None
            call.parent_id = None
        return
    # Distinct anonymous delegates must not collapse onto one label: two workers
    # sharing an ``actor`` reads as one agent making every call, which is the
    # same misattribution as folding them into the root, one level down.
    anonymous: dict[str, str] = {}
    for call in trajectory:
        if call.parent_id is None:
            call.actor = ROOT_ACTOR
            continue
        label = stamped_labels.get(call.parent_id) or arg_labels.get(call.parent_id)
        if label is None:
            # Known to come from *some* delegate — never folded back into the
            # root, which would assert the top-level agent made this call.
            label = anonymous.setdefault(call.parent_id, f"{SUBAGENT_ACTOR}-{len(anonymous) + 1}")
        call.actor = label


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


def parse_stream_json(stdout: str) -> tuple[str, list[dict], dict, list[str]]:
    """Parse a Claude Code ``--output-format stream-json`` stdout stream.

    The stream is newline-delimited JSON in the wrapped SDK form: each line is
    an envelope with a top-level ``type`` (``system`` / ``assistant`` / ``user``
    / ``result``) carrying a nested Anthropic ``message`` object. The parser is
    intentionally lenient (unknown event types are skipped) and surfaces both
    per-line JSON decode errors and unmatched ``tool_result`` blocks on the
    ``errors`` list rather than dropping them.

    | Event type    | Handling                                                  |
    |---------------|-----------------------------------------------------------|
    | ``system``    | ``init`` MCP statuses checked; ``task_*`` lifecycle names |
    |               | the delegate; other metadata ignored                      |
    | ``assistant`` | ``tool_use`` → pending ToolCalls; ``text`` → output;      |
    |               | ``thinking`` / ``redacted_thinking`` dropped;             |
    |               | ``parent_tool_use_id`` → subagent attribution             |
    | ``user``      | ``tool_result`` blocks matched to pending ToolCalls       |
    | ``result``    | terminal: authoritative answer, token usage, failure flag |

    Every envelope, whatever its type, is also scanned for the ``subagent_type``
    the CLI stamps on a delegation, so a delegated call is named even when the
    spawning ``tool_use`` block never arrived (a truncated stream).

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
        A ``(output, trajectory, tokens, errors)`` tuple. ``trajectory`` is a
        list of ``ToolCall.to_dict()`` mappings ordered as emitted. On a run
        that delegated, every entry additionally carries ``actor`` /
        ``call_id`` / ``parent_id``; on a run that did not, the entries are
        exactly what this parser produced before attribution existed.
    """
    text_parts: list[str] = []
    result_output: str | None = None
    tokens: dict = empty_tokens()
    result_usage_seen = False
    acc_usage: dict = {}
    seen_usage_ids: set[str] = set()
    errors: list[str] = []
    # Each id maps to a FIFO queue of pending calls: distinct tool_use blocks can
    # legitimately reuse an id, so results are matched in emission order rather
    # than the second call silently overwriting the first.
    pending: dict[str, list[ToolCall]] = {}
    trajectory: list[ToolCall] = []
    # Spawning ``call_id`` -> delegate role name. Two maps, by authority: the
    # CLI's own stamp beats the label the model passed as a tool argument.
    stamped_labels: dict[str, str] = {}
    arg_labels: dict[str, str] = {}

    for event, error in _iter_events(stdout):
        if error is not None:
            errors.append(error)
            continue
        if not isinstance(event, dict):
            continue

        # Harvest the CLI's delegate naming before the per-type dispatch: it
        # rides on envelopes of every type (each delegated turn) and on the
        # ``task_*`` lifecycle events, keyed by the spawning call either way.
        # First stamp wins — they agree, and a late degenerate one cannot
        # rewrite an actor already established for the delegation.
        stamped = _label_from(event)
        if stamped is not None:
            for key_field in (_PARENT_ID_FIELD, _TOOL_USE_ID_FIELD):
                key = event.get(key_field)
                if key:
                    stamped_labels.setdefault(str(key), stamped)

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
            # Accumulate per-turn usage so a truncated stream (no terminal
            # ``result`` event) still yields token counts. Claude Code emits one
            # envelope per content block of a single API message, each repeating
            # the identical ``usage``, so count each message id only once.
            msg_id = message.get("id")
            if not (isinstance(msg_id, str) and msg_id in seen_usage_ids):
                if isinstance(msg_id, str):
                    seen_usage_ids.add(msg_id)
                _add_usage(acc_usage, message.get("usage"))
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
                    call_id = block.get("id")
                    parent_id = event.get(_PARENT_ID_FIELD)
                    call = ToolCall(
                        name=_normalize_tool_name(raw_name if isinstance(raw_name, str) else ""),
                        args=args if isinstance(args, dict) else {},
                        status="called",
                        call_id=str(call_id) if call_id else None,
                        parent_id=str(parent_id) if parent_id else None,
                    )
                    trajectory.append(call)
                    if call_id:
                        pending.setdefault(str(call_id), []).append(call)
                        # Lowest-authority naming source: a label the model
                        # itself passed to the spawning tool.
                        arg_label = _label_from(call.args)
                        if arg_label is not None:
                            arg_labels.setdefault(str(call_id), arg_label)
        elif etype == "user":
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                # A user message with a bare-string content echoes the prompt.
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                call_id = block.get("tool_use_id") or ""
                queue = pending.get(str(call_id)) if call_id else None
                target = queue.pop(0) if queue else None
                if target is None:
                    errors.append(
                        f"stream-json tool_result without matching tool_use (id={call_id!r})"
                    )
                    continue
                target.result = _block_text(block.get("content"))
                target.status = "error" if block.get("is_error") else "completed"
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
            subtype = event.get("subtype")
            if isinstance(subtype, str) and subtype.startswith("error_"):
                errors.append(f"stream-json result error: {subtype}")
            elif event.get("is_error"):
                # A failed API call can still carry ``subtype: "success"`` and
                # exit 0 (observed: a 404 model-not-found, whose ``result`` is
                # the provider's error text). Without this the run would score
                # as clean with an empty trajectory and zeroed usage.
                status = event.get("api_error_status")
                detail = f" (api status {status})" if status is not None else ""
                errors.append(f"stream-json result flagged is_error{detail}")

    # ``result_output`` may be an empty string (error subtypes emit ``""``); fall
    # back to the accumulated assistant text so a real partial answer survives.
    output = result_output or "".join(text_parts)
    # Only fall back to summed per-turn usage when the terminal event reported no
    # recognized usage — a terminal event that reported genuine zeros is trusted.
    if not result_usage_seen and acc_usage:
        tokens = _usage_tokens(acc_usage)
    _attribute_actors(trajectory, stamped_labels, arg_labels)
    return output, [call.to_dict() for call in trajectory], tokens, errors


_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def _has_usage(usage: dict) -> bool:
    """True if ``usage`` carries at least one recognized integer count.

    Distinguishes a terminal ``result`` that reported genuine (possibly zero)
    counts from one that reported nothing, so the accumulator fallback only
    fires in the latter case.
    """
    return any(_int_or_none(usage.get(key)) is not None for key in _USAGE_KEYS)


# ``output_tokens`` is deliberately absent from the accumulator. The per-turn
# ``usage`` on an assistant envelope is the streaming ``message_start`` snapshot,
# whose ``output_tokens`` is a placeholder of a few tokens rather than the final
# count (observed: a summed 3 against a terminal 3069). The prompt-side fields do
# accumulate faithfully, so they are kept and ``output`` is left unreported —
# per the bucket contract, an absent number beats an invented one.
_ACC_USAGE_KEYS = tuple(key for key in _USAGE_KEYS if key != "output_tokens")


def _add_usage(acc: dict, usage: object) -> None:
    """Fold an Anthropic per-turn ``usage`` block into a running accumulator.

    Callers dedupe by message id first, so each API message is added once. The
    terminal ``result`` usage is cumulative and authoritative; this accumulator
    is only a best-effort stand-in for a truncated stream that never emits it,
    and covers :data:`_ACC_USAGE_KEYS` only.
    """
    if not isinstance(usage, dict):
        return
    for key in _ACC_USAGE_KEYS:
        val = _int_or_none(usage.get(key))
        if val is not None:
            acc[key] = acc.get(key, 0) + val


def _usage_tokens(usage: dict) -> dict[str, int | None]:
    """Normalize an Anthropic ``usage`` block onto :data:`TOKEN_BUCKETS`.

    ``input_tokens`` is already the uncached prompt; cache reads and writes stay
    separate buckets (writes bill at a premium). Extended thinking is billed
    *inside* ``output_tokens`` and counted again under
    ``output_tokens_details.thinking_tokens``, so it is subtracted back out to
    honour the contract that ``output`` excludes ``reasoning`` while ``total``
    stays exact. Without that details block ``reasoning`` stays ``None`` rather
    than a fabricated ``0``.

    ``total`` is only filled in once ``output`` is known. The accumulator path
    leaves that bucket unreported on purpose (see :data:`_ACC_USAGE_KEYS`), and
    a prompt-side-only sum published under ``total`` reaches the dashboard row
    verbatim — an absent total beats one that undercounts the whole output side.
    """
    tokens = empty_tokens()
    output = _int_or_none(usage.get("output_tokens"))
    details = usage.get("output_tokens_details")
    reasoning = _int_or_none(details.get("thinking_tokens")) if isinstance(details, dict) else None
    if output is not None and reasoning is not None:
        output = max(0, output - reasoning)
    tokens.update(
        input=_int_or_none(usage.get("input_tokens")),
        cached=_int_or_none(usage.get("cache_read_input_tokens")),
        cache_write=_int_or_none(usage.get("cache_creation_input_tokens")),
        reasoning=reasoning,
        output=output,
    )
    if output is not None:
        tokens["total"] = sum(v for k, v in tokens.items() if k != "total" and v is not None)
    return tokens
