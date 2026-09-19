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

"""Per-run telemetry every agent parser reports the same way."""

from __future__ import annotations

import dataclasses

from devops_bench.agents.result import AgentResult, TerminalReason

__all__ = ["ParsedRun", "int_or_none", "note_model"]


@dataclasses.dataclass(slots=True)
class ParsedRun:
    """What one agent transcript yielded; fields follow :class:`AgentResult`.

    ``tool_wait_sec`` is a lower bound: a call whose two envelopes are not both
    timestamped counts for nothing. ``terminal_reason`` is ``""`` unless the
    transcript said why the run stopped; only the Claude CLI does.
    """

    output: str = ""
    trajectory: list[dict] = dataclasses.field(default_factory=list)
    tokens: dict = dataclasses.field(default_factory=dict)
    errors: list[str] = dataclasses.field(default_factory=list)
    tool_wait_sec: float | None = None
    served_models: list[str] = dataclasses.field(default_factory=list)
    model_turns: int | None = None
    terminal_reason: TerminalReason = ""

    def to_result(
        self,
        *,
        latency: float,
        terminal_reason: TerminalReason,
        output: str | None = None,
        errors: list[str] | None = None,
        metadata: dict | None = None,
    ) -> AgentResult:
        """Carry this run's telemetry onto an :class:`AgentResult`.

        Overrides win over the parsed values. ``terminal_reason`` is always the
        harness's call: only it knows whether it killed the process.
        """
        return AgentResult(
            output=self.output if output is None else output,
            trajectory=self.trajectory,
            tokens=self.tokens,
            latency=latency,
            errors=self.errors if errors is None else errors,
            terminal_reason=terminal_reason,
            tool_wait_sec=self.tool_wait_sec,
            served_models=self.served_models,
            model_turns=self.model_turns,
            metadata=metadata or {},
        )


def int_or_none(value: object) -> int | None:
    """Coerce to ``int``, rejecting ``bool`` (a JSON ``true`` is not a count)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def note_model(served_models: list[str], value: object) -> None:
    """Append ``value`` to ``served_models`` in place if it is a new model id."""
    if isinstance(value, str) and value and value not in served_models:
        served_models.append(value)
