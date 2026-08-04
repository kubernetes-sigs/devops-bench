# Copyright 2026 Google LLC
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

"""Unit tests for devops_bench.agents.sandbox: container naming and reaping."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from devops_bench.agents import sandbox
from devops_bench.core.errors import SubprocessError


def test_container_name_for_workspace_is_deterministic_and_prefixed() -> None:
    name = sandbox.container_name_for_workspace(Path("/tmp/workspace-abc123"))
    assert name == "devops-bench-agent-workspace-abc123"


def test_container_name_for_workspace_differs_per_workspace() -> None:
    a = sandbox.container_name_for_workspace(Path("/tmp/workspace-a"))
    b = sandbox.container_name_for_workspace(Path("/tmp/workspace-b"))
    assert a != b


def test_wrap_argv_includes_name_flag_when_container_name_given() -> None:
    argv = sandbox.wrap_argv(
        ["gemini", "-p", "hi"],
        workspace=Path("/tmp/ws"),
        kubeconfig=Path("/tmp/ws/kubeconfig"),
        image="agent-image",
        container_name="devops-bench-agent-ws",
    )
    assert "--name" in argv
    assert argv[argv.index("--name") + 1] == "devops-bench-agent-ws"


def test_wrap_argv_omits_name_flag_when_none_given() -> None:
    argv = sandbox.wrap_argv(
        ["gemini", "-p", "hi"],
        workspace=Path("/tmp/ws"),
        kubeconfig=Path("/tmp/ws/kubeconfig"),
        image="agent-image",
    )
    assert "--name" not in argv


def test_kill_container_invokes_docker_kill_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return SimpleNamespace(returncode=0, stdout="devops-bench-agent-ws\n", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    sandbox.kill_container("devops-bench-agent-ws")
    assert captured["argv"] == ["docker", "kill", "devops-bench-agent-ws"]


def test_kill_container_never_raises_when_docker_kill_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Killing an already-gone container (the common case, ``--rm`` beat us to
    it) must be a harmless no-op, not a crash."""

    def fake_run(argv, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="No such container")

    monkeypatch.setattr(sandbox, "run", fake_run)
    sandbox.kill_container("devops-bench-agent-gone")  # must not raise


def test_container_guard_kills_container_on_normal_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    killed: list[str] = []
    monkeypatch.setattr(sandbox, "kill_container", killed.append)

    with sandbox.container_guard("devops-bench-agent-ws"):
        pass

    assert killed == ["devops-bench-agent-ws"]


def test_container_guard_kills_container_on_exception_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash inside the guarded block must still reap the container."""
    killed: list[str] = []
    monkeypatch.setattr(sandbox, "kill_container", killed.append)

    with (
        pytest.raises(RuntimeError, match="boom"),
        sandbox.container_guard("devops-bench-agent-ws"),
    ):
        raise RuntimeError("boom")

    assert killed == ["devops-bench-agent-ws"]


def test_container_guard_kills_container_on_timeout_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A SubprocessError raised by a timed-out ``core.subprocess.run`` call
    inside the guarded block must still reap the container, exactly as the
    gemini_cli agent's own timeout handling relies on."""
    killed: list[str] = []
    monkeypatch.setattr(sandbox, "kill_container", killed.append)

    timeout_exc: SubprocessError | None = None
    with sandbox.container_guard("devops-bench-agent-ws"):
        try:
            raise SubprocessError(["docker", "run"], returncode=-1, stdout="partial", stderr="")
        except SubprocessError as exc:
            # Mirrors how the agent harness swallows the timeout inside the
            # guarded block rather than letting it propagate.
            timeout_exc = exc

    assert timeout_exc is not None
    assert killed == ["devops-bench-agent-ws"]


def test_sweep_stray_containers_kills_only_matching_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["docker", "ps"]:
            return SimpleNamespace(returncode=0, stdout="abc123\ndef456\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    sandbox.sweep_stray_containers()

    list_call = calls[0]
    assert list_call[0:2] == ["docker", "ps"]
    assert any("devops-bench-agent-" in arg for arg in list_call)
    kill_calls = [c for c in calls if c[:2] == ["docker", "kill"]]
    assert kill_calls == [["docker", "kill", "abc123"], ["docker", "kill", "def456"]]


def test_sweep_stray_containers_handles_docker_ps_failure_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(argv, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="docker daemon not running")

    monkeypatch.setattr(sandbox, "run", fake_run)
    sandbox.sweep_stray_containers()  # must not raise


def test_sweep_stray_containers_is_a_noop_when_none_are_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    sandbox.sweep_stray_containers()

    kill_calls = [c for c in calls if c[:2] == ["docker", "kill"]]
    assert kill_calls == []
