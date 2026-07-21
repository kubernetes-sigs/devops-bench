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

"""Concrete chaos triggers, each self-registering in ``chaos.base.TRIGGERS``.

Importing this package pulls each bundled trigger module so its
``@TRIGGERS.register`` decorator fires. The imports stay light: a trigger
module loads only the trigger *class* (and ``core`` / ``pydantic``).
"""

from __future__ import annotations

# Imported for the ``@TRIGGERS.register`` side effect that populates the registry.
from devops_bench.chaos.triggers import time_delay  # noqa: F401

__all__: list[str] = []
