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

"""Async MCP client wrapping the ``mcp`` SDK over a stdio server transport."""

from __future__ import annotations

import shlex
from contextlib import AsyncExitStack
from types import TracebackType
from typing import Any

from devops_bench.core import get_logger

__all__ = ["MCPClient", "extract_tool_text"]

_log = get_logger("agents.api.mcp")


class MCPClient:
    """Async context manager over an MCP stdio server.

    Spawns the MCP server given by ``server_path``, initializes a session, and
    exposes tool discovery and invocation. The ``mcp`` SDK is imported lazily on
    ``__aenter__`` so merely importing this module never requires the SDK.

    Attributes:
        server_path: Command used to launch the MCP server over stdio.
        session: The active ``ClientSession`` once entered, else ``None``.

    Raises:
        ImportError: If the ``mcp`` package is missing (a broken install —
            ``mcp`` is a required dependency; on enter).
        ValueError: If ``server_path`` is empty or whitespace-only (on enter).
    """

    def __init__(self, server_path: str) -> None:
        self.server_path = server_path
        self.exit_stack = AsyncExitStack()
        self.session: Any = None

    async def __aenter__(self) -> MCPClient:
        try:
            from mcp.client.session import ClientSession
            from mcp.client.stdio import StdioServerParameters, stdio_client
        except ImportError as exc:  # pragma: no cover - broken install only
            raise ImportError(
                "The API agent's MCP client needs the 'mcp' package, a required "
                "dependency of devops-bench; reinstall the package to restore it."
            ) from exc

        # Split the command so a multi-word server_path (e.g. "uv run mcp-server")
        # is spawned as program + args rather than a single executable name; the
        # stdio transport spawns without a shell, so an unsplit command would fail
        # with FileNotFoundError.
        parts = shlex.split(self.server_path or "")
        if not parts:
            raise ValueError(
                "MCP server_path is empty; set AGENT_TARGET/MCP_SERVER_PATH to the "
                "MCP server command."
            )
        server_params = StdioServerParameters(command=parts[0], args=parts[1:])
        # Unwind anything already entered (e.g. the spawned server subprocess)
        # when a later setup step fails — __aexit__ never runs if __aenter__
        # raises, so cleanup must happen here.
        try:
            stdio_transport = await self.exit_stack.enter_async_context(stdio_client(server_params))
            self.read_stream, self.write_stream = stdio_transport
            self.session = await self.exit_stack.enter_async_context(
                ClientSession(self.read_stream, self.write_stream)
            )
            await self.session.initialize()
        except BaseException:
            await self.exit_stack.aclose()
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.exit_stack.aclose()

    async def list_tools(self) -> Any:
        """List the tools advertised by the connected MCP server.

        Returns:
            The raw ``list_tools`` result whose ``.tools`` is the tool list.
        """
        return await self.session.list_tools()

    async def call_tool(self, name: str, arguments: dict) -> Any:
        """Invoke an MCP tool by name.

        DeepEval's ``@observe`` wrapper is applied when available so MCP calls
        appear in traces; the import is best-effort and a missing ``deepeval``
        degrades gracefully to an untraced call.

        Args:
            name: Tool name as advertised by the server.
            arguments: Keyword arguments for the tool.

        Returns:
            The raw tool-call result from the MCP session.
        """
        try:
            from deepeval.tracing import observe
        except ImportError:
            return await self.session.call_tool(name, arguments=arguments)

        @observe(span_type="TOOL")
        async def _call() -> Any:
            return await self.session.call_tool(name, arguments=arguments)

        return await _call()


def extract_tool_text(tool_result: Any) -> str:
    """Aggregate the text of every content block in an MCP tool result.

    Args:
        tool_result: The raw result returned by :meth:`MCPClient.call_tool`.

    Returns:
        The newline-joined text of all blocks exposing a ``text`` attribute. If
        the result has no ``content`` blocks (or none carry text), the result is
        stringified instead.
    """
    content = getattr(tool_result, "content", None)
    if content:
        texts = [block.text for block in content if hasattr(block, "text")]
        if texts:
            return "\n".join(texts)
    return str(tool_result)
