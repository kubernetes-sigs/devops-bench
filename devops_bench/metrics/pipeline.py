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

from devops_bench.core import get_bool, get_logger, score_keys

# Imported for their @METRICS.register side effects.
from devops_bench.metrics import (
    chaos_metrics,  # noqa: F401
    grounding,  # noqa: F401
    outcome_validity,  # noqa: F401
    safety,  # noqa: F401
    tool_invocation,  # noqa: F401
    verification,  # noqa: F401
)
from devops_bench.metrics.base import (
    METRICS,
    MetricContext,
)

# Re-exported for callers importing from this module.
from devops_bench.metrics.checklist import (
    CHECKLIST_THRESHOLD,
    ChecklistMetric,
    extract_checklist_items,
)
from devops_bench.metrics.scoring import (
    SCORING_VERSION,
    compute_outcome_score_v1,
    rescale_recoverable_safety,
)

__all__ = [
    "CHECKLIST_THRESHOLD",
    "OUTCOME_SCORE_KEY",
    "ChecklistMetric",
    "evaluate_metrics_batch",
    "extract_checklist_items",
]

_log = get_logger("metrics.pipeline")

#: ``res["scores"]`` key carrying the v1 composite outcome score. Assembled from
#: the sub-scores after all metrics run (see :func:`_finalize_outcome_score`);
#: the flat leaderboard row reads its ``outcomeScore`` from this key.
OUTCOME_SCORE_KEY = score_keys.OUTCOME_SCORE_KEY

# Sub-score keys read to assemble the composite, in preference order. Sourced
# from ``core.score_keys`` so the emitters, this assembly, and
# ``results.normalize`` share one definition; reading them by name still keeps
# the assembly from importing the metric modules that emit them.
#
# Deterministic signals win over judged ones for the same quantity: a task that
# expresses a check in its ``verification_spec`` has said what it means exactly,
# so a judge's reading of prose should not override it. No task declares both
# today; if one ever does, this is the rule it follows.
_CORRECTNESS_KEYS = (
    score_keys.VERIFICATION_CORRECTNESS_KEY,
    score_keys.CHECKLIST_SCORE_KEY,
    score_keys.OUTCOME_VALIDITY_KEY,
)
_RECOVERABLE_KEYS = (
    score_keys.VERIFICATION_RECOVERABLE_KEY,
    score_keys.JUDGED_RECOVERABLE_KEY,
)
_CATASTROPHIC_KEY = score_keys.VERIFICATION_CATASTROPHIC_KEY

# Order in which builtin metric keys appear in results.json.
_BUILTIN_METRIC_KEYS: tuple[str, ...] = (
    "outcome_validity",
    "tool_invocation",
    "checklist",
    "safety",
    "grounding",
    "chaos",
    "verification",
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


def _score_value(entry: Any) -> float | None:
    """Return the numeric score from a ``res["scores"]`` entry, or ``None``.

    Handles both shapes ``MetricScore.to_entry`` produces: a bare number or a
    ``{"score": ...}`` dict. A boolean is treated as absent so a flag never
    masquerades as a 0/1 score.
    """
    if isinstance(entry, dict):
        entry = entry.get("score")
    if isinstance(entry, bool):
        return None
    return float(entry) if isinstance(entry, (int, float)) else None


def _first_score(scores: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    """Return the score under the first key in ``keys`` that carries one.

    Args:
        scores: The per-metric score map for one record.
        keys: Candidate score keys in preference order.

    Returns:
        The first numeric score found, or ``None`` when no key carries one.
    """
    for key in keys:
        value = _score_value(scores.get(key))
        if value is not None:
            return value
    return None


def _finalize_outcome_score(scores: dict[str, Any]) -> None:
    """Assemble the v1 composite ``OutcomeScore`` from the sub-scores, in place.

    Each signal is taken from the first key present in its preference chain, so
    a deterministic verification score wins over the judged equivalent. Both
    recoverable sources emit a raw pass fraction; the ``[0.1, 1.0]`` rescale is
    applied here so the floor lives in one place regardless of which produced
    it. Records with no correctness signal at all (e.g. failed runs with empty
    scores) get no composite, leaving ``outcomeScore`` null downstream.

    Args:
        scores: The per-metric score map for one record, mutated to add
            :data:`OUTCOME_SCORE_KEY`.
    """
    correctness = _first_score(scores, _CORRECTNESS_KEYS)
    if correctness is None:
        return

    catastrophic_score = _score_value(scores.get(_CATASTROPHIC_KEY))
    catastrophic = catastrophic_score == 0.0 if catastrophic_score is not None else False

    # Read the gate before rescaling. ``compute_outcome_score_v1`` deliberately
    # short-circuits a catastrophic run before validating its other inputs, so
    # rescaling first would raise on a malformed value the short-circuit is
    # meant to tolerate, and the record would lose the catastrophic signal too.
    recoverable = None
    if not catastrophic:
        raw_recoverable = _first_score(scores, _RECOVERABLE_KEYS)
        if raw_recoverable is not None:
            recoverable = rescale_recoverable_safety(raw_recoverable)

    outcome = compute_outcome_score_v1(
        correctness=correctness,
        recoverable_safety=recoverable,
        catastrophic=catastrophic,
    )
    scores[OUTCOME_SCORE_KEY] = {
        "score": outcome,
        "version": SCORING_VERSION,
        "reason": (
            f"c={correctness:.3f}, "
            f"rec_v={'n/a' if recoverable is None else format(recoverable, '.3f')}, "
            f"cat_v={0 if catastrophic else 1}"
        ),
    }


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

    builtin_set = set(_BUILTIN_METRIC_KEYS)
    # Builtin metrics in the pinned (results.json) order, then any third-party
    # registrations in registry insertion order. Builtins absent from the
    # registry are skipped: the pipeline defers importing concrete families, so
    # a key may not be registered yet and ``METRICS[k]()`` would otherwise raise.
    ordered_keys = [k for k in _BUILTIN_METRIC_KEYS if k in METRICS] + [
        k for k in METRICS if k not in builtin_set
    ]
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
        # Assemble the versioned composite from the sub-scores once every metric
        # has run, so it can read the checklist / safety outputs above. Guarded
        # like the metric loop: a malformed sub-score raises out of the scoring
        # formula, and must cost this record its composite rather than abort the
        # remaining records in the batch.
        try:
            _finalize_outcome_score(scores)
        except Exception:  # noqa: BLE001 - one record must not abort the batch
            _log.exception("composite outcome score failed for %s", res.get("name"))
        res["scores"] = scores
