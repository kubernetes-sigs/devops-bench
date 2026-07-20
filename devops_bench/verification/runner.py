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
                this once at the top.

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

    def _run_sequence(self, node: SequenceSpec, deadline: float) -> VerificationResult:
        """Run children in order; stop and skip the rest on the first failure."""
        start = time.monotonic()
        children: list[VerificationResult] = []
        reasons: list[str] = []
        ok = True
        for i, child in enumerate(node.checks):
            if time.monotonic() >= deadline:
                children.append(_failed(child, "deadline exhausted"))
                reasons.append(f"[{i}] skipped")
                ok = False
                continue
            res = self._run(child, deadline)
            children.append(res)
            if not res.success:
                ok = False
                reasons.append(f"[{i}] failed: {res.reason}")
                for j, rest in enumerate(node.checks[i + 1 :], start=i + 1):
                    children.append(_failed(rest, "earlier step failed"))
                    reasons.append(f"[{j}] skipped")
                break  # fail-fast
            reasons.append(f"[{i}] succeeded")
        return VerificationResult(
            success=ok,
            elapsed_time=time.monotonic() - start,
            reason="; ".join(reasons),
            name=node.name,
            children=children,
        )

    def _run_parallel(self, node: ParallelSpec, deadline: float) -> VerificationResult:
        """Run children concurrently; each sees the full remaining deadline.

        A parallel child still blocked in ``kubectl wait`` / ``poll_until`` when
        the deadline hits is bounded by the ``remaining`` value handed to its
        ``verify`` call, so worker threads do not linger long past the deadline.
        A leaf that unexpectedly raises is converted to a failed child result so
        one bad leaf does not abort the rest of the group.
        """
        start = time.monotonic()
        if not node.checks:
            return VerificationResult(
                success=True,
                elapsed_time=time.monotonic() - start,
                reason="no checks",
                name=node.name,
                children=[],
            )
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
