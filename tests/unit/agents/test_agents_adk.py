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

"""Unit tests for devops_bench.agents.adk.

The event fixtures below are verbatim ``Event.model_dump(mode="json")`` output
captured from a real ADK run driven by a stub model, so the parser is exercised
against the shape the SDK actually emits rather than an idealized one.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import importlib.util
import os
import pathlib
import subprocess
import sys
import textwrap
from collections.abc import AsyncIterator
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from devops_bench import core
from devops_bench.agents import base, capabilities
from devops_bench.agents import config as agents_config
from devops_bench.agents.adk import agent as adk_mod
from devops_bench.agents.adk import parsing

# The dev group deliberately omits the ``adk`` extra, so every test that reaches
# the SDK carries this marker. It is a marker rather than a module-level
# ``importorskip`` because the parser and the preparation step are SDK-free by
# design — skipping the whole module would stop proving that.
requires_adk = pytest.mark.skipif(
    importlib.util.find_spec("google.adk") is None,
    reason="the optional 'adk' extra is not installed",
)

# --------------------------------------------------------------------------
# Recorded event fixtures
# --------------------------------------------------------------------------

CALL_EVENT = {
    "content": {
        "parts": [
            {
                "function_call": {
                    "id": "adk-ab74b8d2",
                    "args": {"name": "web", "replicas": 3},
                    "name": "scale_deployment",
                }
            }
        ],
        "role": "model",
    },
    "usage_metadata": {
        "candidates_token_count": 10,
        "prompt_token_count": 100,
        "total_token_count": 110,
    },
    "invocation_id": "e-6a3b9866",
    "author": "spike_agent",
    "id": "c50884c8",
}

RESPONSE_EVENT = {
    "content": {
        "parts": [
            {
                "function_response": {
                    "id": "adk-ab74b8d2",
                    "name": "scale_deployment",
                    "response": {"scaled": "web", "replicas": 3},
                }
            }
        ],
        "role": "user",
    },
    "author": "spike_agent",
    "id": "89cd9467",
}

FINAL_EVENT = {
    "content": {"parts": [{"text": "Scaled web to 3 replicas."}], "role": "model"},
    "usage_metadata": {
        "candidates_token_count": 20,
        "prompt_token_count": 200,
        "total_token_count": 220,
    },
    "author": "spike_agent",
    "id": "817831b8",
}

MCP_RESPONSE_EVENT = {
    "content": {
        "parts": [
            {
                "function_response": {
                    "id": "adk-2637655b",
                    "name": "cluster_status",
                    "response": {
                        "content": [{"type": "text", "text": "cluster prod-1 is HEALTHY"}],
                        "structuredContent": {"result": "cluster prod-1 is HEALTHY"},
                        "isError": False,
                    },
                }
            }
        ],
        "role": "user",
    },
    "author": "mcp_spike",
    "id": "aa11",
}

MCP_CALL_EVENT = {
    "content": {
        "parts": [
            {
                "function_call": {
                    "id": "adk-2637655b",
                    "args": {"name": "prod-1"},
                    "name": "cluster_status",
                }
            }
        ],
        "role": "model",
    },
    "author": "mcp_spike",
    "id": "bb22",
}

# Shaped after a ``RemoteA2aAgent`` driven against a real A2A gRPC server, with
# the payload text replaced by neutral stand-ins. Note that ``content.parts``
# mirrors the *trailing artifact* while the answer sits in ``status.message`` —
# the two disagree, which is the point of the fixture.
A2A_EVENT: dict[str, Any] = {
    "content": {"parts": [{"text": "node-1 mem = 0.97"}], "role": "model"},
    "custom_metadata": {
        "a2a:task_id": "494405c7-b9be-441f-beae-be07ba49b5b7",
        "a2a:context_id": "64c847ad-f70e-42f2-b24e-2b88e3550780",
        "a2a:request": {
            "messageId": "6bbd7789-6c50-49bf-a713-6f83a37f4f58",
            "role": "ROLE_USER",
            "parts": [{"text": "Diagnose the evicted pod", "metadata": {"is_user_input": True}}],
        },
        "a2a:response": {
            "id": "494405c7-b9be-441f-beae-be07ba49b5b7",
            "contextId": "64c847ad-f70e-42f2-b24e-2b88e3550780",
            "status": {
                "state": "TASK_STATE_COMPLETED",
                "message": {
                    "messageId": "587e9b357eb547629ef6d675a01d60b5",
                    "role": "ROLE_AGENT",
                    "parts": [{"text": "RCA: node memory pressure evicted the pod."}],
                },
                "timestamp": "2026-09-11T16:47:45.085206Z",
            },
            "artifacts": [
                {
                    "artifactId": "5d57761d-7b73-4100-8457-d26419e3b0a8",
                    "name": "triage_agent",
                    "parts": [{"text": "matched skill k8s-node-pressure"}],
                    "metadata": {"sub_agent": "triage_agent"},
                },
                {
                    "artifactId": "c829905b-dccf-475f-8e22-f4368ee8fca9",
                    "name": "diagnostic_agent",
                    "parts": [{"text": "node-1 mem = 0.97"}],
                    "metadata": {"sub_agent": "diagnostic_agent"},
                },
            ],
        },
    },
    "invocation_id": "e-efdd3f9c",
    "author": "triage_remote",
    "id": "0a2db279",
}


# --------------------------------------------------------------------------
# parse_event_stream
# --------------------------------------------------------------------------


def test_parse_event_stream_folds_call_and_response() -> None:
    output, trajectory, tokens, errors = parsing.parse_event_stream(
        [CALL_EVENT, RESPONSE_EVENT, FINAL_EVENT]
    )

    assert output == "Scaled web to 3 replicas."
    assert errors == []
    assert trajectory == [
        {
            "name": "scale_deployment",
            "args": {"name": "web", "replicas": 3},
            "result": '{"replicas": 3, "scaled": "web"}',
            "status": "completed",
        }
    ]
    # Usage is per LLM call, so the run total is the sum of both blocks.
    assert tokens["input"] == 300
    assert tokens["output"] == 30
    assert tokens["total"] == 330
    assert tokens["cached"] is None
    assert tokens["cache_write"] is None


def test_parse_event_stream_reads_mcp_result_text_and_success() -> None:
    _, trajectory, _, errors = parsing.parse_event_stream([MCP_CALL_EVENT, MCP_RESPONSE_EVENT])

    assert errors == []
    assert trajectory[0]["result"] == "cluster prod-1 is HEALTHY"
    assert trajectory[0]["status"] == "completed"


def test_parse_event_stream_marks_mcp_is_error_as_failed() -> None:
    failed = copy.deepcopy(MCP_RESPONSE_EVENT)
    response = failed["content"]["parts"][0]["function_response"]["response"]
    response["isError"] = True
    response["content"] = [{"type": "text", "text": "permission denied"}]

    _, trajectory, _, _ = parsing.parse_event_stream([MCP_CALL_EVENT, failed])

    assert trajectory[0]["status"] == "error"
    assert trajectory[0]["result"] == "permission denied"


def test_parse_event_stream_marks_adk_error_payload_as_failed() -> None:
    failed = copy.deepcopy(RESPONSE_EVENT)
    failed["content"]["parts"][0]["function_response"]["response"] = {"error": "boom"}

    _, trajectory, _, _ = parsing.parse_event_stream([CALL_EVENT, failed])

    assert trajectory[0]["status"] == "error"


def test_parse_event_stream_keeps_unanswered_calls_as_called() -> None:
    parallel = {
        "content": {
            "parts": [
                {"function_call": {"id": "a1", "name": "ok_tool", "args": {"x": 2}}},
                {"function_call": {"id": "a2", "name": "boom_tool", "args": {"x": 9}}},
            ],
            "role": "model",
        },
        "author": "err_spike",
    }
    # ADK yields an error event and *then* raises, so both shapes must survive.
    error_event = {"error_code": "RuntimeError", "error_message": "kaboom on 9"}

    output, trajectory, _, errors = parsing.parse_event_stream([parallel, error_event])

    assert [entry["status"] for entry in trajectory] == ["called", "called"]
    assert [entry["result"] for entry in trajectory] == [None, None]
    assert output == ""
    assert errors == ["event 1 reported RuntimeError: kaboom on 9"]


def test_parse_event_stream_reports_orphan_tool_response() -> None:
    _, trajectory, _, errors = parsing.parse_event_stream([RESPONSE_EVENT])

    assert trajectory == []
    assert len(errors) == 1
    assert "matched no pending call" in errors[0]


def test_parse_event_stream_pairs_id_less_calls_in_order() -> None:
    call_a = {"content": {"role": "model", "parts": [{"function_call": {"name": "a", "args": {}}}]}}
    call_b = {"content": {"role": "model", "parts": [{"function_call": {"name": "b", "args": {}}}]}}
    resp_a = {"content": {"role": "user", "parts": [{"function_response": {"response": "ra"}}]}}
    resp_b = {"content": {"role": "user", "parts": [{"function_response": {"response": "rb"}}]}}

    _, trajectory, _, errors = parsing.parse_event_stream([call_a, call_b, resp_a, resp_b])

    assert errors == []
    assert [(e["name"], e["result"]) for e in trajectory] == [("a", "ra"), ("b", "rb")]


def test_parse_event_stream_skips_partial_and_thought_text() -> None:
    events = [
        {"content": {"role": "model", "parts": [{"text": "Scal"}]}, "partial": True},
        {"content": {"role": "model", "parts": [{"text": "thinking...", "thought": True}]}},
        {"content": {"role": "user", "parts": [{"text": "the original prompt"}]}},
        {"content": {"role": "model", "parts": [{"text": "Scaled."}]}},
    ]

    output, _, _, errors = parsing.parse_event_stream(events)

    assert output == "Scaled."
    assert errors == []


def test_parse_event_stream_reports_non_mapping_event() -> None:
    _, _, _, errors = parsing.parse_event_stream(["not an event"])

    assert errors == ["event 0: unexpected type str"]


def test_parse_event_stream_reports_unavailable_tokens_as_none() -> None:
    _, _, tokens, _ = parsing.parse_event_stream([MCP_CALL_EVENT, MCP_RESPONSE_EVENT])

    assert set(tokens.values()) == {None}


def test_parse_event_stream_subtracts_cached_from_input() -> None:
    event = {
        "usage_metadata": {
            "prompt_token_count": 100,
            "cached_content_token_count": 40,
            "candidates_token_count": 5,
            "thoughts_token_count": 7,
            "total_token_count": 112,
        }
    }

    _, _, tokens, _ = parsing.parse_event_stream([event])

    assert tokens == {
        "input": 60,
        "cached": 40,
        "cache_write": None,
        "reasoning": 7,
        "output": 5,
        "total": 112,
    }


def test_parse_event_stream_clamps_over_reported_cache_read() -> None:
    event = {
        "usage_metadata": {"prompt_token_count": 10, "cached_content_token_count": 40},
    }

    _, _, tokens, _ = parsing.parse_event_stream([event])

    assert tokens["input"] == 0


def test_parse_event_stream_prefers_the_a2a_status_message() -> None:
    output, trajectory, _, errors = parsing.parse_event_stream([A2A_EVENT])

    assert output == "RCA: node memory pressure evicted the pod."
    assert [entry["name"] for entry in trajectory] == ["triage_agent", "diagnostic_agent"]
    assert errors == []


# --------------------------------------------------------------------------
# parse_event_stream: A2A sub-agent attribution
# --------------------------------------------------------------------------


def test_parse_event_stream_attributes_an_artifact_to_its_sub_agent() -> None:
    """Each tagged artifact is one sub-agent's contribution, kept in order."""
    _, trajectory, _, _ = parsing.parse_event_stream([A2A_EVENT])

    assert trajectory == [
        {
            "name": "triage_agent",
            "args": {},
            "result": "matched skill k8s-node-pressure",
            "status": "completed",
            "actor": "triage_agent",
        },
        {
            "name": "diagnostic_agent",
            "args": {},
            "result": "node-1 mem = 0.97",
            "status": "completed",
            "actor": "diagnostic_agent",
        },
    ]


def test_parse_event_stream_leaves_a_self_tagged_artifact_unattributed() -> None:
    """One agent reporting its own work is not a delegation.

    Tagging is not evidence of a fleet — a single-agent remote can still label
    what it produced. Stamping ``actor`` here would move the score of every
    such run to say nothing.
    """
    event = copy.deepcopy(A2A_EVENT)
    artifacts = event["custom_metadata"]["a2a:response"]["artifacts"]
    del artifacts[1]
    artifacts[0]["metadata"]["sub_agent"] = event["author"]

    _, trajectory, _, _ = parsing.parse_event_stream([event])

    assert len(trajectory) == 1
    assert trajectory[0].keys() == {"name", "args", "result", "status"}


def test_parse_event_stream_stamps_root_on_the_remotes_own_call() -> None:
    """Once attribution is on, every entry carries an actor — including root's."""
    event = copy.deepcopy(A2A_EVENT)
    event["content"]["parts"].append(
        {"function_call": {"id": "c1", "name": "fetch_card", "args": {}}}
    )

    _, trajectory, _, _ = parsing.parse_event_stream([event])

    assert [(entry["name"], entry["actor"]) for entry in trajectory] == [
        ("triage_agent", "triage_agent"),
        ("diagnostic_agent", "diagnostic_agent"),
        ("fetch_card", "root"),
    ]


def test_parse_event_stream_calls_the_remotes_own_artifact_root_when_it_delegated() -> None:
    """One agent must not end a run under two names.

    A remote can tag an artifact with its own name *and* delegate the rest. The
    self-tagged artifact is the remote's own work, so it resolves to ``root``
    like the remote's ordinary calls do — labelling it with the author's name
    would leave the judge reading one agent as two.
    """
    event = copy.deepcopy(A2A_EVENT)
    artifacts = event["custom_metadata"]["a2a:response"]["artifacts"]
    artifacts[0]["metadata"]["sub_agent"] = event["author"]

    _, trajectory, _, _ = parsing.parse_event_stream([event])

    assert [(entry["name"], entry["actor"]) for entry in trajectory] == [
        ("triage_remote", "root"),
        ("diagnostic_agent", "diagnostic_agent"),
    ]


def test_parse_event_stream_folds_a_repeated_artifact_once() -> None:
    """ADK re-emits a task as it progresses; its fleet ran once, not twice.

    The ``working`` snapshot already carries the artifacts produced so far, and
    the ``completed`` one repeats them. Folding both unguarded doubles every
    sub-agent, which corrupts the two questions the trajectory is read for:
    which sub-agents ran, and in what order.
    """
    working = copy.deepcopy(A2A_EVENT)
    working["custom_metadata"]["a2a:response"]["status"] = {"state": "TASK_STATE_WORKING"}

    _, trajectory, _, _ = parsing.parse_event_stream([working, A2A_EVENT])

    assert [entry["name"] for entry in trajectory] == ["triage_agent", "diagnostic_agent"]


def test_parse_event_stream_keeps_attribution_when_only_repeats_remain() -> None:
    """A snapshot that folds nothing new must not retract the run's attribution.

    The final snapshot repeats artifacts the earlier one already folded, so it
    appends no entry. Deciding delegation from what a snapshot *appended* rather
    than from what it *named* would drop every ``actor`` on exactly the runs
    that have a fleet to describe.
    """
    working = copy.deepcopy(A2A_EVENT)
    working["custom_metadata"]["a2a:response"]["status"] = {"state": "TASK_STATE_WORKING"}

    _, trajectory, _, _ = parsing.parse_event_stream([working, A2A_EVENT])

    assert [entry["actor"] for entry in trajectory] == ["triage_agent", "diagnostic_agent"]


def test_parse_event_stream_keeps_two_remote_tasks_sharing_an_artifact_id_apart() -> None:
    """Deduplication is scoped per task, so two remotes never erase each other."""
    first = copy.deepcopy(A2A_EVENT)
    second = copy.deepcopy(A2A_EVENT)
    second["custom_metadata"]["a2a:response"]["id"] = "a-different-task"

    _, trajectory, _, _ = parsing.parse_event_stream([first, second])

    assert [entry["name"] for entry in trajectory] == [
        "triage_agent",
        "diagnostic_agent",
        "triage_agent",
        "diagnostic_agent",
    ]


def test_parse_event_stream_dedupes_an_artifact_carrying_no_id() -> None:
    """``artifactId`` is optional in A2A; position is the fallback identity."""
    working = copy.deepcopy(A2A_EVENT)
    working["custom_metadata"]["a2a:response"]["status"] = {"state": "TASK_STATE_WORKING"}
    completed = copy.deepcopy(A2A_EVENT)
    for event in (working, completed):
        for artifact in event["custom_metadata"]["a2a:response"]["artifacts"]:
            del artifact["artifactId"]

    _, trajectory, _, _ = parsing.parse_event_stream([working, completed])

    assert [entry["name"] for entry in trajectory] == ["triage_agent", "diagnostic_agent"]


def test_parse_event_stream_folds_an_artifact_a_later_snapshot_added() -> None:
    """Deduplication must not swallow a sub-agent that ran after the first snapshot."""
    working = copy.deepcopy(A2A_EVENT)
    working["custom_metadata"]["a2a:response"]["status"] = {"state": "TASK_STATE_WORKING"}
    del working["custom_metadata"]["a2a:response"]["artifacts"][1]

    _, trajectory, _, _ = parsing.parse_event_stream([working, A2A_EVENT])

    assert [entry["name"] for entry in trajectory] == ["triage_agent", "diagnostic_agent"]


def test_parse_event_stream_falls_back_to_the_artifact_name_when_untagged() -> None:
    """``metadata`` is a convention a remote opts into; ``name`` is the backstop."""
    event = copy.deepcopy(A2A_EVENT)
    del event["custom_metadata"]["a2a:response"]["artifacts"][0]["metadata"]

    _, trajectory, _, _ = parsing.parse_event_stream([event])

    assert trajectory[0]["actor"] == "triage_agent"


def test_parse_event_stream_skips_an_artifact_naming_no_producer() -> None:
    """An artifact with neither tag nor name attributes nothing, so it is dropped.

    Inventing a placeholder producer would assert a sub-agent that was never
    reported; the remaining artifacts still attribute normally.
    """
    event = copy.deepcopy(A2A_EVENT)
    artifact = event["custom_metadata"]["a2a:response"]["artifacts"][0]
    del artifact["metadata"]
    del artifact["name"]

    _, trajectory, _, _ = parsing.parse_event_stream([event])

    assert [entry["name"] for entry in trajectory] == ["diagnostic_agent"]


def test_parse_event_stream_keeps_a_failed_tasks_sub_agents_in_the_trajectory() -> None:
    """Which sub-agents ran before a failure is the point of inspecting one.

    The artifacts stay out of ``output`` — that is what makes a failure ungraded
    — but suppressing them from the trajectory too would erase the diagnostic.
    """
    event = copy.deepcopy(A2A_EVENT)
    event["custom_metadata"]["a2a:response"]["status"]["state"] = "TASK_STATE_FAILED"

    output, trajectory, _, errors = parsing.parse_event_stream([event])

    assert output == ""
    assert errors == ["event 0: remote A2A task failed"]
    assert [entry["actor"] for entry in trajectory] == ["triage_agent", "diagnostic_agent"]


def test_parse_event_stream_omits_attribution_on_a_run_with_no_artifacts() -> None:
    """A local run serializes exactly as it did before attribution existed."""
    event = copy.deepcopy(A2A_EVENT)
    del event["custom_metadata"]

    _, trajectory, _, _ = parsing.parse_event_stream([event, CALL_EVENT, RESPONSE_EVENT])

    assert trajectory
    for entry in trajectory:
        assert entry.keys() == {"name", "args", "result", "status"}


def test_parse_event_stream_reports_a_failed_a2a_task() -> None:
    """A failure notice is an error, not the answer.

    The record is written as ``status: "success"`` whatever is in ``errors``,
    and only ``status: "failed"`` records are skipped when scoring, so anything
    left in ``output`` here is graded as the agent's response.
    """
    event = copy.deepcopy(A2A_EVENT)
    status = event["custom_metadata"]["a2a:response"]["status"]
    status["state"] = "TASK_STATE_FAILED"
    status["message"]["parts"] = [{"text": "the metrics backend is unreachable"}]

    output, _, _, errors = parsing.parse_event_stream([event])

    assert output == ""
    assert errors == ["event 0: remote A2A task failed"]


@pytest.mark.parametrize("state", ["TASK_STATE_FAILED", "TASK_STATE_CANCELED", "rejected"])
def test_parse_event_stream_keeps_a_failed_tasks_artifact_out_of_output(state: str) -> None:
    """Nor does the event's own text stand in for the answer a failure lacks.

    Suppressing only the status message would fall straight back to
    ``content.parts`` — the trailing-artifact mirror this whole path exists to
    keep out of the graded output.
    """
    event = copy.deepcopy(A2A_EVENT)
    del event["custom_metadata"]["a2a:response"]["status"]["message"]
    event["custom_metadata"]["a2a:response"]["status"]["state"] = state

    output, _, _, errors = parsing.parse_event_stream([event])

    assert output == ""
    assert errors == [f"event 0: remote A2A task {state.lower().removeprefix('task_state_')}"]


def test_parse_event_stream_falls_back_to_content_on_a_completed_task_with_no_message() -> None:
    """A completed task need not carry a status message; the artifact is all there is.

    Unlike a failure, a completed task did produce something, so the fallback
    that serves a non-terminal state serves this one too.
    """
    event = copy.deepcopy(A2A_EVENT)
    del event["custom_metadata"]["a2a:response"]["status"]["message"]

    output, _, _, errors = parsing.parse_event_stream([event])

    assert output == "node-1 mem = 0.97"
    assert errors == []


def test_parse_event_stream_accepts_a_lowercase_a2a_state() -> None:
    """The pydantic A2A types spell the enum ``rejected``, the proto ones don't."""
    event = copy.deepcopy(A2A_EVENT)
    event["custom_metadata"]["a2a:response"]["status"]["state"] = "rejected"

    _, _, _, errors = parsing.parse_event_stream([event])

    assert errors == ["event 0: remote A2A task rejected"]


def test_parse_event_stream_falls_back_to_content_without_a_status_message() -> None:
    """A task still working carries no status message; the event text is all there is."""
    event = copy.deepcopy(A2A_EVENT)
    event["custom_metadata"]["a2a:response"]["status"] = {"state": "TASK_STATE_WORKING"}

    output, _, _, errors = parsing.parse_event_stream([event])

    assert output == "node-1 mem = 0.97"
    assert errors == []


def test_parse_event_stream_ignores_a_working_tasks_status_message() -> None:
    """A streaming update's status message is progress text, not the answer.

    ADK emits ``working`` events that carry a ``status.message``. Treating one
    as the answer puts narration ahead of the real answer in the graded output —
    and, because a status message displaces the event's own text, drops that
    event's content as well.
    """
    working = copy.deepcopy(A2A_EVENT)
    working["content"]["parts"] = [{"text": "checking node pressure"}]
    status = working["custom_metadata"]["a2a:response"]["status"]
    status["state"] = "TASK_STATE_WORKING"
    status["message"]["parts"] = [{"text": "Analyzing node pressure..."}]

    output, _, _, errors = parsing.parse_event_stream([working, A2A_EVENT])

    assert "Analyzing node pressure..." not in output
    assert output.endswith("RCA: node memory pressure evicted the pod.")
    assert errors == []


@pytest.mark.parametrize("state", ["TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED"])
def test_parse_event_stream_ignores_a_non_terminal_status_message(state: str) -> None:
    """``working`` is not the only non-final state that carries a message."""
    event = copy.deepcopy(A2A_EVENT)
    status = event["custom_metadata"]["a2a:response"]["status"]
    status["state"] = state
    status["message"]["parts"] = [{"text": "which namespace?"}]

    output, _, _, errors = parsing.parse_event_stream([event])

    assert output == "node-1 mem = 0.97"
    assert errors == []


def test_parse_event_stream_still_folds_tool_calls_on_an_a2a_event() -> None:
    event = copy.deepcopy(A2A_EVENT)
    event["content"]["parts"].append(CALL_EVENT["content"]["parts"][0])

    output, trajectory, _, errors = parsing.parse_event_stream([event, RESPONSE_EVENT])

    assert output == "RCA: node memory pressure evicted the pod."
    assert errors == []
    # The remote's sub-agent artifacts lead; the call the event itself carried
    # still folds with its response, and is the remote's own work.
    assert [(entry["name"], entry["status"]) for entry in trajectory] == [
        ("triage_agent", "completed"),
        ("diagnostic_agent", "completed"),
        ("scale_deployment", "completed"),
    ]
    assert trajectory[-1]["actor"] == "root"


# --------------------------------------------------------------------------
# Target resolution helpers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("my_pkg.agent", False),
        ("my_pkg.agent:root_agent", False),
        ("agent.py", True),
        ("~/agents/mine", True),
        ("./mine", True),
        ("/opt/agents/mine", True),
    ],
)
def test_looks_like_path(spec, expected) -> None:
    assert adk_mod._looks_like_path(spec) is expected


# --------------------------------------------------------------------------
# Preparation (SDK-free: the harness only duck-types the agent object)
# --------------------------------------------------------------------------


class FakeAgent:
    """Stand-in exposing the handful of attributes the harness touches."""

    def __init__(
        self,
        name: str,
        model: str | None = None,
        instruction: object = None,
        tools: list[object] | None = None,
        sub_agents: tuple[object, ...] = (),
    ) -> None:
        self.name = name
        self.model = model
        self.instruction = instruction
        self.tools = list(tools or [])
        self.sub_agents = list(sub_agents)

    def model_copy(self, *, deep: bool = False) -> FakeAgent:
        return copy.deepcopy(self)


def test_prepare_leaves_the_imported_agent_untouched() -> None:
    original = FakeAgent("root", model="baked-in", instruction="be helpful")
    harness = adk_mod.AdkAgent(agents_config.AgentConfig(model="bench-model"))

    prepared, metadata, errors = harness._prepare(original)

    assert errors == []
    assert prepared is not original
    assert original.model == "baked-in"
    assert prepared.model == "bench-model"
    assert metadata["model_override_count"] == 1


def test_prepare_overrides_sub_agent_models_too() -> None:
    root = FakeAgent("root", model="a", sub_agents=[FakeAgent("child", model="b")])
    harness = adk_mod.AdkAgent(agents_config.AgentConfig(model="bench-model"))

    prepared, metadata, _ = harness._prepare(root)

    assert prepared.sub_agents[0].model == "bench-model"
    assert metadata["model_override_count"] == 2


def test_prepare_keeps_the_agents_own_model_when_unset() -> None:
    root = FakeAgent("root", model="baked-in")
    harness = adk_mod.AdkAgent(agents_config.AgentConfig(model=None))

    prepared, metadata, _ = harness._prepare(root)

    assert prepared.model == "baked-in"
    assert "model_override_count" not in metadata


def test_prepare_appends_rules_to_the_instruction() -> None:
    root = FakeAgent("root", instruction="you are an operator")
    harness = adk_mod.AdkAgent(
        agents_config.AgentConfig(
            capabilities=capabilities.AllCapabilities(
                rules=capabilities.AgentRules(text="never delete data")
            )
        )
    )

    prepared, _, errors = harness._prepare(root)

    assert errors == []
    assert prepared.instruction == "you are an operator\n\nnever delete data"


def test_prepare_appends_discovered_skills(tmp_path: pathlib.Path) -> None:
    skill_dir = tmp_path / "skills" / "rollout"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: rollout\ndescription: roll out safely\n---\n\nDrain first.\n",
        encoding="utf-8",
    )
    harness = adk_mod.AdkAgent(
        agents_config.AgentConfig(
            capabilities=capabilities.AllCapabilities(
                skills=capabilities.SkillBinding(paths=(str(tmp_path / "skills"),))
            )
        )
    )

    prepared, metadata, errors = harness._prepare(FakeAgent("root"))

    assert errors == []
    assert metadata["skills"] == ["rollout"]
    assert "# Available skills" in prepared.instruction
    assert "Drain first." in prepared.instruction


def test_prepare_reports_a_callable_instruction_provider() -> None:
    root = FakeAgent("root", instruction=lambda ctx: "dynamic")
    harness = adk_mod.AdkAgent(
        agents_config.AgentConfig(
            capabilities=capabilities.AllCapabilities(
                rules=capabilities.AgentRules(text="never delete data")
            )
        )
    )

    _, _, errors = harness._prepare(root)

    assert len(errors) == 1
    assert "callable instruction provider" in errors[0]


def test_prepare_attaches_one_toolset_per_mcp_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[dict] = []

    class FakeToolset:
        def __init__(self, *, connection_params: object, tool_filter: object = None) -> None:
            built.append({"params": connection_params, "tool_filter": tool_filter})

    def fake_connection(*, server_params: object, timeout: float) -> dict:
        return {"server_params": server_params, "timeout": timeout}

    def fake_server_params(*, command: str, args: list[str]) -> dict:
        return {"command": command, "args": args}

    monkeypatch.setattr(
        adk_mod, "_load_toolset_types", lambda: (FakeToolset, fake_connection, fake_server_params)
    )

    harness = adk_mod.AdkAgent(
        agents_config.AgentConfig(
            capabilities=capabilities.AllCapabilities(
                mcp_servers=(
                    # A binding with no command is one the agent hosts itself.
                    capabilities.McpBinding(name="builtin", command=(), tools=("ignored",)),
                    capabilities.McpBinding(
                        name="tools", command=("uv", "run", "k8s-mcp"), tools=("get_pods",)
                    ),
                )
            )
        )
    )

    prepared, metadata, errors = harness._prepare(FakeAgent("root"))

    assert errors == []
    assert metadata["mcp_toolsets"] == 1
    assert len(prepared.tools) == 1
    assert built[0]["tool_filter"] == ["get_pods"]
    assert built[0]["params"]["server_params"] == {"command": "uv", "args": ["run", "k8s-mcp"]}


class FakeWorkflowAgent:
    """A node with children but no model, instruction, or tools of its own.

    Stands in for ``SequentialAgent`` and friends, which the tree walk has to
    step over rather than assign attributes onto.
    """

    def __init__(self, name: str, sub_agents: tuple[object, ...] = ()) -> None:
        self.name = name
        self.sub_agents = list(sub_agents)

    def model_copy(self, *, deep: bool = False) -> FakeWorkflowAgent:
        return copy.deepcopy(self)


def _stub_toolset_types(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Point ``_load_toolset_types`` at cheap stand-ins and return the built list."""
    built: list[object] = []

    class FakeToolset:
        def __init__(self, *, connection_params: object, tool_filter: object = None) -> None:
            self.tool_filter = tool_filter
            built.append(self)

    monkeypatch.setattr(
        adk_mod,
        "_load_toolset_types",
        lambda: (
            FakeToolset,
            lambda *, server_params, timeout: {"server_params": server_params},
            lambda *, command, args: {"command": command, "args": args},
        ),
    )
    return built


def _mcp_harness(**caps: object) -> adk_mod.AdkAgent:
    return adk_mod.AdkAgent(
        agents_config.AgentConfig(
            capabilities=capabilities.AllCapabilities(
                mcp_servers=(
                    capabilities.McpBinding(
                        name="tools", command=("uv", "run", "k8s-mcp"), tools=("get_pods",)
                    ),
                ),
                **caps,
            )
        )
    )


def test_prepare_attaches_toolsets_to_every_agent_in_the_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every agent that can hold tools gets the run's MCP toolset.

    Regression test for a real run. ADK resolves tools from the *active* agent
    (`LlmAgent.canonical_tools`) with no inheritance from a parent, so attaching
    to the root alone left a delegating tree unable to touch the cluster: the
    sub-agent doing the cluster work reported no tools at all, and the run still
    came back `status: success` with `errors: []` while scoring zero.
    """
    built = _stub_toolset_types(monkeypatch)
    root = FakeAgent(
        "root",
        sub_agents=[
            FakeAgent("operator"),
            FakeAgent("reporter", tools=["write_file"]),
        ],
    )

    prepared, metadata, errors = _mcp_harness()._prepare(root)

    assert errors == []
    assert metadata["mcp_toolsets"] == 1
    assert metadata["mcp_agents"] == 3
    operator, reporter = prepared.sub_agents
    assert prepared.tools == built
    assert operator.tools == built
    # An agent's own tools survive alongside the attached toolset.
    assert reporter.tools == ["write_file", *built]
    # One binding is still one server process, shared across the tree.
    assert len(built) == 1


def test_prepare_delivers_rules_to_every_agent_in_the_tree() -> None:
    """A delegate that never sees the operator brief can violate it."""
    root = FakeAgent("root", instruction="coordinate", sub_agents=[FakeAgent("child")])
    harness = adk_mod.AdkAgent(
        agents_config.AgentConfig(
            capabilities=capabilities.AllCapabilities(
                rules=capabilities.AgentRules(text="never delete a namespace")
            )
        )
    )

    prepared, metadata, errors = harness._prepare(root)

    assert errors == []
    assert metadata["instruction_agents"] == 2
    assert "never delete a namespace" in prepared.instruction
    assert "never delete a namespace" in prepared.sub_agents[0].instruction


def test_prepare_names_only_the_agents_that_refused_the_instruction() -> None:
    root = FakeAgent("root", instruction="coordinate")
    root.sub_agents = [
        FakeAgent("dynamic", instruction=lambda ctx: "computed"),
        FakeAgent("static", instruction="plain"),
    ]
    harness = adk_mod.AdkAgent(
        agents_config.AgentConfig(
            capabilities=capabilities.AllCapabilities(
                rules=capabilities.AgentRules(text="never delete a namespace")
            )
        )
    )

    prepared, metadata, errors = harness._prepare(root)

    assert len(errors) == 1
    assert "dynamic" in errors[0]
    assert "static" not in errors[0]
    # The refusal is per node: the rest of the tree still got the brief.
    assert metadata["instruction_agents"] == 2
    assert "never delete a namespace" in prepared.sub_agents[1].instruction


def test_prepare_steps_over_nodes_that_hold_no_tools_or_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = _stub_toolset_types(monkeypatch)
    root = FakeWorkflowAgent("pipeline", sub_agents=[FakeAgent("worker", instruction="work")])
    harness = _mcp_harness(rules=capabilities.AgentRules(text="never delete a namespace"))

    prepared, metadata, errors = harness._prepare(root)

    assert errors == []
    # Only the LlmAgent-shaped child can hold either.
    assert metadata["mcp_agents"] == 1
    assert metadata["instruction_agents"] == 1
    assert not hasattr(prepared, "tools")
    assert prepared.sub_agents[0].tools == built


def test_prepare_records_an_error_when_mcp_support_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom() -> None:
        raise ImportError("no module named mcp")

    monkeypatch.setattr(adk_mod, "_load_toolset_types", boom)

    harness = adk_mod.AdkAgent(
        agents_config.AgentConfig(
            capabilities=capabilities.AllCapabilities(
                mcp_servers=(capabilities.McpBinding(name="tools", command=("k8s-mcp",)),)
            )
        )
    )

    prepared, metadata, errors = harness._prepare(FakeAgent("root"))

    assert prepared.tools == []
    assert "mcp_toolsets" not in metadata
    assert len(errors) == 1
    assert "without MCP tools" in errors[0]


# --------------------------------------------------------------------------
# Harness wiring
# --------------------------------------------------------------------------


def test_adk_agent_is_registered() -> None:
    assert base.AGENTS.get("adk") is adk_mod.AdkAgent


def test_importing_the_harness_pulls_no_sdk() -> None:
    """The module is on ``_BUILTIN_AGENT_MODULES``, so it loads on every run.

    That means importing it must not require the optional ``adk`` extra — nor
    drag in ``google.adk`` and ``mcp`` for operators running a different
    harness. A fresh interpreter keeps earlier tests' imports out of the check.
    """
    script = textwrap.dedent(
        """
        import sys
        import devops_bench.agents.adk.agent  # noqa: F401
        heavy = ["google.adk", "mcp"]
        hits = [m for m in sorted(sys.modules) if any(m == h or m.startswith(h + ".") for h in heavy)]
        print("LEAKED:" + ",".join(hits) if hits else "OK")
        sys.exit(1 if hits else 0)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stdout + result.stderr


@requires_adk
def test_execute_without_a_target_returns_an_errored_result() -> None:
    result = adk_mod.AdkAgent(agents_config.AgentConfig(target=None)).run("scale web")

    assert result.has_errors()
    assert "AGENT_TARGET" in result.errors[0]
    assert result.trajectory == []


@requires_adk
def test_execute_reports_an_unloadable_target() -> None:
    config = agents_config.AgentConfig(target="devops_bench.agents.adk.parsing:not_an_agent")
    result = adk_mod.AdkAgent(config).run("scale web")

    assert result.has_errors()
    assert "could not load the ADK agent" in result.errors[0]


# --------------------------------------------------------------------------
# SDK-backed paths
# --------------------------------------------------------------------------

_AGENT_FIXTURE = textwrap.dedent(
    '''
    """An ADK agent driven by a stub model, so the run needs no network."""

    from google.adk.agents import LlmAgent
    from google.adk.models import BaseLlm, LlmResponse
    from google.genai import types


    def scale_deployment(name: str, replicas: int) -> dict:
        """Scale a deployment to the given replica count."""
        return {"scaled": name, "replicas": replicas}


    class StubLlm(BaseLlm):
        model: str = "stub-model"
        turn: int = 0

        async def generate_content_async(self, llm_request, stream=False):
            self.turn += 1
            usage = types.GenerateContentResponseUsageMetadata(
                prompt_token_count=100, candidates_token_count=10, total_token_count=110
            )
            if self.turn == 1:
                part = types.Part.from_function_call(
                    name="scale_deployment", args={"name": "web", "replicas": 3}
                )
                yield LlmResponse(
                    content=types.Content(role="model", parts=[part]), usage_metadata=usage
                )
            else:
                yield LlmResponse(
                    content=types.Content(
                        role="model", parts=[types.Part(text="Scaled web to 3 replicas.")]
                    ),
                    usage_metadata=usage,
                )


    root_agent = LlmAgent(name="fixture_agent", model=StubLlm(), tools=[scale_deployment])
    '''
)


@pytest.fixture
def agent_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Write an ADK agent directory laid out the way ADK expects."""
    directory = tmp_path / "fixture_agent"
    directory.mkdir()
    (directory / "agent.py").write_text(_AGENT_FIXTURE, encoding="utf-8")
    return directory


@requires_adk
def test_resolve_root_agent_from_an_agent_directory(agent_dir: pathlib.Path) -> None:
    resolved = adk_mod._resolve_root_agent(str(agent_dir))

    assert resolved.name == "fixture_agent"


@requires_adk
def test_resolve_root_agent_from_a_file_with_an_explicit_attribute(agent_dir: pathlib.Path) -> None:
    resolved = adk_mod._resolve_root_agent(f"{agent_dir / 'agent.py'}:root_agent")

    assert resolved.name == "fixture_agent"


@requires_adk
def test_resolve_root_agent_calls_a_factory(agent_dir: pathlib.Path) -> None:
    (agent_dir / "agent.py").write_text(
        _AGENT_FIXTURE + "\n\ndef build():\n    return root_agent\n", encoding="utf-8"
    )

    resolved = adk_mod._resolve_root_agent(f"{agent_dir}:build")

    assert resolved.name == "fixture_agent"


@requires_adk
def test_resolve_root_agent_rejects_a_non_agent(agent_dir: pathlib.Path) -> None:
    (agent_dir / "agent.py").write_text("root_agent = object()\n", encoding="utf-8")

    with pytest.raises(Exception, match="not an ADK agent"):
        adk_mod._resolve_root_agent(str(agent_dir))


def test_import_agent_module_rejects_a_name_already_bound_elsewhere(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cached module of the same name must not stand in for the target.

    ``import_module`` reads ``sys.modules`` before ``sys.path``, so prepending
    the agent's parent cannot win against a name that is already imported. The
    harness would otherwise benchmark whatever got there first and report a
    clean success.
    """
    directory = tmp_path / "collides"
    directory.mkdir()
    (directory / "__init__.py").write_text("root_agent = None\n", encoding="utf-8")

    impostor = ModuleType("collides")
    impostor.__file__ = str(tmp_path / "elsewhere" / "collides" / "__init__.py")
    monkeypatch.setitem(sys.modules, "collides", impostor)

    with pytest.raises(core.ConfigError, match="already resolves to"):
        adk_mod._import_agent_module(str(directory))


def test_import_agent_module_accepts_the_package_it_asked_for(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard must not reject a package genuinely loaded from the target."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "modules", dict(sys.modules))
    directory = tmp_path / "genuine_agent_pkg"
    directory.mkdir()
    (directory / "__init__.py").write_text("root_agent = 'here'\n", encoding="utf-8")

    module = adk_mod._import_agent_module(str(directory))

    assert module.root_agent == "here"


class SlowClosingRunner:
    """Runner whose ``close()`` needs several awaits to release its session.

    ``close()`` awaiting at all is the whole point: that is where an inherited
    cancellation would land, and ``progress`` records how far teardown got.
    """

    progress: list[str] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.session_service = self

    async def create_session(self, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(id="s1")

    async def run_async(self, **kwargs: object) -> AsyncIterator[object]:
        await asyncio.sleep(10)
        yield  # pragma: no cover - the budget always expires first

    async def close(self) -> None:
        type(self).progress.append("started")
        for _ in range(3):
            await asyncio.sleep(0.01)
        type(self).progress.append("finished")


@requires_adk
def test_drive_finishes_teardown_after_the_budget_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cancellation that ends the run must not cut teardown short.

    One MCP binding is shared across the whole tree, so a ``close()`` that
    stops half-way leaves the server subprocess running for the rest of the
    session.
    """
    SlowClosingRunner.progress = []
    import google.adk.runners as adk_runners

    monkeypatch.setattr(adk_runners, "InMemoryRunner", SlowClosingRunner)

    events, errors = adk_mod._drive(object(), "prompt", 0.02)

    assert SlowClosingRunner.progress == ["started", "finished"]
    assert events == []
    assert errors == ["ADK run exceeded the 0.02s budget"]


def test_close_quietly_finishes_under_repeated_cancellation() -> None:
    """Teardown must not inherit cancellations aimed at the run that owns it.

    The budget expiring cancels the owning coroutine once, which teardown
    survives on its own. A second cancellation — a Ctrl-C landing on a run
    whose budget has already blown — is what lands *inside* ``close()``, and a
    bare ``await runner.close()`` stops there with the MCP server still up.
    """
    SlowClosingRunner.progress = []
    runner = SlowClosingRunner()

    async def scenario() -> None:
        async def owner() -> None:
            try:
                await asyncio.sleep(10)
            finally:
                await adk_mod._close_quietly(runner)

        task = asyncio.ensure_future(owner())
        await asyncio.sleep(0.01)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0.01)
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert SlowClosingRunner.progress == ["started", "finished"]


@requires_adk
def test_close_quietly_gives_up_on_a_wedged_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server that never releases must not replace a timeout with a hang."""
    monkeypatch.setattr(adk_mod, "_CLOSE_TIMEOUT_SEC", 0.05)
    cancelled: list[bool] = []

    class WedgedRunner:
        async def close(self) -> None:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

    asyncio.run(adk_mod._close_quietly(WedgedRunner()))

    assert cancelled == [True]


@requires_adk
def test_execute_drives_a_real_adk_agent_end_to_end(agent_dir: pathlib.Path) -> None:
    # No AGENT_MODEL: overriding it would replace the stub with a live model.
    config = agents_config.AgentConfig(target=str(agent_dir), model=None)

    result = adk_mod.AdkAgent(config).run("scale web to 3")

    assert result.errors == []
    assert result.output == "Scaled web to 3 replicas."
    assert result.trajectory == [
        {
            "name": "scale_deployment",
            "args": {"name": "web", "replicas": 3},
            "result": '{"replicas": 3, "scaled": "web"}',
            "status": "completed",
        }
    ]
    assert result.tokens["total"] == 220
    assert result.latency > 0
    assert result.metadata["agent_name"] == "fixture_agent"
    assert result.metadata["event_count"] == 3


@requires_adk
def test_execute_runs_the_agent_inside_the_workspace(
    agent_dir: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """A relative path written by a tool must land where the diff is rooted.

    Regression test for a real run: the agent wrote its `report.md` deliverable
    into the operator's home directory, so the orchestrator's artifact diff came
    back empty and the file was left behind for the next run to trip over.
    """
    (agent_dir / "agent.py").write_text(
        _AGENT_FIXTURE.replace(
            'return {"scaled": name, "replicas": replicas}',
            'open("report.md", "w").write("done")\n    '
            'return {"scaled": name, "replicas": replicas}',
        ),
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    before = os.getcwd()
    config = agents_config.AgentConfig(target=str(agent_dir), model=None)

    result = adk_mod.AdkAgent(config).run("scale web to 3", workspace_path=workspace)

    assert result.errors == []
    assert (workspace / "report.md").read_text() == "done"
    assert result.metadata["workspace"] == str(workspace)
    # The process directory is global state; leaving it moved would silently
    # relocate every later run.
    assert os.getcwd() == before


def test_in_workspace_restores_the_previous_directory_on_failure(tmp_path: pathlib.Path) -> None:
    before = os.getcwd()

    with pytest.raises(RuntimeError), adk_mod._in_workspace(tmp_path):
        assert os.getcwd() == os.path.realpath(tmp_path)
        raise RuntimeError("boom")

    assert os.getcwd() == before


def test_in_workspace_is_a_no_op_without_a_workspace() -> None:
    before = os.getcwd()

    with adk_mod._in_workspace(None):
        assert os.getcwd() == before

    assert os.getcwd() == before


@requires_adk
def test_execute_keeps_the_partial_trajectory_when_a_tool_raises(agent_dir: pathlib.Path) -> None:
    (agent_dir / "agent.py").write_text(
        _AGENT_FIXTURE.replace(
            'return {"scaled": name, "replicas": replicas}',
            'raise RuntimeError("kaboom")',
        ),
        encoding="utf-8",
    )
    config = agents_config.AgentConfig(target=str(agent_dir), model=None)

    result = adk_mod.AdkAgent(config).run("scale web to 3")

    # ADK raises out of the iterator, but the call it already yielded survives.
    assert result.has_errors()
    assert any("kaboom" in message for message in result.errors)
    assert [entry["name"] for entry in result.trajectory] == ["scale_deployment"]
    assert result.trajectory[0]["status"] == "called"
