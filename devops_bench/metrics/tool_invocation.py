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

"""ToolInvocation GEval metric and its skill loading."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from deepeval.metrics import GEval
from deepeval.test_case import SingleTurnParams

from devops_bench.core import get_logger
from devops_bench.metrics._skills import load_skill_text
from devops_bench.metrics.base import (
    METRICS,
    MetricContext,
    MetricScore,
    run_geval,
)

__all__ = [
    "TOOL_SKILL_FILENAME",
    "TOOL_INVOCATION_THRESHOLD",
    "ToolInvocationMetric",
    "build_tool_invocation_metric",
    "load_tool_criteria",
]

TOOL_SKILL_FILENAME = "tool-invocation-skill.md"
TOOL_INVOCATION_THRESHOLD = 0.8

_log = get_logger("metrics.tool_invocation")


def load_tool_criteria() -> str:
    """Read the tool-invocation skill packaged with the project.

    Returns:
        The full markdown text used as the GEval criteria.

    Raises:
        FileNotFoundError: If the packaged skill file is missing.
    """
    return load_skill_text(TOOL_SKILL_FILENAME)


def build_tool_invocation_metric(model: Any) -> GEval:
    """Build the ToolInvocation GEval metric.

    Args:
        model: A ``DeepEvalBaseLLM`` judge model.

    Returns:
        A configured :class:`~deepeval.metrics.GEval` instance with the
        tool-invocation pass threshold applied.
    """
    return GEval(
        name="ToolInvocation",
        criteria=load_tool_criteria(),
        threshold=TOOL_INVOCATION_THRESHOLD,
        evaluation_params=[
            SingleTurnParams.INPUT,
            SingleTurnParams.ACTUAL_OUTPUT,
            SingleTurnParams.EXPECTED_OUTPUT,
        ],
        model=model,
    )


@METRICS.register("tool_invocation")
class ToolInvocationMetric:
    """Registered evaluator scoring ToolInvocation when MCP is enabled.

    Attributes:
        name: Score key written into ``res["scores"]``.
    """

    name = "ToolInvocation"

    def applies(self, ctx: MetricContext) -> bool:
        """Run only when the harness has granted MCP capabilities to the agent."""
        return ctx.use_mcp

    def evaluate(self, ctx: MetricContext) -> Iterable[MetricScore]:
        """Score the tool-invocation GEval against the tool-trace test case."""
        return run_geval(ctx.tool_case, [build_tool_invocation_metric(ctx.judge)])
