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

"""Unit tests for devops_bench.core.context."""

from pathlib import Path

from devops_bench.core.context import ClusterInfo, RunContext


def test_cluster_info_from_dict_full():
    info = ClusterInfo.from_dict(
        {
            "name": "c1",
            "location": "us-central1-a",
            "project": "proj",
            "kubeconfig_path": "/tmp/kubeconfig",
        }
    )
    assert info == ClusterInfo("c1", "us-central1-a", "proj", "/tmp/kubeconfig")


def test_cluster_info_from_dict_minimal():
    info = ClusterInfo.from_dict({"name": "c1"})
    assert info.name == "c1"
    assert info.location is None
    assert info.project is None


def test_kubeconfig_uses_env_when_not_provided(monkeypatch):
    monkeypatch.setenv("KUBECONFIG", "/custom/kubeconfig")
    assert ClusterInfo("c1").kubeconfig_path == "/custom/kubeconfig"
    assert ClusterInfo.from_dict({"name": "c1"}).kubeconfig_path == "/custom/kubeconfig"


def test_kubeconfig_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("KUBECONFIG", raising=False)
    expected = str(Path("~/.kube/config").expanduser())
    assert ClusterInfo("c1").kubeconfig_path == expected


def test_kubeconfig_explicit_path_wins(monkeypatch):
    monkeypatch.setenv("KUBECONFIG", "/custom/kubeconfig")
    assert ClusterInfo("c1", kubeconfig_path="/explicit").kubeconfig_path == "/explicit"


def test_cluster_info_is_frozen():
    info = ClusterInfo("c1")
    import dataclasses

    try:
        info.name = "c2"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:  # pragma: no cover - guards against a regression in frozen=True
        raise AssertionError("ClusterInfo should be immutable")


def test_run_context_defaults():
    ctx = RunContext(task_id="t1")
    assert ctx.task_id == "t1"
    assert ctx.task_name == ""
    assert ctx.cluster is None
    assert ctx.env == {}


def test_run_context_coerces_workspace_to_path():
    ctx = RunContext(task_id="t1", workspace_path="/work/space")
    assert isinstance(ctx.workspace_path, Path)
    assert ctx.workspace_path == Path("/work/space")


def test_run_context_kubeconfig_accessor():
    ctx = RunContext(task_id="t1", cluster=ClusterInfo("c1", kubeconfig_path="/tmp/kc"))
    assert ctx.kubeconfig_path == "/tmp/kc"


def test_run_context_kubeconfig_accessor_without_cluster():
    assert RunContext(task_id="t1").kubeconfig_path is None


def test_run_context_env_is_independent_per_instance():
    a = RunContext(task_id="a")
    b = RunContext(task_id="b")
    a.env["KEY"] = "value"
    assert b.env == {}
