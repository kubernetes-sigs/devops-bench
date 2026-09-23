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

"""Unit tests for :mod:`devops_bench.agents.api.agent`."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from devops_bench.agents import AGENTS, AgentConfig
from devops_bench.agents.api import agent as agent_mod
from devops_bench.agents.api.agent import (
    ApiAgent,
    extract_tokens,
    fold_trajectory,
)
from devops_bench.agents.capabilities import (
    AgentRules,
    AllCapabilities,
    McpBinding,
    SkillBinding,
    SupportsMcp,
    SupportsRules,
    SupportsSkills,
)
from devops_bench.models.base import LLMClient


def _mcp_caps(command: str = "server", *, tools: tuple[str, ...] = ()) -> AllCapabilities:
    """Helper: build capabilities that turn the API agent's MCP path on."""
    return AllCapabilities(
        mcp_servers=(McpBinding(name="test", command=tuple(command.split()), tools=tools),),
    )


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _Turn:
    """One scripted response in :class:`_FakeLLMClient`.

    Attributes:
        text: ``get_text_content`` payload for this turn.
        calls: Function calls returned by ``extract_function_calls`` (each in
            the neutral ``{"name", "args", "id"}`` shape).
        usage: Optional duck-typed usage object surfaced on the raw response.
        usage_attr: Which attribute name to attach ``usage`` under. Defaults to
            ``"usage_metadata"`` (Google shape); set ``"usage"`` to exercise
            Anthropic / OpenAI / Ollama paths.
    """

    text: str
    calls: list[dict] = field(default_factory=list)
    usage: Any = None
    usage_attr: str = "usage_metadata"


class _FakeLLMClient(LLMClient):
    """Scripted :class:`LLMClient` that returns ``_Turn`` objects in order.

    Records every ``generate_content`` invocation and tracks whether
    ``format_tools`` was called so the agent's caller-formats-tools contract is
    asserted.
    """

    def __init__(self, turns: list[_Turn]) -> None:
        self._turns = list(turns)
        self.calls: list[dict] = []
        self.format_tools_calls: list[Any] = []

    async def generate_content(
        self,
        contents: list[dict[str, Any]],
        tools: Any,
        system_instruction: str | None,
    ) -> Any:
        if not self._turns:
            raise AssertionError("FakeLLMClient ran out of scripted turns")
        turn = self._turns.pop(0)
        self.calls.append(
            {
                "contents": [dict(msg) for msg in contents],
                "tools": tools,
                "system_instruction": system_instruction,
            }
        )
        response = SimpleNamespace(text=turn.text, calls=turn.calls)
        if turn.usage is not None:
            setattr(response, turn.usage_attr, turn.usage)
        return response

    def format_tools(self, mcp_tools: Any) -> Any:
        # Snapshot so tests can assert the agent does pre-format tools before
        # calling the loop.
        self.format_tools_calls.append(list(mcp_tools))
        return ("formatted", tuple(getattr(t, "name", "") for t in mcp_tools))

    def extract_function_calls(self, response: Any) -> list[dict]:
        return list(response.calls)

    def get_text_content(self, response: Any) -> str:
        return response.text


class _FakeMCPClient:
    """Stand-in for :class:`MCPClient` exposing only what the agent uses.

    Records every call so tests assert dispatch hit MCP (or skipped it).
    """

    def __init__(self, tools: list[Any] | None = None) -> None:
        self._tools = list(tools or [])
        self.calls: list[tuple[str, dict]] = []
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> _FakeMCPClient:
        self.entered = True
        return self

    async def __aexit__(self, *_a: Any) -> None:
        self.exited = True

    async def list_tools(self) -> Any:
        return SimpleNamespace(tools=self._tools)

    async def call_tool(self, name: str, arguments: dict) -> Any:
        self.calls.append((name, arguments))
        block = SimpleNamespace(text=f"mcp-result-of-{name}")
        return SimpleNamespace(content=[block])


# ---------------------------------------------------------------------------
# Registration & registry wiring
# ---------------------------------------------------------------------------


def test_api_agent_registered_under_canonical_key() -> None:
    assert AGENTS.get("api") is ApiAgent


def test_package_import_alone_registers_api_agent() -> None:
    """``import devops_bench.agents.api`` (no ``.agent`` submodule import) must
    register the harness — the documented consumer convention resolves via
    ``AGENTS.get`` after a package import. Runs in a fresh interpreter because
    this test module itself imports ``.agent``, which would mask a regression
    (review finding: the package ``__init__`` didn't import the module, so the
    registration decorator never ran)."""
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent(
        """
        import devops_bench.agents.api
        from devops_bench.agents.base import AGENTS
        assert AGENTS.get("api").__name__ == "ApiAgent"
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# fold_trajectory
# ---------------------------------------------------------------------------


def test_fold_trajectory_pairs_assistant_calls_with_tool_results() -> None:
    contents = [
        {"role": "user", "content": "g"},
        {
            "role": "assistant",
            "content": "thinking",
            "tool_calls": [
                {"name": "alpha", "args": {"a": 1}, "id": "1"},
                {"name": "beta", "args": {"b": 2}, "id": "2"},
            ],
        },
        {"role": "tool", "tool_call_id": "1", "name": "alpha", "content": "A-result"},
        {"role": "tool", "tool_call_id": "2", "name": "beta", "content": "B-result"},
        {"role": "assistant", "content": "all done"},
    ]
    assert fold_trajectory(contents) == [
        {"name": "alpha", "args": {"a": 1}, "result": "A-result", "status": "completed"},
        {"name": "beta", "args": {"b": 2}, "result": "B-result", "status": "completed"},
    ]


def test_fold_trajectory_marks_dispatcher_errors_as_error_status() -> None:
    # The dispatcher returns ``"Error: ..."`` for a failed tool call (matching
    # the agent's _build_dispatch contract) — folding must surface that as the
    # ``"error"`` status so metrics see the failure mode, not a clean call.
    contents = [
        {"role": "user", "content": "g"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"name": "boom", "args": {}, "id": "x"}],
        },
        {"role": "tool", "tool_call_id": "x", "name": "boom", "content": "Error: kaboom"},
        {"role": "assistant", "content": "abort"},
    ]
    folded = fold_trajectory(contents)
    assert folded == [
        {"name": "boom", "args": {}, "result": "Error: kaboom", "status": "error"},
    ]


def test_fold_trajectory_leaves_unmatched_call_as_called_status() -> None:
    # If a result never lands (e.g. dispatch raised before appending), the call
    # entry survives with ``status="called"`` and ``result=None`` rather than
    # being silently dropped.
    contents = [
        {"role": "user", "content": "g"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"name": "lost", "args": {}, "id": "9"}],
        },
    ]
    assert fold_trajectory(contents) == [
        {"name": "lost", "args": {}, "result": None, "status": "called"},
    ]


def test_fold_trajectory_skips_text_only_turns() -> None:
    contents = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "assistant", "content": "bye"},
    ]
    assert fold_trajectory(contents) == []


def test_fold_trajectory_handles_none_args_and_none_call_id() -> None:
    # Defensive: missing ``args`` becomes ``{}``; an entry with no ``id`` is
    # emitted as ``status="called"`` (no result to pair with).
    contents = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"name": "t", "args": None, "id": None}],
        },
    ]
    assert fold_trajectory(contents) == [
        {"name": "t", "args": {}, "result": None, "status": "called"},
    ]


def test_fold_trajectory_pairs_unkeyed_gemini_style_calls_fifo() -> None:
    """Gemini emits every function call with ``id=None`` and ``run_tool_loop``
    appends one result per call in order — the fold must pair them FIFO
    instead of dropping every result as an orphan (review finding: the default
    provider produced all-'called' trajectories and errored every clean run)."""
    contents = [
        {"role": "user", "content": "g"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"name": "alpha", "args": {"a": 1}, "id": None},
                {"name": "beta", "args": {"b": 2}, "id": None},
            ],
        },
        {"role": "tool", "tool_call_id": None, "name": "alpha", "content": "A-result"},
        {"role": "tool", "tool_call_id": None, "name": "beta", "content": "Error: b"},
        {"role": "assistant", "content": "done"},
    ]
    from devops_bench.agents.api.agent import _fold_with_extraction_errors

    folded, orphans = _fold_with_extraction_errors(contents)
    assert orphans == []
    assert folded == [
        {"name": "alpha", "args": {"a": 1}, "result": "A-result", "status": "completed"},
        {"name": "beta", "args": {"b": 2}, "result": "Error: b", "status": "error"},
    ]


def test_fold_trajectory_extra_unkeyed_result_is_still_an_orphan() -> None:
    """Id-less results beyond the number of id-less calls stay orphans —
    FIFO pairing must not absorb genuinely unmatched results."""
    contents = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"name": "t", "args": {}, "id": None}],
        },
        {"role": "tool", "tool_call_id": None, "name": "t", "content": "paired"},
        {"role": "tool", "tool_call_id": None, "name": "ghost", "content": "stray"},
    ]
    from devops_bench.agents.api.agent import _fold_with_extraction_errors

    folded, orphans = _fold_with_extraction_errors(contents)
    assert folded == [{"name": "t", "args": {}, "result": "paired", "status": "completed"}]
    assert len(orphans) == 1
    assert "stray" in orphans[0]


def test_fold_trajectory_drops_unpaired_tool_results_silently_from_trajectory() -> None:
    """An orphan ``role: tool`` (no matching assistant call_id) is dropped from
    the canonical trajectory — synthesizing a free-floating entry would break
    the "every trajectory item is a real ToolCall the model issued" invariant
    metrics depend on. The diagnostic flows out via ``_fold_with_extraction_errors``
    instead (asserted in ``test_execute_orphan_tool_result_lands_in_errors``).
    """
    contents = [
        {"role": "user", "content": "g"},
        # No assistant turn → no matching call_id for the ghost result.
        {"role": "tool", "tool_call_id": "ghost", "name": "x", "content": "?"},
    ]
    assert fold_trajectory(contents) == []


def test_fold_with_extraction_errors_surfaces_orphan_results() -> None:
    from devops_bench.agents.api.agent import _fold_with_extraction_errors

    contents = [
        {"role": "user", "content": "g"},
        {"role": "tool", "tool_call_id": "ghost", "name": "x", "content": "stray"},
    ]
    folded, orphans = _fold_with_extraction_errors(contents)
    assert folded == []
    assert len(orphans) == 1
    assert "no matching call" in orphans[0]
    assert "ghost" in orphans[0]


# ---------------------------------------------------------------------------
# extract_tokens
# ---------------------------------------------------------------------------


def test_extract_tokens_reads_usage_metadata() -> None:
    usage = SimpleNamespace(prompt_token_count=3, candidates_token_count=5, total_token_count=8)
    response = SimpleNamespace(usage_metadata=usage)
    assert extract_tokens(response) == {
        "prompt_tokens": 3,
        "candidates_tokens": 5,
        "total_tokens": 8,
    }


def test_extract_tokens_falls_back_to_usage_attribute() -> None:
    usage = SimpleNamespace(prompt_token_count=1, candidates_token_count=2, total_token_count=3)
    # No ``usage_metadata``; the function should still find ``usage``.
    response = SimpleNamespace(usage=usage)
    assert extract_tokens(response) == {
        "prompt_tokens": 1,
        "candidates_tokens": 2,
        "total_tokens": 3,
    }


def test_extract_tokens_returns_empty_dict_when_no_usage() -> None:
    assert extract_tokens(SimpleNamespace()) == {}
    assert extract_tokens(None) == {}


def test_extract_tokens_defaults_missing_counts_to_zero() -> None:
    """Missing counts default to 0; if the source provided no total but the
    other two fields are non-zero, the helper computes prompt+candidates so a
    non-empty run never shows ``total_tokens: 0`` in results.json."""
    usage = SimpleNamespace(prompt_token_count=4)  # other counts unset
    response = SimpleNamespace(usage_metadata=usage)
    assert extract_tokens(response) == {
        "prompt_tokens": 4,
        "candidates_tokens": 0,
        # 4 + 0 (no source total provided, but a non-zero prompt → compute it).
        "total_tokens": 4,
    }


def test_extract_tokens_reads_anthropic_input_output_shape() -> None:
    """Anthropic emits ``usage.input_tokens`` / ``usage.output_tokens`` and **no**
    aggregated total. The helper must map both onto the legacy keys and compute
    the total so non-Google providers do not silently drop tokens.

    Regression test for the blocking bug found in review: the previous
    extract_tokens only read Google-style fields and returned zeros for any
    Anthropic response, corrupting token metrics in results.json.
    """
    usage = SimpleNamespace(input_tokens=42, output_tokens=17)
    response = SimpleNamespace(usage=usage)
    assert extract_tokens(response) == {
        "prompt_tokens": 42,
        "candidates_tokens": 17,
        "total_tokens": 59,
    }


def test_extract_tokens_reads_openai_ollama_shape() -> None:
    """OpenAI / Ollama emit ``usage.prompt_tokens`` / ``completion_tokens`` /
    ``total_tokens``. The helper must map ``completion_tokens`` → the legacy
    ``candidates_tokens`` slot and pass ``total_tokens`` through verbatim.

    Regression test for the same blocking bug — Ollama responses returned
    all-zero tokens before the provider-shape detection landed.
    """
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=20, total_tokens=30)
    response = SimpleNamespace(usage=usage)
    assert extract_tokens(response) == {
        "prompt_tokens": 10,
        "candidates_tokens": 20,
        "total_tokens": 30,
    }


def test_extract_tokens_google_total_passes_through_when_present() -> None:
    """When the provider supplies its own total it wins over the computed sum."""
    usage = SimpleNamespace(prompt_token_count=5, candidates_token_count=7, total_token_count=99)
    response = SimpleNamespace(usage_metadata=usage)
    # The helper does not second-guess a provider-supplied total even when it
    # disagrees with prompt+candidates (some providers include reasoning/cached
    # tokens in the total).
    assert extract_tokens(response)["total_tokens"] == 99


def test_extract_tokens_preserves_legacy_on_disk_key_scheme() -> None:
    """The on-disk dict shape must stay ``{prompt_tokens, candidates_tokens,
    total_tokens}`` for D3 (results.json stability) — three keys, exactly.
    """
    usage = SimpleNamespace(input_tokens=1, output_tokens=2)
    keys = set(extract_tokens(SimpleNamespace(usage=usage)).keys())
    assert keys == {"prompt_tokens", "candidates_tokens", "total_tokens"}


# ---------------------------------------------------------------------------
# ApiAgent._execute — MCP-off path
# ---------------------------------------------------------------------------


def test_execute_runs_with_no_tools_when_capabilities_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default capabilities (no MCP binding, no skills) → loop runs tool-less.

    Renamed from ``..._when_target_unset`` because the MCP gate is no longer
    ``config.target`` — it's ``config.capabilities.mcp``. The old name
    survives in git history but the new one matches the post-PR3 reality.
    """
    fake = _FakeLLMClient(
        [
            _Turn(
                text="done",
                usage=SimpleNamespace(
                    prompt_token_count=3,
                    candidates_token_count=5,
                    total_token_count=8,
                ),
            )
        ]
    )
    monkeypatch.setattr(agent_mod, "get_model", lambda *a, **kw: fake)

    agent = ApiAgent(AgentConfig())
    result = agent.run("hello")

    assert result.output == "done"
    assert result.trajectory == []
    assert result.tokens == {
        "prompt_tokens": 3,
        "candidates_tokens": 5,
        "total_tokens": 8,
    }
    assert result.errors == []
    # Caller-formats-tools: the agent must call format_tools on the (empty)
    # skill list before invoking the loop.
    assert fake.format_tools_calls == [[]]
    # The loop saw the pre-formatted tools sentinel, not the raw list.
    assert fake.calls[0]["tools"] == ("formatted", ())


def test_execute_passes_explicit_provider_and_model_to_get_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_get_model(provider, model):
        captured["provider"] = provider
        captured["model"] = model
        return _FakeLLMClient([_Turn(text="ok")])

    monkeypatch.setattr(agent_mod, "get_model", fake_get_model)
    cfg = AgentConfig(provider="anthropic", model="claude-3-5")
    ApiAgent(cfg).run("p")
    assert captured == {"provider": "anthropic", "model": "claude-3-5"}


def test_execute_records_anthropic_tokens_through_to_agentresult(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: an Anthropic-shaped usage object surfaces on
    ``AgentResult.tokens`` under the legacy key scheme — regression for the
    blocking bug where non-Google providers silently logged zero tokens."""
    fake = _FakeLLMClient(
        [
            _Turn(
                text="done",
                usage=SimpleNamespace(input_tokens=42, output_tokens=17),
                usage_attr="usage",
            )
        ]
    )
    monkeypatch.setattr(agent_mod, "get_model", lambda *a, **kw: fake)
    result = ApiAgent(AgentConfig()).run("p")
    assert result.tokens == {
        "prompt_tokens": 42,
        "candidates_tokens": 17,
        "total_tokens": 59,
    }


def test_execute_records_openai_tokens_through_to_agentresult(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: an OpenAI/Ollama-shaped usage object surfaces under the
    legacy key scheme."""
    fake = _FakeLLMClient(
        [
            _Turn(
                text="done",
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20, total_tokens=30),
                usage_attr="usage",
            )
        ]
    )
    monkeypatch.setattr(agent_mod, "get_model", lambda *a, **kw: fake)
    result = ApiAgent(AgentConfig()).run("p")
    assert result.tokens == {
        "prompt_tokens": 10,
        "candidates_tokens": 20,
        "total_tokens": 30,
    }


# ---------------------------------------------------------------------------
# ApiAgent._execute — MCP-on, trajectory folding & tools_used metadata
# ---------------------------------------------------------------------------


def test_execute_folds_assistant_tool_pairs_into_canonical_trajectory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: an MCP tool turn followed by a finishing turn → one ToolCall."""
    fc = [{"name": "do_thing", "args": {"a": 1}, "id": "call-1"}]
    fake = _FakeLLMClient(
        [
            _Turn(text="working", calls=fc),
            _Turn(
                text="done",
                usage=SimpleNamespace(
                    prompt_token_count=10,
                    candidates_token_count=20,
                    total_token_count=30,
                ),
            ),
        ]
    )
    mcp_advertised = [SimpleNamespace(name="do_thing", description="d", inputSchema=None)]
    mcp = _FakeMCPClient(tools=mcp_advertised)
    monkeypatch.setattr(agent_mod, "get_model", lambda *a, **kw: fake)
    monkeypatch.setattr(agent_mod, "MCPClient", lambda _path, **_kw: mcp)

    result = ApiAgent(AgentConfig(capabilities=_mcp_caps("server"))).run("ping")

    assert result.output == "done"
    assert result.trajectory == [
        {
            "name": "do_thing",
            "args": {"a": 1},
            "result": "mcp-result-of-do_thing",
            "status": "completed",
        },
    ]
    assert result.tokens == {
        "prompt_tokens": 10,
        "candidates_tokens": 20,
        "total_tokens": 30,
    }
    assert result.errors == []
    assert result.metadata["tools_used"] == ["do_thing"]
    # MCP session was entered & exited via the async-context-manager protocol.
    assert mcp.entered and mcp.exited
    # The agent passed the MCP-advertised tool through format_tools before
    # invoking the loop (caller-formats-tools contract).
    assert fake.format_tools_calls == [mcp_advertised]


# ---------------------------------------------------------------------------
# Dispatcher error handling
# ---------------------------------------------------------------------------


def test_execute_dispatch_error_lands_in_errors_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool call that raises must surface on errors, not crash the agent."""

    class _ExplodingMCP(_FakeMCPClient):
        async def call_tool(self, name: str, arguments: dict) -> Any:
            raise RuntimeError("kaboom")

    fc = [{"name": "boom", "args": {}, "id": "c1"}]
    fake = _FakeLLMClient(
        [
            _Turn(text="trying", calls=fc),
            _Turn(text="giving up"),
        ]
    )
    mcp = _ExplodingMCP(tools=[SimpleNamespace(name="boom", description="d", inputSchema=None)])
    monkeypatch.setattr(agent_mod, "get_model", lambda *a, **kw: fake)
    monkeypatch.setattr(agent_mod, "MCPClient", lambda _path, **_kw: mcp)

    result = ApiAgent(AgentConfig(capabilities=_mcp_caps("server"))).run("ping")

    assert result.output == "giving up"
    assert any("Error calling tool boom" in e for e in result.errors)
    # The trajectory still records the failed call with ``status="error"`` so
    # the metrics layer can see the failure mode.
    assert result.trajectory == [
        {"name": "boom", "args": {}, "result": "Error: kaboom", "status": "error"},
    ]


def test_execute_marks_mcp_iserror_result_as_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """An MCP server reporting failure via ``CallToolResult.isError`` (rather
    than raising) must fold as ``status="error"`` and land on ``errors`` —
    previously the flag was ignored and failures scored as completed (review
    finding)."""

    class _IsErrorMCP(_FakeMCPClient):
        async def call_tool(self, name: str, arguments: dict) -> Any:
            block = SimpleNamespace(text="unknown pod foo")
            return SimpleNamespace(content=[block], isError=True)

    fc = [{"name": "get_pod", "args": {}, "id": "c1"}]
    fake = _FakeLLMClient([_Turn(text="trying", calls=fc), _Turn(text="giving up")])
    mcp = _IsErrorMCP(tools=[SimpleNamespace(name="get_pod", description="d", inputSchema=None)])
    monkeypatch.setattr(agent_mod, "get_model", lambda *a, **kw: fake)
    monkeypatch.setattr(agent_mod, "MCPClient", lambda _path, **_kw: mcp)

    result = ApiAgent(AgentConfig(capabilities=_mcp_caps("server"))).run("ping")

    assert any("Error calling tool get_pod" in e for e in result.errors)
    assert result.trajectory == [
        {
            "name": "get_pod",
            "args": {},
            "result": "Error: unknown pod foo",
            "status": "error",
        },
    ]


def test_execute_records_missing_mcp_when_tool_requested_without_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No MCP server, but the model still requests a tool → recorded, not crashed."""
    fc = [{"name": "ghost", "args": {}, "id": "c2"}]
    fake = _FakeLLMClient(
        [
            _Turn(text="requesting", calls=fc),
            _Turn(text="abandoned"),
        ]
    )
    monkeypatch.setattr(agent_mod, "get_model", lambda *a, **kw: fake)
    result = ApiAgent(AgentConfig()).run("p")  # no target -> no MCP

    assert any("no MCP server is configured" in e for e in result.errors)
    assert result.trajectory == [
        {
            "name": "ghost",
            "args": {},
            "result": (
                "Error: tool 'ghost' requested but no MCP server is configured for this agent."
            ),
            "status": "error",
        },
    ]


# ---------------------------------------------------------------------------
# Skills are independent of MCP
# ---------------------------------------------------------------------------


def test_execute_skills_discover_without_mcp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Skills must be loadable on the MCP-off path, not gated on MCP being on."""
    skill_dir = tmp_path / "skills"
    skill = skill_dir / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill_body = '---\nname: "demo-skill"\ndescription: does things\n---\nbody\n'
    skill.write_text(skill_body)

    fc = [{"name": "skill_demo_skill", "args": {}, "id": "s1"}]
    fake = _FakeLLMClient(
        [
            _Turn(text="using skill", calls=fc),
            _Turn(text="done"),
        ]
    )
    monkeypatch.setattr(agent_mod, "get_model", lambda *a, **kw: fake)
    cfg = AgentConfig(
        capabilities=AllCapabilities(skills=SkillBinding(paths=(str(skill_dir),))),
    )  # NB: no mcp binding → MCP path disabled
    result = ApiAgent(cfg).run("p")

    assert result.errors == []  # skill dispatch hit the file, not the MCP error
    # Dispatch serves the whole file (frontmatter + body) captured at
    # discovery so the
    # model can read either as it sees fit — matches the legacy semantic.
    assert result.trajectory == [
        {
            "name": "skill_demo_skill",
            "args": {},
            "result": skill_body,
            "status": "completed",
        },
    ]
    assert result.metadata["skills_loaded"] == ["demo-skill"]
    # format_tools received the synthetic skill descriptor — one entry.
    assert len(fake.format_tools_calls[0]) == 1
    assert fake.format_tools_calls[0][0].name == "skill_demo_skill"


def test_execute_no_skills_no_mcp_runs_tool_less(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty skills + no target → loop seen no tools, no metadata noise."""
    fake = _FakeLLMClient([_Turn(text="hi")])
    monkeypatch.setattr(agent_mod, "get_model", lambda *a, **kw: fake)
    result = ApiAgent(AgentConfig()).run("p")
    assert result.output == "hi"
    assert "skills_loaded" not in result.metadata
    assert result.metadata["tools_used"] == []


# ---------------------------------------------------------------------------
# Surfaced model-construction errors / empty MCP target
# ---------------------------------------------------------------------------


def test_execute_lets_value_error_reach_base_safety_net(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``ValueError`` raised inside the run (MCP setup, provider adapter,
    anywhere) must bubble to :meth:`AgentHarness.run`'s safety net, which logs
    the full traceback and converts to an errored result tagged with the
    exception type.

    The agent previously intercepted ``ValueError`` itself "for empty MCP
    commands" — a case its own pre-guard makes unreachable — thereby swallowing
    unrelated ValueErrors without a traceback (review finding); the catch is
    gone.
    """

    class _BoomMCP:
        def __init__(self, _path: str, **_kw: Any) -> None: ...

        async def __aenter__(self) -> _BoomMCP:
            raise ValueError("bad provider argument")

        async def __aexit__(self, *_a: Any) -> None: ...

    fake = _FakeLLMClient([_Turn(text="never reached")])
    monkeypatch.setattr(agent_mod, "get_model", lambda *a, **kw: fake)
    monkeypatch.setattr(agent_mod, "MCPClient", _BoomMCP)

    caps = AllCapabilities(
        mcp_servers=(McpBinding(name="bad", command=("/never-spawned",)),),
    )
    result = ApiAgent(AgentConfig(capabilities=caps)).run("p")
    assert result.has_errors()
    # The base safety net formats as "<ExcType>: <msg>" — proof it was used.
    assert result.errors[0] == "ValueError: bad provider argument"
    assert result.output.startswith("Error: ")


def test_execute_times_out_via_config_timeout_sec(monkeypatch: pytest.MonkeyPatch) -> None:
    """``AgentConfig.timeout_sec`` must bound the whole run — a hanging MCP
    server or provider call converts to an errored result at the deadline
    instead of wedging the benchmark (review finding: the field was ignored)."""

    async def _hang(*_a: Any, **_kw: Any):
        await asyncio.sleep(30)

    monkeypatch.setattr(agent_mod, "_run_async", _hang)
    monkeypatch.setattr(agent_mod, "get_model", lambda *a, **kw: _FakeLLMClient([]))

    result = ApiAgent(AgentConfig(timeout_sec=0.05)).run("p")
    assert result.has_errors()
    assert "timed out after 0.05s" in result.errors[0]


def test_execute_reraises_internal_timeout_before_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``TimeoutError`` from inside the run (e.g. a provider socket timeout —
    ``socket.timeout`` is ``TimeoutError`` since 3.10) raised well before the
    configured deadline must not be relabeled as the agent-level timeout: it
    re-raises to the base safety net, which tags the exception type and logs
    the traceback."""

    async def _boom(*_a: Any, **_kw: Any):
        raise TimeoutError("socket read timed out")

    monkeypatch.setattr(agent_mod, "_run_async", _boom)
    monkeypatch.setattr(agent_mod, "get_model", lambda *a, **kw: _FakeLLMClient([]))

    result = ApiAgent(AgentConfig(timeout_sec=600.0)).run("p")
    assert result.has_errors()
    assert result.errors[0] == "TimeoutError: socket read timed out"


def test_execute_honors_max_turns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """``max_turns=0`` means zero model turns — it must not be silently
    replaced by the 50-turn default via a falsy-``or`` (review finding)."""
    fake = _FakeLLMClient([_Turn(text="never called")])
    monkeypatch.setattr(agent_mod, "get_model", lambda *a, **kw: fake)
    result = ApiAgent(AgentConfig(max_turns=0)).run("p")
    # The loop never invoked the model.
    assert fake.calls == []
    assert result.output == ""


def _per_command_mcp(by_command: dict[str, _FakeMCPClient]):
    """Build an ``MCPClient`` replacement handing each command its own client.

    Multi-server behavior is only observable when the doubles have distinct
    identities: a single shared client cannot show which server a call was
    routed to.
    """

    def _factory(command: str, **_kw: Any) -> _FakeMCPClient:
        assert command in by_command, f"unexpected server launch: {command!r}"
        return by_command[command]

    return _factory


def test_execute_merges_tools_from_every_granted_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every launchable binding is opened and its tools advertised together.

    Dropping servers 2..N would score the run as if the agent had the whole
    grant while half its toolset was unreachable.
    """
    fake = _FakeLLMClient([_Turn(text="ok")])
    first = _FakeMCPClient(tools=[SimpleNamespace(name="alpha")])
    second = _FakeMCPClient(tools=[SimpleNamespace(name="beta")])
    monkeypatch.setattr(agent_mod, "get_model", lambda *a, **kw: fake)
    monkeypatch.setattr(
        agent_mod,
        "MCPClient",
        _per_command_mcp({"server-a": first, "server-b": second}),
    )
    caps = AllCapabilities(
        mcp_servers=(
            McpBinding(name="one", command=("server-a",)),
            McpBinding(name="two", command=("server-b",)),
        ),
    )
    result = ApiAgent(AgentConfig(capabilities=caps)).run("p")
    assert not result.has_errors()
    assert first.entered and second.entered
    assert first.exited and second.exited
    # Advertised in grant order, both servers' tools present.
    assert [getattr(t, "name", "") for t in fake.format_tools_calls[0]] == ["alpha", "beta"]


def test_execute_routes_each_call_to_the_server_that_advertised_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool call reaches the session that listed the name, not the first one."""
    fake = _FakeLLMClient(
        [
            _Turn(text="", calls=[{"name": "beta", "args": {}, "id": "c1"}]),
            _Turn(text="done"),
        ]
    )
    first = _FakeMCPClient(tools=[SimpleNamespace(name="alpha")])
    second = _FakeMCPClient(tools=[SimpleNamespace(name="beta")])
    monkeypatch.setattr(agent_mod, "get_model", lambda *a, **kw: fake)
    monkeypatch.setattr(
        agent_mod,
        "MCPClient",
        _per_command_mcp({"server-a": first, "server-b": second}),
    )
    caps = AllCapabilities(
        mcp_servers=(
            McpBinding(name="one", command=("server-a",)),
            McpBinding(name="two", command=("server-b",)),
        ),
    )
    result = ApiAgent(AgentConfig(capabilities=caps)).run("p")
    assert not result.has_errors()
    assert first.calls == []
    assert second.calls == [("beta", {})]


def test_execute_fails_when_two_servers_advertise_the_same_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duplicate tool name across servers fails the run.

    Routing to whichever server was listed first would score the arm against a
    toolset nobody chose, so ambiguity is fatal rather than resolved by order.
    """
    fake = _FakeLLMClient([_Turn(text="ok")])
    first = _FakeMCPClient(tools=[SimpleNamespace(name="shared")])
    second = _FakeMCPClient(tools=[SimpleNamespace(name="shared")])
    monkeypatch.setattr(agent_mod, "get_model", lambda *a, **kw: fake)
    monkeypatch.setattr(
        agent_mod,
        "MCPClient",
        _per_command_mcp({"server-a": first, "server-b": second}),
    )
    caps = AllCapabilities(
        mcp_servers=(
            McpBinding(name="one", command=("server-a",)),
            McpBinding(name="two", command=("server-b",)),
        ),
    )
    result = ApiAgent(AgentConfig(capabilities=caps)).run("p")
    assert result.has_errors()
    assert "MCP tool name conflict" in result.errors[0]
    assert "'one' and 'two'" in result.errors[0]
    assert "'shared'" in result.errors[0]
    # Both sessions are torn down despite the failure.
    assert first.exited and second.exited
    # The model was never called — the run fails before any provider spend.
    assert fake.calls == []


def test_execute_fails_when_a_skill_and_a_server_share_a_tool_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A skill name colliding with a granted server's tool fails the run.

    The model would be handed the same name twice, and the dispatcher's
    skill-first rule would serve the skill while the server's tool never runs —
    an MCP arm scored without the tool it granted.
    """
    skill_dir = tmp_path / "skills"
    (skill_dir / "demo").mkdir(parents=True)
    (skill_dir / "demo" / "SKILL.md").write_text('---\nname: "demo"\ndescription: x\n---\nbody\n')

    fake = _FakeLLMClient([_Turn(text="ok")])
    mcp = _FakeMCPClient(tools=[SimpleNamespace(name="skill_demo")])
    monkeypatch.setattr(agent_mod, "get_model", lambda *a, **kw: fake)
    monkeypatch.setattr(agent_mod, "MCPClient", lambda _path, **_kw: mcp)

    caps = AllCapabilities(
        mcp_servers=(McpBinding(name="one", command=("server-a",)),),
        skills=SkillBinding(paths=(str(skill_dir),)),
    )
    result = ApiAgent(AgentConfig(capabilities=caps)).run("p")

    assert result.has_errors()
    assert "MCP tool name conflict" in result.errors[0]
    assert "skill_demo" in result.errors[0]
    assert mcp.exited
    # The model was never called — the run fails before any provider spend.
    assert fake.calls == []


def test_execute_tears_down_open_sessions_when_a_later_server_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Server N failing to start must not orphan the N-1 already running."""

    class _FailingMCP(_FakeMCPClient):
        async def __aenter__(self) -> _FakeMCPClient:
            raise RuntimeError("connect refused")

    fake = _FakeLLMClient([_Turn(text="ok")])
    first = _FakeMCPClient(tools=[])
    monkeypatch.setattr(agent_mod, "get_model", lambda *a, **kw: fake)
    monkeypatch.setattr(
        agent_mod,
        "MCPClient",
        _per_command_mcp({"server-a": first, "server-b": _FailingMCP()}),
    )
    caps = AllCapabilities(
        mcp_servers=(
            McpBinding(name="one", command=("server-a",)),
            McpBinding(name="two", command=("server-b",)),
        ),
    )
    result = ApiAgent(AgentConfig(capabilities=caps)).run("p")
    assert result.has_errors()
    assert first.exited


def test_execute_allows_hosted_bindings_beside_launchable_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A command-less binding denotes a server the CLI hosts itself, so this
    agent has nothing to launch for it."""
    fake = _FakeLLMClient([_Turn(text="ok")])
    mcp = _FakeMCPClient(tools=[])
    monkeypatch.setattr(agent_mod, "get_model", lambda *a, **kw: fake)
    monkeypatch.setattr(agent_mod, "MCPClient", _per_command_mcp({"server-b": mcp}))
    caps = AllCapabilities(
        mcp_servers=(
            McpBinding(name="hosted", command=()),
            McpBinding(name="spawned", command=("server-b",)),
        ),
    )
    result = ApiAgent(AgentConfig(capabilities=caps)).run("p")
    assert not result.has_errors()
    assert result.output == "ok"


# ---------------------------------------------------------------------------
# No env-smuggling — neither BENCH_USE_MCP nor any direct env read
# ---------------------------------------------------------------------------


def test_execute_ignores_bench_use_mcp_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting BENCH_USE_MCP must not change the agent's behavior.

    The MCP on/off gate is ``config.capabilities.mcp`` (a binding with a
    non-empty ``command``), not env.
    """
    monkeypatch.setenv("BENCH_USE_MCP", "false")
    fake = _FakeLLMClient([_Turn(text="ok")])
    monkeypatch.setattr(agent_mod, "get_model", lambda *a, **kw: fake)
    # No MCP binding → loop runs without MCP regardless of the env flag.
    result = ApiAgent(AgentConfig()).run("p")
    assert result.output == "ok"


def test_agent_source_has_no_bench_use_mcp_or_environ_reads() -> None:
    """Statically verify the agents/api source carries no env-smuggling.

    The conventions doc forbids agents from reading ``BENCH_USE_MCP`` (the
    harness threads the boolean instead) or any ``os.environ`` lookups inside
    the agent — capability/MCP on-off comes from ``AgentConfig``. This test
    walks the agents/api source AST and asserts no code (i.e. anything that is
    not a docstring or comment) references those names.
    """
    import ast
    import pathlib

    api_root = pathlib.Path(agent_mod.__file__).parent
    forbidden_names = {"get_env", "get_bool", "first_env", "require_env"}
    forbidden_strings = {"BENCH_USE_MCP"}

    for src in api_root.glob("*.py"):
        tree = ast.parse(src.read_text(), filename=str(src))

        for node in ast.walk(tree):
            # Bare names: ``get_env(...)`` / ``os.environ``.
            if isinstance(node, ast.Name) and node.id in forbidden_names:
                pytest.fail(
                    f"{src.name} references env-helper {node.id!r} at line "
                    f"{node.lineno}; config flows through AgentConfig"
                )
            # Attribute access: ``os.environ``, ``os.getenv``, and the
            # ``os.environ.get(...)`` call form (which presents as an outer
            # Attribute("get", Attribute("environ", Name("os")))).
            if isinstance(node, ast.Attribute):
                attr_chain = []
                cur: ast.AST | None = node
                while isinstance(cur, ast.Attribute):
                    attr_chain.append(cur.attr)
                    cur = cur.value
                if isinstance(cur, ast.Name):
                    attr_chain.append(cur.id)
                joined = ".".join(reversed(attr_chain))
                assert joined != "os.environ", (
                    f"{src.name} accesses os.environ at line {node.lineno}; "
                    "config flows through AgentConfig"
                )
                assert joined != "os.getenv", (
                    f"{src.name} calls os.getenv at line {node.lineno}; "
                    "config flows through AgentConfig"
                )
                assert joined != "os.environ.get", (
                    f"{src.name} calls os.environ.get at line {node.lineno}; "
                    "config flows through AgentConfig"
                )
            # Subscript access: ``os.environ["FOO"]`` / ``os.environ.get(...)``
            # patterns. The outer ``ast.Subscript`` wraps the ``os.environ``
            # attribute lookup; the inner Attribute is also caught above via
            # ast.walk, but checking the Subscript explicitly makes the intent
            # legible and bug-proofs the guard against future AST refactors.
            if isinstance(node, ast.Subscript):
                target = node.value
                if isinstance(target, ast.Attribute):
                    attr_chain = []
                    cur = target
                    while isinstance(cur, ast.Attribute):
                        attr_chain.append(cur.attr)
                        cur = cur.value
                    if isinstance(cur, ast.Name):
                        attr_chain.append(cur.id)
                    joined = ".".join(reversed(attr_chain))
                    assert joined != "os.environ", (
                        f"{src.name} subscripts os.environ[...] at line "
                        f"{node.lineno}; config flows through AgentConfig"
                    )
            # String literals: catch ``getenv("BENCH_USE_MCP")`` /
            # ``os.environ["BENCH_USE_MCP"]`` patterns even when wrapped in a
            # helper we didn't enumerate. Docstrings are still ``Str``/
            # ``Constant`` nodes but the parent statement is an ``Expr`` at the
            # head of a module/function/class — skip those.
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in forbidden_strings
            ):
                # Allow doc references — only reject when the same line names an
                # env accessor (``environ`` / ``getenv`` / ``get_env``).
                line_text = src.read_text().splitlines()[node.lineno - 1]
                accessor_hit = any(a in line_text for a in ("environ", "getenv", "get_env"))
                assert not accessor_hit, (
                    f"{src.name} reads {node.value!r} via an env accessor at line {node.lineno}"
                )


def test_api_package_does_not_expose_legacy_context_or_system_instruction() -> None:
    """The PR2 contract drops the ``context``/``system_instruction`` grab-bag.

    No symbol or kwarg with that name should remain in the public surface.
    """
    import inspect

    sig = inspect.signature(ApiAgent.run)
    assert list(sig.parameters) == ["self", "prompt", "workspace_path"], (
        "ApiAgent.run must take only (self, prompt, workspace_path); the legacy "
        "context/system_instruction kwargs are gone."
    )
    assert "context" not in sig.parameters
    assert "system_instruction" not in sig.parameters
    init_sig = inspect.signature(ApiAgent.__init__)
    # Inherited from AgentHarness — accepts config only.
    assert list(init_sig.parameters) == ["self", "config"]


@pytest.mark.parametrize("method_name", ["_execute"])
def test_execute_is_synchronous_and_safe_for_harness_invocation(method_name: str) -> None:
    """The harness calls ``run(prompt)`` synchronously; ``_execute`` must be sync too."""
    import inspect

    method = getattr(ApiAgent, method_name)
    assert not inspect.iscoroutinefunction(method), (
        f"ApiAgent.{method_name} must be synchronous so the harness can call "
        "agent.run(prompt) without managing an event loop itself."
    )


# ---------------------------------------------------------------------------
# PR3 — capability negotiation: ApiAgent implements every Supports* Protocol
# ---------------------------------------------------------------------------


def test_api_agent_satisfies_all_three_capability_protocols() -> None:
    """``isinstance`` against each Protocol is how the harness negotiates
    capabilities before granting a binding (handoff §6). ApiAgent declares
    every capability it can drive — MCP, skills, rules — so an instance must
    pass each ``runtime_checkable`` check."""
    agent = ApiAgent(AgentConfig())
    assert isinstance(agent, SupportsMcp)
    assert isinstance(agent, SupportsSkills)
    assert isinstance(agent, SupportsRules)


def test_api_agent_mirrors_capability_bindings_onto_mixin_attributes() -> None:
    """The structural-Protocol attributes (``mcp_servers``/``skills``/``rules``)
    must reflect the bindings the orchestrator granted; otherwise capability
    negotiation would see the mixin defaults instead of the live config."""
    binding = McpBinding(name="x", command=("/bin/mcp",), tools=("t",))
    caps = AllCapabilities(
        mcp_servers=(binding,),
        skills=SkillBinding(paths=("/sk",)),
        rules=AgentRules(text="rules"),
    )
    agent = ApiAgent(AgentConfig(capabilities=caps))
    assert agent.mcp_servers == (binding,)
    assert agent.skills == SkillBinding(paths=("/sk",))
    assert agent.rules == AgentRules(text="rules")


# ---------------------------------------------------------------------------
# Skills ⊥ MCP independence still holds under the binding-based config
# ---------------------------------------------------------------------------


def test_execute_runs_with_mcp_only_no_skills(monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP binding present, skills binding empty → MCP path opens, no skills loaded."""
    fc = [{"name": "do_thing", "args": {}, "id": "c1"}]
    fake = _FakeLLMClient([_Turn(text="working", calls=fc), _Turn(text="done")])
    mcp_tools = [SimpleNamespace(name="do_thing", description="d", inputSchema=None)]
    mcp = _FakeMCPClient(tools=mcp_tools)
    monkeypatch.setattr(agent_mod, "get_model", lambda *a, **kw: fake)
    monkeypatch.setattr(agent_mod, "MCPClient", lambda _path, **_kw: mcp)

    result = ApiAgent(AgentConfig(capabilities=_mcp_caps("server"))).run("p")
    assert result.errors == []
    assert mcp.entered
    assert "skills_loaded" not in result.metadata  # SkillBinding stayed empty


def test_execute_runs_with_both_mcp_and_skills(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both bindings populated → MCP session is opened *and* skills are discovered."""
    skill_dir = tmp_path / "skills"
    (skill_dir / "demo").mkdir(parents=True)
    (skill_dir / "demo" / "SKILL.md").write_text('---\nname: "demo"\ndescription: x\n---\nbody\n')

    fc = [{"name": "do_thing", "args": {}, "id": "c1"}]
    fake = _FakeLLMClient([_Turn(text="working", calls=fc), _Turn(text="done")])
    mcp = _FakeMCPClient(
        tools=[SimpleNamespace(name="do_thing", description="d", inputSchema=None)]
    )
    monkeypatch.setattr(agent_mod, "get_model", lambda *a, **kw: fake)
    monkeypatch.setattr(agent_mod, "MCPClient", lambda _path, **_kw: mcp)

    caps = AllCapabilities(
        mcp_servers=(McpBinding(name="t", command=("server",)),),
        skills=SkillBinding(paths=(str(skill_dir),)),
    )
    result = ApiAgent(AgentConfig(capabilities=caps)).run("p")
    assert result.errors == []
    assert mcp.entered
    assert result.metadata["skills_loaded"] == ["demo"]


def test_execute_runs_with_neither_mcp_nor_skills(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default capabilities → tool-less run, no MCP session, no skills loaded."""
    fake = _FakeLLMClient([_Turn(text="bare")])
    monkeypatch.setattr(agent_mod, "get_model", lambda *a, **kw: fake)
    # If MCPClient is ever entered the test would import mcp; replace with a sentinel.
    monkeypatch.setattr(
        agent_mod, "MCPClient", lambda _path, **_kw: pytest.fail("MCPClient must not be used")
    )
    result = ApiAgent(AgentConfig()).run("p")
    assert result.output == "bare"
    assert result.errors == []
    assert "skills_loaded" not in result.metadata


# ---------------------------------------------------------------------------
# Rules flow into the loop's system_instruction
# ---------------------------------------------------------------------------


def test_execute_threads_rules_text_into_system_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-empty rules text must ride on the provider's ``system_instruction``."""
    fake = _FakeLLMClient([_Turn(text="ok")])
    monkeypatch.setattr(agent_mod, "get_model", lambda *a, **kw: fake)
    caps = AllCapabilities(rules=AgentRules(text="be careful"))
    ApiAgent(AgentConfig(capabilities=caps)).run("p")
    assert fake.calls[0]["system_instruction"] == "be careful"


def test_execute_empty_rules_text_yields_none_system_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty rules text → ``system_instruction=None`` (the loop's "no preamble")."""
    fake = _FakeLLMClient([_Turn(text="ok")])
    monkeypatch.setattr(agent_mod, "get_model", lambda *a, **kw: fake)
    ApiAgent(AgentConfig()).run("p")
    assert fake.calls[0]["system_instruction"] is None


# ---------------------------------------------------------------------------
# Mcp gate: an empty-command McpBinding does NOT open an MCP session in the
# API agent (it is for CLI agents whose binary launches MCP in-process).
# ---------------------------------------------------------------------------


def test_execute_skips_mcp_when_binding_has_no_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """A binding with no launch command (CLI-agent shape) → API agent runs MCP-off."""
    fake = _FakeLLMClient([_Turn(text="ok")])
    monkeypatch.setattr(agent_mod, "get_model", lambda *a, **kw: fake)
    monkeypatch.setattr(
        agent_mod, "MCPClient", lambda _path, **_kw: pytest.fail("MCPClient must not be used")
    )
    caps = AllCapabilities(
        mcp_servers=(McpBinding(name="cli-shape", command=(), tools=("x",)),),
    )
    result = ApiAgent(AgentConfig(capabilities=caps)).run("p")
    assert result.output == "ok"


def test_execute_preserves_spaced_command_token_through_shlex_roundtrip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spaced argv token (``("uv run", "mcp-server")``) must reach MCPClient
    intact: ``shlex.join`` quotes it on the way in, ``MCPClient``'s
    ``shlex.split`` recovers the original parts on the way out.

    Regression for the lossy ``" ".join`` that previously silently expanded
    one token into two when the binding carried a spaced word (e.g. a launch
    command that wraps an interpreter invocation).
    """
    import shlex

    captured: dict = {}

    class _RecordingMCP(_FakeMCPClient):
        def __init__(self, path: str, **_kw: Any) -> None:
            super().__init__(tools=[])
            captured["path"] = path

    fake = _FakeLLMClient([_Turn(text="done")])
    monkeypatch.setattr(agent_mod, "get_model", lambda *a, **kw: fake)
    monkeypatch.setattr(agent_mod, "MCPClient", _RecordingMCP)

    original = ("uv run", "mcp-server", "--flag")
    caps = AllCapabilities(
        mcp_servers=(McpBinding(name="spaced", command=original),),
    )
    ApiAgent(AgentConfig(capabilities=caps)).run("p")

    # MCPClient calls ``shlex.split`` on its ``server_path``; re-splitting the
    # path the agent handed it must recover the original tuple element-for-
    # element, including the spaced first token.
    assert tuple(shlex.split(captured["path"])) == original


def _recording_mcp(captured: dict) -> type:
    """Return an ``MCPClient`` stand-in recording the kwargs it was built with."""

    class _Recorder(_FakeMCPClient):
        def __init__(self, path: str, **kw: Any) -> None:
            super().__init__(tools=[])
            captured.update(path=path, **kw)

    return _Recorder


def test_execute_threads_declared_env_and_cwd_to_the_mcp_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A binding's declared ``env`` and ``cwd`` must reach the server process.

    ``${VAR}`` references resolve against the runner env, and the resolved pair
    is layered *over* that env rather than replacing it: the SDK swaps the child
    environment outright for any mapping it is given, so a bare ``{"TOKEN": ...}``
    would launch the server without ``PATH`` and it would not start.
    """
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("REAL_TOKEN", "s3cret")
    captured: dict = {}
    fake = _FakeLLMClient([_Turn(text="done")])
    monkeypatch.setattr(agent_mod, "get_model", lambda *a, **kw: fake)
    monkeypatch.setattr(agent_mod, "MCPClient", _recording_mcp(captured))

    caps = AllCapabilities(
        mcp_servers=(
            McpBinding(
                name="gh",
                command=("gh-mcp",),
                env=(("GH_TOKEN", "${REAL_TOKEN}"), ("MODE", "read-only")),
                cwd="/srv/work",
            ),
        ),
    )
    ApiAgent(AgentConfig(capabilities=caps)).run("p")

    assert captured["cwd"] == "/srv/work"
    assert captured["env"]["GH_TOKEN"] == "s3cret"
    assert captured["env"]["MODE"] == "read-only"
    assert captured["env"]["PATH"] == "/usr/bin"


def test_execute_leaves_mcp_child_env_unset_when_binding_declares_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No declared env means ``env=None`` — the SDK's own minimal default, not
    a copy of the runner env, so nothing incidental leaks into the server."""
    captured: dict = {}
    fake = _FakeLLMClient([_Turn(text="done")])
    monkeypatch.setattr(agent_mod, "get_model", lambda *a, **kw: fake)
    monkeypatch.setattr(agent_mod, "MCPClient", _recording_mcp(captured))

    caps = AllCapabilities(mcp_servers=(McpBinding(name="plain", command=("srv",)),))
    ApiAgent(AgentConfig(capabilities=caps)).run("p")

    assert captured["env"] is None
    assert captured["cwd"] is None
