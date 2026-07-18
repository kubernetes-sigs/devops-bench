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

"""Result model, abstract bases, and the :data:`FAULTS` / :data:`TRIGGERS` registries."""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod

from pydantic import BaseModel

from devops_bench.core import Registry
from devops_bench.core.context import RunContext

__all__ = [
    "ChaosResult",
    "Fault",
    "Trigger",
    "FAULTS",
    "TRIGGERS",
]

#: Registry of concrete :class:`Fault` subclasses, keyed by their ``type``.
#: ``entry_point_group`` lets external packages register a fault without
#: touching this tree.
FAULTS: Registry[type[Fault]] = Registry("faults", entry_point_group="devops_bench.faults")

#: Registry of concrete :class:`Trigger` subclasses, keyed by their ``type``.
TRIGGERS: Registry[type[Trigger]] = Registry("triggers", entry_point_group="devops_bench.triggers")


class ChaosResult(BaseModel):
    """Structured outcome of a chaos fault injection.

    Attributes:
        success: True when the fault completed without raising.
        injected_fault: Fault id (``Fault.type``) — keys diagnosis scoring in
            metrics.
        output: Free-form text payload (typically the model's final summary).
        elapsed_time: Wall-clock seconds spent injecting the fault.
        error: Human-readable error string when ``success`` is False; ``None``
            on success.
    """

    success: bool
    injected_fault: str
    output: str = ""
    elapsed_time: float = 0.0
    error: str | None = None


class Fault(BaseModel, ABC):
    """Abstract base for a ``type``-tagged chaos fault node.

    Concrete faults are pydantic models that carry their own typed parameters
    plus a ``type: Literal["..."]`` discriminator and self-register under that
    key via ``@FAULTS.register(...)``. They implement :meth:`inject`, which
    drives the disruption against the cluster described by ``ctx`` and returns
    a typed :class:`ChaosResult`.
    """

    @abstractmethod
    def inject(
        self,
        ctx: RunContext,
        chaos_active_event: threading.Event | None = None,
    ) -> ChaosResult:
        """Inject the fault and return its structured outcome.

        Args:
            ctx: Run context describing the target cluster and workspace.
            chaos_active_event: Optional event the fault sets when the
                disruption is observably active (e.g. load is flowing), so the
                harness can coordinate measurements. ``None`` disables the
                signal.

        Returns:
            A :class:`ChaosResult` describing the injection outcome.
        """
        raise NotImplementedError


class Trigger(BaseModel, ABC):
    """Abstract base for a ``type``-tagged chaos firing condition.

    Concrete triggers are pydantic models with a ``type: Literal["..."]``
    discriminator and self-register via ``@TRIGGERS.register(...)``. They
    implement :meth:`wait`, which blocks until the condition the trigger
    encodes is met.
    """

    @abstractmethod
    def wait(self, ctx: RunContext) -> None:
        """Block until the trigger's condition is satisfied.

        Args:
            ctx: Run context describing the target cluster and workspace.
        """
        raise NotImplementedError
