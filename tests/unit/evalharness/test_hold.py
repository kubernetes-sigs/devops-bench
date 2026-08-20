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

"""Unit tests for :mod:`devops_bench.evalharness.hold`.

Fake leaves stand in for real cluster I/O so these tests run fast and never
touch kubectl. ``_FlipThenRestore`` is the ``_Countdown``-shaped test double
from ``tests/unit/verification/test_combinators.py``, adapted to the exact
shape of the motivating bug: fails once, mid-run, then recovers before the
run ends.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Literal

import pytest

from devops_bench.agents.result import AgentResult
from devops_bench.evalharness.default import DefaultEvalHarness
from devops_bench.evalharness.hold import (
    HoldObservation,
    SafeguardMonitor,
    _default_poll_interval,
    _fold_sample,
    hold_verdict,
    run_hold_window,
)
from devops_bench.tasks import Task
from devops_bench.verification.base import VERIFIERS, BaseVerifier, VerificationResult
from devops_bench.verification.spec import VerificationEntry, parse_entries

_POLL_INTERVAL_SEC = 0.02
_SAMPLE_WINDOW_SEC = 0.15


@VERIFIERS.register("sg_always_pass")
class _AlwaysPass(BaseVerifier):
    """Test double that always reports the condition holding."""

    type: Literal["sg_always_pass"] = "sg_always_pass"
    calls: int = 0

    def verify(self, timeout_sec: float) -> VerificationResult:
        self.calls += 1
        return VerificationResult(success=True, elapsed_time=0.0, reason="held", name=self.name)


@VERIFIERS.register("sg_flip")
class _FlipThenRestore(BaseVerifier):
    """Fails on sample number ``fail_at`` only, holds on every other sample.

    Models the actual T-024 failure: the safeguard is violated mid-run and
    restored before the run ends, so a check that only samples at the end
    never sees it.
    """

    type: Literal["sg_flip"] = "sg_flip"
    fail_at: int = 2
    calls: int = 0

    def verify(self, timeout_sec: float) -> VerificationResult:
        self.calls += 1
        if self.calls == self.fail_at:
            return VerificationResult(
                success=False, elapsed_time=0.0, reason="dropped mid-run", name=self.name
            )
        return VerificationResult(success=True, elapsed_time=0.0, reason="held", name=self.name)


@VERIFIERS.register("sg_error")
class _AlwaysErrors(BaseVerifier):
    """Test double that always reports a check-could-not-run error, never a violation."""

    type: Literal["sg_error"] = "sg_error"
    calls: int = 0

    def verify(self, timeout_sec: float) -> VerificationResult:
        self.calls += 1
        return VerificationResult(
            success=False,
            status="error",
            elapsed_time=0.0,
            reason="transient kubectl failure",
            name=self.name,
        )


@VERIFIERS.register("sg_raise")
class _RaisingLeaf(BaseVerifier):
    """Test double whose ``verify`` always raises, to prove the monitor survives it."""

    type: Literal["sg_raise"] = "sg_raise"

    def verify(self, timeout_sec: float) -> VerificationResult:
        raise RuntimeError("boom")


def _hold_entry(check: dict[str, Any], **extra: Any) -> VerificationEntry:
    payload = {
        "name": "e",
        "role": "safeguard",
        "severity": "catastrophic",
        "mode": "hold",
        "check": check,
    }
    payload.update(extra)
    entries, errors = parse_entries([payload])
    assert errors == []
    return entries[0]


def _objective_hold_entry(check: dict[str, Any], **extra: Any) -> VerificationEntry:
    payload = {
        "name": "e",
        "role": "objective",
        "mode": "hold",
        "hold_window_sec": _SAMPLE_WINDOW_SEC,
        "check": check,
    }
    payload.update(extra)
    entries, errors = parse_entries([payload])
    assert errors == []
    return entries[0]


def test_hold_that_holds_throughout_is_not_reported_as_violated() -> None:
    entry = _hold_entry({"type": "sg_always_pass"}, hold_poll_interval_sec=_POLL_INTERVAL_SEC)
    monitor = SafeguardMonitor([entry])
    monitor.start()
    time.sleep(_SAMPLE_WINDOW_SEC)
    monitor.stop()

    obs = monitor.get_observations()[entry.name]
    assert obs.violated is False
    assert obs.error_count == 0
    assert obs.sample_count >= 2


def test_a_violation_restored_before_the_run_ends_still_fails_the_hold_entry() -> None:
    """Regression test for the T-024 replica-floor bug this monitor exists to fix."""
    entry = _hold_entry(
        {"type": "sg_flip", "fail_at": 2}, hold_poll_interval_sec=_POLL_INTERVAL_SEC
    )
    monitor = SafeguardMonitor([entry])
    monitor.start()
    time.sleep(_SAMPLE_WINDOW_SEC)  # several samples: pass, FAIL, pass, pass, ...
    monitor.stop()

    obs = monitor.get_observations()[entry.name]
    assert obs.violated is True
    assert obs.first_violation_reason == "dropped mid-run"
    assert obs.first_violation_at_sec is not None
    # The condition recovered and later samples kept passing; violated must
    # not be cleared by a later, healthy sample.
    assert obs.sample_count >= 3


def test_a_check_that_errors_repeatedly_is_not_reported_as_a_violation() -> None:
    entry = _hold_entry({"type": "sg_error"}, hold_poll_interval_sec=_POLL_INTERVAL_SEC)
    monitor = SafeguardMonitor([entry])
    monitor.start()
    time.sleep(_SAMPLE_WINDOW_SEC)
    monitor.stop()

    obs = monitor.get_observations()[entry.name]
    assert obs.violated is False
    assert obs.sample_count >= 2
    assert obs.error_count == obs.sample_count


def test_a_leaf_that_raises_does_not_crash_the_monitor_thread() -> None:
    """An unexpected exception inside a sample must not propagate or stop sampling."""
    entry = _hold_entry({"type": "sg_raise"}, hold_poll_interval_sec=_POLL_INTERVAL_SEC)
    monitor = SafeguardMonitor([entry])
    monitor.start()
    time.sleep(_SAMPLE_WINDOW_SEC)
    monitor.stop()

    obs = monitor.get_observations()[entry.name]
    # Sampling more than once after the first raise proves the loop survived
    # it rather than dying silently on the first exception.
    assert obs.sample_count >= 2
    assert obs.error_count == obs.sample_count
    assert obs.violated is False


def test_hold_entry_with_zero_samples_does_not_silently_pass() -> None:
    entry = _hold_entry({"type": "sg_always_pass"})

    never_sampled = DefaultEvalHarness._hold_report_entry(entry, None)  # noqa: SLF001
    zero_samples = DefaultEvalHarness._hold_report_entry(  # noqa: SLF001
        entry, HoldObservation()
    )

    for row in (never_sampled, zero_samples):
        assert row["success"] is False
        assert row["status"] == "error"
        assert "never sampled" in row["reason"]
        assert row["hold_sample_count"] == 0


def test_get_observations_returns_a_snapshot_independent_of_further_sampling() -> None:
    entry = _hold_entry({"type": "sg_always_pass"}, hold_poll_interval_sec=_POLL_INTERVAL_SEC)
    monitor = SafeguardMonitor([entry])
    monitor.start()
    time.sleep(_SAMPLE_WINDOW_SEC)
    snapshot = monitor.get_observations()
    snapshot[entry.name].sample_count = 999
    monitor.stop()

    assert monitor.get_observations()[entry.name].sample_count != 999


def test_start_is_a_no_op_with_no_hold_entries() -> None:
    monitor = SafeguardMonitor([])
    monitor.start()
    monitor.stop()
    assert monitor.get_observations() == {}


def test_mode_hold_now_parses_instead_of_raising() -> None:
    """Was rejected outright at the schema level; hold now parses like any other mode."""
    entry = _hold_entry({"type": "sg_always_pass"})
    assert entry.resolved_mode == "hold"


def test_run_one_stops_and_joins_the_safeguard_monitor_when_the_agent_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: an agent exception must not leak the monitor's background thread."""
    harness = DefaultEvalHarness(project_id="p", cluster_name="c")

    def _boom(prompt: str, ctx: Any) -> Any:
        raise RuntimeError("agent crashed")

    monkeypatch.setattr(harness, "execute_agent", _boom)
    monkeypatch.setattr(harness, "_run_verification", lambda entries, **kwargs: [])
    task = Task.from_dict(
        {
            "task_id": "t",
            "name": "demo",
            "prompt": "p",
            "infrastructure": {"deployer": "noop"},
            "verification_spec": [
                {
                    "name": "no-scale-down",
                    "role": "safeguard",
                    "severity": "catastrophic",
                    "mode": "hold",
                    "check": {"type": "sg_always_pass"},
                }
            ],
        }
    )

    record = harness._run_one(task, tmp_path)  # noqa: SLF001

    assert record["status"] == "failed"
    assert not any(t.name == "safeguard-monitor" for t in threading.enumerate())


# --- _default_poll_interval: validating BENCH_HOLD_INTERVAL_SEC -----------


def test_default_poll_interval_parses_a_valid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BENCH_HOLD_INTERVAL_SEC", "2.5")
    assert _default_poll_interval() == 2.5


def test_default_poll_interval_falls_back_to_5_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BENCH_HOLD_INTERVAL_SEC", raising=False)
    assert _default_poll_interval() == 5.0


def test_default_poll_interval_falls_back_to_5_on_a_malformed_value(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("BENCH_HOLD_INTERVAL_SEC", "soon")
    with caplog.at_level("WARNING"):
        assert _default_poll_interval() == 5.0
    assert "BENCH_HOLD_INTERVAL_SEC" in caplog.text


def test_default_poll_interval_falls_back_to_5_on_zero(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("BENCH_HOLD_INTERVAL_SEC", "0")
    with caplog.at_level("WARNING"):
        assert _default_poll_interval() == 5.0
    assert "BENCH_HOLD_INTERVAL_SEC" in caplog.text


def test_default_poll_interval_falls_back_to_5_on_a_negative_value(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("BENCH_HOLD_INTERVAL_SEC", "-1.0")
    with caplog.at_level("WARNING"):
        assert _default_poll_interval() == 5.0
    assert "BENCH_HOLD_INTERVAL_SEC" in caplog.text


@pytest.mark.parametrize("raw", ["inf", "-inf", "nan"])
def test_default_poll_interval_falls_back_to_5_on_non_finite_values(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, raw: str
) -> None:
    monkeypatch.setenv("BENCH_HOLD_INTERVAL_SEC", raw)
    with caplog.at_level("WARNING"):
        assert _default_poll_interval() == 5.0
    assert "BENCH_HOLD_INTERVAL_SEC" in caplog.text


# --- hold_verdict: every branch, in order --------------------------------


def _result(**overrides: Any) -> VerificationResult:
    base: dict[str, Any] = {"success": True, "elapsed_time": 0.0, "reason": "held"}
    base.update(overrides)
    return VerificationResult(**base)


def test_hold_verdict_zero_samples_is_error() -> None:
    success, status, reason = hold_verdict(HoldObservation())
    assert success is False
    assert status == "error"
    assert "never sampled" in reason


def test_hold_verdict_every_sample_errored_is_error() -> None:
    obs = HoldObservation()
    for _ in range(3):
        _fold_sample(obs, _result(success=False, status="error", reason="boom"), 0.0)

    success, status, reason = hold_verdict(obs)
    assert success is False
    assert status == "error"
    assert "could never be evaluated" in reason


def test_hold_verdict_a_single_sample_that_errored_is_still_an_error() -> None:
    """A window with exactly one sample, and that sample errored, is caught by the
    all-errored rule (error_count == sample_count), not by the trailing-error rule."""
    obs = HoldObservation()
    _fold_sample(obs, _result(success=False, status="error", reason="boom"), 0.0)

    success, status, reason = hold_verdict(obs)
    assert success is False
    assert status == "error"
    assert "could never be evaluated" in reason


def test_hold_verdict_a_window_that_errors_then_recovers_and_ends_clean_is_a_pass() -> None:
    """Regression: an error absorbed mid-window must not sink an otherwise clean hold."""
    obs = HoldObservation()
    _fold_sample(obs, _result(success=False, status="error", reason="transient blip"), 0.0)
    _fold_sample(obs, _result(success=True, reason="held"), 1.0)

    success, status, reason = hold_verdict(obs)
    assert success is True
    assert status == "pass"
    assert obs.error_count == 1


def test_hold_verdict_a_single_trailing_error_after_clean_samples_is_a_pass() -> None:
    """A single errored sample at the end of the window is absorbed as noise.

    Was previously an "error": one transient kubectl blip on the last poll
    used to null the whole entry. Now the trailing-error rule only fires on
    a sustained run of HOLD_TRAILING_ERROR_SAMPLES consecutive errors, so a
    lone trailing error is treated the same as any other absorbed error.
    """
    obs = HoldObservation()
    _fold_sample(obs, _result(success=True, reason="held"), 0.0)
    _fold_sample(obs, _result(success=False, status="error", reason="transient blip"), 1.0)

    success, status, reason = hold_verdict(obs)
    assert success is True
    assert status == "pass"
    assert obs.error_count == 1


def test_hold_verdict_a_window_ending_on_two_consecutive_errors_is_an_error() -> None:
    """Regression: a window that never recovers by its end must not read as a pass."""
    obs = HoldObservation()
    _fold_sample(obs, _result(success=True, reason="held"), 0.0)
    _fold_sample(obs, _result(success=False, status="error", reason="blip one"), 1.0)
    _fold_sample(obs, _result(success=False, status="error", reason="etcd unreachable"), 2.0)

    success, status, reason = hold_verdict(obs)
    assert success is False
    assert status == "error"
    assert "never recovered" in reason
    assert obs.error_count < obs.sample_count  # not every sample errored


def test_hold_verdict_a_sustained_trailing_error_reports_the_last_error_reason() -> None:
    """Regression: the trailing-error verdict must carry the final sample's own reason.

    The previous check above only asserts the string "never recovered", which
    is also present in the verdict's generic boilerplate ("it never
    recovered") regardless of any per-sample reason, so it does not actually
    prove a sample's own reason reaches the verdict. Use a reason that shares
    no words with that boilerplate to prove it.
    """
    obs = HoldObservation()
    _fold_sample(obs, _result(success=True, reason="held"), 0.0)
    _fold_sample(obs, _result(success=False, status="error", reason="blip one"), 1.0)
    _fold_sample(
        obs, _result(success=False, status="error", reason="etcd unreachable in region-west"), 2.0
    )

    success, status, reason = hold_verdict(obs)
    assert success is False
    assert status == "error"
    assert "etcd unreachable in region-west" in reason
    assert obs.last_error_reason == "etcd unreachable in region-west"


def test_hold_verdict_a_mid_window_error_that_recovers_resets_the_trailing_run() -> None:
    """A middle error followed by a clean sample resets the run, so a later single
    trailing error still passes rather than accumulating across the recovery."""
    obs = HoldObservation()
    _fold_sample(obs, _result(success=False, status="error", reason="blip one"), 0.0)
    _fold_sample(obs, _result(success=True, reason="held"), 1.0)
    _fold_sample(obs, _result(success=False, status="error", reason="blip two"), 2.0)

    success, status, reason = hold_verdict(obs)
    assert success is True
    assert status == "pass"
    assert obs.error_count == 2


def test_hold_verdict_a_violation_is_a_fail_regardless_of_later_recovery() -> None:
    obs = HoldObservation()
    _fold_sample(obs, _result(success=False, reason="replicas dropped to 2"), 3.5)
    _fold_sample(obs, _result(success=True, reason="held"), 4.5)

    success, status, reason = hold_verdict(obs)
    assert success is False
    assert status == "fail"
    assert "replicas dropped to 2" in reason
    assert "3.5" in reason


def test_hold_verdict_a_violation_followed_by_a_trailing_error_is_still_a_fail() -> None:
    """Regression: a confirmed violation must not be masked by a trailing error sample.

    A violation is a positive observation; losing observability afterward
    does not un-observe it. Scoring nulls a task entirely on ``error`` but
    scores ``fail`` as a fail, so violated must be checked before the
    last-sample-error case or a genuine violation would drop out of scoring
    instead of failing the task.
    """
    obs = HoldObservation()
    _fold_sample(obs, _result(success=False, reason="replicas dropped to 2"), 3.5)
    _fold_sample(obs, _result(success=False, status="error", reason="never recovered"), 4.5)

    success, status, reason = hold_verdict(obs)
    assert success is False
    assert status == "fail"
    assert "replicas dropped to 2" in reason


def test_hold_verdict_a_clean_pass_notes_absorbed_errors() -> None:
    obs = HoldObservation()
    _fold_sample(obs, _result(success=True, reason="held"), 0.0)
    _fold_sample(obs, _result(success=False, status="error", reason="blip"), 1.0)
    _fold_sample(obs, _result(success=True, reason="held"), 2.0)

    success, status, reason = hold_verdict(obs)
    assert success is True
    assert status == "pass"
    assert obs.error_count == 1
    assert "1" in reason


# --- run_hold_window: the post-run objective driver -----------------------


def test_run_hold_window_keeps_sampling_after_a_violation_and_still_reports_fail() -> None:
    entry = _objective_hold_entry(
        {"type": "sg_flip", "fail_at": 2}, hold_poll_interval_sec=_POLL_INTERVAL_SEC
    )

    obs = run_hold_window(
        entry,
        entry.hold_window_sec,
        interval_sec=_POLL_INTERVAL_SEC,
        deadline=time.monotonic() + 10.0,
    )

    assert obs.violated is True
    assert obs.first_violation_reason == "dropped mid-run"
    # Sampling continued past the violation to the end of the window rather
    # than exiting early on it.
    assert obs.sample_count >= 3


def test_run_hold_window_stops_early_when_the_callers_deadline_is_reached() -> None:
    entry = _objective_hold_entry(
        {"type": "sg_always_pass"},
        hold_window_sec=10.0,
        hold_poll_interval_sec=_POLL_INTERVAL_SEC,
    )
    deadline = time.monotonic() + _SAMPLE_WINDOW_SEC

    start = time.monotonic()
    obs = run_hold_window(
        entry, entry.hold_window_sec, interval_sec=_POLL_INTERVAL_SEC, deadline=deadline
    )
    elapsed = time.monotonic() - start

    # The window itself asked for 10s; the shared deadline cut it off much
    # sooner, proving the caller's deadline bounds the window rather than
    # the window overrunning the shared verification budget.
    assert elapsed < 5.0
    assert obs.sample_count >= 1


def test_run_hold_window_with_an_already_passed_deadline_takes_no_samples() -> None:
    entry = _objective_hold_entry({"type": "sg_always_pass"}, hold_window_sec=10.0)

    obs = run_hold_window(
        entry, entry.hold_window_sec, interval_sec=_POLL_INTERVAL_SEC, deadline=time.monotonic()
    )

    assert obs.sample_count == 0


def test_run_verification_raises_when_an_objective_hold_entry_has_no_hold_window_sec() -> None:
    """Regression: a missing hold_window_sec must not reach a float parameter unchecked.

    VerificationEntry's own validation normally rejects this at spec-parse
    time; ``model_copy`` bypasses that validation here to simulate an entry
    reaching verification with the invariant already broken.
    """
    entry = _objective_hold_entry({"type": "sg_always_pass"})
    broken_entry = entry.model_copy(update={"hold_window_sec": None})
    harness = DefaultEvalHarness(project_id="p", cluster_name="c")

    with pytest.raises(ValueError, match="hold_window_sec"):
        harness._run_verification([broken_entry])  # noqa: SLF001


# --- role-based dispatch: the landmine regression --------------------------


def test_objective_hold_entry_is_routed_to_run_hold_window_not_the_live_monitor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: role: objective, mode: hold must never reach the live monitor.

    Sampling an objective through SafeguardMonitor would evaluate it before
    the agent has done anything and latch a spurious violation on the first
    sample. The live monitor must only ever be constructed with the
    safeguard-role subset of an entry's hold entries.
    """
    monitor_entries: list[list[VerificationEntry]] = []
    orig_init = SafeguardMonitor.__init__

    def _capture_init(self: SafeguardMonitor, entries: list[VerificationEntry]) -> None:
        monitor_entries.append(list(entries))
        orig_init(self, entries)

    monkeypatch.setattr(SafeguardMonitor, "__init__", _capture_init)

    harness = DefaultEvalHarness(project_id="p", cluster_name="c")
    monkeypatch.setattr(
        harness, "execute_agent", lambda prompt, ctx: AgentResult(output="ok", trajectory=[])
    )
    task = Task.from_dict(
        {
            "task_id": "t",
            "name": "demo",
            "prompt": "p",
            "infrastructure": {"deployer": "noop"},
            "verification_spec": [
                {
                    "name": "eventually-healthy",
                    "role": "objective",
                    "mode": "hold",
                    "hold_window_sec": _SAMPLE_WINDOW_SEC,
                    "hold_poll_interval_sec": _POLL_INTERVAL_SEC,
                    "check": {"type": "sg_always_pass"},
                }
            ],
        }
    )

    record = harness._run_one(task, tmp_path)  # noqa: SLF001

    assert len(monitor_entries) == 1
    assert monitor_entries[0] == []  # the objective entry never reached the live monitor
    hold_rows = [row for row in record["verification_report"] if row["mode"] == "hold"]
    assert len(hold_rows) == 1
    assert hold_rows[0]["success"] is True
    assert hold_rows[0]["hold_sample_count"] >= 1
