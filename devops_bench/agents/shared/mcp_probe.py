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

"""Pre-run reachability probe for granted MCP servers.

Probes each stdio MCP server over JSON-RPC (``initialize`` -> ``tools/list``)
before the agent is invoked so an unreachable or tool-less server fails the run
instead of silently falling back to shell tools.
"""

from __future__ import annotations

import collections
import contextlib
import json
import os
import queue
import re
import signal
import subprocess
import threading
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import IO, TYPE_CHECKING

from devops_bench.core import DevOpsBenchError, get_logger

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from devops_bench.agents.capabilities import McpBinding

__all__ = [
    "McpUnreachableError",
    "child_env",
    "expand_env",
    "preflight_mcp",
    "probe_stdio_server",
]

_log = get_logger("agents.shared.mcp_probe")

PROBE_TIMEOUT_SEC = 30.0
_MAX_CONCURRENT_PROBES = 8
_SIGNAL_GRACE_SEC = 2.0
_READER_JOIN_SEC = 2.0
_MAX_STDOUT_LINES = 1000
_MAX_STDERR_CHUNKS = 64
_STDERR_TAIL_CHARS = 500
_PROTOCOL_VERSION = "2025-06-18"
_CLIENT_INFO = {"name": "devops-bench-preflight", "version": "1"}
# Braces are required so literal `$` characters in values are not mangled.
_ENV_REF = re.compile(r"\$\{(\w+)\}")


class McpUnreachableError(DevOpsBenchError):
    """A granted MCP server did not complete the handshake."""


def expand_env(
    value: str,
    *,
    source: Mapping[str, str],
    missing: list[str] | None = None,
) -> str:
    """Substitute ``${VAR}`` references in ``value`` from ``source``.

    Args:
        value: The raw declared value.
        source: Mapping references are resolved against.
        missing: Optional list collecting names absent from ``source``, in
            encounter order. Unresolved references expand to ``""``.

    Returns:
        ``value`` with every reference substituted.
    """

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in source:
            if missing is not None and name not in missing:
                missing.append(name)
            return ""
        return source[name]

    return _ENV_REF.sub(_sub, value)


def child_env(binding: McpBinding, base_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build the child environment for ``binding``, resolving ``${VAR}`` references.

    Args:
        binding: The server whose declared ``env`` is resolved.
        base_env: Mapping references resolve against; defaults to ``os.environ``.

    Returns:
        The full environment the server subprocess should be launched with.

    Raises:
        McpUnreachableError: If a declared reference is absent from ``base_env``.
    """
    if base_env is None:
        base_env = os.environ
    env = dict(base_env)
    missing: list[str] = []
    for key, raw in binding.env:
        env[key] = expand_env(raw, source=base_env, missing=missing)
    if missing:
        raise McpUnreachableError(f"references unset environment variable(s): {', '.join(missing)}")
    return env


def _secret_values(binding: McpBinding, source: Mapping[str, str]) -> tuple[str, ...]:
    """Return distinct ``${VAR}`` values expanded for ``binding``, longest first."""
    secrets = {
        source[name]
        for _key, raw in binding.env
        for name in _ENV_REF.findall(raw)
        if source.get(name)
    }
    return tuple(sorted(secrets, key=len, reverse=True))


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    """Replace every expanded secret in ``text`` with ``***``."""
    for secret in secrets:
        text = text.replace(secret, "***")
    return text


def _pump(stream: IO[str], sink: queue.Queue[str | None]) -> None:
    """Forward lines from ``stream`` to ``sink`` (evicting oldest when full), then ``None``."""
    dropped = 0
    try:
        for line in stream:
            if _put_evicting_oldest(sink, line):
                dropped += 1
    finally:
        if dropped:
            _log.warning(
                "MCP probe discarded %d stdout line(s) past the %d-line cap",
                dropped,
                _MAX_STDOUT_LINES,
            )
        _put_evicting_oldest(sink, None)


def _put_evicting_oldest(sink: queue.Queue[str | None], item: str | None) -> bool:
    """Enqueue ``item`` without blocking, discarding the oldest item if full."""
    try:
        sink.put_nowait(item)
        return False
    except queue.Full:
        with contextlib.suppress(queue.Empty):
            sink.get_nowait()
        with contextlib.suppress(queue.Full):
            sink.put_nowait(item)
        return True


def _drain_stderr(stream: IO[str], sink: collections.deque[str]) -> None:
    """Collect ``stream``'s tail into ``sink`` line by line."""
    try:
        for line in stream:
            sink.append(line)
    except (OSError, ValueError):  # pragma: no cover - stream torn down mid-read
        pass


def _read_result(lines: queue.Queue[str | None], request_id: int, deadline: float) -> dict:
    """Read newline-delimited JSON-RPC until the response for ``request_id`` arrives.

    Non-JSON lines, notifications, and requests (messages carrying ``method``)
    are skipped so stdout log noise or an echo process cannot satisfy the probe.

    Raises:
        McpUnreachableError: On timeout, premature EOF, or a JSON-RPC error reply.
    """
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise McpUnreachableError(f"timed out waiting for response to request {request_id}")
        try:
            line = lines.get(timeout=remaining)
        except queue.Empty:
            raise McpUnreachableError(
                f"timed out waiting for response to request {request_id}"
            ) from None
        if line is None:
            raise McpUnreachableError("server closed its stdout before responding")
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict) or message.get("id") != request_id:
            continue
        if "method" in message or ("result" not in message and "error" not in message):
            continue
        error = message.get("error")
        if error is not None:
            raise McpUnreachableError(f"server returned an error: {error}")
        result = message.get("result")
        return result if isinstance(result, dict) else {}


def probe_stdio_server(
    binding: McpBinding,
    *,
    base_env: Mapping[str, str] | None = None,
    timeout: float = PROBE_TIMEOUT_SEC,
    cwd: str | os.PathLike[str] | None = None,
) -> tuple[str, ...]:
    """Launch ``binding``'s server, handshake, and return its advertised tools.

    Args:
        binding: The stdio binding to probe; ``command`` must be non-empty.
        base_env: Environment the server is launched with and whose values
            resolve ``${VAR}`` references. Defaults to ``os.environ``.
        timeout: Wall-clock budget for launch plus handshake.
        cwd: Fallback working directory when ``binding.cwd`` is unset.

    Returns:
        The advertised tool names in server order.

    Raises:
        McpUnreachableError: If launch or handshake fails, times out, returns a
            JSON-RPC error, or advertises no tools.
    """
    source = os.environ if base_env is None else base_env
    env = child_env(binding, source)
    secrets = _secret_values(binding, source)
    deadline = time.monotonic() + timeout
    try:
        proc = subprocess.Popen(  # noqa: S603 - argv list, never a shell string
            list(binding.command),
            cwd=binding.cwd or cwd or None,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,
        )
    except OSError as exc:
        raise McpUnreachableError(f"could not launch server: {exc}") from exc

    lines: queue.Queue[str | None] = queue.Queue(maxsize=_MAX_STDOUT_LINES)
    stderr_tail: collections.deque[str] = collections.deque(maxlen=_MAX_STDERR_CHUNKS)
    readers = (
        threading.Thread(target=_pump, args=(proc.stdout, lines), daemon=True),
        threading.Thread(target=_drain_stderr, args=(proc.stderr, stderr_tail), daemon=True),
    )
    for reader in readers:
        reader.start()

    failure: McpUnreachableError | None = None
    listing: dict = {}
    try:
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": _init_params()})
        _read_result(lines, 1, deadline)
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listing = _read_result(lines, 2, deadline)
    except McpUnreachableError as exc:
        failure = exc
    finally:
        # Terminate before reading stderr_tail so the stderr reader flushes on
        # timeout and teardown only runs once.
        _terminate(proc, readers)

    if failure is not None:
        stderr = _redact("".join(stderr_tail).strip(), secrets)
        detail = f"{failure}; stderr: {stderr[-_STDERR_TAIL_CHARS:]}" if stderr else str(failure)
        raise McpUnreachableError(detail) from failure

    tools = listing.get("tools")
    names = (
        tuple(t["name"] for t in tools if isinstance(t, dict) and isinstance(t.get("name"), str))
        if isinstance(tools, list)
        else ()
    )
    if not names:
        raise McpUnreachableError("server completed the handshake but advertised no tools")
    return names


def _init_params() -> dict:
    """Return the ``initialize`` params this probe sends."""
    return {
        "protocolVersion": _PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": dict(_CLIENT_INFO),
    }


def _send(proc: subprocess.Popen, message: dict) -> None:
    """Write one newline-delimited JSON-RPC message to the server's stdin."""
    try:
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()
    except (OSError, ValueError) as exc:
        raise McpUnreachableError(f"server exited before the handshake completed: {exc}") from exc


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    """Send ``sig`` to the server's process group, falling back to the child.

    ``start_new_session=True`` sets ``pgid == proc.pid``, which remains valid in
    the kernel while any descendant is alive even after the leader has exited.
    """
    try:
        os.killpg(proc.pid, sig)
    except (OSError, AttributeError):
        if proc.poll() is None:
            with contextlib.suppress(OSError):
                proc.send_signal(sig)


def _terminate(proc: subprocess.Popen, readers: tuple[threading.Thread, ...] = ()) -> None:
    """Stop the probed server process group and join its pipe readers."""
    if proc.stdin is not None:
        with contextlib.suppress(OSError):
            proc.stdin.close()

    _signal_group(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=_SIGNAL_GRACE_SEC)
    except subprocess.TimeoutExpired:
        _signal_group(proc, signal.SIGKILL)
        try:
            proc.wait(timeout=_SIGNAL_GRACE_SEC)
        except subprocess.TimeoutExpired:  # pragma: no cover - unkillable child
            _log.warning("MCP probe child %d survived SIGKILL", proc.pid)

    stuck = False
    for reader in readers:
        reader.join(timeout=_READER_JOIN_SEC)
        stuck = stuck or reader.is_alive()

    # A wrapper launcher may exit on SIGTERM while its child ignores SIGTERM and
    # keeps the pipe open; escalate SIGKILL to the process group so no child
    # holds locks or ports when the CLI launches the server for real.
    if stuck:
        _signal_group(proc, signal.SIGKILL)
        stuck = False
        for reader in readers:
            reader.join(timeout=_READER_JOIN_SEC)
            stuck = stuck or reader.is_alive()

    if stuck:
        _log.warning(
            "MCP probe reader still blocked after the server was killed; "
            "leaving its pipes open to avoid a hang"
        )
        return

    for stream in (proc.stdout, proc.stderr):
        try:
            if stream is not None:
                stream.close()
        except OSError:
            pass


def preflight_mcp(
    bindings: tuple[McpBinding, ...],
    *,
    base_env: Mapping[str, str] | None = None,
    timeout: float = PROBE_TIMEOUT_SEC,
    cwd: str | os.PathLike[str] | None = None,
) -> dict[str, tuple[str, ...]]:
    """Verify every launchable binding answers, or fail the run.

    Args:
        bindings: The MCP bindings granted for the run (empty-command bindings
            are skipped).
        base_env: Environment the servers are launched with.
        timeout: Per-server wall-clock budget.
        cwd: Fallback working directory when a binding does not pin one.

    Returns:
        A ``{server name: advertised tool names}`` mapping for every probed
        binding.

    Raises:
        McpUnreachableError: If any probed server fails, naming the first
            failure in binding order.
    """
    probed = [
        (binding.name or f"mcp{index}", binding)
        for index, binding in enumerate(bindings)
        if binding.command
    ]
    if not probed:
        return {}

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=min(_MAX_CONCURRENT_PROBES, len(probed))) as pool:
        outcomes = list(
            pool.map(
                lambda item: _probe_one(item[1], base_env=base_env, timeout=timeout, cwd=cwd),
                probed,
            )
        )

    discovered: dict[str, tuple[str, ...]] = {}
    for (name, _binding), outcome in zip(probed, outcomes, strict=True):
        if isinstance(outcome, McpUnreachableError):
            raise McpUnreachableError(f"MCP server {name!r} is unreachable: {outcome}")
        discovered[name] = outcome
    _log.info(
        "MCP preflight ok: %d server(s) in %.1fs (%s)",
        len(discovered),
        time.monotonic() - started,
        ", ".join(f"{name}={len(tools)} tool(s)" for name, tools in discovered.items()),
    )
    return discovered


def _probe_one(
    binding: McpBinding,
    *,
    base_env: Mapping[str, str] | None,
    timeout: float,
    cwd: str | os.PathLike[str] | None,
) -> tuple[str, ...] | McpUnreachableError:
    """Probe one binding, returning its failure so callers report in binding order."""
    try:
        return probe_stdio_server(binding, base_env=base_env, timeout=timeout, cwd=cwd)
    except McpUnreachableError as exc:
        return exc
