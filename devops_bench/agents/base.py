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
``@AGENTS.register``. Heavy imports (``deepeval``, provider SDKs) stay
function-local — ``import devops_bench.agents`` pulls only this module.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod

from devops_bench.agents.config import AgentConfig
from devops_bench.agents.result import AgentResult
from devops_bench.core import Registry, get_logger

__all__ = ["AgentHarness", "AGENTS"]

AGENTS: Registry[type[AgentHarness]] = Registry("agents")

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

    def run(self, prompt: str) -> AgentResult:
        """Execute the agent against ``prompt`` and return a typed result.

        Template method: wraps :meth:`_execute` in the latency stamp and the
        safety net. ``agent.run(prompt) -> AgentResult`` is the only entry point
        the harness calls.

        Args:
            prompt: Task prompt handed to the agent.

        Returns:
            An :class:`AgentResult` with ``latency`` always populated. A
            subclass crash produces ``AgentResult.errored(msg)``.
        """
        traced = _maybe_observe(self._execute)
        start = time.monotonic()
        try:
            result = traced(prompt)
        except Exception as exc:  # noqa: BLE001 - safety net for the whole benchmark
            elapsed = time.monotonic() - start
            _log.exception("agent _execute raised; converting to errored result")
            return AgentResult.errored(f"{type(exc).__name__}: {exc}", latency=elapsed)

        elapsed = time.monotonic() - start
        # Trust _execute when it already stamped latency (e.g. it has finer
        # timing for a sub-step it wants surfaced); only fill in when zero.
        if not result.latency:
            result.latency = elapsed
        return result

    @abstractmethod
    def _execute(self, prompt: str) -> AgentResult:
        """Run the agent and return its typed result.

        Subclass extension point. Implementations build the provider-specific
        invocation, parse the output into the canonical trajectory, and return
        an :class:`AgentResult`. Subclasses handle their own *known* errors by
        populating ``AgentResult.errors``; the base's safety net catches only
        unexpected exceptions.

        Args:
            prompt: Task prompt handed to the agent.

        Returns:
            An :class:`AgentResult` (``latency`` may be left zero — the base
            fills it in).
        """


def _maybe_observe(func):
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
