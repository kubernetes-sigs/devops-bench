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

"""Extension-axis acceptance: a dummy verifier parses with no central edit."""

from __future__ import annotations

from typing import Literal

import pytest
from pydantic import BaseModel, ValidationError

from devops_bench.verification import (
    VERIFIERS,
    BaseVerifier,
    SequenceSpec,
    VerificationResult,
    VerificationSpec,
)


def test_dummy_verifier_parses_without_union_edit():
    # A new leaf is one decorator away — no edit to a central pydantic
    # ``Annotated[Union, Field(discriminator="type")]`` is required.
    @VERIFIERS.register("dummy_check")
    class _DummyCheck(BaseVerifier):
        type: Literal["dummy_check"] = "dummy_check"
        payload: str

        def verify(self, timeout_sec: float) -> VerificationResult:
            return VerificationResult(
                success=True,
                elapsed_time=0.0,
                reason=f"dummy:{self.payload}",
                name=self.name,
            )

    try:
        spec = VerificationSpec({"type": "dummy_check", "payload": "hello"})
        assert isinstance(spec.root, _DummyCheck)
        assert spec.root.payload == "hello"

        # It composes inside compound nodes too, again with no union edit.
        seq = VerificationSpec(
            {
                "type": "sequence",
                "checks": [
                    {"type": "dummy_check", "payload": "first"},
                    {"type": "dummy_check", "payload": "second"},
                ],
            }
        )
        assert isinstance(seq.root, SequenceSpec)
        assert [c.payload for c in seq.root.checks] == ["first", "second"]
    finally:
        # Keep the global registry clean for sibling tests.
        VERIFIERS._items.pop("dummy_check", None)


def test_unknown_type_lists_registered_keys_in_error():
    # When the registry lookup misses, the parser must surface as a
    # ``ValidationError`` (the codebase catches that), and the message should
    # name the registered keys so authors can self-diagnose typos.
    with pytest.raises(ValidationError) as excinfo:
        VerificationSpec({"type": "definitely_not_registered"})
    msg = str(excinfo.value)
    assert "definitely_not_registered" in msg
    # At least one of the builtins must show up in the listed keys.
    assert "pod_healthy" in msg or "sequence" in msg


class _UnregisteredVerifier(BaseModel):
    """A pydantic model that walks and talks like a verifier but is NOT registered."""

    type: Literal["totally_off_the_books"] = "totally_off_the_books"
    payload: str = "x"


def test_already_parsed_unregistered_basemodel_is_rejected():
    # ``parse_node`` used to bypass any already-parsed ``BaseModel`` outright,
    # which let an unregistered model slip through. Guard now requires the
    # concrete class to be in ``VERIFIERS``; an unregistered instance must
    # raise the same ``ValidationError`` surface as an unknown ``type`` string.
    instance = _UnregisteredVerifier(payload="x")
    with pytest.raises(ValidationError) as excinfo:
        VerificationSpec(instance)
    assert "not a registered verifier" in str(excinfo.value)


def test_unregistered_basemodel_as_checks_child_is_rejected():
    # The same guard must fire for compound children: an unregistered
    # pre-parsed model handed in as a ``checks`` entry must not silently land
    # in a sequence/parallel node.
    rogue = _UnregisteredVerifier(payload="x")
    with pytest.raises(ValidationError) as excinfo:
        VerificationSpec(
            {
                "type": "sequence",
                "checks": [rogue],
            }
        )
    assert "not a registered verifier" in str(excinfo.value)
