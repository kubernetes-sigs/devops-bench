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

"""Result model, abstract base, and registry for verification checks.

Each leaf verifier and each compound spec (``sequence`` / ``parallel``)
registers itself in the :data:`VERIFIERS` registry by its ``type`` literal.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from devops_bench.core import Registry
from devops_bench.k8s import poll_until

__all__ = [
    "VERIFIERS",
    "MIN_LEAF_BUDGET_SECONDS",
    "BaseVerifier",
    "VerificationResult",
    "VerificationStatus",
    "single_call_timeout",
]

# Registry keyed by the ``type`` discriminator literal. Entry-point discovery
# lets external packages register a verifier without touching this tree.
VERIFIERS: Registry[type[BaseModel]] = Registry(
    "verifiers", entry_point_group="devops_bench.verifiers"
)

# The single definition of the leaf-budget threshold. Lives here, not in
# runner.py, because base.py sits below runner.py in the import graph (runner
# imports from here, not the other way around) and single_call_timeout below
# needs the same threshold runner.py's own leaf dispatch uses to treat a
# budget as the zero/assert path rather than a real one. runner.py and
# default.py both import this constant rather than defining their own copy.
# Public because default.py (outside this package) needs it too; the private
# alias below is kept so the in-package call sites and docstrings that spell
# out the underscored name keep working unchanged.
MIN_LEAF_BUDGET_SECONDS = 1.0
_MIN_LEAF_BUDGET_SECONDS = MIN_LEAF_BUDGET_SECONDS

# A single kubectl call needs a hard bound even when the caller's own budget
# is (near) zero: an assert-mode single_shot call passes timeout_sec=0.0,
# which as a literal subprocess timeout would mean "give up immediately"
# rather than "evaluate once". This is what actually bounds that one
# evaluation's I/O so an unresponsive API server cannot hang the whole
# benchmark run.
_KUBECTL_TIMEOUT_FLOOR_SEC = 30.0


def single_call_timeout(timeout_sec: float) -> float:
    """Bound one kubectl call's timeout without extending a real budget.

    Naively flooring every call (``max(timeout_sec, _KUBECTL_TIMEOUT_FLOOR_SEC)``)
    fixes the zero-budget case but also raises any small *real* budget up to
    the floor: a leaf handed 5 remaining seconds on a converging deadline
    would then spend up to 30 seconds in kubectl, well past what its caller
    budgeted. The floor should only ever apply to the zero/assert path, where
    there is no real budget to protect. ``timeout_sec`` below the runner's
    minimum effective leaf budget is that path (see
    :data:`_MIN_LEAF_BUDGET_SECONDS`); anything at or above it is a
    real budget and is returned unchanged.

    Args:
        timeout_sec: The caller's remaining budget for this one evaluation.

    Returns:
        ``timeout_sec`` unchanged when it is a real budget, else
        :data:`_KUBECTL_TIMEOUT_FLOOR_SEC`.
    """
    if timeout_sec < _MIN_LEAF_BUDGET_SECONDS:
        return _KUBECTL_TIMEOUT_FLOOR_SEC
    return timeout_sec


# "error" is not a third outcome of the condition, it is the absence of an
# observation: the check could not run (kubectl/subprocess failure, unhandled
# exception) rather than ran and found the condition false. Keeping it
# distinct from "fail" is what stops an environmental hiccup from reading as
# an observed violation.
VerificationStatus = Literal["pass", "fail", "error"]


class VerificationResult(BaseModel):
    """Structured outcome of a verification check.

    Compound nodes (sequence/parallel) populate ``children`` with one entry per
    member; leaf checks populate ``raw`` with kubectl diagnostics. ``name`` is
    echoed from the originating spec node's optional label.

    Attributes:
        success: True when every condition the check covers was met. Kept for
            backward compatibility; always equal to ``status == "pass"``.
        status: The tri-state outcome. ``"error"`` means the check could not
            be evaluated (environmental), not that the condition was observed
            false. Left unset, it is derived from ``success`` (``"pass"`` /
            ``"fail"``) so existing ``VerificationResult(success=...)`` call
            sites keep working; a caller that needs to report an error must
            pass ``status="error"`` explicitly (and, for the invariant above,
            ``success=False``). The ``| None`` in the annotation is only a
            pre-validation sentinel: ``_resolve_status`` below always
            resolves it before construction completes, so a constructed
            result's ``status`` is never actually ``None``.
        elapsed_time: Wall-clock seconds spent evaluating the check.
        reason: Human-readable summary of the outcome or failure.
        name: Optional label echoed from the spec node, for result rendering.
        children: Per-member results from compound (sequence/parallel) nodes.
        raw: Leaf-only kubectl diagnostics or supporting data.
    """

    success: bool
    status: VerificationStatus | None = None
    elapsed_time: float
    reason: str
    name: str | None = None
    children: list[VerificationResult] = Field(default_factory=list)
    raw: dict | None = None

    @model_validator(mode="after")
    def _resolve_status(self) -> VerificationResult:
        """Derive ``status`` from ``success`` when unset; enforce the invariant otherwise."""
        if self.status is None:
            self.status = "pass" if self.success else "fail"
        elif (self.status == "pass") != self.success:
            raise ValueError(
                f"status='pass' iff success=True; got status={self.status!r}, "
                f"success={self.success!r}"
            )
        return self


VerificationResult.model_rebuild()


class BaseVerifier(BaseModel, ABC):
    """Abstract base for a single leaf verification check.

    Concrete verifiers carry a ``type`` literal, an optional ``name`` for result
    labeling, and implement :meth:`verify`.

    Attributes:
        name: Optional label echoed onto the result; metadata, never structural.
        kubeconfig: Optional path to a kubeconfig file, forwarded to the
            ``devops_bench.k8s`` wrappers so a check can target a specific
            cluster. When ``None`` the wrappers use the ambient kubeconfig.
    """

    # Reject any key the concrete verifier does not declare. A typo'd or
    # stale field would otherwise be silently dropped and the check would
    # run with defaults instead of the author's intent.
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    kubeconfig: str | None = None

    @abstractmethod
    def verify(self, timeout_sec: float) -> VerificationResult:
        """Run the check and report the outcome.

        Args:
            timeout_sec: Maximum seconds the check may spend before giving up.

        Returns:
            The structured verification result.
        """
        raise NotImplementedError

    def _poll_to_result(
        self,
        check: Callable[[], tuple[VerificationStatus, str, dict[str, Any] | None]],
        timeout_sec: float,
    ) -> VerificationResult:
        """Poll ``check`` to a :class:`VerificationResult`.

        Shared scaffolding for predicate-style verifiers: times the run, polls
        ``check`` via :func:`devops_bench.k8s.poll_until`, and folds the last
        observation into a result. Verifiers backed by a server-side watch
        (e.g. ``kubectl wait``) build their result directly instead.

        Args:
            check: Evaluated once per poll, returning ``(status, reason, raw)``
                where ``status`` is "pass" when the condition currently holds,
                "fail" when it was observed not to hold, or "error" when the
                check itself could not be evaluated (e.g. a kubectl failure);
                ``reason`` describes the latest observation and ``raw`` carries
                optional diagnostics.
            timeout_sec: Maximum seconds to keep polling.

        Returns:
            A result reflecting the last observed ``status`` before the
            timeout (last observation wins, even if that observation is
            "error"), carrying the last observed ``reason`` and ``raw``.
        """
        start_time = time.monotonic()
        last: dict[str, Any] = {"status": "fail", "reason": "", "raw": None}

        def predicate() -> bool:
            status, reason, raw = check()
            last["status"] = status
            last["reason"] = reason
            last["raw"] = raw
            return status == "pass"

        poll_until(predicate, timeout_sec=timeout_sec)
        status: VerificationStatus = last["status"]
        return VerificationResult(
            success=status == "pass",
            status=status,
            elapsed_time=time.monotonic() - start_time,
            reason=last["reason"],
            name=self.name,
            raw=last["raw"],
        )
