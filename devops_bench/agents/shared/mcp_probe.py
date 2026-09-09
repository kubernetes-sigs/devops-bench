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

A CLI agent that is handed an MCP server which never starts exits 0 with an
empty error list: the model silently falls back to shell tools and the run is
still recorded as an MCP arm. :func:`preflight_mcp` closes that hole by
speaking the MCP stdio handshake directly — ``initialize`` /
``notifications/initialized`` / ``tools/list`` as newline-delimited JSON-RPC —
before the agent is invoked, so an unreachable server fails the run instead of
scoring one.

The handshake is hand-rolled rather than driven through the ``mcp`` SDK (which
the project already depends on) because the SDK's client is async and manages
the child process for you, while this probe needs synchronous control of the
launch: its own process group, a hard wall-clock deadline, and the child's
stderr tail to report on failure. It is ~3 messages over a line-based transport.
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

# Wall-clock budget per server. Servers are probed concurrently, so this is the
# budget for the slowest one rather than a per-run sum. A cold ``uvx``/``npx``
# package fetch can exceed it; warm the launcher cache during host setup instead
# of paying a cold-fetch budget on every run of the matrix.
PROBE_TIMEOUT_SEC = 30.0

# Servers probed at once. Bounded so a large config cannot fork a process storm.
_MAX_CONCURRENT_PROBES = 8

# Grace given to a signalled server before escalating, and to a reader thread
# before its stream is abandoned.
_SIGNAL_GRACE_SEC = 2.0
_READER_JOIN_SEC = 2.0

# Caps on what a misbehaving server can accumulate in this process. Only the
# stderr tail is ever reported, so older output is discarded as it arrives.
_MAX_STDOUT_LINES = 1000
_MAX_STDERR_CHUNKS = 64
_STDERR_TAIL_CHARS = 500

# Protocol revision this probe advertises. Servers negotiate down, so an older
# server still answers; the reply's version is not enforced.
_PROTOCOL_VERSION = "2025-06-18"

_CLIENT_INFO = {"name": "devops-bench-preflight", "version": "1"}

# ``${VAR}`` reference in an MCP server's declared env value. Braces are
# required: a bare ``$VAR`` form would silently mangle any literal value that
# happens to contain a dollar sign (``p$ssw0rd`` -> ``p``) and would leak the
# fragment after the ``$`` into the "unset variable" error.
_ENV_REF = re.compile(r"\$\{(\w+)\}")


class McpUnreachableError(DevOpsBenchError):
    """A granted MCP server did not complete the handshake."""


def expand_env(
    value: str,
    *,
    source: Mapping[str, str],
    missing: list[str] | None = None,
) -> str:
    """Substitute ``${VAR}`` references from ``source``.

    MCP configs carry secrets by reference, never by value, so the token stays
    out of any file written into the agent's workspace (which is collected
    wholesale into the run's artifacts). This resolves those references for the
    probe's own child process only.

    Args:
        value: The raw declared value.
        source: Mapping references are resolved against (the runner env).
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
    """Build the child environment for ``binding``, resolving secret references.

    The runner env is the base rather than the declared vars alone: a stdio
    server is a subprocess that still needs ``PATH``, ``HOME``, and the run's
    isolation vars to start at all.

    Args:
        binding: The server whose declared ``env`` is resolved.
        base_env: Mapping references resolve against; defaults to ``os.environ``.
            The API agent routes through here precisely so it holds no
            ``os.environ`` read of its own.

    Returns:
        The full environment the server subprocess should be launched with.

    Raises:
        McpUnreachableError: If a declared reference is absent from ``base_env``.
            Fail here rather than launch the server with an empty credential and
            report the resulting auth error as "unreachable".
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
    """Return the values ``${VAR}`` expansion injected, for redaction.

    Only values that came from a reference are returned: a literal declared in
    the config is already visible to anyone reading the config, whereas an
    expanded reference is a credential this process resolved and must not echo
    back into a run artifact.

    Each *substituted value* is collected, not the declared value it was spliced
    into. A binding declaring ``AUTH="Bearer ${TOKEN}"`` must redact ``ghp_...``
    on its own, because a server rejecting the credential echoes the bare token
    far more often than the whole header.

    Args:
        binding: The binding whose declared ``env`` is scanned for references.
        source: Mapping the references resolve against (the runner env).

    Returns:
        The distinct substituted values, longest first so a secret that contains
        another is replaced before its substring.
    """
    secrets = {
        source[name]
        for _key, raw in binding.env
        for name in _ENV_REF.findall(raw)
        if source.get(name)
    }
    return tuple(sorted(secrets, key=len, reverse=True))


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    """Replace every expanded secret in ``text`` with ``***``.

    Probe failures embed the server's stderr tail, and a server that rejects a
    credential routinely echoes it (``input_value='ghp_...'``). That text reaches
    ``AgentResult.errors`` and is persisted to the run's ``results.json``, so it
    is scrubbed before it can become an artifact.
    """
    for secret in secrets:
        text = text.replace(secret, "***")
    return text


def _pump(stream: IO[str], sink: queue.Queue[str | None]) -> None:
    """Forward each line of ``stream`` onto ``sink``, then a ``None`` sentinel.

    Never blocks on a full queue: this thread outlives the probe's own deadline,
    so a wait here would strand it after teardown. Instead the *oldest* queued
    line is evicted to make room. That direction matters — the probe reads a
    reply only after it has sent the request, so everything the server logged
    beforehand is noise and the awaited reply is always the newest line. Dropping
    the newest would discard the reply itself and report a merely chatty server
    as unreachable.

    Bounding the queue keeps a server that chatters for its whole timeout budget
    from growing this process without bound.
    """
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
    """Enqueue ``item`` without blocking, discarding the head if the queue is full.

    Returns:
        ``True`` if a queued item had to be discarded to make room.
    """
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
    """Collect ``stream``'s tail into ``sink`` line by line.

    Line-at-a-time rather than one blocking ``read()`` so the tail is available
    as soon as the process is signalled, instead of only at EOF — the timeout
    path needs it and never reaches EOF on its own.
    """
    try:
        for line in stream:
            sink.append(line)
    except (OSError, ValueError):  # pragma: no cover - stream torn down mid-read
        pass


def _read_result(lines: queue.Queue[str | None], request_id: int, deadline: float) -> dict:
    """Read newline-delimited JSON-RPC until the reply to ``request_id`` arrives.

    Non-JSON lines and unrelated messages (notifications, other ids) are skipped:
    some servers log to stdout despite the spec reserving it for the protocol.
    A match must be a *response* — carrying ``result`` or ``error`` and no
    ``method`` — so a process that merely echoes stdin back (``cat``) cannot pass
    the handshake by replaying the request's id.

    Raises:
        McpUnreachableError: On timeout, premature stream end, or an error reply.
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
            resolve the binding's ``${VAR}`` references. Defaults to
            ``os.environ``.
        timeout: Wall-clock budget for launch plus handshake.
        cwd: Directory to launch the server in when the binding does not pin one.
            Pass the run's workspace so the probe launches the server exactly as
            the CLI later will; a relative path in ``args`` resolves the same way
            in both, instead of probing green and failing under the CLI.

    Returns:
        The tool names the server advertises, in the order it lists them.

    Raises:
        McpUnreachableError: If the server cannot be launched, does not complete
            the handshake within ``timeout``, or answers with a JSON-RPC error,
            or completes the handshake advertising no tools.
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
            bufsize=1,
            # Own process group, so _terminate can signal the whole tree. A
            # launcher (uvx/npx/sh -c/docker) spawns the real server as a child
            # that inherits these pipes; signalling only the launcher leaves the
            # grandchild holding the write end, and the reader threads then never
            # reach EOF.
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

    try:
        try:
            _send(
                proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": _init_params()}
            )
            _read_result(lines, 1, deadline)
            _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            listing = _read_result(lines, 2, deadline)
        except McpUnreachableError as exc:
            # Signal first: on a timeout the server is still running and its
            # stderr reader has not reached EOF, so the tail is empty until the
            # process is stopped — and the timeout path is exactly the one whose
            # cause the stderr explains.
            _terminate(proc, readers)
            stderr = _redact("".join(stderr_tail).strip(), secrets)
            detail = f"{exc}; stderr: {stderr[-_STDERR_TAIL_CHARS:]}" if stderr else str(exc)
            raise McpUnreachableError(detail) from exc
    finally:
        _terminate(proc, readers)

    tools = listing.get("tools")
    names = (
        tuple(t["name"] for t in tools if isinstance(t, dict) and isinstance(t.get("name"), str))
        if isinstance(tools, list)
        else ()
    )
    if not names:
        # A granted server exposing nothing is the tool-less arm this probe
        # exists to catch: the model would fall back to shell tools and the run
        # would still score as an MCP arm.
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
    """Write one newline-delimited JSON-RPC message to the server's stdin.

    Raises:
        McpUnreachableError: If the server's stdin is already closed — i.e. the
            process died on launch. ``OSError`` covers the whole family the pipe
            can fail with (``BrokenPipeError`` when the reader is gone,
            ``ConnectionResetError`` when it is torn down mid-write); ``ValueError``
            is what a closed file object raises.
    """
    try:
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()
    except (OSError, ValueError) as exc:
        raise McpUnreachableError(f"server exited before the handshake completed: {exc}") from exc


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    """Send ``sig`` to the server's whole process group, falling back to the child.

    The group is what matters: launcher-style commands (``uvx``, ``npx -y``,
    ``sh -c``, ``docker run``) exec or fork the real server, and signalling only
    the direct child leaves that descendant alive holding the inherited pipes.
    """
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (OSError, AttributeError):
        with contextlib.suppress(OSError):
            proc.send_signal(sig)


def _terminate(proc: subprocess.Popen, readers: tuple[threading.Thread, ...] = ()) -> None:
    """Stop the probed server and release its pipes, always within a bounded time.

    Ordering is load-bearing. ``TextIOWrapper.close()`` must take the buffer lock
    a blocked reader thread holds, and that reader only unblocks at EOF, which
    needs *every* holder of the pipe's write end to exit. So the process group is
    signalled first, then the readers are joined with a deadline, and any stream
    whose reader is still alive is abandoned rather than closed — a leaked file
    descriptor is recoverable, an unbounded hang on the per-run path is not.

    Idempotent: called on both the success and failure paths of a single probe.
    """
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

    streams = (proc.stdin,) if stuck else (proc.stdin, proc.stdout, proc.stderr)
    if stuck:
        _log.warning(
            "MCP probe reader still blocked after the server was killed; "
            "leaving its pipes open to avoid a hang"
        )
    for stream in streams:
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

    Bindings with an empty ``command`` are skipped: they denote a server the CLI
    binary hosts itself, which has no process for this probe to speak to.

    Servers are probed concurrently — they are independent, and probing in
    sequence would make the worst case the sum of every server's timeout on a
    path that runs before each of the matrix's runs.

    Args:
        bindings: The MCP bindings granted for the run.
        base_env: Environment the servers are launched with (defaults to
            ``os.environ``).
        timeout: Per-server wall-clock budget.
        cwd: Directory to launch servers in when a binding does not pin one;
            pass the run's workspace so the probe matches the CLI's launch.

    Returns:
        A ``{server name: advertised tool names}`` mapping for every probed
        binding.

    Raises:
        McpUnreachableError: If any probed server fails, naming the first
            failure in binding order so the message is stable across runs.
            Raised rather than reported so a granted-but-dead server can never
            be scored as a working MCP arm.
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
    """Probe one binding, returning its failure instead of raising.

    Returning the error keeps the concurrent pool from surfacing whichever
    server happened to fail first; the caller reports failures in binding order.
    """
    try:
        return probe_stdio_server(binding, base_env=base_env, timeout=timeout, cwd=cwd)
    except McpUnreachableError as exc:
        return exc
