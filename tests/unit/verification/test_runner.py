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
from concurrent.futures import wait as real_futures_wait
from typing import Any, Literal
from unittest.mock import patch

import pytest

from devops_bench.verification import (
    VERIFIERS,
    BaseVerifier,
    VerificationResult,
)
from devops_bench.verification.runner import _SINGLE_SHOT_WAIT_CEILING_SEC, VerifierAgent
from devops_bench.verification.spec import parse_entries


class _FakeLeaf(BaseVerifier):
    """In-memory leaf verifier whose outcome is fixed by its fields.

    Attributes:
        succeed: Value of the returned result's ``success``.
        status: Explicit tri-state status; defaults to deriving "pass"/"fail"
            from ``succeed``. Set to "error" (with ``succeed=False``) to model
            a check that could not be evaluated.
        boom: When true, :meth:`verify` raises instead of returning a result.
        sleep_for: Seconds to block inside :meth:`verify` before returning.
        tag: Label folded into the result ``reason`` for assertions.
    """

    type: Literal["fake_leaf"] = "fake_leaf"
    succeed: bool = True
    status: Literal["pass", "fail", "error"] | None = None
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
            status=self.status,
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


def _pass_leaf(tag: str) -> dict[str, Any]:
    return _leaf(succeed=True, tag=tag)


def _fail_leaf(tag: str) -> dict[str, Any]:
    return _leaf(succeed=False, tag=tag)


def _error_leaf(tag: str) -> dict[str, Any]:
    return _leaf(succeed=False, status="error", tag=tag)


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


def test_sequence_child_error_stops_the_walk_with_status_error(agent: VerifierAgent) -> None:
    """A child that errors (not fails) halts the sequence with status "error"."""
    spec = {
        "type": "sequence",
        "checks": [_pass_leaf("1"), _error_leaf("2"), _pass_leaf("3")],
    }

    res = agent.wait_for_condition(spec)

    assert res.status == "error"
    assert res.success is False
    assert len(res.children) == 3
    assert res.children[0].status == "pass"
    assert res.children[1].status == "error"
    assert res.children[2].reason == "earlier step errored"


# --- sequence truth table (Change 1) ---------------------------------------


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        (_pass_leaf, _error_leaf, "error"),
        (_fail_leaf, _error_leaf, "fail"),
        (_error_leaf, _error_leaf, "error"),
    ],
)
def test_sequence_truth_table(agent: VerifierAgent, first: Any, second: Any, expected: str) -> None:
    spec = {"type": "sequence", "checks": [first("1"), second("2")]}
    res = agent.wait_for_condition(spec)
    assert res.status == expected


# --- parallel truth table (Change 1) ----------------------------------------


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ((_pass_leaf, _error_leaf), "error"),
        ((_fail_leaf, _error_leaf), "fail"),
        ((_error_leaf, _error_leaf), "error"),
    ],
)
def test_parallel_truth_table(agent: VerifierAgent, statuses: Any, expected: str) -> None:
    spec = {"type": "parallel", "checks": [f(str(i)) for i, f in enumerate(statuses)]}
    res = agent.wait_for_condition(spec)
    assert res.status == expected


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
    assert res.status == "error"
    failed = [c for c in res.children if not c.success]
    assert len(failed) == 1
    assert failed[0].status == "error"
    assert "unhandled error" in failed[0].reason
    assert "boom:2" in failed[0].reason


def test_parallel_single_shot_unfinished_child_is_error_not_fail(
    agent: VerifierAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child still running when the single_shot wait ceiling hits is "error".

    It was never observed to violate anything, only never finished in time;
    reading it as a definite "fail" would turn an unobserved outcome into a
    safeguard VIOLATION.
    """
    monkeypatch.setattr("devops_bench.verification.runner._SINGLE_SHOT_WAIT_CEILING_SEC", 0.05)
    entries, errors = parse_entries(
        [
            {
                "name": "e",
                "role": "safeguard",
                "severity": "catastrophic",
                "check": {
                    "type": "parallel",
                    "checks": [_leaf(succeed=True, tag="hung", sleep_for=0.5)],
                },
            }
        ]
    )
    assert errors == []

    res = agent.run_entry(entries[0], timeout_sec=30)

    assert res.status == "error"
    assert res.success is False
    assert len(res.children) == 1
    assert res.children[0].status == "error"
    assert res.children[0].reason == "evaluation did not complete before the deadline"


def test_parallel_converge_hung_child_alongside_an_observed_fail_stays_fail(
    agent: VerifierAgent,
) -> None:
    """An observed fail still wins the group verdict over an unfinished sibling."""
    # The deadline must clear _MIN_LEAF_BUDGET_SECONDS so the hung leaf's
    # verify() is actually invoked (and outlasts the wait) rather than being
    # short-circuited by the per-leaf min-budget guard before it ever runs.
    spec = {
        "type": "parallel",
        "checks": [
            _leaf(succeed=False, tag="observed"),
            _leaf(succeed=True, tag="hung", sleep_for=3.0),
        ],
    }

    res = agent.wait_for_condition(spec, timeout_sec=1.2)

    assert res.status == "fail"
    assert res.success is False
    assert {c.status for c in res.children} == {"fail", "error"}
    hung = next(c for c in res.children if c.status == "error")
    assert hung.reason == "evaluation did not complete before the deadline"


def test_parallel_single_shot_bounds_the_wait_at_the_ceiling(agent: VerifierAgent) -> None:
    """A single_shot parallel group's futures_wait is bounded, not unbounded.

    ``None`` would let one hung child stall the run forever; assert-mode
    (single_shot) passes :data:`_SINGLE_SHOT_WAIT_CEILING_SEC` instead.
    """
    entries, errors = parse_entries(
        [
            {
                "name": "e",
                "role": "safeguard",
                "severity": "catastrophic",
                "check": {
                    "type": "parallel",
                    "checks": [_leaf(succeed=True, tag="1")],
                },
            }
        ]
    )
    assert errors == []

    recorded: dict[str, float | None] = {}

    def fake_wait(futs: Any, timeout: float | None = None) -> Any:
        recorded["timeout"] = timeout
        return real_futures_wait(futs, timeout=timeout)

    with patch("devops_bench.verification.runner.futures_wait", side_effect=fake_wait):
        agent.run_entry(entries[0], timeout_sec=30)

    assert recorded["timeout"] == _SINGLE_SHOT_WAIT_CEILING_SEC


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
