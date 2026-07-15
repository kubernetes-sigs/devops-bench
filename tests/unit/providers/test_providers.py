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

import pytest
from pytest_mock import MockerFixture

from devops_bench.core import ConfigError
from devops_bench.providers import PROVIDERS, ResolveContext
from devops_bench.providers.gcp import GcpProvider
from devops_bench.providers.kind import KindProvider


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
    assert "gcp" in PROVIDERS
    assert "kind" in PROVIDERS


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
