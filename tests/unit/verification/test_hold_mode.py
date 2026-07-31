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

"""Unit tests for hold-mode sampling in the entry-level runner.

Windows are kept sub-second (or fully clock-mocked) throughout, so this
module runs in well under a second: no test here waits on a real multi-second
window.
"""

from typing import Any, Literal

import pytest

from devops_bench.verification.base import VERIFIERS, BaseVerifier, VerificationResult
from devops_bench.verification.runner import VerifierAgent
from devops_bench.verification.spec import parse_entries


@VERIFIERS.register("hold_stub")
class _Stepping(BaseVerifier):
    """Test double returning a scripted status per call, in declaration order.

    Attributes:
        statuses: Per-call outcomes; the last entry repeats once exhausted,
            so a scripted list shorter than the eventual sample count still
            behaves sensibly.
        calls: Total number of times :meth:`verify` has been invoked.
    """

    type: Literal["hold_stub"] = "hold_stub"
    statuses: list[str] = []
    calls: int = 0

    def verify(self, timeout_sec: float) -> VerificationResult:
        idx = min(self.calls, len(self.statuses) - 1)
        status = self.statuses[idx]
        self.calls += 1
        return VerificationResult(
            success=status == "pass",
            status=status,
            elapsed_time=0.0,
            reason=f"sample {self.calls}: {status}",
            name=self.name,
        )


def _entry(statuses: list[str], **extra: Any) -> Any:
    payload = {
        "name": "e",
        "role": "safeguard",
        "severity": "recoverable",
        "mode": "hold",
        "check": {"type": "hold_stub", "statuses": statuses},
    }
    payload.update(extra)
    entries, errors = parse_entries([payload])
    assert errors == []
    return entries[0]


def test_hold_all_pass_window_samples_at_least_twice_and_names_the_count() -> None:
    entry = _entry(["pass"] * 8, hold_window_sec=0.05, hold_poll_interval_sec=0.01)

    result = VerifierAgent().run_entry(entry, timeout_sec=5)

    assert result.status == "pass"
    assert result.success is True
    assert entry.check.calls >= 2
    assert f"{entry.check.calls} sample" in result.reason


def test_hold_fails_fast_on_the_failing_sample_and_stops_sampling() -> None:
    entry = _entry(
        ["pass", "pass", "fail", "pass"], hold_window_sec=0.05, hold_poll_interval_sec=0.01
    )

    result = VerifierAgent().run_entry(entry, timeout_sec=30)

    assert result.status == "fail"
    assert result.success is False
    # No sample past the failing one, so this stops after the third sample
    # regardless of how large the configured window is.
    assert entry.check.calls == 3
    assert "hold failed at sample 3" in result.reason
    assert len(result.children) == 1
    assert result.children[0].status == "fail"


def test_hold_error_tainted_window_continues_sampling_and_reports_error() -> None:
    entry = _entry(
        ["pass", "error", "pass", "error", "pass"],
        hold_window_sec=0.05,
        hold_poll_interval_sec=0.01,
    )

    result = VerifierAgent().run_entry(entry, timeout_sec=5)

    assert result.status == "error"
    assert result.success is False
    # Sampling continued well past the first error sample (index 1).
    assert entry.check.calls >= 4
    assert "error sample" in result.reason


def test_hold_truncation_upfront_is_immediate_error_with_zero_samples() -> None:
    entry = _entry(["pass"], hold_window_sec=10, hold_poll_interval_sec=1)

    result = VerifierAgent().run_entry(entry, timeout_sec=0.05)

    assert result.status == "error"
    assert result.success is False
    assert entry.check.calls == 0
    assert "cannot be observed" in result.reason
    assert result.children == []


def test_hold_mid_window_deadline_expiry_reports_partial_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deadline that lands strictly before the window completes is an error.

    Exercises ``_hold`` directly with a scripted clock: the upfront
    remaining-budget check and the window's own start are read from separate
    ``time.monotonic()`` calls, so a caller-supplied deadline that does not
    actually cover the full window despite passing that upfront check must
    still be caught mid-loop rather than reported as a completed window.
    """
    node = _Stepping(statuses=["pass"] * 5)
    times = iter([0.0, 15.0, 18.0, 21.0])
    monkeypatch.setattr("devops_bench.verification.runner.time.monotonic", lambda: next(times))
    monkeypatch.setattr("devops_bench.verification.runner.time.sleep", lambda _: None)

    result = VerifierAgent()._hold(node, window_sec=10.0, interval_sec=1.0, deadline=20.0)

    assert result.status == "error"
    assert result.success is False
    assert node.calls == 2
    assert "cut short" in result.reason
    assert "2 sample" in result.reason


def test_hold_samples_a_compound_subtree_correctly() -> None:
    payload = {
        "name": "e",
        "role": "safeguard",
        "severity": "recoverable",
        "mode": "hold",
        "hold_window_sec": 0.05,
        "hold_poll_interval_sec": 0.01,
        "check": {
            "type": "all",
            "checks": [
                {"type": "hold_stub", "statuses": ["pass"] * 8},
                {"type": "hold_stub", "statuses": ["pass"] * 8},
            ],
        },
    }
    entries, errors = parse_entries([payload])
    assert errors == []

    result = VerifierAgent().run_entry(entries[0], timeout_sec=5)

    assert result.status == "pass"
    assert result.success is True
    # The hold's own children carry the last sample: the `all` node's result.
    assert len(result.children) == 1
    last_sample = result.children[0]
    assert len(last_sample.children) == 2
    assert all(c.status == "pass" for c in last_sample.children)
    leaves = entries[0].check.checks
    assert all(leaf.calls >= 2 for leaf in leaves)
