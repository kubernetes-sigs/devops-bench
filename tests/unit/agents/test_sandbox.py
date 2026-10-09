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

import json
from collections.abc import Callable
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


def _complete_spec(tmp_path: Path, **overrides: object) -> sandbox.SandboxSpec:
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


def test_image_digest_prefers_the_repo_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    """RepoDigests is the cross-host identity; the local Id is only the never-pushed fallback."""
    inspected = [
        {
            "Id": "sha256:aaaa",
            "RepoDigests": ["registry.example/agent-sandbox@sha256:bbbb"],
        }
    ]

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        assert argv == ["docker", "image", "inspect", "agent-sandbox:v1"]
        return SimpleNamespace(returncode=0, stdout=json.dumps(inspected), stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    assert sandbox.image_digest("agent-sandbox:v1") == (
        "registry.example/agent-sandbox@sha256:bbbb"
    )


def test_image_digest_falls_back_to_the_local_id(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=0, stdout=json.dumps([{"Id": "sha256:aaaa", "RepoDigests": []}]), stderr=""
        )

    monkeypatch.setattr(sandbox, "run", fake_run)
    assert sandbox.image_digest("agent-sandbox:dev") == "sha256:aaaa"


def _unknown_image(argv: list[str], **kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(returncode=1, stdout="", stderr="No such image")


def _malformed(argv: list[str], **kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout="not-json", stderr="")


def _missing_runtime(argv: list[str], **kwargs: object) -> SimpleNamespace:
    raise FileNotFoundError("docker")


def _wedged_daemon(argv: list[str], **kwargs: object) -> SimpleNamespace:
    assert kwargs.get("timeout"), "inspect must be bounded"
    raise sandbox.SubprocessError(argv, returncode=-1, stdout="", stderr="")


@pytest.mark.parametrize(
    "fake_run",
    [_unknown_image, _malformed, _missing_runtime, _wedged_daemon],
    ids=["unknown-image", "malformed-output", "missing-runtime", "timeout"],
)
def test_image_digest_never_raises(
    monkeypatch: pytest.MonkeyPatch, fake_run: Callable[..., SimpleNamespace]
) -> None:
    """Every failure mode yields None rather than raising."""
    monkeypatch.setattr(sandbox, "run", fake_run)
    assert sandbox.image_digest("nope:latest") is None


def test_kill_container_invokes_docker_kill_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
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

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=1, stdout="", stderr="No such container")

    monkeypatch.setattr(sandbox, "run", fake_run)
    sandbox.kill_container("devops-bench-agent-gone")  # must not raise


def test_sweep_stray_containers_kills_only_matching_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        if argv[:2] == ["docker", "ps"]:
            return SimpleNamespace(
                returncode=0,
                stdout="devops-bench-agent-attemptA-ws\ndevops-bench-agent-attemptAB-ws\ndevops-bench-agent-legacy\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    sandbox.sweep_stray_containers(owner="attemptA")

    list_call = calls[0]
    assert list_call[0:2] == ["docker", "ps"]
    assert "{{.Names}}" in list_call
    kill_calls = [c for c in calls if c[:2] == ["docker", "kill"]]
    assert kill_calls == [["docker", "kill", "devops-bench-agent-attemptA-ws"]]


def test_sweep_stray_containers_handles_docker_ps_failure_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=1, stdout="", stderr="docker daemon not running")

    monkeypatch.setattr(sandbox, "run", fake_run)
    sandbox.sweep_stray_containers()  # must not raise


def test_sweep_stray_containers_is_a_noop_when_none_are_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
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
    monkeypatch: pytest.MonkeyPatch,
    *,
    contexts: tuple[str, ...] = (),
    server: str = "",
    tls_server_name: str = "",
) -> None:
    """Answer the kubectl reads a plan build makes; the context probe uses
    ``sandbox.run``, the server reads ``k8s.kubectl``, so patch both."""

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        if argv[:3] == ["kubectl", "config", "get-contexts"]:
            return SimpleNamespace(returncode=0, stdout="\n".join(contexts) + "\n", stderr="")
        # ``--context`` sits right after the binary, so match the subcommand.
        assert "view" in argv
        # Server and tls-server-name come back from one jsonpath read, newline-separated.
        return SimpleNamespace(returncode=0, stdout=f"{server}\n{tls_server_name}", stderr="")

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
    # A provider-supplied rewrite is left alone: kind's in-network name verifies as-is.
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
    """Loopback is remapped; TLS is redirected to the ``localhost`` SAN, not disabled."""
    _patch_plan_reads(monkeypatch, contexts=("kind-c1",), server=server)

    plan = sandbox.build_network_plan(
        _FakeProvider(NetworkPlan(kubectl_context="kind-c1")), _cluster()
    )

    assert plan.rewrite_server == expected
    assert plan.tls_server_name == "localhost"


def test_build_network_plan_preserves_a_declared_tls_server_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cluster that needs a ``tls-server-name`` override outside the sandbox needs the
    same one inside; hardcoding ``localhost`` would break TLS."""
    _patch_plan_reads(
        monkeypatch,
        contexts=("kind-c1",),
        server="https://127.0.0.1:6443",
        tls_server_name="10.96.0.1",
    )

    plan = sandbox.build_network_plan(
        _FakeProvider(NetworkPlan(kubectl_context="kind-c1")), _cluster()
    )

    assert plan.rewrite_server == "https://host.docker.internal:6443"
    assert plan.tls_server_name == "10.96.0.1"


def test_build_network_plan_leaves_a_routable_server_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A routable endpoint must not be rewritten."""
    provider = _FakeProvider(NetworkPlan(kubectl_context="gke_p_us-central1_c1"))
    _patch_plan_reads(monkeypatch, contexts=("gke_p_us-central1_c1",), server="https://34.10.0.1")

    plan = sandbox.build_network_plan(provider, _cluster())

    assert plan.rewrite_server is None
    assert plan.tls_server_name is None
    assert plan.docker_network is None


def test_build_network_plan_accepts_a_deployer_without_a_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No provider (no-op deployer) yields the default ambient plan, not a refusal."""
    _patch_plan_reads(monkeypatch, server="https://34.10.0.1")

    assert sandbox.build_network_plan(None, _cluster()) == NetworkPlan()


def test_build_network_plan_refuses_a_context_kubectl_does_not_know(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refuse when this kubeconfig never saw the provider-named context."""
    provider = _FakeProvider(NetworkPlan(kubectl_context="kind-c1"))
    _patch_plan_reads(monkeypatch, contexts=("kind-someone-elses-cluster",))

    with pytest.raises(SandboxError, match="kind-c1"):
        sandbox.build_network_plan(provider, _cluster())


def test_build_network_plan_refuses_an_unreadable_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_plan_reads(monkeypatch, contexts=("kind-c1",), server="")

    with pytest.raises(SandboxError, match="server URL"):
        sandbox.build_network_plan(
            _FakeProvider(NetworkPlan(kubectl_context="kind-c1")), _cluster()
        )


def test_build_network_plan_refuses_a_provider_backed_plan_without_a_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unpinned plan from a provider would mint the credential on the ambient
    current-context, which only a provider-less run may waive."""
    _patch_plan_reads(monkeypatch, server="https://34.10.0.1")

    with pytest.raises(SandboxError, match="no\\s+kubectl context pin"):
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
    """Two same-named fixtures would map onto one container destination (a docker
    'Duplicate mount point' abort); refuse up front, naming both host paths."""
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
        "CLOUDSDK_AUTH_ACCESS_TOKEN": "ya29.host",
        "GOOGLE_APPLICATION_CREDENTIALS": "/home/op/adc.json",
        "GOOGLE_OAUTH_ACCESS_TOKEN": "ya29.host",
        # Routing, not credentials: shares the GOOGLE_ prefix and must cross.
        "GOOGLE_GENAI_USE_VERTEXAI": "true",
        "GOOGLE_CLOUD_PROJECT": "proj",
        "BENCH_CHEAT_DETECT": "0",
        "TF_VAR_project": "p",
        "AWS_ACCESS_KEY_ID": "AKIA...",
        "AZURE_CLIENT_SECRET": "s",
        "ARM_CLIENT_ID": "c",
        "HOME": "/home/op",
        "KUBECONFIG": "/home/op/.kube/config",
    }
    kept = sandbox.filter_boundary_env(overlay)
    assert kept == {
        "GEMINI_API_KEY": "k",
        "GEMINI_MODEL": "m",
        "OTEL_SDK_DISABLED": "true",
        "GOOGLE_GENAI_USE_VERTEXAI": "true",
        "GOOGLE_CLOUD_PROJECT": "proj",
    }


def test_filter_boundary_env_allowlist_overrides_a_denial() -> None:
    kept = sandbox.filter_boundary_env({"TF_VAR_task_input": "x"}, allowlist=("TF_VAR_task_input",))
    assert kept == {"TF_VAR_task_input": "x"}


def test_filter_boundary_env_never_admits_container_owned_vars() -> None:
    """HOME/KUBECONFIG/PATH are container-owned; docker's last ``-e`` wins, so even an
    allowlist must not let an overlay value repoint them."""
    overlay = {"HOME": "/home/op", "KUBECONFIG": "/home/op/.kube/config", "PATH": "/evil/bin"}
    kept = sandbox.filter_boundary_env(overlay, allowlist=("HOME", "KUBECONFIG", "PATH"))
    assert kept == {}


def test_filter_boundary_env_handles_none_overlay() -> None:
    assert sandbox.filter_boundary_env(None) == {}


# -- executor: spec validation ------------------------------------------------


def test_executor_refuses_a_spec_without_an_image(tmp_path: Path) -> None:
    with pytest.raises(SandboxError, match="BENCH_SANDBOX_IMAGE"):
        sandbox.SandboxExecutor(_complete_spec(tmp_path, image=""))


def test_executor_refuses_a_spec_whose_paths_do_not_exist(tmp_path: Path) -> None:
    """Set is not enough: a stale or misbuilt spec must fail here, not as a
    cryptic docker bind-mount error mid-run."""
    with pytest.raises(SandboxError, match="missing workspace or kubeconfig"):
        sandbox.SandboxExecutor(_complete_spec(tmp_path, workspace=tmp_path / "nope"))
    with pytest.raises(SandboxError, match="missing workspace or kubeconfig"):
        sandbox.SandboxExecutor(_complete_spec(tmp_path, kubeconfig=tmp_path / "nope.kc"))


def test_executor_refuses_an_incomplete_spec(tmp_path: Path) -> None:
    """The skeletal from_env spec (no workspace/kubeconfig) must never run."""
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
    # Boundary invariants: no stdin, host-gateway alias, no capabilities, no setuid
    # re-escalation (the latter pair contains the no-``--user`` case).
    assert "-i" not in argv
    assert "host.docker.internal:host-gateway" in argv
    assert "--cap-drop=ALL" in argv
    assert "--security-opt=no-new-privileges=true" in argv
    # Mount set: workspace RW, kubeconfig RO.
    assert f"{spec.workspace}:/workspace" in argv
    assert f"{spec.kubeconfig}:/creds/kubeconfig:ro" in argv
    # Container-owned vars inline, the overlay as name-only -e flags: a secret value
    # must never appear in the argv (world-readable in /proc, rendered into errors).
    assert "HOME=/workspace/home" in argv
    assert "KUBECONFIG=/creds/kubeconfig" in argv
    assert "GEMINI_API_KEY" in argv
    assert all("sekrit" not in part for part in argv)
    # Default working directory is the workspace; image then the raw argv.
    assert argv[argv.index("-w") + 1] == "/workspace"
    assert argv[-4:] == ["agent-image", "gemini", "-p", "hi"]


def test_wrap_argv_launches_the_pinned_digest_over_the_tag(tmp_path: Path) -> None:
    """The reference docker runs is the one the manifest records; the tag alone can move."""
    spec = _complete_spec(tmp_path, image_digest="agent-image@sha256:feed")
    argv = sandbox.SandboxExecutor(spec).wrap_argv(["gemini", "-p", "hi"])
    assert argv[-4] == "agent-image@sha256:feed"
    assert "agent-image" not in argv[:-4]
    unpinned = sandbox.SandboxExecutor(_complete_spec(tmp_path)).wrap_argv(["gemini"])
    assert unpinned[-2] == "agent-image"


def test_wrap_argv_container_owned_env_flags_come_last(tmp_path: Path) -> None:
    """Defense in depth: the executor's own ``-e HOME``/``-e KUBECONFIG`` trail every
    overlay flag, so last-one-wins keeps them authoritative."""
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


def test_executor_run_refuses_an_outside_cwd_before_any_docker_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    with pytest.raises(SandboxError, match="outside the sandbox workspace"):
        executor.run(["gemini"], cwd=tmp_path / "elsewhere")
    # Neither docker run nor the finally-reap's docker kill ever fired.
    assert calls == []


def test_executor_run_rejects_a_full_environment(tmp_path: Path) -> None:
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))
    with pytest.raises(SandboxError, match="full environment"):
        executor.run(["gemini"], env={"ALL": "of it"})


def test_executor_run_rejects_stdin_input(tmp_path: Path) -> None:
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))
    with pytest.raises(SandboxError, match="stdin"):
        executor.run(["gemini"], input="data")


def test_executor_run_accepts_an_empty_stdin_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``input=""`` means closed stdin, which the container already has; not a refusal."""
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    result = executor.run(["claude", "-p", "hi"], check=False, input="")

    assert result.stdout == "ok"
    assert seen[0][:2] == ["docker", "run"]


def test_executor_run_reaps_the_container_on_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--rm`` cannot clean up a container whose ``docker run`` client was
    SIGKILLed by the host-side timeout; the executor must kill by name."""
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))
    kills: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
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

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
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

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        if argv[:2] == ["docker", "run"]:
            seen.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    executor.run(["gemini"], check=False, timeout=15.5)

    assert seen["check"] is False
    assert seen["timeout"] == 15.5


@pytest.mark.parametrize("code", [125, 126, 127])
def test_executor_run_raises_sandbox_error_on_docker_launch_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, code: int
) -> None:
    """125/126/127 are docker's own launch failures; with check=False they must not be
    scored as the agent's exit code."""
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))

    def fake_run(argv, **kwargs):
        if argv[:2] == ["docker", "run"]:
            return SimpleNamespace(returncode=code, stdout="", stderr="launch failed")
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


@pytest.mark.parametrize("code", [125, 126, 127])
def test_executor_run_raises_sandbox_error_on_launch_failures_under_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, code: int
) -> None:
    """With check=True the host run raises before the returncode test, so the translation
    must cover the exception path too."""
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))

    def fake_run(argv, **kwargs):
        if argv[:2] == ["docker", "run"]:
            raise SubprocessError(argv, returncode=code, stderr="launch failed")
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
    """The checkout is excluded by path, not token: glob hits are skipped and an explicit
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
    """Overlay values ride the docker client's environment, never the ``docker run`` argv,
    which is world-readable in /proc and rendered into error messages."""
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
    """Without host_run= the seam resolves the harness module's own ``run`` import, the
    symbol its tests patch, so test patches cannot be bypassed."""
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

    def fake_host_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
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

    def fake_core_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
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

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
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
    """An incomplete spec raises SandboxError and the safety net re-raises it: a broken
    boundary must record a failed, unscored run, not a badly-performing agent."""

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
    """A harness that never routed its subprocesses through run_agent_cmd would run
    ambient while flagged contained; the refusal is loud and before _execute."""
    agent = _DummyAgent(AgentConfig(sandbox=_complete_spec(tmp_path)))
    with pytest.raises(SandboxError, match="not been migrated"):
        agent.run("p")


def test_gemini_declares_sandbox_support() -> None:
    from devops_bench.agents.cli.gemini_cli.agent import GeminiCliAgent

    assert GeminiCliAgent.supports_sandbox is True
    # The base default stays False so a new harness must opt in explicitly.
    assert AgentHarness.supports_sandbox is False


# --- container_path: harnesses translate values, not just cwd ----------------


def test_container_path_maps_a_workspace_child(tmp_path) -> None:
    # Env values like OPENCLAW_STATE_DIR cross in the overlay and need the container spelling.
    assert sandbox.container_path(tmp_path, tmp_path / "state") == "/workspace/state"


def test_container_path_maps_the_workspace_root(tmp_path) -> None:
    assert sandbox.container_path(tmp_path, tmp_path) == "/workspace"


def test_container_path_refuses_a_path_outside_the_workspace(tmp_path) -> None:
    # The mount set is the boundary; it never widens to make a path exist.
    outside = tmp_path.parent / "elsewhere"
    with pytest.raises(SandboxError, match="outside the sandbox workspace"):
        sandbox.container_path(tmp_path, outside)


def test_cli_harness_sandbox_support_is_declared_deliberately() -> None:
    """Three harnesses declare the seam; claude_code opts out until its host paths are ported."""
    from devops_bench.agents.cli.antigravity.agent import AgyCliAgent
    from devops_bench.agents.cli.claude_code.agent import ClaudeCodeAgent
    from devops_bench.agents.cli.gemini_cli.agent import GeminiCliAgent
    from devops_bench.agents.cli.openclaw.agent import OpenClawAgent

    for cls in (AgyCliAgent, GeminiCliAgent, OpenClawAgent):
        assert cls.supports_sandbox is True, f"{cls.__name__} is not wired onto the seam"
    assert ClaudeCodeAgent.supports_sandbox is False


def test_wrap_argv_remaps_user_when_uid_exceeds_dockers_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An IdP-minted uid past docker's int32 ``--user`` limit is remapped, not refused."""
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox.os, "getuid", lambda: 3998470835)
    monkeypatch.setattr(sandbox.os, "getgid", lambda: 1000)

    argv = executor.wrap_argv(["gemini"])

    assert "--user" in argv
    assert argv[argv.index("--user") + 1] == "1000:1000"


def test_wrap_argv_remaps_user_when_gid_exceeds_dockers_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox.os, "getuid", lambda: 1000)
    monkeypatch.setattr(sandbox.os, "getgid", lambda: 3998470835)

    argv = executor.wrap_argv(["gemini"])

    assert "--user" in argv
    assert argv[argv.index("--user") + 1] == "1000:1000"


def test_executor_run_skips_chown_containers_when_ids_are_in_range(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """In-range ids (root included) take the old path: no chown containers."""
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox.os, "getuid", lambda: 1000)
    monkeypatch.setattr(sandbox.os, "getgid", lambda: 1000)

    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    executor.run(["gemini"], check=False)

    assert not any("chown" in call for call in calls)
    assert len(calls) == 2  # the agent container, then the by-name kill


def test_executor_run_chowns_workspace_and_fixtures_around_a_remapped_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Out-of-range uid: chown to the remap id and back brackets the run, fixtures included."""
    fixture = tmp_path / "fixture-repo"
    fixture.mkdir()
    spec = _complete_spec(tmp_path, fixture_mounts={str(fixture): "/workspace/home/fixture-repo"})
    executor = sandbox.SandboxExecutor(spec)
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox.os, "getuid", lambda: 3998470835)
    monkeypatch.setattr(sandbox.os, "getgid", lambda: 3998470835)

    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    executor.run(["gemini"], check=False)

    chown_calls = [call for call in calls if "chown" in call]
    assert len(chown_calls) == 2
    pre, post = chown_calls
    assert "1000:1000" in pre
    assert "--from=3998470835" in pre  # only the operator's entries move to the remap id
    assert "--from=1000" in post  # and only remap-owned entries come back
    assert f"{spec.workspace}:/workspace" in pre
    assert f"{fixture}:/workspace/home/fixture-repo" in pre
    assert "3998470835:3998470835" in post
    assert f"{spec.workspace}:/workspace" in post
    assert f"{fixture}:/workspace/home/fixture-repo" in post

    agent_call = next(
        call for call in calls if "chown" not in call and call[:2] == ["docker", "run"]
    )
    assert agent_call[agent_call.index("--user") + 1] == "1000:1000"


def test_executor_run_chowns_workspace_and_fixtures_when_only_the_gid_is_out_of_range(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox.os, "getuid", lambda: 1000)
    monkeypatch.setattr(sandbox.os, "getgid", lambda: 3998470835)

    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    executor.run(["gemini"], check=False)

    chown_calls = [call for call in calls if "chown" in call]
    assert len(chown_calls) == 2
    assert "1000:1000" in chown_calls[0]
    assert "1000:3998470835" in chown_calls[1]


def test_executor_run_chowns_back_even_when_the_agent_container_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The handback runs in a ``finally``, so an agent crash cannot strand the artifacts."""
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox.os, "getuid", lambda: 3998470835)
    monkeypatch.setattr(sandbox.os, "getgid", lambda: 1000)

    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["docker", "run"] and "chown" not in argv:
            raise SubprocessError(argv, returncode=1, stdout="", stderr="agent crashed")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    with pytest.raises(SubprocessError):
        executor.run(["gemini"])

    chown_calls = [call for call in calls if "chown" in call]
    assert len(chown_calls) == 2


def test_executor_run_raises_sandboxerror_when_the_pre_run_chown_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fatal: without the pre-run chown the remapped agent cannot write its workspace."""
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox.os, "getuid", lambda: 3998470835)
    monkeypatch.setattr(sandbox.os, "getgid", lambda: 1000)

    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["docker", "kill"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="No such container")
        if "chown" in argv:
            raise SubprocessError(argv, returncode=1, stdout="", stderr="boom")
        raise AssertionError("the agent container must not run when the pre-run chown fails")

    monkeypatch.setattr(sandbox, "run", fake_run)
    with pytest.raises(SandboxError, match="chown"):
        executor.run(["gemini"])
    # A partial pre-run chown still gets the handback attempt.
    assert len([c for c in calls if "chown" in c]) == 2


def test_remap_chowns_are_time_bounded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A wedged daemon must not hang the harness from either chown pass."""
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox.os, "getuid", lambda: 3998470835)
    monkeypatch.setattr(sandbox.os, "getgid", lambda: 1000)
    timeouts: list[object] = []

    def fake_run(argv, **kwargs):
        if "chown" in argv:
            timeouts.append(kwargs.get("timeout"))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    executor.run(["gemini"], check=False)
    assert timeouts == [sandbox._HOUSEKEEPING_TIMEOUT_SEC] * 2


def test_executor_run_handback_oserror_does_not_mask_a_successful_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An OSError from the finally-block chown must not replace the agent's result."""
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox.os, "getuid", lambda: 3998470835)
    monkeypatch.setattr(sandbox.os, "getgid", lambda: 1000)

    def fake_run(argv, **kwargs):
        if "chown" in argv and "3998470835:1000" in argv:
            raise FileNotFoundError("docker")
        return SimpleNamespace(returncode=0, stdout="agent output", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    with caplog.at_level("ERROR"):
        result = executor.run(["gemini"], check=False)
    assert result.stdout == "agent output"
    assert "chown" in caplog.text


def test_executor_run_raises_sandboxerror_when_the_pre_run_chown_cannot_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox.os, "getuid", lambda: 3998470835)
    monkeypatch.setattr(sandbox.os, "getgid", lambda: 1000)

    def fake_run(argv, **kwargs):
        if argv[:2] == ["docker", "kill"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="No such container")
        if "chown" in argv:
            raise FileNotFoundError("docker")
        raise AssertionError("the agent container must not run when the pre-run chown fails")

    monkeypatch.setattr(sandbox, "run", fake_run)
    with pytest.raises(SandboxError, match="chown"):
        executor.run(["gemini"])


def test_needs_id_remap_is_false_off_linux(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    monkeypatch.setattr(sandbox.os, "getuid", lambda: 3998470835)
    assert executor._needs_id_remap() is False
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    assert executor._needs_id_remap() is True


def test_wrap_argv_omits_user_flag_on_non_linux_even_when_ids_are_out_of_range(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Non-Linux still omits ``--user`` entirely; the remap is Linux-only."""
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    monkeypatch.setattr(sandbox.os, "getuid", lambda: 3998470835)
    monkeypatch.setattr(sandbox.os, "getgid", lambda: 3998470835)

    assert "--user" not in executor.wrap_argv(["gemini"])


def test_unscoped_sweep_is_skipped_under_parallel(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unscoped prefix match could reap a sibling harness's live container."""
    monkeypatch.setattr(sandbox, "run", lambda *a, **kw: pytest.fail("unscoped docker call"))
    sandbox.sweep_stray_containers(parallel=True)


def test_unscoped_sweep_reaps_every_prefixed_container_when_serial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Serial, no owner: every prefixed container is a stray (the crash-recovery case)."""
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        if argv[:2] == ["docker", "ps"]:
            return SimpleNamespace(
                returncode=0,
                stdout="devops-bench-agent-ws1\ndevops-bench-agent-attemptA-ws2\nunrelated\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    sandbox.sweep_stray_containers(parallel=False)

    kill_calls = [c for c in calls if c[:2] == ["docker", "kill"]]
    assert kill_calls == [
        ["docker", "kill", "devops-bench-agent-ws1"],
        ["docker", "kill", "devops-bench-agent-attemptA-ws2"],
    ]


def test_owner_scoped_sweep_runs_even_under_parallel(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        if argv[:2] == ["docker", "ps"]:
            return SimpleNamespace(
                returncode=0, stdout="devops-bench-agent-attemptA-ws\n", stderr=""
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    sandbox.sweep_stray_containers(owner="attemptA", parallel=True)

    assert [c for c in calls if c[:2] == ["docker", "kill"]] == [
        ["docker", "kill", "devops-bench-agent-attemptA-ws"]
    ]


def test_owner_is_part_of_container_name(tmp_path: Path) -> None:
    spec = _complete_spec(tmp_path, owner="attemptA")
    executor = sandbox.SandboxExecutor(spec)
    assert executor.container_name == f"devops-bench-agent-attemptA-{spec.workspace.name}"
    assert sandbox.container_name_for_workspace(tmp_path) == f"devops-bench-agent-{tmp_path.name}"


def test_spec_from_env_carries_a_valid_owner() -> None:
    spec = sandbox.spec_from_env({"BENCH_AGENT_SANDBOX": "1", "BENCH_AGENT_SANDBOX_OWNER": "run_7"})
    assert spec is not None and spec.owner == "run_7"


@pytest.mark.parametrize("owner", ["bad-owner", "a" * 129, "sp ace"])
def test_spec_from_env_rejects_a_malformed_owner(owner: str) -> None:
    """Refused at opt-in; from inside the executor it would score as an errored agent."""
    with pytest.raises(SandboxError, match="BENCH_AGENT_SANDBOX_OWNER"):
        sandbox.spec_from_env({"BENCH_AGENT_SANDBOX": "1", "BENCH_AGENT_SANDBOX_OWNER": owner})


def test_chown_helper_is_named_and_killed_before_the_handback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A timed-out chown helper must not keep changing ownership during the handback."""
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox.os, "getuid", lambda: 3998470835)
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    executor.run(["agent"], check=False)

    helper = f"{executor.container_name}-chown"
    pre = calls[0]
    assert "chown" in pre and pre[pre.index("--name") + 1] == helper
    killed = calls.index(["docker", "kill", helper])
    handback = next(i for i, c in enumerate(calls) if "3998470835:1000" in c)
    assert killed < handback


def test_remap_covers_external_generated_kubeconfig(tmp_path: Path) -> None:
    spec = _complete_spec(tmp_path)
    executor = sandbox.SandboxExecutor(spec)
    assert (str(spec.kubeconfig), sandbox.CONTAINER_KUBECONFIG) in executor._remap_mounts()


@pytest.fixture(autouse=True)
def _ordinary_host_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ordinary-path tests independent of the host's real ids."""
    monkeypatch.setattr(
        sandbox,
        "os",
        SimpleNamespace(
            environ=sandbox.os.environ,
            PathLike=sandbox.os.PathLike,
            getuid=lambda: 1000,
            getgid=lambda: 1000,
        ),
    )


def test_remap_timeout_stops_agent_before_restoring_ownership(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox.os, "getuid", lambda: 3998470835)
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        if argv[:2] == ["docker", "run"] and "chown" not in argv:
            raise SubprocessError(argv, returncode=-1, stdout="", stderr="timeout")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    with pytest.raises(SubprocessError):
        executor.run(["agent"], timeout=1)
    killed = next(i for i, argv in enumerate(calls) if argv[:2] == ["docker", "kill"])
    restored = next(i for i, argv in enumerate(calls) if "3998470835:1000" in argv)
    assert killed < restored


def test_invalid_wrap_never_changes_mount_ownership(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox.os, "getuid", lambda: 3998470835)
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    with pytest.raises(SandboxError):
        executor.run(["agent"], cwd=tmp_path / "outside")
    assert not calls


def test_executor_run_handback_failure_does_not_mask_a_successful_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed handback is logged with the repair command; the agent result still returns."""
    executor = sandbox.SandboxExecutor(_complete_spec(tmp_path))
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox.os, "getuid", lambda: 3998470835)
    monkeypatch.setattr(sandbox.os, "getgid", lambda: 1000)

    def fake_run(argv, **kwargs):
        if argv[:2] == ["docker", "kill"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="No such container")
        if "chown" in argv and "3998470835:1000" in argv:
            raise SubprocessError(argv, returncode=1, stdout="", stderr="boom")
        return SimpleNamespace(returncode=0, stdout="agent output", stderr="")

    monkeypatch.setattr(sandbox, "run", fake_run)
    with caplog.at_level("ERROR"):
        result = executor.run(["gemini"], check=False)

    assert result.stdout == "agent output"
    assert "docker run --rm" in caplog.text
    assert "chown" in caplog.text
    assert "3998470835:1000" in caplog.text
