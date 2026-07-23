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

"""Unit tests for :mod:`devops_bench.agents.api.mcp`."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from devops_bench.agents.api.mcp import MCPClient, extract_tool_text


def test_extract_tool_text_joins_text_blocks() -> None:
    blocks = [SimpleNamespace(text="alpha"), SimpleNamespace(text="beta")]
    assert extract_tool_text(SimpleNamespace(content=blocks)) == "alpha\nbeta"


def test_extract_tool_text_skips_blocks_without_text() -> None:
    # A block missing ``.text`` must not crash; mixed lists fall through to the
    # text-only blocks.
    blocks = [SimpleNamespace(text="alpha"), SimpleNamespace()]
    assert extract_tool_text(SimpleNamespace(content=blocks)) == "alpha"


def test_extract_tool_text_falls_back_to_str_when_no_blocks() -> None:
    obj = SimpleNamespace(content=[])
    assert extract_tool_text(obj) == str(obj)


def test_extract_tool_text_falls_back_to_str_when_no_content_attr() -> None:
    obj = SimpleNamespace()
    assert extract_tool_text(obj) == str(obj)


def test_mcp_client_enter_rejects_empty_server_path() -> None:
    # We must surface a clear error rather than spawning ``""``; ValueError
    # surfaces through the agent's _execute as an errored result (see
    # test_execute_returns_errored_on_empty_mcp_server_string).
    client = MCPClient("   ")
    with pytest.raises(ValueError, match="MCP server_path is empty"):
        asyncio.run(client.__aenter__())


def test_aenter_unwinds_exit_stack_when_session_setup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If session setup fails after the stdio transport spawned the server,
    ``__aenter__`` must unwind its exit stack — ``__aexit__`` never runs when
    ``__aenter__`` raises, so without the unwind the server subprocess leaks
    (regression for the review finding)."""
    import mcp.client.session as session_mod
    import mcp.client.stdio as stdio_mod

    state = {"transport_exited": False}

    class _Transport:
        def __init__(self, _params) -> None: ...

        async def __aenter__(self):
            return (SimpleNamespace(), SimpleNamespace())

        async def __aexit__(self, *_a) -> None:
            state["transport_exited"] = True

    class _BoomSession:
        def __init__(self, _r, _w) -> None: ...

        async def __aenter__(self):
            raise RuntimeError("handshake failed")

        async def __aexit__(self, *_a) -> None: ...

    monkeypatch.setattr(stdio_mod, "stdio_client", lambda params: _Transport(params))
    monkeypatch.setattr(session_mod, "ClientSession", _BoomSession)

    client = MCPClient("server")
    with pytest.raises(RuntimeError, match="handshake failed"):
        asyncio.run(client.__aenter__())
    assert state["transport_exited"] is True


def test_call_tool_degrades_gracefully_when_deepeval_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``deepeval`` is not installed, ``call_tool`` still works (untraced).

    The legacy implementation imported ``deepeval.tracing.observe`` at the top
    of ``call_tool``, so a host without deepeval would crash with ImportError
    on the first tool call. The fix mirrors
    :func:`devops_bench.agents.base._maybe_observe`: try the import, fall
    through to the bare ``call_tool`` on failure.
    """
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("deepeval"):
            raise ImportError(f"simulated missing dependency: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    sentinel = SimpleNamespace(content=[SimpleNamespace(text="ok")])

    class _FakeSession:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def call_tool(self, name: str, arguments: dict) -> SimpleNamespace:
            self.calls.append((name, arguments))
            return sentinel

    client = MCPClient("does-not-matter")
    client.session = _FakeSession()

    result = asyncio.run(client.call_tool("t", {"k": "v"}))
    assert result is sentinel
    assert client.session.calls == [("t", {"k": "v"})]


def test_mcp_module_does_not_import_sdk_at_module_load() -> None:
    """Importing the module must not pull the ``mcp`` SDK — it loads on enter."""
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent(
        """
        import sys
        import devops_bench.agents.api.mcp  # noqa: F401
        forbidden = ("mcp.client", "mcp.server", "deepeval", "anthropic",
                     "google.genai", "openai")
        hits = sorted(
            m for m in sys.modules
            if any(m == p or m.startswith(p + ".") for p in forbidden)
        )
        if hits:
            sys.stderr.write("LEAKED:" + ",".join(hits))
            sys.exit(1)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
