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
from typing import Any

__all__: list[str] = [
    "ROOT_ACTOR",
    "SUBAGENT_ACTOR",
    "AgentResult",
    "TOKEN_BUCKETS",
    "ToolCall",
    "empty_tokens",
]

# Canonical token buckets every harness maps onto: ``input`` is the non-cached
# prompt, ``cached`` is cache-read only (cache writes go in ``cache_write``),
# ``output`` excludes ``reasoning``, and ``total`` is the sum of all buckets.
TOKEN_BUCKETS: tuple[str, ...] = ("input", "cached", "cache_write", "reasoning", "output", "total")

#: :attr:`ToolCall.actor` value for a call the top-level agent made itself.
ROOT_ACTOR = "root"

#: :attr:`ToolCall.actor` fallback for a call made by a delegated agent the
#: harness could not name (the spawning call carried no recognizable label).
SUBAGENT_ACTOR = "subagent"


def empty_tokens() -> dict[str, int | None]:
    """Return the canonical token dict with every bucket ``None`` (unavailable)."""
    return dict.fromkeys(TOKEN_BUCKETS, None)


@dataclass
class ToolCall:
    """Canonical trajectory entry emitted by every agent.

    The three attribution fields (``actor`` / ``call_id`` / ``parent_id``) carry
    *which* agent in a fleet made the call. A single-agent harness leaves them
    unset and they are omitted from :meth:`to_dict`, so its trajectory
    serializes exactly as it did before the fields existed — the metrics layer
    re-serializes the trajectory into judge prompts, so a key that appeared on
    every entry would perturb the scores of runs that have no fleet at all.

    Attributes:
        name: Tool name as advertised by the agent (e.g. an MCP tool name).
        args: Tool arguments as a JSON-serializable mapping.
        result: Tool output text once the tool returns; ``None`` until then.
        status: Lifecycle marker — ``"called"`` when first emitted,
            ``"completed"`` once the result is folded in, ``"error"`` when the
            tool failed.
        actor: Label for the agent that made the call — :data:`ROOT_ACTOR` for
            the top-level agent, otherwise the delegated agent's role name (a
            harness-defined string, e.g. ``"cluster"``). ``None`` when the
            harness reports no delegation.
        call_id: The agent's own id for this call, when it exposes one. Lets a
            child's :attr:`parent_id` resolve back to the call that spawned it.
        parent_id: :attr:`call_id` of the delegating call this one was made
            *inside*. ``None`` for a top-level call.
    """

    name: str
    args: dict[str, Any]
    result: str | None = None
    status: str = "called"
    actor: str | None = None
    call_id: str | None = None
    parent_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable mapping the harness writes to disk.

        Unset attribution fields are omitted rather than written as ``None``;
        see the class docstring for why.
        """
        entry: dict[str, Any] = {
            "name": self.name,
            "args": self.args,
            "result": self.result,
            "status": self.status,
        }
        for key, value in (
            ("actor", self.actor),
            ("call_id", self.call_id),
            ("parent_id", self.parent_id),
        ):
            if value is not None:
                entry[key] = value
        return entry


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
    trajectory: list[dict[str, Any]]
    tokens: dict[str, Any] = field(default_factory=dict)
    latency: float = 0.0
    errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

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
        )
