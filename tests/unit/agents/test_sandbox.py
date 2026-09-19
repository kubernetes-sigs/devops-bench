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

"""Unit tests for devops_bench.agents.sandbox and the run_agent_cmd seam."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from devops_bench.agents import base as base_mod
from devops_bench.agents import sandbox
from devops_bench.agents.base import AgentHarness
from devops_bench.agents.config import AgentConfig
from devops_bench.agents.result import AgentResult
from devops_bench.core import ClusterInfo, NetworkPlan
from devops_bench.core.errors import SandboxError, SubprocessError
from devops_bench.k8s import kubectl


class _DummyAgent(AgentHarness):
    """Minimal concrete harness so ``run_agent_cmd`` can be exercised directly."""

    def _execute(self, prompt: str, workspace_path: Path | None = None) -> AgentResult:
        raise NotImplementedError


def _complete_spec(tmp_path: Path, **overrides) -> sandbox.SandboxSpec:
    """A fully-populated spec rooted in ``tmp_path``."""
    workspace = tmp_path / "workspace-abc123"
    workspace.mkdir(exist_ok=True)
    creds = tmp_path / "creds"
    creds.mkdir(exist_ok=True)
    kubeconfig = creds / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
    fields = {
        "image": "agent-image",
        "workspace": workspace,
        "kubeconfig": kubeconfig,
        "network": sandbox.NetworkPlan(docker_network="kind"),
    }
    fields.update(overrides)
    return sandbox.SandboxSpec(**fields)


# -- opt-in parsing -------------------------------------------------------


def test_spec_from_env_is_none_when_unset() -> None:
    assert sandbox.spec_from_env({}) is None


@pytest.mark.parametrize("value", ["docker", "1", "true", "TRUE", " Docker "])
def test_spec_from_env_accepts_the_documented_switch_values(value: str) -> None:
    spec = sandbox.spec_from_env({"BENCH_AGENT_SANDBOX": value, "BENCH_SANDBOX_IMAGE": "img:1"})
    assert spec is not None
    assert spec.image == "img:1"


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_spec_from_env_is_none_on_an_explicit_off_value(value: str) -> None:
    assert sandbox.spec_from_env({"BENCH_AGENT_SANDBOX": value}) is None


@pytest.mark.parametrize("value", ["yes", "podman", "on", "enabled"])
def test_spec_from_env_raises_on_an_unrecognized_value(value: str) -> None:
    """An unrecognized spelling must fail loud, never silently run ambient."""
    with pytest.raises(SandboxError, match="not a recognized value"):
        sandbox.spec_from_env({"BENCH_AGENT_SANDBOX": value})


def test_spec_from_env_tolerates_a_missing_image() -> None:
    """The image check lives in the executor, where it can fail loud per run."""
    spec = sandbox.spec_from_env({"BENCH_AGENT_SANDBOX": "1"})
    assert spec is not None
    assert spec.image == ""


# -- container naming and reaping -----------------------------------------


def test_container_name_for_workspace_is_deterministic_and_prefixed() -> None:
    name = sandbox.container_name_for_workspace(Path("/tmp/workspace-abc123"))
    assert name == "devops-bench-agent-workspace-abc123"


def test_container_name_for_workspace_differs_per_workspace() -> None:
    a = sandbox.container_name_for_workspace(Path("/tmp/workspace-a"))
    b = sandbox.container_name_for_workspace(Path("/tmp/workspace-b"))
    assert a != b


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

    assert [c for c in calls if c[:2] == ["docker", "kill"]] == []


# -- cluster context and network plan --------------------------------------


class _FakeProvider:
    """A provider stand-in returning a fixed plan, as the real hook does."""

    def __init__(self, plan: NetworkPlan) -> None:
        self.plan = plan
        self.seen: list[ClusterInfo] = []

    def sandbox_network_plan(self, cluster_info: ClusterInfo) -> NetworkPlan:
        self.seen.append(cluster_info)
        return self.plan


def _cluster(name: str = "c1") -> ClusterInfo:
    return ClusterInfo(name=name, kubeconfig_path="/tmp/kc")


def _patch_plan_reads(
    monkeypatch: pytest.MonkeyPatch, *, contexts: tuple[str, ...] = (), server: str = ""
) -> None:
    """Answer the two kubectl reads a plan build makes.

    Both modules are patched because the reads leave by different doors: the
    context probe calls ``sandbox.run`` directly, while the server read goes
    through ``k8s.kubectl.config_value``.
    """

    def fake_run(argv, **kwargs):
        if argv[:3] == ["kubectl", "config", "get-contexts"]:
            return SimpleNamespace(returncode=0, stdout="\n".join(contexts) + "\n", stderr="")
        # ``--context`` is pinned right after the binary, so match on the
        # subcommand rather than a fixed offset.
        assert "view" in argv
        return SimpleNamespace(returncode=0, stdout=server, stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    monkeypatch.setattr(kubectl, "run", fake_run)


def test_build_network_plan_asks_the_provider_and_passes_the_cluster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FakeProvider(
        NetworkPlan(
            docker_network="kind",
            rewrite_server="https://c1-control-plane:6443",
            kubectl_context="kind-c1",
        )
    )
    _patch_plan_reads(monkeypatch, contexts=("kind-c1", "kind-other"))

    plan = sandbox.build_network_plan(provider, _cluster())

    assert [c.name for c in provider.seen] == ["c1"]
    assert plan.docker_network == "kind"
    # A provider that supplied its own rewrite is left entirely alone: kind's
    # in-network name verifies against the apiserver cert with no override.
    assert plan.rewrite_server == "https://c1-control-plane:6443"
    assert plan.tls_server_name is None
    assert plan.kubectl_context == "kind-c1"


@pytest.mark.parametrize(
    ("server", "expected"),
    [
        ("https://127.0.0.1:6443", "https://host.docker.internal:6443"),
        ("https://localhost:6443", "https://host.docker.internal:6443"),
        ("https://[::1]:6443", "https://host.docker.internal:6443"),
        ("https://0.0.0.0:8443", "https://host.docker.internal:8443"),
    ],
)
def test_build_network_plan_rewrites_a_loopback_server(
    monkeypatch: pytest.MonkeyPatch, server: str, expected: str
) -> None:
    """Loopback inside a container is the container, so it must be remapped —
    and the cert only carries ``localhost``, so TLS is redirected, not disabled."""
    _patch_plan_reads(monkeypatch, server=server)

    plan = sandbox.build_network_plan(_FakeProvider(NetworkPlan()), _cluster())

    assert plan.rewrite_server == expected
    assert plan.tls_server_name == "localhost"


def test_build_network_plan_leaves_a_routable_server_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A GKE endpoint already means something from a bridge-networked
    container; rewriting it would break the run this PR exists to enable."""
    provider = _FakeProvider(NetworkPlan(kubectl_context="gke_p_us-central1_c1"))
    _patch_plan_reads(monkeypatch, contexts=("gke_p_us-central1_c1",), server="https://34.10.0.1")

    plan = sandbox.build_network_plan(provider, _cluster())

    assert plan.rewrite_server is None
    assert plan.tls_server_name is None
    assert plan.docker_network is None


def test_build_network_plan_accepts_a_deployer_without_a_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The no-op deployer has no provider; the run still gets a usable plan
    from the ambient context rather than a refusal."""
    _patch_plan_reads(monkeypatch, server="https://34.10.0.1")

    assert sandbox.build_network_plan(None, _cluster()) == NetworkPlan()


def test_build_network_plan_refuses_a_context_kubectl_does_not_know(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The provider names the run's own context. If this kubeconfig never saw
    it, refuse rather than silently building against whatever is active."""
    provider = _FakeProvider(NetworkPlan(kubectl_context="kind-c1"))
    _patch_plan_reads(monkeypatch, contexts=("kind-someone-elses-cluster",))

    with pytest.raises(SandboxError, match="kind-c1"):
        sandbox.build_network_plan(provider, _cluster())


def test_build_network_plan_refuses_an_unreadable_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_plan_reads(monkeypatch, server="")

    with pytest.raises(SandboxError, match="server URL"):
        sandbox.build_network_plan(_FakeProvider(NetworkPlan()), _cluster())


# -- fixture discovery -------------------------------------------------------


def test_discover_fixture_mounts_matches_only_this_runs_cluster_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "opa-repo-c1.git").mkdir()
    (home / "advisory-c1.json").write_text("{}", encoding="utf-8")
    # Another run's fixture and an unrelated operator file must not be mounted.
    (home / "opa-repo-c2.git").mkdir()
    (home / "taxes.pdf").write_text("x", encoding="utf-8")
    monkeypatch.setattr(sandbox.Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv(sandbox.FIXTURES_ENV, raising=False)

    mounts = sandbox.discover_fixture_mounts("c1")

    # Container paths live under the container HOME, so a prompt's
    # ``~/<name>`` resolves to exactly the mounted fixture.
    assert sorted(mounts.values()) == [
        "/workspace/home/advisory-c1.json",
        "/workspace/home/opa-repo-c1.git",
    ]


def test_discover_fixture_mounts_matches_top_level_entries_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    (home / "nested").mkdir(parents=True)
    (home / "nested" / "opa-repo-c1.git").mkdir()
    monkeypatch.setattr(sandbox.Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv(sandbox.FIXTURES_ENV, raising=False)

    assert sandbox.discover_fixture_mounts("c1") == {}


def test_discover_fixture_mounts_is_empty_without_a_cluster_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(sandbox.FIXTURES_ENV, raising=False)
    assert sandbox.discover_fixture_mounts(None) == {}


def test_discover_fixture_mounts_honours_the_explicit_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixture = tmp_path / "oddly-named-repo.git"
    fixture.mkdir()
    monkeypatch.setenv(sandbox.FIXTURES_ENV, f"{fixture}:{tmp_path / 'missing'}")

    mounts = sandbox.discover_fixture_mounts(None)

    # The declared-but-absent path is skipped rather than turned into a broken
    # bind mount; the real one lands under the container's HOME.
    assert mounts == {str(fixture.resolve()): "/workspace/home/oddly-named-repo.git"}


def test_discover_fixture_mounts_refuses_duplicate_fixture_basenames(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two same-named fixtures would emit two ``-v`` flags onto one container
    destination, which docker aborts on with a cryptic 'Duplicate mount
    point' — refuse up front, naming both host paths."""
    first = tmp_path / "a" / "fix.git"
    second = tmp_path / "b" / "fix.git"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    monkeypatch.setenv(sandbox.FIXTURES_ENV, f"{first}:{second}")

    with pytest.raises(SandboxError, match="collision") as excinfo:
        sandbox.discover_fixture_mounts(None)
    assert str(first.resolve()) in str(excinfo.value)
    assert str(second.resolve()) in str(excinfo.value)


# -- boundary env filter ------------------------------------------------------


def test_filter_boundary_env_rejects_credential_and_benchmark_vars() -> None:
    overlay = {
        "GEMINI_API_KEY": "k",
        "GEMINI_MODEL": "m",
        "OTEL_SDK_DISABLED": "true",
        "CLOUDSDK_CONFIG": "/home/op/.config/gcloud",
        "GOOGLE_APPLICATION_CREDENTIALS": "/home/op/adc.json",
        "BENCH_CHEAT_DETECT": "0",
        "TF_VAR_project": "p",
        "AWS_ACCESS_KEY_ID": "AKIA...",
        "AZURE_CLIENT_SECRET": "s",
        "ARM_CLIENT_ID": "c",
        "HOME": "/home/op",
        "KUBECONFIG": "/home/op/.kube/config",
    }
    kept = sandbox.filter_boundary_env(overlay)
    assert kept == {"GEMINI_API_KEY": "k", "GEMINI_MODEL": "m", "OTEL_SDK_DISABLED": "true"}


def test_filter_boundary_env_allowlist_overrides_a_denial() -> None:
    kept = sandbox.filter_boundary_env({"TF_VAR_task_input": "x"}, allowlist=("TF_VAR_task_input",))
    assert kept == {"TF_VAR_task_input": "x"}


def test_filter_boundary_env_never_admits_container_owned_vars() -> None:
    """HOME/KUBECONFIG/PATH are the executor's own inside the container;
    docker's last ``-e`` wins, so even an explicit allowlist must not let an
    overlay value repoint them."""
    overlay = {"HOME": "/home/op", "KUBECONFIG": "/home/op/.kube/config", "PATH": "/evil/bin"}
    kept = sandbox.filter_boundary_env(overlay, allowlist=("HOME", "KUBECONFIG", "PATH"))
    assert kept == {}


def test_filter_boundary_env_handles_none_overlay() -> None:
    assert sandbox.filter_boundary_env(None) == {}


# -- executor: spec validation ------------------------------------------------


def test_executor_refuses_a_spec_without_an_image(tmp_path: Path) -> None:
    with pytest.raises(SandboxError, match="BENCH_SANDBOX_IMAGE"):
        sandbox.SandboxExecutor(_complete_spec(tmp_path, image=""))


def test_executor_refuses_an_incomplete_spec(tmp_path: Path) -> None:
    """The skeletal from_env spec must never run: no workspace/kubeconfig means
    the harness has not completed it, and running anyway would improvise a
    boundary."""
    with pytest.raises(SandboxError, match="incomplete"):
        sandbox.SandboxExecutor(sandbox.SandboxSpec(image="img"))


# -- executor: argv construction ------------------------------------------------


def test_wrap_argv_core_shape(tmp_path: Path) -> None:
    spec = _complete_spec(tmp_path)
    executor = sandbox.SandboxExecutor(spec)

    argv = executor.wrap_argv(["gemini", "-p", "hi"], extra_env={"GEMINI_API_KEY": "k"})

    assert argv[:3] == ["docker", "run", "--rm"]
    assert argv[argv.index("--name") + 1] == "devops-bench-agent-workspace-abc123"
    assert argv[argv.index("--network") + 1] == "kind"
    # Boundary invariants: no stdin, host-gateway alias always present, no
    # capabilities and no setuid re-escalation — the latter pair is what
    # contains the macOS case, where the process runs as root (no --user).
    assert "-i" not in argv
    assert "host.docker.internal:host-gateway" in argv
    assert "--cap-drop=ALL" in argv
    assert "--security-opt=no-new-privileges=true" in argv
    # Mount set: workspace RW, kubeconfig RO.
    assert f"{spec.workspace}:/workspace" in argv
    assert f"{spec.kubeconfig}:/creds/kubeconfig:ro" in argv
    # Env: container-owned vars plus the filtered overlay, by value.
    assert "HOME=/workspace/home" in argv
    assert "KUBECONFIG=/creds/kubeconfig" in argv
    assert "GEMINI_API_KEY=k" in argv
    # Default working directory is the workspace; image then the raw argv.
    assert argv[argv.index("-w") + 1] == "/workspace"
    assert argv[-4:] == ["agent-image", "gemini", "-p", "hi"]


def test_wrap_argv_container_owned_env_flags_come_last(tmp_path: Path) -> None:
    """Defense in depth against a filter regression: the executor's own
    ``-e HOME``/``-e KUBECONFIG`` trail every overlay flag, so docker's
    last-one-wins keeps them authoritative no matter what crossed."""
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))
    argv = executor.wrap_argv(["gemini"], extra_env={"GEMINI_API_KEY": "k"})
    assert argv.index("HOME=/workspace/home") > argv.index("GEMINI_API_KEY=k")
    assert argv.index("KUBECONFIG=/creds/kubeconfig") > argv.index("GEMINI_API_KEY=k")


def test_wrap_argv_never_forwards_denied_env(tmp_path: Path) -> None:
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))
    argv = executor.wrap_argv(
        ["gemini"],
        extra_env={"GOOGLE_APPLICATION_CREDENTIALS": "/adc.json", "BENCH_AGENT_SANDBOX": "1"},
    )
    joined = " ".join(argv)
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in joined
    assert "BENCH_AGENT_SANDBOX" not in joined


def test_wrap_argv_mounts_fixtures_read_write(tmp_path: Path) -> None:
    executor = sandbox.SandboxExecutor(
        _complete_spec(
            tmp_path,
            fixture_mounts={"/home/op/opa-repo-c1.git": "/workspace/home/opa-repo-c1.git"},
        )
    )
    argv = executor.wrap_argv(["gemini"])
    # No ``:ro``: several tasks ask the agent to commit back to the seeded repo.
    assert "/home/op/opa-repo-c1.git:/workspace/home/opa-repo-c1.git" in argv
    assert "/home/op/opa-repo-c1.git:/workspace/home/opa-repo-c1.git:ro" not in argv


def test_wrap_argv_omits_network_flag_on_the_default_bridge(tmp_path: Path) -> None:
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path, network=sandbox.NetworkPlan()))
    assert "--network" not in executor.wrap_argv(["gemini"])


def test_wrap_argv_adds_plan_extra_hosts(tmp_path: Path) -> None:
    plan = sandbox.NetworkPlan(extra_hosts=("apiserver.local:10.0.0.5",))
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path, network=plan))
    assert "apiserver.local:10.0.0.5" in executor.wrap_argv(["gemini"])


def test_wrap_argv_sets_user_mapping_on_linux_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))

    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    assert "--user" in executor.wrap_argv(["gemini"])

    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    # Docker Desktop already remaps file ownership on macOS.
    assert "--user" not in executor.wrap_argv(["gemini"])


def test_wrap_argv_maps_a_cwd_under_the_workspace(tmp_path: Path) -> None:
    spec = _complete_spec(tmp_path)
    subdir = Path(spec.workspace) / "repo"
    subdir.mkdir()
    executor = sandbox.SandboxExecutor(spec)
    argv = executor.wrap_argv(["git", "log"], cwd=subdir)
    assert argv[argv.index("-w") + 1] == "/workspace/repo"


def test_map_host_path_raises_outside_the_workspace(tmp_path: Path) -> None:
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))
    with pytest.raises(SandboxError, match="outside the sandbox workspace"):
        executor.map_host_path(tmp_path / "elsewhere")


# -- executor: run semantics -----------------------------------------------------


def test_executor_run_rejects_a_full_environment(tmp_path: Path) -> None:
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))
    with pytest.raises(SandboxError, match="full environment"):
        executor.run(["gemini"], env={"ALL": "of it"})


def test_executor_run_rejects_stdin_input(tmp_path: Path) -> None:
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))
    with pytest.raises(SandboxError, match="stdin"):
        executor.run(["gemini"], input="data")


def test_executor_run_reaps_the_container_on_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--rm`` cannot clean up a container whose ``docker run`` client was
    SIGKILLed by the host-side timeout; the executor must kill by name."""
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))
    kills: list[list[str]] = []

    def fake_run(argv, **kwargs):
        if argv[:2] == ["docker", "run"]:
            raise SubprocessError(argv, returncode=-1, stdout="partial", stderr="")
        if argv[:2] == ["docker", "kill"]:
            kills.append(argv)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected argv: {argv}")

    monkeypatch.setattr(sandbox, "run", fake_run)
    with pytest.raises(SubprocessError):
        executor.run(["gemini", "-p", "hi"], timeout=1)

    assert kills == [["docker", "kill", executor.container_name]]


def test_executor_run_reaps_the_container_after_a_clean_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Best-effort double-tap: ``--rm`` normally already removed it, and the
    by-name kill of a gone container is a harmless no-op."""
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))
    kills: list[list[str]] = []

    def fake_run(argv, **kwargs):
        if argv[:2] == ["docker", "kill"]:
            kills.append(argv)
            return SimpleNamespace(returncode=1, stdout="", stderr="No such container")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    completed = executor.run(["gemini", "-p", "hi"], check=False, timeout=5)

    assert completed.stdout == "ok"
    assert kills == [["docker", "kill", executor.container_name]]


def test_executor_run_passes_through_check_and_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))
    seen: dict = {}

    def fake_run(argv, **kwargs):
        if argv[:2] == ["docker", "run"]:
            seen.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    executor.run(["gemini"], check=False, timeout=15.5)

    assert seen["check"] is False
    assert seen["timeout"] == 15.5


# -- the run_agent_cmd seam --------------------------------------------------------


def test_run_agent_cmd_flag_off_is_a_verbatim_passthrough() -> None:
    """With no sandbox configured the seam must hand every argument through
    unchanged — same values, same defaults as ``core.subprocess.run``."""
    agent = _DummyAgent(AgentConfig())
    captured: dict = {}

    def fake_host_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    agent.run_agent_cmd(
        ["gemini", "-p", "hi"],
        cwd="/tmp/ws",
        extra_env={"GEMINI_MODEL": "m"},
        check=False,
        timeout=15.5,
        host_run=fake_host_run,
    )

    assert captured == {
        "cmd": ["gemini", "-p", "hi"],
        "cwd": "/tmp/ws",
        "env": None,
        "extra_env": {"GEMINI_MODEL": "m"},
        "check": False,
        "capture": True,
        "text": True,
        "timeout": 15.5,
        "input": None,
    }


def test_run_agent_cmd_defaults_to_core_subprocess_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _DummyAgent(AgentConfig())
    called: dict = {}

    def fake_core_run(cmd, **kwargs):
        called["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(base_mod, "_host_subprocess_run", fake_core_run)
    agent.run_agent_cmd(["echo", "hi"], check=False)
    assert called["cmd"] == ["echo", "hi"]


def test_run_agent_cmd_dispatches_to_the_executor_when_sandbox_is_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spec = _complete_spec(tmp_path)
    agent = _DummyAgent(AgentConfig(sandbox=spec))
    docker_argvs: list[list[str]] = []

    def fake_run(argv, **kwargs):
        docker_argvs.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def must_not_run_on_host(cmd, **kwargs):
        raise AssertionError("host path taken despite config.sandbox")

    monkeypatch.setattr(sandbox, "run", fake_run)
    agent.run_agent_cmd(["gemini", "-p", "hi"], check=False, host_run=must_not_run_on_host)

    wrapped = docker_argvs[0]
    assert wrapped[:2] == ["docker", "run"]
    assert wrapped[-4:] == ["agent-image", "gemini", "-p", "hi"]


def test_sandbox_error_from_the_executor_propagates_out_of_run(tmp_path: Path) -> None:
    """An incomplete spec raises in the executor and the base safety net
    deliberately re-raises it: converted to an errored result it would score
    a broken boundary as a badly-performing agent, when the eval harness
    should record a failed, unscored run instead."""

    class _Boomy(AgentHarness):
        supports_sandbox = True

        def _execute(self, prompt: str, workspace_path: Path | None = None) -> AgentResult:
            self.run_agent_cmd(["gemini"])
            raise AssertionError("unreachable")

    agent = _Boomy(AgentConfig(sandbox=sandbox.SandboxSpec(image="img")))
    with pytest.raises(SandboxError, match="incomplete"):
        agent.run("p")


def test_run_still_converts_non_sandbox_crashes_to_errored_results() -> None:
    """The SandboxError carve-out must not widen: every other crash keeps the
    safety-net behaviour so one agent fault never aborts the benchmark."""
    agent = _DummyAgent(AgentConfig())  # _execute raises NotImplementedError
    result = agent.run("p")
    assert result.errors
    assert "NotImplementedError" in result.errors[0]


def test_run_refuses_a_sandboxed_config_on_an_unmigrated_agent(tmp_path: Path) -> None:
    """A harness that never routed its subprocesses through run_agent_cmd
    would run on the host with the operator's ambient credentials while the
    flag says 'contained'. Refusal must be loud, before _execute ever runs."""
    agent = _DummyAgent(AgentConfig(sandbox=_complete_spec(tmp_path)))
    with pytest.raises(SandboxError, match="not been migrated"):
        agent.run("p")


def test_gemini_declares_sandbox_support() -> None:
    from devops_bench.agents.cli.gemini_cli.agent import GeminiCliAgent

    assert GeminiCliAgent.supports_sandbox is True
    # The base default stays False so a new harness must opt in explicitly.
    assert AgentHarness.supports_sandbox is False
