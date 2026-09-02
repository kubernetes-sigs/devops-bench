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

"""Unit tests for devops_bench.agents.shared.mcp_probe."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from devops_bench.agents.capabilities import McpBinding
from devops_bench.agents.shared.mcp_probe import (
    McpUnreachableError,
    expand_env,
    preflight_mcp,
    probe_stdio_server,
)

# A minimal stdio MCP server: answers ``initialize`` and ``tools/list`` with
# newline-delimited JSON-RPC, exactly as the transport specifies.
_FAKE_SERVER = """
import json, os, sys

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                          "result": {"protocolVersion": "2025-06-18",
                                     "capabilities": {},
                                     "serverInfo": {"name": "fake", "version": "1"}}}),
              flush=True)
    elif method == "tools/list":
        names = os.environ.get("FAKE_TOOLS", "alpha,beta").split(",")
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                          "result": {"tools": [{"name": n} for n in names]}}), flush=True)
    elif method == "notifications/initialized":
        continue
"""

# Speaks the protocol but reports a working directory rather than tool names,
# so a test can assert the ``cwd`` a binding requested was honored.
_CWD_SERVER = _FAKE_SERVER.replace(
    'os.environ.get("FAKE_TOOLS", "alpha,beta").split(",")', "[os.getcwd()]"
)


def _server(tmp_path: Path, source: str, name: str = "server.py") -> tuple[str, ...]:
    """Write ``source`` as a script and return the argv that launches it."""
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    return (sys.executable, str(script))


def test_probe_returns_advertised_tool_names(tmp_path: Path) -> None:
    """A server completing the handshake yields the tools it lists."""
    binding = McpBinding(name="fake", command=_server(tmp_path, _FAKE_SERVER))

    assert probe_stdio_server(binding, timeout=30) == ("alpha", "beta")


def test_probe_launches_server_with_declared_env(tmp_path: Path) -> None:
    """A binding's ``env`` reaches the server process."""
    binding = McpBinding(
        name="fake",
        command=_server(tmp_path, _FAKE_SERVER),
        env=(("FAKE_TOOLS", "only_one"),),
    )

    assert probe_stdio_server(binding, timeout=30) == ("only_one",)


def test_probe_resolves_secret_references_from_the_runner_env(tmp_path: Path) -> None:
    """``${VAR}`` in a declared value is resolved from the launching environment."""
    binding = McpBinding(
        name="fake",
        command=_server(tmp_path, _FAKE_SERVER),
        env=(("FAKE_TOOLS", "${SECRET_TOOL}"),),
    )

    tools = probe_stdio_server(binding, base_env={**os.environ, "SECRET_TOOL": "sourced"})

    assert tools == ("sourced",)


def test_probe_fails_when_a_referenced_secret_is_unset(tmp_path: Path) -> None:
    """An unset reference fails before launch rather than starting the server
    with an empty credential and reporting the resulting auth error."""
    binding = McpBinding(
        name="fake",
        command=_server(tmp_path, _FAKE_SERVER),
        env=(("TOKEN", "${DEFINITELY_UNSET_VAR}"),),
    )

    with pytest.raises(McpUnreachableError, match="DEFINITELY_UNSET_VAR"):
        probe_stdio_server(binding, base_env={})


def test_probe_launches_server_in_declared_cwd(tmp_path: Path) -> None:
    """A binding's ``cwd`` becomes the server process's working directory."""
    workdir = tmp_path / "elsewhere"
    workdir.mkdir()
    binding = McpBinding(
        name="fake",
        command=_server(tmp_path, _CWD_SERVER),
        cwd=str(workdir),
    )

    (reported,) = probe_stdio_server(binding, timeout=30)

    assert Path(reported).resolve() == workdir.resolve()


def test_probe_reports_a_missing_binary(tmp_path: Path) -> None:
    """The dominant real failure — the server binary is not installed."""
    binding = McpBinding(name="gone", command=("definitely-not-a-real-binary-xyz",))

    with pytest.raises(McpUnreachableError, match="could not launch server"):
        probe_stdio_server(binding, timeout=5)


def test_probe_reports_a_server_that_exits_immediately(tmp_path: Path) -> None:
    """A server that dies on startup (bad args, failed package fetch) is caught,
    and its stderr is carried into the error so the cause is diagnosable."""
    script = tmp_path / "dies.py"
    script.write_text("import sys; sys.stderr.write('boom: bad config\\n')", encoding="utf-8")
    binding = McpBinding(name="dies", command=(sys.executable, str(script)))

    with pytest.raises(McpUnreachableError, match="boom: bad config"):
        probe_stdio_server(binding, timeout=15)


def test_probe_times_out_on_a_server_that_never_responds(tmp_path: Path) -> None:
    """A server that starts but never answers must not hang the run."""
    script = tmp_path / "mute.py"
    script.write_text("import time; time.sleep(60)", encoding="utf-8")
    binding = McpBinding(name="mute", command=(sys.executable, str(script)))

    with pytest.raises(McpUnreachableError, match="timed out"):
        probe_stdio_server(binding, timeout=1.5)


def test_probe_rejects_a_process_that_only_echoes_its_input(tmp_path: Path) -> None:
    """Echoing stdin replays the request id; a reply must carry ``result`` or
    ``error`` and no ``method``, so an echo cannot pass as a handshake."""
    script = tmp_path / "echo.py"
    script.write_text(
        "import sys\nfor line in sys.stdin: sys.stdout.write(line); sys.stdout.flush()",
        encoding="utf-8",
    )
    binding = McpBinding(name="echo", command=(sys.executable, str(script)))

    with pytest.raises(McpUnreachableError, match="timed out"):
        probe_stdio_server(binding, timeout=1.5)


def test_probe_skips_noise_on_stdout(tmp_path: Path) -> None:
    """Servers that log to stdout despite the spec still pass: non-JSON lines
    and unrelated notifications are skipped rather than failing the handshake."""
    noisy = _FAKE_SERVER.replace(
        'if method == "initialize":',
        'print("starting up, not json", flush=True)\n'
        '    print(json.dumps({"jsonrpc": "2.0", "method": "notifications/message"}), flush=True)\n'
        '    if method == "initialize":',
    )
    binding = McpBinding(name="noisy", command=_server(tmp_path, noisy))

    assert probe_stdio_server(binding, timeout=30) == ("alpha", "beta")


def test_preflight_probes_every_binding(tmp_path: Path) -> None:
    """Each launchable binding is probed and keyed by its name."""
    command = _server(tmp_path, _FAKE_SERVER)
    bindings = (
        McpBinding(name="one", command=command, env=(("FAKE_TOOLS", "a"),)),
        McpBinding(name="two", command=command, env=(("FAKE_TOOLS", "b,c"),)),
    )

    assert preflight_mcp(bindings, timeout=30) == {"one": ("a",), "two": ("b", "c")}


def test_preflight_names_the_failing_server(tmp_path: Path) -> None:
    """The error identifies which of several servers is unreachable."""
    bindings = (
        McpBinding(name="good", command=_server(tmp_path, _FAKE_SERVER)),
        McpBinding(name="bad", command=("definitely-not-a-real-binary-xyz",)),
    )

    with pytest.raises(McpUnreachableError, match="'bad' is unreachable"):
        preflight_mcp(bindings, timeout=10)


def test_preflight_skips_bindings_the_cli_hosts_itself() -> None:
    """An empty command denotes a server the CLI binary hosts; there is no
    process to probe, so it is skipped rather than failed."""
    assert preflight_mcp((McpBinding(name="builtin", tools=("x",)),)) == {}


def test_expand_env_leaves_plain_values_untouched() -> None:
    """A value with no reference passes through unchanged."""
    assert expand_env("plain-value", source={}) == "plain-value"


def test_expand_env_resolves_braced_references_and_leaves_bare_dollars_alone() -> None:
    """Only ``${VAR}`` is a reference.

    A bare ``$VAR`` form would treat the dollar in a literal credential as a
    reference, mangling the value (``p$ssw0rd`` -> ``p``) and leaking the
    fragment after it into the "unset variable" error.
    """
    source = {"A": "1", "B": "2"}

    assert expand_env("${A}/${B}", source=source) == "1/2"
    assert expand_env("p$ssw0rd", source=source) == "p$ssw0rd"
    assert expand_env("Bearer ${A}", source=source) == "Bearer 1"


_FORKING_SERVER = """
import json, subprocess, sys
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "serverInfo": {"name": "f", "version": "1"}}}), flush=True)
    elif msg.get("method") == "tools/list":
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                          "result": {"tools": [{"name": "t"}]}}), flush=True)
"""

_SECRET_ECHO_SERVER = """
import os, sys
sys.stderr.write("FATAL: rejected input_value='%s'\\n" % os.environ.get("TOK"))
sys.exit(1)
"""

_HANGS_AFTER_STDERR = """
import sys, time
sys.stderr.write("FATAL: token rejected\\n")
sys.stderr.flush()
time.sleep(60)
"""

_NO_TOOLS_SERVER = """
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "serverInfo": {"name": "n", "version": "1"}}}), flush=True)
    elif msg.get("method") == "tools/list":
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": []}}), flush=True)
"""

_CWD_ECHO_SERVER = """
import json, os, sys
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "serverInfo": {"name": "c", "version": "1"}}}), flush=True)
    elif msg.get("method") == "tools/list":
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                          "result": {"tools": [{"name": os.getcwd()}]}}), flush=True)
"""


def test_probe_returns_promptly_when_the_server_forks_a_surviving_grandchild(
    tmp_path: Path,
) -> None:
    """A launcher (uvx/npx/sh -c/docker) execs the real server as a child.

    Signalling only the direct child leaves that grandchild holding the inherited
    stdout pipe, so the reader thread never reaches EOF and closing the stream
    blocks on its lock — the probe used to hang for the grandchild's whole life
    (2 minutes here) instead of returning. Regression test for that hang, which
    also fired on the success path.
    """
    binding = McpBinding(name="forker", command=_server(tmp_path, _FORKING_SERVER))

    started = time.monotonic()
    tools = probe_stdio_server(binding, base_env=dict(os.environ), timeout=10.0)

    assert tools == ("t",)
    assert time.monotonic() - started < 20.0, "probe must not wait out the grandchild"


def test_probe_redacts_expanded_secrets_from_the_reported_stderr(tmp_path: Path) -> None:
    """The probe holds the only expanded copy of a ``${VAR}`` credential.

    Its failure message reaches ``AgentResult.errors`` and is persisted to the
    run's results.json, and a server rejecting a credential routinely echoes it,
    so the value must never survive into the message.
    """
    binding = McpBinding(
        name="leaky",
        command=_server(tmp_path, _SECRET_ECHO_SERVER),
        env=(("TOK", "${REAL_SECRET}"),),
    )

    with pytest.raises(McpUnreachableError) as excinfo:
        probe_stdio_server(binding, base_env={"REAL_SECRET": "ghp_supersecret123"}, timeout=10.0)

    assert "ghp_supersecret123" not in str(excinfo.value)
    assert "***" in str(excinfo.value)


def test_probe_reports_stderr_when_the_server_starts_then_hangs(tmp_path: Path) -> None:
    """Timeout is the case the stderr explains, so it must carry it.

    The stderr reader only reaches EOF once the process stops, so composing the
    message before terminating produced a bare "timed out" with no cause.
    """
    binding = McpBinding(name="hangs", command=_server(tmp_path, _HANGS_AFTER_STDERR))

    with pytest.raises(McpUnreachableError, match="token rejected"):
        probe_stdio_server(binding, base_env=dict(os.environ), timeout=2.0)


def test_probe_fails_a_server_that_advertises_no_tools(tmp_path: Path) -> None:
    """Handshaking with an empty tool list is the tool-less arm this probe exists
    to catch: the model falls back to shell tools and the run still scores."""
    binding = McpBinding(name="empty", command=_server(tmp_path, _NO_TOOLS_SERVER))

    with pytest.raises(McpUnreachableError, match="advertised no tools"):
        probe_stdio_server(binding, base_env=dict(os.environ), timeout=10.0)


def test_probe_launches_the_server_in_the_supplied_cwd(tmp_path: Path) -> None:
    """The probe must launch a server where the CLI later will, so a relative
    path in the binding's args resolves the same way in both."""
    run_dir = tmp_path / "workspace"
    run_dir.mkdir()
    binding = McpBinding(name="cwd", command=_server(tmp_path, _CWD_ECHO_SERVER))

    tools = probe_stdio_server(binding, base_env=dict(os.environ), timeout=10.0, cwd=run_dir)

    assert Path(tools[0]).resolve() == run_dir.resolve()


def test_preflight_probes_servers_concurrently(tmp_path: Path) -> None:
    """Servers are independent, so the budget is the slowest one, not the sum.

    Sequential probing put N * timeout on a path that runs before every run in
    the matrix.
    """
    slow = tmp_path / "slow.py"
    slow.write_text("import time; time.sleep(30)", encoding="utf-8")
    bindings = tuple(
        McpBinding(name=f"s{i}", command=(sys.executable, str(slow))) for i in range(4)
    )

    started = time.monotonic()
    with pytest.raises(McpUnreachableError):
        preflight_mcp(bindings, base_env=dict(os.environ), timeout=2.0)

    assert time.monotonic() - started < 6.0, "probes must overlap, not queue"


def test_preflight_names_the_first_failure_in_binding_order(tmp_path: Path) -> None:
    """Concurrency must not make the reported failure depend on a race."""
    good = _server(tmp_path, _FORKING_SERVER)
    bindings = (
        McpBinding(name="alpha", command=("/nonexistent/a",)),
        McpBinding(name="beta", command=("/nonexistent/b",)),
        McpBinding(name="gamma", command=good),
    )

    with pytest.raises(McpUnreachableError, match="'alpha'"):
        preflight_mcp(bindings, base_env=dict(os.environ), timeout=10.0)


def test_probe_survives_a_stdout_burst_larger_than_the_line_cap(tmp_path: Path) -> None:
    """A server logging far more than ``_MAX_STDOUT_LINES`` before its reply must
    still hand that reply to the probe.

    The reader thread holds a bounded queue so a chatty server cannot exhaust
    memory, but the cap is a backlog limit, not a total: the consumer drains
    concurrently. If the reader ever blocked on a full queue instead of dropping,
    the server would stall behind its own pipe and the probe would report a
    healthy server as unreachable.
    """
    burst = _FAKE_SERVER.replace(
        "for line in sys.stdin:",
        'for _i in range(5000):\n    print("log line", _i, flush=True)\n\nfor line in sys.stdin:',
    )
    binding = McpBinding(name="chatty", command=_server(tmp_path, burst))

    assert probe_stdio_server(binding, timeout=30) == ("alpha", "beta")
