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

"""Agent-under-test interface and the agent-selection registry.

This module defines the template-method :class:`AgentHarness` consumed by every
concrete agent. The base owns latency bookkeeping and a broad safety net so a
single agent crash never aborts the benchmark. Subclasses implement
:meth:`AgentHarness._execute` to do the provider-specific work and return an
:class:`AgentResult`.

Each concrete harness lives in a sibling subpackage (``cli.gemini_cli`` /
``cli.openclaw``) and self-registers under its canonical key via
``@AGENTS.register``. External packages register theirs through the
``devops_bench.agents`` entry-point group instead, so a downstream harness
resolves by key with no import of its module here. Keys on both paths must be
lowercase — the harness lowercases the configured agent type before lookup — so
an uppercase one is rejected at registration rather than left unreachable.
Heavy imports (``deepeval``, provider SDKs) stay function-local — ``import
devops_bench.agents`` pulls only this module.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from devops_bench.agents.config import AgentConfig
from devops_bench.agents.result import AgentResult
from devops_bench.core import Registry, get_logger

__all__ = ["AgentHarness", "AGENTS"]


def _reject_non_lowercase_key(key: str) -> str | None:
    """Reject an agent key that a configured agent type could never match.

    The harness lowercases the configured agent type before looking it up, so a
    key carrying any uppercase character is unreachable — and the failure is
    silent in the worst way: the configured name shows up verbatim in the
    ``available:`` list of the resulting :class:`NotRegisteredError`. Rejecting
    at registration turns that into an actionable message at the point the key
    is introduced.

    Args:
        key: Candidate registry key.

    Returns:
        None when ``key`` is acceptable, else the reason it was rejected.
    """
    if key != key.lower():
        return "agent keys must be lowercase; the configured agent type is lowercased before lookup"
    return None


#: Registry of concrete :class:`AgentHarness` subclasses, keyed by agent type.
#: ``entry_point_group`` lets external packages register a harness without
#: touching this tree; the key policy holds those external keys to the same
#: lowercase contract the in-tree ones follow.
AGENTS: Registry[type[AgentHarness]] = Registry(
    "agents",
    entry_point_group="devops_bench.agents",
    key_validator=_reject_non_lowercase_key,
)

_log = get_logger("agents.base")


class AgentHarness(ABC):
    """Template-method base class for an agent driven during a benchmark run.

    The base owns three concerns common to every agent:

    1. **Latency bookkeeping** — :meth:`run` measures wall-clock seconds and
       stamps ``AgentResult.latency`` so subclasses never re-implement it.
    2. **Broad safety net** — any unexpected exception from :meth:`_execute`
       (including subclass bugs and provider SDK crashes) is caught and
       converted to ``AgentResult.errored(...)``; one agent fault never aborts
       the benchmark.
    3. **Optional tracing** — when ``deepeval`` is installed, the run is wrapped
       in an ``@observe`` span. The import stays function-local so the agents
       package can be imported on a host without ``deepeval``.

    Concrete subclasses live in sibling modules and self-register a canonical
    key via ``@AGENTS.register(...)``. They override :meth:`_execute` to build
    argv / drive the loop, run, parse, and return an :class:`AgentResult`. They
    handle their own *known* errors (subprocess failures, parse misses) by
    populating ``AgentResult.errors`` — the safety net is only for unexpected
    exceptions.

    Args:
        config: Typed configuration. ``None`` substitutes a default
            ``AgentConfig()`` (use the agent's built-in defaults).
    """

    def __init__(self, config: AgentConfig | None = None) -> None:
        self.config = config or AgentConfig()

    def run(self, prompt: str, workspace_path: Path | None = None) -> AgentResult:
        """Execute the agent against ``prompt`` and return a typed result.

        Template method: wraps :meth:`_execute` in the latency stamp and the
        safety net. ``agent.run(prompt) -> AgentResult`` is the only entry point
        the harness calls.

        Args:
            prompt: Task prompt handed to the agent.
            workspace_path: Harness-owned working directory the agent should
                execute in, when the harness supplies one (so files the agent
                writes can be diffed and collected afterward). ``None`` lets
                the agent fall back to its own throwaway working directory.

        Returns:
            An :class:`AgentResult` with ``latency`` always populated. A
            subclass crash produces ``AgentResult.errored(msg)``.
        """
        return self._guarded(_maybe_observe(self._execute), prompt, workspace_path)

    def run_turns(self, prompts: Sequence[str], workspace_path: Path | None = None) -> AgentResult:
        """Execute an ordered multi-turn conversation and return one result.

        The turns are a *single* conversation, not independent runs: the agent
        keeps whatever session state it maintains across them, and the whole
        exchange folds into one :class:`AgentResult`. That is what a remote
        agent needs in order to hold its own session — an A2A service keyed on
        ``ContextId``, for example, only stays on one context while the caller
        stays in one session.

        Args:
            prompts: Turn texts in the order they should be sent. A one-element
                sequence is exactly :meth:`run`.
            workspace_path: As :meth:`run`.

        Returns:
            An :class:`AgentResult` covering the whole conversation, with
            ``latency`` always populated.
        """
        return self._guarded(_maybe_observe(self._execute_turns), list(prompts), workspace_path)

    def _guarded(
        self,
        call: Callable[[Any, Path | None], AgentResult],
        payload: Any,
        workspace_path: Path | None,
    ) -> AgentResult:
        """Run one execution hook under the latency stamp and the safety net."""
        start = time.monotonic()
        try:
            result = call(payload, workspace_path)
            elapsed = time.monotonic() - start
            # Trust the hook when it already stamped latency (e.g. it has finer
            # timing for a sub-step it wants surfaced); only fill in when zero.
            if not result.latency:
                result.latency = elapsed
            return result
        except Exception as exc:  # noqa: BLE001 - safety net for the whole benchmark
            elapsed = time.monotonic() - start
            _log.exception("agent execution raised; converting to errored result")
            return AgentResult.errored(f"{type(exc).__name__}: {exc}", latency=elapsed)

    def _execute_turns(
        self, prompts: Sequence[str], workspace_path: Path | None = None
    ) -> AgentResult:
        """Run a multi-turn conversation; override to support more than one turn.

        The default is deliberately not a loop over :meth:`_execute`. Replaying
        a harness that holds no session would restart the conversation on every
        turn, and the agent would answer turn *n* having forgotten turns 1..n-1
        — a plausible-looking transcript that silently means nothing. A harness
        that cannot hold a session says so instead, and the task fails loudly.

        Args:
            prompts: Turn texts in order.
            workspace_path: As :meth:`_execute`.

        Returns:
            An :class:`AgentResult`; errored when this harness is single-turn
            and more than one turn was asked for.
        """
        if not prompts:
            return AgentResult.errored("no turns to run")
        if len(prompts) == 1:
            return self._execute(prompts[0], workspace_path)
        return AgentResult.errored(
            f"{type(self).__name__} is single-turn, but the task asked for "
            f"{len(prompts)} turns; it cannot hold a session across them"
        )

    @abstractmethod
    def _execute(self, prompt: str, workspace_path: Path | None = None) -> AgentResult:
        """Run the agent and return its typed result.

        Subclass extension point. Implementations build the provider-specific
        invocation, parse the output into the canonical trajectory, and return
        an :class:`AgentResult`. Subclasses handle their own *known* errors by
        populating ``AgentResult.errors``; the base's safety net catches only
        unexpected exceptions.

        Args:
            prompt: Task prompt handed to the agent.
            workspace_path: Harness-owned working directory, or ``None`` when
                the harness has not supplied one. A subclass with no local
                filesystem workspace (e.g. a pure API agent) may ignore it.

        Returns:
            An :class:`AgentResult` (``latency`` may be left zero — the base
            fills it in).
        """


def _maybe_observe(
    func: Callable[[Any, Path | None], AgentResult],
) -> Callable[[Any, Path | None], AgentResult]:
    """Return ``func`` wrapped in ``deepeval.tracing.observe`` when available.

    The wrap is performed once per ``run()`` call rather than at import time so
    the agents package can be imported on hosts without ``deepeval``. Import
    failures degrade gracefully — the run proceeds untraced.
    """
    try:
        from deepeval.tracing import observe
    except ImportError:
        return func
    return observe()(func)
