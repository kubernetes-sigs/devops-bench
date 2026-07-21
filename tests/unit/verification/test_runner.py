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

"""Self-contained tests for the deadline-aware verification runner.

The runner's dispatch, fail-fast, deadline-skip, and exception-conversion paths
are exercised with an in-memory fake leaf verifier, so these tests do not depend
on any concrete verifier module that lands in a follow-up.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any, Literal

import pytest

from devops_bench.verification import (
    VERIFIERS,
    BaseVerifier,
    VerificationResult,
)
from devops_bench.verification.runner import VerifierAgent


class _FakeLeaf(BaseVerifier):
    """In-memory leaf verifier whose outcome is fixed by its fields.

    Attributes:
        succeed: Value of the returned result's ``success``.
        boom: When true, :meth:`verify` raises instead of returning a result.
        sleep_for: Seconds to block inside :meth:`verify` before returning.
        tag: Label folded into the result ``reason`` for assertions.
    """

    type: Literal["fake_leaf"] = "fake_leaf"
    succeed: bool = True
    boom: bool = False
    sleep_for: float = 0.0
    tag: str = ""

    def verify(self, timeout_sec: float) -> VerificationResult:
        """Return (or raise) a canned result, echoing the budget it was given."""
        if self.boom:
            raise RuntimeError(f"boom:{self.tag}")
        if self.sleep_for:
            time.sleep(self.sleep_for)
        return VerificationResult(
            success=self.succeed,
            elapsed_time=0.0,
            reason=f"leaf:{self.tag}",
            name=self.name,
            raw={"timeout_sec": timeout_sec},
        )


@pytest.fixture(autouse=True)
def _register_fake_leaf() -> Iterator[None]:
    """Register the fake leaf for one test, then drop it from the registry."""
    VERIFIERS.register("fake_leaf")(_FakeLeaf)
    try:
        yield
    finally:
        VERIFIERS._items.pop("fake_leaf", None)


@pytest.fixture
def agent() -> VerifierAgent:
    """A fresh runner under test."""
    return VerifierAgent()


def _leaf(**kwargs: Any) -> dict[str, Any]:
    """Build a raw fake-leaf spec node."""
    return {"type": "fake_leaf", **kwargs}


# --- leaf dispatch --------------------------------------------------------


def test_leaf_success_passes_through(agent: VerifierAgent) -> None:
    """A passing leaf result is returned unchanged."""
    res = agent.wait_for_condition(_leaf(succeed=True, tag="a"))

    assert res.success is True
    assert res.reason == "leaf:a"


def test_leaf_failure_passes_through(agent: VerifierAgent) -> None:
    """A failing leaf result is returned unchanged."""
    res = agent.wait_for_condition(_leaf(succeed=False, tag="b"))

    assert res.success is False
    assert res.reason == "leaf:b"


def test_leaf_receives_remaining_deadline_budget(agent: VerifierAgent) -> None:
    """A leaf is handed the budget remaining on the shared deadline."""
    # The leaf is handed the budget left on the shared deadline, not the raw
    # ``timeout_sec`` verbatim, but for a bare leaf they are within epsilon.
    res = agent.wait_for_condition(_leaf(), timeout_sec=30)

    assert res.raw is not None
    assert 0 < res.raw["timeout_sec"] <= 30


def test_leaf_short_circuits_below_min_budget(agent: VerifierAgent) -> None:
    """A sub-floor budget fails the leaf without ever calling ``verify()``."""
    # A sub-``_MIN_LEAF_BUDGET_SECONDS`` budget fails the leaf without ever
    # running verify() (raw stays None because verify() was skipped).
    res = agent.wait_for_condition(_leaf(succeed=True), timeout_sec=0.0)

    assert res.success is False
    assert res.reason == "deadline exhausted before evaluation"
    assert res.raw is None


# --- sequence dispatch ----------------------------------------------------


def test_sequence_all_pass(agent: VerifierAgent) -> None:
    """A sequence whose children all pass succeeds and echoes its name."""
    spec = {
        "type": "sequence",
        "name": "seq",
        "checks": [_leaf(succeed=True, tag="1"), _leaf(succeed=True, tag="2")],
    }

    res = agent.wait_for_condition(spec)

    assert res.success is True
    assert res.name == "seq"
    assert [c.success for c in res.children] == [True, True]


def test_sequence_fail_fast_skips_remaining(agent: VerifierAgent) -> None:
    """The first failure halts the sequence and marks the rest skipped."""
    spec = {
        "type": "sequence",
        "checks": [
            _leaf(succeed=False, tag="1"),
            _leaf(succeed=True, tag="2"),
            _leaf(succeed=True, tag="3"),
        ],
    }

    res = agent.wait_for_condition(spec)

    assert res.success is False
    assert len(res.children) == 3
    assert res.children[0].success is False
    # The trailing checks are marked skipped rather than executed.
    assert res.children[1].reason == "earlier step failed"
    assert res.children[2].reason == "earlier step failed"
    assert "[0] failed" in res.reason
    assert "[1] skipped" in res.reason
    assert "[2] skipped" in res.reason


def test_sequence_bulk_skips_all_when_deadline_already_passed(agent: VerifierAgent) -> None:
    """An already-passed deadline bulk-skips every child without running them."""
    # A non-positive budget puts the deadline in the past, so the first loop
    # iteration bulk-marks every child skipped without running any of them.
    spec = {
        "type": "sequence",
        "checks": [_leaf(succeed=True, tag="1"), _leaf(succeed=True, tag="2")],
    }

    res = agent.wait_for_condition(spec, timeout_sec=-1)

    assert res.success is False
    assert len(res.children) == 2
    assert all(not c.success for c in res.children)
    assert all(c.reason == "deadline exhausted" for c in res.children)
    assert "[0] skipped" in res.reason
    assert "[1] skipped" in res.reason


# --- parallel dispatch ----------------------------------------------------


def test_parallel_all_pass(agent: VerifierAgent) -> None:
    """A parallel group whose children all pass succeeds."""
    spec = {
        "type": "parallel",
        "checks": [_leaf(succeed=True, tag="1"), _leaf(succeed=True, tag="2")],
    }

    res = agent.wait_for_condition(spec)

    assert res.success is True
    assert [c.success for c in res.children] == [True, True]


def test_parallel_one_failure_fails_group(agent: VerifierAgent) -> None:
    """A single failing child fails the parallel group as a whole."""
    spec = {
        "type": "parallel",
        "checks": [_leaf(succeed=True, tag="1"), _leaf(succeed=False, tag="2")],
    }

    res = agent.wait_for_condition(spec)

    assert res.success is False
    assert {c.success for c in res.children} == {True, False}


def test_parallel_leaf_exception_becomes_failed_child(agent: VerifierAgent) -> None:
    """A raising leaf is converted to a failed child, not propagated."""
    spec = {
        "type": "parallel",
        "checks": [_leaf(succeed=True, tag="1"), _leaf(boom=True, tag="2")],
    }

    res = agent.wait_for_condition(spec)

    assert res.success is False
    failed = [c for c in res.children if not c.success]
    assert len(failed) == 1
    assert "unhandled error" in failed[0].reason
    assert "boom:2" in failed[0].reason


def test_parallel_empty_checks_is_vacuously_true(agent: VerifierAgent) -> None:
    """An empty parallel group succeeds vacuously."""
    res = agent.wait_for_condition({"type": "parallel", "checks": []})

    assert res.success is True
    assert res.reason == "no checks"
    assert res.children == []


# --- nesting --------------------------------------------------------------


def test_nested_parallel_inside_sequence(agent: VerifierAgent) -> None:
    """Compound nodes nest: a parallel group runs as a sequence child."""
    spec = {
        "type": "sequence",
        "checks": [
            {
                "type": "parallel",
                "checks": [_leaf(succeed=True, tag="a"), _leaf(succeed=True, tag="b")],
            },
            _leaf(succeed=True, tag="c"),
        ],
    }

    res = agent.wait_for_condition(spec)

    assert res.success is True
    assert res.children[0].success is True
    assert len(res.children[0].children) == 2
