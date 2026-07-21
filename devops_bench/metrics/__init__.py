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

"""LLM-as-judge scoring for benchmark runs.

The public extension surface is :data:`METRICS` (the registry), :class:`MetricScore`,
:class:`MetricContext`, and :func:`run_geval`. The batch entry point
:func:`evaluate_metrics_batch` consumes the registry; concrete metric modules
self-register on import. Importing this package pulls ``deepeval``, but never a
provider SDK — those stay lazy in the models layer.
"""

from __future__ import annotations

from devops_bench.metrics.base import (
    METRICS,
    MetricContext,
    MetricEvaluator,
    MetricScore,
    run_geval,
)
from devops_bench.metrics.chaos_metrics import evaluate_chaos_metrics
from devops_bench.metrics.geval import ModelLayerJudge, get_judge_model
from devops_bench.metrics.grounding import (
    calculate_doc_retrieval_rate,
    evaluate_documentation_grounding,
)
from devops_bench.metrics.outcome_validity import build_outcome_validity_metric
from devops_bench.metrics.pipeline import (
    CHECKLIST_THRESHOLD,
    evaluate_metrics_batch,
    extract_checklist_items,
)
from devops_bench.metrics.tool_invocation import build_tool_invocation_metric

__all__ = [
    "CHECKLIST_THRESHOLD",
    "METRICS",
    "MetricContext",
    "MetricEvaluator",
    "MetricScore",
    "ModelLayerJudge",
    "build_outcome_validity_metric",
    "build_tool_invocation_metric",
    "calculate_doc_retrieval_rate",
    "evaluate_chaos_metrics",
    "evaluate_documentation_grounding",
    "evaluate_metrics_batch",
    "extract_checklist_items",
    "get_judge_model",
    "run_geval",
]
