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

"""Unit tests for devops_bench.agents.cli.claude_code."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from devops_bench.agents import AGENTS, AgentConfig
from devops_bench.agents.capabilities import (
    AgentRules,
    AllCapabilities,
    McpBinding,
    SkillBinding,
    SupportsMcp,
    SupportsRules,
    SupportsSkills,
)
from devops_bench.agents.cli.claude_code import ClaudeCodeAgent, parse_stream_json
from devops_bench.agents.cli.claude_code import agent as claude_mod
from devops_bench.agents.cli.claude_code.agent import _build_argv, _build_env
from devops_bench.agents.cli.claude_code.parsing import StreamParse
from devops_bench.agents.result import TOKEN_BUCKETS, empty_tokens
from devops_bench.core.errors import ConfigError, SubprocessError
from devops_bench.results.normalize import normalize_tokens


@pytest.fixture(autouse=True)
def _reset_version_cache() -> Iterator[None]:
    """``_claude_version`` is cached per target, and every test here drives the
    same ``"claude"`` target through a different probe double."""
    claude_mod._claude_version.cache_clear()  # noqa: SLF001 - cache under test control
    yield
    claude_mod._claude_version.cache_clear()  # noqa: SLF001 - cache under test control


def _stream(*events: dict) -> str:
    """Render a list of events as a stream-json stdout blob."""
    return "\n".join(json.dumps(event) for event in events) + "\n"


def _assistant(
    *blocks: dict,
    msg_id: str | None = None,
    usage: dict | None = None,
    model: str | None = None,
    ts: str | None = None,
) -> dict:
    message: dict = {"content": list(blocks)}
    if msg_id is not None:
        message["id"] = msg_id
    if usage is not None:
        message["usage"] = usage
    if model is not None:
        message["model"] = model
    event: dict = {"type": "assistant", "message": message}
    if ts is not None:
        event["timestamp"] = ts
    return event


def _user(*blocks: dict, ts: str | None = None) -> dict:
    event: dict = {"type": "user", "message": {"content": list(blocks)}}
    if ts is not None:
        event["timestamp"] = ts
    return event


SAMPLE_STREAM = _stream(
    {"type": "system", "subtype": "init", "session_id": "abc-123", "model": "claude-opus-4-8"},
    _assistant(
        {
            "type": "tool_use",
            "id": "call-1",
            "name": "mcp__gke__list_clusters",
            "input": {"project": "p1"},
        }
    ),
    _user({"type": "tool_result", "tool_use_id": "call-1", "content": "cluster-a, cluster-b"}),
    _assistant(
        {"type": "tool_use", "id": "call-2", "name": "mcp__gke__get_cluster", "input": {"c": "a"}}
    ),
    _user(
        {
            "type": "tool_result",
            "tool_use_id": "call-2",
            "content": [{"type": "text", "text": "v1.30"}],
            "is_error": False,
        }
    ),
    _assistant({"type": "text", "text": "Done."}),
    {
        "type": "result",
        "subtype": "success",
        "result": "Done.",
        "usage": {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 5},
    },
)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _tok(**overrides: int | None) -> dict[str, int | None]:
    """Canonical token dict with every bucket None, overridden per test.

    Built from the shared :func:`empty_tokens` rather than a local literal, so a
    bucket added to :data:`TOKEN_BUCKETS` shows up in every token assertion here
    instead of being silently absent.
    """
    base = empty_tokens()
    base.update(overrides)
    return base


def test_parser_fills_the_shared_canonical_buckets() -> None:
    """Guards against ``TOKEN_BUCKETS`` drifting away from what this parser emits."""
    parsed = parse_stream_json(SAMPLE_STREAM)

    assert set(parsed.tokens) == set(TOKEN_BUCKETS)


def test_tokens_reach_the_result_row_unchanged() -> None:
    """Cross-layer guard: the emitted keys are the ones ``normalize_tokens`` reads.

    A bucket named differently here would silently normalize to ``None`` on the
    dashboard row rather than failing loudly.
    """
    blob = _stream(
        {
            "type": "result",
            "result": "ok",
            "usage": {
                "input_tokens": 2748,
                "output_tokens": 11267,
                "cache_read_input_tokens": 334987,
                "cache_creation_input_tokens": 12000,
            },
        }
    )

    parsed = parse_stream_json(blob)
    normalized = normalize_tokens(parsed.tokens)

    assert normalized.input == 2748
    assert normalized.output == 11267
    assert normalized.cached == 334987
    assert normalized.cache_write == 12000
    assert normalized.reasoning is None
    assert normalized.total == 2748 + 11267 + 334987 + 12000


def test_thinking_tokens_split_out_of_output() -> None:
    """Extended thinking is billed inside ``output_tokens`` and reported again
    under ``output_tokens_details``. The canonical contract says ``output``
    excludes ``reasoning``, so it is subtracted back out — leaving ``total``
    (which sums every bucket) equal to the provider's own accounting."""
    blob = _stream(
        {
            "type": "result",
            "result": "ok",
            "usage": {
                "input_tokens": 4,
                "output_tokens": 3069,
                "cache_read_input_tokens": 13445,
                "cache_creation_input_tokens": 31257,
                "output_tokens_details": {"thinking_tokens": 2778},
            },
        }
    )

    parsed = parse_stream_json(blob)

    assert parsed.tokens == _tok(
        input=4,
        cached=13445,
        cache_write=31257,
        reasoning=2778,
        output=3069 - 2778,
        total=4 + 13445 + 31257 + 3069,
    )


def test_thinking_tokens_absent_leaves_reasoning_unreported() -> None:
    """No ``output_tokens_details`` means nothing to split: ``reasoning`` stays
    ``None`` rather than a fabricated ``0``, and ``output`` is untouched."""
    blob = _stream(
        {"type": "result", "result": "ok", "usage": {"input_tokens": 4, "output_tokens": 78}}
    )
    parsed = parse_stream_json(blob)
    assert parsed.tokens == _tok(input=4, reasoning=None, output=78, total=82)


def test_thinking_tokens_exceeding_output_clamp_at_zero() -> None:
    """A provider quirk must not produce a negative bucket."""
    blob = _stream(
        {
            "type": "result",
            "result": "ok",
            "usage": {"output_tokens": 5, "output_tokens_details": {"thinking_tokens": 9}},
        }
    )
    parsed = parse_stream_json(blob)
    assert parsed.tokens["output"] == 0
    assert parsed.tokens["reasoning"] == 9


def test_parse_stream_json_emits_canonical_trajectory() -> None:
    parsed = parse_stream_json(SAMPLE_STREAM)
    assert parsed.output == "Done."
    assert parsed.errors == []
    assert parsed.tokens == _tok(input=10, cached=5, output=20, total=35)
    assert parsed.trajectory == [
        {
            "name": "gke__list_clusters",
            "args": {"project": "p1"},
            "result": "cluster-a, cluster-b",
            "status": "completed",
        },
        {
            "name": "gke__get_cluster",
            "args": {"c": "a"},
            "result": "v1.30",
            "status": "completed",
        },
    ]


def test_parse_stream_json_marks_failed_tool_results_as_error_status() -> None:
    blob = _stream(
        _assistant({"type": "tool_use", "id": "c", "name": "x", "input": {}}),
        _user({"type": "tool_result", "tool_use_id": "c", "content": "oops", "is_error": True}),
    )
    parsed = parse_stream_json(blob)
    assert parsed.errors == []
    assert parsed.trajectory[0]["status"] == "error"


def test_parse_stream_json_records_unmatched_tool_results() -> None:
    blob = _stream(_user({"type": "tool_result", "tool_use_id": "ghost", "content": "?"}))
    parsed = parse_stream_json(blob)
    assert parsed.trajectory == []
    assert any("without matching tool_use" in msg for msg in parsed.errors)


def test_parse_stream_json_records_json_decode_errors_on_errors_list() -> None:
    blob = "{not json}\n" + json.dumps({"type": "result", "result": "ok"}) + "\n"
    parsed = parse_stream_json(blob)
    assert parsed.output == "ok"
    assert parsed.trajectory == []
    assert len(parsed.errors) == 1
    assert "parse error" in parsed.errors[0]


def test_parse_stream_json_records_error_result_subtype() -> None:
    blob = _stream({"type": "result", "subtype": "error_max_turns", "result": ""})
    parsed = parse_stream_json(blob)
    assert parsed.errors == ["stream-json result error: error_max_turns"]


def test_parse_stream_json_records_is_error_under_a_success_subtype() -> None:
    """An API failure can exit 0 under ``subtype: "success"`` with ``is_error``
    set (observed: a 404 model-not-found), which must not score as a clean run."""
    blob = _stream(
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "api_error_status": 404,
            "result": "The model x is not available on your vertex deployment.",
        }
    )
    parsed = parse_stream_json(blob)
    assert parsed.output == "The model x is not available on your vertex deployment."
    assert parsed.errors == ["stream-json result flagged is_error (api status 404)"]


def test_parse_stream_json_records_is_error_without_an_api_status() -> None:
    blob = _stream({"type": "result", "subtype": "success", "is_error": True, "result": "nope"})
    parsed = parse_stream_json(blob)
    assert parsed.errors == ["stream-json result flagged is_error"]


def test_parse_stream_json_does_not_double_report_an_error_subtype() -> None:
    """``error_*`` subtypes also carry ``is_error``; one error line, not two."""
    blob = _stream({"type": "result", "subtype": "error_max_turns", "is_error": True, "result": ""})
    parsed = parse_stream_json(blob)
    assert parsed.errors == ["stream-json result error: error_max_turns"]


def test_parse_stream_json_ignores_a_false_is_error() -> None:
    blob = _stream({"type": "result", "subtype": "success", "is_error": False, "result": "ok"})
    parsed = parse_stream_json(blob)
    assert parsed.errors == []


def test_parse_stream_json_empty_input_returns_empty() -> None:
    assert parse_stream_json("") == StreamParse(
        output="",
        trajectory=[],
        tokens=_tok(),
        errors=[],
        terminal_reason="",
        model_turns=None,
        tool_wait_sec=None,
        served_models=[],
    )


def test_parse_stream_json_flags_failed_mcp_server_at_init() -> None:
    """A failed MCP server in the init event surfaces on ``errors`` — a
    tool-less-but-exit-0 run must not look clean."""
    blob = _stream(
        {
            "type": "system",
            "subtype": "init",
            "mcp_servers": [
                {"name": "gke", "status": "connected"},
                {"name": "broken", "status": "failed"},
            ],
        },
        {"type": "result", "subtype": "success", "result": "ok"},
    )
    parsed = parse_stream_json(blob)
    assert parsed.output == "ok"
    assert any("broken" in e and "failed" in e for e in parsed.errors)
    assert not any("gke" in e for e in parsed.errors)


def test_parse_stream_json_ignores_transient_pending_mcp_status_at_init() -> None:
    """A ``pending`` MCP status at the init snapshot is transient — the server
    (e.g. gke-mcp) connects moments later and serves tools normally — so it must
    NOT surface an error, which would otherwise flip the run-level ``validated``
    gate to ``False`` on a fully working MCP run."""
    blob = _stream(
        {
            "type": "system",
            "subtype": "init",
            "mcp_servers": [{"name": "gke", "status": "pending"}],
        },
        _assistant(
            {"type": "tool_use", "id": "t1", "name": "mcp__gke__list_clusters", "input": {}}
        ),
        _user({"type": "tool_result", "tool_use_id": "t1", "content": "cluster-a"}),
        {"type": "result", "subtype": "success", "result": "ok"},
    )
    parsed = parse_stream_json(blob)
    assert parsed.output == "ok"
    assert parsed.errors == []
    assert parsed.trajectory[0]["status"] == "completed"


def test_parse_stream_json_strips_mcp_client_prefix_from_tool_names() -> None:
    """Claude Code names MCP tools ``mcp__<server>__<tool>``; the parser drops the
    literal ``mcp__`` prefix so names match the pipeline's ``<server>__<tool>``
    convention (which the metrics canonicalizer reduces to the bare tool name).
    Built-in tools keep their names."""
    blob = _stream(
        _assistant(
            {"type": "tool_use", "id": "a", "name": "mcp__default__generate_manifest", "input": {}},
            {"type": "tool_use", "id": "b", "name": "Bash", "input": {"command": "ls"}},
        ),
    )
    parsed = parse_stream_json(blob)
    assert [t["name"] for t in parsed.trajectory] == ["default__generate_manifest", "Bash"]


def test_parse_stream_json_drops_thinking_blocks() -> None:
    """``thinking`` / ``redacted_thinking`` blocks are dropped so the trajectory
    is tool-calls-only, matching the shape the other CLI harnesses emit. Only the
    tool call survives; the surrounding reasoning leaves no trajectory step."""
    blob = _stream(
        _assistant(
            {"type": "thinking", "thinking": "let me plan", "signature": "sig"},
            {"type": "tool_use", "id": "c1", "name": "Bash", "input": {"command": "ls"}},
            {"type": "redacted_thinking", "data": "enc"},
        ),
        _user({"type": "tool_result", "tool_use_id": "c1", "content": "ok"}),
    )
    parsed = parse_stream_json(blob)
    assert parsed.errors == []
    assert parsed.trajectory == [
        {"name": "Bash", "args": {"command": "ls"}, "result": "ok", "status": "completed"},
    ]


def test_parse_stream_json_keeps_pending_tool_use_as_called() -> None:
    """A tool_use with no matching tool_result (timeout-truncated stream) stays
    in the trajectory with status ``called`` and a ``None`` result."""
    blob = _stream(_assistant({"type": "tool_use", "id": "c1", "name": "do", "input": {"k": "v"}}))
    parsed = parse_stream_json(blob)
    assert parsed.trajectory == [
        {"name": "do", "args": {"k": "v"}, "result": None, "status": "called"}
    ]
    assert parsed.errors == []


def test_parse_stream_json_falls_back_to_assistant_text_without_result_event() -> None:
    """A truncated stream (no terminal ``result``) still yields the answer from
    the accumulated assistant ``text`` blocks."""
    blob = _stream(
        _assistant({"type": "text", "text": "partial "}),
        _assistant({"type": "text", "text": "answer"}),
    )
    parsed = parse_stream_json(blob)
    assert parsed.output == "partial answer"
    assert parsed.tokens == _tok()
    assert parsed.errors == []


def test_parse_stream_json_falls_back_to_accumulated_usage_without_result_event() -> None:
    """A truncated stream (no terminal ``result``) still yields prompt-side token
    counts, summed from the per-turn assistant ``usage``. ``output`` stays
    unreported — see the next test — so ``total`` stays unreported with it."""
    blob = _stream(
        _assistant(
            {"type": "text", "text": "a"},
            usage={"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 2},
        ),
        _assistant(
            {"type": "text", "text": "b"},
            usage={"input_tokens": 20, "output_tokens": 7, "cache_creation_input_tokens": 3},
        ),
    )
    parsed = parse_stream_json(blob)
    assert parsed.output == "ab"
    assert parsed.tokens == _tok(input=30, cached=2, cache_write=3)
    assert parsed.errors == []


def test_parse_stream_json_accumulator_leaves_output_unreported() -> None:
    """Per-turn ``usage.output_tokens`` is the ``message_start`` placeholder — a
    handful of tokens against a real terminal count in the thousands. Summing it
    would persist an invented number, so the bucket is left ``None``. ``total``
    follows it: a prompt-side-only sum reaches the dashboard row as the run's
    total, understating it by the whole output side."""
    blob = _stream(
        _assistant(
            {"type": "text", "text": "..."},
            msg_id="msg_1",
            usage={"input_tokens": 4, "output_tokens": 3},
        ),
    )
    parsed = parse_stream_json(blob)
    assert parsed.tokens["output"] is None
    assert parsed.tokens["total"] is None
    assert parsed.tokens == _tok(input=4)


def test_parse_stream_json_result_usage_wins_over_accumulated() -> None:
    """When the terminal ``result`` carries usage it is authoritative — the
    accumulated per-turn usage is not added on top."""
    blob = _stream(
        _assistant(
            {"type": "text", "text": "x"}, usage={"input_tokens": 999, "output_tokens": 999}
        ),
        {
            "type": "result",
            "subtype": "success",
            "result": "x",
            "usage": {"input_tokens": 10, "output_tokens": 20},
        },
    )
    parsed = parse_stream_json(blob)
    assert parsed.tokens == _tok(input=10, output=20, total=30)


def test_parse_stream_json_falls_back_when_result_usage_degenerate() -> None:
    """A terminal ``result`` whose ``usage`` carries no recognized counts must
    not shadow the accumulated per-turn usage — the all-None result is treated
    as absent so the summed per-turn counts survive."""
    blob = _stream(
        _assistant({"type": "text", "text": "x"}, usage={"input_tokens": 15, "output_tokens": 4}),
        {"type": "result", "subtype": "success", "result": "x", "usage": {}},
    )
    parsed = parse_stream_json(blob)
    assert parsed.tokens == _tok(input=15)


def test_parse_stream_json_result_string_is_authoritative_over_text() -> None:
    """When a ``result`` event is present its string wins over assistant text
    (which merely duplicates it) — no double-counting."""
    blob = _stream(
        _assistant({"type": "text", "text": "Done."}),
        {"type": "result", "subtype": "success", "result": "Done."},
    )
    parsed = parse_stream_json(blob)
    assert parsed.output == "Done."


def test_parse_stream_json_dedupes_accumulated_usage_by_message_id() -> None:
    """Claude Code emits one envelope per content block of a single API message,
    each repeating the identical ``usage``; a truncated stream must count that
    message's usage once, not once per block."""
    usage = {"input_tokens": 100, "output_tokens": 40, "cache_read_input_tokens": 8}
    blob = _stream(
        _assistant({"type": "thinking"}, msg_id="msg_1", usage=usage),
        _assistant({"type": "text", "text": "hi"}, msg_id="msg_1", usage=usage),
        _assistant(
            {"type": "tool_use", "id": "t", "name": "Bash", "input": {}},
            msg_id="msg_1",
            usage=usage,
        ),
    )
    parsed = parse_stream_json(blob)
    assert parsed.tokens == _tok(input=100, cached=8)


def test_parse_stream_json_empty_result_falls_back_to_text() -> None:
    """An error subtype emits ``result: ""``; the real partial answer in the
    accumulated assistant text must survive rather than grading as empty."""
    blob = _stream(
        _assistant({"type": "text", "text": "partial answer"}),
        {"type": "result", "subtype": "error_max_turns", "result": ""},
    )
    parsed = parse_stream_json(blob)
    assert parsed.output == "partial answer"
    assert any("error_max_turns" in e for e in parsed.errors)


def test_parse_stream_json_all_zero_result_usage_is_authoritative() -> None:
    """A terminal ``result`` reporting genuine zeros is trusted — it is not
    conflated with 'no usage reported' and replaced by the accumulator."""
    blob = _stream(
        _assistant({"type": "text", "text": "x"}, usage={"input_tokens": 50}),
        {
            "type": "result",
            "subtype": "success",
            "result": "x",
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    )
    parsed = parse_stream_json(blob)
    assert parsed.tokens == _tok(input=0, output=0, total=0)


def test_parse_stream_json_degenerate_second_result_does_not_clobber() -> None:
    """A later degenerate ``result`` (empty answer, no usage) must not destroy a
    good earlier one."""
    blob = _stream(
        {
            "type": "result",
            "subtype": "success",
            "result": "first",
            "usage": {"input_tokens": 10, "output_tokens": 20},
        },
        {"type": "result", "subtype": "error_during_execution", "result": "", "usage": {}},
    )
    parsed = parse_stream_json(blob)
    assert parsed.output == "first"
    assert parsed.tokens == _tok(input=10, output=20, total=30)


def test_parse_stream_json_recovers_concatenated_objects_on_one_line() -> None:
    """A rebuffered stream that concatenates objects onto one physical line must
    not lose the whole run to a single 'Extra data' error."""
    line = json.dumps(_assistant({"type": "text", "text": "hi"})) + json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "result": "hi",
            "usage": {"input_tokens": 3, "output_tokens": 1},
        }
    )
    parsed = parse_stream_json(line + "\n")
    assert parsed.output == "hi"
    assert parsed.tokens == _tok(input=3, output=1, total=4)
    assert parsed.errors == []


def test_parse_stream_json_survives_unescaped_unicode_line_breaks() -> None:
    """The CLI leaves U+0085 (NEL) unescaped inside JSON strings — real command
    output carries it when a log is mis-decoded as latin-1. Framing must key on
    ``\\n`` alone; ``str.splitlines`` would shred that event into undecodable
    fragments, losing the tool call and injecting bogus errors that flip the run
    to unvalidated.
    """
    body = "line\u0085next"
    events = (
        _assistant({"type": "tool_use", "id": "t1", "name": "Bash", "input": {"cmd": "cat log"}}),
        _user({"type": "tool_result", "tool_use_id": "t1", "content": body}),
        {"type": "result", "subtype": "success", "result": "done"},
    )
    # ensure_ascii=False mirrors Node's JSON.stringify, which leaves U+0085 raw.
    blob = "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n"
    assert "\u0085" in blob  # guard: the fixture must carry the raw character

    parsed = parse_stream_json(blob)
    assert parsed.errors == []
    assert parsed.output == "done"
    assert len(parsed.trajectory) == 1
    assert parsed.trajectory[0]["status"] == "completed"
    assert parsed.trajectory[0]["result"] == body


def test_parse_stream_json_clips_oversized_tool_results() -> None:
    """Claude Code echoes the full body of every tool result. The trajectory is
    re-serialized into the LLM judge prompts, so an uncapped result overruns the
    judge's context — keep the head and tail with the middle marked elided."""
    payload = "A" * 20_000 + "TAIL"
    blob = _stream(
        _assistant({"type": "tool_use", "id": "t1", "name": "Read", "input": {}}),
        _user({"type": "tool_result", "tool_use_id": "t1", "content": payload}),
    )
    parsed = parse_stream_json(blob)
    result = parsed.trajectory[0]["result"]
    assert parsed.errors == []
    assert len(result) < len(payload)
    assert result.startswith("A" * 100)
    assert result.endswith("TAIL")
    assert "chars elided" in result


def test_parse_stream_json_keeps_tool_results_under_the_cap_verbatim() -> None:
    """The clip must not touch an ordinary result."""
    payload = "B" * 500
    blob = _stream(
        _assistant({"type": "tool_use", "id": "t1", "name": "Read", "input": {}}),
        _user({"type": "tool_result", "tool_use_id": "t1", "content": payload}),
    )
    parsed = parse_stream_json(blob)
    assert parsed.trajectory[0]["result"] == payload


def test_parse_stream_json_non_dict_message_does_not_crash() -> None:
    """A malformed line whose ``message`` is a bare string is skipped, not fatal
    — one bad envelope must not lose the whole otherwise-parseable run."""
    blob = _stream(
        {"type": "assistant", "message": "Execution error"},
        {"type": "user", "message": "echo"},
        {
            "type": "result",
            "subtype": "success",
            "result": "ok",
            "usage": {"input_tokens": 5, "output_tokens": 2},
        },
    )
    parsed = parse_stream_json(blob)
    assert parsed.output == "ok"
    assert parsed.tokens == _tok(input=5, output=2, total=7)


def test_parse_stream_json_rejects_bool_usage_values() -> None:
    """A stray JSON ``true`` in a usage field is rejected, never summed as 1."""
    blob = _stream(
        {
            "type": "result",
            "subtype": "success",
            "result": "x",
            "usage": {"input_tokens": True, "output_tokens": 20},
        },
    )
    parsed = parse_stream_json(blob)
    assert parsed.tokens == _tok(input=None, output=20, total=20)


def test_parse_stream_json_matches_duplicate_tool_use_ids_fifo() -> None:
    """Distinct tool_use blocks reusing an id are matched in emission order, so
    neither call is orphaned and no spurious 'unmatched' error is raised."""
    blob = _stream(
        _assistant({"type": "tool_use", "id": "x", "name": "A", "input": {}}),
        _assistant({"type": "tool_use", "id": "x", "name": "B", "input": {}}),
        _user({"type": "tool_result", "tool_use_id": "x", "content": "r1"}),
        _user({"type": "tool_result", "tool_use_id": "x", "content": "r2"}),
    )
    parsed = parse_stream_json(blob)
    assert [(t["name"], t["result"]) for t in parsed.trajectory] == [("A", "r1"), ("B", "r2")]
    assert parsed.errors == []


def test_block_text_preserves_non_text_blocks() -> None:
    """A mixed tool_result content list must not silently drop non-text blocks
    (e.g. an image) — they are JSON-encoded in place."""
    blob = _stream(
        _assistant({"type": "tool_use", "id": "c", "name": "Read", "input": {}}),
        _user(
            {
                "type": "tool_result",
                "tool_use_id": "c",
                "content": [{"type": "image", "source": {"x": 1}}, {"type": "text", "text": "ok"}],
            }
        ),
    )
    parsed = parse_stream_json(blob)
    assert parsed.trajectory[0]["result"] == '{"type": "image", "source": {"x": 1}}ok'


# ---------------------------------------------------------------------------
# Per-run telemetry: terminal_reason, model_turns, tool_wait_sec, served_models
# ---------------------------------------------------------------------------


def test_parse_stream_json_maps_a_completed_run_to_completed() -> None:
    blob = _stream(
        {"type": "result", "subtype": "success", "terminal_reason": "completed", "result": "ok"}
    )
    assert parse_stream_json(blob).terminal_reason == "completed"


def test_parse_stream_json_buckets_a_cli_specific_reason_as_error() -> None:
    """The CLI's reason vocabulary is far wider than the bench's four values, so
    a failure reason buckets to ``error`` — with the specific reason kept on
    ``errors`` rather than dropped."""
    blob = _stream(
        {
            "type": "result",
            "subtype": "error_during_execution",
            "terminal_reason": "model_error",
            "result": "",
        }
    )
    parsed = parse_stream_json(blob)
    assert parsed.terminal_reason == "error"
    assert parsed.errors == ["stream-json result terminal_reason: model_error"]


def test_parse_stream_json_scores_a_turn_capped_run_as_completed() -> None:
    """``TERMINAL_REASONS`` puts a turn cap in ``completed`` so an efficiency
    ceiling does not read as a capability failure, and the sibling harnesses
    land there because their cap is invisible from outside the process. Claude
    Code can see its cap, so it has to be mapped there deliberately or the two
    harnesses report opposite values for the same event."""
    blob = _stream(
        {
            "type": "result",
            "subtype": "error_max_turns",
            "terminal_reason": "max_turns",
            "is_error": True,
            "result": "",
        }
    )
    parsed = parse_stream_json(blob)
    assert parsed.terminal_reason == "completed"
    # The cap itself is not lost — it just is not a failure.
    assert parsed.errors == ["stream-json result terminal_reason: max_turns"]


def test_parse_stream_json_scores_a_turn_cap_as_completed_without_a_cli_reason() -> None:
    """A binary predating ``terminal_reason`` shows the cap only in the subtype;
    it must not land in a different bucket than the same run on a newer one."""
    blob = _stream({"type": "result", "subtype": "error_max_turns", "result": ""})
    parsed = parse_stream_json(blob)
    assert parsed.terminal_reason == "completed"
    assert parsed.errors == ["stream-json result error: error_max_turns"]


def test_parse_stream_json_keeps_the_cli_reason_when_a_failure_flag_also_fired() -> None:
    """Every reason the CLI itself classifies as an error (``api_error``,
    ``prompt_too_long``, ``blocking_limit``) arrives as ``subtype: "success"``
    with ``is_error`` set, so a mapping that only reads the flags would record
    an anonymous failure for the whole reachable failure vocabulary."""
    blob = _stream(
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "api_error_status": 404,
            "terminal_reason": "api_error",
            "result": "The model x is not available.",
        }
    )
    parsed = parse_stream_json(blob)
    assert parsed.terminal_reason == "error"
    assert parsed.errors == ["stream-json result terminal_reason: api_error (api status 404)"]


def test_parse_stream_json_buckets_a_stopped_short_reason_without_failure_flags() -> None:
    """A loop the CLI does not itself flag (``hook_stopped``, ``aborted_tools``)
    sets neither ``is_error`` nor an ``error_*`` subtype; the reason alone must
    still mark the run as failed."""
    blob = _stream(
        {
            "type": "result",
            "subtype": "success",
            "terminal_reason": "hook_stopped",
            "result": "partial",
        }
    )
    parsed = parse_stream_json(blob)
    assert parsed.terminal_reason == "error"
    assert parsed.errors == ["stream-json result terminal_reason: hook_stopped"]


def test_parse_stream_json_resolves_reason_from_flags_when_the_cli_omits_it() -> None:
    """``terminal_reason`` is optional on the event; older binaries omit it."""
    ok = _stream({"type": "result", "subtype": "success", "result": "ok"})
    bad = _stream({"type": "result", "subtype": "success", "is_error": True, "result": "no"})
    assert parse_stream_json(ok).terminal_reason == "completed"
    assert parse_stream_json(bad).terminal_reason == "error"


def test_parse_stream_json_leaves_reason_unset_without_a_terminal_event() -> None:
    """A killed or truncated stream reports no reason of its own, leaving the
    call to the harness, which knows whether it did the killing."""
    blob = _stream(_assistant({"type": "text", "text": "hi"}))
    assert parse_stream_json(blob).terminal_reason == ""


def test_parse_stream_json_first_terminal_event_wins_for_reason_and_errors() -> None:
    """A later terminal event must not append a failure the resolved reason no
    longer reflects, or the row says ``completed`` while its own error list
    names a mid-execution failure."""
    blob = _stream(
        {"type": "result", "subtype": "success", "terminal_reason": "completed", "result": "ok"},
        {"type": "result", "subtype": "error_during_execution", "result": ""},
    )
    parsed = parse_stream_json(blob)
    assert parsed.terminal_reason == "completed"
    assert parsed.errors == []


def test_parse_stream_json_ignores_the_cli_synthetic_error_envelope() -> None:
    """The CLI renders its own failures as an assistant envelope carrying
    ``model: "<synthetic>"`` and a UUID id. Its text is the error the user
    should see, but no model was called, so counting it would put a model id
    that does not exist on the leaderboard and invent a round-trip."""
    blob = _stream(
        _assistant({"type": "text", "text": "hi"}, msg_id="msg_1", model="claude-opus-5"),
        {
            "type": "assistant",
            "is_api_error_message": True,
            "error": "model_not_found",
            "message": {
                "id": "0335a8d1-0723-4f3b-a1f6-d5d0b352aa5a",
                "model": "<synthetic>",
                "content": [{"type": "text", "text": "There's an issue with the selected model."}],
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )
    parsed = parse_stream_json(blob)
    assert parsed.served_models == ["claude-opus-5"]
    assert parsed.model_turns == 1
    assert "There's an issue with the selected model." in parsed.output


def test_parse_stream_json_counts_model_turns_by_message_id() -> None:
    """Claude Code emits one envelope per content block, all repeating the
    message id, so envelope count would overcount a single model turn."""
    blob = _stream(
        _assistant({"type": "text", "text": "on it"}, msg_id="m1"),
        _assistant({"type": "tool_use", "id": "a", "name": "Read", "input": {}}, msg_id="m1"),
        _assistant({"type": "tool_use", "id": "b", "name": "Read", "input": {}}, msg_id="m1"),
        _user({"type": "tool_result", "tool_use_id": "a", "content": "x"}),
        _user({"type": "tool_result", "tool_use_id": "b", "content": "y"}),
        _assistant({"type": "text", "text": "done"}, msg_id="m2"),
    )
    assert parse_stream_json(blob).model_turns == 2


def test_parse_stream_json_leaves_model_turns_unreported_without_message_ids() -> None:
    """Zero turns and "the stream carries no ids" are different facts; only the
    latter is unknown, and ``None`` is how the row records unknown."""
    blob = _stream(_assistant({"type": "text", "text": "hi"}))
    assert parse_stream_json(blob).model_turns is None


def test_parse_stream_json_treats_an_empty_message_id_as_unidentified() -> None:
    """An empty id is no id: counting it invents a turn, and deduping on it
    merges genuinely distinct messages and drops the second one's usage."""
    blob = _stream(
        _assistant({"type": "text", "text": "a"}, msg_id="", usage={"input_tokens": 5}),
        _assistant({"type": "text", "text": "b"}, msg_id="", usage={"input_tokens": 5}),
    )
    parsed = parse_stream_json(blob)
    assert parsed.model_turns is None
    assert parsed.tokens["input"] == 10


def test_parse_stream_json_times_a_tool_call_from_its_envelope_timestamps() -> None:
    blob = _stream(
        _assistant(
            {"type": "tool_use", "id": "a", "name": "Read", "input": {}},
            ts="2026-08-24T17:44:45.000Z",
        ),
        _user(
            {"type": "tool_result", "tool_use_id": "a", "content": "x"},
            ts="2026-08-24T17:44:47.500Z",
        ),
    )
    assert parse_stream_json(blob).tool_wait_sec == pytest.approx(2.5)


def test_parse_stream_json_counts_concurrent_tool_calls_once() -> None:
    """A model turn dispatches its calls together; summing their durations
    would report more tool time than the run took."""
    blob = _stream(
        _assistant(
            {"type": "tool_use", "id": "a", "name": "Read", "input": {}},
            {"type": "tool_use", "id": "b", "name": "Read", "input": {}},
            ts="2026-08-24T17:44:45.000Z",
        ),
        _user(
            {"type": "tool_result", "tool_use_id": "a", "content": "x"},
            ts="2026-08-24T17:44:47.000Z",
        ),
        _user(
            {"type": "tool_result", "tool_use_id": "b", "content": "y"},
            ts="2026-08-24T17:44:48.000Z",
        ),
    )
    assert parse_stream_json(blob).tool_wait_sec == pytest.approx(3.0)


def test_parse_stream_json_leaves_tool_wait_unreported_without_timestamps() -> None:
    blob = _stream(
        _assistant({"type": "tool_use", "id": "a", "name": "Read", "input": {}}),
        _user({"type": "tool_result", "tool_use_id": "a", "content": "x"}),
    )
    parsed = parse_stream_json(blob)
    assert parsed.trajectory[0]["status"] == "completed"
    assert parsed.tool_wait_sec is None


def test_parse_stream_json_leaves_tool_wait_unreported_for_an_unanswered_call() -> None:
    """A call the run never got an answer for has no measurable wait; the model
    time before it must not be attributed to tools."""
    blob = _stream(
        _assistant(
            {"type": "tool_use", "id": "a", "name": "Read", "input": {}},
            ts="2026-08-24T17:44:45.000Z",
        ),
        {"type": "result", "subtype": "success", "result": "ok"},
    )
    assert parse_stream_json(blob).tool_wait_sec is None


def test_parse_stream_json_records_served_models_in_first_seen_order() -> None:
    blob = _stream(
        _assistant({"type": "text", "text": "a"}, msg_id="m1", model="claude-opus-5"),
        _assistant({"type": "text", "text": "b"}, msg_id="m2", model="claude-sonnet-5"),
        _assistant({"type": "text", "text": "c"}, msg_id="m3", model="claude-opus-5"),
    )
    assert parse_stream_json(blob).served_models == ["claude-opus-5", "claude-sonnet-5"]


def test_parse_stream_json_served_models_excludes_the_cli_helper_model() -> None:
    """``modelUsage`` on the terminal event also lists the CLI's own background
    helper, which never answered the task; only assistant envelopes count."""
    blob = _stream(
        _assistant({"type": "text", "text": "a"}, msg_id="m1", model="claude-opus-5"),
        {
            "type": "result",
            "subtype": "success",
            "result": "a",
            "modelUsage": {"claude-opus-5": {}, "claude-haiku-4-5@20251001": {}},
        },
    )
    assert parse_stream_json(blob).served_models == ["claude-opus-5"]


def test_parse_stream_json_leaves_served_models_empty_when_unstamped() -> None:
    blob = _stream(_assistant({"type": "text", "text": "hi"}, msg_id="m1"))
    assert parse_stream_json(blob).served_models == []


# ---------------------------------------------------------------------------
# argv
# ---------------------------------------------------------------------------


def test_build_argv_base_flags_and_prompt_via_argv() -> None:
    argv = _build_argv("/bin/claude", "hi", model=None, max_turns=None, mcp_config_path=None)
    assert argv[0] == "/bin/claude"
    # Prompt is the trailing argv value (never a shell string), behind ``--``.
    assert argv[-2:] == ["--", "hi"]
    assert "-p" in argv
    assert "--output-format" in argv and "stream-json" in argv
    assert "--verbose" in argv  # required for stream-json under -p
    assert "--dangerously-skip-permissions" in argv
    # Optional flags absent when unset.
    assert "--model" not in argv
    assert "--max-turns" not in argv
    assert "--mcp-config" not in argv
    # This harness never emits an allowlist (bare names can't match mcp__ tools).
    assert "--allowedTools" not in argv and "--allowed-tools" not in argv


def test_build_argv_shields_a_flag_like_prompt_behind_a_separator() -> None:
    """A prompt whose first token looks like a flag must reach the CLI as the
    prompt. Without ``--`` the option parser consumes it and aborts the run."""
    argv = _build_argv(
        "/bin/claude", "--settings=/tmp/x.json", model=None, max_turns=None, mcp_config_path=None
    )
    assert argv[-2:] == ["--", "--settings=/tmp/x.json"]


def test_build_argv_threads_model_and_max_turns_when_set() -> None:
    argv = _build_argv(
        "/bin/claude", "hi", model="claude-opus-4-8", max_turns=7, mcp_config_path=None
    )
    assert argv[argv.index("--model") + 1] == "claude-opus-4-8"
    assert argv[argv.index("--max-turns") + 1] == "7"


def test_build_argv_always_sets_strict_mcp_config() -> None:
    """Strict mode is unconditional: on a baseline arm it pins the run to zero
    servers, so a stray ``.mcp.json`` in the workspace cannot grant MCP tools."""
    bare = _build_argv("/bin/claude", "hi", model=None, max_turns=None, mcp_config_path=None)
    assert "--strict-mcp-config" in bare

    bound = _build_argv(
        "/bin/claude", "hi", model=None, max_turns=None, mcp_config_path="/w/.claude/mcp.json"
    )
    assert bound[bound.index("--mcp-config") + 1] == "/w/.claude/mcp.json"
    assert "--strict-mcp-config" in bound


# ---------------------------------------------------------------------------
# env
# ---------------------------------------------------------------------------


def test_build_env_threads_api_key_into_anthropic_var() -> None:
    env = _build_env(AgentConfig(api_key="sk-abc"), config_dir=None)
    assert env["ANTHROPIC_API_KEY"] == "sk-abc"
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert env["DISABLE_AUTOUPDATER"] == "1"


def test_build_env_keyless_vertex_sets_switch_and_maps_project_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GCP_PROJECT_ID", "proj-42")
    monkeypatch.setenv("GCP_VERTEX_LOCATION", "us-east5")
    env = _build_env(AgentConfig(provider="anthropic-vertex"), config_dir=None)
    assert env["CLAUDE_CODE_USE_VERTEX"] == "1"
    assert env["ANTHROPIC_VERTEX_PROJECT_ID"] == "proj-42"
    assert env["CLOUD_ML_REGION"] == "us-east5"
    # Keyless: no API-key var written even absent an explicit api_key.
    assert "ANTHROPIC_API_KEY" not in env


def test_build_env_vertex_region_defaults_to_global(monkeypatch: pytest.MonkeyPatch) -> None:
    # Both inputs to the region chain must be cleared: an ambient CLOUD_ML_REGION
    # on the developer's machine or the CI runner would shadow the fallback.
    monkeypatch.delenv("GCP_VERTEX_LOCATION", raising=False)
    monkeypatch.delenv("CLOUD_ML_REGION", raising=False)
    monkeypatch.setenv("GCP_PROJECT_ID", "proj-42")
    env = _build_env(AgentConfig(provider="anthropic-vertex"), config_dir=None)
    assert env["CLOUD_ML_REGION"] == "global"


def test_build_env_keyless_bedrock_sets_switch() -> None:
    env = _build_env(AgentConfig(provider="anthropic-bedrock"), config_dir=None)
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert "ANTHROPIC_API_KEY" not in env


def test_build_env_unknown_provider_raises_even_when_keyless() -> None:
    with pytest.raises(ConfigError):
        _build_env(AgentConfig(provider="anthropi"), config_dir=None)


def test_build_env_extra_env_wins_over_computed_vars() -> None:
    cfg = AgentConfig(api_key="sk-abc", extra_env={"ANTHROPIC_API_KEY": "override", "X": "y"})
    env = _build_env(cfg, config_dir=None)
    assert env["ANTHROPIC_API_KEY"] == "override"
    assert env["X"] == "y"


def test_build_env_sets_config_dir_when_provided() -> None:
    env = _build_env(AgentConfig(), config_dir="/tmp/cfg")
    assert env["CLAUDE_CONFIG_DIR"] == "/tmp/cfg"


def test_build_env_omits_config_dir_when_none() -> None:
    env = _build_env(AgentConfig(), config_dir=None)
    assert "CLAUDE_CONFIG_DIR" not in env


# ---------------------------------------------------------------------------
# Registry + capability protocols
# ---------------------------------------------------------------------------


def test_claude_agent_registered_under_canonical_key() -> None:
    assert AGENTS.get("claude") is ClaudeCodeAgent


def test_claude_agent_satisfies_mcp_skills_and_rules_protocols() -> None:
    agent = ClaudeCodeAgent(AgentConfig())
    assert isinstance(agent, SupportsMcp)
    assert isinstance(agent, SupportsSkills)
    assert isinstance(agent, SupportsRules)


def test_claude_agent_mirrors_capability_bindings_onto_mixin_attributes() -> None:
    binding = McpBinding(name="x", command=(), tools=("t",))
    skills = SkillBinding(paths=("/some/skills",))
    caps = AllCapabilities(
        mcp_servers=(binding,),
        skills=skills,
        rules=AgentRules(text="be a sre"),
    )
    agent = ClaudeCodeAgent(AgentConfig(capabilities=caps))
    assert agent.mcp_servers == (binding,)
    assert agent.skills == skills
    assert agent.rules == AgentRules(text="be a sre")


# ---------------------------------------------------------------------------
# _execute: return shape, wiring, and error paths
# ---------------------------------------------------------------------------


def test_execute_returns_typed_result_with_trajectory(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        captured["argv"] = argv
        captured["timeout"] = kwargs.get("timeout")
        return SimpleNamespace(stdout=SAMPLE_STREAM, stderr="", returncode=0)

    monkeypatch.setattr(claude_mod, "run", fake_run)
    result = ClaudeCodeAgent(AgentConfig(target="claude-x", timeout_sec=30.0)).run("ping")
    assert result.output == "Done."
    assert len(result.trajectory) == 2
    assert result.errors == []
    assert result.tokens == _tok(input=10, cached=5, output=20, total=35)
    assert captured["timeout"] == 30.0
    assert captured["argv"][0].endswith("claude-x")
    assert captured["argv"][-2:] == ["--", "ping"]


def test_execute_closes_child_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    """An inherited stdin makes the CLI wait out its 3s piped-prompt timeout on
    every run and warn on stderr; an empty ``input`` closes the pipe at once."""
    captured: dict = {}

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        captured["input"] = kwargs.get("input")
        return SimpleNamespace(stdout=SAMPLE_STREAM, stderr="", returncode=0)

    monkeypatch.setattr(claude_mod, "run", fake_run)
    ClaudeCodeAgent(AgentConfig(target="claude")).run("p")
    assert captured["input"] == ""


def test_execute_wires_extra_env_into_subprocess_call(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        captured["extra_env"] = kwargs.get("extra_env")
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(claude_mod, "run", fake_run)
    ClaudeCodeAgent(AgentConfig(target="claude", api_key="sk-abc")).run("p")
    env = captured["extra_env"]
    assert env["ANTHROPIC_API_KEY"] == "sk-abc"
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"


def test_execute_records_non_zero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(stdout="", stderr="boom", returncode=2)

    monkeypatch.setattr(claude_mod, "run", fake_run)
    result = ClaudeCodeAgent(AgentConfig(target="claude")).run("p")
    assert result.has_errors()
    assert any("exited 2" in e for e in result.errors)
    assert result.metadata.get("returncode") == 2


def test_execute_parses_stream_on_non_zero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ``error_max_turns`` run exits non-zero *after* emitting a full stream;
    output and trajectory must still be parsed, with the exit + subtype recorded."""
    stream = _stream(
        _assistant({"type": "tool_use", "id": "c1", "name": "do", "input": {}}),
        {"type": "result", "subtype": "error_max_turns", "result": "hit the cap"},
    )

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(stdout=stream, stderr="turn limit reached", returncode=1)

    monkeypatch.setattr(claude_mod, "run", fake_run)
    result = ClaudeCodeAgent(AgentConfig(target="claude")).run("p")
    assert result.output == "hit the cap"
    assert len(result.trajectory) == 1
    assert result.metadata["returncode"] == 1
    assert result.metadata["stderr"] == "turn limit reached"
    assert any("error_max_turns" in e for e in result.errors)
    assert any("exited 1" in e for e in result.errors)


def test_execute_captures_stderr_on_clean_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """stderr is kept for diagnosis even when the process exits 0."""

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(stdout=SAMPLE_STREAM, stderr="a warning", returncode=0)

    monkeypatch.setattr(claude_mod, "run", fake_run)
    result = ClaudeCodeAgent(AgentConfig(target="claude")).run("p")
    assert result.metadata["stderr"] == "a warning"
    assert "returncode" not in result.metadata
    assert not any("exited" in e for e in result.errors)


def test_execute_reports_a_timeout_as_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run`` is called with ``check=False``, so the only ``SubprocessError`` it
    raises is a timeout. Naming it keeps the wall-clock budget out of the crash
    bucket, where ``exit -1`` would have put it."""

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        raise SubprocessError(argv, returncode=-1, stdout="", stderr="killed", timed_out=True)

    monkeypatch.setattr(claude_mod, "run", fake_run)
    result = ClaudeCodeAgent(AgentConfig(target="claude", timeout_sec=900)).run("p")
    assert result.has_errors()
    assert result.errors[0] == "claude timed out after 900s: killed"
    assert result.metadata["returncode"] == -1
    assert result.trajectory == []


def test_execute_recovers_partial_trajectory_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timeout carries the partial stream-json captured before the kill; the
    harness recovers the trajectory instead of discarding the run's work, while
    still surfacing the error."""
    partial = _stream(
        _assistant(
            {"type": "tool_use", "id": "c1", "name": "mcp__gke__list_clusters", "input": {}}
        ),
        _user({"type": "tool_result", "tool_use_id": "c1", "content": "ok"}),
    )

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        raise SubprocessError(
            argv, returncode=-1, stdout=partial, stderr="killed after timeout", timed_out=True
        )

    monkeypatch.setattr(claude_mod, "run", fake_run)
    result = ClaudeCodeAgent(AgentConfig(target="claude")).run("p")
    assert result.has_errors()
    assert any("timed out" in e for e in result.errors)
    assert [step["name"] for step in result.trajectory] == ["gke__list_clusters"]
    assert result.metadata["stderr"] == "killed after timeout"


def test_execute_handles_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        raise OSError("not found")

    monkeypatch.setattr(claude_mod, "run", fake_run)
    result = ClaudeCodeAgent(AgentConfig(target="claude")).run("p")
    assert result.has_errors()
    assert "failed to spawn claude" in result.errors[0]
    assert result.tokens == _tok()
    assert result.terminal_reason == "error"
    assert result.latency > 0


def test_execute_passes_timeout_to_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(claude_mod, "run", fake_run)
    ClaudeCodeAgent(AgentConfig(target="claude", timeout_sec=15.5)).run("p")
    assert captured["timeout"] == 15.5


# ---------------------------------------------------------------------------
# _execute: per-run telemetry reaching the result
# ---------------------------------------------------------------------------


def test_execute_reports_per_run_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = _stream(
        _assistant(
            {"type": "tool_use", "id": "c1", "name": "Read", "input": {}},
            msg_id="m1",
            model="claude-opus-5",
            ts="2026-08-24T17:44:45.000Z",
        ),
        _user(
            {"type": "tool_result", "tool_use_id": "c1", "content": "x"},
            ts="2026-08-24T17:44:46.000Z",
        ),
        _assistant({"type": "text", "text": "done"}, msg_id="m2", model="claude-opus-5"),
        {"type": "result", "subtype": "success", "terminal_reason": "completed", "result": "done"},
    )

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(stdout=stream, stderr="", returncode=0)

    monkeypatch.setattr(claude_mod, "run", fake_run)
    result = ClaudeCodeAgent(AgentConfig(target="claude")).run("p")
    assert result.terminal_reason == "completed"
    assert result.model_turns == 2
    assert result.tool_wait_sec == pytest.approx(1.0)
    assert result.served_models == ["claude-opus-5"]
    assert result.latency > 0


def test_execute_reports_timeout_over_the_streams_own_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The kill is the harness's own doing, so it outranks a terminal event the
    process managed to emit before dying."""
    partial = _stream(
        {"type": "result", "subtype": "success", "terminal_reason": "completed", "result": "ok"}
    )

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        raise SubprocessError(argv, returncode=-1, stdout=partial, stderr="killed", timed_out=True)

    monkeypatch.setattr(claude_mod, "run", fake_run)
    result = ClaudeCodeAgent(AgentConfig(target="claude")).run("p")
    assert result.terminal_reason == "timeout"


def test_execute_reports_a_non_timeout_subprocess_error_as_an_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``timed_out`` distinguishes the wall-clock kill from any other raise, so a
    non-timeout failure is not mislabelled as one."""

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        raise SubprocessError(argv, returncode=2, stdout="", stderr="boom", timed_out=False)

    monkeypatch.setattr(claude_mod, "run", fake_run)
    result = ClaudeCodeAgent(AgentConfig(target="claude")).run("p")
    assert result.errors[0] == "claude failed to run: exit 2: boom"
    assert result.terminal_reason == "error"


def test_execute_reports_the_terminal_event_over_a_non_zero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI exits non-zero on a turn cap, which the parser resolves to
    ``completed``, so the terminal event is the more specific signal."""
    stream = _stream(
        {
            "type": "result",
            "subtype": "error_max_turns",
            "terminal_reason": "max_turns",
            "is_error": True,
            "result": "ok",
        }
    )

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(stdout=stream, stderr="", returncode=1)

    monkeypatch.setattr(claude_mod, "run", fake_run)
    result = ClaudeCodeAgent(AgentConfig(target="claude")).run("p")
    assert result.terminal_reason == "completed"
    assert any("max_turns" in e for e in result.errors)


def test_execute_falls_back_to_the_exit_code_without_a_terminal_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(claude_mod, "run", fake_run)
    result = ClaudeCodeAgent(AgentConfig(target="claude")).run("p")
    assert result.terminal_reason == "completed"


def test_execute_falls_back_to_a_non_zero_exit_without_a_terminal_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A truncated pipe or a binary that died before its terminal event leaves
    the exit code as the only verdict available."""
    stream = _stream(_assistant({"type": "text", "text": "partial"}))

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(stdout=stream, stderr="boom", returncode=1)

    monkeypatch.setattr(claude_mod, "run", fake_run)
    result = ClaudeCodeAgent(AgentConfig(target="claude")).run("p")
    assert result.terminal_reason == "error"


# ---------------------------------------------------------------------------
# _execute side-effects: capabilities land in the cwd before the subprocess.
# Assertions run INSIDE the fake ``run`` because the temp cwd is cleaned up
# once ``_execute`` returns.
# ---------------------------------------------------------------------------


def test_execute_writes_claude_md_with_rules_text_before_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        cwd = kwargs.get("cwd")
        captured["cwd"] = cwd
        claude_md = Path(cwd) / "CLAUDE.md" if cwd else None
        captured["exists"] = bool(claude_md and claude_md.exists())
        captured["text"] = claude_md.read_text(encoding="utf-8") if captured["exists"] else None
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(claude_mod, "run", fake_run)
    caps = AllCapabilities(rules=AgentRules(text="you are a precise SRE"))
    ClaudeCodeAgent(AgentConfig(target="claude", capabilities=caps)).run("p")
    assert captured["exists"], "CLAUDE.md must exist in cwd before subprocess"
    assert captured["text"] == "you are a precise SRE"


def test_execute_skips_writing_claude_md_when_rules_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        cwd = kwargs.get("cwd")
        captured["exists"] = bool(cwd and os.path.exists(os.path.join(cwd, "CLAUDE.md")))
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(claude_mod, "run", fake_run)
    ClaudeCodeAgent(AgentConfig(target="claude")).run("p")
    assert captured["exists"] is False


def test_execute_writes_mcp_config_and_passes_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        if "--version" in argv:
            return SimpleNamespace(stdout="2.1.228 (Claude Code)\n", stderr="", returncode=0)
        captured["argv"] = argv
        mcp_path = os.path.join(kwargs["cwd"], ".claude", "mcp-config.json")
        captured["exists"] = os.path.exists(mcp_path)
        if captured["exists"]:
            captured["mode"] = stat.S_IMODE(os.stat(mcp_path).st_mode)
            with open(mcp_path) as f:
                captured["payload"] = json.load(f)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(claude_mod, "run", fake_run)
    caps = AllCapabilities(
        mcp_servers=(McpBinding(name="gke", command=("gke-mcp",), tools=("mcp__gke__x",)),),
    )
    ClaudeCodeAgent(AgentConfig(target="claude", capabilities=caps)).run("p")

    assert captured["exists"], "mcp-config.json must exist in cwd before subprocess"
    assert captured["payload"] == {"mcpServers": {"gke": {"command": "gke-mcp"}}}
    # A binding's argv can carry a server credential, so the file must not be
    # left at the umask default in a workspace the harness later collects.
    assert captured["mode"] == 0o600
    argv = captured["argv"]
    assert argv[argv.index("--mcp-config") + 1].endswith(os.path.join(".claude", "mcp-config.json"))
    assert "--strict-mcp-config" in argv


def _mcp_caps() -> AllCapabilities:
    return AllCapabilities(
        mcp_servers=(McpBinding(name="gke", command=("gke-mcp",), tools=("mcp__gke__x",)),),
    )


def test_execute_refuses_mcp_run_on_a_binary_predating_the_startup_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An older binary accepts ``--mcp-config`` but starts the first turn without
    waiting for the servers, so the arm runs un-augmented yet is scored as
    augmented. Fail loud instead of recording a contaminated result."""
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(list(argv))
        if "--version" in argv:
            return SimpleNamespace(stdout="2.1.220 (Claude Code)\n", stderr="", returncode=0)
        raise AssertionError("the agent turn must not run on an under-version binary")

    monkeypatch.setattr(claude_mod, "run", fake_run)
    result = ClaudeCodeAgent(AgentConfig(target="claude", capabilities=_mcp_caps())).run("p")

    assert calls == [["claude", "--version"]]
    assert result.tokens == empty_tokens()
    assert "2.1.220" in result.errors[0]
    assert "2.1.221" in result.errors[0]


def test_execute_probes_the_version_once_per_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """A matrix run drives one binary across every task, and the version cannot
    change under a live process, so the probe is spawned once rather than once
    per MCP-bound task."""
    probes = 0

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        nonlocal probes
        if "--version" in argv:
            probes += 1
            return SimpleNamespace(stdout="2.1.228 (Claude Code)\n", stderr="", returncode=0)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(claude_mod, "run", fake_run)
    agent = ClaudeCodeAgent(AgentConfig(target="claude", capabilities=_mcp_caps()))
    agent.run("first")
    agent.run("second")

    assert probes == 1


def test_execute_skips_the_version_probe_without_mcp_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The floor only bites on ``--mcp-config``; a baseline arm must not pay for
    an extra subprocess on every run."""
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(list(argv))
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(claude_mod, "run", fake_run)
    ClaudeCodeAgent(AgentConfig(target="claude")).run("p")

    assert all("--version" not in argv for argv in calls)


@pytest.mark.parametrize(
    "probe",
    [
        SimpleNamespace(stdout="", stderr="not a claude binary", returncode=1),
        SimpleNamespace(stdout="wrapper build abc\n", stderr="", returncode=0),
        OSError("no such binary"),
    ],
    ids=["nonzero-exit", "unparseable", "spawn-failure"],
)
def test_execute_proceeds_when_the_version_probe_is_inconclusive(
    monkeypatch: pytest.MonkeyPatch, probe: SimpleNamespace | OSError
) -> None:
    """``config.target`` may be a wrapper with its own ``--version`` surface, so a
    probe that fails to yield a number must not block the run."""
    ran: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        if "--version" in argv:
            if isinstance(probe, OSError):
                raise probe
            return probe
        ran.append(list(argv))
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(claude_mod, "run", fake_run)
    ClaudeCodeAgent(AgentConfig(target="claude", capabilities=_mcp_caps())).run("p")

    assert len(ran) == 1
    assert "--mcp-config" in ran[0]


def test_execute_writes_no_mcp_config_when_no_command(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        captured["argv"] = argv
        mcp_path = os.path.join(kwargs["cwd"], ".claude", "mcp-config.json")
        captured["exists"] = os.path.exists(mcp_path)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(claude_mod, "run", fake_run)
    # Binding carries tools but no launch command → nothing to write.
    caps = AllCapabilities(
        mcp_servers=(McpBinding(name="builtin", command=(), tools=("alpha",)),),
    )
    ClaudeCodeAgent(AgentConfig(target="claude", capabilities=caps)).run("p")
    assert captured["exists"] is False
    assert "--mcp-config" not in captured["argv"]


def test_execute_materializes_skills_into_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    src = tmp_path / "skills" / "my-skill"
    src.mkdir(parents=True)
    skill_text = "---\nname: my-skill\ndescription: do things\n---\nbody\n"
    (src / "SKILL.md").write_text(skill_text)

    captured: dict = {}

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        skill_path = os.path.join(kwargs["cwd"], ".claude", "skills", "my-skill", "SKILL.md")
        captured["exists"] = os.path.exists(skill_path)
        if captured["exists"]:
            with open(skill_path) as f:
                captured["text"] = f.read()
        else:
            captured["text"] = None
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(claude_mod, "run", fake_run)
    caps = AllCapabilities(skills=SkillBinding(paths=(str(tmp_path / "skills"),)))
    ClaudeCodeAgent(AgentConfig(target="claude", capabilities=caps)).run("p")
    assert captured["exists"], "skill must be materialized before subprocess"
    assert captured["text"] == skill_text


# ---------------------------------------------------------------------------
# Parallel isolation + CLAUDE_CONFIG_DIR handling.
# ---------------------------------------------------------------------------


def test_execute_injects_per_run_config_dir_when_ambient_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    captured: dict = {}

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        captured["cwd"] = kwargs.get("cwd")
        captured["config_dir"] = kwargs.get("extra_env", {}).get("CLAUDE_CONFIG_DIR")
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(claude_mod, "run", fake_run)
    ClaudeCodeAgent(AgentConfig(target="claude")).run("p")

    cfg_dir = captured["config_dir"]
    assert cfg_dir is not None
    assert os.path.basename(cfg_dir).startswith("claude-config-")
    assert os.path.realpath(cfg_dir).startswith(os.path.realpath(tempfile.gettempdir()))
    assert os.path.expanduser("~/.claude") not in os.path.realpath(cfg_dir)
    # cwd is a fresh throwaway too.
    assert os.path.basename(captured["cwd"]).startswith("claude-run-")


def test_execute_respects_operator_config_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/operator/claude")
    monkeypatch.delenv("BENCH_PARALLEL", raising=False)
    captured: dict = {}

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        captured["config_dir"] = kwargs.get("extra_env", {}).get("CLAUDE_CONFIG_DIR")
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(claude_mod, "run", fake_run)
    ClaudeCodeAgent(AgentConfig(target="claude")).run("p")
    # The harness does not override an operator-exported value (it flows through
    # os.environ, not the overlay), so no CLAUDE_CONFIG_DIR is added to extra_env.
    assert captured["config_dir"] is None


def test_execute_overrides_operator_config_dir_under_parallel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--parallel`` means several benchmark processes share this host. Honouring
    the operator's dir would hand them one mutable Claude config to race on, so
    isolation outranks the login cache the hatch exists to preserve."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/operator/claude")
    monkeypatch.setenv("BENCH_PARALLEL", "true")
    cfg_dirs: list[str | None] = []

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        cfg_dirs.append(kwargs.get("extra_env", {}).get("CLAUDE_CONFIG_DIR"))
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(claude_mod, "run", fake_run)
    agent = ClaudeCodeAgent(AgentConfig(target="claude"))
    agent.run("p")
    agent.run("p")

    assert all(d and d != "/operator/claude" for d in cfg_dirs), cfg_dirs
    assert len(set(cfg_dirs)) == 2, f"config dirs must be unique per run, got {cfg_dirs}"


def test_execute_uses_distinct_cwd_and_config_dir_per_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    cwds: list[str] = []
    cfg_dirs: list[str] = []

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        cwds.append(kwargs.get("cwd"))
        cfg_dirs.append(kwargs.get("extra_env", {}).get("CLAUDE_CONFIG_DIR"))
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(claude_mod, "run", fake_run)
    agent = ClaudeCodeAgent(AgentConfig(target="claude"))
    agent.run("p")
    agent.run("p")

    assert len(set(map(str, cwds))) == 2, f"cwds must be unique per run, got {cwds}"
    assert len(set(cfg_dirs)) == 2, f"config dirs must be unique per run, got {cfg_dirs}"
