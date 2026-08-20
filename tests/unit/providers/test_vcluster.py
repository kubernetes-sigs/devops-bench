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

"""Tests for the vcluster provider."""

from __future__ import annotations

from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from devops_bench.providers import PROVIDERS, ResolveContext
from devops_bench.providers.vcluster import VclusterProvider


@pytest.fixture
def ctx() -> ResolveContext:
    return ResolveContext(
        stack="prebuilt/vcluster",
        project_id="test-project",
        cluster_name="test-cluster",
        location="us-central1",
    )


def test_registry_populated() -> None:
    assert PROVIDERS.get("vcluster") is VclusterProvider
    assert "vcluster" in PROVIDERS


def test_resolve_variables_fills_defaults(
    ctx: ResolveContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VCLUSTER_HOST_CLUSTER", raising=False)
    monkeypatch.delenv("VCLUSTER_HOST_CONTEXT", raising=False)
    monkeypatch.delenv("VCLUSTER_HOST_CLOUD", raising=False)
    variables = VclusterProvider().resolve_variables(ctx, {})
    assert variables["infra_provider"] == "vcluster"
    assert variables["project_id"] == "test-project"
    assert variables["cluster_name"] == "test-cluster"
    assert variables["location"] == "us-central1"
    assert variables["host_cloud"] == "gke"
    assert variables["host_cluster_name"] == ""
    assert "host_context" not in variables
    expected_kubeconfig = str(Path("~/.kube").expanduser() / "vcluster-test-cluster.yaml")
    assert variables["kubeconfig_path"] == expected_kubeconfig


def test_resolve_variables_host_cloud_env(
    ctx: ResolveContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VCLUSTER_HOST_CLOUD", "eks")
    variables = VclusterProvider().resolve_variables(ctx, {})
    assert variables["host_cloud"] == "eks"


def test_resolve_variables_custom_precedence(ctx: ResolveContext) -> None:
    variables = VclusterProvider().resolve_variables(
        ctx, {"cluster_name": "override", "kubeconfig_path": "/tmp/custom.yaml"}
    )
    assert variables["cluster_name"] == "override"
    assert variables["kubeconfig_path"] == "/tmp/custom.yaml"


def test_resolve_variables_host_env(ctx: ResolveContext, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VCLUSTER_HOST_CLUSTER", "host-cluster")
    monkeypatch.setenv("VCLUSTER_HOST_CONTEXT", "my-context")
    variables = VclusterProvider().resolve_variables(ctx, {})
    assert variables["host_cluster_name"] == "host-cluster"
    assert variables["host_context"] == "my-context"


def test_ensure_cluster_credentials_returns_cluster_info(mocker: MockerFixture) -> None:
    mock_run = mocker.patch("devops_bench.providers.vcluster.run")
    info = VclusterProvider().ensure_cluster_credentials(
        "test-cluster",
        "us-central1",
        {"project_id": "test-project", "kubeconfig_path": "/tmp/vcluster-test-cluster.yaml"},
    )
    assert info.name == "test-cluster"
    assert info.location == "us-central1"
    assert info.project == "test-project"
    assert info.kubeconfig_path == "/tmp/vcluster-test-cluster.yaml"
    mock_run.assert_not_called()


def test_ensure_account_credentials_noop_when_env_unset(
    mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VCLUSTER_HOST_CLUSTER", raising=False)
    mock_run = mocker.patch("devops_bench.providers.vcluster.run")
    VclusterProvider().ensure_account_credentials()
    mock_run.assert_not_called()


def test_ensure_account_credentials_runs_gcloud_when_host_cluster_set(
    mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VCLUSTER_HOST_CLUSTER", "host-cluster")
    monkeypatch.setenv("GCP_PROJECT_ID", "test-project")
    monkeypatch.setenv("VCLUSTER_HOST_LOCATION", "us-central1")
    monkeypatch.delenv("VCLUSTER_HOST_CLOUD", raising=False)
    mock_run = mocker.patch("devops_bench.providers.vcluster.run")
    VclusterProvider().ensure_account_credentials()
    mock_run.assert_called_once_with(
        [
            "gcloud",
            "container",
            "clusters",
            "get-credentials",
            "host-cluster",
            "--location",
            "us-central1",
            "--project",
            "test-project",
        ],
        capture=False,
    )


def test_ensure_account_credentials_noop_for_eks_host_cloud(
    mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VCLUSTER_HOST_CLOUD", "eks")
    monkeypatch.setenv("VCLUSTER_HOST_CLUSTER", "host-cluster")
    monkeypatch.setenv("GCP_PROJECT_ID", "test-project")
    monkeypatch.setenv("VCLUSTER_HOST_LOCATION", "us-central1")
    mock_run = mocker.patch("devops_bench.providers.vcluster.run")
    VclusterProvider().ensure_account_credentials()
    mock_run.assert_not_called()
