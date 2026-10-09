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

import logging
import os
from pathlib import Path

import pytest

from devops_bench.core import ConfigError
from devops_bench.deployers.factory import get_deployer, needs_cloud_project
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


def test_get_deployer_null_variables_key(mocker, base_config):
    # A YAML "variables:" key with no value parses to None, not a missing key.
    mocker.patch("devops_bench.deployers.tofu.Path.exists", return_value=True)
    deployer = get_deployer(
        {"deployer": "tofu", "variables": None},
        base_config["project_id"],
        base_config["cluster_name"],
        base_config["location"],
    )
    assert isinstance(deployer, TFDeployer)
    assert deployer.custom_keys == set()


def test_get_deployer_non_dict_variables_raises(base_config):
    with pytest.raises(ConfigError, match="must be a mapping"):
        get_deployer(
            {"deployer": "tofu", "variables": "not-a-mapping"},
            base_config["project_id"],
            base_config["cluster_name"],
            base_config["location"],
        )


def test_get_deployer_tofu_custom_stack_and_vars(mocker, base_config, monkeypatch):
    monkeypatch.delenv("KUBECONFIG", raising=False)
    mocker.patch("devops_bench.deployers.tofu.Path.exists", return_value=True)
    infra_config = {
        "deployer": "tofu",
        "stack": "custom/stack",
        "provider": "gcp",
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


def test_get_deployer_infra_location_env_overrides_gcp_location(mocker, base_config, monkeypatch):
    # INFRA_LOCATION outranks the vendor-specific GCP_LOCATION env var.
    mocker.patch("devops_bench.deployers.tofu.Path.exists", return_value=True)
    monkeypatch.setenv("GCP_LOCATION", "us-east1-b")
    monkeypatch.setenv("INFRA_LOCATION", "europe-west1-b")
    deployer = get_deployer(
        {"deployer": "tofu", "stack": "custom/stack", "provider": "gcp"},
        base_config["project_id"],
        base_config["cluster_name"],
        None,
    )
    assert deployer.variables["location"] == "europe-west1-b"


def test_get_deployer_non_kind_stack_requires_explicit_provider(base_config):
    # No cloud is assumed by default; only a stack named "kind" deduces one.
    with pytest.raises(ConfigError, match="requires an explicit provider"):
        get_deployer(
            {"deployer": "tofu", "stack": "custom/stack"},
            base_config["project_id"],
            base_config["cluster_name"],
            base_config["location"],
        )


def test_get_deployer_kind_like_stack_name_requires_explicit_provider(base_config):
    # Deduction matches the final path component exactly, not a substring.
    with pytest.raises(ConfigError, match="requires an explicit provider"):
        get_deployer(
            {"deployer": "tofu", "stack": "custom/kindness"},
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


def test_get_deployer_relative_escaping_stack_requires_explicit_provider(base_config):
    # A relative stack that escapes the tf/ directory must require an explicit provider.
    with pytest.raises(ConfigError) as exc_info:
        get_deployer(
            {"deployer": "tofu", "stack": "../vcluster"},
            base_config["project_id"],
            base_config["cluster_name"],
            base_config["location"],
        )
    err_msg = str(exc_info.value)
    assert "requires an explicit provider" in err_msg


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


def test_get_deployer_tofu_vcluster_stack(mocker, base_config):
    mocker.patch("devops_bench.deployers.tofu.Path.exists", return_value=True)
    mocker.patch(
        "devops_bench.providers.vcluster._get_current_context",
        return_value="kind-test",
    )
    deployer = get_deployer(
        {"deployer": "tofu", "stack": "prebuilt/vcluster"},
        base_config["project_id"],
        base_config["cluster_name"],
        base_config["location"],
    )
    assert isinstance(deployer, TFDeployer)
    assert deployer.variables["infra_provider"] == "vcluster"
    assert deployer.variables["project_id"] == base_config["project_id"]
    assert deployer.variables["cluster_name"] == base_config["cluster_name"]
    assert deployer.variables["namespace"] == f"vcluster-{base_config['cluster_name']}"
    assert deployer.tf_dir == str(_TF_ROOT / "prebuilt/vcluster")


def test_get_deployer_infra_provider_env_vcluster(mocker, base_config, monkeypatch):
    monkeypatch.setenv("INFRA_PROVIDER", "vcluster")
    mocker.patch("devops_bench.deployers.tofu.Path.exists", return_value=True)
    mocker.patch(
        "devops_bench.providers.vcluster._get_current_context",
        return_value="kind-test",
    )
    deployer = get_deployer(
        {"deployer": "tofu", "stack": "prebuilt/kind"},
        base_config["project_id"],
        base_config["cluster_name"],
        base_config["location"],
    )
    assert isinstance(deployer, TFDeployer)
    assert deployer.variables["namespace"] == f"vcluster-{base_config['cluster_name']}"


def test_get_deployer_tofu_gcp_stack_requires_explicit_provider(base_config):
    # Cloud stacks cannot be auto-deduced by path alone to prevent unexpected charges.
    with pytest.raises(ConfigError, match="requires an explicit provider"):
        get_deployer(
            {"deployer": "tofu", "stack": "prebuilt/gcp"},
            base_config["project_id"],
            base_config["cluster_name"],
            base_config["location"],
        )


def test_get_deployer_tofu_gcp_stack_explicit_provider(mocker, base_config, monkeypatch):
    monkeypatch.delenv("KUBECONFIG", raising=False)
    mocker.patch("devops_bench.deployers.tofu.Path.exists", return_value=True)
    deployer = get_deployer(
        {"deployer": "tofu", "stack": "prebuilt/gcp", "provider": "gcp"},
        base_config["project_id"],
        base_config["cluster_name"],
        base_config["location"],
    )
    assert isinstance(deployer, TFDeployer)
    assert deployer.variables == {
        "infra_provider": "gcp",
        "project_id": base_config["project_id"],
        "cluster_name": base_config["cluster_name"],
        "location": base_config["location"],
    }
    assert deployer.tf_dir == str(_TF_ROOT / "prebuilt/gcp")


def test_get_deployer_bench_tf_root_override(tmp_path, mocker, base_config, monkeypatch):
    custom_tf_root = tmp_path / "custom_tf"
    stack_dir = custom_tf_root / "prebuilt" / "kind"
    stack_dir.mkdir(parents=True)
    monkeypatch.setenv("BENCH_TF_ROOT", str(custom_tf_root))
    mocker.patch("devops_bench.deployers.tofu.Path.exists", return_value=True)

    deployer = get_deployer(
        {"deployer": "tofu", "stack": "prebuilt/kind"},
        base_config["project_id"],
        base_config["cluster_name"],
        base_config["location"],
    )
    assert isinstance(deployer, TFDeployer)
    assert deployer.variables["infra_provider"] == "kind"
    assert deployer.tf_dir == str((custom_tf_root / "prebuilt/kind").resolve())


class TestTheInfraProviderDisagreementWarning:
    """A stale ``INFRA_PROVIDER`` export still wins, but never in silence.

    The variable outlives the command that set it, so a run sent to the wrong
    provider looks like a task bug rather than a shell one. Overriding is
    still allowed -- the warning is what makes the override visible.
    """

    def test_a_disagreeing_env_override_warns_naming_both(
        self, mocker, base_config, monkeypatch, caplog
    ):
        mocker.patch("devops_bench.deployers.tofu.Path.exists", return_value=True)
        monkeypatch.setenv("INFRA_PROVIDER", "gcp")
        with caplog.at_level(logging.WARNING, logger="devops_bench.deployers.factory"):
            get_deployer(
                {"deployer": "tofu", "stack": "prebuilt/kind", "provider": "kind"},
                base_config["project_id"],
                base_config["cluster_name"],
                base_config["location"],
            )
        assert "gcp" in caplog.text
        assert "kind" in caplog.text
        assert "INFRA_PROVIDER" in caplog.text

    def test_an_agreeing_env_override_is_quiet(self, mocker, base_config, monkeypatch, caplog):
        """Exporting what the task already declares is not a mistake."""
        mocker.patch("devops_bench.deployers.tofu.Path.exists", return_value=True)
        monkeypatch.setenv("INFRA_PROVIDER", "KIND")  # case and spacing are normalized
        with caplog.at_level(logging.WARNING, logger="devops_bench.deployers.factory"):
            get_deployer(
                {"deployer": "tofu", "stack": "prebuilt/kind", "provider": "kind"},
                base_config["project_id"],
                base_config["cluster_name"],
                base_config["location"],
            )
        assert caplog.text == ""

    def test_an_env_override_of_a_task_that_declares_nothing_is_quiet(
        self, mocker, base_config, monkeypatch, caplog
    ):
        """With no ``provider:`` key there is nothing to disagree with."""
        mocker.patch("devops_bench.deployers.tofu.Path.exists", return_value=True)
        monkeypatch.setenv("INFRA_PROVIDER", "gcp")
        with caplog.at_level(logging.WARNING, logger="devops_bench.deployers.factory"):
            get_deployer(
                {"deployer": "tofu", "stack": "prebuilt/kind"},
                base_config["project_id"],
                base_config["cluster_name"],
                base_config["location"],
            )
        assert caplog.text == ""


class TestNeedsCloudProject:
    """Answers the launcher's question: does this task bill to a project?"""

    @pytest.fixture(autouse=True)
    def _no_ambient_provider(self, monkeypatch):
        monkeypatch.delenv("INFRA_PROVIDER", raising=False)

    @pytest.mark.parametrize(
        ("infra_config", "expected"),
        [
            pytest.param({"provider": "gcp"}, True, id="declared-cloud"),
            pytest.param({"provider": "kind"}, False, id="declared-kind"),
            pytest.param({"provider": "vcluster"}, False, id="declared-vcluster"),
            pytest.param({"provider": "GCP"}, True, id="declared-cloud-uppercase"),
            pytest.param({}, False, id="empty-config-defaults-to-the-kind-stack"),
            pytest.param({"stack": "prebuilt/kind"}, False, id="deduced-kind"),
            pytest.param({"stack": "prebuilt/gcp"}, False, id="unresolvable-stack"),
            pytest.param({"deployer": "noop"}, False, id="noop"),
            pytest.param({"deployer": "noop", "provider": "gcp"}, False, id="noop-beats-provider"),
        ],
    )
    def test_the_provider_decides(self, infra_config, expected):
        assert needs_cloud_project(infra_config) is expected

    def test_an_unresolvable_provider_is_not_a_project_requirement(self):
        """``prebuilt/gcp`` names no provider, and saying so is get_deployer's job.

        Answering ``True`` here would replace that accurate error with a
        demand for a project id the task may not even need.
        """
        with pytest.raises(ConfigError, match="requires an explicit provider"):
            get_deployer({"stack": "prebuilt/gcp"}, "p", "c", "us-central1-a")
        assert needs_cloud_project({"stack": "prebuilt/gcp"}) is False

    def test_an_unknown_provider_still_counts_as_a_cloud(self):
        """Not on the local allowlist means treat it as billable.

        ``get_deployer`` rejects the name afterwards; until then the safe
        answer is the one that asks for a project rather than the one that
        quietly sends a placeholder to an unknown cloud.
        """
        assert needs_cloud_project({"provider": "azure"}) is True

    def test_an_env_override_is_honoured(self, monkeypatch):
        """The survey has to resolve providers the way provisioning will."""
        monkeypatch.setenv("INFRA_PROVIDER", "gcp")
        assert needs_cloud_project({"provider": "kind"}) is True
