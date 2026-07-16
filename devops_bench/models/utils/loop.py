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

"""Shared model-agnostic tool-use loop for agents and chaos.

:func:`run_tool_loop` drives an :class:`~devops_bench.models.base.LLMClient`
through repeated ``generate_content`` turns until the model stops requesting
tools or a safety cap is reached. Messages use a neutral shape:

- user turn:      ``{"role": "user", "content": goal}``
- assistant turn: ``{"role": "assistant", "content": text[, "tool_calls": [...]]}``
- tool result:    ``{"role": "tool", "tool_call_id": id, "name": name, "content": text}``

A function call (an entry in ``tool_calls``) is
``{"name": ..., "args": ..., "id": ...}``.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from devops_bench.core import get_logger
from devops_bench.models.base import LLMClient

__all__ = ["LoopResult", "ToolDispatcher", "run_tool_loop"]

_log = get_logger("models.utils.loop")

#: Dispatch a tool call to its implementation.
#:
#: Called once per function call the model issues with ``(name, args, call_id)``
#: and must return the tool's textual result. Raising propagates out of
#: :func:`run_tool_loop` so the caller controls error handling.
ToolDispatcher = Callable[[str, Any, str | None], Awaitable[str]]


@dataclass
class LoopResult:
    """Outcome of a :func:`run_tool_loop` invocation.

    Attributes:
        response: The last raw provider response object (or ``None`` if the
            loop never ran a turn).
        contents: The full conversation history in the neutral message shape.
        final_text: The model's most recent text content, kept even when the
            final turn also requests tools.
        latency: Total seconds spent inside ``generate_content`` across turns.
        tools_used: Names of every tool the model requested.
    """

    response: Any
    contents: list[dict]
    final_text: str
    latency: float
    tools_used: set[str] = field(default_factory=set)


async def run_tool_loop(
    client: LLMClient,
    goal: str,
    tools: Any,
    system_instruction: str | None,
    dispatch: ToolDispatcher,
    max_turns: int,
) -> LoopResult:
    """Drive ``client`` through a tool-use loop until it stops or the cap hits.

    Args:
        client: Neutral LLM client.
        goal: The task prompt that seeds the conversation as the first user
            message.
        tools: Pre-formatted tool descriptors (see :meth:`LLMClient.format_tools`).
        system_instruction: Optional system prompt forwarded to every turn.
        dispatch: Async callable invoked for each tool call. Exceptions raised
            by ``dispatch`` propagate to the caller; the loop does not swallow
            them.
        max_turns: Safety cap on turns. Reaching the cap ends the loop with a
            warning rather than looping forever.

    Returns:
        A :class:`LoopResult` with the last response, full ``contents``,
        retained ``final_text``, accumulated ``latency``, and the set of
        ``tools_used``.
    """
    contents: list[dict] = [{"role": "user", "content": goal}]
    tools_used: set[str] = set()
    response: Any = None
    final_text = ""
    total_latency = 0.0

    for turn in range(max_turns):
        _log.debug("--- Turn %d ---", turn + 1)

        start = time.monotonic()
        response = await client.generate_content(contents, tools, system_instruction)
        total_latency += time.monotonic() - start

        # Guard against ``get_text_content`` returning ``None``.
        text = client.get_text_content(response) or ""
        function_calls = client.extract_function_calls(response)

        final_text = text

        assistant_message: dict[str, Any] = {"role": "assistant", "content": text}
        if function_calls:
            assistant_message["tool_calls"] = function_calls
        contents.append(assistant_message)

        if not function_calls:
            _log.debug("No further tool calls; loop finished.")
            break

        for call in function_calls:
            name = call["name"]
            args = call["args"]
            call_id = call.get("id")
            tools_used.add(name)

            result_text = await dispatch(name, args, call_id)
            contents.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": result_text,
                }
            )
    else:
        _log.warning("tool loop stopped after reaching the turn limit (%d)", max_turns)

    return LoopResult(
        response=response,
        contents=contents,
        final_text=final_text,
        latency=total_latency,
        tools_used=tools_used,
    )
