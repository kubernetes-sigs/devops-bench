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
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as futures_wait
from typing import Any

from devops_bench.verification.base import BaseVerifier, VerificationResult
from devops_bench.verification.spec import (
    ParallelSpec,
    SequenceSpec,
    VerificationSpec,
)

__all__ = ["VerifierAgent"]

_MAX_PARALLEL_WORKERS = 8

# A leaf invoked with less than this many seconds left on the deadline is
# short-circuited as timed out. Avoids issuing useless ``kubectl wait
# --timeout=0.001s`` calls at the tail of the budget.
_MIN_LEAF_BUDGET_SECONDS = 1.0


def _node_name(node: Any) -> str | None:
    """Echo the optional ``name`` label from a spec node, if any."""
    return getattr(node, "name", None)


def _failed(node: Any, reason: str) -> VerificationResult:
    """Build a failed result for a node not run to completion.

    Covers deadline exhaustion and sequence fail-fast skips alike; ``reason``
    carries the specific cause.
    """
    return VerificationResult(
        success=False,
        elapsed_time=0.0,
        reason=reason,
        name=_node_name(node),
    )


class VerifierAgent:
    """Evaluate single or compound verification specs against cluster state.

    All evaluations share a single monotonic deadline established by
    :meth:`wait_for_condition`. Compound nodes propagate the deadline without
    rebudgeting; leaves consume it directly via their ``verify`` method.
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

    def _run(self, node: Any, deadline: float) -> VerificationResult:
        """Dispatch a node against the shared deadline."""
        if isinstance(node, SequenceSpec):
            return self._run_sequence(node, deadline)
        if isinstance(node, ParallelSpec):
            return self._run_parallel(node, deadline)
        return self._run_leaf(node, deadline)

    def _run_leaf(self, node: Any, deadline: float) -> VerificationResult:
        """Run a leaf verifier with whatever budget remains on the deadline.

        Short-circuits when the remaining budget is below
        :data:`_MIN_LEAF_BUDGET_SECONDS` so we never issue a useless
        sub-second ``kubectl wait`` at the tail of the deadline.
        """
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
    ) -> None:
        """Mark every child from ``start_index`` onward as skipped, in one pass."""
        for j, rest in enumerate(checks[start_index:], start=start_index):
            children.append(_failed(rest, reason))
            reasons.append(f"[{j}] skipped")

    def _run_sequence(self, node: SequenceSpec, deadline: float) -> VerificationResult:
        """Run children in order; stop and skip the rest on the first failure.

        Both a hit deadline and a failed step halt the walk immediately and
        bulk-mark every remaining child skipped, so later children never incur a
        per-item loop iteration (nor a redundant deadline check) once the
        sequence can no longer make progress.
        """
        start = time.monotonic()
        children: list[VerificationResult] = []
        reasons: list[str] = []
        ok = True
        for i, child in enumerate(node.checks):
            if time.monotonic() >= deadline:
                ok = False
                self._skip_rest(node.checks, i, "deadline exhausted", children, reasons)
                break
            res = self._run(child, deadline)
            children.append(res)
            if not res.success:
                ok = False
                reasons.append(f"[{i}] failed: {res.reason}")
                self._skip_rest(node.checks, i + 1, "earlier step failed", children, reasons)
                break  # fail-fast
            reasons.append(f"[{i}] succeeded")
        return VerificationResult(
            success=ok,
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
            return self._eval_any_round(node, deadline)
        return self._poll_rounds(lambda: self._eval_any_round(node, deadline), deadline)

    def _eval_any_round(self, node: AnySpec, deadline: float) -> VerificationResult:
        """Run one bounded pass over ``node``'s children, stopping at the first success.

        Every child is evaluated with ``single_shot=True`` (compound children
        propagate it), so this pass is safe to call repeatedly as one round of
        a converge poll without any child's own I/O overrunning the round.
        ``deadline`` is threaded through (not zeroed) so a nested parallel
        child's single_shot wait stays bounded by whatever remains on the
        converge deadline instead of always waiting up to the ceiling; see
        :meth:`_run_parallel`.
        """
        start = time.monotonic()
        children: list[VerificationResult] = []
        reasons: list[str] = []

        for i, child in enumerate(node.checks):
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
            return self._eval_none_round(node, deadline)
        return self._poll_rounds(lambda: self._eval_none_round(node, deadline), deadline)

    def _eval_none_round(self, node: NoneSpec, deadline: float) -> VerificationResult:
        """Run one bounded pass over ``node``'s children, stopping at the first pass.

        Mirrors :meth:`_eval_any_round`'s bounded-pass shape; a child that
        passes ends the round immediately, since the round has already
        answered "not everything is false" for this pass. ``deadline`` is
        threaded through for the same reason: a nested parallel child's
        single_shot wait stays bounded by whatever remains on the converge
        deadline, capped at the ceiling; see :meth:`_run_parallel`.
        """
        start = time.monotonic()
        children: list[VerificationResult] = []
        reasons: list[str] = []

        for i, child in enumerate(node.checks):
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
        A leaf that unexpectedly raises is converted to a failed child result so
        one bad leaf does not abort the rest of the group.
        """
        start = time.monotonic()
        results: list[VerificationResult] = [
            _failed(child, "deadline reached") for child in node.checks
        ]
        workers = min(_MAX_PARALLEL_WORKERS, len(node.checks))
        # ``cancel_futures=True`` (3.9+) drops queued-but-not-started futures so
        # an exhausted deadline does not block on workers we never want to wait
        # for. In-flight workers are still bounded by the deadline-aware
        # ``verify(remaining)`` call, so they cannot linger long.
        ex = ThreadPoolExecutor(max_workers=workers)
        try:
            futs = {ex.submit(self._run, child, deadline): i for i, child in enumerate(node.checks)}
            done, _ = futures_wait(futs, timeout=max(0.0, deadline - time.monotonic()))
            for f, i in futs.items():
                if f not in done:
                    continue
                try:
                    results[i] = f.result()
                except Exception as exc:  # noqa: BLE001 - convert to a failed child
                    results[i] = VerificationResult(
                        success=False,
                        elapsed_time=0.0,
                        reason=f"unhandled error: {exc}",
                        name=_node_name(node.checks[i]),
                    )
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
        ok = all(r.success for r in results)
        reasons = [f"[{i}] {'ok' if r.success else 'fail'}" for i, r in enumerate(results)]
        return VerificationResult(
            success=ok,
            elapsed_time=time.monotonic() - start,
            reason="; ".join(reasons),
            name=node.name,
            children=results,
        )
