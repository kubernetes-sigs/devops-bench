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

"""Tests for the OpenTofu deployer engine.

The engine is provider-agnostic: it runs ``tofu`` and delegates credentials and
project resolution to its provider. These tests use a recording stub provider;
credential behavior is covered in ``tests/unit/providers``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from devops_bench.core import ClusterInfo, ConfigError
from devops_bench.deployers.tofu import _TF_ROOT, TFDeployer
from devops_bench.providers.base import Provider, ResolveContext


class StubProvider(Provider):
    """Provider that records delegation and returns a canned ClusterInfo."""

    def __init__(self) -> None:
        self.account_calls = 0
        self.cluster_calls: list[tuple[str, str, dict[str, Any]]] = []

    def ensure_account_credentials(self) -> None:
        self.account_calls += 1

    def ensure_cluster_credentials(
        self, cluster_name: str, location: str, variables: dict[str, Any]
    ) -> ClusterInfo:
        self.cluster_calls.append((cluster_name, location, variables))
        return ClusterInfo.from_dict(
            {"name": cluster_name, "location": location, "project": variables.get("project_id")}
        )

    def resolve_variables(
        self, ctx: ResolveContext, custom_variables: dict[str, Any]
    ) -> dict[str, Any]:
        return dict(custom_variables)


@pytest.fixture
def stack_dir(tmp_path):
    path = tmp_path / "prebuilt" / "minimum"
    path.mkdir(parents=True)
    (path / "variables.tf").write_text("""
variable "project_id" {}
variable "cluster_name" {}
variable "location" {}
variable "node_count" {}
""")
    return path


@pytest.fixture
def provider():
    return StubProvider()


@pytest.fixture
def tf_deployer(stack_dir, provider):
    variables = {
        "project_id": "test-project",
        "cluster_name": "test-cluster",
        "location": "us-central1-a",
        "node_count": 3,
    }
    return TFDeployer(tf_dir=str(stack_dir), provider=provider, variables=variables)


def test_up(mocker, monkeypatch, tf_deployer, provider):
    monkeypatch.delenv("TF_DATA_DIR", raising=False)
    mock_run = mocker.patch("devops_bench.deployers.tofu.run")
    tf_deployer.up()

    assert provider.account_calls == 1
    calls = mock_run.call_args_list
    assert len(calls) == 2
    assert calls[0].args[0] == ["tofu", "init", "-input=false"]
    assert calls[0].kwargs["cwd"] == tf_deployer.tf_dir
    assert calls[1].args[0] == [
        "tofu",
        "apply",
        "-auto-approve",
        "-input=false",
        "-var",
        "project_id=test-project",
        "-var",
        "cluster_name=test-cluster",
        "-var",
        "location=us-central1-a",
        "-var",
        "node_count=3",
    ]
    assert calls[1].kwargs["cwd"] == tf_deployer.tf_dir


def test_down(mocker, monkeypatch, tf_deployer, provider):
    monkeypatch.delenv("TF_DATA_DIR", raising=False)
    mock_run = mocker.patch("devops_bench.deployers.tofu.run")
    tf_deployer.down()

    assert provider.account_calls == 1
    calls = mock_run.call_args_list
    assert len(calls) == 2
    assert calls[0].args[0] == ["tofu", "init", "-input=false"]
    assert calls[1].args[0] == [
        "tofu",
        "destroy",
        "-auto-approve",
        "-input=false",
        "-var",
        "project_id=test-project",
        "-var",
        "cluster_name=test-cluster",
        "-var",
        "location=us-central1-a",
        "-var",
        "node_count=3",
    ]


def test_up_isolates_state_beside_tf_data_dir(mocker, monkeypatch, tmp_path, tf_deployer):
    monkeypatch.setenv("TF_DATA_DIR", str(tmp_path / "tf-data"))
    mock_run = mocker.patch("devops_bench.deployers.tofu.run")
    tf_deployer.up()

    apply_argv = mock_run.call_args_list[1].args[0]
    expected_state = str((tmp_path / "tf-data").resolve().parent / "terraform.tfstate")
    assert "-state" in apply_argv
    state_path = apply_argv[apply_argv.index("-state") + 1]
    assert state_path == expected_state
    # Must NOT be inside TF_DATA_DIR (that path is reserved by OpenTofu).
    assert f"{os.sep}tf-data{os.sep}" not in state_path
    # init carries no -state (it does not touch state).
    assert "-state" not in mock_run.call_args_list[0].args[0]


def test_down_isolates_state_beside_tf_data_dir(mocker, monkeypatch, tmp_path, tf_deployer):
    monkeypatch.setenv("TF_DATA_DIR", str(tmp_path / "tf-data"))
    mock_run = mocker.patch("devops_bench.deployers.tofu.run")
    tf_deployer.down()

    destroy_argv = mock_run.call_args_list[1].args[0]
    expected_state = str((tmp_path / "tf-data").resolve().parent / "terraform.tfstate")
    assert destroy_argv[destroy_argv.index("-state") + 1] == expected_state


def _output_process(location):
    proc = MagicMock()
    proc.stdout = json.dumps(
        {
            "cluster_name": {"value": "test-cluster"},
            "cluster_location": {"value": location},
        }
    )
    return proc


def test_get_cluster_info_parses_and_delegates(mocker, monkeypatch, tf_deployer, provider):
    monkeypatch.delenv("TF_DATA_DIR", raising=False)
    mock_run = mocker.patch("devops_bench.deployers.tofu.run")
    mock_run.side_effect = [MagicMock(), _output_process("us-central1-a")]

    info = tf_deployer.get_cluster_info()

    # Engine runs only init + output; no credential side effects of its own.
    calls = mock_run.call_args_list
    assert len(calls) == 2
    assert calls[0].args[0] == ["tofu", "init", "-input=false"]
    assert calls[1].args[0] == ["tofu", "output", "-json"]
    for call in calls:
        assert "gcloud" not in call.args[0]

    # Parsed outputs are handed to the provider, which builds the ClusterInfo.
    assert provider.cluster_calls == [("test-cluster", "us-central1-a", tf_deployer.variables)]
    assert info.name == "test-cluster"
    assert info.location == "us-central1-a"
    assert info.project == "test-project"


def test_get_cluster_info_reads_isolated_state(mocker, monkeypatch, tmp_path, tf_deployer):
    monkeypatch.setenv("TF_DATA_DIR", str(tmp_path / "tf-data"))
    mock_run = mocker.patch("devops_bench.deployers.tofu.run")
    mock_run.side_effect = [MagicMock(), _output_process("us-central1-a")]

    tf_deployer.get_cluster_info()

    output_argv = mock_run.call_args_list[1].args[0]
    expected_state = str((tmp_path / "tf-data").resolve().parent / "terraform.tfstate")
    assert output_argv[:3] == ["tofu", "output", "-json"]
    assert output_argv[output_argv.index("-state") + 1] == expected_state


def test_get_cluster_info_missing_name_raises(mocker, tf_deployer):
    proc = MagicMock()
    proc.stdout = json.dumps({"cluster_location": {"value": "us-central1-a"}})
    mocker.patch("devops_bench.deployers.tofu.run", side_effect=[MagicMock(), proc])

    with pytest.raises(ConfigError, match="cluster_name"):
        tf_deployer.get_cluster_info()


def test_get_cluster_info_bad_json_raises(mocker, tf_deployer):
    proc = MagicMock()
    proc.stdout = "not-json"
    mocker.patch("devops_bench.deployers.tofu.run", side_effect=[MagicMock(), proc])

    with pytest.raises(ConfigError, match="tofu output"):
        tf_deployer.get_cluster_info()


def test_init_path_resolution(tmp_path, mocker, provider):
    # Absolute path that exists on disk is used as-is.
    abs_path = tmp_path / "my-tf-stack"
    abs_path.mkdir()
    deployer = TFDeployer(tf_dir=str(abs_path), provider=provider)
    assert deployer.tf_dir == str(abs_path)

    # Relative path resolved under <repo_root>/tf.
    mocker.patch("devops_bench.deployers.tofu.Path.exists", return_value=True)
    deployer = TFDeployer(tf_dir="my-repo-stack", provider=provider)
    assert deployer.tf_dir == str(_TF_ROOT / "my-repo-stack")
    assert Path(deployer.tf_dir) == _TF_ROOT / "my-repo-stack"


def test_init_expands_user_path(tmp_path, monkeypatch, provider):
    # A ``~`` path expands to an absolute path and is used as-is (out-of-repo).
    monkeypatch.setenv("HOME", str(tmp_path))
    stack = tmp_path / "ext-stack"
    stack.mkdir()
    deployer = TFDeployer(tf_dir="~/ext-stack", provider=provider)
    assert deployer.tf_dir == str(stack)


def test_init_missing_dir_raises(provider):
    with pytest.raises(ConfigError, match="TF stack not found in repo"):
        TFDeployer(tf_dir="non-existent-stack-xyz", provider=provider)


def test_get_declared_variables_robustness(tmp_path):
    tf_file = tmp_path / "variables.tf"
    tf_file.write_text("""
variable "var1" {}
  variable "var2" {
    type = string
  }
variable "var3" { } # trailing comment
# variable "commented_var" {}
// variable "commented_var2" {}
/* variable "commented_var3" {} */
""")
    # Variables declared in .tf.json files are also discovered.
    (tmp_path / "extra.tf.json").write_text('{"variable": {"json_var": {"type": "string"}}}')
    # Malformed .tf.json is skipped, not fatal.
    (tmp_path / "broken.tf.json").write_text("{ not json")

    from devops_bench.deployers.tofu import _get_declared_variables

    declared = _get_declared_variables(str(tmp_path))
    assert "var1" in declared
    assert "var2" in declared
    assert "var3" in declared
    assert "json_var" in declared
    assert "commented_var" not in declared
    assert "commented_var2" not in declared


def test_var_flags_drops_and_logs_undeclared_variables(stack_dir, provider, caplog):
    import logging

    variables = {
        "project_id": "test-project",
        "cluster_name": "test-cluster",
        "location": "us-central1-a",
        "node_count": 3,
        "undeclared_var": "should-be-dropped",
    }
    deployer = TFDeployer(tf_dir=str(stack_dir), provider=provider, variables=variables)

    with caplog.at_level(logging.WARNING):
        flags = deployer._var_flags()

    assert "undeclared_var" not in "".join(flags)
    assert any(
        "dropping variable 'undeclared_var'" in record.message
        and "not declared in tf files" in record.message
        for record in caplog.records
    )
    assert "project_id=test-project" in flags


def test_var_flags_raises_on_undeclared_custom_variables(stack_dir, provider):
    variables = {
        "project_id": "test-project",
        "cluster_name": "test-cluster",
        "location": "us-central1-a",
        "node_count": 3,
        "undeclared_custom_var": "should-raise",
    }
    # Pass undeclared_custom_var as a custom key (simulating task config variables)
    custom_keys = {"undeclared_custom_var"}
    deployer = TFDeployer(
        tf_dir=str(stack_dir),
        provider=provider,
        variables=variables,
        custom_keys=custom_keys,
    )

    with pytest.raises(
        ConfigError, match="Variable 'undeclared_custom_var' defined in task config is not declared"
    ):
        deployer._var_flags()
