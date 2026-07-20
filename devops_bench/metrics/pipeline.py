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

"""Batch scoring loop that turns execution results into per-task scores."""

from __future__ import annotations

import json
from typing import Any

from deepeval.test_case import LLMTestCase

from devops_bench.core import get_bool, get_logger

# Metric families populate the ``METRICS`` registry via their
# ``@METRICS.register`` decorators when their modules are imported.
from devops_bench.metrics.base import (
    METRICS,
    MetricContext,
)

__all__ = [
    "evaluate_metrics_batch",
]

_log = get_logger("metrics.pipeline")

# Order in which builtin metric keys appear in results.json.
_BUILTIN_METRIC_KEYS: tuple[str, ...] = (
    "outcome_validity",
    "tool_invocation",
    "checklist",
    "grounding",
    "chaos",
)


def _canonical_tool_name(name: str) -> str:
    """Strip an MCP server prefix from a tool name for judge matching.

    MCP tools surface as ``<server>__<tool>`` (e.g. ``default__generate_manifest``);
    tasks reference the canonical ``<tool>``. Returns the segment after the first
    ``"__"``, or the name unchanged when there is no prefix.

    >>> _canonical_tool_name("default__generate_manifest")
    'generate_manifest'
    >>> _canonical_tool_name("run_shell_command")
    'run_shell_command'
    """
    if not isinstance(name, str):
        return name
    return name.split("__", 1)[1] if "__" in name else name


def _build_context(res: dict[str, Any], judge_model: Any, use_mcp: bool) -> MetricContext:
    """Build the per-result :class:`MetricContext`, sharing test cases.

    Args:
        res: One execution result dict.
        judge_model: A ``DeepEvalBaseLLM`` judge.
        use_mcp: Whether the run was granted MCP capabilities.

    Returns:
        A populated :class:`MetricContext` with the three test cases built once.
    """
    # ``input`` / ``output`` / ``expected_output`` are the minimum a judge needs
    # and are always written by the harness. The trajectory / latency /
    # retrieval-context fields default to empty so a slimmer external result dict
    # degrades to an unscored case rather than raising an uncaught ``KeyError``
    # here (before the per-metric ``try`` in the batch loop).
    prompt = res["input"]
    actual_output = res["output"]
    expected_output = res.get("expected_output", "")
    if not expected_output:
        _log.warning(
            "Result %r has an empty expected_output. If this task is scored by "
            "the LLM judge, this may skew evaluation scores.",
            res.get("name"),
        )
    trajectory = res.get("trajectory", [])
    latency = res.get("latency")
    retrieval_context = res.get("retrieval_context")

    # Tool names surface with an MCP server prefix (e.g. ``default__generate_manifest``);
    # expected-tool checks in tasks reference the canonical name (``generate_manifest``).
    # Normalize only for the judge test cases so tool-call matching isn't brittle;
    # the on-disk record keeps the raw names.
    norm_tools = [_canonical_tool_name(t) for t in res.get("tools", [])]
    norm_trajectory = [
        {**entry, "name": _canonical_tool_name(entry.get("name", ""))}
        if isinstance(entry, dict)
        else entry
        for entry in trajectory
    ]

    outcome_case = LLMTestCase(
        input=prompt,
        actual_output=actual_output if actual_output else "No response generated",
        expected_output=expected_output,
        retrieval_context=retrieval_context,
        latency=latency,
    )

    combined_actual = {
        "tools_used": norm_tools,
        "execution_trace": norm_trajectory,
    }
    tool_case = LLMTestCase(
        input=prompt,
        actual_output=json.dumps(combined_actual, indent=2),
        expected_output=expected_output,
        latency=latency,
    )

    all_context = {
        "tools_used": norm_tools,
        "execution_trace": norm_trajectory,
        "text_output": actual_output if actual_output else "No response generated",
    }
    all_case = LLMTestCase(
        input=prompt,
        actual_output=json.dumps(all_context, indent=2),
        expected_output=expected_output,
        latency=latency,
    )

    return MetricContext(
        result=res,
        judge=judge_model,
        use_mcp=use_mcp,
        outcome_case=outcome_case,
        tool_case=tool_case,
        all_case=all_case,
        generation_only=bool(res.get("generation_only", False)),
    )


def evaluate_metrics_batch(
    detailed_results: list[dict[str, Any]],
    judge_model: Any,
    *,
    use_mcp: bool | None = None,
) -> None:
    """Score a batch of execution results in place via the metrics registry.

    Adding a metric is a new ``@METRICS.register(...)`` class in any metric
    module — there is no orchestration surgery required here. Each registered
    evaluator's :meth:`MetricEvaluator.applies` gates whether it runs for a
    given result, and one failing metric never aborts the rest.

    Args:
        detailed_results: Execution result dicts. ``input``, ``output``, and
            ``expected_output`` are required; ``trajectory``, ``latency``,
            ``retrieval_context``, ``name``, ``documentation``, ``chaos_spec`` /
            ``chaos_report`` / ``perf_report``, and ``tools`` are optional and
            default to empty when absent.
        judge_model: A ``DeepEvalBaseLLM`` judge model.
        use_mcp: Whether the harness granted MCP tool capabilities. ``None``
            falls back to the ``BENCH_USE_MCP`` env var.
    """
    _log.info(
        "Starting batch post-processing evaluation metrics for %d result(s)...",
        len(detailed_results),
    )
    if use_mcp is None:
        use_mcp = get_bool("BENCH_USE_MCP", True)

    builtin_keys = list(_BUILTIN_METRIC_KEYS)
    builtin_set = set(builtin_keys)
    # Builtin metrics in the pinned (results.json) order, then any third-party
    # registrations in registry insertion order.
    ordered_keys = builtin_keys + [k for k in METRICS if k not in builtin_set]
    evaluators = [METRICS[k]() for k in ordered_keys]

    for res in detailed_results:
        ctx = _build_context(res, judge_model, use_mcp)
        scores: dict[str, Any] = {}
        _log.debug("Evaluating metrics for: %s...", res.get("name"))
        for ev in evaluators:
            try:
                if not ev.applies(ctx):
                    continue
                for ms in ev.evaluate(ctx):
                    scores[ms.name] = ms.to_entry()
            except Exception:  # noqa: BLE001 - one metric must not abort the rest
                _log.exception(
                    "metric %r failed for %s",
                    getattr(ev, "name", ev),
                    res.get("name"),
                )
        res["scores"] = scores
