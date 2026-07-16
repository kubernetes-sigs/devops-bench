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

"""Unit tests for devops_bench.k8s.conditions.

A fake clock and fake sleep drive ``poll_until`` so the backoff schedule is
verified without any real waiting.
"""

from devops_bench.k8s import conditions


class FakeClock:
    """Monotonic clock stub whose ``sleep`` advances the recorded time."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_poll_until_returns_true_immediately_without_sleeping() -> None:
    clock = FakeClock()

    result = conditions.poll_until(
        lambda: True,
        timeout_sec=100,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert result is True
    assert clock.sleeps == []


def test_poll_until_succeeds_after_a_few_attempts() -> None:
    clock = FakeClock()
    calls = {"n": 0}

    def predicate() -> bool:
        calls["n"] += 1
        return calls["n"] == 3

    result = conditions.poll_until(
        predicate,
        timeout_sec=100,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert result is True
    # Two failures before success -> two backoff sleeps.
    assert clock.sleeps == [1.0, 2.0]


def test_poll_until_backoff_sequence_is_capped() -> None:
    clock = FakeClock()

    # Always false; deadline large enough to exercise the cap, but the fake
    # clock advances via sleep so the loop terminates deterministically.
    result = conditions.poll_until(
        lambda: False,
        timeout_sec=40,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert result is False
    # Doubling is capped at max_delay (10), and the final sleep is clamped to the
    # remaining time so the loop lands exactly on the 40s deadline rather than
    # overshooting it (0,1,3,7,15,25,35,40).
    assert clock.sleeps == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0, 5.0]


def test_poll_until_respects_custom_delays() -> None:
    clock = FakeClock()

    result = conditions.poll_until(
        lambda: False,
        timeout_sec=20,
        initial_delay=2.0,
        max_delay=5.0,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert result is False
    # 0 -> 2 -> 6 -> 11 -> 16 -> 20: the final sleep is clamped from 5 to the
    # remaining 4 seconds so the loop lands exactly on the deadline.
    assert clock.sleeps == [2.0, 4.0, 5.0, 5.0, 4.0]


def test_poll_until_timeout_returns_false() -> None:
    clock = FakeClock()

    result = conditions.poll_until(
        lambda: False,
        timeout_sec=0,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    # Deadline already passed: one predicate check, no sleep.
    assert result is False
    assert clock.sleeps == []


def test_poll_until_catches_success_on_next_check_after_sleep() -> None:
    clock = FakeClock()
    calls = {"n": 0}

    def predicate() -> bool:
        calls["n"] += 1
        # False on the first check, True on the recheck after the backoff sleep.
        return calls["n"] == 2

    result = conditions.poll_until(
        predicate,
        timeout_sec=1,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert result is True
