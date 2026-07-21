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

"""The ``generate_load`` fault: LLM-driven fortio traffic spikes.

This module owns every fortio-specific concern: the load-target schema, the SRE
system prompt, the ``run_command`` tool descriptor, the shell-free argv
executor, and the typed :class:`GenerateLoadFault` node; the ``kubectl
port-forward`` tunnel is driven by :func:`devops_bench.k8s.port_forward`. The
chaos agent is imported lazily inside :meth:`GenerateLoadFault.inject` so
registering this fault does not pull the agent or :mod:`devops_bench.models`
chain.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import textwrap
import threading
import time
from types import SimpleNamespace
from typing import Any, Literal

from pydantic import BaseModel, Field

from devops_bench.chaos.base import FAULTS, ChaosResult, Fault
from devops_bench.core import get_logger
from devops_bench.core.context import RunContext
from devops_bench.core.subprocess import run
from devops_bench.k8s import port_forward, rollout_status

__all__ = [
    "GenerateLoadFault",
    "LoadTarget",
    "RUN_COMMAND_TOOL",
    "build_system_instruction",
    "run_chaos_command",
]

_log = get_logger("chaos.generate_load")

# Marker substring that, when present in a command, indicates a load spike is
# active. The harness watches the shared event to coordinate measurements.
_LOAD_MARKER = "fortio load"

# Wall-clock ceiling for a single chaos command.
_COMMAND_TIMEOUT = 40

# The workload's in-cluster (remote) port for chaos load generation, and the
# default local side of the port-forward. Parallel runs override only the local
# side via ``_ENV_LOCAL_PORT`` so two concurrent forwards do not contend.
_LOCAL_PORT = 8080

# Single source of truth for the load target when a spec omits one. The local
# port-forward URL the cluster workload is exposed on.
_DEFAULT_TARGET_URL = f"http://localhost:{_LOCAL_PORT}"

# ``RunContext.env`` keys naming the port-forward target the harness writes
# onto the context before injection.
_ENV_TARGET_DEPLOYMENT = "CHAOS_TARGET_DEPLOYMENT"
_ENV_TARGET_NAMESPACE = "CHAOS_TARGET_NAMESPACE"
_ENV_SKIP_PORT_FORWARD = "CHAOS_SKIP_PORT_FORWARD"
# Optional per-run local port the harness allocates for parallel isolation; the
# fault binds the port-forward's local side here and points the load URL at it.
_ENV_LOCAL_PORT = "CHAOS_LOCAL_PORT"

# Seconds to wait for the target deployment's rollout to finish before opening
# the port-forward. The agent often mutates the deployment (e.g. adds resource
# limits) just before the spike, triggering a rolling update; without this wait
# the port-forward can race a not-yet-Ready pod and exit early (code 1).
_TARGET_READY_TIMEOUT_SEC = 120


def build_system_instruction(target_url: str = _DEFAULT_TARGET_URL) -> str:
    """Build the SRE system instruction, targeting ``target_url`` for load.

    Args:
        target_url: URL fortio load should be directed at. Flows from the
            fault's ``target.service_url`` — the harness rewrites this to the
            target Service's external LoadBalancer URL on a real cluster, or to
            a local port-forward URL as the fallback; defaults to
            :data:`_DEFAULT_TARGET_URL`.

    Returns:
        The system instruction string with the target URL injected.
    """
    # Each paragraph the model sees is one physical source line; ``dedent``
    # strips the shared leading indentation.
    return textwrap.dedent(
        f"""\
        You are a professional Site Reliability Engineer (SRE) and Chaos Engineering Expert.
        Your role is to disrupt Kubernetes workloads to test system resilience, which can happen in two modes:
        1. Planned Mode: Execute a specific chaos disruption according to a provided JSON spec.
        2. Autonomous Mode: Autonomously explore the Kubernetes cluster state, identify critical targets (pods, nodes, services), and inject transient faults to test recovery.

        You are equipped with the `run_command` tool, which runs a single command locally on the host control machine (which is fully authenticated and has cluster-admin kubectl privileges). Each call must be ONE non-piped command: shell pipelines, redirection, command chaining (``|``, ``>``, ``&&``, ``;``) and environment-variable interpolation (``$VAR``) are NOT supported.

        Strict Guidelines for Execution:
        - Single Execution Policy: You MUST execute exactly one tool call to run the planned 'fortio' load generation spike. Do NOT attempt to rerun, adjust, or tune the load generation if the target service saturates or returns timeouts. Once the single load command is executed, analyze the output, write your final performance summary, and exit immediately.
        - Safety First: Only inject transient, safe, and recoverable faults (e.g. killing pods, scaling deployments, generating traffic spikes). Do NOT permanently destroy clusters, namespaces, or nodes.
        - Traffic Generation: For load spikes, use the 'fortio' binary. Target '{target_url}' directly — this is the workload's reachable URL (an external LoadBalancer or a local tunnel, depending on the runner). Do NOT use *.svc.cluster.local URLs from outside the cluster.
        - Analysis & Clarity: Analyze command outputs carefully, report stdout/stderr accurately, and confirm in your final response when the disruption has been successfully completed."""
    )


# Neutral, duck-typed tool descriptor consumed by ``LLMClient.format_tools``
# (mirrors the MCP tool shape: name/description/inputSchema).
RUN_COMMAND_TOOL = SimpleNamespace(
    name="run_command",
    description=(
        "Run a shell command on the host control machine (authenticated kubectl + fortio). "
        "Returns combined stdout and stderr."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute, e.g. a fortio load invocation.",
            }
        },
        "required": ["command"],
    },
)


# TODO(#33): replace the free-form command tool with structured
# qps / duration / concurrency parameters and a code-owned fortio argv pinned
# to the target URL, so the model never chooses the executable or the target.
def run_chaos_command(
    command: str,
    chaos_active_event: threading.Event | None = None,
    *,
    load_result: dict[str, Any] | None = None,
) -> str:
    """Execute a single chaos command and return its combined output.

    The command is a single, non-piped string produced by the LLM (typically a
    ``fortio load`` invocation). It is tokenized with :func:`shlex.split` and
    run shell-free, so shell features (pipes, redirection, ``$VAR``) are not
    supported; a leading ``~`` in each token is expanded to the user's home.
    When the command is a load spike and an event is supplied, the event is
    set so the harness can observe that load is active before measuring impact.

    Args:
        command: Single command to execute (no shell pipelines or redirection).
        chaos_active_event: Optional event signaled when a load spike starts.
        load_result: Optional mutable dict the caller reads to learn whether the
            load spike actually ran and exited 0. When the command is a load
            spike (:data:`_LOAD_MARKER`), the keys ``attempted`` (bool),
            ``returncode`` (int), and ``ok`` (bool) are written. Lets the fault
            fail closed instead of reporting success for a spike that never
            reached the workload.

    Returns:
        A string combining stdout and stderr, or an ``"Error: ..."`` string if
        the command could not be parsed or run.
    """
    if not command.strip():
        return "Error: command string is empty"

    # Provisional, from the raw string: parsing may fail below, and the failure
    # path still needs to know a spike was intended. Refined from the parsed
    # argv once it exists.
    is_load = _LOAD_MARKER in command
    try:
        _log.info("running chaos command: %s", command)

        # The model emits shell strings (e.g. a single fortio invocation); split
        # into argv so execution stays shell-free. shlex.split does not expand
        # ``~``, so expand each token's leading home to keep ``~/go/bin/fortio``
        # style paths resolvable.
        argv = [os.path.expanduser(arg) for arg in shlex.split(command)]

        # A load spike is an actual fortio invocation, not any command that
        # merely mentions one (``echo "fortio load"`` must not count): derive
        # the flag from the parsed argv so a non-spike can never mark the
        # spike attempted / ok.
        is_load = len(argv) >= 2 and os.path.basename(argv[0]) == "fortio" and argv[1] == "load"

        # Signal "load active" only once the command parses cleanly and we are
        # about to execute it, so a parse failure never falsely tells the
        # harness that load is live.
        if chaos_active_event is not None and is_load:
            _log.info("load spike detected; signaling harness via chaos event")
            chaos_active_event.set()

        completed = run(argv, check=False, timeout=_COMMAND_TIMEOUT)
        if is_load and load_result is not None:
            # Record the spike's real exit status so the fault can fail closed:
            # a non-zero fortio exit means it could not reach the workload.
            load_result["attempted"] = True
            load_result["returncode"] = completed.returncode
            load_result["ok"] = completed.returncode == 0
        return f"Stdout:\n{completed.stdout}\nStderr:\n{completed.stderr}"
    except Exception as exc:  # noqa: BLE001 - surface any failure back to the LLM
        if is_load and load_result is not None:
            load_result["attempted"] = True
            load_result["returncode"] = None
            load_result["ok"] = False
            load_result["error"] = f"{type(exc).__name__}: {exc}"
        return f"Error: {exc}"


class LoadTarget(BaseModel):
    """Target for a generated load spike.

    Attributes:
        service_url: HTTP URL the load should be directed at. When the fault
            opens its own port-forward (see :meth:`GenerateLoadFault.inject`),
            this in-cluster ``*.svc.cluster.local`` URL is pointed at the local
            tunnel for the duration of injection.
        qps: Target queries per second; the LLM passes this to fortio.
        duration: Optional fortio duration string (e.g. ``"30s"``). When
            omitted, the model picks one consistent with the goal.
        concurrency: Optional fortio ``-c`` concurrency override.
    """

    service_url: str
    qps: int = Field(default=100, ge=1)
    duration: str | None = None
    concurrency: int | None = Field(default=None, ge=1)


@FAULTS.register("generate_load")
class GenerateLoadFault(Fault):
    """A traffic-spike fault driven by an LLM issuing fortio commands.

    The fault exposes a single command-execution capability to the model and
    relies on the agent loop to plan and issue exactly one ``fortio load``
    spike against the port-forwarded target. The fault owns the connectivity:
    :meth:`inject` opens its own ``kubectl port-forward`` to the target
    deployment, points the load URL at the local tunnel, and tears the tunnel
    down when injection completes.

    Attributes:
        type: Discriminator literal, always ``"generate_load"``.
        target: Typed load-target description (URL, qps, optional duration /
            concurrency).
    """

    type: Literal["generate_load"] = "generate_load"
    target: LoadTarget

    def goal(self) -> str:
        """Build the planned-mode goal prompt for the LLM.

        The target URL is read from :attr:`target`; :meth:`inject` points it at
        the local port-forward for the duration of injection, so the model's
        prompt always agrees with the URL the load actually hits.

        Returns:
            The goal prompt instructing the model to issue one fortio spike.
        """
        spec_dump: dict[str, Any] = self.model_dump(exclude_none=True)
        target_url = self.target.service_url
        spec_json = json.dumps(spec_dump, indent=2)
        # Dedent the template BEFORE interpolating ``spec_json``: the dumped
        # JSON is multi-line with no shared leading indentation, so substituting
        # it first would defeat ``dedent``'s common-prefix strip.
        template = textwrap.dedent(
            """\
            Your goal is to execute the following planned chaos engineering disruption action:
            ```json
            {spec_json}
            ```

            Guidelines for execution:
            1. Use the 'fortio' tool to inject traffic into the target workload.
            2. Run fortio against {target_url} directly — that is the workload's reachable URL for this run.
            Use your run_command tool to execute this disruption safely and effectively."""
        )
        return template.format(spec_json=spec_json, target_url=target_url)

    def inject(
        self,
        ctx: RunContext,
        chaos_active_event: threading.Event | None = None,
    ) -> ChaosResult:
        """Open a port-forward, run the LLM-planned chaos loop, tear it down.

        The fault owns its own connectivity: when the run context names a
        target deployment (via :data:`_ENV_TARGET_DEPLOYMENT` /
        :data:`_ENV_TARGET_NAMESPACE` on ``ctx.env``) and the port-forward is
        not skipped, :meth:`inject` opens a ``kubectl port-forward`` to that
        deployment, points the load URL at ``http://localhost:<port>`` for the
        duration of injection, generates load, and tears the tunnel down in a
        ``finally``. When the deployment is absent or
        :data:`_ENV_SKIP_PORT_FORWARD` is set (E2E smoke / unit tests), it runs
        the chaos loop against whatever URL the target already carries.

        Args:
            ctx: Run context; ``ctx.env`` carries the port-forward target the
                harness threaded in (deployment, namespace, skip flag).
            chaos_active_event: Optional event the executor signals when a
                load spike starts, so the harness can synchronize measurements.

        Returns:
            A :class:`ChaosResult` whose ``success`` is True on a clean loop
            completion. Exceptions raised by the agent or the port-forward are
            converted to ``success=False`` with the failure surfaced in
            ``error``.
        """
        env = ctx.env or {}
        deployment = env.get(_ENV_TARGET_DEPLOYMENT)
        namespace = env.get(_ENV_TARGET_NAMESPACE, "default")
        skip_port_forward = bool(env.get(_ENV_SKIP_PORT_FORWARD))

        start = time.monotonic()
        # Populated by ``run_chaos_command`` with the spike's real exit status so
        # the fault can fail closed when load never reached the workload.
        load_result: dict[str, Any] = {}
        try:
            # Parallel runs pass a free local port so two concurrent forwards do
            # not contend; the remote (workload) side stays ``_LOCAL_PORT``.
            # Parsed inside the guard so a malformed value fails this fault
            # instead of escaping ``inject``.
            local_port = int(env.get(_ENV_LOCAL_PORT) or _LOCAL_PORT)

            # Open the fault's own tunnel only when a target deployment is named
            # and the caller did not opt out; otherwise run against the existing
            # URL.
            forward: contextlib.AbstractContextManager[Any]
            if deployment and not skip_port_forward:
                # Wait for the deployment to be rolled out (a Ready pod exists)
                # before forwarding, so the port-forward does not race a rolling
                # update the agent may have just triggered. Best-effort: a
                # failure here (e.g. no such deployment) should not abort the
                # fault — the port-forward attempt below surfaces the real
                # problem.
                try:
                    rollout_status(
                        f"deployment/{deployment}",
                        timeout_sec=_TARGET_READY_TIMEOUT_SEC,
                        namespace=namespace,
                    )
                except Exception as exc:  # noqa: BLE001 - wait is best-effort
                    _log.warning(
                        "rollout wait for deployment/%s did not complete: %s",
                        deployment,
                        exc,
                    )
                forward = port_forward(
                    f"deployment/{deployment}",
                    local_port,
                    remote_port=_LOCAL_PORT,
                    namespace=namespace,
                )
                local_url: str | None = f"http://localhost:{local_port}"
            else:
                forward = contextlib.nullcontext()
                local_url = None

            with forward:
                output = self._run_agent_loop(local_url, chaos_active_event, load_result)
        except Exception as exc:  # noqa: BLE001 - one fault must never abort the run
            elapsed = time.monotonic() - start
            _log.exception("generate_load fault crashed")
            return ChaosResult(
                success=False,
                injected_fault=self.type,
                output="",
                elapsed_time=elapsed,
                error=f"{type(exc).__name__}: {exc}",
            )
        elapsed = time.monotonic() - start

        # Fail closed: a clean agent loop does not mean the spike landed. Treat
        # "no fortio load ran" and "fortio exited non-zero" (could not reach the
        # workload) as failures so a no-op spike never passes as a measured one.
        if not load_result.get("attempted"):
            return ChaosResult(
                success=False,
                injected_fault=self.type,
                output=output,
                elapsed_time=elapsed,
                error="no fortio load command was executed",
            )
        if not load_result.get("ok"):
            rc = load_result.get("returncode")
            detail = load_result.get("error") or f"fortio load exited with code {rc}"
            return ChaosResult(
                success=False,
                injected_fault=self.type,
                output=output,
                elapsed_time=elapsed,
                error=f"load did not reach the workload: {detail}",
            )
        return ChaosResult(
            success=True,
            injected_fault=self.type,
            output=output,
            elapsed_time=elapsed,
        )

    def _run_agent_loop(
        self,
        local_url: str | None,
        chaos_active_event: threading.Event | None,
        load_result: dict[str, Any],
    ) -> str:
        """Drive the chaos agent against the effective target URL.

        When ``local_url`` is set the fault is reaching its target through a
        port-forward, so the load URL is pointed at the local tunnel for the
        duration of the loop (and restored afterwards) — this keeps both the
        system instruction and the goal prompt agreeing with the URL the load
        actually hits. When ``local_url`` is ``None`` the existing target URL is
        used unchanged.

        Args:
            local_url: Local port-forward URL to target, or ``None`` to use the
                target's own ``service_url``.
            chaos_active_event: Optional event signaled when load goes active.
            load_result: Mutable dict the tool handler records the spike's exit
                status into, so the caller can fail closed.

        Returns:
            The agent's final summary string.
        """
        # Lazy import keeps the agent + models chain out of sys.modules until a
        # fault actually injects; registering the fault only needs the class.
        from devops_bench.chaos.agent import ChaosAgent

        # Bind ``load_result`` into the handler so each tool call records the
        # spike's exit status without changing the ``(command, event)`` handler
        # signature the agent invokes.
        def tool_handler(command: str, event: threading.Event | None) -> str:
            return run_chaos_command(command, event, load_result=load_result)

        original_url = self.target.service_url
        if local_url is not None:
            self.target.service_url = local_url
        try:
            system_instruction = build_system_instruction(self.target.service_url)
            agent = ChaosAgent(
                system_instruction=system_instruction,
                tool=RUN_COMMAND_TOOL,
                tool_handler=tool_handler,
                chaos_active_event=chaos_active_event,
            )
            return agent.run(self.goal())
        finally:
            self.target.service_url = original_url
