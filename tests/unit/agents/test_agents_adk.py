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

import copy
import importlib.util
import os
import subprocess
import sys
import textwrap

import pytest

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


# --------------------------------------------------------------------------
# parse_event_stream
# --------------------------------------------------------------------------


def test_parse_event_stream_folds_call_and_response():
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


def test_parse_event_stream_reads_mcp_result_text_and_success():
    _, trajectory, _, errors = parsing.parse_event_stream([MCP_CALL_EVENT, MCP_RESPONSE_EVENT])

    assert errors == []
    assert trajectory[0]["result"] == "cluster prod-1 is HEALTHY"
    assert trajectory[0]["status"] == "completed"


def test_parse_event_stream_marks_mcp_is_error_as_failed():
    failed = copy.deepcopy(MCP_RESPONSE_EVENT)
    response = failed["content"]["parts"][0]["function_response"]["response"]
    response["isError"] = True
    response["content"] = [{"type": "text", "text": "permission denied"}]

    _, trajectory, _, _ = parsing.parse_event_stream([MCP_CALL_EVENT, failed])

    assert trajectory[0]["status"] == "error"
    assert trajectory[0]["result"] == "permission denied"


def test_parse_event_stream_marks_adk_error_payload_as_failed():
    failed = copy.deepcopy(RESPONSE_EVENT)
    failed["content"]["parts"][0]["function_response"]["response"] = {"error": "boom"}

    _, trajectory, _, _ = parsing.parse_event_stream([CALL_EVENT, failed])

    assert trajectory[0]["status"] == "error"


def test_parse_event_stream_keeps_unanswered_calls_as_called():
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


def test_parse_event_stream_reports_orphan_tool_response():
    _, trajectory, _, errors = parsing.parse_event_stream([RESPONSE_EVENT])

    assert trajectory == []
    assert len(errors) == 1
    assert "matched no pending call" in errors[0]


def test_parse_event_stream_pairs_id_less_calls_in_order():
    call_a = {"content": {"role": "model", "parts": [{"function_call": {"name": "a", "args": {}}}]}}
    call_b = {"content": {"role": "model", "parts": [{"function_call": {"name": "b", "args": {}}}]}}
    resp_a = {"content": {"role": "user", "parts": [{"function_response": {"response": "ra"}}]}}
    resp_b = {"content": {"role": "user", "parts": [{"function_response": {"response": "rb"}}]}}

    _, trajectory, _, errors = parsing.parse_event_stream([call_a, call_b, resp_a, resp_b])

    assert errors == []
    assert [(e["name"], e["result"]) for e in trajectory] == [("a", "ra"), ("b", "rb")]


def test_parse_event_stream_skips_partial_and_thought_text():
    events = [
        {"content": {"role": "model", "parts": [{"text": "Scal"}]}, "partial": True},
        {"content": {"role": "model", "parts": [{"text": "thinking...", "thought": True}]}},
        {"content": {"role": "user", "parts": [{"text": "the original prompt"}]}},
        {"content": {"role": "model", "parts": [{"text": "Scaled."}]}},
    ]

    output, _, _, errors = parsing.parse_event_stream(events)

    assert output == "Scaled."
    assert errors == []


def test_parse_event_stream_reports_non_mapping_event():
    _, _, _, errors = parsing.parse_event_stream(["not an event"])

    assert errors == ["event 0: unexpected type str"]


def test_parse_event_stream_reports_unavailable_tokens_as_none():
    _, _, tokens, _ = parsing.parse_event_stream([MCP_CALL_EVENT, MCP_RESPONSE_EVENT])

    assert set(tokens.values()) == {None}


def test_parse_event_stream_subtracts_cached_from_input():
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


def test_parse_event_stream_clamps_over_reported_cache_read():
    event = {
        "usage_metadata": {"prompt_token_count": 10, "cached_content_token_count": 40},
    }

    _, _, tokens, _ = parsing.parse_event_stream([event])

    assert tokens["input"] == 0


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
def test_looks_like_path(spec, expected):
    assert adk_mod._looks_like_path(spec) is expected


# --------------------------------------------------------------------------
# Preparation (SDK-free: the harness only duck-types the agent object)
# --------------------------------------------------------------------------


class FakeAgent:
    """Stand-in exposing the handful of attributes the harness touches."""

    def __init__(self, name, model=None, instruction=None, tools=None, sub_agents=()):
        self.name = name
        self.model = model
        self.instruction = instruction
        self.tools = list(tools or [])
        self.sub_agents = list(sub_agents)

    def model_copy(self, *, deep=False):
        return copy.deepcopy(self)


def test_prepare_leaves_the_imported_agent_untouched():
    original = FakeAgent("root", model="baked-in", instruction="be helpful")
    harness = adk_mod.AdkAgent(agents_config.AgentConfig(model="bench-model"))

    prepared, metadata, errors = harness._prepare(original)

    assert errors == []
    assert prepared is not original
    assert original.model == "baked-in"
    assert prepared.model == "bench-model"
    assert metadata["model_override_count"] == 1


def test_prepare_overrides_sub_agent_models_too():
    root = FakeAgent("root", model="a", sub_agents=[FakeAgent("child", model="b")])
    harness = adk_mod.AdkAgent(agents_config.AgentConfig(model="bench-model"))

    prepared, metadata, _ = harness._prepare(root)

    assert prepared.sub_agents[0].model == "bench-model"
    assert metadata["model_override_count"] == 2


def test_prepare_keeps_the_agents_own_model_when_unset():
    root = FakeAgent("root", model="baked-in")
    harness = adk_mod.AdkAgent(agents_config.AgentConfig(model=None))

    prepared, metadata, _ = harness._prepare(root)

    assert prepared.model == "baked-in"
    assert "model_override_count" not in metadata


def test_prepare_appends_rules_to_the_instruction():
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


def test_prepare_appends_discovered_skills(tmp_path):
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


def test_prepare_reports_a_callable_instruction_provider():
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


def test_prepare_attaches_one_toolset_per_mcp_binding(monkeypatch):
    built: list[dict] = []

    class FakeToolset:
        def __init__(self, *, connection_params, tool_filter=None):
            built.append({"params": connection_params, "tool_filter": tool_filter})

    def fake_connection(*, server_params, timeout):
        return {"server_params": server_params, "timeout": timeout}

    def fake_server_params(*, command, args):
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

    def __init__(self, name, sub_agents=()):
        self.name = name
        self.sub_agents = list(sub_agents)

    def model_copy(self, *, deep=False):
        return copy.deepcopy(self)


def _stub_toolset_types(monkeypatch):
    """Point ``_load_toolset_types`` at cheap stand-ins and return the built list."""
    built: list[object] = []

    class FakeToolset:
        def __init__(self, *, connection_params, tool_filter=None):
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


def _mcp_harness(**caps):
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


def test_prepare_attaches_toolsets_to_every_agent_in_the_tree(monkeypatch):
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


def test_prepare_delivers_rules_to_every_agent_in_the_tree():
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


def test_prepare_names_only_the_agents_that_refused_the_instruction():
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


def test_prepare_steps_over_nodes_that_hold_no_tools_or_instruction(monkeypatch):
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


def test_prepare_records_an_error_when_mcp_support_is_missing(monkeypatch):
    def boom():
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


def test_adk_agent_is_registered():
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
def test_execute_without_a_target_returns_an_errored_result():
    result = adk_mod.AdkAgent(agents_config.AgentConfig(target=None)).run("scale web")

    assert result.has_errors()
    assert "AGENT_TARGET" in result.errors[0]
    assert result.trajectory == []


@requires_adk
def test_execute_reports_an_unloadable_target():
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
def agent_dir(tmp_path):
    """Write an ADK agent directory laid out the way ADK expects."""
    directory = tmp_path / "fixture_agent"
    directory.mkdir()
    (directory / "agent.py").write_text(_AGENT_FIXTURE, encoding="utf-8")
    return directory


@requires_adk
def test_resolve_root_agent_from_an_agent_directory(agent_dir):
    resolved = adk_mod._resolve_root_agent(str(agent_dir))

    assert resolved.name == "fixture_agent"


@requires_adk
def test_resolve_root_agent_from_a_file_with_an_explicit_attribute(agent_dir):
    resolved = adk_mod._resolve_root_agent(f"{agent_dir / 'agent.py'}:root_agent")

    assert resolved.name == "fixture_agent"


@requires_adk
def test_resolve_root_agent_calls_a_factory(agent_dir):
    (agent_dir / "agent.py").write_text(
        _AGENT_FIXTURE + "\n\ndef build():\n    return root_agent\n", encoding="utf-8"
    )

    resolved = adk_mod._resolve_root_agent(f"{agent_dir}:build")

    assert resolved.name == "fixture_agent"


@requires_adk
def test_resolve_root_agent_rejects_a_non_agent(agent_dir):
    (agent_dir / "agent.py").write_text("root_agent = object()\n", encoding="utf-8")

    with pytest.raises(Exception, match="not an ADK agent"):
        adk_mod._resolve_root_agent(str(agent_dir))


@requires_adk
def test_execute_drives_a_real_adk_agent_end_to_end(agent_dir):
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
def test_execute_runs_the_agent_inside_the_workspace(agent_dir, tmp_path):
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


def test_in_workspace_restores_the_previous_directory_on_failure(tmp_path):
    before = os.getcwd()

    with pytest.raises(RuntimeError), adk_mod._in_workspace(tmp_path):
        assert os.getcwd() == os.path.realpath(tmp_path)
        raise RuntimeError("boom")

    assert os.getcwd() == before


def test_in_workspace_is_a_no_op_without_a_workspace():
    before = os.getcwd()

    with adk_mod._in_workspace(None):
        assert os.getcwd() == before

    assert os.getcwd() == before


@requires_adk
def test_execute_keeps_the_partial_trajectory_when_a_tool_raises(agent_dir):
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
