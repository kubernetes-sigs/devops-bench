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

from devops_bench.evalharness.default import DefaultEvalHarness
from devops_bench.evalharness.hold import HoldObservation, SafeguardMonitor
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
