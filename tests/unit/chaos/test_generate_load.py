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

"""Tests for GenerateLoadFault — inject path, command runner, system prompt.

The port-forward the load fault uses to reach its target lives in this fault
(#33): :meth:`GenerateLoadFault.inject` opens its own ``kubectl port-forward``,
points the load URL at the local tunnel, and tears the tunnel down — so the
port-forward lifecycle is covered here, not in the harness scenario tests.
"""

from __future__ import annotations

import stat
import threading
import time
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from devops_bench.chaos.base import ChaosResult
from devops_bench.chaos.faults import generate_load as gl
from devops_bench.chaos.faults.generate_load import (
    _ENV_SKIP_PORT_FORWARD,
    _ENV_TARGET_DEPLOYMENT,
    _ENV_TARGET_NAMESPACE,
    GenerateLoadFault,
    LoadTarget,
    build_system_instruction,
    run_chaos_command,
)
from devops_bench.core import SubprocessError
from devops_bench.core.context import RunContext
from devops_bench.k8s import kubectl as k8s_kubectl


def _make_ctx(env: dict[str, str] | None = None) -> RunContext:
    return RunContext(task_id="test", env=env or {})


def _drive_load(
    kwargs: dict[str, Any],
    *,
    returncode: int = 0,
    command: str = "fortio load -qps 50 http://localhost:8080",
) -> None:
    """Simulate a fortio spike by invoking the agent's bound tool handler.

    The fault now fails closed unless a ``fortio load`` command actually ran and
    exited 0, so a stub agent must drive the handler the same way the real loop
    would. ``gl.run`` is patched to a fake completion with ``returncode``.
    """
    handler = kwargs["tool_handler"]
    event = kwargs.get("chaos_active_event")
    fake = CompletedProcess(args=["fortio"], returncode=returncode, stdout="OUT", stderr="ERR")
    with patch.object(gl, "run", return_value=fake):
        handler(command, event)


def test_build_system_instruction_embeds_target_url() -> None:
    msg = build_system_instruction("http://localhost:9999")
    assert "http://localhost:9999" in msg
    assert "fortio" in msg


def test_run_chaos_command_rejects_empty_command() -> None:
    assert run_chaos_command("   ") == "Error: command string is empty"


def test_run_chaos_command_sets_event_only_on_load_marker() -> None:
    event = threading.Event()
    fake = CompletedProcess(args=["fortio"], returncode=0, stdout="OUT", stderr="ERR")
    with patch.object(gl, "run", return_value=fake) as run_mock:
        out = run_chaos_command("fortio load -qps 50 http://x", chaos_active_event=event)

    assert event.is_set()
    assert "Stdout:\nOUT" in out
    assert "Stderr:\nERR" in out
    # shlex-split argv reached the executor, not a shell string.
    argv = run_mock.call_args.args[0]
    assert argv[0] == "fortio"
    assert argv[1:3] == ["load", "-qps"]


def test_run_chaos_command_does_not_set_event_for_unrelated_command() -> None:
    event = threading.Event()
    fake = CompletedProcess(args=["kubectl"], returncode=0, stdout="x", stderr="")
    with patch.object(gl, "run", return_value=fake):
        run_chaos_command("kubectl get pods", chaos_active_event=event)
    assert not event.is_set()


def test_run_chaos_command_surfaces_executor_exception_as_error_string() -> None:
    with patch.object(gl, "run", side_effect=RuntimeError("boom")):
        out = run_chaos_command("fortio load http://x")
    assert out.startswith("Error: ")
    assert "boom" in out


def test_inject_returns_chaos_result_on_success() -> None:
    fault = GenerateLoadFault(target=LoadTarget(service_url="http://localhost:8080", qps=50))

    # Patch the ChaosAgent the fault constructs so no model / SDK / network runs.
    class _StubAgent:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def run(self, goal: str) -> str:
            assert "http://localhost:8080" in goal  # goal carries the rewritten URL
            _drive_load(self.kwargs)  # a real spike that exits 0
            return "spike complete"

    # ``ChaosAgent`` is imported lazily inside ``inject`` (Phase 4 keeps the
    # agent + models chain out of sys.modules until injection runs), so the
    # patch must target the source module rather than the fault module.
    with patch("devops_bench.chaos.agent.ChaosAgent", _StubAgent):
        result = fault.inject(_make_ctx())

    assert isinstance(result, ChaosResult)
    assert result.success is True
    assert result.injected_fault == "generate_load"
    assert result.output == "spike complete"
    assert result.elapsed_time >= 0.0
    assert result.error is None


def test_inject_fails_closed_when_no_load_command_ran() -> None:
    """A clean agent loop that never issued a ``fortio load`` is a failure."""
    fault = GenerateLoadFault(target=LoadTarget(service_url="http://x", qps=1))

    class _IdleAgent:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def run(self, goal: str) -> str:
            return "I decided not to run any load"

    with patch("devops_bench.chaos.agent.ChaosAgent", _IdleAgent):
        result = fault.inject(_make_ctx())

    assert result.success is False
    assert result.error is not None
    assert "no fortio load command" in result.error


def test_inject_fails_closed_when_load_exits_nonzero() -> None:
    """A fortio spike that could not reach the workload (exit != 0) fails closed."""
    fault = GenerateLoadFault(target=LoadTarget(service_url="http://x", qps=1))

    class _FailingLoadAgent:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def run(self, goal: str) -> str:
            _drive_load(self.kwargs, returncode=1)  # connection refused, etc.
            return "spike attempted"

    with patch("devops_bench.chaos.agent.ChaosAgent", _FailingLoadAgent):
        result = fault.inject(_make_ctx())

    assert result.success is False
    assert result.error is not None
    assert "did not reach the workload" in result.error


def test_inject_converts_agent_failure_to_failed_chaos_result() -> None:
    fault = GenerateLoadFault(target=LoadTarget(service_url="http://x", qps=1))

    class _BoomAgent:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def run(self, goal: str) -> str:
            raise RuntimeError("model offline")

    with patch("devops_bench.chaos.agent.ChaosAgent", _BoomAgent):
        result = fault.inject(_make_ctx())

    assert result.success is False
    assert result.injected_fault == "generate_load"
    assert result.error is not None
    assert "model offline" in result.error


def test_inject_threads_chaos_active_event_through_to_agent() -> None:
    fault = GenerateLoadFault(target=LoadTarget(service_url="http://x", qps=1))
    event = threading.Event()

    captured: dict = {}

    class _CapturingAgent:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def run(self, goal: str) -> str:
            return "ok"

    with patch("devops_bench.chaos.agent.ChaosAgent", _CapturingAgent):
        fault.inject(_make_ctx(), chaos_active_event=event)

    assert captured["chaos_active_event"] is event
    assert captured["tool"] is gl.RUN_COMMAND_TOOL
    # The handler is a thin wrapper binding the per-injection load_result; it
    # still delegates to run_chaos_command (sets the event, returns the output).
    handler = captured["tool_handler"]
    fake = CompletedProcess(args=["fortio"], returncode=0, stdout="OUT", stderr="ERR")
    with patch.object(gl, "run", return_value=fake):
        out = handler("fortio load http://x", event)
    assert "Stdout:\nOUT" in out
    assert event.is_set()
    # The system instruction targets the rewritten URL from the spec.
    assert "http://x" in captured["system_instruction"]


def test_goal_dumps_spec_with_target_url() -> None:
    fault = GenerateLoadFault(target=LoadTarget(service_url="http://svc", qps=42))
    goal = fault.goal()
    assert "generate_load" in goal
    assert "http://svc" in goal
    assert "42" in goal


# -- port-forward lifecycle (moved from the harness scenario tests, #33) ------


def _live_popen() -> MagicMock:
    """A fake ``Popen`` that looks like a healthy, still-running tunnel."""
    proc = MagicMock()
    proc.poll.return_value = None  # still running after the settle window
    proc.returncode = None
    return proc


def test_inject_opens_port_forward_and_points_url_at_local_tunnel() -> None:
    """With a target deployment on ``ctx.env``, inject port-forwards + rewrites URL.

    The agent must see ``http://localhost:8080`` (the tunnel), not the
    in-cluster URL, and the tunnel must be terminated when injection finishes.
    """
    fault = GenerateLoadFault(
        target=LoadTarget(service_url="http://example.svc.cluster.local", qps=50)
    )
    proc = _live_popen()
    captured: dict = {}

    class _StubAgent:
        def __init__(self, **kwargs: Any) -> None:
            captured["system_instruction"] = kwargs["system_instruction"]
            self.kwargs = kwargs

        def run(self, goal: str) -> str:
            captured["goal"] = goal
            captured["url_during_run"] = fault.target.service_url
            _drive_load(self.kwargs)
            return "spike complete"

    ctx = _make_ctx({_ENV_TARGET_DEPLOYMENT: "web-app", _ENV_TARGET_NAMESPACE: "prod"})
    with (
        patch.object(k8s_kubectl.subprocess, "Popen", return_value=proc) as popen_mock,
        patch.object(k8s_kubectl.time, "sleep"),  # don't actually sleep the settle window
        # The fault waits for the target rollout before forwarding; stub it so
        # this test isolates the port-forward behavior.
        patch("devops_bench.chaos.faults.generate_load.rollout_status"),
        patch("devops_bench.chaos.agent.ChaosAgent", _StubAgent),
    ):
        result = fault.inject(ctx)

    # Port-forward opened against the threaded deployment / namespace.
    popen_mock.assert_called_once()
    pf_cmd = popen_mock.call_args.args[0]
    assert pf_cmd[:3] == ["kubectl", "port-forward", "deployment/web-app"]
    assert "prod" in pf_cmd
    assert pf_cmd[3] == "8080:8080"

    # The agent saw the local tunnel URL, both in the system prompt and the
    # goal, while the tunnel was open.
    assert "http://localhost:8080" in captured["system_instruction"]
    assert "http://localhost:8080" in captured["goal"]
    assert captured["url_during_run"] == "http://localhost:8080"

    # Tunnel torn down; the fault's stored URL restored afterwards.
    proc.terminate.assert_called_once()
    proc.wait.assert_called()
    assert fault.target.service_url == "http://example.svc.cluster.local"

    assert result.success is True
    assert result.output == "spike complete"


def test_inject_uses_custom_local_port_for_parallel_runs() -> None:
    """``CHAOS_LOCAL_PORT`` binds the local side of the forward and the load URL.

    Parallel runs pass a free local port so two concurrent forwards do not
    contend; the remote (workload) side stays 8080.
    """
    from devops_bench.chaos.faults.generate_load import _ENV_LOCAL_PORT

    fault = GenerateLoadFault(
        target=LoadTarget(service_url="http://example.svc.cluster.local", qps=50)
    )
    proc = _live_popen()
    captured: dict = {}

    class _StubAgent:
        def __init__(self, **kwargs: Any) -> None:
            captured["system_instruction"] = kwargs["system_instruction"]

        def run(self, goal: str) -> str:
            captured["url_during_run"] = fault.target.service_url
            return "spike complete"

    ctx = _make_ctx({_ENV_TARGET_DEPLOYMENT: "web-app", _ENV_LOCAL_PORT: "34567"})
    with (
        patch.object(k8s_kubectl.subprocess, "Popen", return_value=proc) as popen_mock,
        patch.object(k8s_kubectl.time, "sleep"),
        patch("devops_bench.chaos.faults.generate_load.rollout_status"),
        patch("devops_bench.chaos.agent.ChaosAgent", _StubAgent),
    ):
        fault.inject(ctx)

    # Local side is the per-run port; remote side stays the workload's 8080.
    assert popen_mock.call_args.args[0][3] == "34567:8080"
    assert captured["url_during_run"] == "http://localhost:34567"


def test_inject_early_port_forward_exit_becomes_failed_result() -> None:
    """A port-forward that dies in the settle window yields a failed ChaosResult."""
    fault = GenerateLoadFault(target=LoadTarget(service_url="http://x.svc.cluster.local", qps=1))
    dead = MagicMock()
    dead.poll.return_value = 1  # exited during the settle window
    dead.returncode = 1

    ctx = _make_ctx({_ENV_TARGET_DEPLOYMENT: "web-app"})
    with (
        patch.object(k8s_kubectl.subprocess, "Popen", return_value=dead),
        patch.object(k8s_kubectl.time, "sleep"),
        patch("devops_bench.chaos.faults.generate_load.rollout_status"),
        # The agent must never be constructed when the tunnel fails to come up.
        patch(
            "devops_bench.chaos.agent.ChaosAgent",
            side_effect=AssertionError("agent ran despite dead port-forward"),
        ),
    ):
        result = fault.inject(ctx)

    assert result.success is False
    assert result.error is not None
    assert "port-forward exited early" in result.error


def test_inject_skips_port_forward_when_flagged() -> None:
    """``CHAOS_SKIP_PORT_FORWARD`` runs the loop against the existing URL, no Popen."""
    fault = GenerateLoadFault(target=LoadTarget(service_url="http://existing", qps=1))
    captured: dict = {}

    class _StubAgent:
        def __init__(self, **kwargs: Any) -> None:
            captured["system_instruction"] = kwargs["system_instruction"]
            self.kwargs = kwargs

        def run(self, goal: str) -> str:
            captured["url_during_run"] = fault.target.service_url
            _drive_load(self.kwargs, command="fortio load http://existing")
            return "ok"

    ctx = _make_ctx(
        {
            _ENV_TARGET_DEPLOYMENT: "web-app",
            _ENV_SKIP_PORT_FORWARD: "1",
        }
    )
    with (
        patch.object(k8s_kubectl.subprocess, "Popen") as popen_mock,
        patch("devops_bench.chaos.agent.ChaosAgent", _StubAgent),
    ):
        result = fault.inject(ctx)

    popen_mock.assert_not_called()
    # Skip flag means no rewrite — the agent targets the spec's own URL.
    assert captured["url_during_run"] == "http://existing"
    assert "http://existing" in captured["system_instruction"]
    assert result.success is True


def test_inject_without_target_deployment_runs_against_existing_url() -> None:
    """No deployment on ``ctx.env`` -> no port-forward, existing URL preserved."""
    fault = GenerateLoadFault(target=LoadTarget(service_url="http://plain", qps=1))
    captured: dict = {}

    class _StubAgent:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def run(self, goal: str) -> str:
            captured["url_during_run"] = fault.target.service_url
            _drive_load(self.kwargs, command="fortio load http://plain")
            return "ok"

    with (
        patch.object(k8s_kubectl.subprocess, "Popen") as popen_mock,
        patch("devops_bench.chaos.agent.ChaosAgent", _StubAgent),
    ):
        result = fault.inject(_make_ctx())

    popen_mock.assert_not_called()
    assert captured["url_during_run"] == "http://plain"
    assert result.success is True


def test_run_chaos_command_echoing_load_marker_is_not_a_spike() -> None:
    """A command that merely mentions ``fortio load`` must not count as one."""
    event = threading.Event()
    load_result: dict[str, Any] = {}
    fake = CompletedProcess(args=["echo"], returncode=0, stdout="fortio load", stderr="")
    with patch.object(gl, "run", return_value=fake):
        out = run_chaos_command('echo "fortio load"', event, load_result=load_result)

    assert "Stdout" in out
    assert not event.is_set()
    assert load_result == {}  # the spike was never attempted, so nothing recorded


def test_inject_invalid_local_port_becomes_failed_result() -> None:
    """A malformed CHAOS_LOCAL_PORT fails the fault instead of escaping inject."""
    fault = GenerateLoadFault(target=LoadTarget(service_url="http://svc", qps=1))

    result = fault.inject(_make_ctx({gl._ENV_LOCAL_PORT: "not-a-port"}))

    assert result.success is False
    assert result.error is not None
    assert "not-a-port" in result.error


def test_inject_port_forward_setup_failure_becomes_failed_result() -> None:
    """A port-forward that raises at construction yields a failed ChaosResult."""
    fault = GenerateLoadFault(target=LoadTarget(service_url="http://x.svc.cluster.local", qps=1))
    ctx = _make_ctx({_ENV_TARGET_DEPLOYMENT: "web-app"})
    with (
        patch.object(gl, "rollout_status"),
        patch.object(gl, "port_forward", side_effect=RuntimeError("kubectl missing")),
        patch(
            "devops_bench.chaos.agent.ChaosAgent",
            side_effect=AssertionError("agent ran despite failed port-forward setup"),
        ),
    ):
        result = fault.inject(ctx)

    assert result.success is False
    assert result.error is not None
    assert "kubectl missing" in result.error


class TestLoadCommandTimeout:
    """A spike must outlive its own ``-t``; everything else keeps the flat cap."""

    def test_spike_timeout_covers_the_declared_duration(self) -> None:
        # 300s is what optimize-scale declares; the flat 40s ceiling killed it mid-spike.
        argv = ["fortio", "load", "-qps", "300", "-t", "300s", "-c", "2", "http://localhost:8080"]
        assert gl._command_timeout(argv, is_load=True) > 300

    def test_spike_timeout_is_bounded(self) -> None:
        argv = ["fortio", "load", "-t", "24h", "http://localhost:8080"]
        assert gl._command_timeout(argv, is_load=True) == gl._LOAD_TIMEOUT_CEILING_SEC

    def test_non_load_command_keeps_the_flat_ceiling(self) -> None:
        assert gl._command_timeout(["kubectl", "get", "pods"], is_load=False) == gl._COMMAND_TIMEOUT

    def test_unparsable_duration_falls_back_rather_than_guessing(self) -> None:
        argv = ["fortio", "load", "-t", "banana", "http://localhost:8080"]
        assert gl._command_timeout(argv, is_load=True) == gl._COMMAND_TIMEOUT

    @pytest.mark.parametrize("flag", ["-t=300s", "--t=300s"])
    def test_the_equals_flag_form_is_honored(self, flag: str) -> None:
        # Go's flag package accepts -t=300s; missing it re-creates the 40s kill.
        argv = ["fortio", "load", flag, "http://localhost:8080"]
        assert gl._command_timeout(argv, is_load=True) > 300

    def test_the_double_dash_flag_form_is_honored(self) -> None:
        argv = ["fortio", "load", "--t", "300s", "http://localhost:8080"]
        assert gl._command_timeout(argv, is_load=True) > 300

    def test_load_without_a_duration_flag_keeps_the_flat_ceiling(self) -> None:
        assert gl._command_timeout(["fortio", "load", "http://x"], is_load=True) == (
            gl._COMMAND_TIMEOUT
        )

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("300s", 300.0), ("5m", 300.0), ("1h30m", 5400.0), ("250ms", 0.25), ("nope", None)],
    )
    def test_go_duration_parsing(self, value: str, expected: float | None) -> None:
        assert gl._go_duration_seconds(value) == expected


class TestToolOutputClamp:
    """One chatty load run must not exhaust the model's context."""

    def test_short_output_is_passed_through_untouched(self) -> None:
        assert gl._clamp_tool_output("Stdout: done\n") == "Stdout: done\n"

    def test_a_per_request_log_is_bounded_and_keeps_both_ends(self) -> None:
        # fortio logs a line per request; a 300s spike at 300 QPS is what
        # overflowed a one-million-token context on the recorded runs.
        text = "HEAD-MARKER\n" + "".join(f"request {i} ok\n" for i in range(90_000))
        text += "\nAll done 90000 calls\nTAIL-MARKER"

        clamped = gl._clamp_tool_output(text)

        assert len(clamped) < len(text)
        # The summary is the whole reason the output is read at all.
        assert clamped.endswith("TAIL-MARKER")
        assert "All done 90000 calls" in clamped
        assert clamped.startswith("HEAD-MARKER")
        # Told it was elided, rather than silently shown a truncated log.
        assert "elided by the harness" in clamped

    def test_the_elision_marker_reports_how_much_went_missing(self) -> None:
        text = "x" * (gl._MAX_TOOL_OUTPUT_CHARS + 500)
        assert "[500 characters elided" in gl._clamp_tool_output(text)

    def test_output_at_the_limit_is_not_clamped(self) -> None:
        text = "y" * gl._MAX_TOOL_OUTPUT_CHARS
        assert gl._clamp_tool_output(text) == text


class TestLoadTimeoutFailsClosed:
    """A spike killed by its own timeout must not read as a spike that ran."""

    def test_a_timed_out_spike_is_recorded_as_not_ok(self) -> None:
        """The exact shape of the 8-of-8 failure, pinned.

        Under the old flat 40s ceiling a declared 300s spike was killed here
        every time. Note the exception type: ``core.subprocess.run`` catches
        ``TimeoutExpired`` and re-raises ``SubprocessError`` with
        ``returncode=-1``, which is why the recorded runs showed the fault
        exiting -1 rather than timing out. The handler has to mark the spike
        attempted-and-not-ok so ``inject`` fails closed; if it left
        ``load_result`` untouched the fault would report success for a spike
        the harness had just SIGKILLed.
        """
        load_result: dict[str, Any] = {}
        killed = SubprocessError(["fortio"], returncode=-1, stdout="", stderr="")
        with patch.object(gl, "run", side_effect=killed):
            out = run_chaos_command(
                "fortio load -t 300s http://localhost:8080", load_result=load_result
            )

        assert load_result["attempted"] is True
        assert load_result["ok"] is False
        # None, not -1: the spike record says "no status reported", distinct
        # from the -1 the subprocess layer synthesizes for a kill.
        assert load_result["returncode"] is None
        assert "SubprocessError" in load_result["error"]
        assert "-1" in load_result["error"]
        assert out.startswith("Error:")

    def test_a_non_load_command_that_raises_leaves_the_spike_record_alone(self) -> None:
        """Only a real spike may write the spike record."""
        load_result: dict[str, Any] = {}
        killed = SubprocessError(["kubectl"], returncode=-1, stdout="", stderr="")
        with patch.object(gl, "run", side_effect=killed):
            out = run_chaos_command("kubectl get pods", load_result=load_result)

        assert load_result == {}
        assert out.startswith("Error:")

    def test_a_timeout_error_is_clamped_before_reaching_the_model(self) -> None:
        # SubprocessError's message embeds the captured stderr, so an unclamped
        # timeout could hand the model everything the clamp exists to drop.
        err = SubprocessError(["fortio", "load"], returncode=-1, stderr="x" * 100_000)
        with patch.object(gl, "run", side_effect=err):
            out = run_chaos_command("fortio load -t 300s http://localhost:8080")
        assert out.startswith("Error:")
        assert len(out) <= gl._MAX_TOOL_OUTPUT_CHARS + 200
        assert "elided by the harness" in out

    def test_the_declared_optimize_scale_spike_gets_its_full_duration_plus_slack(self) -> None:
        """Exact value, not just "more than 300"; 300s + 60s slack."""
        argv = ["fortio", "load", "-qps", "300", "-t", "300s", "-c", "2", "http://localhost:8080"]
        assert gl._command_timeout(argv, is_load=True) == 360.0


def _fortio_shim(tmp_path: Path, body: str) -> Path:
    """Write an executable stand-in named ``fortio`` so ``is_load`` is real.

    ``run_chaos_command`` decides a command is a spike from the parsed argv
    (basename ``fortio``, subcommand ``load``), so exercising the real
    subprocess path needs a real binary with that name rather than a mock.
    """
    shim = tmp_path / "fortio"
    shim.write_text(f"#!/bin/sh\n{body}\n")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return shim


class TestLoadTimeoutAgainstARealSubprocess:
    """Drive a real child process: mocks cannot catch a bad argument handed to run."""

    def test_a_declared_spike_outlives_the_flat_ceiling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gl, "_COMMAND_TIMEOUT", 0.5)
        shim = _fortio_shim(tmp_path, "sleep 2\necho 'All done 100 calls'")
        load_result: dict[str, Any] = {}

        started = time.monotonic()
        out = run_chaos_command(f"{shim} load -t 2s http://localhost:8080", load_result=load_result)
        elapsed = time.monotonic() - started

        # It ran to completion instead of dying at the 0.5s flat ceiling.
        assert load_result["ok"] is True
        assert load_result["returncode"] == 0
        assert "All done 100 calls" in out
        assert elapsed >= 2

    def test_a_command_with_no_declared_duration_still_hits_the_flat_ceiling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ceiling is not simply gone — an undeclared runaway is still cut off."""
        monkeypatch.setattr(gl, "_COMMAND_TIMEOUT", 0.5)
        shim = _fortio_shim(tmp_path, "sleep 5")
        load_result: dict[str, Any] = {}

        started = time.monotonic()
        out = run_chaos_command(f"{shim} load http://localhost:8080", load_result=load_result)
        elapsed = time.monotonic() - started

        assert load_result["ok"] is False
        # The real kill path, end to end: TimeoutExpired -> SubprocessError(-1).
        assert "SubprocessError" in load_result["error"]
        assert "exit code -1" in load_result["error"]
        assert out.startswith("Error:")
        assert elapsed < 5

    def test_a_chatty_spike_comes_back_bounded(self, tmp_path: Path) -> None:
        """The clamp applies to real captured output, not just to a string."""
        shim = _fortio_shim(
            tmp_path,
            'i=0\nwhile [ $i -lt 40000 ]; do echo "request $i ok"; i=$((i+1)); done\n'
            "echo 'All done 40000 calls'",
        )
        load_result: dict[str, Any] = {}

        out = run_chaos_command(f"{shim} load -t 1s http://localhost:8080", load_result=load_result)

        assert load_result["ok"] is True
        assert len(out) <= gl._MAX_TOOL_OUTPUT_CHARS + 500
        # The summary survives the clamp; that is the whole point of keeping the tail.
        assert "All done 40000 calls" in out
        assert "elided by the harness" in out
