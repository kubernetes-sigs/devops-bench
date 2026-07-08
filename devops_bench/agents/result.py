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

__all__ = ["ToolCall", "AgentResult"]


@dataclass
class ToolCall:
    """Canonical trajectory entry emitted by every agent.

    Attributes:
        name: Tool name as advertised by the agent (e.g. an MCP tool name).
        args: Tool arguments as a JSON-serializable mapping.
        result: Tool output text once the tool returns; ``None`` until then.
        status: Lifecycle marker — ``"called"`` when first emitted,
            ``"completed"`` once the result is folded in, ``"error"`` when the
            tool failed.
    """

    name: str
    args: dict
    result: str | None = None
    status: str = "called"

    def to_dict(self) -> dict:
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
        trajectory: Ordered list of ``ToolCall.to_dict()`` entries (optionally
            interleaved with text turns by API agents). Every agent emits the
            same canonical entry shape so metrics consume one schema.
        tokens: Provider-reported token usage (shape is provider-defined; pass
            through verbatim).
        latency: Total wall-clock seconds spent inside the agent run, stamped
            by :meth:`AgentHarness.run`.
        errors: Human-readable error or extraction-failure messages. **Empty**
            on a clean run; populated when a known-error path (subprocess
            failure, parse miss, timeout) is reached — never silently dropped.
        metadata: Agent-specific extras (e.g. raw provider stats, session ids)
            that do not fit the typed fields above.
    """

    output: str
    trajectory: list[dict]
    tokens: dict = field(default_factory=dict)
    latency: float = 0.0
    errors: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
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
            "metadata": dict(self.metadata),
        }

    def has_errors(self) -> bool:
        """Return ``True`` when at least one error was recorded.

        The metrics layer uses this to distinguish a real model run that
        finished with empty output from one that aborted mid-flight.
        """
        return bool(self.errors)

    @classmethod
    def errored(cls, msg: str, *, latency: float = 0.0) -> AgentResult:
        """Build a result representing a failed run.

        Args:
            msg: Error message to surface on :attr:`errors` and ``output``.
            latency: Elapsed seconds before the failure, when available.

        Returns:
            An :class:`AgentResult` with empty trajectory and the message in
            both ``output`` and ``errors``.
        """
        return cls(output=f"Error: {msg}", trajectory=[], latency=latency, errors=[msg])
