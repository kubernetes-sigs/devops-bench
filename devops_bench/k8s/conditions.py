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

"""Generic wait/poll engine for Kubernetes resources."""

from __future__ import annotations

import time
from collections.abc import Callable

__all__ = [
    "poll_until",
]


def poll_until(
    predicate: Callable[[], bool],
    *,
    timeout_sec: float,
    initial_delay: float = 1.0,
    max_delay: float = 10.0,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> bool:
    """Poll a predicate with exponential backoff until it holds or time runs out.

    The predicate is checked once per iteration; on failure the loop sleeps for a
    delay that doubles each round (capped at ``max_delay``) before rechecking. The
    final sleep is clamped to the time left, so the call never blocks past
    ``timeout_sec``.

    Args:
        predicate: Zero-argument callable returning True when the wait is done.
        timeout_sec: Maximum wall-clock seconds to keep polling.
        initial_delay: Seconds to sleep after the first failed check.
        max_delay: Upper bound on the backoff delay.
        sleep: Sleep function; injectable so tests can avoid real waiting.
        monotonic: Elapsed-time source; injectable for testing.

    Returns:
        True if the predicate held within the timeout, otherwise False.
    """
    start = monotonic()
    delay = initial_delay
    while True:
        if predicate():
            return True
        elapsed = monotonic() - start
        if elapsed >= timeout_sec:
            return False
        sleep(min(delay, timeout_sec - elapsed))
        delay = min(delay * 2, max_delay)
