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

"""Tests for cloud providers and the provider registry."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pytest_mock import MockerFixture

from devops_bench.core import (
    ClusterInfo,
    ConfigError,
    NetworkPlan,
    SandboxError,
    SubprocessError,
)
from devops_bench.providers import PROVIDERS, ResolveContext
from devops_bench.providers.base import Provider
from devops_bench.providers.gcp import GcpProvider
from devops_bench.providers.kind import KindProvider
from devops_bench.providers.vcluster import VClusterProvider


@pytest.fixture
def ctx() -> ResolveContext:
    return ResolveContext(
        stack="custom/stack",
        project_id="test-project",
        cluster_name="test-cluster",
        location="us-central1-a",
    )


def test_registry_populated() -> None:
    assert PROVIDERS.get("gcp") is GcpProvider
    assert PROVIDERS.get("kind") is KindProvider
    assert PROVIDERS.get("vcluster") is VClusterProvider
    assert "gcp" in PROVIDERS
    assert "kind" in PROVIDERS
    assert "vcluster" in PROVIDERS


# --- GcpProvider ---------------------------------------------------------------


def test_gcp_resolve_variables_fills_defaults(
    ctx: ResolveContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KUBECONFIG", raising=False)
    variables = GcpProvider().resolve_variables(ctx, {"node_count": 5, "cluster_name": "override"})
    assert variables == {
        "infra_provider": "gcp",
        "project_id": "test-project",
        "cluster_name": "override",  # custom value preserved
        "location": "us-central1-a",
        "node_count": 5,
    }


def test_gcp_resolve_variables_namespace_from_env(
    ctx: ResolveContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NAMESPACE", "team-a")
    variables = GcpProvider().resolve_variables(ctx, {})
    assert variables["namespace"] == "team-a"


def test_gcp_resolve_variables_kubeconfig_from_env(
    ctx: ResolveContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KUBECONFIG", "/path/to/kubeconfig")
    variables = GcpProvider().resolve_variables(ctx, {})
    assert variables["kubeconfig_path"] == "/path/to/kubeconfig"


def test_gcp_ensure_cluster_credentials_runs_gcloud_no_adc(
    mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GCP_USE_ADC", raising=False)
    mock_run = mocker.patch("devops_bench.providers.gcp.run")
    info = GcpProvider().ensure_cluster_credentials(
        "test-cluster", "us-central1-a", {"project_id": "test-project"}
    )

    assert info.name == "test-cluster"
    assert info.location == "us-central1-a"
    assert info.project == "test-project"
    assert mock_run.call_count == 1
    assert mock_run.call_args_list[0].args[0] == [
        "gcloud",
        "container",
        "clusters",
        "get-credentials",
        "test-cluster",
        "--location",
        "us-central1-a",
        "--project",
        "test-project",
    ]


def test_gcp_ensure_cluster_credentials_runs_gcloud_with_adc(
    mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GCP_USE_ADC", "true")
    mock_run = mocker.patch("devops_bench.providers.gcp.run")
    info = GcpProvider().ensure_cluster_credentials(
        "test-cluster", "us-central1-a", {"project_id": "test-project"}
    )

    assert info.name == "test-cluster"
    assert info.location == "us-central1-a"
    assert info.project == "test-project"
    assert mock_run.call_count == 2
    assert mock_run.call_args_list[0].args[0] == [
        "gcloud",
        "container",
        "clusters",
        "get-credentials",
        "test-cluster",
        "--location",
        "us-central1-a",
        "--project",
        "test-project",
    ]
    assert mock_run.call_args_list[1].args[0] == [
        "kubectl",
        "config",
        "set-credentials",
        "gke_test-project_us-central1-a_test-cluster",
        "--exec-arg=--use_application_default_credentials",
    ]


def test_gcp_ensure_cluster_credentials_project_from_env(
    mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    mocker.patch("devops_bench.providers.gcp.run")
    monkeypatch.setenv("GCP_PROJECT_ID", "env-project")
    info = GcpProvider().ensure_cluster_credentials("c", "us-central1-a", {})
    assert info.project == "env-project"


def test_gcp_ensure_cluster_credentials_no_project_raises(
    mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    mocker.patch("devops_bench.providers.gcp.run")
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
    with pytest.raises(ConfigError, match="Project ID not found"):
        GcpProvider().ensure_cluster_credentials("c", "us-central1-a", {})


def test_gcp_ensure_account_credentials_is_noop() -> None:
    # No exception, no external calls.
    GcpProvider().ensure_account_credentials()


def test_gcp_ensure_cluster_credentials_reads_the_agent_cloud_identity_output(
    mocker: MockerFixture,
) -> None:
    mocker.patch("devops_bench.providers.gcp.run")
    info = GcpProvider().ensure_cluster_credentials(
        "test-cluster",
        "us-central1-a",
        {"project_id": "test-project"},
        outputs={"agent_cloud_identity": "rot-x@test-project.iam.gserviceaccount.com"},
    )
    assert info.agent_cloud_identity == "rot-x@test-project.iam.gserviceaccount.com"


def test_gcp_cloud_credential_env_is_empty_without_an_identity(
    mocker: MockerFixture,
) -> None:
    mock_run = mocker.patch("devops_bench.providers.gcp.run")
    info = ClusterInfo(name="c", location="us-central1-a", project="p")
    assert GcpProvider().sandbox_cloud_credential_env(info) == {}
    mock_run.assert_not_called()


def test_gcp_cloud_credential_env_mints_an_impersonated_token(
    mocker: MockerFixture,
) -> None:
    mock_run = mocker.patch(
        "devops_bench.providers.gcp.run",
        return_value=SimpleNamespace(returncode=0, stdout="tok-123\n", stderr=""),
    )
    env = GcpProvider().sandbox_cloud_credential_env(_identity_info())
    assert env["CLOUDSDK_AUTH_ACCESS_TOKEN"] == "tok-123"
    assert env["GOOGLE_OAUTH_ACCESS_TOKEN"] == "tok-123"
    assert env["CLOUDSDK_CORE_PROJECT"] == "p"
    # The agent overlay owns GOOGLE_CLOUD_PROJECT (model routing); the mint must not clobber it.
    assert "GOOGLE_CLOUD_PROJECT" not in env
    assert mock_run.call_args.args[0] == [
        "gcloud",
        "auth",
        "print-access-token",
        "--impersonate-service-account=rot-x@p.iam.gserviceaccount.com",
        "--scopes=https://www.googleapis.com/auth/cloud-platform",
        "--lifetime=3600s",
    ]
    # Bounded: a stalled gcloud must become a failed record, not a hung batch.
    assert mock_run.call_args.kwargs["timeout"] == 60


def _identity_info() -> ClusterInfo:
    return ClusterInfo(
        name="c",
        location="us-central1-a",
        project="p",
        agent_cloud_identity="rot-x@p.iam.gserviceaccount.com",
    )


def _lifetime_arg(call: Any) -> str:
    return next(a for a in call.args[0] if a.startswith("--lifetime="))


def test_gcp_cloud_credential_env_requests_the_agent_token_budget(mocker: MockerFixture) -> None:
    mock_run = mocker.patch(
        "devops_bench.providers.gcp.run",
        return_value=SimpleNamespace(returncode=0, stdout="tok\n", stderr=""),
    )
    GcpProvider().sandbox_cloud_credential_env(_identity_info(), lifetime_sec=5400)
    assert mock_run.call_count == 1
    assert _lifetime_arg(mock_run.call_args) == "--lifetime=5400s"


def test_gcp_cloud_credential_env_falls_back_to_an_hour_when_extension_is_refused(
    mocker: MockerFixture, caplog: pytest.LogCaptureFixture
) -> None:
    mock_run = mocker.patch(
        "devops_bench.providers.gcp.run",
        side_effect=[
            SimpleNamespace(returncode=1, stdout="", stderr="lifetime exceeds policy"),
            SimpleNamespace(returncode=0, stdout="tok\n", stderr=""),
        ],
    )
    with caplog.at_level("WARNING"):
        env = GcpProvider().sandbox_cloud_credential_env(_identity_info(), lifetime_sec=5400)
    assert env["CLOUDSDK_AUTH_ACCESS_TOKEN"] == "tok"
    assert [_lifetime_arg(c) for c in mock_run.call_args_list] == [
        "--lifetime=5400s",
        "--lifetime=3600s",
    ]
    assert "lifetime exceeds policy" in caplog.text
    assert "expires before the agent's token budget" in caplog.text


def test_gcp_cloud_credential_env_does_not_retry_a_budget_within_an_hour(
    mocker: MockerFixture,
) -> None:
    mock_run = mocker.patch(
        "devops_bench.providers.gcp.run",
        return_value=SimpleNamespace(returncode=1, stdout="", stderr="denied"),
    )
    with pytest.raises(SandboxError, match="denied"):
        GcpProvider().sandbox_cloud_credential_env(_identity_info(), lifetime_sec=1500)
    assert mock_run.call_count == 1


@pytest.mark.parametrize(
    "exc",
    [SubprocessError(["gcloud"], returncode=-1, stdout="", stderr=""), FileNotFoundError("gcloud")],
)
def test_gcp_cloud_credential_env_turns_a_hung_or_missing_gcloud_into_a_sandbox_error(
    mocker: MockerFixture, exc: Exception
) -> None:
    mocker.patch("devops_bench.providers.gcp.run", side_effect=exc)
    with pytest.raises(SandboxError, match="bounded at 60s"):
        GcpProvider().sandbox_cloud_credential_env(_identity_info())


def test_gcp_cloud_credential_env_fails_loud_when_the_mint_fails(
    mocker: MockerFixture,
) -> None:
    mocker.patch(
        "devops_bench.providers.gcp.run",
        return_value=SimpleNamespace(returncode=1, stdout="", stderr="PERMISSION_DENIED"),
    )
    info = ClusterInfo(name="c", agent_cloud_identity="rot-x@p.iam.gserviceaccount.com")
    with pytest.raises(SandboxError, match="serviceAccountTokenCreator") as excinfo:
        GcpProvider().sandbox_cloud_credential_env(info)
    assert "PERMISSION_DENIED" in str(excinfo.value)


class _MinimalProvider(Provider):
    def ensure_account_credentials(self) -> None: ...
    def ensure_cluster_credentials(self, *args: Any, **kwargs: Any) -> ClusterInfo:
        return ClusterInfo(name="c")

    def cleanup(self, *args: Any, **kwargs: Any) -> None: ...
    def resolve_variables(
        self, ctx: ResolveContext, custom_variables: dict[str, Any]
    ) -> dict[str, Any]:
        return custom_variables


def test_provider_default_cloud_credential_env_is_empty() -> None:
    assert _MinimalProvider().sandbox_cloud_credential_env(ClusterInfo(name="c")) == {}


def test_provider_default_cloud_credential_env_refuses_a_named_identity() -> None:
    info = ClusterInfo(name="c", agent_cloud_identity="rot-x@p.iam.gserviceaccount.com")
    with pytest.raises(SandboxError, match="_MinimalProvider cannot mint"):
        _MinimalProvider().sandbox_cloud_credential_env(info)


# --- KindProvider --------------------------------------------------------------


def test_kind_resolve_variables_fills_defaults(ctx: ResolveContext) -> None:
    variables = KindProvider().resolve_variables(ctx, {})
    assert variables["cluster_name"] == "test-cluster"
    assert variables["location"] == "local"
    expected_kubeconfig = os.environ.get("KUBECONFIG") or str(
        Path("~/.kube/config").expanduser().resolve()
    )
    assert variables["kubeconfig_path"] == expected_kubeconfig


def test_kind_resolve_variables_default_cluster_name() -> None:
    empty_ctx = ResolveContext(stack="prebuilt/kind", project_id="", cluster_name="", location="")
    variables = KindProvider().resolve_variables(empty_ctx, {})
    assert variables["cluster_name"] == "devops-bench-kind"


def test_kind_ensure_cluster_credentials_no_gcloud(mocker: MockerFixture) -> None:
    # KinD must never shell out for credentials. Patch the shared command runner
    # at its source so any shell-out is caught regardless of import path.
    mock_run = mocker.patch("devops_bench.core.subprocess.run")
    info = KindProvider().ensure_cluster_credentials("kind-cluster", "local", {})
    assert info.name == "kind-cluster"
    assert info.location == "local"
    assert info.project == "local-kind"  # fallback when no project set
    mock_run.assert_not_called()


def test_kind_ensure_account_credentials_is_noop() -> None:
    KindProvider().ensure_account_credentials()


# -- sandbox network plans --------------------------------------------------


def test_base_provider_refuses_to_be_sandboxed_without_an_override() -> None:
    """A forgotten override fails as a plain refusal, not a run on the wrong cluster."""

    class _BareProvider(Provider):
        def ensure_account_credentials(self) -> None: ...

        def ensure_cluster_credentials(
            self,
            cluster_name: str,
            location: str,
            variables: dict[str, Any],
            outputs: dict[str, Any] | None = None,
        ) -> ClusterInfo:
            raise NotImplementedError

        def resolve_variables(
            self, ctx: ResolveContext, custom_variables: dict[str, Any]
        ) -> dict[str, Any]:
            raise NotImplementedError

    with pytest.raises(SandboxError, match="does not implement sandbox_network_plan"):
        _BareProvider().sandbox_network_plan(ClusterInfo(name="c1"))


def test_kind_plan_joins_the_kind_network_and_rewrites_the_server() -> None:
    plan = KindProvider().sandbox_network_plan(ClusterInfo(name="c1"))

    assert plan.docker_network == "kind"
    assert plan.rewrite_server == "https://c1-control-plane:6443"
    assert plan.kubectl_context == "kind-c1"
    # The apiserver cert covers the control-plane node name, so no override.
    assert plan.tls_server_name is None


def test_gcp_plan_pins_the_context_without_rewriting() -> None:
    """A GKE endpoint routes as-is; the plan only pins the context."""
    info = ClusterInfo(name="c1", location="us-central1-a", project="p")

    plan = GcpProvider().sandbox_network_plan(info)

    assert plan == NetworkPlan(kubectl_context="gke_p_us-central1-a_c1")


def test_gcp_plan_refuses_when_the_cluster_is_underspecified() -> None:
    """Unpinned would mint the agent's credential on the ambient context."""
    with pytest.raises(SandboxError, match="project/location"):
        GcpProvider().sandbox_network_plan(ClusterInfo(name="c1"))


def test_vcluster_plan_pins_the_virtual_clusters_own_context(tmp_path: Path) -> None:
    """The pin keeps the agent's ServiceAccount inside the virtual cluster."""
    kubeconfig = tmp_path / "vcluster.yaml"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\ncurrent-context: vcluster-c1\n")

    plan = VClusterProvider().sandbox_network_plan(
        ClusterInfo(name="c1", kubeconfig_path=str(kubeconfig))
    )

    assert plan == NetworkPlan(kubectl_context="vcluster-c1")


def test_vcluster_plan_refuses_rather_than_dropping_the_pin(tmp_path: Path) -> None:
    """Unpinned would mint the agent's credential on the host cluster."""
    with pytest.raises(SandboxError, match="host cluster"):
        VClusterProvider().sandbox_network_plan(
            ClusterInfo(name="c1", kubeconfig_path=str(tmp_path / "missing.yaml"))
        )


def test_vcluster_plan_refuses_without_a_kubeconfig_path() -> None:
    with pytest.raises(SandboxError, match="host cluster"):
        VClusterProvider().sandbox_network_plan(ClusterInfo(name="c1"))
