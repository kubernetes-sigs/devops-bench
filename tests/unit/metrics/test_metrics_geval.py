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

"""Tests for the model-agnostic DeepEval judge."""

from __future__ import annotations

import logging
import os
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_mock import MockerFixture

from devops_bench.metrics import geval
from devops_bench.metrics.geval import ModelLayerJudge, describe_judge


def _fake_client(text="judged", model_name="judge-model", provider=None):
    client = MagicMock()
    client.model_name = model_name
    client.provider = provider  # get_model stamps this on a real client
    client.generate_content = AsyncMock(return_value="raw-response")
    client.get_text_content = MagicMock(return_value=text)
    return client


def test_wraps_supplied_client_without_get_model(mocker):
    get_model = mocker.patch.object(geval, "get_model")
    client = _fake_client()

    judge = ModelLayerJudge(client=client, model_name="explicit")

    get_model.assert_not_called()
    assert judge.load_model() is client
    assert judge.get_model_name() == "explicit"


def test_warns_and_records_provider_when_judge_model_unset(
    mocker: MockerFixture, caplog: pytest.LogCaptureFixture
) -> None:
    built = _fake_client(model_name="arm-model", provider="anthropic")
    mocker.patch.object(geval, "get_model", return_value=built)
    mocker.patch.dict(
        os.environ, {"AGENT_PROVIDER": "anthropic", "AGENT_MODEL": "arm-model"}, clear=True
    )

    with caplog.at_level(logging.WARNING):
        judge = ModelLayerJudge()

    assert "JUDGE_MODEL unset; the judge resolved to anthropic/arm-model" in caplog.text
    assert "(agent: anthropic/arm-model)" in caplog.text
    assert describe_judge(judge) == {"provider": "anthropic", "model": "arm-model"}


def test_warning_names_a_cross_provider_fallback(
    mocker: MockerFixture, caplog: pytest.LogCaptureFixture
) -> None:
    """JUDGE_PROVIDER set, JUDGE_MODEL unset: the other provider gets the agent's model id."""
    built = _fake_client(model_name="gemini-x", provider="ollama")
    mocker.patch.object(geval, "get_model", return_value=built)
    mocker.patch.dict(
        os.environ,
        {"JUDGE_PROVIDER": "ollama", "AGENT_PROVIDER": "google", "AGENT_MODEL": "gemini-x"},
        clear=True,
    )

    with caplog.at_level(logging.WARNING):
        ModelLayerJudge()

    assert "the judge resolved to ollama/gemini-x (agent: google/gemini-x)" in caplog.text


def test_supplied_client_provider_alias_is_canonicalized(mocker: MockerFixture) -> None:
    get_model = mocker.patch.object(geval, "get_model")

    judge = ModelLayerJudge(client=_fake_client(), provider="gemini")

    get_model.assert_not_called()
    assert judge.provider == "google"
    assert describe_judge(judge)["provider"] == "google"


def test_describe_judge_never_reports_the_placeholder_name() -> None:
    judge = ModelLayerJudge(client=_fake_client(model_name=None))

    assert judge.get_model_name() == "judge"
    assert describe_judge(judge) == {"provider": None, "model": None}


def test_describe_judge_uses_judge_env_when_set(mocker: MockerFixture) -> None:
    built = _fake_client(model_name="judge-x", provider="google")
    mocker.patch.object(geval, "get_model", return_value=built)
    mocker.patch.dict(
        os.environ,
        {"AGENT_PROVIDER": "anthropic", "JUDGE_PROVIDER": "gemini", "JUDGE_MODEL": "judge-x"},
        clear=True,
    )

    assert describe_judge(ModelLayerJudge()) == {"provider": "google", "model": "judge-x"}


def test_describe_judge_tolerates_a_foreign_judge() -> None:
    assert describe_judge(object()) == {"provider": None, "model": None}


def test_builds_client_from_config(mocker):
    get_model = mocker.patch.object(geval, "get_model")
    client = _fake_client(model_name="from-adapter")
    get_model.return_value = client
    mocker.patch.dict(
        os.environ,
        {"JUDGE_PROVIDER": "anthropic", "JUDGE_MODEL": "claude-test"},
        clear=True,
    )

    judge = ModelLayerJudge()

    get_model.assert_called_once_with(provider="anthropic", model_name="claude-test")
    assert judge.get_model_name() == "claude-test"


def test_model_name_falls_back_to_client(mocker):
    mocker.patch.object(geval, "get_model")
    client = _fake_client(model_name="adapter-default")

    judge = ModelLayerJudge(client=client)

    assert judge.get_model_name() == "adapter-default"


def test_a_generate_uses_text_only_call():
    client = _fake_client(text="the verdict")
    judge = ModelLayerJudge(client=client)

    import asyncio

    out = asyncio.run(judge.a_generate("score this"))

    assert out == "the verdict"
    client.generate_content.assert_awaited_once_with(
        contents=[{"role": "user", "content": "score this"}],
        tools=None,
        system_instruction=None,
    )


def test_a_generate_returns_empty_string_for_none():
    client = _fake_client()
    client.get_text_content.return_value = None
    judge = ModelLayerJudge(client=client)

    import asyncio

    assert asyncio.run(judge.a_generate("x")) == ""


def test_generate_runs_async():
    client = _fake_client(text="sync result")
    judge = ModelLayerJudge(client=client)

    assert judge.generate("prompt") == "sync result"


def test_generate_is_loop_aware():
    # When a loop is already running, generate() must not call asyncio.run()
    # re-entrantly; it offloads to a worker thread and still returns the text.
    import asyncio

    client = _fake_client(text="loop-safe result")
    judge = ModelLayerJudge(client=client)

    async def _call_from_running_loop():
        return judge.generate("prompt")

    assert asyncio.run(_call_from_running_loop()) == "loop-safe result"


def test_get_judge_model_passes_through(mocker):
    get_model = mocker.patch.object(geval, "get_model")
    get_model.return_value = _fake_client(model_name="gm")

    judge = geval.get_judge_model(provider="ollama", model_name="gemma")

    get_model.assert_called_once_with(provider="ollama", model_name="gemma")
    assert judge.get_model_name() == "gemma"
