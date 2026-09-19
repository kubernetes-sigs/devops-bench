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

import os
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from devops_bench.agents.config import AgentConfig
from devops_bench.agents.result import AgentResult
from devops_bench.core import Registry, SandboxError, get_logger
from devops_bench.core.subprocess import CompletedProcess
from devops_bench.core.subprocess import run as _host_subprocess_run

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

    #: Whether every agent-owned subprocess in this harness goes through
    #: :meth:`run_agent_cmd`. :meth:`run` refuses a sandboxed config on a
    #: harness that has not been migrated onto the seam: its direct
    #: ``run(...)`` calls would execute on the host with the operator's
    #: ambient credentials while the operator believes the run is contained.
    #: A subclass flips this to ``True`` only once all its call sites are on
    #: the seam.
    supports_sandbox: bool = False

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

        Raises:
            SandboxError: When the run is sandboxed but this harness has not
                been migrated onto the :meth:`run_agent_cmd` seam, or when
                the executor itself refuses mid-run. Deliberately *not*
                converted to an errored result: a containment failure is an
                infrastructure failure, not an agent performance, and the
                eval harness records it as a failed, unscored run.
        """
        if self.config.sandbox is not None and not self.supports_sandbox:
            raise SandboxError(
                f"agent harness {type(self).__name__} has not been migrated onto the "
                "sandbox seam (run_agent_cmd); refusing to run it unsandboxed on the "
                "host while BENCH_AGENT_SANDBOX is set"
            )
        start = time.monotonic()
        try:
            traced = _maybe_observe(self._execute)
            result = traced(prompt, workspace_path)
            elapsed = time.monotonic() - start
            # Trust _execute when it already stamped latency (e.g. it has finer
            # timing for a sub-step it wants surfaced); only fill in when zero.
            if not result.latency:
                result.latency = elapsed
            return result
        except SandboxError:
            # Never swallowed into an errored result: that would score a broken
            # boundary as a badly-performing agent. Propagates to the eval
            # harness's failed-record path instead.
            raise
        except Exception as exc:  # noqa: BLE001 - safety net for the whole benchmark
            elapsed = time.monotonic() - start
            _log.exception("agent _execute raised; converting to errored result")
            return AgentResult.errored(f"{type(exc).__name__}: {exc}", latency=elapsed)

    def run_agent_cmd(
        self,
        cmd: Sequence[str | os.PathLike[str]],
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        extra_env: Mapping[str, str] | None = None,
        check: bool = True,
        capture: bool = True,
        text: bool = True,
        timeout: float | None = None,
        input: str | None = None,
        host_run: Callable[..., CompletedProcess] | None = None,
    ) -> CompletedProcess:
        """Run an agent-owned command through the sandbox seam.

        This is the one dispatch point that decides whether the agent binary
        (and anything else run on its behalf, e.g. a post-run trajectory
        export) executes on the host or inside the sandbox container. When
        ``config.sandbox`` is set the command is wrapped by
        :class:`~devops_bench.agents.sandbox.SandboxExecutor`; otherwise it is
        handed through unchanged — same arguments, same defaults, same return
        shape as :func:`devops_bench.core.subprocess.run` — so with the flag
        off a call site swapped onto this method behaves byte-for-byte as its
        direct ``run(...)`` call did.

        A sandbox that cannot run raises ``SandboxError`` rather than falling
        back to the host: a containment control that quietly degrades is
        worse than none. :meth:`run`'s safety net deliberately re-raises it
        (instead of converting to an errored result) so the eval harness
        records a failed, unscored run — a broken boundary must never read
        as a badly-performing agent.

        Args:
            cmd: Command and arguments, never a shell string.
            cwd: Working directory; inside the sandbox it must lie under the
                run workspace (it is remapped to the container path).
            env: Full-environment replacement. Only meaningful on the host
                path; the sandbox rejects it rather than forwarding a whole
                host environment across the boundary.
            extra_env: The resolved per-run overlay. On the host path it is
                overlaid on the process env; in the sandbox it is the *only*
                environment that crosses, by value, after the deny filter.
            check / capture / text / timeout / input: As in
                ``core.subprocess.run``. ``input`` is rejected in the sandbox
                (the container runs without stdin, by design).
            host_run: Callable used on the unsandboxed path, defaulting to
                ``core.subprocess.run``. Concrete agents pass their own
                module-level ``run`` import so that symbol stays the seam
                their unit tests already patch.

        Returns:
            The completed process, in either mode.
        """
        if self.config.sandbox is not None:
            # Function-local import: the sandbox module is only needed once a
            # run actually opted in, and this keeps ``import
            # devops_bench.agents`` byte-identical for everyone else.
            from devops_bench.agents.sandbox import SandboxExecutor

            return SandboxExecutor(self.config.sandbox).run(
                cmd,
                cwd=cwd,
                env=env,
                extra_env=extra_env,
                check=check,
                capture=capture,
                text=text,
                timeout=timeout,
                input=input,
            )
        runner = host_run if host_run is not None else _host_subprocess_run
        return runner(
            cmd,
            cwd=cwd,
            env=env,
            extra_env=extra_env,
            check=check,
            capture=capture,
            text=text,
            timeout=timeout,
            input=input,
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
    func: Callable[[str, Path | None], AgentResult],
) -> Callable[[str, Path | None], AgentResult]:
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
