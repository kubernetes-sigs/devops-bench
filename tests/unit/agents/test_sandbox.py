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
import yaml

from devops_bench.agents import base as base_mod
from devops_bench.agents import sandbox
from devops_bench.agents.base import AgentHarness
from devops_bench.agents.config import AgentConfig
from devops_bench.agents.result import AgentResult
from devops_bench.core.errors import SandboxError, SubprocessError


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


def test_current_cluster_name_returns_none_for_non_kind_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sandbox,
        "run",
        lambda argv, **kwargs: SimpleNamespace(
            returncode=0, stdout="some-cloud_project_region_cluster\n", stderr=""
        ),
    )
    assert sandbox.current_cluster_name() is None


def test_current_cluster_name_strips_kind_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sandbox,
        "run",
        lambda argv, **kwargs: SimpleNamespace(returncode=0, stdout="kind-my-cluster\n", stderr=""),
    )
    assert sandbox.current_cluster_name() == "my-cluster"


def _known_contexts(*names: str):
    """A fake ``run`` answering ``kubectl config get-contexts -o name``."""

    def fake_run(argv, **kwargs):
        assert argv[:3] == ["kubectl", "config", "get-contexts"]
        return SimpleNamespace(returncode=0, stdout="\n".join(names) + "\n", stderr="")

    return fake_run


def test_build_network_plan_joins_kind_network_and_rewrites_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox, "run", _known_contexts("kind-c1", "kind-other"))
    plan = sandbox.build_network_plan("c1")
    assert plan.docker_network == "kind"
    assert plan.rewrite_server == "https://c1-control-plane:6443"
    # The plan pins credential reads to the run's own context, so an ambient
    # current-context switch can never redirect the kubeconfig build.
    assert plan.kubectl_context == "kind-c1"


def test_build_network_plan_falls_back_to_the_current_context_without_a_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox, "current_cluster_name", lambda: "c1")
    monkeypatch.setattr(sandbox, "run", _known_contexts("kind-c1"))
    assert sandbox.build_network_plan().kubectl_context == "kind-c1"


def test_build_network_plan_refuses_a_non_kind_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox, "current_cluster_name", lambda: None)
    with pytest.raises(SandboxError, match="not a kind context"):
        sandbox.build_network_plan()


def test_build_network_plan_refuses_when_the_runs_cluster_has_no_kind_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run's own cluster (from the deployer), not the ambient context, is
    authoritative: if kubectl has no ``kind-<cluster>`` context for it, the
    plan must refuse rather than build against whatever is currently active."""
    monkeypatch.setattr(sandbox, "run", _known_contexts("kind-someone-elses-cluster"))
    with pytest.raises(SandboxError, match="kind-c1"):
        sandbox.build_network_plan("c1")


# -- kubeconfig generation --------------------------------------------------


def _kubectl_config_dispatch(
    *,
    ca: str = "ZmFrZS1jYQ==",
    server: str = "https://127.0.0.1:6443",
    cert: str = "Y2VydA==",
    key: str = "a2V5",
    expect_context: str | None = None,
):
    """A fake ``run`` answering the four kubectl jsonpath reads.

    With ``expect_context`` every read must carry ``--context <name>`` —
    the pin that keeps the generated kubeconfig on the run's own cluster.
    """

    answers = {
        "jsonpath={.clusters[0].cluster.certificate-authority-data}": ca,
        "jsonpath={.clusters[0].cluster.server}": server,
        "jsonpath={.users[0].user.client-certificate-data}": cert,
        "jsonpath={.users[0].user.client-key-data}": key,
    }

    def fake_run(argv, **kwargs):
        if argv[-1] in answers:
            if expect_context is not None:
                assert argv[argv.index("--context") + 1] == expect_context
            return SimpleNamespace(returncode=0, stdout=answers[argv[-1]], stderr="")
        raise AssertionError(f"unexpected argv in kubeconfig test: {argv}")

    return fake_run


def test_build_agent_kubeconfig_renders_exactly_one_cluster_and_no_exec_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sandbox, "run", _kubectl_config_dispatch())
    plan = sandbox.NetworkPlan(
        docker_network="kind", rewrite_server="https://c1-control-plane:6443"
    )

    path = sandbox.build_agent_kubeconfig(plan, tmp_path)

    text = path.read_text()
    config = yaml.safe_load(text)
    assert len(config["clusters"]) == 1
    assert len(config["users"]) == 1
    assert len(config["contexts"]) == 1
    assert config["clusters"][0]["cluster"]["server"] == "https://c1-control-plane:6443"
    # No exec-plugin block and no ADC anywhere: the container can never be
    # asked to shell out to a cloud credential helper it does not have.
    assert "exec" not in config["users"][0]["user"]
    assert "exec:" not in text
    assert "application_default" not in text
    assert config["users"][0]["user"]["client-certificate-data"] == "Y2VydA=="


def test_build_agent_kubeconfig_is_owner_readable_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sandbox, "run", _kubectl_config_dispatch())
    path = sandbox.build_agent_kubeconfig(sandbox.NetworkPlan(), tmp_path)
    assert (path.stat().st_mode & 0o777) == 0o600


def test_build_agent_kubeconfig_keeps_context_server_without_a_rewrite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sandbox, "run", _kubectl_config_dispatch(server="https://34.1.2.3"))
    path = sandbox.build_agent_kubeconfig(sandbox.NetworkPlan(), tmp_path)
    assert (
        yaml.safe_load(path.read_text())["clusters"][0]["cluster"]["server"] == "https://34.1.2.3"
    )


def test_build_agent_kubeconfig_renders_tls_server_name_when_the_plan_sets_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sandbox, "run", _kubectl_config_dispatch())
    plan = sandbox.NetworkPlan(
        rewrite_server="https://host.docker.internal:8443", tls_server_name="localhost"
    )
    path = sandbox.build_agent_kubeconfig(plan, tmp_path)
    cluster = yaml.safe_load(path.read_text())["clusters"][0]["cluster"]
    assert cluster["tls-server-name"] == "localhost"


def test_build_agent_kubeconfig_pins_reads_to_the_plans_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every credential read carries ``--context kind-c1``: the rendered CA /
    cert / server belong to the run's cluster even if the ambient
    current-context was switched after provisioning."""
    monkeypatch.setattr(sandbox, "run", _kubectl_config_dispatch(expect_context="kind-c1"))
    plan = sandbox.NetworkPlan(kubectl_context="kind-c1")
    path = sandbox.build_agent_kubeconfig(plan, tmp_path)
    assert yaml.safe_load(path.read_text())["users"][0]["user"]["client-key-data"] == "a2V5"


def test_build_agent_kubeconfig_refuses_without_a_ca(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sandbox, "run", _kubectl_config_dispatch(ca=""))
    with pytest.raises(SandboxError, match="CA"):
        sandbox.build_agent_kubeconfig(sandbox.NetworkPlan(), tmp_path)


def test_build_agent_kubeconfig_refuses_without_a_static_client_cert(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An exec-plugin context has no client cert; that path is a follow-up,
    and reusing the operator's plugin in-container must never happen."""
    monkeypatch.setattr(sandbox, "run", _kubectl_config_dispatch(cert="", key=""))
    with pytest.raises(SandboxError, match="client certificate"):
        sandbox.build_agent_kubeconfig(sandbox.NetworkPlan(), tmp_path)


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


def test_discover_fixture_mounts_requires_a_token_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A substring hit is not a fixture: cluster "dev" must not RW-mount
    ~/devops-bench (the bench checkout) or any dot-entry like ~/.config."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "devops-bench").mkdir()
    (home / ".devrc").write_text("x", encoding="utf-8")
    (home / "opa-repo-dev.git").mkdir()
    (home / "dev").mkdir()
    monkeypatch.setattr(sandbox.Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv(sandbox.FIXTURES_ENV, raising=False)

    mounts = sandbox.discover_fixture_mounts("dev")

    assert sorted(mounts.values()) == [
        "/workspace/home/dev",
        "/workspace/home/opa-repo-dev.git",
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

    argv = executor.wrap_argv(["gemini", "-p", "hi"], extra_env={"GEMINI_API_KEY": "sekrit"})

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
    # Env: container-owned vars inline (non-secret constants), the overlay
    # as name-only -e flags — the secret value must never appear in the
    # argv, which is world-readable in /proc and rendered into error
    # messages on timeout.
    assert "HOME=/workspace/home" in argv
    assert "KUBECONFIG=/creds/kubeconfig" in argv
    assert "GEMINI_API_KEY" in argv
    assert all("sekrit" not in part for part in argv)
    # Default working directory is the workspace; image then the raw argv.
    assert argv[argv.index("-w") + 1] == "/workspace"
    assert argv[-4:] == ["agent-image", "gemini", "-p", "hi"]


def test_wrap_argv_container_owned_env_flags_come_last(tmp_path: Path) -> None:
    """Defense in depth against a filter regression: the executor's own
    ``-e HOME``/``-e KUBECONFIG`` trail every overlay flag, so docker's
    last-one-wins keeps them authoritative no matter what crossed."""
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))
    argv = executor.wrap_argv(["gemini"], extra_env={"GEMINI_API_KEY": "k"})
    assert argv.index("HOME=/workspace/home") > argv.index("GEMINI_API_KEY")
    assert argv.index("KUBECONFIG=/creds/kubeconfig") > argv.index("GEMINI_API_KEY")


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


def test_executor_run_raises_sandbox_error_on_docker_exit_125(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """125 is docker's own failure (missing image/network), not the agent's
    exit code; with check=False it must not be scored as an agent failure."""
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))

    def fake_run(argv, **kwargs):
        if argv[:2] == ["docker", "run"]:
            return SimpleNamespace(returncode=125, stdout="", stderr="No such image")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    with pytest.raises(SandboxError, match="could not start the sandbox"):
        executor.run(["gemini"], check=False)


def test_executor_run_raises_sandbox_error_when_docker_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A missing docker binary is an infra failure, not "gemini unavailable" —
    and the finally-reap must swallow its own OSError on the way out."""
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))

    def fake_run(argv, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(sandbox, "run", fake_run)
    with pytest.raises(SandboxError, match="docker is unavailable"):
        executor.run(["gemini"])


def test_kill_container_never_raises_when_the_daemon_is_wedged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(argv, **kwargs):
        raise SubprocessError(argv, returncode=-1)

    monkeypatch.setattr(sandbox, "run", fake_run)
    sandbox.kill_container("devops-bench-agent-stuck")  # must not raise


def test_kill_container_is_time_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """docker kill gets a timeout so a wedged daemon cannot hang the
    finally-reap and with it the whole batch."""
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    sandbox.kill_container("devops-bench-agent-ws")
    assert captured["timeout"] is not None


def test_sweep_stray_containers_never_raises_when_docker_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(argv, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(sandbox, "run", fake_run)
    sandbox.sweep_stray_containers()  # must not raise


def test_executor_run_raises_sandbox_error_on_125_under_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With check=True the host run raises before the returncode test, so the
    125 translation must also cover the exception path — otherwise the next
    harness migrated with default check=True scores docker failures as its
    agent's."""
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))

    def fake_run(argv, **kwargs):
        if argv[:2] == ["docker", "run"]:
            raise SubprocessError(argv, returncode=125, stderr="No such image")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    with pytest.raises(SandboxError, match="could not start the sandbox"):
        executor.run(["gemini"], check=True)


def test_executor_run_propagates_non_docker_failures_under_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))

    def fake_run(argv, **kwargs):
        if argv[:2] == ["docker", "run"]:
            raise SubprocessError(argv, returncode=7, stderr="agent failed")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    with pytest.raises(SubprocessError):
        executor.run(["gemini"], check=True)


def test_discover_fixture_mounts_never_mounts_the_bench_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No token rule can exclude the checkout for a cluster named "bench" or
    "dev" — the exclusion is by path: glob hits are skipped, an explicit
    override is refused loudly."""
    home = tmp_path / "home"
    home.mkdir()
    checkout = home / "devops-bench-dev"
    (checkout / "tasks").mkdir(parents=True)
    (home / "opa-repo-dev.git").mkdir()
    monkeypatch.setattr(sandbox, "_BENCH_REPO_ROOT", checkout.resolve())
    monkeypatch.setattr(sandbox.Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv(sandbox.FIXTURES_ENV, raising=False)

    mounts = sandbox.discover_fixture_mounts("dev")
    assert sorted(mounts.values()) == ["/workspace/home/opa-repo-dev.git"]

    monkeypatch.setenv(sandbox.FIXTURES_ENV, str(checkout))
    with pytest.raises(SandboxError, match="overlaps the benchmark checkout"):
        sandbox.discover_fixture_mounts("dev")


def test_executor_run_keeps_secret_values_out_of_the_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The overlay's values ride the docker client's environment (extra_env
    on the host run), never the ``docker run`` command line: the argv is
    world-readable in /proc for the whole run and is rendered into
    SubprocessError messages — which the harness embeds in errored results —
    so an inline key would leak on every timeout."""
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))
    seen: dict = {}

    def fake_run(argv, **kwargs):
        if argv[:2] == ["docker", "run"]:
            seen["argv"] = argv
            seen["extra_env"] = kwargs.get("extra_env")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    executor.run(
        ["gemini"],
        extra_env={"GEMINI_API_KEY": "sekrit", "GOOGLE_APPLICATION_CREDENTIALS": "/adc.json"},
    )

    # The name crosses as a name-only -e flag; the value only via extra_env.
    assert "GEMINI_API_KEY" in seen["argv"]
    assert all("sekrit" not in part for part in seen["argv"])
    assert seen["extra_env"] == {"GEMINI_API_KEY": "sekrit"}
    # The deny filter guards the client-env transport too: a denied var's
    # value must not reach the docker client process either.
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in seen["extra_env"]


# -- the run_agent_cmd seam --------------------------------------------------------


def test_run_agent_cmd_defaults_to_the_harness_modules_run_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without host_run=, the seam resolves the concrete harness module's own
    ``run`` import — the symbol its unit tests patch — so forgetting the
    parameter cannot silently bypass test patches and hit a real subprocess."""
    import sys as _sys

    called: dict = {}

    def module_run(cmd, **kwargs):
        called["cmd"] = list(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_sys.modules[__name__], "run", module_run, raising=False)
    agent = _DummyAgent(AgentConfig())
    agent.run_agent_cmd(["echo", "hi"])
    assert called["cmd"] == ["echo", "hi"]


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
