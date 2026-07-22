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

"""Parser for Antigravity CLI session JSONL logs.

Reconstructs the final message history by respecting ``$rewindTo`` and ``$set``
control records, then extracts the canonical tool call trajectory, final output,
and aggregated token usage.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3

from devops_bench import core
from devops_bench.agents import result as agents_result

__all__ = ["parse_session_jsonl", "empty_tokens", "db_token_state"]

_log = core.get_logger("agents.cli.antigravity.parsing")


def _extract_text(content: object) -> str:
    """Extract plain text from a message content field."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    return ""


def _parse_tool_result(result_list: list) -> tuple[str, str]:
    """Parse a tool result list into a (result_text, status) tuple."""
    text_parts = []
    status = "completed"

    for item in result_list:
        if not isinstance(item, dict):
            continue

        # Check for functionResponse structure
        func_resp = item.get("functionResponse")
        if isinstance(func_resp, dict):
            response = func_resp.get("response")
            if isinstance(response, dict):
                # Look for explicit output
                output = response.get("output")
                if output is not None:
                    text_parts.append(output if isinstance(output, str) else json.dumps(output))

                # Check for error indicators
                if response.get("error") or response.get("is_error") or response.get("failed"):
                    status = "error"
            else:
                text_parts.append(json.dumps(func_resp))
        else:
            # Fallback for other result shapes
            text_parts.append(json.dumps(item))

    return "\n".join(text_parts), status


def parse_transcript_jsonl(jsonl_text: str) -> tuple[str, list[dict], dict, list[str]]:
    """Parse Antigravity CLI transcript.jsonl into the canonical shape."""
    errors: list[str] = []
    trajectory: list[agents_result.ToolCall] = []
    output_parts: list[str] = []
    aggregated_tokens = {"input": 0, "output": 0, "total": 0, "cached": 0}

    pending_tool_calls: list[agents_result.ToolCall] = []

    for lineno, raw in enumerate(jsonl_text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"transcript line {lineno} parse error: {exc}")
            continue
        if not isinstance(record, dict):
            continue

        stype = record.get("type")
        source = record.get("source")

        # Aggregate tokens if present
        if "tokens" in record and isinstance(record["tokens"], dict):
            t = record["tokens"]
            aggregated_tokens["input"] += t.get("input", 0)
            aggregated_tokens["output"] += t.get("output", 0)
            aggregated_tokens["cached"] += t.get("cached", 0)

        if source == "MODEL":
            if stype == "PLANNER_RESPONSE":
                # If it has content, it's a text response
                if "content" in record and record["content"]:
                    output_parts.append(record["content"])

                # If it has tool calls, queue them
                if "tool_calls" in record and isinstance(record["tool_calls"], list):
                    for tc in record["tool_calls"]:
                        if isinstance(tc, dict):
                            pending_tool_calls.append(
                                agents_result.ToolCall(
                                    name=tc.get("name", ""),
                                    args=tc.get("args") or {},
                                )
                            )
            else:
                # This is a tool execution result! Match it with the first pending call.
                if pending_tool_calls:
                    call = pending_tool_calls.pop(0)
                    call.result = record.get("content") or record.get("error") or ""
                    call.status = "completed" if record.get("status") == "DONE" else "error"
                    trajectory.append(call)
                # else: ignore tool results that don't match any pending call
        elif stype == "ERROR_MESSAGE" and record.get("content"):
            errors.append(f"System error: {record['content']}")

    # If there are still pending tool calls at the end, they were probably interrupted
    for call in pending_tool_calls:
        call.status = "interrupted"
        trajectory.append(call)

    aggregated_tokens["total"] = (
        aggregated_tokens["input"] + aggregated_tokens["output"] + aggregated_tokens["cached"]
    )
    output = "".join(output_parts)
    return output, [call.to_dict() for call in trajectory], aggregated_tokens, errors


def parse_session_jsonl(jsonl_text: str) -> tuple[str, list[dict], dict, list[str]]:
    """Parse Antigravity CLI session JSONL into the canonical shape.

    Automatically detects if the format is the old session log or the new
    transcript log and delegates accordingly.
    """
    if not jsonl_text:
        return "", [], {"input": 0, "output": 0, "total": 0, "cached": 0}, ["Empty session log"]

    # Detect format from the first non-empty line's parsed keys, not a raw
    # substring match (a user message that merely contains the text
    # "step_index" would otherwise be misrouted).
    first_record: dict | None = None
    for line in io.StringIO(jsonl_text):  # lazy: only the first non-empty line is read
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            break
        if isinstance(parsed, dict):
            first_record = parsed
        break

    if first_record is not None and "step_index" in first_record:
        return parse_transcript_jsonl(jsonl_text)

    # Fallback to old parser
    return _parse_old_session_jsonl(jsonl_text)


def _parse_old_session_jsonl(jsonl_text: str) -> tuple[str, list[dict], dict, list[str]]:
    """Old parser for Antigravity CLI session JSONL logs."""
    errors: list[str] = []
    message_ids: list[str] = []
    messages_by_id: dict[str, dict] = {}

    # 1. Reconstruct final history by applying rewinds
    for lineno, raw in enumerate(jsonl_text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"session line {lineno} parse error: {exc}")
            continue
        if not isinstance(record, dict):
            continue

        if "$rewindTo" in record:
            rewind_id = str(record["$rewindTo"])
            if rewind_id in message_ids:
                idx = message_ids.index(rewind_id)
                # Keep everything up to the rewind target, discard the rest
                for removed in message_ids[idx + 1 :]:
                    messages_by_id.pop(removed, None)
                del message_ids[idx + 1 :]
            else:
                # If target not found, clear everything (defensive)
                message_ids.clear()
                messages_by_id.clear()
        elif "$set" in record and isinstance(record["$set"], dict):
            continue  # control record, not a chat message
        elif "id" in record and "type" in record:
            mid = str(record["id"])
            if mid not in messages_by_id:
                message_ids.append(mid)
            messages_by_id[mid] = record
        elif "sessionId" in record:
            continue  # session header, not a chat message

    # 2. Extract trajectory, output, and tokens from the reconstructed messages
    trajectory: list[agents_result.ToolCall] = []
    output_parts: list[str] = []
    aggregated_tokens = {"input": 0, "output": 0, "total": 0, "cached": 0}

    for mid in message_ids:
        msg = messages_by_id[mid]
        mtype = msg.get("type")

        if mtype in ("gemini", "agent", "assistant"):
            # Extract text output
            content_text = _extract_text(msg.get("content", ""))
            if content_text:
                output_parts.append(content_text)

            # Extract tool calls
            tool_calls_data = msg.get("toolCalls")
            if isinstance(tool_calls_data, list):
                for tc in tool_calls_data:
                    if not isinstance(tc, dict):
                        continue

                    name = tc.get("name", "")
                    args = tc.get("args") or tc.get("arguments") or {}

                    # Parse result and status
                    result_text = None
                    status = "called"
                    result_data = tc.get("result")

                    if isinstance(result_data, list):
                        result_text, status = _parse_tool_result(result_data)
                    elif result_data is not None:
                        result_text = (
                            result_data if isinstance(result_data, str) else json.dumps(result_data)
                        )
                        status = "completed"

                    call = agents_result.ToolCall(
                        name=name,
                        args=args if isinstance(args, dict) else {},
                        result=result_text,
                        status=status,
                    )
                    trajectory.append(call)

            # Aggregate tokens
            msg_tokens = msg.get("tokens")
            if isinstance(msg_tokens, dict):
                aggregated_tokens["input"] += msg_tokens.get("input", 0)
                output_cnt = msg_tokens.get("output", 0)
                thoughts_cnt = msg_tokens.get("thoughts", 0)
                tool_cnt = msg_tokens.get("tool", 0)
                aggregated_tokens["output"] += output_cnt + thoughts_cnt + tool_cnt
                aggregated_tokens["cached"] += msg_tokens.get("cached", 0)

    aggregated_tokens["total"] = (
        aggregated_tokens["input"] + aggregated_tokens["output"] + aggregated_tokens["cached"]
    )

    output = "".join(output_parts)
    return output, [call.to_dict() for call in trajectory], aggregated_tokens, errors


# Harness-local until the unified token schema lands: input = non-cached prompt,
# cached = cache reads, output excludes reasoning, total = sum of all buckets.
_TOKEN_BUCKETS = ("input", "cached", "cache_write", "reasoning", "output", "total")


def empty_tokens() -> dict:
    """Return the canonical token dict with every bucket ``None`` (unavailable)."""
    return dict.fromkeys(_TOKEN_BUCKETS, None)


# --- Token usage from the conversation DB ----------------------------------
#
# agy persists a per-turn usage record in the conversation DB
# (``gen_metadata.data`` blob) at protobuf wire path ``.1.4``:
#
#   f2 -> input (non-cached), f5 -> cached, f9 -> reasoning, f10 -> output,
#   f3 == f9 + f10 (integrity guard; there is no cache-write field).
#
# The format is private and version-sensitive: reads are guarded by the f3
# invariant and yield all-``None`` on mismatch (never a fake 0). Side-call usage
# (e.g. title generation) is not stored here, so totals cover the main
# trajectory only (~0.2% low observed).

# Tables agy creates before the usage flush lands; their presence distinguishes
# "flush pending, retry" from "not an agy DB / nothing to wait for".
_AGY_DB_TABLES = frozenset({"trajectory_meta", "steps"})


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    """Decode a base-128 varint at ``buf[i:]``; return ``(value, next_index)``."""
    shift = 0
    result = 0
    while i < len(buf):
        byte = buf[i]
        result |= (byte & 0x7F) << shift
        i += 1
        if not byte & 0x80:
            return result, i
        shift += 7
        if shift >= 64:
            raise ValueError("varint too long")
    raise ValueError("truncated varint")


def _parse_message(buf: bytes) -> list[tuple[int, int, object]] | None:
    """Parse protobuf wire bytes into ``(field_num, wire_type, value)`` triples.

    Returns ``None`` when ``buf`` is not a clean protobuf message (e.g. it is a
    UTF-8 string rather than a sub-message), so callers can safely probe any
    length-delimited field without misparsing text.
    """
    fields: list[tuple[int, int, object]] = []
    i, n = 0, len(buf)
    try:
        while i < n:
            key, i = _read_varint(buf, i)
            field_num, wire = key >> 3, key & 7
            if wire == 0:  # varint
                val, i = _read_varint(buf, i)
                fields.append((field_num, 0, val))
            elif wire == 2:  # length-delimited
                ln, i = _read_varint(buf, i)
                if i + ln > n:
                    return None
                fields.append((field_num, 2, buf[i : i + ln]))
                i += ln
            elif wire == 5:  # 32-bit: bounds-check and skip (no consumer)
                if i + 4 > n:
                    return None
                i += 4
            elif wire == 1:  # 64-bit: bounds-check and skip (no consumer)
                if i + 8 > n:
                    return None
                i += 8
            else:  # groups (3/4) / unknown: not a message we understand
                return None
    except ValueError:
        return None
    return fields


def _collect_usage(buf: bytes, out: list[dict], depth: int = 0) -> None:
    """Recursively collect usage records (``f3 == f9 + f10``)."""
    if depth > 12:
        return
    fields = _parse_message(buf)
    if fields is None:
        return
    v = {fn: val for fn, wt, val in fields if wt == 0}
    # proto3 omits zero-valued scalars: a fully-cached turn omits f2 and a
    # thinking-only turn omits f10, so require f3 plus at least one of f9/f10.
    # f3 > 0 keeps config-shaped noise records (f3=0, no f9/f10) from matching.
    if 3 in v and v[3] > 0 and (9 in v or 10 in v) and v[3] == v.get(9, 0) + v.get(10, 0):
        out.append(
            {
                "input": v.get(2, 0),
                "cached": v.get(5, 0),
                "reasoning": v.get(9, 0),
                "output": v.get(10, 0),
            }
        )
    for _fn, wire, val in fields:
        if wire == 2 and isinstance(val, bytes | bytearray) and len(val) >= 2:
            _collect_usage(val, out, depth + 1)


def _turn_usage(blob: bytes) -> dict | None:
    """Return the main-generation usage record for one blob.

    The record is mirrored at two wire paths with identical values; picking the
    largest-total record dedups the mirrors and keeps any smaller side-call
    record from shadowing the real turn.
    """
    records: list[dict] = []
    _collect_usage(bytes(blob), records)
    if not records:
        return None
    return max(records, key=lambda r: r["input"] + r["cached"] + r["reasoning"] + r["output"])


def db_token_state(db_path: str | os.PathLike[str]) -> tuple[str, dict | None]:
    """Read canonical token usage from an ``agy`` conversation DB.

    Returns:
        A ``(state, tokens)`` tuple so the caller can handle the async flush:

        * ``("ready", tokens)`` — usage decoded; ``tokens`` is the canonical dict.
        * ``("pending", None)`` — an ``agy`` DB whose usage rows have not flushed
          yet (retry after a short wait).
        * ``("undecodable", None)`` — usage rows exist but none matches the
          expected layout (schema drift; retrying will not help).
        * ``("absent", None)`` — no DB, not an ``agy`` DB, or unreadable.

    Buckets are summed per-turn across ``gen_metadata``. ``cached`` is a genuine
    ``0`` when no turn hit the cache (protobuf omits the field when zero);
    ``cache_write`` is always ``None``. ``total`` is the full footprint
    (input + cached + reasoning + output), the same quantity as Gemini's
    provider ``total_tokens``.
    """
    if not db_path or not os.path.exists(db_path):
        return "absent", None
    # The DB is read during agy's post-exit flush window, so a read can hit a
    # locked/half-written image (OperationalError/DatabaseError). Those are
    # transient -> "pending" so the poll retries; only genuinely unusable DBs
    # (e.g. InterfaceError) are terminal "absent".
    try:
        con = sqlite3.connect(f"file:{os.fspath(db_path)}?mode=ro", uri=True)
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        return "pending", None
    except sqlite3.Error:
        return "absent", None

    totals = {"input": 0, "cached": 0, "reasoning": 0, "output": 0}
    saw_row = False
    seen_turn = False
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "gen_metadata" not in tables:
            return ("pending", None) if tables & _AGY_DB_TABLES else ("absent", None)
        # Stream the cursor: peak memory is one blob, not the whole turn history.
        for (blob,) in con.execute("SELECT data FROM gen_metadata ORDER BY idx"):
            saw_row = True
            if not isinstance(blob, bytes | bytearray):
                continue
            usage = _turn_usage(blob)
            if usage is None:
                continue
            seen_turn = True
            for key in totals:
                totals[key] += usage[key]
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        return "pending", None
    except sqlite3.Error:
        return "absent", None
    finally:
        con.close()

    if not saw_row:
        return "pending", None  # table created but rows not flushed yet
    if not seen_turn:
        return "undecodable", None  # rows flushed, but no usage record matched
    tokens = empty_tokens()
    tokens.update(
        input=totals["input"],
        cached=totals["cached"],
        reasoning=totals["reasoning"],
        output=totals["output"],
        total=totals["input"] + totals["cached"] + totals["reasoning"] + totals["output"],
    )
    return "ready", tokens
