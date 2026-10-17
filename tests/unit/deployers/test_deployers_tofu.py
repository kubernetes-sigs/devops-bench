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
from pytest_mock import MockerFixture

from devops_bench.core import ClusterInfo, ConfigError
from devops_bench.core.errors import SubprocessError
from devops_bench.deployers.tofu import _TF_ROOT, TFDeployer
from devops_bench.providers.base import Provider, ResolveContext


class StubProvider(Provider):
    """Provider that records delegation and returns a canned ClusterInfo."""

    def __init__(self) -> None:
        self.account_calls = 0
        self.cluster_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.output_calls: list[dict[str, Any] | None] = []
        self.cleanup_calls: list[tuple[ClusterInfo, dict[str, Any] | None, bool]] = []

    def ensure_account_credentials(self) -> None:
        self.account_calls += 1

    def ensure_cluster_credentials(
        self,
        cluster_name: str,
        location: str,
        variables: dict[str, Any],
        outputs: dict[str, Any] | None = None,
    ) -> ClusterInfo:
        self.cluster_calls.append((cluster_name, location, variables))
        self.output_calls.append(outputs)
        return ClusterInfo.from_dict(
            {"name": cluster_name, "location": location, "project": variables.get("project_id")}
        )

    def cleanup(
        self,
        cluster_info: ClusterInfo,
        variables: dict[str, Any] | None = None,
        success: bool = True,
    ) -> None:
        self.cleanup_calls.append((cluster_info, variables, success))

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

    assert len(provider.cleanup_calls) == 1
    cleanup_info, cleanup_vars, success = provider.cleanup_calls[0]
    assert cleanup_info.name == "test-cluster"
    assert cleanup_info.location == "us-central1-a"
    assert cleanup_info.project == "test-project"
    assert cleanup_vars == tf_deployer.variables
    assert success is True


def test_down_missing_tf_dir_skips_destroy_but_runs_cleanup(
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
    tf_deployer: TFDeployer,
    provider: StubProvider,
) -> None:
    monkeypatch.delenv("TF_DATA_DIR", raising=False)
    mock_run = mocker.patch("devops_bench.deployers.tofu.run")
    tf_deployer.work_dir = "/nonexistent/path/for/test"

    tf_deployer.down()

    mock_run.assert_not_called()
    assert provider.account_calls == 0
    assert len(provider.cleanup_calls) == 1
    cleanup_info, cleanup_vars, success = provider.cleanup_calls[0]
    assert cleanup_info.name == "test-cluster"
    assert cleanup_vars == tf_deployer.variables
    assert success is False


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
    assert provider.output_calls == [
        {"cluster_name": "test-cluster", "cluster_location": "us-central1-a"}
    ]
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


def test_get_cluster_info_non_dict_json_raises(mocker, tf_deployer):
    proc = MagicMock()
    proc.stdout = '["not-a-dict"]'
    mocker.patch("devops_bench.deployers.tofu.run", side_effect=[MagicMock(), proc])

    with pytest.raises(ConfigError, match="Expected dict from 'tofu output -json'"):
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
    with pytest.raises(ConfigError, match="TF stack not found under"):
        TFDeployer(tf_dir="non-existent-stack-xyz", provider=provider)


def test_init_resolves_relative_stack_under_bench_tf_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: StubProvider
) -> None:
    """A relative stack name resolves under ``$BENCH_TF_ROOT`` when set."""
    root = tmp_path / "stacks"
    (root / "my-stack").mkdir(parents=True)
    monkeypatch.setenv("BENCH_TF_ROOT", str(root))
    monkeypatch.delenv("TF_DATA_DIR", raising=False)

    deployer = TFDeployer(tf_dir="my-stack", provider=provider)

    assert Path(deployer.tf_dir) == root.resolve() / "my-stack"
    # No isolation without TF_DATA_DIR: tofu runs in the shared stack dir.
    assert deployer.work_dir == deployer.tf_dir


def test_init_isolates_stack_under_bench_tf_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: StubProvider
) -> None:
    """Per-run isolation still applies to stacks under an overridden root.

    The roots are deliberately disjoint (``stacks/`` vs ``run/``): pointing
    ``BENCH_TF_ROOT`` at an ancestor of the run scratch dir would make the
    whole-tree copy fold run artifacts back into the stack tree.
    """
    root = tmp_path / "stacks"
    (root / "my-stack").mkdir(parents=True)
    run_dir = tmp_path / "run"
    monkeypatch.setenv("BENCH_TF_ROOT", str(root))
    monkeypatch.setenv("TF_DATA_DIR", str(run_dir / "tf-data"))

    deployer = TFDeployer(tf_dir="my-stack", provider=provider)

    assert Path(deployer.work_dir) == run_dir.resolve() / "tf" / "my-stack"
    assert Path(deployer.work_dir).is_dir()
    # The shared stack dir is untouched; tofu runs in the private copy.
    assert Path(deployer.tf_dir) == root.resolve() / "my-stack"


def test_isolation_refused_when_root_contains_scratch_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: StubProvider, caplog: Any
) -> None:
    """A stack root that contains the run scratch dir must not be copied.

    Copying a tree into its own descendant recurses until the OS path-length
    limit; the deployer must refuse and degrade to the shared stack dir.
    """
    root = tmp_path  # scratch dir lives INSIDE the stack root
    (root / "my-stack").mkdir()
    monkeypatch.setenv("BENCH_TF_ROOT", str(root))
    monkeypatch.setenv("TF_DATA_DIR", str(root / "runs" / "r1" / "tf-data"))

    deployer = TFDeployer(tf_dir="my-stack", provider=provider)

    assert deployer.work_dir == deployer.tf_dir  # shared dir, no copy
    assert not (root / "runs" / "r1" / "tf").exists()
    assert "contains the run scratch dir" in caplog.text


def test_init_missing_stack_under_bench_tf_root_names_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: StubProvider
) -> None:
    """The error for a missing stack names the checked root and the override."""
    root = tmp_path / "stacks"
    root.mkdir()
    monkeypatch.setenv("BENCH_TF_ROOT", str(root))

    with pytest.raises(ConfigError, match="TF stack not found under") as excinfo:
        TFDeployer(tf_dir="absent-stack", provider=provider)

    assert str(root.resolve()) in str(excinfo.value)
    assert "BENCH_TF_ROOT" in str(excinfo.value)


def test_init_blank_bench_tf_root_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, provider: StubProvider
) -> None:
    """A blank override is treated as unset (``get_env`` semantics)."""
    monkeypatch.setenv("BENCH_TF_ROOT", "   ")

    with pytest.raises(ConfigError, match="TF stack not found under") as excinfo:
        TFDeployer(tf_dir="non-existent-stack-xyz", provider=provider)

    assert str(_TF_ROOT) in str(excinfo.value)


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


class TestDownWhenTheClusterIsGone:
    """Teardown after the Kubernetes API server is already unreachable.

    This is the leak in the ticket: ``tofu destroy`` cannot delete a Helm
    release inside a cluster that no longer answers, so it stops before the
    cluster, node service account, secrets and load-balancer parts behind it.
    The deployer drops the in-cluster addresses from state and destroys again.
    """

    @staticmethod
    def _state_list(addresses: list[str]) -> MagicMock:
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = "".join(f"{a}\n" for a in addresses)
        return proc

    @staticmethod
    def _argv(call) -> list[str]:
        return list(call.args[0])

    def _runner(self, *, state: list[str], destroy_failures: int, rm_ok: bool = True):
        """Return a ``run`` side effect that fails the first N destroy calls.

        Each failure carries a distinct message so a test can tell which of the
        two destroy attempts an escaping error came from.
        """
        seen = {"destroy": 0}

        def side_effect(cmd, **kwargs):
            argv = list(cmd)
            if argv[1] == "destroy":
                seen["destroy"] += 1
                if seen["destroy"] <= destroy_failures:
                    raise SubprocessError(
                        argv,
                        returncode=1,
                        stderr=f"destroy attempt {seen['destroy']}: cluster unreachable",
                    )
                return MagicMock(returncode=0, stdout="")
            if argv[1:3] == ["state", "list"]:
                return self._state_list(state)
            if argv[1:3] == ["state", "rm"]:
                return MagicMock(returncode=0 if rm_ok else 1, stdout="")
            return MagicMock(returncode=0, stdout="")

        return side_effect

    def test_in_cluster_resources_are_dropped_and_the_destroy_retried(
        self, mocker, monkeypatch, tf_deployer, provider
    ):
        monkeypatch.delenv("TF_DATA_DIR", raising=False)
        mock_run = mocker.patch(
            "devops_bench.deployers.tofu.run",
            side_effect=self._runner(
                state=[
                    "module.cluster.module.gke.google_container_cluster.this",
                    "helm_release.workload",
                    "module.app.kubernetes_namespace.team_alpha",
                ],
                destroy_failures=1,
            ),
        )

        tf_deployer.down()

        argvs = [self._argv(c) for c in mock_run.call_args_list]
        removed = [a[-1] for a in argvs if a[1:3] == ["state", "rm"]]
        # Only the resources that need a live API server, and each one in its
        # own invocation so an already-gone address cannot abort the rest.
        assert removed == ["helm_release.workload", "module.app.kubernetes_namespace.team_alpha"]
        # The cluster itself is never forgotten — destroying it is the point.
        assert not any("google_container_cluster" in a for a in removed)
        assert sum(1 for a in argvs if a[1] == "destroy") == 2
        assert provider.cleanup_calls[0][2] is True

    def test_a_clean_destroy_never_touches_state(self, mocker, monkeypatch, tf_deployer):
        monkeypatch.delenv("TF_DATA_DIR", raising=False)
        mock_run = mocker.patch(
            "devops_bench.deployers.tofu.run",
            side_effect=self._runner(state=["helm_release.workload"], destroy_failures=0),
        )

        tf_deployer.down()

        argvs = [self._argv(c) for c in mock_run.call_args_list]
        assert not any(a[1] == "state" for a in argvs)

    def test_a_failure_with_nothing_in_cluster_is_raised_not_retried(
        self, mocker, monkeypatch, tf_deployer, provider
    ):
        # Quota, a held lock, a bad credential: dropping state fixes none of
        # these, so the error must surface instead of being retried into silence.
        monkeypatch.delenv("TF_DATA_DIR", raising=False)
        mock_run = mocker.patch(
            "devops_bench.deployers.tofu.run",
            side_effect=self._runner(
                state=["module.cluster.module.gke.google_container_cluster.this"],
                destroy_failures=1,
            ),
        )

        with pytest.raises(SubprocessError):
            tf_deployer.down()

        argvs = [self._argv(c) for c in mock_run.call_args_list]
        assert sum(1 for a in argvs if a[1] == "destroy") == 1
        assert not any(a[1:3] == ["state", "rm"] for a in argvs)
        # cleanup still runs, and is told the destroy did not succeed.
        assert provider.cleanup_calls[0][2] is False

    def test_a_second_failure_reports_the_original_error(
        self, mocker, monkeypatch, tf_deployer, provider
    ):
        monkeypatch.delenv("TF_DATA_DIR", raising=False)
        mocker.patch(
            "devops_bench.deployers.tofu.run",
            side_effect=self._runner(state=["helm_release.workload"], destroy_failures=2),
        )

        with pytest.raises(SubprocessError) as excinfo:
            tf_deployer.down()

        # The first error says why teardown started failing; the second only
        # says it failed again.
        assert "destroy attempt 1" in str(excinfo.value.stderr)
        assert provider.cleanup_calls[0][2] is False

    def test_an_unreadable_state_does_not_raise_from_the_error_path(
        self, mocker, monkeypatch, tf_deployer
    ):
        monkeypatch.delenv("TF_DATA_DIR", raising=False)

        def side_effect(cmd, **kwargs):
            argv = list(cmd)
            if argv[1] == "destroy":
                raise SubprocessError(argv, returncode=1, stderr="boom")
            if argv[1:3] == ["state", "list"]:
                return MagicMock(returncode=1, stdout="")
            return MagicMock(returncode=0, stdout="")

        mocker.patch("devops_bench.deployers.tofu.run", side_effect=side_effect)

        with pytest.raises(SubprocessError) as excinfo:
            tf_deployer.down()
        assert excinfo.value.stderr == "boom"

    def test_a_state_rm_that_fails_does_not_stop_the_others(self, mocker, monkeypatch, tf_deployer):
        monkeypatch.delenv("TF_DATA_DIR", raising=False)
        mock_run = mocker.patch(
            "devops_bench.deployers.tofu.run",
            side_effect=self._runner(
                state=["helm_release.a", "helm_release.b", "kubernetes_secret.c"],
                destroy_failures=1,
                rm_ok=False,
            ),
        )

        tf_deployer.down()

        argvs = [self._argv(c) for c in mock_run.call_args_list]
        assert sum(1 for a in argvs if a[1:3] == ["state", "rm"]) == 3

    def test_the_state_flags_are_carried_into_state_subcommands(
        self, mocker, monkeypatch, tmp_path, tf_deployer
    ):
        # The per-run state file lives beside TF_DATA_DIR, so a state list/rm
        # without -state would read the wrong (or an empty) state.
        monkeypatch.setenv("TF_DATA_DIR", str(tmp_path / "tf-data"))
        mock_run = mocker.patch(
            "devops_bench.deployers.tofu.run",
            side_effect=self._runner(state=["helm_release.workload"], destroy_failures=1),
        )

        tf_deployer.down()

        expected = str((tmp_path / "tf-data").resolve().parent / "terraform.tfstate")
        for argv in (self._argv(c) for c in mock_run.call_args_list):
            if argv[1] == "state":
                assert argv[argv.index("-state") + 1] == expected
