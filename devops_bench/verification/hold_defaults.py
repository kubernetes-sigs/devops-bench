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

"""Defaults shared by hold-mode validation and the hold drivers.

Lives in the verification package so the entry schema can validate a hold
window against the effective poll interval without importing the eval
harness, which imports the verification package itself.
"""

from __future__ import annotations

import math
import os

from devops_bench.core import get_logger

_log = get_logger("verification.hold_defaults")


def _default_poll_interval() -> float:
    """Parse ``BENCH_HOLD_INTERVAL_SEC`` as a finite float greater than zero.

    Falls back to ``5.0`` when the variable is unset. Also falls back to
    ``5.0``, logging a warning naming the variable and its offending value,
    when the variable is set but is not a finite positive number, so a bad
    override degrades to the safe default instead of raising a bare
    ``ValueError`` deep inside module import or letting a zero/negative value
    make the scheduler spin without sleeping.
    """
    raw = os.environ.get("BENCH_HOLD_INTERVAL_SEC")
    if raw is None:
        return 5.0
    try:
        value = float(raw)
    except ValueError:
        _log.warning("BENCH_HOLD_INTERVAL_SEC=%r is not a valid number; falling back to 5.0", raw)
        return 5.0
    if not math.isfinite(value) or value <= 0:
        _log.warning(
            "BENCH_HOLD_INTERVAL_SEC=%r must be a finite number greater than zero; "
            "falling back to 5.0",
            raw,
        )
        return 5.0
    return value


# Default seconds between samples for a hold entry that does not set its own
# ``hold_poll_interval_sec``. Overridable via BENCH_HOLD_INTERVAL_SEC.
HOLD_POLL_INTERVAL_SEC: float = _default_poll_interval()


def effective_poll_interval(hold_poll_interval_sec: float | None) -> float:
    """Return the interval a hold entry samples at: its own value, else the default.

    The one place the fallback is decided, used by the entry validator and by
    both hold drivers so they cannot disagree.
    """
    if hold_poll_interval_sec is not None:
        return hold_poll_interval_sec
    return HOLD_POLL_INTERVAL_SEC
