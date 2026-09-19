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

"""Typed agent results and the canonical trajectory entry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, get_args

__all__: list[str] = [
    "AgentResult",
    "TERMINAL_REASONS",
    "TerminalReason",
    "TOKEN_BUCKETS",
    "ToolCall",
    "empty_tokens",
]

# Canonical token buckets every harness maps onto: ``input`` is the non-cached
# prompt, ``cached`` is cache-read only (cache writes go in ``cache_write``),
# ``output`` excludes ``reasoning``, and ``total`` is the sum of all buckets.
TOKEN_BUCKETS: tuple[str, ...] = ("input", "cached", "cache_write", "reasoning", "output", "total")

#: Why a run stopped, as the *harness* saw it. ``""`` is unreported, not ``completed``.
TerminalReason = Literal["", "completed", "timeout", "error"]
TERMINAL_REASONS: tuple[TerminalReason, ...] = get_args(TerminalReason)


def empty_tokens() -> dict[str, int | None]:
    """Return the canonical token dict with every bucket ``None`` (unavailable)."""
    return dict.fromkeys(TOKEN_BUCKETS, None)


@dataclass
class ToolCall:
    """Canonical trajectory entry emitted by every agent.

    Attributes:
        name: Tool name as advertised by the agent (e.g. an MCP tool name).
        args: Tool arguments as a JSON-serializable mapping.
        result: Tool output text once the tool returns; ``None`` until then.
        status: ``"called"``, ``"completed"``, ``"error"``, or antigravity's
            ``"interrupted"`` — the same unresolved condition as ``"called"``,
            so neither counts as a tool error.
    """

    name: str
    args: dict[str, Any]
    result: str | None = None
    status: str = "called"

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable mapping the harness writes to disk."""
        return {
            "name": self.name,
            "args": self.args,
            "result": self.result,
            "status": self.status,
        }


@dataclass
class AgentResult:
    """Outcome of a single agent invocation.

    Attributes:
        output: Final assistant text the judge grades.
        trajectory: Ordered list of ``ToolCall.to_dict()`` entries. Every agent
            emits the same canonical entry shape so metrics consume one schema.
        tokens: Provider-reported token usage (shape is provider-defined; pass
            through verbatim).
        latency: Wall-clock seconds of the agent turn. A harness that can bracket
            it more tightly stamps it in ``_execute``; :meth:`AgentHarness.run`
            backfills the whole-run elapsed only when it was left at zero.
        errors: Human-readable error or extraction-failure messages. **Empty**
            on a clean run; populated when a known-error path (subprocess
            failure, parse miss, timeout) is reached — never silently dropped.
        terminal_reason: One of :data:`TERMINAL_REASONS`; ``ValueError`` otherwise.
        tool_wait_sec: Seconds inside tool calls, concurrent calls counted once;
            ``None`` when the transcript carried no timings.
        served_models: Model ids that actually answered, first-seen order. Config
            names the *requested* model, which an alias or failover can redirect.
        model_turns: Model calls, or ``None`` when the harness cannot tell. Not
            ``len(trajectory)``: one turn can issue several tool calls or none.
        metadata: Agent-specific extras (e.g. raw provider stats, session ids)
            that do not fit the typed fields above.
    """

    output: str
    trajectory: list[dict[str, Any]]
    tokens: dict[str, Any] = field(default_factory=dict)
    latency: float = 0.0
    errors: list[str] = field(default_factory=list)
    terminal_reason: TerminalReason = ""
    tool_wait_sec: float | None = None
    served_models: list[str] = field(default_factory=list)
    model_turns: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # An unrecognized reason matches no dashboard grouping and is dropped there.
        if self.terminal_reason not in TERMINAL_REASONS:
            raise ValueError(
                f"terminal_reason must be one of {TERMINAL_REASONS}, got {self.terminal_reason!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable mapping consumed by the harness.

        Container fields are shallow copies: mutating the returned dict's lists
        does not leak back into this :class:`AgentResult`.

        >>> r = AgentResult(output="ok", trajectory=[{"name": "ls"}])
        >>> snapshot = r.to_dict()
        >>> snapshot["trajectory"].append({"name": "rm"})
        >>> r.trajectory
        [{'name': 'ls'}]
        """
        return {
            "output": self.output,
            "trajectory": list(self.trajectory),
            "tokens": dict(self.tokens),
            "latency": self.latency,
            "errors": list(self.errors),
            "terminal_reason": self.terminal_reason,
            "tool_wait_sec": self.tool_wait_sec,
            "served_models": list(self.served_models),
            "model_turns": self.model_turns,
            "metadata": dict(self.metadata),
        }

    def has_errors(self) -> bool:
        """Return ``True`` when at least one error was recorded.

        The metrics layer uses this to distinguish a real model run that
        finished with empty output from one that aborted mid-flight.
        """
        return bool(self.errors)

    @classmethod
    def errored(
        cls, msg: str, *, latency: float = 0.0, terminal_reason: str = "error"
    ) -> AgentResult:
        """Build a result representing a failed run.

        Args:
            msg: Error message to surface on :attr:`errors` and ``output``.
            latency: Elapsed seconds before the failure, when available.
            terminal_reason: Pass ``"timeout"`` when the harness cut the run off
                at its budget — a different signal from the agent failing.

        Returns:
            An :class:`AgentResult` with empty trajectory, the canonical
            all-``None`` token shape, and the message in both ``output`` and
            ``errors``.
        """
        return cls(
            output=f"Error: {msg}",
            trajectory=[],
            tokens=empty_tokens(),
            latency=latency,
            errors=[msg],
            terminal_reason=terminal_reason,
        )
