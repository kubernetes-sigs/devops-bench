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

    Attributes:
        output: The agent's final answer text; ``""`` when none was found.
        trajectory: ``ToolCall.to_dict()`` mappings, in emission order.
        tokens: Canonical token buckets summed over the run.
        errors: Decode failures, unmatched tool results, and any failure the
            transcript itself reported.
        tool_wait_sec: Best-effort lower bound — a call whose two envelopes are
            not both timestamped counts for nothing.
        served_models: Distinct model ids the provider answered with.
        model_turns: Model round-trips, or ``None`` when the transcript carried
            nothing to count.
        terminal_reason: ``""`` unless the transcript itself said why the run
            stopped; only the Claude CLI does.
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

        Args:
            latency: Wall-clock seconds of the agent turn.
            terminal_reason: Why the run stopped (the harness's verdict).
            output: Optional override for :attr:`output`.
            errors: Optional override for :attr:`errors`.
            metadata: Harness-specific metadata mapping.

        Returns:
            An :class:`AgentResult` populated from this parsed run.
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
    """Coerce ``value`` to ``int``, rejecting ``bool``.

    Args:
        value: Candidate count from a transcript payload.

    Returns:
        ``value`` when it is a non-boolean ``int``, else ``None``.
    """
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def note_model(served_models: list[str], value: object) -> None:
    """Append ``value`` to ``served_models`` in place if it is a new model id.

    Args:
        served_models: List to append to, mutated in place.
        value: The transcript's model field, or anything else. Non-strings, the
            empty string, and ids already recorded are ignored.
    """
    if isinstance(value, str) and value and value not in served_models:
        served_models.append(value)
