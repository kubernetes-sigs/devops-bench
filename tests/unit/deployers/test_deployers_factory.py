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

"""Tests for the deployer factory."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from devops_bench.core import ConfigError
from devops_bench.deployers.factory import get_deployer
from devops_bench.deployers.noop import NoOpDeployer
from devops_bench.deployers.tofu import _TF_ROOT, TFDeployer


@pytest.fixture
def base_config():
    return {
        "project_id": "test-project",
        "cluster_name": "test-cluster",
        "location": "us-central1-a",
    }


def _expected_kubeconfig():
    return os.environ.get("KUBECONFIG") or str(Path("~/.kube/config").expanduser().resolve())


def test_get_deployer_default(mocker, base_config):
    mocker.patch("devops_bench.deployers.tofu.Path.exists", return_value=True)
    deployer = get_deployer(
        {},
        base_config["project_id"],
        base_config["cluster_name"],
        base_config["location"],
    )
    assert isinstance(deployer, TFDeployer)
    assert deployer.tf_dir == str(_TF_ROOT / "prebuilt/kind")


def test_get_deployer_unsupported(base_config):
    with pytest.raises(ConfigError):
        get_deployer(
            {"deployer": "kubetest2"},
            base_config["project_id"],
            base_config["cluster_name"],
            base_config["location"],
        )


def test_get_deployer_tofu_default_stack(mocker, base_config):
    mocker.patch("devops_bench.deployers.tofu.Path.exists", return_value=True)
    deployer = get_deployer(
        {"deployer": "tofu"},
        base_config["project_id"],
        base_config["cluster_name"],
        base_config["location"],
    )
    assert isinstance(deployer, TFDeployer)
    assert deployer.variables == {
        "infra_provider": "kind",
        "project_id": base_config["project_id"],
        "cluster_name": base_config["cluster_name"],
        "location": "local",
        "kubeconfig_path": _expected_kubeconfig(),
    }
    assert deployer.tf_dir == str(_TF_ROOT / "prebuilt/kind")


def test_get_deployer_tofu_custom_stack_and_vars(mocker, base_config, monkeypatch):
    monkeypatch.delenv("KUBECONFIG", raising=False)
    mocker.patch("devops_bench.deployers.tofu.Path.exists", return_value=True)
    infra_config = {
        "deployer": "tofu",
        "stack": "custom/stack",
        "variables": {
            "node_count": 5,
            "machine_type": "n2-standard-4",
            "cluster_name": "custom-cluster",  # overrides global
        },
    }
    deployer = get_deployer(
        infra_config,
        base_config["project_id"],
        base_config["cluster_name"],
        base_config["location"],
    )
    assert isinstance(deployer, TFDeployer)
    assert deployer.variables == {
        "infra_provider": "gcp",
        "project_id": base_config["project_id"],
        "cluster_name": "custom-cluster",
        "location": base_config["location"],
        "node_count": 5,
        "machine_type": "n2-standard-4",
    }
    assert deployer.tf_dir == str(_TF_ROOT / "custom/stack")


def test_get_deployer_tofu_kind_stack(mocker, base_config):
    mocker.patch("devops_bench.deployers.tofu.Path.exists", return_value=True)
    deployer = get_deployer(
        {"deployer": "tofu", "stack": "prebuilt/kind"},
        base_config["project_id"],
        base_config["cluster_name"],
        base_config["location"],
    )
    assert isinstance(deployer, TFDeployer)
    assert deployer.variables == {
        "infra_provider": "kind",
        "project_id": base_config["project_id"],
        "cluster_name": base_config["cluster_name"],
        "location": "local",
        "kubeconfig_path": _expected_kubeconfig(),
    }
    assert deployer.tf_dir == str(_TF_ROOT / "prebuilt/kind")


def test_get_deployer_noop(base_config):
    deployer = get_deployer(
        {"deployer": "noop"},
        base_config["project_id"],
        base_config["cluster_name"],
        base_config["location"],
    )
    assert isinstance(deployer, NoOpDeployer)
    assert deployer.cluster_name == base_config["cluster_name"]
    assert deployer.project_id == base_config["project_id"]


def test_get_deployer_no_infra_env_precedence(mocker, base_config):
    # BENCH_NO_INFRA wins even when a tofu deployer/stack is configured.
    mocker.patch.dict(os.environ, {"BENCH_NO_INFRA": "true"})
    deployer = get_deployer(
        {"deployer": "tofu", "stack": "prebuilt/kind"},
        base_config["project_id"],
        base_config["cluster_name"],
        base_config["location"],
    )
    assert isinstance(deployer, NoOpDeployer)
    assert deployer.cluster_name == base_config["cluster_name"]
    assert deployer.project_id == base_config["project_id"]


def test_get_deployer_explicit_provider_overrides_deduction(mocker, base_config, monkeypatch):
    monkeypatch.delenv("KUBECONFIG", raising=False)
    # A 'kind'-looking stack name is forced to the gcp provider via config.
    mocker.patch("devops_bench.deployers.tofu.Path.exists", return_value=True)
    deployer = get_deployer(
        {"deployer": "tofu", "stack": "prebuilt/kind", "provider": "gcp"},
        base_config["project_id"],
        base_config["cluster_name"],
        base_config["location"],
    )
    assert isinstance(deployer, TFDeployer)
    # GCP variables (project_id/location), not the kind kubeconfig defaults.
    assert deployer.variables["project_id"] == base_config["project_id"]
    assert deployer.variables["location"] == base_config["location"]
    assert "kubeconfig_path" not in deployer.variables


def test_get_deployer_infra_provider_env_overrides_config(mocker, base_config, monkeypatch):
    # INFRA_PROVIDER outranks a pinned 'provider:' key so runs stay overridable.
    monkeypatch.delenv("KUBECONFIG", raising=False)
    mocker.patch("devops_bench.deployers.tofu.Path.exists", return_value=True)
    mocker.patch.dict(os.environ, {"INFRA_PROVIDER": "gcp"})
    deployer = get_deployer(
        {"deployer": "tofu", "stack": "prebuilt/kind", "provider": "kind"},
        base_config["project_id"],
        base_config["cluster_name"],
        base_config["location"],
    )
    assert isinstance(deployer, TFDeployer)
    # GCP variables win over the pinned kind provider.
    assert deployer.variables["project_id"] == base_config["project_id"]
    assert deployer.variables["location"] == base_config["location"]
    assert "kubeconfig_path" not in deployer.variables


def test_get_deployer_infra_provider_env(mocker, base_config):
    mocker.patch("devops_bench.deployers.tofu.Path.exists", return_value=True)
    mocker.patch.dict(os.environ, {"INFRA_PROVIDER": "gcp"})
    deployer = get_deployer(
        {"deployer": "tofu", "stack": "prebuilt/kind"},
        base_config["project_id"],
        base_config["cluster_name"],
        base_config["location"],
    )
    assert isinstance(deployer, TFDeployer)
    assert deployer.variables["project_id"] == base_config["project_id"]


def test_get_deployer_cloud_provider_raises_rename_error(mocker, base_config):
    mocker.patch("devops_bench.deployers.tofu.Path.exists", return_value=True)
    mocker.patch.dict(os.environ, {"CLOUD_PROVIDER": "gcp"})
    with pytest.raises(ConfigError, match="has been renamed to INFRA_PROVIDER"):
        get_deployer(
            {"deployer": "tofu", "stack": "prebuilt/kind"},
            base_config["project_id"],
            base_config["cluster_name"],
            base_config["location"],
        )


def test_get_deployer_unknown_provider_raises(mocker, base_config):
    mocker.patch("devops_bench.deployers.tofu.Path.exists", return_value=True)
    with pytest.raises(ConfigError, match="unknown provider"):
        get_deployer(
            {"deployer": "tofu", "stack": "custom/stack", "provider": "aws"},
            base_config["project_id"],
            base_config["cluster_name"],
            base_config["location"],
        )


def test_get_deployer_absolute_stack_requires_explicit_provider(tmp_path, base_config):
    # An out-of-repo stack must not have its provider guessed from the path.
    abs_stack = tmp_path / "ext" / "stack"
    abs_stack.mkdir(parents=True)
    with pytest.raises(ConfigError) as exc_info:
        get_deployer(
            {"deployer": "tofu", "stack": str(abs_stack)},
            base_config["project_id"],
            base_config["cluster_name"],
            base_config["location"],
        )
    err_msg = str(exc_info.value)
    assert "requires an explicit provider" in err_msg
    assert "INFRA_PROVIDER" in err_msg
    assert "CLOUD_PROVIDER" not in err_msg


def test_get_deployer_absolute_stack_with_provider(tmp_path, base_config):
    abs_stack = tmp_path / "ext" / "stack"
    abs_stack.mkdir(parents=True)
    deployer = get_deployer(
        {"deployer": "tofu", "stack": str(abs_stack), "provider": "gcp"},
        base_config["project_id"],
        base_config["cluster_name"],
        base_config["location"],
    )
    assert isinstance(deployer, TFDeployer)
    assert deployer.tf_dir == str(abs_stack)
