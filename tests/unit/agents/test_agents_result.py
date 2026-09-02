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

"""Unit tests for devops_bench.agents.result."""

import pytest

from devops_bench.agents.result import TERMINAL_REASONS, AgentResult, ToolCall


def test_tool_call_to_dict_round_trip() -> None:
    call = ToolCall(name="k", args={"a": 1}, result="ok", status="completed")
    assert call.to_dict() == {
        "name": "k",
        "args": {"a": 1},
        "result": "ok",
        "status": "completed",
    }


def test_tool_call_defaults() -> None:
    call = ToolCall(name="x", args={})
    assert call.result is None
    assert call.status == "called"


def test_agent_result_defaults_to_empty_collections() -> None:
    result = AgentResult(output="hi", trajectory=[])
    assert result.tokens == {}
    assert result.errors == []
    assert result.metadata == {}
    assert result.latency == 0.0
    assert not result.has_errors()
    # Unreported, not "finished cleanly" — a harness that has not been taught
    # to set this must not be read as having observed a clean ending.
    assert result.terminal_reason == ""


def test_agent_result_to_dict_is_serializable_copies() -> None:
    trajectory = [ToolCall(name="t", args={}).to_dict()]
    result = AgentResult(
        output="done",
        trajectory=trajectory,
        tokens={"prompt": 1},
        latency=2.5,
        errors=["x"],
        terminal_reason="completed",
        metadata={"k": 1},
    )
    out = result.to_dict()
    assert out == {
        "output": "done",
        "trajectory": trajectory,
        "tokens": {"prompt": 1},
        "latency": 2.5,
        "errors": ["x"],
        "terminal_reason": "completed",
        "tool_wait_sec": None,
        "served_models": [],
        "model_turns": None,
        "metadata": {"k": 1},
    }
    # to_dict must hand the harness fresh containers so mutating the snapshot
    # never bleeds back into the result it came from.
    out["trajectory"].append({"name": "extra", "args": {}, "result": None, "status": "called"})
    out["tokens"]["mutated"] = True
    out["errors"].append("mutated")
    out["metadata"]["mutated"] = True
    assert result.trajectory == trajectory
    assert result.tokens == {"prompt": 1}
    assert result.errors == ["x"]
    assert result.metadata == {"k": 1}


def test_agent_result_errored_classmethod_populates_errors() -> None:
    result = AgentResult.errored("boom", latency=1.25)
    assert result.output == "Error: boom"
    assert result.trajectory == []
    assert result.errors == ["boom"]
    assert result.latency == 1.25
    assert result.has_errors()
    assert result.terminal_reason == "error"


def test_agent_result_errored_can_report_a_timeout_instead() -> None:
    """A run the harness cut off at its budget is not the agent failing.

    Both populate ``errors``, so ``errors`` alone cannot tell a slow model from
    a broken one.
    """
    result = AgentResult.errored("timed out after 600s", terminal_reason="timeout")
    assert result.terminal_reason == "timeout"
    assert result.has_errors()


def test_terminal_reasons_are_the_documented_set() -> None:
    """Pinned so a harness cannot invent a value the dashboard will not group."""
    assert TERMINAL_REASONS == ("", "completed", "timeout", "error")


@pytest.mark.parametrize("reason", TERMINAL_REASONS)
def test_agent_result_accepts_every_documented_terminal_reason(reason: str) -> None:
    assert AgentResult(output="", trajectory=[], terminal_reason=reason).terminal_reason == reason


def test_agent_result_rejects_an_unknown_terminal_reason() -> None:
    """A value outside the set reaches the dashboard and groups under nothing."""
    with pytest.raises(ValueError, match="terminal_reason"):
        AgentResult(output="", trajectory=[], terminal_reason="cancelled")


def test_agent_result_has_errors_is_false_on_clean_run() -> None:
    result = AgentResult(output="ok", trajectory=[])
    assert result.has_errors() is False
    result.errors.append("late")
    assert result.has_errors() is True
