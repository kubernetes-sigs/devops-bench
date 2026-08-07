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

"""Unit tests for the standalone vCluster provider."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from devops_bench.core import ClusterInfo, ConfigError
from devops_bench.providers import ResolveContext
from devops_bench.providers.vcluster import VClusterProvider


@pytest.fixture
def ctx() -> ResolveContext:
    return ResolveContext(
        stack="prebuilt/vcluster",
        project_id="test-project",
        cluster_name="test-cluster",
        location="us-central1-a",
    )


@pytest.fixture
def fake_kubeconfig(tmp_path: Path) -> str:
    path = tmp_path / "kubeconfig.yaml"
    path.write_text(
        """apiVersion: v1
current-context: kind-local
contexts:
- context:
    cluster: kind-cluster
  name: kind-local
- context:
    cluster: remote-cluster
  name: gke_remote_ctx
- context:
    cluster: private-cluster
  name: my-private-ctx
clusters:
- cluster:
    server: https://127.0.0.1:6443
  name: kind-cluster
- cluster:
    server: https://35.192.1.100
  name: remote-cluster
- cluster:
    server: https://10.240.0.5:6443
  name: private-cluster
""",
        encoding="utf-8",
    )
    return str(path)


def test_vcluster_ensure_account_credentials_noop() -> None:
    VClusterProvider().ensure_account_credentials()


def test_vcluster_resolve_variables_defaults(
    ctx: ResolveContext,
    fake_kubeconfig: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOST_KUBECONFIG", fake_kubeconfig)
    variables = VClusterProvider().resolve_variables(ctx, {})

    assert variables["infra_provider"] == "vcluster"
    assert variables["project_id"] == "test-project"
    assert variables["cluster_name"] == "test-cluster"
    assert variables["location"] == "us-central1-a"
    assert variables["namespace"] == "vcluster-test-cluster"
    assert variables["host_kubeconfig_path"] == str(Path(fake_kubeconfig).resolve())
    assert variables["host_kubecontext"] == "kind-local"
    assert variables["service_type"] == "NodePort"
    assert "vcluster-test-cluster-kubeconfig.yaml" in variables["kubeconfig_path"]


def test_vcluster_resolve_variables_remote_context_raises(
    ctx: ResolveContext,
    fake_kubeconfig: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOST_KUBECONFIG", fake_kubeconfig)
    monkeypatch.setenv("HOST_KUBECONTEXT", "gke_remote_ctx")
    monkeypatch.delenv("ALLOW_REMOTE_HOST_KUBECONTEXT", raising=False)

    with pytest.raises(ConfigError, match="classified as remote"):
        VClusterProvider().resolve_variables(ctx, {})


def test_vcluster_resolve_variables_remote_context_allowlisted_with_env(
    ctx: ResolveContext,
    fake_kubeconfig: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOST_KUBECONFIG", fake_kubeconfig)
    monkeypatch.setenv("HOST_KUBECONTEXT", "gke_remote_ctx")
    monkeypatch.setenv("ALLOW_REMOTE_HOST_KUBECONTEXT", "true")

    variables = VClusterProvider().resolve_variables(ctx, {})
    assert variables["service_type"] == "LoadBalancer"


def test_vcluster_resolve_variables_private_ip_context(
    ctx: ResolveContext,
    fake_kubeconfig: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOST_KUBECONFIG", fake_kubeconfig)
    monkeypatch.setenv("HOST_KUBECONTEXT", "my-private-ctx")

    variables = VClusterProvider().resolve_variables(ctx, {})
    assert variables["service_type"] == "NodePort"


def test_vcluster_resolve_variables_parallel_kubeconfig(
    ctx: ResolveContext,
    fake_kubeconfig: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOST_KUBECONFIG", fake_kubeconfig)
    monkeypatch.setenv("BENCH_PARALLEL", "true")
    run_kubeconfig = str(tmp_path / "run-kubeconfig")
    monkeypatch.setenv("KUBECONFIG", run_kubeconfig)

    variables = VClusterProvider().resolve_variables(ctx, {})
    assert variables["kubeconfig_path"] == run_kubeconfig


def test_vcluster_ensure_cluster_credentials(tmp_path: Path) -> None:
    target_path = tmp_path / "vc-config.yaml"
    variables = {"kubeconfig_path": str(target_path)}
    outputs = {
        "kubeconfig": (
            "apiVersion: v1\n"
            "clusters:\n"
            "- cluster:\n"
            "    server: https://127.0.0.1:8443\n"
            "  name: vc\n"
        )
    }

    info = VClusterProvider().ensure_cluster_credentials(
        "test-cluster", "local", variables, outputs=outputs
    )
    assert info.name == "test-cluster"
    assert info.location == "local"
    assert info.project == "local-vcluster"
    assert info.kubeconfig_path == str(target_path.resolve())

    assert target_path.exists()
    assert "https://127.0.0.1:8443" in target_path.read_text(encoding="utf-8")
    assert target_path.stat().st_mode & 0o777 == 0o600


def test_vcluster_ensure_cluster_credentials_rewrite_127_0_0_1(
    tmp_path: Path,
) -> None:
    target_path = tmp_path / "vc-config-port.yaml"
    variables = {"kubeconfig_path": str(target_path), "node_port": 31234}
    outputs = {
        "kubeconfig": (
            "apiVersion: v1\n"
            "clusters:\n"
            "- cluster:\n"
            "    server: https://127.0.0.1:8443\n"
            "  name: vc\n"
        )
    }

    VClusterProvider().ensure_cluster_credentials(
        "test-cluster", "local", variables, outputs=outputs
    )
    content = target_path.read_text(encoding="utf-8")
    assert "https://127.0.0.1:31234" in content


def test_vcluster_ensure_cluster_credentials_missing_outputs_raises() -> None:
    with pytest.raises(ConfigError, match="must be a non-empty string"):
        VClusterProvider().ensure_cluster_credentials("c", "local", {}, outputs=None)


def test_vcluster_ensure_cluster_credentials_refuses_default_kubeconfig(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ConfigError, match="Refusing to overwrite ~/.kube/config"):
        VClusterProvider().ensure_cluster_credentials(
            "c",
            "local",
            {"kubeconfig_path": "~/.kube/config"},
            outputs={"kubeconfig": "foo"},
        )


def test_vcluster_cleanup_deletes_orphaned_pvs(mocker: MockerFixture, tmp_path: Path) -> None:
    mock_run = mocker.patch("devops_bench.providers.vcluster.run")
    mock_run.side_effect = [
        mocker.MagicMock(returncode=0, stdout="pv-1 pv-2\n"),
        mocker.MagicMock(returncode=0, stdout=""),
    ]

    info = ClusterInfo(
        name="test-cluster",
        location="local",
        project="local-vcluster",
        kubeconfig_path=str(tmp_path / "vc.yaml"),
    )
    variables = {
        "host_kubeconfig_path": "/fake/host-config",
        "host_kubecontext": "kind-host",
    }
    VClusterProvider().cleanup(info, variables=variables)

    assert mock_run.call_count == 2
    get_cmd = mock_run.call_args_list[0].args[0]
    assert get_cmd[0] == "kubectl"
    assert "--kubeconfig=/fake/host-config" in get_cmd
    assert "--context=kind-host" in get_cmd
    assert "devops-bench/run-scoped=true,devops-bench/cluster-name=test-cluster" in get_cmd

    del_cmd = mock_run.call_args_list[1].args[0]
    assert del_cmd[0] == "kubectl"
    assert "delete" in del_cmd
    assert "pv-1" in del_cmd
    assert "pv-2" in del_cmd


def test_vcluster_cleanup_deletes_scratch_kubeconfig(
    mocker: MockerFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mocker.patch("devops_bench.providers.vcluster.run")
    monkeypatch.setenv("BENCH_RUN_STATE_ROOT", str(tmp_path))
    scratch_file = tmp_path / "vcluster-clean-vc-config.yaml"
    scratch_file.write_text("test-kubeconfig", encoding="utf-8")

    info = ClusterInfo(
        name="test-cluster",
        location="local",
        project="local-vcluster",
        kubeconfig_path=str(scratch_file),
    )
    VClusterProvider().cleanup(
        info,
        variables={
            "host_kubeconfig_path": "/fake/host",
            "host_kubecontext": "kind-host",
        },
    )

    assert not scratch_file.exists()


def test_vcluster_cleanup_preserves_non_scratch_kubeconfig(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    mocker.patch("devops_bench.providers.vcluster.run")
    custom_file = tmp_path / "user-custom-vc.yaml"
    custom_file.write_text("test-kubeconfig", encoding="utf-8")

    info = ClusterInfo(
        name="test-cluster",
        location="local",
        project="local-vcluster",
        kubeconfig_path=str(custom_file),
    )
    VClusterProvider().cleanup(
        info,
        variables={
            "host_kubeconfig_path": "/fake/host",
            "host_kubecontext": "kind-host",
        },
    )

    assert custom_file.exists()


def test_vcluster_ensure_cluster_credentials_refuses_symlink_path(
    tmp_path: Path,
) -> None:
    target = tmp_path / "real_file.yaml"
    target.write_text("hello", encoding="utf-8")
    symlink_path = tmp_path / "symlink.yaml"
    symlink_path.symlink_to(target)

    with pytest.raises(ConfigError, match="Refusing to write kubeconfig to symlink"):
        VClusterProvider().ensure_cluster_credentials(
            "c",
            "local",
            {"kubeconfig_path": str(symlink_path)},
            outputs={"kubeconfig": "foo"},
        )


def test_vcluster_ensure_cluster_credentials_refuses_symlink_atomic(
    mocker: MockerFixture,
    tmp_path: Path,
) -> None:
    fake_resolved = tmp_path / "resolved_path.yaml"
    target = tmp_path / "secret.yaml"
    target.write_text("secrets", encoding="utf-8")
    fake_resolved.symlink_to(target)

    # Mock is_symlink to return False to simulate TOCTOU race (bypassing early checks)
    mocker.patch("pathlib.Path.is_symlink", return_value=False)

    # Mock Path.resolve to return fake_resolved for our target path to simulate post-resolution swap
    original_resolve = Path.resolve

    def side_effect(self, *args, **kwargs):
        if "input_path.yaml" in self.name or "resolved_path.yaml" in self.name:
            return fake_resolved
        return original_resolve(self, *args, **kwargs)

    mocker.patch.object(Path, "resolve", side_effect)

    with pytest.raises(
        ConfigError, match="Refusing to write kubeconfig to symlink or invalid path"
    ):
        VClusterProvider().ensure_cluster_credentials(
            "c",
            "local",
            {"kubeconfig_path": str(tmp_path / "input_path.yaml")},
            outputs={"kubeconfig": "foo"},
        )


def test_vcluster_cleanup_skips_pv_when_cluster_name_empty(
    mocker: MockerFixture,
) -> None:
    mock_run = mocker.patch("devops_bench.providers.vcluster.run")
    info = ClusterInfo(
        name="",
        location="local",
        project="local-vcluster",
        kubeconfig_path="",
    )
    VClusterProvider().cleanup(info, variables={})
    mock_run.assert_not_called()


def test_is_safe_scratch_path_branches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. ~/.kube/config is never safe
    default_kube = Path("~/.kube/config").expanduser().resolve()
    assert not VClusterProvider._is_safe_scratch_path(default_kube)

    # 2. Project working directory is never safe
    assert not VClusterProvider._is_safe_scratch_path(Path.cwd())

    # 3. Directories are never safe
    assert not VClusterProvider._is_safe_scratch_path(tmp_path)

    # 4. Standard temp dir file is safe
    temp_file = Path(tempfile.gettempdir()) / "vcluster-test-file.yaml"
    assert VClusterProvider._is_safe_scratch_path(temp_file)

    # 5. BENCH_RUN_STATE_ROOT child is safe
    state_root = tmp_path / "runs"
    state_root.mkdir()
    monkeypatch.setenv("BENCH_RUN_STATE_ROOT", str(state_root))
    state_file = state_root / "run-1" / "kubeconfig"
    assert VClusterProvider._is_safe_scratch_path(state_file)

    # 6. TF_DATA_DIR parent sibling is safe if matching safe naming prefix
    tf_data_dir = tmp_path / "run-2" / "tf-data"
    tf_data_dir.mkdir(parents=True)
    monkeypatch.setenv("TF_DATA_DIR", str(tf_data_dir))
    scratch_file = tmp_path / "run-2" / "vcluster-config.yaml"
    assert VClusterProvider._is_safe_scratch_path(scratch_file)

    # 7. Unrelated external file is not safe
    custom_ext_file = tmp_path / "custom" / "my_config.yaml"
    assert not VClusterProvider._is_safe_scratch_path(custom_ext_file)
