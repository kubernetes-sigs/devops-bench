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

"""Tests for skill loading and GEval metric construction (outcome + tool)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from deepeval.metrics.g_eval.utils import construct_test_case_string
from deepeval.models import DeepEvalBaseLLM
from deepeval.test_case import LLMTestCase, SingleTurnParams
from pytest_mock import MockerFixture

from devops_bench.metrics import _skills, outcome_validity, tool_invocation
from devops_bench.metrics.base import GEVAL_PASS_THRESHOLD
from devops_bench.metrics.tool_invocation import TOOL_INVOCATION_THRESHOLD


class _StubJudgeModel(DeepEvalBaseLLM):
    """Minimal DeepEvalBaseLLM stub so GEval can be constructed without credentials."""

    def load_model(self):
        return self

    def generate(self, prompt: str) -> str:
        return ""

    async def a_generate(self, prompt: str) -> str:
        return ""

    def get_model_name(self) -> str:
        return "stub-judge"


def _patch_resources(mocker: MockerFixture, files_by_name: dict[str, str]) -> None:
    """Patch ``_skills.resources.files`` with a fake package traversable.

    ``files_by_name`` maps a filename to its text content; any name absent from
    the mapping is treated as a missing resource (``is_file()`` is False).
    """

    class _FakeResource:
        def __init__(self, name):
            self._name = name

        def is_file(self):
            return self._name in files_by_name

        def read_text(self, encoding="utf-8"):
            return files_by_name[self._name]

    class _FakePackage:
        def __truediv__(self, name):
            return _FakeResource(name)

    mocker.patch.object(_skills.resources, "files", return_value=_FakePackage())


def test_load_skill_text_reads_packaged_resource(mocker: MockerFixture) -> None:
    _patch_resources(mocker, {"outcome-validity-checklist.md": "## Evaluation Criteria"})
    assert _skills.load_skill_text("outcome-validity-checklist.md") == "## Evaluation Criteria"


def test_load_outcome_criteria_uses_loader(mocker: MockerFixture) -> None:
    _patch_resources(mocker, {outcome_validity.OUTCOME_SKILL_FILENAME: "OUTCOME-MD"})
    assert outcome_validity.load_outcome_criteria() == "OUTCOME-MD"


def test_load_tool_criteria_uses_loader(mocker: MockerFixture) -> None:
    _patch_resources(mocker, {tool_invocation.TOOL_SKILL_FILENAME: "TOOL-MD"})
    assert tool_invocation.load_tool_criteria() == "TOOL-MD"


def test_load_skill_text_missing_raises(mocker: MockerFixture) -> None:
    _patch_resources(mocker, {})  # nothing exists
    with pytest.raises(FileNotFoundError):
        _skills.load_skill_text("does-not-exist.md")


def test_build_outcome_validity_metric(mocker: MockerFixture) -> None:
    geval_cls = mocker.patch.object(outcome_validity, "GEval")
    mocker.patch.object(outcome_validity, "load_outcome_criteria", return_value="CRIT")
    model = MagicMock()

    outcome_validity.build_outcome_validity_metric(model)

    kwargs = geval_cls.call_args.kwargs
    assert kwargs["name"] == "OutcomeValidity"
    assert kwargs["criteria"] == "CRIT"
    assert kwargs["threshold"] == GEVAL_PASS_THRESHOLD
    assert kwargs["model"] is model


def test_build_tool_invocation_metric_applies_threshold(mocker: MockerFixture) -> None:
    geval_cls = mocker.patch.object(tool_invocation, "GEval")
    mocker.patch.object(tool_invocation, "load_tool_criteria", return_value="CRIT")
    model = MagicMock()

    tool_invocation.build_tool_invocation_metric(model)

    kwargs = geval_cls.call_args.kwargs
    assert kwargs["name"] == "ToolInvocation"
    assert kwargs["threshold"] == TOOL_INVOCATION_THRESHOLD
    assert kwargs["model"] is model


def test_build_outcome_validity_metric_includes_expected_output(mocker: MockerFixture) -> None:
    mocker.patch.object(outcome_validity, "load_outcome_criteria", return_value="CRIT")

    metric = outcome_validity.build_outcome_validity_metric(_StubJudgeModel())

    assert SingleTurnParams.INPUT in metric.evaluation_params
    assert SingleTurnParams.ACTUAL_OUTPUT in metric.evaluation_params
    assert SingleTurnParams.EXPECTED_OUTPUT in metric.evaluation_params


def test_build_tool_invocation_metric_includes_expected_output(mocker: MockerFixture) -> None:
    mocker.patch.object(tool_invocation, "load_tool_criteria", return_value="CRIT")

    metric = tool_invocation.build_tool_invocation_metric(_StubJudgeModel())

    assert SingleTurnParams.INPUT in metric.evaluation_params
    assert SingleTurnParams.ACTUAL_OUTPUT in metric.evaluation_params
    assert SingleTurnParams.EXPECTED_OUTPUT in metric.evaluation_params


def test_outcome_validity_judge_text_includes_expected_output(mocker: MockerFixture) -> None:
    mocker.patch.object(outcome_validity, "load_outcome_criteria", return_value="CRIT")
    metric = outcome_validity.build_outcome_validity_metric(_StubJudgeModel())
    case = LLMTestCase(
        input="do the task",
        actual_output="did something",
        expected_output="UNIQUE_EXPECTED_OUTPUT_MARKER",
    )

    judge_text = construct_test_case_string(metric.evaluation_params, case)

    assert "UNIQUE_EXPECTED_OUTPUT_MARKER" in judge_text
