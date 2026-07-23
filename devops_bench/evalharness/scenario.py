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

"""Background chaos + verification on a daemon thread.

The :class:`ScenarioManager` resolves the target Service's external
LoadBalancer IP, points the load action at it, threads the port-forward target
env onto the run context as a fallback, and runs chaos plus verification on a
daemon thread, resolving :attr:`ChaosSpec.verify` against a name-keyed
verification mapping supplied by the caller.
"""

from __future__ import annotations

import contextlib
import copy
import socket
import threading
import time
from typing import Any

from devops_bench.chaos import ChaosResult, ChaosSpec
from devops_bench.chaos.faults.generate_load import (
    _ENV_LOCAL_PORT,
    _ENV_SKIP_PORT_FORWARD,
    _ENV_TARGET_DEPLOYMENT,
    _ENV_TARGET_NAMESPACE,
    _LOCAL_PORT,
)
from devops_bench.core import get_logger
from devops_bench.core.context import RunContext
from devops_bench.k8s import get_resource, poll_until
from devops_bench.verification import VerifierAgent

__all__ = ["ScenarioManager", "VERIFICATION_TIMEOUT_SEC", "pick_free_port"]

_log = get_logger("evalharness.scenario")

# Verification budget shared across the (possibly nested) checks.
VERIFICATION_TIMEOUT_SEC = 120

# Seconds to wait for the target Service's external LoadBalancer IP to be
# assigned by GKE. LB provisioning typically completes within a minute but can
# lag — bound it so a stuck assignment falls back to the port-forward path
# instead of stalling the run.
_LB_IP_TIMEOUT_SEC = 180


def pick_free_port() -> int:
    """Return an ephemeral TCP port currently free on the loopback interface.

    Binds to port 0 and reads back the kernel-assigned port. There is an
    inherent (small) race between releasing the probe socket and ``kubectl``
    binding the port; callers accept it as the cost of avoiding a fixed-port
    collision across concurrent runs.

    Returns:
        A port number that was free at probe time.
    """
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("", 0))
        return sock.getsockname()[1]


class ScenarioManager:
    """Orchestrate a background chaos disruption and its verification.

    The manager runs on a daemon thread alongside the agent under test: it
    waits on the typed trigger, drives the typed
    :class:`~devops_bench.chaos.base.Fault` (via ``action.inject``) to inject
    the planned disruption, then resolves the spec's ``verify:`` key against the
    per-task verification mapping and runs
    :meth:`~devops_bench.verification.VerifierAgent.wait_for_condition` on the
    resolved node. The load fault reaches its target through the target
    Service's external LoadBalancer IP — the manager resolves it from the
    Service status and rewrites the action's load URL before injection — so the
    fortio spike works from any runner location (in-VPC bastion or off-VPC
    local). The ``kubectl port-forward`` the fault still owns is kept as a
    fallback for when LB resolution fails or the caller opts out.

    Args:
        target_deployment: Deployment the load fault should disrupt. Also the
            Service name (the optimize-scale stack seeds the Service with the
            same name), used here to look up the external LB IP and threaded
            onto ``ctx.env`` for the fault's port-forward fallback.
        namespace: Namespace the deployment / Service lives in; threaded onto
            ``ctx.env``.
        verification_mapping: Name-keyed mapping of verification specs the
            chaos ``verify:`` reference is resolved against. The mapping carries
            already-validated :class:`VerificationSpec` instances (or any value
            ``VerifierAgent.wait_for_condition`` accepts); the manager never
            re-validates. Empty mapping disables verification lookups.
        skip_port_forward: When True, the fault runs without resolving an LB IP
            and without opening a ``kubectl port-forward``. The E2E smoke
            harness (against :class:`~devops_bench.deployers.NoOpDeployer`)
            flips this on so tests can exercise the wiring without a real
            cluster.

    Attributes:
        chaos_active_event: Set by the chaos fault once the disruption is
            observably active, so the harness can synchronize the operator
            agent's start.
    """

    def __init__(
        self,
        target_deployment: str,
        namespace: str,
        verification_mapping: dict[str, Any] | None = None,
        *,
        skip_port_forward: bool = False,
        local_port: int | None = None,
    ) -> None:
        self.target_deployment = target_deployment
        self.namespace = namespace
        self.verification_mapping: dict[str, Any] = dict(verification_mapping or {})
        self.skip_port_forward = skip_port_forward
        # Per-run local port for the fault's port-forward; None keeps the
        # fault's default. Parallel runs pass a free port to avoid contention.
        self.local_port = local_port
        self.chaos_active_event = threading.Event()
        self.verifier_agent = VerifierAgent()
        self.result_holder: dict[str, dict[str, Any]] = {
            "chaos_report": {},
            "perf_report": {},
        }
        self.start_time: float | None = None
        self._aborted = threading.Event()
        # Guards writes to ``result_holder`` against a concurrent snapshot in
        # ``get_reports`` — the reader can run while this thread is still writing
        # (the timeout path in the harness drains reports without a completed join).
        self._report_lock = threading.Lock()

    def run_chaos_and_verification(
        self,
        spec: ChaosSpec,
        ctx: RunContext,
    ) -> None:
        """Inject the planned fault, then gather verification metrics.

        Args:
            spec: A typed :class:`ChaosSpec` carrying the trigger, action, and
                opaque ``verify:`` key to resolve.
            ctx: The per-task run context (forwarded to the trigger / fault).
        """
        self.start_time = time.time()

        # Record initial chaos metadata before injection begins, so a crash
        # mid-injection still produces a partial report rather than silence.
        with self._report_lock:
            self.result_holder["chaos_report"] = {
                "injected_fault": spec.action.type,
                "name": spec.name,
                "status": "initiated",
            }

        try:
            chaos_result = self._inject_chaos(spec, ctx)
            with self._report_lock:
                self.result_holder["chaos_report"] = self._chaos_report_from_result(
                    spec, chaos_result
                )
        except Exception as exc:  # noqa: BLE001 - surface failure into the report
            _log.error("error running scenario: %s", exc)
            with self._report_lock:
                self.result_holder["chaos_report"]["status"] = "failed"
                self.result_holder["chaos_report"]["error"] = str(exc)
            # Unblock the main thread immediately: it waits on this event to
            # learn the disruption is active, and a failed injection never sets
            # it via the fault, so without this it stalls for the full
            # ``_CHAOS_ACTIVE_WAIT_SEC`` timeout before proceeding.
            self.chaos_active_event.set()
            return

        if self._aborted.is_set():
            return

        verification_node = self._resolve_verification(spec.verify)
        if verification_node is None:
            # No verification scheduled (``verify`` was None) — leave the
            # chaos_report alone. An UNKNOWN key, on the other hand, has
            # already stamped ``chaos_report["verification"]`` with the
            # failure record so the operator sees the typo'd cross-reference
            # on the run record, not just in the log.
            return

        _log.info("starting planned verification using VerifierAgent...")
        try:
            verification_result = self.verifier_agent.wait_for_condition(
                verification_node, timeout_sec=VERIFICATION_TIMEOUT_SEC
            )
            _log.info(
                "verification completed: %s",
                verification_result.model_dump_json(indent=2),
            )
            with self._report_lock:
                self.result_holder["chaos_report"]["verification"] = (
                    verification_result.model_dump()
                )
                self.result_holder["perf_report"] = self._perf_from_verification(
                    verification_result
                )
        except Exception as exc:  # noqa: BLE001 - surface failure into the report
            _log.error("verification failed with exception: %s", exc)
            with self._report_lock:
                self.result_holder["chaos_report"]["verification"] = {
                    "success": False,
                    "reason": f"Verification exception: {exc}",
                }

    def _inject_chaos(self, spec: ChaosSpec, ctx: RunContext) -> ChaosResult:
        """Wait on the trigger, then drive ``action.inject`` with the target env.

        The trigger is a typed node; wait through its own ``wait(ctx)`` rather
        than reading raw ``delay_seconds`` here — the harness only knows the
        ``Trigger`` Protocol, not the concrete trigger's parameters. Before
        injecting, the manager resolves the target Service's external
        LoadBalancer IP and points the action's load URL at
        ``http://<lb-ip>:8080`` (so the fortio spike hits the workload directly
        from any runner location); the port-forward target is also threaded onto
        ``ctx.env`` as a fallback for when LB resolution fails. When
        ``skip_port_forward`` is True (E2E smoke / no real cluster), the LB
        resolution is skipped and the fault runs against whatever URL the
        action already carries.

        Args:
            spec: Typed chaos spec.
            ctx: Run context handed to the trigger / fault.

        Returns:
            The :class:`~devops_bench.chaos.ChaosResult` returned by the fault.
        """
        spec.trigger.wait(ctx)

        # Thread the port-forward target onto the context. ``ctx.env`` values
        # are strings; flags are written only when truthy so the fault's
        # ``bool(env.get(...))`` reads cleanly.
        ctx.env[_ENV_TARGET_DEPLOYMENT] = self.target_deployment
        ctx.env[_ENV_TARGET_NAMESPACE] = self.namespace
        if self.local_port is not None:
            ctx.env[_ENV_LOCAL_PORT] = str(self.local_port)

        if self.skip_port_forward:
            # Smoke / no-cluster path: there is no cluster to query for an LB
            # IP, so leave the action's URL alone and just flag the fault to
            # skip the tunnel.
            ctx.env[_ENV_SKIP_PORT_FORWARD] = "1"
        else:
            is_local = ctx.cluster is not None and ctx.cluster.location == "local"
            if is_local:
                _log.info(
                    "local cluster (location=%r) detected; skipping LoadBalancer IP resolution "
                    "to rely directly on port-forward fallback",
                    ctx.cluster.location,
                )
                lb_ip = None
            else:
                # Real-cluster path (GKE): resolve the external LB IP and rewrite the
                # action's load URL to point at it directly. Fall back to the
                # port-forward path if resolution fails or times out — that way a
                # delayed LB still produces a load attempt instead of an aborted
                # run.
                lb_ip = self._resolve_lb_ip(self.target_deployment, self.namespace)

            if lb_ip is not None and hasattr(spec.action, "target"):
                target = spec.action.target
                if hasattr(target, "service_url"):
                    lb_url = f"http://{lb_ip}:{_LOCAL_PORT}"
                    _log.info(
                        "chaos load will hit external LB %s (no port-forward)",
                        lb_url,
                    )
                    target.service_url = lb_url
                    ctx.env[_ENV_SKIP_PORT_FORWARD] = "1"
            # If lb_ip is None (timeout / local skip / kubectl error) the skip env is left
            # unset; the fault then opens its own port-forward as the fallback.

        return spec.action.inject(ctx, self.chaos_active_event)

    @staticmethod
    def _resolve_lb_ip(service: str, namespace: str) -> str | None:
        """Poll the target Service for an external LoadBalancer IP.

        Reads ``status.loadBalancer.ingress[0].ip`` (falling back to
        ``hostname`` when the cloud provider hands back a DNS name instead of an
        IP) and waits up to :data:`_LB_IP_TIMEOUT_SEC` for it to appear, since
        GKE may take a minute or two to provision the underlying network LB
        and firewall rule.

        Args:
            service: Service name (the optimize-scale target Service is named
                after the target deployment, so the manager reuses
                ``target_deployment`` here).
            namespace: Namespace the Service lives in.

        Returns:
            The external IP/hostname as a string, or ``None`` if the timeout
            elapses or kubectl fails. The caller treats ``None`` as a signal to
            fall back to the port-forward transport.
        """
        resolved: dict[str, str] = {}

        def _has_ip() -> bool:
            try:
                doc = get_resource("service", service, namespace=namespace)
            except Exception as exc:  # noqa: BLE001 - poll keeps retrying
                _log.debug("waiting for LB IP on %s/%s: %s", namespace, service, exc)
                return False
            ingress = (doc.get("status") or {}).get("loadBalancer", {}).get("ingress") or []
            if not ingress:
                return False
            entry = ingress[0] or {}
            ip = entry.get("ip") or entry.get("hostname")
            if not ip:
                return False
            resolved["ip"] = ip
            return True

        _log.info(
            "resolving external LB IP for service %s/%s (timeout %ss)",
            namespace,
            service,
            _LB_IP_TIMEOUT_SEC,
        )
        ok = poll_until(_has_ip, timeout_sec=_LB_IP_TIMEOUT_SEC)
        if not ok:
            _log.warning(
                "external LB IP for service %s/%s not assigned within %ss; "
                "falling back to port-forward",
                namespace,
                service,
                _LB_IP_TIMEOUT_SEC,
            )
            return None
        return resolved["ip"]

    @staticmethod
    def _chaos_report_from_result(spec: ChaosSpec, result: Any) -> dict[str, Any]:
        """Shape a typed ``ChaosResult`` into the chaos-report dict.

        Args:
            spec: The originating spec (carries the human-readable ``name``).
            result: A :class:`~devops_bench.chaos.ChaosResult`.

        Returns:
            The ``chaos_report`` dict consumed by the result reporter; the
            ``status`` field is derived from ``ChaosResult.success``.
        """
        dumped = result.model_dump()
        # Carry both the human-readable name and the typed result fields so
        # downstream consumers see a superset of both.
        report: dict[str, Any] = {
            "injected_fault": result.injected_fault,
            "name": spec.name,
            "status": "success" if result.success else "failed",
            "output": dumped.get("output", ""),
            "elapsed_time": dumped.get("elapsed_time", 0.0),
        }
        if dumped.get("error") is not None:
            report["error"] = dumped["error"]
        return report

    @staticmethod
    def _perf_from_verification(result: Any) -> dict[str, Any]:
        """Derive ``perf_report`` from a verification result.

        Deployment time flows through on success; the uptime / utilization
        fields collapse to a success binary.
        """
        success = bool(result.success)
        elapsed = float(result.elapsed_time)
        return {
            "deployment_time_seconds": elapsed if success else None,
            "uptime_percentage": 100.0 if success else 0.0,
            "resource_utilization_efficiency": 1.0 if success else 0.0,
        }

    def _resolve_verification(self, verify_ref: str | None) -> Any | None:
        """Resolve the chaos spec's opaque ``verify`` key against the mapping.

        Args:
            verify_ref: The string key carried on :attr:`ChaosSpec.verify`, or
                ``None`` when the spec opts out of verification.

        Returns:
            The mapped verification node (already validated) when the key is
            present and known; ``None`` when the spec opts out **or** the
            key is unknown. The unknown-key case is *not* silent — a
            verification-failure entry is written into ``chaos_report``
            naming the missing key + the available keys, so a typo'd
            cross-reference shows up on the run record (not just in the
            log).
        """
        if not verify_ref:
            return None
        node = self.verification_mapping.get(verify_ref)
        if node is None:
            known = sorted(self.verification_mapping.keys())
            reason = (
                f"chaos verify reference {verify_ref!r} not found in "
                f"verification mapping; known keys: {known}"
            )
            _log.warning(reason)
            # Surface the unresolved reference on the chaos_report so the
            # operator sees the typo'd cross-reference in results.json, not
            # just in the log. The shape mirrors the typed
            # VerificationResult dump (success/reason/name) so downstream
            # consumers don't need a special-case parse path.
            with self._report_lock:
                self.result_holder["chaos_report"]["verification"] = {
                    "success": False,
                    "reason": reason,
                    "name": verify_ref,
                    "unresolved_reference": verify_ref,
                    "known_references": known,
                }
            return None
        return node

    def get_reports(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return the aggregated chaos and performance reports.

        Returns:
            A ``(chaos_report, perf_report)`` pair derived from the most recent
            scenario run; each is an empty dict before the run produces it. The
            snapshot is a deep copy taken under the report lock, so a caller may
            read it safely while the scenario thread is still writing.
        """
        with self._report_lock:
            return (
                copy.deepcopy(self.result_holder.get("chaos_report", {})),
                copy.deepcopy(self.result_holder.get("perf_report", {})),
            )

    def stop(self) -> None:
        """Abort the scenario so a pending verification is skipped.

        Sets an abort flag the scenario thread checks before dispatching
        verification. The ``kubectl port-forward`` is owned by the load fault
        (which tears it down in its own ``finally``), so there is nothing for
        the manager to release here. Safe to call more than once and from a
        different thread than the scenario's; it never raises, so it can run
        from a ``finally`` block during cleanup.
        """
        self._aborted.set()
