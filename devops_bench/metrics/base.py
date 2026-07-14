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

"""Core extension surface for metrics: registry, typed score, run context."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from devops_bench.core import Registry

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from deepeval.test_case import LLMTestCase

__all__ = [
    "GEVAL_PASS_THRESHOLD",
    "METRICS",
    "MetricContext",
    "MetricEvaluator",
    "MetricScore",
    "run_geval",
]

# Default pass cutoff for a single GEval judgment. DeepEval's own default is
# 0.5; the builtin metrics deliberately score at 0.8, so every GEval-backed
# metric passes this explicitly rather than inheriting the looser SDK default.
GEVAL_PASS_THRESHOLD = 0.8

# Entry-point discovery lets external packages add metrics.
METRICS: Registry[type[MetricEvaluator]] = Registry(
    "metrics", entry_point_group="devops_bench.metrics"
)


@dataclass
class MetricScore:
    """One score entry produced by a metric evaluator.

    Attributes:
        name: Score key written into ``res["scores"]``.
        score: Numeric score, or ``None`` for metrics that only report success.
        success: Pass/fail flag; ``None`` for bare-value metrics (rates / perf
            passthroughs).
        reason: Human-readable explanation, when the metric produced one.
    """

    name: str
    score: float | None
    success: bool | None = None
    reason: str | None = None

    def to_entry(self) -> dict[str, Any] | float | None:
        """Serialize into the ``results.json`` score shape.

        Returns:
            The dict form for judged metrics, or the bare value when both
            ``success`` and ``reason`` are ``None``.

        Example:
            >>> MetricScore("DocRetrievalRate", 0.5).to_entry()
            0.5
            >>> MetricScore(
            ...     "OutcomeValidity", 1.0, success=True, reason="ok"
            ... ).to_entry()
            {'score': 1.0, 'success': True, 'reason': 'ok'}
        """
        if self.success is None and self.reason is None:
            return self.score
        return {"score": self.score, "success": self.success, "reason": self.reason}


@dataclass
class MetricContext:
    """Everything a metric needs to score one execution result.

    Attributes:
        result: The raw execution result dict.
        judge: A ``DeepEvalBaseLLM`` judge model.
        use_mcp: Whether the run was granted MCP tool capabilities.
        outcome_case: The outcome-focused ``LLMTestCase``.
        tool_case: The tools-and-trajectory ``LLMTestCase``.
        all_case: The combined (text + trace) ``LLMTestCase``.
        generation_only: Whether the task provisions no cluster (``deployer:
            noop``); outcome scoring must not require cluster application for these.
    """

    result: dict[str, Any]
    judge: Any
    use_mcp: bool
    outcome_case: LLMTestCase
    tool_case: LLMTestCase
    all_case: LLMTestCase
    generation_only: bool = False


@runtime_checkable
class MetricEvaluator(Protocol):
    """A self-registering metric family.

    Concrete evaluators are registered via ``@METRICS.register("<key>")`` and
    implement two methods: :meth:`applies` (gate) and :meth:`evaluate` (yield
    zero or more :class:`MetricScore`).

    Attributes:
        name: Identifier used in logs; per-score keys come from each
            :class:`MetricScore`'s ``name`` instead.
    """

    name: str

    def applies(self, ctx: MetricContext) -> bool:
        """Whether this metric runs for the given result."""

    def evaluate(self, ctx: MetricContext) -> Iterable[MetricScore]:
        """Produce zero or more score entries for the result."""


def run_geval(case: LLMTestCase, metrics: list[Any]) -> list[MetricScore]:
    """Evaluate GEval ``metrics`` against ``case`` and return clean scores.

    The trailing ``" [GEval]"`` suffix DeepEval appends to GEval metric names is
    stripped from the resulting score keys.

    Args:
        case: The test case to score.
        metrics: GEval metric instances to evaluate against ``case``.

    Returns:
        One :class:`MetricScore` per metric reported by DeepEval.
    """
    # ``deepeval`` is imported lazily so ``import devops_bench.metrics.base``
    # (and the registry registrations it triggers) never pulls the SDK.
    from deepeval import evaluate

    out: list[MetricScore] = []
    result = evaluate([case], metrics=metrics)
    for test_result in result.test_results:
        for md in test_result.metrics_data:
            name = md.name.removesuffix(" [GEval]")
            out.append(
                MetricScore(
                    name=name,
                    score=md.score,
                    success=md.success,
                    reason=getattr(md, "reason", None),
                )
            )
    return out
