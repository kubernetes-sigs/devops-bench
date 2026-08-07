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

"""Unit tests for the any, all, and none combinators."""

import time
from typing import Any, Literal

import pytest
from pydantic import Field, ValidationError

from devops_bench.verification.base import VERIFIERS, BaseVerifier, VerificationResult
from devops_bench.verification.runner import VerifierAgent
from devops_bench.verification.spec import (
    AllSpec,
    AnySpec,
    NoneSpec,
    ParallelSpec,
    SequenceSpec,
    VerificationEntry,
    parse_entries,
    parse_node,
)


@VERIFIERS.register("always")
class _Always(BaseVerifier):
    """Test double that returns a fixed verdict and counts its calls."""

    type: Literal["always"]
    ok: bool = True
    status: Literal["pass", "fail", "error"] | None = None
    calls: list[float] = Field(default_factory=list)

    def verify(self, timeout_sec: float) -> VerificationResult:
        self.calls.append(timeout_sec)
        return VerificationResult(
            success=self.ok, status=self.status, elapsed_time=0.0, reason="stub", name=self.name
        )


def _leaf(ok: bool, name: str = "leaf") -> dict[str, Any]:
    return {"type": "always", "ok": ok, "name": name}


def _error_leaf(name: str = "leaf") -> dict[str, Any]:
    return {"type": "always", "ok": False, "status": "error", "name": name}


@VERIFIERS.register("countdown")
class _Countdown(BaseVerifier):
    """Test double that passes for its first ``succeed_for`` calls, then fails.

    Models a child genuinely converging toward "false" across poll rounds,
    which is what a round-based ``none`` must be able to wait out.
    """

    type: Literal["countdown"] = "countdown"
    succeed_for: int = 0
    calls: int = 0

    def verify(self, timeout_sec: float) -> VerificationResult:
        self.calls += 1
        ok = self.calls <= self.succeed_for
        return VerificationResult(success=ok, elapsed_time=0.0, reason="stub", name=self.name)


@VERIFIERS.register("slow")
class _SlowLeaf(BaseVerifier):
    """Test double that sleeps past its caller's timeout before returning.

    Models a leaf genuinely blocked in I/O; used to prove a nested parallel
    child's single_shot wait stays bounded by the outer converge deadline
    rather than always waiting up to the full wait ceiling.
    """

    type: Literal["slow"] = "slow"
    sleep_for: float = 3.0

    def verify(self, timeout_sec: float) -> VerificationResult:
        time.sleep(self.sleep_for)
        return VerificationResult(
            success=False, elapsed_time=self.sleep_for, reason="stub", name=self.name
        )


def _assert_entry(check: dict[str, Any]) -> VerificationEntry:
    """Build a single safeguard (assert-mode) entry wrapping ``check``."""
    entries, errors = parse_entries(
        [{"name": "e", "role": "safeguard", "severity": "catastrophic", "check": check}]
    )
    assert errors == []
    return entries[0]


def test_all_is_registered_and_parses() -> None:
    node = parse_node({"type": "all", "checks": [_leaf(True)]})
    assert isinstance(node, AllSpec)


def test_all_passes_only_when_every_child_passes() -> None:
    agent = VerifierAgent()
    ok = agent.wait_for_condition(
        {"type": "all", "checks": [_leaf(True), _leaf(True)]}, timeout_sec=5
    )
    bad = agent.wait_for_condition(
        {"type": "all", "checks": [_leaf(True), _leaf(False)]}, timeout_sec=5
    )
    assert ok.success is True
    assert bad.success is False


def test_any_passes_when_one_child_passes() -> None:
    agent = VerifierAgent()
    result = agent.wait_for_condition(
        {"type": "any", "checks": [_leaf(False, "a"), _leaf(True, "b")]},
        timeout_sec=5,
    )
    assert result.success is True
    assert len(result.children) == 2


def test_any_fails_when_every_child_fails() -> None:
    agent = VerifierAgent()
    # Every round fails, so this genuinely polls to the deadline; keep the
    # deadline small so the test doesn't burn several real seconds.
    result = agent.wait_for_condition(
        {"type": "any", "checks": [_leaf(False), _leaf(False)]}, timeout_sec=0.3
    )
    assert result.success is False


def test_any_short_circuits_after_the_first_success() -> None:
    agent = VerifierAgent()
    result = agent.wait_for_condition(
        {"type": "any", "checks": [_leaf(True, "a"), _leaf(False, "b")]},
        timeout_sec=5,
    )
    assert result.success is True
    assert len(result.children) == 1


def test_none_passes_when_every_child_fails() -> None:
    agent = VerifierAgent()
    result = agent.wait_for_condition(
        {"type": "none", "checks": [_leaf(False), _leaf(False)]}, timeout_sec=5
    )
    assert result.success is True


def test_none_fails_at_deadline_when_a_child_keeps_passing() -> None:
    agent = VerifierAgent()
    # Under round-based polling a passing child no longer fails the group
    # outright (the state may still be converging toward false), so this
    # genuinely polls to the deadline; keep it small. The last round still
    # short-circuits at the first passing child, so only "a" is evaluated.
    result = agent.wait_for_condition(
        {"type": "none", "checks": [_leaf(True, "a"), _leaf(False, "b")]},
        timeout_sec=0.3,
    )
    assert result.success is False
    assert len(result.children) == 1


def test_combinators_nest() -> None:
    agent = VerifierAgent()
    result = agent.wait_for_condition(
        {
            "type": "all",
            "checks": [
                {"type": "any", "checks": [_leaf(False), _leaf(True)]},
                {"type": "none", "checks": [_leaf(False)]},
            ],
        },
        timeout_sec=5,
    )
    assert result.success is True


def test_child_name_is_carried_onto_the_result() -> None:
    node = parse_node({"type": "none", "name": "no-public-lb", "checks": [_leaf(False)]})
    assert isinstance(node, NoneSpec)
    assert node.name == "no-public-lb"


def test_any_spec_type_discriminator() -> None:
    node = parse_node({"type": "any", "checks": [_leaf(True)]})
    assert isinstance(node, AnySpec)
    assert node.type == "any"


# Additions on top of the brief's Step 1 file: spec-level parsing behavior
# (registration, defaults, recursion into children, and validation) that the
# runtime-focused tests above do not exercise directly.


def test_all_is_a_parallel_spec() -> None:
    node = parse_node({"type": "all", "checks": [_leaf(True)]})
    assert isinstance(node, ParallelSpec)


def test_any_spec_defaults() -> None:
    node = parse_node({"type": "any", "checks": [_leaf(True)]})
    assert node.name is None
    assert len(node.checks) == 1


def test_any_parses_and_recurses_children() -> None:
    node = parse_node(
        {
            "type": "any",
            "name": "at-least-one",
            "checks": [_leaf(True, "a"), _leaf(False, "b")],
        }
    )
    assert isinstance(node, AnySpec)
    assert node.name == "at-least-one"
    assert len(node.checks) == 2
    assert all(isinstance(c, _Always) for c in node.checks)


def test_none_spec_type_discriminator_and_defaults() -> None:
    node = parse_node({"type": "none", "checks": [_leaf(False)]})
    assert isinstance(node, NoneSpec)
    assert node.type == "none"
    assert node.name is None
    assert len(node.checks) == 1


def test_none_parses_and_recurses_children() -> None:
    node = parse_node({"type": "none", "name": "no-public-lb", "checks": [_leaf(False)]})
    assert isinstance(node, NoneSpec)
    assert node.name == "no-public-lb"
    assert len(node.checks) == 1
    assert isinstance(node.checks[0], _Always)


def test_combinators_compose_when_nested() -> None:
    node = parse_node(
        {
            "type": "all",
            "checks": [
                {"type": "any", "checks": [_leaf(False), _leaf(True)]},
                {"type": "none", "checks": [_leaf(False)]},
            ],
        }
    )
    assert isinstance(node, AllSpec)
    any_child, none_child = node.checks
    assert isinstance(any_child, AnySpec)
    assert isinstance(none_child, NoneSpec)


def test_any_and_none_are_not_sequence_or_parallel_specs() -> None:
    any_node = parse_node({"type": "any", "checks": [_leaf(True)]})
    none_node = parse_node({"type": "none", "checks": [_leaf(False)]})
    assert not isinstance(any_node, ParallelSpec)
    assert not isinstance(any_node, SequenceSpec)
    assert not isinstance(none_node, ParallelSpec)
    assert not isinstance(none_node, SequenceSpec)


def test_any_and_none_reject_missing_checks() -> None:
    with pytest.raises(ValidationError):
        AnySpec.model_validate({"type": "any"})
    with pytest.raises(ValidationError):
        NoneSpec.model_validate({"type": "none"})


# --- empty combinator groups are a validation error (Change 2) -------------


@pytest.mark.parametrize(
    ("model", "type_"),
    [
        (SequenceSpec, "sequence"),
        (ParallelSpec, "parallel"),
        (AllSpec, "all"),
        (AnySpec, "any"),
        (NoneSpec, "none"),
    ],
)
def test_empty_checks_list_is_a_validation_error(model: type[Any], type_: str) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({"type": type_, "checks": []})


# --- unknown keys are a validation error (Change 1) -------------------------


def test_leaf_check_rejects_an_unknown_key() -> None:
    with pytest.raises(ValidationError):
        parse_node({"type": "always", "ok": True, "bogus_key": "x"})


def test_compound_node_rejects_an_unknown_key() -> None:
    with pytest.raises(ValidationError):
        parse_node({"type": "sequence", "checks": [_leaf(True)], "bogus_key": "x"})


# Round-based polling under converge (PR review fix): `any` and `none` no
# longer hand the shared deadline to one child, which used to starve or
# poison its siblings. See runner.py's module docstring for the rationale.


def test_converge_none_with_always_failing_children_passes_without_burning_the_deadline() -> None:
    agent = VerifierAgent()
    start = time.monotonic()
    result = agent.wait_for_condition(
        {"type": "none", "checks": [_leaf(False, "a"), _leaf(False, "b")]},
        timeout_sec=30,
    )
    elapsed = time.monotonic() - start

    assert result.success is True
    # The first round already satisfies "nothing holds"; a much smaller bound
    # than the 30s deadline proves this didn't wait the whole thing out.
    assert elapsed < 5.0


def test_converge_none_succeeds_once_a_child_stops_passing() -> None:
    agent = VerifierAgent()
    node = parse_node(
        {"type": "none", "checks": [{"type": "countdown", "succeed_for": 2, "name": "c"}]}
    )
    result = agent.wait_for_condition(node, timeout_sec=15)

    assert result.success is True
    assert node.checks[0].calls >= 3


def test_converge_any_reaches_a_later_passing_child() -> None:
    """Regression: child 1 always fails, child 2 passes; `any` must reach it."""
    agent = VerifierAgent()
    result = agent.wait_for_condition(
        {"type": "any", "checks": [_leaf(False, "a"), _leaf(True, "b")]},
        timeout_sec=15,
    )
    assert result.success is True


def test_converge_any_fails_at_deadline_expiry_when_every_round_fails() -> None:
    agent = VerifierAgent()
    result = agent.wait_for_condition(
        {"type": "any", "checks": [_leaf(False, "a"), _leaf(False, "b")]},
        timeout_sec=0.3,
    )
    assert result.success is False


def test_converge_none_fails_at_deadline_expiry_when_every_round_sees_a_pass() -> None:
    agent = VerifierAgent()
    result = agent.wait_for_condition(
        {"type": "none", "checks": [_leaf(True, "a")]},
        timeout_sec=0.3,
    )
    assert result.success is False


def test_converge_any_with_a_blocking_nested_parallel_child_stays_bounded_by_the_deadline() -> None:
    """Regression: a round's deadline must reach nested single_shot children.

    Before the fix, round evaluation handed nested children a literal 0.0
    deadline instead of the real converge deadline, and a single_shot
    parallel node ignored the deadline outright, always waiting up to
    :data:`_SINGLE_SHOT_WAIT_CEILING_SEC`. A round containing a nested
    parallel child could then overshoot the converge deadline by up to that
    ceiling. Here the leaf blocks well past the small converge timeout; the
    whole evaluation must still finish in a small fraction of the ceiling.
    """
    agent = VerifierAgent()
    start = time.monotonic()
    result = agent.wait_for_condition(
        {
            "type": "any",
            "checks": [
                {"type": "parallel", "checks": [{"type": "slow", "sleep_for": 5.0, "name": "s"}]}
            ],
        },
        timeout_sec=1,
    )
    elapsed = time.monotonic() - start

    assert result.success is False
    # Bounded by the (small) converge deadline, not by how long the leaf
    # actually blocks (5s) or the full wait ceiling (120s).
    assert elapsed < 3.0


# --- converge rounds are bounded mid-round, not just between rounds (Change 3) --


def test_converge_any_round_bounds_mid_round_by_deadline() -> None:
    """A converge round stops evaluating children once the deadline passes mid-round.

    Without a per-child deadline check inside the round loop, one round could
    overshoot the shared deadline by up to len(checks) x the per-leaf I/O
    floor; the fix bounds the overshoot to at most one more leaf call. The
    first child is still evaluated unconditionally (the always-at-least-one
    contract); children after the deadline expires are recorded "error"
    (never observed), not "fail".
    """
    agent = VerifierAgent()
    result = agent.wait_for_condition(
        {
            "type": "any",
            "checks": [
                {"type": "slow", "sleep_for": 1.0, "name": "a"},
                {"type": "slow", "sleep_for": 1.0, "name": "b"},
                {"type": "slow", "sleep_for": 1.0, "name": "c"},
            ],
        },
        timeout_sec=0.3,
    )

    assert result.status == "error"
    assert len(result.children) == 3
    assert result.children[0].reason == "stub"  # evaluated
    assert result.children[1].status == "error"
    assert result.children[1].reason == "deadline exhausted before evaluation"
    assert result.children[2].status == "error"
    assert result.children[2].reason == "deadline exhausted before evaluation"


def test_converge_none_round_bounds_mid_round_by_deadline() -> None:
    """Mirrors the ``any`` case: ``none``'s round loop is bounded mid-round too."""
    agent = VerifierAgent()
    result = agent.wait_for_condition(
        {
            "type": "none",
            "checks": [
                {"type": "slow", "sleep_for": 1.0, "name": "a"},
                {"type": "slow", "sleep_for": 1.0, "name": "b"},
            ],
        },
        timeout_sec=0.3,
    )

    assert result.status == "error"
    assert len(result.children) == 2
    assert result.children[0].reason == "stub"  # evaluated
    assert result.children[1].status == "error"
    assert result.children[1].reason == "deadline exhausted before evaluation"


def test_assert_mode_any_evaluates_exactly_one_round() -> None:
    entry = _assert_entry({"type": "any", "checks": [_leaf(False, "a"), _leaf(False, "b")]})
    result = VerifierAgent().run_entry(entry, timeout_sec=30)

    assert result.success is False
    for child in entry.check.checks:
        assert len(child.calls) == 1


def test_assert_mode_none_evaluates_exactly_one_round_even_when_a_child_passes() -> None:
    entry = _assert_entry({"type": "none", "checks": [_leaf(True, "a"), _leaf(False, "b")]})
    result = VerifierAgent().run_entry(entry, timeout_sec=30)

    assert result.success is False
    # The round short-circuits at the first passing child, so "b" never runs.
    assert len(entry.check.checks[0].calls) == 1
    assert len(entry.check.checks[1].calls) == 0


# --- any / none truth tables (Change 1) -------------------------------------
#
# One bounded (single_shot) round each, so these are exact truth-table checks
# rather than converge-polling behavior.


@pytest.mark.parametrize(
    ("checks", "expected"),
    [
        ([_leaf(True, "a"), _error_leaf("b")], "pass"),
        ([_leaf(False, "a"), _error_leaf("b")], "error"),
        ([_error_leaf("a"), _error_leaf("b")], "error"),
    ],
)
def test_any_truth_table(checks: list[dict[str, Any]], expected: str) -> None:
    entry = _assert_entry({"type": "any", "checks": checks})
    result = VerifierAgent().run_entry(entry, timeout_sec=30)
    assert result.status == expected


@pytest.mark.parametrize(
    ("checks", "expected"),
    [
        ([_leaf(True, "a"), _error_leaf("b")], "fail"),
        ([_leaf(False, "a"), _error_leaf("b")], "error"),
        ([_error_leaf("a"), _error_leaf("b")], "error"),
    ],
)
def test_none_truth_table(checks: list[dict[str, Any]], expected: str) -> None:
    entry = _assert_entry({"type": "none", "checks": checks})
    result = VerifierAgent().run_entry(entry, timeout_sec=30)
    assert result.status == expected
