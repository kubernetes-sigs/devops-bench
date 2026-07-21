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

"""Tests for the time-delay chaos trigger."""

from __future__ import annotations

from unittest.mock import patch

from devops_bench.chaos.triggers import time_delay
from devops_bench.chaos.triggers.time_delay import TimeTrigger
from devops_bench.core.context import RunContext


def _ctx() -> RunContext:
    return RunContext(task_id="t1")


def test_zero_delay_returns_without_sleeping() -> None:
    trigger = TimeTrigger(delay_seconds=0)
    with patch.object(time_delay.time, "sleep") as sleep_mock:
        trigger.wait(_ctx())
    sleep_mock.assert_not_called()


def test_positive_delay_sleeps_for_that_many_seconds() -> None:
    trigger = TimeTrigger(delay_seconds=4)
    with patch.object(time_delay.time, "sleep") as sleep_mock:
        trigger.wait(_ctx())
    sleep_mock.assert_called_once_with(4)
