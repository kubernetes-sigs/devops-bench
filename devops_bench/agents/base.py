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

:class:`AgentHarness` is the template method every concrete agent implements via
:meth:`AgentHarness._execute`; the base owns latency bookkeeping and the safety
net that keeps one agent crash from aborting the benchmark. Harnesses
self-register under a lowercase key via ``@AGENTS.register``; external packages
use the ``devops_bench.agents`` entry-point group. Heavy imports stay
function-local so ``import devops_bench.agents`` pulls only this module.
"""

from __future__ import annotations

import os
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from devops_bench.agents.config import AgentConfig
from devops_bench.agents.result import AgentResult
from devops_bench.agents.sandbox import SandboxExecutor
from devops_bench.core import Registry, SandboxError, get_logger
from devops_bench.core.subprocess import CompletedProcess
from devops_bench.core.subprocess import run as _host_subprocess_run

__all__ = ["AgentHarness", "AGENTS"]


def _reject_non_lowercase_key(key: str) -> str | None:
    """Reject a key with uppercase characters: lookups lowercase the agent type first.

    Returns:
        None when ``key`` is acceptable, else the reason it was rejected.
    """
    if key != key.lower():
        return "agent keys must be lowercase; the configured agent type is lowercased before lookup"
    return None


#: Registry of concrete :class:`AgentHarness` subclasses, keyed by agent type.
#: External packages register through the entry-point group under the same key policy.
AGENTS: Registry[type[AgentHarness]] = Registry(
    "agents",
    entry_point_group="devops_bench.agents",
    key_validator=_reject_non_lowercase_key,
)

_log = get_logger("agents.base")


class AgentHarness(ABC):
    """Template-method base class for an agent driven during a benchmark run.

    :meth:`run` stamps ``AgentResult.latency``, converts any unexpected exception
    from :meth:`_execute` into ``AgentResult.errored``, and wraps the call in a
    ``deepeval`` span when that package is installed. Subclasses report their
    *known* errors through ``AgentResult.errors``; the safety net is for the rest.

    Args:
        config: Typed configuration; ``None`` means a default ``AgentConfig()``.
    """

    #: Whether every agent-owned subprocess goes through :meth:`run_agent_cmd`;
    #: :meth:`run` refuses a sandboxed config on a harness that has not declared it.
    supports_sandbox: bool = False

    @classmethod
    def reasoning_effort(cls, config: AgentConfig) -> str | None:
        """The reasoning tier a run of ``config.model`` uses, when the harness names one apart from the id."""
        return None

    def __init__(self, config: AgentConfig | None = None) -> None:
        self.config = config or AgentConfig()

    def run(self, prompt: str, workspace_path: Path | None = None) -> AgentResult:
        """Execute the agent against ``prompt``; the only entry point the harness calls.

        Args:
            prompt: Task prompt handed to the agent.
            workspace_path: Harness-owned working directory whose writes are
                collected afterward; ``None`` lets the agent use a throwaway one.

        Returns:
            An :class:`AgentResult` with ``latency`` populated; a subclass crash
            becomes ``AgentResult.errored``.

        Raises:
            SandboxError: A sandboxed config on a harness without sandbox support,
                or an executor refusal. Never converted to an errored result, since
                a containment failure must not score as agent performance.
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
            # A broken boundary must not score as a badly-performing agent.
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

        With ``config.sandbox`` set the command runs in
        :class:`~devops_bench.agents.sandbox.SandboxExecutor`; otherwise it is
        handed through with the signature and defaults of
        :func:`devops_bench.core.subprocess.run`. A sandbox that cannot run raises
        ``SandboxError``, which :meth:`run` re-raises.

        Args:
            cmd: Command and arguments, never a shell string.
            cwd: Working directory; sandboxed it must lie under the run workspace.
            env: Full-environment replacement; the sandbox rejects it.
            extra_env: The resolved per-run overlay; sandboxed it is the only
                environment that crosses, by value, after the deny filter.
            check / capture / text / timeout / input: As in
                ``core.subprocess.run``; the sandbox rejects a non-empty ``input``.
            host_run: Callable for the unsandboxed path; defaults to the harness
                module's own ``run`` import (the symbol its tests patch).

        Returns:
            The completed process, in either mode.
        """
        if self.config.sandbox is not None:
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
        if host_run is None:
            # The harness module's own ``run`` import, so its unit-test patches still apply.
            candidate = getattr(sys.modules.get(type(self).__module__), "run", None)
            host_run = candidate if callable(candidate) else _host_subprocess_run
        return host_run(
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
        """Run the agent and return its typed result; known errors go in ``AgentResult.errors``.

        Args:
            prompt: Task prompt handed to the agent.
            workspace_path: Harness-owned working directory, or ``None``; an agent
                with no local workspace may ignore it.

        Returns:
            An :class:`AgentResult`; ``latency`` may be left zero for the base to fill.
        """


def _maybe_observe(
    func: Callable[[str, Path | None], AgentResult],
) -> Callable[[str, Path | None], AgentResult]:
    """Return ``func`` wrapped in ``deepeval.tracing.observe`` when importable, else as is."""
    try:
        from deepeval.tracing import observe
    except ImportError:
        return func
    return observe()(func)
