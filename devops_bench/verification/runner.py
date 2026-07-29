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

"""Deadline-based dispatcher that evaluates a verification specification.

The whole verification races a single monotonic deadline computed once at the
top of :meth:`VerifierAgent.wait_for_condition`. Sequence nodes consume the
deadline serially and fail fast (later children are recorded as skipped);
parallel nodes hand each child the full remaining deadline and AND the results.
Leaves consume the deadline directly via ``leaf.verify(remaining)``.

``any`` and ``none`` are the exception: handing either combinator's shared
deadline straight to one child starves or poisons its siblings (a child that
correctly stays false can burn the whole deadline just sitting inside its own
``verify(remaining)``, leaving nothing for the next child to run against).
Under converge they instead poll in ROUNDS, each round giving every needed
child a single bounded pass, so one child's patience never comes out of
another's budget. A round also checks the deadline between children (not just
between rounds), so one round overshoots the deadline by at most one leaf
call rather than by up to ``len(checks)`` of them. See
:meth:`VerifierAgent._run_any` and :meth:`VerifierAgent._run_none`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as futures_wait
from typing import Any

from devops_bench.k8s import poll_until
from devops_bench.verification.base import (
    _MIN_LEAF_BUDGET_SECONDS,
    BaseVerifier,
    VerificationResult,
    VerificationStatus,
)
from devops_bench.verification.spec import (
    AnySpec,
    NoneSpec,
    ParallelSpec,
    SequenceSpec,
    VerificationEntry,
    VerificationSpec,
)

__all__ = ["VerifierAgent"]

_MAX_PARALLEL_WORKERS = 8

# _MIN_LEAF_BUDGET_SECONDS (imported above): a leaf invoked with less than
# this many seconds left on the deadline is short-circuited as timed out,
# avoiding useless ``kubectl wait --timeout=0.001s`` calls at the tail of the
# budget. Defined in base.py, not here, since base.py's single_call_timeout
# needs the same threshold and sits below this module in the import graph.

# Backstop on how long a single_shot parallel group waits for its children.
# ``None`` would let one hung child stall the run forever; this is generous
# enough to cover a compound child made of several single-shot leaves, each
# already I/O-floored at 30s (see the verifier modules' own floors).
_SINGLE_SHOT_WAIT_CEILING_SEC = 120.0

# A child still running when the deadline (or the single_shot wait ceiling)
# hits was never observed one way or the other, so it is recorded as "error"
# rather than "fail". Shared wording for both the converge deadline-expiry
# path and the single-shot wait-ceiling path in :meth:`VerifierAgent._run_parallel`.
_PARALLEL_INCOMPLETE_REASON = "evaluation did not complete before the deadline"


def _node_name(node: Any) -> str | None:
    """Echo the optional ``name`` label from a spec node, if any."""
    return getattr(node, "name", None)


def _failed(node: Any, reason: str, status: VerificationStatus = "fail") -> VerificationResult:
    """Build a not-run-to-completion result for a node.

    Covers deadline exhaustion and sequence fail-fast skips alike; ``reason``
    carries the specific cause. ``status`` defaults to "fail" (a deadline
    that expires without the condition converging is a definite fail); a
    child skipped because an earlier sibling errored instead of failed
    passes ``status="error"``, since it was never observed either way.
    """
    return VerificationResult(
        success=status == "pass",
        status=status,
        elapsed_time=0.0,
        reason=reason,
        name=_node_name(node),
    )


def _combine_conjunction(statuses: list[VerificationStatus]) -> VerificationStatus:
    """Three-valued AND: sequence / parallel / all.

    Any child "fail" wins outright (a definite verdict always beats
    unknown); otherwise any child "error" makes the group's status unknown;
    only when every child passed does the group pass.
    """
    if "fail" in statuses:
        return "fail"
    if "error" in statuses:
        return "error"
    return "pass"


def _combine_disjunction(statuses: list[VerificationStatus]) -> VerificationStatus:
    """Three-valued OR: any.

    Any child "pass" wins outright; otherwise any child "error" leaves the
    group's status unknown (a passing child might still exist among the
    unevaluated/errored ones); only when every child definitely failed does
    the group fail.
    """
    if "pass" in statuses:
        return "pass"
    if "error" in statuses:
        return "error"
    return "fail"


def _combine_negated_disjunction(statuses: list[VerificationStatus]) -> VerificationStatus:
    """Three-valued NOR: none.

    Any child "pass" is a definite violation (something held that must
    not); otherwise any child "error" leaves the group's status unknown;
    only when every child definitely failed to hold does the group pass.
    """
    if "pass" in statuses:
        return "fail"
    if "error" in statuses:
        return "error"
    return "pass"


class VerifierAgent:
    """Evaluate single or compound verification specs against cluster state.

    All evaluations share a single monotonic deadline established by
    :meth:`wait_for_condition`. Sequence and parallel nodes propagate the
    deadline without rebudgeting; leaves consume it directly via their
    ``verify`` method. ``any`` and ``none`` are the exception: under converge
    they repoll in bounded rounds against the shared deadline instead of
    handing it to one child, since a child that legitimately holds (or stays
    false) for the whole deadline would otherwise starve its siblings of any
    chance to run.
    """

    def wait_for_condition(
        self,
        spec: VerificationSpec | Any,
        timeout_sec: float = 120,
    ) -> VerificationResult:
        """Wait for a spec to hold within ``timeout_sec``.

        Args:
            spec: A :class:`VerificationSpec`, an already-parsed node, or a raw
                mapping the spec validator can parse.
            timeout_sec: Total wall-clock budget shared across the (possibly
                nested) checks. A single monotonic deadline is computed from
                this once at the top. Note: a value below
                :data:`_MIN_LEAF_BUDGET_SECONDS` (1.0s) short-circuits a bare
                leaf as failed without ever calling ``verify()``, so callers
                should not mistake such a result for a check failure.

        Returns:
            The aggregated verification result.
        """
        if isinstance(spec, VerificationSpec):
            node: Any = spec.root
        elif isinstance(spec, SequenceSpec | ParallelSpec | BaseVerifier):
            node = spec  # already-parsed node (compound or leaf)
        else:
            node = VerificationSpec(spec).root  # raw mapping -> parse

        deadline = time.monotonic() + timeout_sec
        return self._run(node, deadline)

    def run_entry(self, entry: VerificationEntry, timeout_sec: float = 120) -> VerificationResult:
        """Evaluate one entry's check subtree under the entry's resolved mode.

        ``converge`` polls the whole subtree against a shared deadline, which is
        what an objective wants: the agent is working toward the state and the
        check should wait for it. ``assert`` evaluates once with a zero budget,
        which is what a safeguard wants: a violation that has already happened
        will not heal, and polling one would only waste the run's time.

        Args:
            entry: The parsed entry to evaluate.
            timeout_sec: Total budget for a converging entry. Ignored under
                ``assert``.

        Returns:
            The subtree's result, including per-child results.
        """
        single_shot = entry.resolved_mode == "assert"
        deadline = time.monotonic() + (0.0 if single_shot else timeout_sec)
        return self._run(entry.check, deadline, single_shot=single_shot)

    def _run(self, node: Any, deadline: float, *, single_shot: bool = False) -> VerificationResult:
        """Dispatch a node against the shared deadline."""
        if isinstance(node, SequenceSpec):
            return self._run_sequence(node, deadline, single_shot=single_shot)
        if isinstance(node, ParallelSpec):
            return self._run_parallel(node, deadline, single_shot=single_shot)
        if isinstance(node, AnySpec):
            return self._run_any(node, deadline, single_shot=single_shot)
        if isinstance(node, NoneSpec):
            return self._run_none(node, deadline, single_shot=single_shot)
        return self._run_leaf(node, deadline, single_shot=single_shot)

    def _run_leaf(
        self, node: Any, deadline: float, *, single_shot: bool = False
    ) -> VerificationResult:
        """Run a leaf verifier with whatever budget remains on the deadline.

        Short-circuits when the remaining budget is below
        :data:`_MIN_LEAF_BUDGET_SECONDS` so we never issue a useless
        sub-second ``kubectl wait`` at the tail of the deadline. ``single_shot``
        bypasses that guard and evaluates once with a zero budget instead.
        """
        if single_shot:
            # Deliberately zero, not some small poll budget: single_shot means
            # a safeguard that must never hold, and polling one (even briefly)
            # just gives an already-happened violation time to heal before we
            # notice it. Each leaf verifier floors its own I/O timeout instead
            # (see devops_bench.verification.base.single_call_timeout), so
            # a zero-second budget here still bounds the underlying kubectl
            # call rather than issuing a doomed zero-timeout request.
            return node.verify(0.0)
        remaining = deadline - time.monotonic()
        if remaining < _MIN_LEAF_BUDGET_SECONDS:
            return _failed(node, "deadline exhausted before evaluation")
        return node.verify(remaining)

    @staticmethod
    def _skip_rest(
        checks: list[Any],
        start_index: int,
        reason: str,
        children: list[VerificationResult],
        reasons: list[str],
        status: VerificationStatus = "fail",
    ) -> None:
        """Mark every child from ``start_index`` onward as skipped, in one pass."""
        for j, rest in enumerate(checks[start_index:], start=start_index):
            children.append(_failed(rest, reason, status=status))
            reasons.append(f"[{j}] skipped")

    def _run_sequence(
        self, node: SequenceSpec, deadline: float, *, single_shot: bool = False
    ) -> VerificationResult:
        """Run children in order; stop and skip the rest on the first failure or error.

        Both a hit deadline and a failed step halt the walk immediately and
        bulk-mark every remaining child skipped, so later children never incur a
        per-item loop iteration (nor a redundant deadline check) once the
        sequence can no longer make progress. A child that errors (could not be
        evaluated, as distinct from a condition observed false) also halts the
        walk: the sequence cannot proceed meaningfully past a step it never
        observed, so the whole sequence's status is "error" rather than "fail",
        even if a later step would have failed. This is a deliberate departure
        from the conjunction truth table (fail beats error): the sequence
        stops before it can find out.
        """
        start = time.monotonic()
        children: list[VerificationResult] = []
        reasons: list[str] = []
        status: VerificationStatus = "pass"
        for i, child in enumerate(node.checks):
            if not single_shot and time.monotonic() >= deadline:
                status = "fail"
                self._skip_rest(node.checks, i, "deadline exhausted", children, reasons)
                break
            res = self._run(child, deadline, single_shot=single_shot)
            children.append(res)
            if res.status == "error":
                status = "error"
                reasons.append(f"[{i}] errored: {res.reason}")
                self._skip_rest(
                    node.checks, i + 1, "earlier step errored", children, reasons, status="error"
                )
                break  # fail-fast
            if not res.success:
                status = "fail"
                reasons.append(f"[{i}] failed: {res.reason}")
                self._skip_rest(node.checks, i + 1, "earlier step failed", children, reasons)
                break  # fail-fast
            reasons.append(f"[{i}] succeeded")
        return VerificationResult(
            success=status == "pass",
            status=status,
            elapsed_time=time.monotonic() - start,
            reason="; ".join(reasons),
            name=node.name,
            children=children,
        )

    def _run_any(
        self, node: AnySpec, deadline: float, *, single_shot: bool = False
    ) -> VerificationResult:
        """Evaluate members, succeeding once some child passes.

        Under ``single_shot`` (assert, or one round of a converge poll) this is
        exactly one bounded pass: evaluate members in order, stop at the first
        success. Under converge, handing the whole shared deadline to child 1
        (the pre-fix behavior) means a later, passing child is never reached;
        instead this repolls in ROUNDS via :meth:`_poll_rounds`, each round a
        fresh bounded pass over every needed child, until some round succeeds
        or the deadline expires. The reported result reflects the last round
        evaluated.
        """
        if single_shot:
            return self._eval_any_round(node, deadline, bound_by_deadline=False)
        return self._poll_rounds(
            lambda: self._eval_any_round(node, deadline, bound_by_deadline=True), deadline
        )

    def _eval_any_round(
        self, node: AnySpec, deadline: float, *, bound_by_deadline: bool = False
    ) -> VerificationResult:
        """Run one bounded pass over ``node``'s children, stopping at the first success.

        Every child is evaluated with ``single_shot=True`` (compound children
        propagate it), so this pass is safe to call repeatedly as one round of
        a converge poll without any child's own I/O overrunning the round.
        ``deadline`` is threaded through (not zeroed) so a nested parallel
        child's single_shot wait stays bounded by whatever remains on the
        converge deadline instead of always waiting up to the ceiling; see
        :meth:`_run_parallel`.

        With no deadline check between children, one round could overshoot the
        shared deadline by up to ``len(node.checks)`` x a leaf's I/O floor.
        When ``bound_by_deadline`` is set (converge polling only, via
        :meth:`_run_any`), the first child is still always evaluated (the
        always-at-least-one-evaluation contract), but each subsequent child
        checks the deadline first; once it has passed, the rest of the round
        is skipped with status "error" (never observed) via
        :meth:`_skip_rest`, bounding the overshoot to at most one more leaf
        call. Assert/single_shot rounds pass ``bound_by_deadline=False``: a
        one-shot evaluation must still cover every needed child regardless of
        its (already-zero) deadline.
        """
        start = time.monotonic()
        children: list[VerificationResult] = []
        reasons: list[str] = []

        for i, child in enumerate(node.checks):
            if bound_by_deadline and i > 0 and time.monotonic() >= deadline:
                self._skip_rest(
                    node.checks,
                    i,
                    "deadline exhausted before evaluation",
                    children,
                    reasons,
                    status="error",
                )
                break
            res = self._run(child, deadline, single_shot=True)
            children.append(res)
            if res.success:
                reasons.append(f"[{i}] succeeded")
                break
            reasons.append(f"[{i}] failed: {res.reason}")

        status = _combine_disjunction([c.status for c in children])
        return VerificationResult(
            success=status == "pass",
            status=status,
            elapsed_time=time.monotonic() - start,
            reason="; ".join(reasons),
            name=node.name,
            children=children,
        )

    def _run_none(
        self, node: NoneSpec, deadline: float, *, single_shot: bool = False
    ) -> VerificationResult:
        """Evaluate members, succeeding once a round finds nothing holds.

        Under ``single_shot`` this is exactly one bounded pass, same shape as
        :meth:`_run_any`. Under converge it repolls in ROUNDS: a child passing
        mid-poll does not fail the group outright (the desired state "nothing
        holds" may still be converging), so polling continues until a round
        has every child fail, or the deadline expires with every round seeing
        some child pass. The reported result reflects the last round
        evaluated.
        """
        if single_shot:
            return self._eval_none_round(node, deadline, bound_by_deadline=False)
        return self._poll_rounds(
            lambda: self._eval_none_round(node, deadline, bound_by_deadline=True), deadline
        )

    def _eval_none_round(
        self, node: NoneSpec, deadline: float, *, bound_by_deadline: bool = False
    ) -> VerificationResult:
        """Run one bounded pass over ``node``'s children, stopping at the first pass.

        Mirrors :meth:`_eval_any_round`'s bounded-pass shape; a child that
        passes ends the round immediately, since the round has already
        answered "not everything is false" for this pass. ``deadline`` is
        threaded through for the same reason: a nested parallel child's
        single_shot wait stays bounded by whatever remains on the converge
        deadline, capped at the ceiling; see :meth:`_run_parallel`.

        ``bound_by_deadline`` bounds the round overshoot to at most one more
        leaf call, the same way and for the same converge-only reason as
        :meth:`_eval_any_round`: the first child always runs, later children
        check the deadline first and, once it has passed, the rest of the
        round is skipped with status "error" via :meth:`_skip_rest`.
        """
        start = time.monotonic()
        children: list[VerificationResult] = []
        reasons: list[str] = []

        for i, child in enumerate(node.checks):
            if bound_by_deadline and i > 0 and time.monotonic() >= deadline:
                self._skip_rest(
                    node.checks,
                    i,
                    "deadline exhausted before evaluation",
                    children,
                    reasons,
                    status="error",
                )
                break
            res = self._run(child, deadline, single_shot=True)
            children.append(res)
            if res.success:
                reasons.append(f"[{i}] unexpectedly succeeded: {res.reason}")
                break
            reasons.append(f"[{i}] did not hold, as required")

        status = _combine_negated_disjunction([c.status for c in children])
        return VerificationResult(
            success=status == "pass",
            status=status,
            elapsed_time=time.monotonic() - start,
            reason="; ".join(reasons),
            name=node.name,
            children=children,
        )

    @staticmethod
    def _poll_rounds(
        run_round: Callable[[], VerificationResult], deadline: float
    ) -> VerificationResult:
        """Poll ``run_round`` until it succeeds or ``deadline`` passes.

        Shared by the converge branches of :meth:`_run_any` and
        :meth:`_run_none`: each call to ``run_round`` is one bounded pass over
        every needed child, so repolling here (rather than inside a single
        child's own ``verify``) is what keeps one child's patience from coming
        out of another's budget. Always evaluates at least one round, even
        against an already-past deadline, mirroring how a single_shot round
        would run. The elapsed time reported spans every round polled, not
        just the last one.
        """
        start = time.monotonic()
        last: VerificationResult | None = None

        def predicate() -> bool:
            nonlocal last
            last = run_round()
            return last.success

        poll_until(predicate, timeout_sec=max(0.0, deadline - time.monotonic()))
        assert last is not None  # predicate always runs at least once
        return last.model_copy(update={"elapsed_time": time.monotonic() - start})

    def _run_parallel(
        self, node: ParallelSpec, deadline: float, *, single_shot: bool = False
    ) -> VerificationResult:
        """Run children concurrently; each sees the full remaining deadline.

        A parallel child still blocked in ``kubectl wait`` / ``poll_until`` when
        the deadline hits is bounded by the ``remaining`` value handed to its
        ``verify`` call, so worker threads do not linger long past the deadline.
        A child whose future is not in ``done`` once the wait returns was never
        observed to pass or fail, so it is recorded with status "error"
        (reason :data:`_PARALLEL_INCOMPLETE_REASON`), not "fail": a hung
        ``kubectl`` call under assert mode must not read as an observed
        safeguard VIOLATION. A leaf that unexpectedly raises is converted to a
        failed child result so one bad leaf does not abort the rest of the
        group. Under ``single_shot``
        the wait is capped at :data:`_SINGLE_SHOT_WAIT_CEILING_SEC`; without
        that a single hung child would stall the whole run forever. In pure
        assert mode ``deadline`` is effectively "now" (``run_entry`` sets it
        to zero budget), so remaining time is not meaningful there and the
        wait uses the ceiling outright. Nested inside a converge round,
        though, ``deadline`` is the real shared deadline, and the wait must
        not outlive it or a single round can overshoot the converge deadline
        by up to the ceiling, defeating the total-budget bound; the wait is
        clamped to whichever of the ceiling and the remaining deadline is
        smaller.
        """
        start = time.monotonic()
        results: list[VerificationResult] = [
            _failed(child, _PARALLEL_INCOMPLETE_REASON, status="error") for child in node.checks
        ]
        workers = min(_MAX_PARALLEL_WORKERS, len(node.checks))
        # ``cancel_futures=True`` (3.9+) drops queued-but-not-started futures so
        # an exhausted deadline does not block on workers we never want to wait
        # for. In-flight workers are still bounded by the deadline-aware
        # ``verify(remaining)`` call, so they cannot linger long.
        ex = ThreadPoolExecutor(max_workers=workers)
        try:
            futs = {
                ex.submit(self._run, child, deadline, single_shot=single_shot): i
                for i, child in enumerate(node.checks)
            }
            if single_shot:
                remaining = deadline - time.monotonic()
                wait_timeout = (
                    min(_SINGLE_SHOT_WAIT_CEILING_SEC, remaining)
                    if remaining > 0
                    else _SINGLE_SHOT_WAIT_CEILING_SEC
                )
            else:
                wait_timeout = max(0.0, deadline - time.monotonic())
            done, _ = futures_wait(futs, timeout=wait_timeout)
            for f, i in futs.items():
                if f not in done:
                    continue
                try:
                    results[i] = f.result()
                except Exception as exc:  # noqa: BLE001 - convert to an errored child
                    results[i] = VerificationResult(
                        success=False,
                        status="error",
                        elapsed_time=0.0,
                        reason=f"unhandled error: {exc}",
                        name=_node_name(node.checks[i]),
                    )
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
        status = _combine_conjunction([r.status for r in results])
        # A parallel node runs every child, so the joined reason is the only
        # place the caller sees which child failed and why.
        reasons = [
            f"[{i}] {'ok' if r.success else f'failed: {r.reason}'}" for i, r in enumerate(results)
        ]
        return VerificationResult(
            success=status == "pass",
            status=status,
            elapsed_time=time.monotonic() - start,
            reason="; ".join(reasons),
            name=node.name,
            children=results,
        )
