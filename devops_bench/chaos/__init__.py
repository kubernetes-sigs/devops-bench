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

"""Chaos injection: fault/trigger interfaces, registries, and the typed spec.

Exports only ``Fault``, ``Trigger``, ``ChaosResult``, ``FAULTS``, ``TRIGGERS``,
``ChaosSpec``. :class:`ChaosSpec` parses through the :data:`FAULTS` /
:data:`TRIGGERS` registries; importing the package imports the concrete
fault/trigger leaves so their ``@register`` decorators fire. ``ChaosAgent`` and
the provider SDK chain are not pulled — they load lazily inside
:meth:`Fault.inject`.
"""

from __future__ import annotations

from devops_bench.chaos.base import FAULTS, TRIGGERS, ChaosResult, Fault, Trigger
from devops_bench.chaos.spec import ChaosSpec

__all__ = [
    "ChaosResult",
    "ChaosSpec",
    "FAULTS",
    "Fault",
    "TRIGGERS",
    "Trigger",
]
