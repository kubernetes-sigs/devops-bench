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

"""Tests for the no-op deployer."""

from __future__ import annotations

from devops_bench.core import ClusterInfo
from devops_bench.deployers.noop import NoOpDeployer


def test_up_is_noop(mocker):
    # No provisioning subprocess should ever be spawned.
    mock_run = mocker.patch("devops_bench.core.subprocess.run")
    mock_popen = mocker.patch("subprocess.Popen")
    deployer = NoOpDeployer(cluster_name="test-cluster", project_id="test-project")
    assert deployer.up() is None
    mock_run.assert_not_called()
    mock_popen.assert_not_called()


def test_down_is_noop(mocker):
    mock_run = mocker.patch("devops_bench.core.subprocess.run")
    mock_popen = mocker.patch("subprocess.Popen")
    deployer = NoOpDeployer(cluster_name="test-cluster", project_id="test-project")
    assert deployer.down() is None
    mock_run.assert_not_called()
    mock_popen.assert_not_called()


def test_get_cluster_info():
    deployer = NoOpDeployer(cluster_name="test-cluster", project_id="test-project")
    info = deployer.get_cluster_info()

    assert isinstance(info, ClusterInfo)
    assert info.name == "test-cluster"
    assert info.location == "local"
    assert info.project == "test-project"


def test_get_cluster_info_without_project():
    deployer = NoOpDeployer(cluster_name="test-cluster")
    info = deployer.get_cluster_info()

    assert isinstance(info, ClusterInfo)
    assert info.name == "test-cluster"
    assert info.location == "local"
