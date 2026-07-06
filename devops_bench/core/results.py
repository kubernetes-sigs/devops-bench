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

"""Outcome types shared by evaluated steps."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = ["Status", "Result"]


class Status(StrEnum):
    """Terminal status of an evaluated step."""

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass
class Result:
    """Outcome of a single evaluated step.

    Attributes:
        status: Terminal status; accepts a :class:`Status` or its string value.
        reason: Explanation of the outcome.
        elapsed_sec: Duration of the step in seconds.
        details: JSON-serializable supporting data.
    """

    status: Status
    reason: str = ""
    elapsed_sec: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.status, Status):
            self.status = Status(self.status)

    @property
    def ok(self) -> bool:
        """True only when the step passed."""
        return self.status is Status.PASSED

    @classmethod
    def passed(cls, reason: str = "", **kwargs: Any) -> Result:
        return cls(status=Status.PASSED, reason=reason, **kwargs)

    @classmethod
    def failed(cls, reason: str = "", **kwargs: Any) -> Result:
        return cls(status=Status.FAILED, reason=reason, **kwargs)

    @classmethod
    def errored(cls, reason: str = "", **kwargs: Any) -> Result:
        return cls(status=Status.ERROR, reason=reason, **kwargs)

    @classmethod
    def skipped(cls, reason: str = "", **kwargs: Any) -> Result:
        return cls(status=Status.SKIPPED, reason=reason, **kwargs)

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-serializable mapping.

        Returns:
            A dict with ``status``, ``reason``, ``elapsed_sec``, and ``details``.
        """
        return {
            "status": self.status.value,
            "reason": self.reason,
            "elapsed_sec": self.elapsed_sec,
            "details": self.details,
        }
