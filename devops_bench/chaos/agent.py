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

"""LLM-driven orchestration loop for injecting chaos faults."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any

from devops_bench.core import first_env, get_logger
from devops_bench.models import LLMClient, get_model
from devops_bench.models.utils.loop import LoopResult, run_tool_loop

__all__ = ["ChaosAgent", "ToolHandler"]

_log = get_logger("chaos.agent")

# Safety bound on the agent loop so a misbehaving model cannot spin forever.
_MAX_TURNS = 8

#: Signature of a chaos command handler: ``(command, chaos_active_event) -> str``.
#: Concrete faults implement this and pass it to :class:`ChaosAgent` — see
#: :func:`devops_bench.chaos.faults.generate_load.run_chaos_command`.
ToolHandler = Callable[[str, threading.Event | None], str]


class ChaosAgent:
    """Drives an :class:`LLMClient` through a tool-calling loop to inject chaos.

    Args:
        system_instruction: System prompt for the loop; supplied by the fault.
        tool: Neutral tool descriptor (duck-typed: ``.name`` plus the fields
            ``LLMClient.format_tools`` consumes). Supplied by the fault.
        tool_handler: ``(command, chaos_active_event) -> str`` callable. The
            fault implements this; commonly
            :func:`~devops_bench.chaos.faults.generate_load.run_chaos_command`.
        chaos_active_event: Optional :class:`threading.Event` the handler may
            set when a disruption is observably active (e.g. load is flowing);
            forwarded unchanged.
        client: Optional pre-built LLM client. When omitted one is selected
            via ``first_env("CHAOS_PROVIDER","AGENT_PROVIDER")`` /
            ``first_env("CHAOS_MODEL","AGENT_MODEL")``.
        max_turns: Override for the safety turn cap.
    """

    def __init__(
        self,
        system_instruction: str,
        tool: Any,
        tool_handler: ToolHandler,
        chaos_active_event: threading.Event | None = None,
        client: LLMClient | None = None,
        max_turns: int = _MAX_TURNS,
    ) -> None:
        if client is None:
            provider = first_env("CHAOS_PROVIDER", "AGENT_PROVIDER")
            model_name = first_env("CHAOS_MODEL", "AGENT_MODEL")
            client = get_model(provider=provider, model_name=model_name)
        self._client = client
        self._system_instruction = system_instruction
        self._tool = tool
        self._tool_handler = tool_handler
        self._chaos_active_event = chaos_active_event
        self._max_turns = max_turns

    def run(self, goal: str) -> str:
        """Run the chaos loop synchronously and return the model's final text.

        Args:
            goal: The planned-mode goal prompt for the model.

        Returns:
            The model's final text response.
        """
        return asyncio.run(self._run_async(goal)).final_text

    async def _run_async(self, goal: str) -> LoopResult:
        """Drive :func:`run_tool_loop` with the fault-supplied tool/handler."""
        tools = self._client.format_tools([self._tool])
        return await run_tool_loop(
            client=self._client,
            goal=goal,
            tools=tools,
            system_instruction=self._system_instruction,
            dispatch=self._dispatch,
            max_turns=self._max_turns,
        )

    async def _dispatch(self, name: str, args: Any, call_id: str | None) -> str:
        """Adapt :data:`~devops_bench.models.utils.loop.ToolDispatcher` to the handler.

        :func:`run_tool_loop` calls this once per function call. We forward to
        the fault's handler, contributing the only chaos-specific glue (the
        active-event flag) and reject malformed args / unknown tool names with
        a descriptive error string so the model sees the failure instead of
        silently looping.

        Args:
            name: Tool name requested by the model.
            args: Raw arguments from the model.
            call_id: Provider-supplied call id (unused; surfaced by the loop).

        Returns:
            The handler's textual result, or an ``"Error: ..."`` description.
        """
        if not isinstance(args, dict):
            return "Error: tool args must be an object"
        expected = getattr(self._tool, "name", None)
        if name != expected:
            return f"Error: unknown tool {name!r}"
        command = args.get("command", "")
        if not isinstance(command, str):
            return "Error: command must be a string"
        return self._tool_handler(command, self._chaos_active_event)
