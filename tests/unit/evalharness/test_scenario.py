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

"""Scenario wiring tests (CONVENTIONS.md §4.2 / harness handoff §9.1/§9.2).

The scenario manager runs the typed chaos seam (``trigger.wait`` then
``action.inject``) and resolves the chaos ``verify`` key against a
**mapping** (not a list scan) supplied by the orchestrator. Fakes stand in
for both seams so the test exercises only the wiring — never a real cluster
or a real LLM.
"""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import patch

import pytest

from devops_bench.chaos import ChaosResult, ChaosSpec
from devops_bench.chaos.faults.generate_load import GenerateLoadFault
from devops_bench.chaos.triggers.time_delay import TimeTrigger
from devops_bench.core.context import RunContext
from devops_bench.evalharness.scenario import ScenarioManager, pick_free_port
from devops_bench.verification import VerificationResult, VerifierAgent


def _build_spec(*, verify_key: str | None) -> ChaosSpec:
    """Build a typed :class:`ChaosSpec` mirroring the optimize-scale entry."""
    return ChaosSpec.model_validate(
        {
            "name": "Test Disruption",
            "trigger": {"type": "time", "delay_seconds": 0},
            "action": {
                "type": "generate_load",
                "target": {
                    "service_url": "http://example.svc.cluster.local",
                    "qps": 50,
                },
            },
            "verify": verify_key,
        }
    )


def _build_ctx() -> RunContext:
    return RunContext(task_id="t", task_name="t")


def test_scenario_drives_trigger_wait_then_action_inject() -> None:
    """The seam: trigger.wait runs first, then action.inject — no inline goal builder."""
    spec = _build_spec(verify_key=None)
    order: list[str] = []

    def fake_wait(self: TimeTrigger, ctx: RunContext) -> None:
        order.append("trigger.wait")

    def fake_inject(
        self: GenerateLoadFault,
        ctx: RunContext,
        event: threading.Event | None,
    ) -> ChaosResult:
        order.append("action.inject")
        if event is not None:
            event.set()
        return ChaosResult(
            success=True,
            injected_fault=self.type,
            output="ok",
            elapsed_time=0.1,
        )

    with (
        patch.object(TimeTrigger, "wait", fake_wait),
        patch.object(GenerateLoadFault, "inject", fake_inject),
    ):
        manager = ScenarioManager(
            target_deployment="dep",
            namespace="ns",
            verification_mapping={},
            skip_port_forward=True,
        )
        manager.run_chaos_and_verification(spec, _build_ctx())

    assert order == ["trigger.wait", "action.inject"]
    chaos_report, perf_report = manager.get_reports()
    assert chaos_report["status"] == "success"
    assert chaos_report["injected_fault"] == "generate_load"
    assert chaos_report["name"] == "Test Disruption"
    # No verification mapping resolution happened (verify_key was None).
    assert "verification" not in chaos_report
    assert perf_report == {}


def test_scenario_threads_port_forward_target_onto_ctx_env() -> None:
    """The manager threads the port-forward target onto ``ctx.env`` for the fault.

    Connectivity moved into the load fault (#33): the manager no longer rewrites
    the action URL or opens a port-forward itself — it hands the fault the
    target deployment / namespace (and the skip flag) via the run context's
    ``env`` so the fault can open its own tunnel.
    """
    from devops_bench.chaos.faults.generate_load import (
        _ENV_SKIP_PORT_FORWARD,
        _ENV_TARGET_DEPLOYMENT,
        _ENV_TARGET_NAMESPACE,
    )

    spec = _build_spec(verify_key=None)
    captured: dict[str, Any] = {}

    def fake_inject(self: GenerateLoadFault, ctx: RunContext, event):
        captured["env"] = dict(ctx.env)
        # The in-cluster URL is untouched by the manager now; the fault is what
        # would point it at the local tunnel (skipped here).
        captured["service_url"] = self.target.service_url
        return ChaosResult(success=True, injected_fault=self.type, elapsed_time=0.0)

    with (
        patch.object(TimeTrigger, "wait", lambda self, ctx: None),
        patch.object(GenerateLoadFault, "inject", fake_inject),
    ):
        manager = ScenarioManager(
            target_deployment="dep",
            namespace="ns",
            skip_port_forward=True,
        )
        manager.run_chaos_and_verification(spec, _build_ctx())

    assert captured["env"][_ENV_TARGET_DEPLOYMENT] == "dep"
    assert captured["env"][_ENV_TARGET_NAMESPACE] == "ns"
    # ``skip_port_forward=True`` flips the fault's opt-out flag on the env.
    assert captured["env"][_ENV_SKIP_PORT_FORWARD] == "1"
    # The manager leaves the action's in-cluster URL alone — no rewrite seam.
    assert captured["service_url"] == "http://example.svc.cluster.local"


def test_scenario_threads_custom_local_port_onto_ctx_env() -> None:
    """A per-run ``local_port`` is threaded onto ``ctx.env`` for the fault.

    Connectivity lives in the fault, so the manager hands it the per-run local
    port via ``CHAOS_LOCAL_PORT``; the fault binds the port-forward's local side
    there. No port is threaded when ``local_port`` is None (default behavior).
    """
    from devops_bench.chaos.faults.generate_load import _ENV_LOCAL_PORT

    spec = _build_spec(verify_key=None)
    captured: dict[str, Any] = {}

    def fake_inject(self: GenerateLoadFault, ctx: RunContext, event):
        captured["env"] = dict(ctx.env)
        return ChaosResult(success=True, injected_fault=self.type, elapsed_time=0.0)

    with (
        patch.object(TimeTrigger, "wait", lambda self, ctx: None),
        patch.object(GenerateLoadFault, "inject", fake_inject),
    ):
        manager = ScenarioManager(
            target_deployment="dep",
            namespace="ns",
            skip_port_forward=True,
            local_port=34567,
        )
        assert manager.local_port == 34567
        manager.run_chaos_and_verification(spec, _build_ctx())

    assert captured["env"][_ENV_LOCAL_PORT] == "34567"


def test_scenario_omits_local_port_env_by_default() -> None:
    """Without a per-run port, no ``CHAOS_LOCAL_PORT`` is threaded (fault default)."""
    from devops_bench.chaos.faults.generate_load import _ENV_LOCAL_PORT

    spec = _build_spec(verify_key=None)
    captured: dict[str, Any] = {}

    def fake_inject(self: GenerateLoadFault, ctx: RunContext, event):
        captured["env"] = dict(ctx.env)
        return ChaosResult(success=True, injected_fault=self.type, elapsed_time=0.0)

    with (
        patch.object(TimeTrigger, "wait", lambda self, ctx: None),
        patch.object(GenerateLoadFault, "inject", fake_inject),
    ):
        ScenarioManager(
            target_deployment="dep", namespace="ns", skip_port_forward=True
        ).run_chaos_and_verification(spec, _build_ctx())

    assert _ENV_LOCAL_PORT not in captured["env"]


def test_pick_free_port_returns_a_usable_port() -> None:
    # pick_free_port binds port 0 and releases it, so consecutive calls may reuse
    # the same port — it guarantees a usable ephemeral port, not distinctness.
    port = pick_free_port()
    assert isinstance(port, int)
    assert 1 <= port <= 65535


def test_scenario_resolves_verify_against_mapping() -> None:
    """The chaos ``verify`` key is looked up in the harness-supplied mapping."""
    spec = _build_spec(verify_key="planned-verify")
    verification_node = object()  # opaque stand-in; VerifierAgent is mocked

    fake_result = VerificationResult(success=True, elapsed_time=2.5, reason="all good")

    with (
        patch.object(TimeTrigger, "wait", lambda self, ctx: None),
        patch.object(
            GenerateLoadFault,
            "inject",
            lambda self, ctx, event: ChaosResult(
                success=True, injected_fault=self.type, elapsed_time=0.0
            ),
        ),
        patch.object(VerifierAgent, "wait_for_condition", return_value=fake_result) as mock_wait,
    ):
        manager = ScenarioManager(
            target_deployment="dep",
            namespace="ns",
            verification_mapping={"planned-verify": verification_node},
            skip_port_forward=True,
        )
        manager.run_chaos_and_verification(spec, _build_ctx())

    # The mapping value (not a list-scanned dict) flowed straight to the
    # VerifierAgent — the lookup is O(1) and never imports verification on the
    # chaos side.
    mock_wait.assert_called_once_with(verification_node, timeout_sec=120)

    chaos_report, perf_report = manager.get_reports()
    assert chaos_report["verification"]["success"] is True
    assert chaos_report["verification"]["reason"] == "all good"
    # Perf report is derived from the typed VerificationResult.
    assert perf_report == {
        "deployment_time_seconds": 2.5,
        "uptime_percentage": 100.0,
        "resource_utilization_efficiency": 1.0,
    }


def test_scenario_unknown_verify_key_surfaces_failure_into_report() -> None:
    """An unmapped ``verify:`` key writes a verification-failure entry, not silence.

    The chaos seam never silently drops a verify reference — a typo'd key
    must be visible on ``results.json``, not just in the log, so the
    operator can spot a broken cross-reference without trawling stdout.
    """
    spec = _build_spec(verify_key="not-in-mapping")

    with (
        patch.object(TimeTrigger, "wait", lambda self, ctx: None),
        patch.object(
            GenerateLoadFault,
            "inject",
            lambda self, ctx, event: ChaosResult(
                success=True, injected_fault=self.type, elapsed_time=0.0
            ),
        ),
        patch.object(VerifierAgent, "wait_for_condition") as mock_wait,
    ):
        manager = ScenarioManager(
            target_deployment="dep",
            namespace="ns",
            verification_mapping={"some-other-key": object()},
            skip_port_forward=True,
        )
        manager.run_chaos_and_verification(spec, _build_ctx())

    # Never called — the unknown key short-circuited before dispatch.
    mock_wait.assert_not_called()
    chaos_report, _ = manager.get_reports()
    assert chaos_report["status"] == "success"
    verification = chaos_report["verification"]
    assert verification["success"] is False
    assert verification["unresolved_reference"] == "not-in-mapping"
    assert verification["known_references"] == ["some-other-key"]
    assert "not found" in verification["reason"]


def test_chaos_failure_lands_typed_error_into_report() -> None:
    """A ``ChaosResult(success=False, error=...)`` flows into the chaos report."""
    spec = _build_spec(verify_key=None)

    def failing_inject(self, ctx, event):
        return ChaosResult(
            success=False,
            injected_fault=self.type,
            elapsed_time=0.0,
            error="fortio not found",
        )

    with (
        patch.object(TimeTrigger, "wait", lambda self, ctx: None),
        patch.object(GenerateLoadFault, "inject", failing_inject),
    ):
        manager = ScenarioManager(
            target_deployment="dep",
            namespace="ns",
            skip_port_forward=True,
        )
        manager.run_chaos_and_verification(spec, _build_ctx())

    chaos_report, _ = manager.get_reports()
    assert chaos_report["status"] == "failed"
    assert chaos_report["error"] == "fortio not found"


def test_injection_exception_sets_chaos_active_event() -> None:
    """A raising injection still sets the event so the main thread unblocks.

    The main thread waits on ``chaos_active_event`` to learn the disruption is
    active; if injection raises and the event is never set, it stalls for the
    full activation timeout. The failure path must signal the event.
    """
    spec = _build_spec(verify_key=None)

    def raising_inject(self, ctx, event):
        raise RuntimeError("port-forward refused")

    with (
        patch.object(TimeTrigger, "wait", lambda self, ctx: None),
        patch.object(GenerateLoadFault, "inject", raising_inject),
    ):
        manager = ScenarioManager(
            target_deployment="dep",
            namespace="ns",
            skip_port_forward=True,
        )
        manager.run_chaos_and_verification(spec, _build_ctx())

    assert manager.chaos_active_event.is_set()
    chaos_report, _ = manager.get_reports()
    assert chaos_report["status"] == "failed"


def test_stop_aborts_verification() -> None:
    """``stop()`` is exception-safe and sets the abort flag once."""
    manager = ScenarioManager(
        target_deployment="dep",
        namespace="ns",
        skip_port_forward=True,
    )
    manager.stop()  # The fault owns the port-forward; stop only sets the flag.
    assert manager._aborted.is_set()  # noqa: SLF001
    # Idempotent — the second call must not raise.
    manager.stop()


def test_scenario_resolves_lb_ip_and_points_action_url_at_it() -> None:
    """Real-cluster path: manager resolves the Service LB IP and rewrites the URL.

    The harness no longer relies on a port-forward to reach the workload — it
    reads ``status.loadBalancer.ingress[0].ip`` from the target Service and
    rewrites the action's ``target.service_url`` to ``http://<ip>:8080`` so the
    fortio spike hits the LB directly. The skip flag is set on ``ctx.env`` so
    the fault does not also open a redundant tunnel.
    """
    from devops_bench.chaos.faults.generate_load import (
        _ENV_SKIP_PORT_FORWARD,
        _ENV_TARGET_DEPLOYMENT,
        _ENV_TARGET_NAMESPACE,
    )

    spec = _build_spec(verify_key=None)
    captured: dict[str, Any] = {}

    def fake_inject(self: GenerateLoadFault, ctx: RunContext, event):
        captured["env"] = dict(ctx.env)
        captured["service_url"] = self.target.service_url
        return ChaosResult(success=True, injected_fault=self.type, elapsed_time=0.0)

    fake_svc = {"status": {"loadBalancer": {"ingress": [{"ip": "34.10.20.30"}]}}}

    with (
        patch.object(TimeTrigger, "wait", lambda self, ctx: None),
        patch.object(GenerateLoadFault, "inject", fake_inject),
        patch(
            "devops_bench.evalharness.scenario.get_resource",
            return_value=fake_svc,
        ) as get_resource_mock,
    ):
        manager = ScenarioManager(
            target_deployment="dep",
            namespace="ns",
            skip_port_forward=False,
        )
        manager.run_chaos_and_verification(spec, _build_ctx())

    # The Service was queried by deployment name in the right namespace.
    get_resource_mock.assert_called_with("service", "dep", namespace="ns")
    # The action's URL was rewritten to the LB endpoint, port 8080.
    assert captured["service_url"] == "http://34.10.20.30:8080"
    # Skip flag is set so the fault doesn't also open a port-forward.
    assert captured["env"][_ENV_SKIP_PORT_FORWARD] == "1"
    # Deployment / namespace are still threaded for the fallback path.
    assert captured["env"][_ENV_TARGET_DEPLOYMENT] == "dep"
    assert captured["env"][_ENV_TARGET_NAMESPACE] == "ns"


def test_scenario_falls_back_to_port_forward_when_lb_ip_unavailable() -> None:
    """When LB resolution times out, the manager leaves the skip flag unset.

    The fault then opens its own ``kubectl port-forward`` to the target
    deployment as the fallback transport — a degraded but functional path so
    the run still attempts load.
    """
    from devops_bench.chaos.faults.generate_load import _ENV_SKIP_PORT_FORWARD

    spec = _build_spec(verify_key=None)
    captured: dict[str, Any] = {}

    def fake_inject(self: GenerateLoadFault, ctx: RunContext, event):
        captured["env"] = dict(ctx.env)
        captured["service_url"] = self.target.service_url
        return ChaosResult(success=True, injected_fault=self.type, elapsed_time=0.0)

    # Service has no LB ingress assigned yet — every poll returns "not ready",
    # and the bounded poll eventually gives up.
    no_ip_svc = {"status": {"loadBalancer": {}}}

    with (
        patch.object(TimeTrigger, "wait", lambda self, ctx: None),
        patch.object(GenerateLoadFault, "inject", fake_inject),
        patch(
            "devops_bench.evalharness.scenario.get_resource",
            return_value=no_ip_svc,
        ),
        # Make poll_until short-circuit to a single failed check so the test
        # doesn't actually wait 180s on the wall clock.
        patch(
            "devops_bench.evalharness.scenario.poll_until",
            return_value=False,
        ) as poll_mock,
    ):
        manager = ScenarioManager(
            target_deployment="dep",
            namespace="ns",
            skip_port_forward=False,
        )
        manager.run_chaos_and_verification(spec, _build_ctx())

    poll_mock.assert_called_once()
    # The action URL is untouched (no IP to rewrite to).
    assert captured["service_url"] == "http://example.svc.cluster.local"
    # And critically the skip flag is NOT set — the fault falls back to its
    # port-forward to keep the run functional.
    assert _ENV_SKIP_PORT_FORWARD not in captured["env"]


def test_scenario_skips_lb_resolution_in_smoke_path() -> None:
    """``skip_port_forward=True`` (smoke) never queries the cluster for an LB IP.

    The smoke harness runs against NoOpDeployer with no real cluster, so
    asking kubectl for a Service would either fail or accidentally hit the
    operator's current context. The skip flag short-circuits both the
    resolution and the port-forward.
    """
    from devops_bench.chaos.faults.generate_load import _ENV_SKIP_PORT_FORWARD

    spec = _build_spec(verify_key=None)
    captured: dict[str, Any] = {}

    def fake_inject(self: GenerateLoadFault, ctx: RunContext, event):
        captured["env"] = dict(ctx.env)
        captured["service_url"] = self.target.service_url
        return ChaosResult(success=True, injected_fault=self.type, elapsed_time=0.0)

    with (
        patch.object(TimeTrigger, "wait", lambda self, ctx: None),
        patch.object(GenerateLoadFault, "inject", fake_inject),
        patch(
            "devops_bench.evalharness.scenario.get_resource",
            side_effect=AssertionError("smoke path must not query the cluster"),
        ),
        patch(
            "devops_bench.evalharness.scenario.poll_until",
            side_effect=AssertionError("smoke path must not poll"),
        ),
    ):
        manager = ScenarioManager(
            target_deployment="dep",
            namespace="ns",
            skip_port_forward=True,
        )
        manager.run_chaos_and_verification(spec, _build_ctx())

    # Skip flag set, URL untouched, no kubectl / poll happened (the patches
    # would have raised AssertionError otherwise).
    assert captured["env"][_ENV_SKIP_PORT_FORWARD] == "1"
    assert captured["service_url"] == "http://example.svc.cluster.local"


def test_scenario_skips_lb_resolution_for_local_cluster() -> None:
    """When the cluster is local (location='local'), skip LB resolution but keep port-forward fallback.

    This avoids the 180s LoadBalancer IP resolution timeout on KinD, where
    the Service type is ClusterIP and will never get an external IP, while
    still allowing the port-forward tunnel to be opened.
    """
    from devops_bench.chaos.faults.generate_load import (
        _ENV_SKIP_PORT_FORWARD,
        _ENV_TARGET_DEPLOYMENT,
        _ENV_TARGET_NAMESPACE,
    )
    from devops_bench.core.context import ClusterInfo

    spec = _build_spec(verify_key=None)
    captured: dict[str, Any] = {}

    def fake_inject(self: GenerateLoadFault, ctx: RunContext, event):
        captured["env"] = dict(ctx.env)
        captured["service_url"] = self.target.service_url
        return ChaosResult(success=True, injected_fault=self.type, elapsed_time=0.0)

    # Local cluster info
    local_cluster = ClusterInfo(name="my-kind", location="local")
    ctx = _build_ctx()
    ctx.cluster = local_cluster

    with (
        patch.object(TimeTrigger, "wait", lambda self, ctx: None),
        patch.object(GenerateLoadFault, "inject", fake_inject),
        patch(
            "devops_bench.evalharness.scenario.get_resource",
            side_effect=AssertionError("local path must not query the cluster for LB"),
        ),
        patch(
            "devops_bench.evalharness.scenario.poll_until",
            side_effect=AssertionError("local path must not poll for LB"),
        ),
    ):
        manager = ScenarioManager(
            target_deployment="dep",
            namespace="ns",
            skip_port_forward=False,
        )
        manager.run_chaos_and_verification(spec, ctx)

    assert captured["service_url"] == "http://example.svc.cluster.local"
    assert _ENV_SKIP_PORT_FORWARD not in captured["env"]
    assert captured["env"][_ENV_TARGET_DEPLOYMENT] == "dep"
    assert captured["env"][_ENV_TARGET_NAMESPACE] == "ns"


@pytest.fixture(autouse=True)
def _no_real_kubectl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard against this file accidentally shelling out to ``kubectl``.

    Tests that don't exercise the port-forward path run with
    ``skip_port_forward=True`` so the load fault never opens a tunnel; this guard
    patches the ``subprocess.Popen`` the port-forward helper would use so a test
    that forgets the flag (and isn't deliberately driving the port-forward path)
    fails loudly instead of attempting a real port-forward.
    """

    def _boom(*args, **kwargs):  # pragma: no cover - exercised only on regression
        raise RuntimeError("test attempted to spawn a real kubectl process")

    monkeypatch.setattr("devops_bench.k8s.kubectl.subprocess.Popen", _boom)
