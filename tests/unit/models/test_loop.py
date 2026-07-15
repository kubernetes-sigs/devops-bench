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

"""Tests for the shared tool-use loop primitive."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from devops_bench.models.base import LLMClient
from devops_bench.models.utils.loop import LoopResult, run_tool_loop


@dataclass
class _Turn:
    """One scripted response in :class:`FakeLLMClient`.

    Attributes:
        text: Text returned by ``get_text_content`` for this turn.
        calls: Function calls returned by ``extract_function_calls`` (each in
            the neutral ``{"name", "args", "id"}`` shape).
        latency: Seconds the fake awaits inside ``generate_content`` to make
            latency accumulation observable.
    """

    text: str
    calls: list[dict] = field(default_factory=list)
    latency: float = 0.0


class FakeLLMClient(LLMClient):
    """Scripted :class:`LLMClient` that plays back ``_Turn`` objects in order.

    Records every ``generate_content`` invocation so tests can inspect the
    ``contents``/``tools``/``system_instruction`` arguments and confirm the
    loop forwards them unchanged.
    """

    def __init__(self, turns: list[_Turn]) -> None:
        self._turns = list(turns)
        self.calls: list[dict] = []

    async def generate_content(
        self,
        contents: list[dict[str, Any]],
        tools: Any,
        system_instruction: str | None,
    ) -> Any:
        if not self._turns:
            raise AssertionError("FakeLLMClient ran out of scripted turns")
        turn = self._turns.pop(0)
        # Snapshot the inputs so assertions can verify the loop forwards them.
        self.calls.append(
            {
                "contents": [dict(msg) for msg in contents],
                "tools": tools,
                "system_instruction": system_instruction,
            }
        )
        if turn.latency:
            await asyncio.sleep(turn.latency)
        return turn

    def format_tools(self, mcp_tools: Any) -> Any:
        # Not exercised by run_tool_loop (caller-formats-tools).
        return mcp_tools

    def extract_function_calls(self, response: Any) -> list[dict]:
        return list(response.calls)

    def get_text_content(self, response: Any) -> str:
        return response.text


def _dispatcher_recording(results: dict[str, str]):
    """Build a dispatcher that returns scripted text and records calls.

    Args:
        results: Mapping of tool name to the text to return.

    Returns:
        A ``(dispatch, log)`` tuple. ``log`` is a list mutated on every call.
    """
    log: list[tuple[str, Any, str | None]] = []

    async def dispatch(name: str, args: Any, call_id: str | None) -> str:
        log.append((name, args, call_id))
        return results.get(name, f"unhandled:{name}")

    return dispatch, log


@pytest.mark.asyncio
async def test_turn_cap_warns_and_stops_at_max_turns(caplog):
    # Every turn issues a tool call, so only the cap stops the loop.
    turns = [
        _Turn(text=f"turn{i}", calls=[{"name": "t", "args": {}, "id": str(i)}]) for i in range(5)
    ]
    client = FakeLLMClient(turns)
    dispatch, _ = _dispatcher_recording({"t": "ok"})

    with caplog.at_level("WARNING", logger="devops_bench.models.utils.loop"):
        result = await run_tool_loop(
            client=client,
            goal="g",
            tools=None,
            system_instruction=None,
            dispatch=dispatch,
            max_turns=3,
        )

    assert len(client.calls) == 3, "loop must stop at max_turns"
    assert any("turn limit (3)" in rec.message for rec in caplog.records)
    # The 4th and 5th scripted turns were never requested.
    assert len(client._turns) == 2
    assert result.final_text == "turn2"


@pytest.mark.asyncio
async def test_final_text_retained_when_last_turn_issues_tool_call():
    # The last turn within the cap still issues a tool call: its text must survive.
    turns = [
        _Turn(text="thinking", calls=[{"name": "t", "args": {}, "id": "a"}]),
        _Turn(text="goodbye-with-tool", calls=[{"name": "t", "args": {}, "id": "b"}]),
    ]
    client = FakeLLMClient(turns)
    dispatch, _ = _dispatcher_recording({"t": "ok"})

    result = await run_tool_loop(
        client=client,
        goal="g",
        tools=None,
        system_instruction=None,
        dispatch=dispatch,
        max_turns=2,
    )

    assert result.final_text == "goodbye-with-tool"


@pytest.mark.asyncio
async def test_final_text_retained_on_natural_exit():
    # Two turns: first calls a tool, second answers with no calls.
    turns = [
        _Turn(text="t1", calls=[{"name": "t", "args": {}, "id": "x"}]),
        _Turn(text="t2-final", calls=[]),
    ]
    client = FakeLLMClient(turns)
    dispatch, _ = _dispatcher_recording({"t": "ok"})

    result = await run_tool_loop(
        client=client,
        goal="g",
        tools=None,
        system_instruction=None,
        dispatch=dispatch,
        max_turns=10,
    )

    assert result.final_text == "t2-final"
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_latency_accumulates_across_turns():
    turns = [
        _Turn(text="t1", calls=[{"name": "t", "args": {}, "id": "1"}], latency=0.02),
        _Turn(text="t2", calls=[], latency=0.03),
    ]
    client = FakeLLMClient(turns)
    dispatch, _ = _dispatcher_recording({"t": "ok"})

    result = await run_tool_loop(
        client=client,
        goal="g",
        tools=None,
        system_instruction=None,
        dispatch=dispatch,
        max_turns=10,
    )

    # Each scripted turn slept; the total must cover both, not just one.
    assert result.latency >= 0.05
    # And it must reflect summed turns, not a single observation.
    assert result.latency < 0.5


@pytest.mark.asyncio
async def test_dispatch_error_surfaces_to_caller():
    turns = [_Turn(text="t1", calls=[{"name": "boom", "args": {}, "id": "1"}])]
    client = FakeLLMClient(turns)

    async def dispatch(name: str, args: Any, call_id: str | None) -> str:
        raise RuntimeError("dispatch blew up")

    with pytest.raises(RuntimeError, match="dispatch blew up"):
        await run_tool_loop(
            client=client,
            goal="g",
            tools=None,
            system_instruction=None,
            dispatch=dispatch,
            max_turns=5,
        )


@pytest.mark.asyncio
async def test_tools_used_reflects_dispatched_names():
    turns = [
        _Turn(
            text="t1",
            calls=[
                {"name": "alpha", "args": {}, "id": "1"},
                {"name": "beta", "args": {}, "id": "2"},
            ],
        ),
        _Turn(text="t2", calls=[{"name": "alpha", "args": {}, "id": "3"}]),
        _Turn(text="t3", calls=[]),
    ]
    client = FakeLLMClient(turns)
    dispatch, log = _dispatcher_recording({"alpha": "A", "beta": "B"})

    result = await run_tool_loop(
        client=client,
        goal="g",
        tools=None,
        system_instruction=None,
        dispatch=dispatch,
        max_turns=10,
    )

    assert result.tools_used == {"alpha", "beta"}
    assert [name for name, _, _ in log] == ["alpha", "beta", "alpha"]


@pytest.mark.asyncio
async def test_caller_formats_tools_passed_through_unchanged():
    # The loop must not call format_tools and must forward `tools` verbatim.
    sentinel = object()
    turns = [_Turn(text="done", calls=[])]
    client = FakeLLMClient(turns)
    dispatch, _ = _dispatcher_recording({})

    result = await run_tool_loop(
        client=client,
        goal="hello",
        tools=sentinel,
        system_instruction="sys",
        dispatch=dispatch,
        max_turns=3,
    )

    assert len(client.calls) == 1
    assert client.calls[0]["tools"] is sentinel, "tools must be forwarded unchanged"
    assert client.calls[0]["system_instruction"] == "sys"
    # First turn sees only the seeded user message.
    assert client.calls[0]["contents"] == [{"role": "user", "content": "hello"}]
    assert isinstance(result, LoopResult)


@pytest.mark.asyncio
async def test_contents_record_user_assistant_and_tool_entries():
    calls_in = [{"name": "t", "args": {"x": 1}, "id": "abc"}]
    turns = [
        _Turn(text="thinking", calls=calls_in),
        _Turn(text="all done", calls=[]),
    ]
    client = FakeLLMClient(turns)
    dispatch, _ = _dispatcher_recording({"t": "tool-result"})

    result = await run_tool_loop(
        client=client,
        goal="do thing",
        tools=None,
        system_instruction=None,
        dispatch=dispatch,
        max_turns=5,
    )

    assert result.contents == [
        {"role": "user", "content": "do thing"},
        {
            "role": "assistant",
            "content": "thinking",
            "tool_calls": [{"name": "t", "args": {"x": 1}, "id": "abc"}],
        },
        {
            "role": "tool",
            "tool_call_id": "abc",
            "name": "t",
            "content": "tool-result",
        },
        {"role": "assistant", "content": "all done"},
    ]
    # The list returned by ``extract_function_calls`` is forwarded into the
    # assistant message verbatim; the loop must not copy or rewrap the dicts.
    # ``FakeLLMClient.extract_function_calls`` returns ``list(response.calls)`` —
    # a fresh list whose elements are the original dicts. The loop must preserve
    # the per-element identity (it is allowed to wrap them in its own list).
    forwarded = result.contents[1]["tool_calls"]
    assert forwarded == calls_in
    assert forwarded[0] is calls_in[0], (
        "loop must forward function-call dicts by identity, not by copy"
    )


@pytest.mark.asyncio
async def test_loop_with_zero_max_turns_yields_empty_result(caplog):
    client = FakeLLMClient([])
    dispatch, _ = _dispatcher_recording({})

    with caplog.at_level("WARNING", logger="devops_bench.models.utils.loop"):
        result = await run_tool_loop(
            client=client,
            goal="g",
            tools=None,
            system_instruction=None,
            dispatch=dispatch,
            max_turns=0,
        )

    assert result.final_text == ""
    assert result.latency == 0.0
    assert result.response is None
    assert result.contents == [{"role": "user", "content": "g"}]
    # `for ... else` runs `else` when the range is empty too.
    assert any("turn limit (0)" in rec.message for rec in caplog.records)


def test_loop_import_pulls_no_provider_sdk():
    """Importing the loop primitive must not drag in provider SDKs.

    Runs in a fresh interpreter subprocess so prior imports in the pytest
    process (the adapter SDKs are imported by ``test_models_*`` siblings) do
    not mask a real import-graph leak.
    """
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent(
        """
        import sys
        import devops_bench.models.utils.loop  # noqa: F401

        forbidden = ("google.genai", "anthropic", "openai", "ollama")
        leaked = sorted(
            m for m in sys.modules
            if any(m == p or m.startswith(p + ".") for p in forbidden)
        )
        if leaked:
            sys.stderr.write("LEAKED:" + ",".join(leaked))
            sys.exit(1)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"provider SDK leaked into devops_bench.models.utils.loop: {result.stderr}"
    )
