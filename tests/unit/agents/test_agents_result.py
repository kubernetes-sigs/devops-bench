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

from devops_bench.agents.result import ROOT_ACTOR, AgentResult, ToolCall, scoped_actor


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
    assert call.actor is None
    assert call.call_id is None
    assert call.parent_id is None


def test_tool_call_omits_unset_attribution_fields() -> None:
    """A single-agent harness serializes exactly the four original keys.

    The trajectory is re-serialized into the judge's prompt downstream, so a key
    written as ``None`` on every entry would perturb the scores of every run that
    has no fleet to attribute.
    """
    assert ToolCall(name="x", args={}).to_dict().keys() == {"name", "args", "result", "status"}


def test_tool_call_carries_set_attribution_fields() -> None:
    call = ToolCall(name="x", args={}, actor="cluster", call_id="c1", parent_id="p1")
    assert call.to_dict() == {
        "name": "x",
        "args": {},
        "result": None,
        "status": "called",
        "actor": "cluster",
        "call_id": "c1",
        "parent_id": "p1",
    }


def test_tool_call_omits_only_the_attribution_fields_left_unset() -> None:
    """A top-level call in a delegated run: labeled, but with no parent to name."""
    entry = ToolCall(name="x", args={}, actor=ROOT_ACTOR, call_id="c1").to_dict()
    assert entry["actor"] == "root"
    assert "parent_id" not in entry


def test_agent_result_defaults_to_empty_collections() -> None:
    result = AgentResult(output="hi", trajectory=[])
    assert result.tokens == {}
    assert result.errors == []
    assert result.metadata == {}
    assert result.latency == 0.0
    assert not result.has_errors()


def test_agent_result_to_dict_is_serializable_copies() -> None:
    trajectory = [ToolCall(name="t", args={}).to_dict()]
    result = AgentResult(
        output="done",
        trajectory=trajectory,
        tokens={"prompt": 1},
        latency=2.5,
        errors=["x"],
        metadata={"k": 1},
    )
    out = result.to_dict()
    assert out == {
        "output": "done",
        "trajectory": trajectory,
        "tokens": {"prompt": 1},
        "latency": 2.5,
        "errors": ["x"],
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


def test_agent_result_has_errors_is_false_on_clean_run() -> None:
    result = AgentResult(output="ok", trajectory=[])
    assert result.has_errors() is False
    result.errors.append("late")
    assert result.has_errors() is True


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        # Ordinary names pass through untouched, which is every name any
        # recorded run has produced: no existing trajectory serializes
        # differently because the reserved namespace exists.
        ("triage_agent", "triage_agent"),
        ("subagent", "subagent"),
        ("subagent-", "subagent-"),
        ("subagent-1a", "subagent-1a"),
        ("rooted", "rooted"),
        # Reserved: ``root`` is the top-level agent and ``subagent-N`` is the
        # N-th delegate the harness could not name. An outside label claiming
        # either would merge two agents under one name.
        ("root", "subagent-root"),
        ("subagent-1", "subagent-subagent-1"),
        ("subagent-42", "subagent-subagent-42"),
    ],
)
def test_scoped_actor_moves_only_reserved_labels(label: str, expected: str) -> None:
    assert scoped_actor(label) == expected


def test_scoped_actor_lands_outside_the_reserved_namespace_in_one_pass() -> None:
    """The remap is a fixed point, so a double application cannot drift.

    If ``subagent-root`` were itself reserved, a parser that scoped a label
    twice would produce a different one each time and two sightings of the same
    delegate would not match.
    """
    for label in (ROOT_ACTOR, "subagent-1"):
        once = scoped_actor(label)
        assert scoped_actor(once) == once
