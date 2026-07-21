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

"""The ``time`` trigger: blocks for a fixed delay before the fault fires."""

from __future__ import annotations

import time
from typing import Literal

from pydantic import Field

from devops_bench.chaos.base import TRIGGERS, Trigger
from devops_bench.core import get_logger
from devops_bench.core.context import RunContext

__all__ = ["TimeTrigger"]

_log = get_logger("chaos.time_trigger")


@TRIGGERS.register("time")
class TimeTrigger(Trigger):
    """Sleep for ``delay_seconds`` then return.

    Attributes:
        type: Discriminator literal, always ``"time"``.
        delay_seconds: Seconds to block in :meth:`wait`. ``0`` returns
            immediately; negative values are rejected.
    """

    type: Literal["time"] = "time"
    delay_seconds: int = Field(default=0, ge=0)

    def wait(self, ctx: RunContext) -> None:
        """Block for :attr:`delay_seconds` seconds.

        Args:
            ctx: Run context (unused; accepted for interface symmetry with
                triggers that observe cluster state).
        """
        if self.delay_seconds <= 0:
            return
        _log.info("time trigger sleeping for %d seconds", self.delay_seconds)
        time.sleep(self.delay_seconds)
