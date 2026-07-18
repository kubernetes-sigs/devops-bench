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

"""Extension-axis acceptance: a dummy fault / trigger lands with no central edit.

CONVENTIONS §9 ("Extension-axis tests"): the harness-resolution path is the
:data:`FAULTS` / :data:`TRIGGERS` registries. Phase 4 (CONVENTIONS §4) makes
this stricter — :class:`ChaosSpec` is now registry-driven, so a dummy
``@FAULTS.register("dummy")`` must additionally **parse through ChaosSpec**
with no edit to ``chaos/spec.py`` (the bar verification met). That is what
these tests pin.
"""

from __future__ import annotations

import threading
from collections.abc import Generator
from typing import Literal

import pytest

from devops_bench.chaos import ChaosSpec
from devops_bench.chaos.base import FAULTS, TRIGGERS, ChaosResult, Fault, Trigger
from devops_bench.core.context import RunContext


@pytest.fixture
def _isolated_registries() -> Generator[None, None, None]:
    """Register dummies for one test and rip them out afterwards.

    The chaos registries are module-scoped and mutable; we snapshot them on
    entry and restore on exit so dummies registered here cannot leak into other
    tests in the same interpreter.

    The bundled ``generate_load`` / ``time`` concretes register **eagerly** on
    ``import devops_bench.chaos`` now (Phase 4 dropped the lazy first-parse
    load), so the snapshot taken here already contains the real baseline — no
    explicit pre-load step is needed before snapshotting.
    """
    saved_faults = dict(FAULTS.items())
    saved_triggers = dict(TRIGGERS.items())
    try:
        yield
    finally:
        for key in list(FAULTS.keys()):
            if key not in saved_faults:
                del FAULTS._items[key]  # type: ignore[attr-defined]
        for key in list(TRIGGERS.keys()):
            if key not in saved_triggers:
                del TRIGGERS._items[key]  # type: ignore[attr-defined]


def test_dummy_fault_resolves_via_registry_with_no_central_edit(_isolated_registries: None) -> None:
    @FAULTS.register("dummy")
    class _DummyFault(Fault):
        type: Literal["dummy"] = "dummy"
        payload: str = ""

        def inject(
            self,
            ctx: RunContext,
            chaos_active_event: threading.Event | None = None,
        ) -> ChaosResult:
            return ChaosResult(success=True, injected_fault=self.type, output=self.payload)

    # Harness-resolution path: registry.get(key) — no edit to chaos/spec.py
    # required for the harness to find a brand-new fault.
    assert "dummy" in FAULTS
    cls = FAULTS.get("dummy")
    assert cls is _DummyFault
    res = _DummyFault(payload="ok").inject(RunContext(task_id="t"))
    assert res.success is True
    assert res.injected_fault == "dummy"


def test_dummy_trigger_resolves_via_registry_with_no_central_edit(
    _isolated_registries: None,
) -> None:
    @TRIGGERS.register("dummy")
    class _DummyTrigger(Trigger):
        type: Literal["dummy"] = "dummy"

        def wait(self, ctx: RunContext) -> None:
            return None

    assert "dummy" in TRIGGERS
    cls = TRIGGERS.get("dummy")
    assert cls is _DummyTrigger
    _DummyTrigger().wait(RunContext(task_id="t"))  # smoke: no crash


def test_dummy_fault_parses_through_chaos_spec_with_no_spec_edit(
    _isolated_registries: None,
) -> None:
    """Phase 4 bar: ``ChaosSpec`` finds a new fault via the registry alone."""

    @FAULTS.register("dummy")
    class _DummyFault(Fault):
        type: Literal["dummy"] = "dummy"
        payload: str = "hi"

        def inject(
            self,
            ctx: RunContext,
            chaos_active_event: threading.Event | None = None,
        ) -> ChaosResult:  # pragma: no cover - parser-only test
            return ChaosResult(success=True, injected_fault=self.type)

    spec = ChaosSpec.model_validate(
        {
            "trigger": {"type": "time", "delay_seconds": 0},
            "action": {"type": "dummy", "payload": "from-parse"},
        }
    )

    assert isinstance(spec.action, _DummyFault)
    assert spec.action.type == "dummy"
    assert spec.action.payload == "from-parse"


def test_dummy_trigger_parses_through_chaos_spec_with_no_spec_edit(
    _isolated_registries: None,
) -> None:
    """Phase 4 bar: ``ChaosSpec`` finds a new trigger via the registry alone."""

    @TRIGGERS.register("dummy")
    class _DummyTrigger(Trigger):
        type: Literal["dummy"] = "dummy"
        label: str = ""

        def wait(self, ctx: RunContext) -> None:  # pragma: no cover - parser-only test
            return None

    spec = ChaosSpec.model_validate(
        {
            "trigger": {"type": "dummy", "label": "from-parse"},
            "action": {
                "type": "generate_load",
                "target": {"service_url": "http://x", "qps": 1},
            },
        }
    )

    assert isinstance(spec.trigger, _DummyTrigger)
    assert spec.trigger.label == "from-parse"


def test_unknown_type_error_message_lists_registered_keys(_isolated_registries: None) -> None:
    """The registry-driven parser surfaces ``registered: [...]`` in its error."""

    # No dummy registered — assert the error names the bundled concretes so
    # authors can see the available options.
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc_info:
        ChaosSpec.model_validate(
            {
                "trigger": {"type": "time"},
                "action": {"type": "definitely_unknown"},
            }
        )
    msg = str(exc_info.value)
    assert "definitely_unknown" in msg
    assert "generate_load" in msg
