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

"""ChaosAgent tests with a fake LLMClient (no SDK / no network).

These cover the agent's three responsibilities: drive
:func:`run_tool_loop` correctly via the fault-supplied tool descriptor and
handler; reject malformed tool args / unknown tool names with a descriptive
error string; and retain the model's final text across the turn cap.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from devops_bench.chaos.agent import ChaosAgent
from devops_bench.models.base import LLMClient


class _ScriptedClient(LLMClient):
    """Replays a scripted sequence of (text, function_calls) tuples per turn."""

    def __init__(self, script: list[tuple[str, list[dict[str, Any]]]]) -> None:
        self._script = list(script)
        self.format_tools_called_with: Any = None
        self.contents_log: list[list[dict[str, Any]]] = []

    async def generate_content(
        self, contents: list[dict[str, Any]], tools: Any, system_instruction: str | None
    ) -> Any:
        # Snapshot what the loop has accumulated so tests can pin the message
        # history shape per CONVENTIONS §5.
        self.contents_log.append([dict(m) for m in contents])
        text, calls = self._script.pop(0)
        return SimpleNamespace(_text=text, _calls=calls)

    def format_tools(self, mcp_tools: Any) -> Any:
        self.format_tools_called_with = list(mcp_tools)
        return ("formatted", tuple(getattr(t, "name", "?") for t in mcp_tools))

    def extract_function_calls(self, response: Any) -> list[dict[str, Any]]:
        return list(response._calls)

    def get_text_content(self, response: Any) -> str:
        return response._text


_TOOL = SimpleNamespace(
    name="run_command",
    description="run",
    inputSchema={"type": "object"},
)


def _handler_returning(
    text: str,
) -> tuple[Callable[[str, threading.Event | None], str], list[tuple[str, threading.Event | None]]]:
    seen: list[tuple[str, threading.Event | None]] = []

    def _handler(command: str, event: threading.Event | None) -> str:
        seen.append((command, event))
        if event is not None:
            event.set()
        return text

    return _handler, seen


def test_agent_runs_one_turn_when_model_emits_no_tool_calls() -> None:
    client = _ScriptedClient([("done, nothing to do", [])])
    handler, _ = _handler_returning("unused")
    agent = ChaosAgent(
        system_instruction="be safe",
        tool=_TOOL,
        tool_handler=handler,
        client=client,
    )

    out = agent.run("goal")

    assert out == "done, nothing to do"
    # CONVENTIONS §5: format_tools is called by the agent before run_tool_loop.
    assert client.format_tools_called_with == [_TOOL]


def test_agent_dispatches_tool_calls_and_returns_final_text() -> None:
    client = _ScriptedClient(
        [
            (
                "running fortio now",
                [
                    {
                        "name": "run_command",
                        "args": {"command": "fortio load -qps 50 http://x"},
                        "id": "c1",
                    }
                ],
            ),
            ("done; saturated at 50 qps", []),
        ]
    )
    handler, seen = _handler_returning("Stdout: ok")
    event = threading.Event()
    agent = ChaosAgent(
        system_instruction="be safe",
        tool=_TOOL,
        tool_handler=handler,
        chaos_active_event=event,
        client=client,
    )

    out = agent.run("planned spike")

    assert out == "done; saturated at 50 qps"
    # Handler observed the model's command + the same event we passed in.
    assert seen == [("fortio load -qps 50 http://x", event)]
    # Conversation acquired the assistant message + the tool result entry.
    final_contents = client.contents_log[-1]
    assert final_contents[0] == {"role": "user", "content": "planned spike"}
    assert final_contents[1]["role"] == "assistant"
    assert final_contents[1]["tool_calls"][0]["name"] == "run_command"
    assert final_contents[2] == {
        "role": "tool",
        "tool_call_id": "c1",
        "name": "run_command",
        "content": "Stdout: ok",
    }


def test_agent_returns_error_string_for_non_dict_tool_args() -> None:
    # Model fabricates a tool call with a list payload instead of a dict.
    client = _ScriptedClient(
        [
            ("calling tool", [{"name": "run_command", "args": ["nope"], "id": "c1"}]),
            ("acknowledged the error", []),
        ]
    )
    handler, seen = _handler_returning("never invoked")
    agent = ChaosAgent(
        system_instruction="x",
        tool=_TOOL,
        tool_handler=handler,
        client=client,
    )

    out = agent.run("goal")

    assert out == "acknowledged the error"
    assert seen == []  # handler must not run for malformed args
    tool_msg = client.contents_log[-1][-1]
    assert tool_msg["role"] == "tool"
    assert tool_msg["content"] == "Error: tool args must be an object"


def test_agent_returns_error_string_for_unknown_tool_name() -> None:
    client = _ScriptedClient(
        [
            ("calling fake tool", [{"name": "rm_rf", "args": {}, "id": "c1"}]),
            ("backed off", []),
        ]
    )
    handler, seen = _handler_returning("nope")
    agent = ChaosAgent(
        system_instruction="x",
        tool=_TOOL,
        tool_handler=handler,
        client=client,
    )

    out = agent.run("goal")

    assert out == "backed off"
    assert seen == []
    tool_msg = client.contents_log[-1][-1]
    assert tool_msg["content"] == "Error: unknown tool 'rm_rf'"


def test_agent_retains_final_text_when_turn_cap_hits_with_tool_call() -> None:
    # Loop the model forever requesting a tool; the cap should stop the loop
    # but the most recent text must be preserved (the regression fix in §5.1).
    script: list[tuple[str, list[dict[str, Any]]]] = []
    for i in range(20):
        script.append(
            (
                f"step {i}",
                [{"name": "run_command", "args": {"command": f"echo {i}"}, "id": f"c{i}"}],
            )
        )
    client = _ScriptedClient(script)
    handler, seen = _handler_returning("ok")
    agent = ChaosAgent(
        system_instruction="x",
        tool=_TOOL,
        tool_handler=handler,
        client=client,
        max_turns=3,
    )

    out = agent.run("goal")

    # Final text comes from the third (capped) turn.
    assert out == "step 2"
    assert len(seen) == 3


def test_agent_returns_error_string_when_command_key_is_missing() -> None:
    # Model emits the right tool but forgets the ``command`` key entirely.
    # The dispatcher must not crash — it forwards an empty command to the
    # handler, which surfaces it as an "Error: ..." string the model can read.
    client = _ScriptedClient(
        [
            ("calling tool wrong", [{"name": "run_command", "args": {}, "id": "c1"}]),
            ("fixed up", []),
        ]
    )
    handler, seen = _handler_returning("never invoked")
    agent = ChaosAgent(
        system_instruction="x",
        tool=_TOOL,
        tool_handler=handler,
        client=client,
    )

    out = agent.run("goal")

    assert out == "fixed up"
    # Handler IS invoked (with an empty command) — the actual fault handler
    # decides whether to error; this asserts the dispatcher never crashes.
    assert seen == [("", None)]
    tool_msg = client.contents_log[-1][-1]
    assert tool_msg["role"] == "tool"
    # The handler returned its scripted text since this test stubs it; the
    # production handler (run_chaos_command) returns "Error: command string is
    # empty" — covered in test_generate_load.test_run_chaos_command_rejects_empty_command.
    assert tool_msg["content"] == "never invoked"


def test_agent_rejects_non_string_command() -> None:
    # Model passes a null command; the guard must bounce it before the handler.
    client = _ScriptedClient(
        [
            ("calling tool", [{"name": "run_command", "args": {"command": None}, "id": "c1"}]),
            ("gave up", []),
        ]
    )
    handler, seen = _handler_returning("never invoked")
    agent = ChaosAgent(
        system_instruction="x",
        tool=_TOOL,
        tool_handler=handler,
        client=client,
    )

    out = agent.run("goal")

    assert out == "gave up"
    assert seen == []  # handler must not run for a non-string command
    tool_msg = client.contents_log[-1][-1]
    assert tool_msg["content"] == "Error: command must be a string"
