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

"""Tests for the deployer factory.

The concrete deployers (gcp, tf, kind) are migrated in later steps, so these
tests inject lightweight test doubles into ``sys.modules`` for the modules the
factory imports lazily. This keeps the test focused on the factory's own
responsibilities -- deployer selection, location precedence, stack defaulting,
and provider deduction -- rather than on concrete deployer behaviour.
"""

import sys
import types

import pytest

from devops_bench.deployers.base import Deployer
from devops_bench.deployers.factory import get_deployer


class DummyTFDeployer(Deployer):
    """Records the arguments the factory passes to the TF deployer."""

    def __init__(self, tf_dir, variables=None):
        self.tf_dir = tf_dir
        self.variables = variables

    def up(self) -> None:  # pragma: no cover - not exercised
        pass

    def down(self) -> None:  # pragma: no cover - not exercised
        pass

    def get_cluster_info(self):  # pragma: no cover - not exercised
        return {}


class DummyGCPDeployer(Deployer):
    """Records the arguments the factory passes to the GCP deployer."""

    def __init__(self, project=None, location=None, cluster_name=None, zone=None, **config):
        self.project = project
        self.location = location
        self.cluster_name = cluster_name
        self.zone = zone or location
        self.config = config

    def up(self) -> None:  # pragma: no cover - not exercised
        pass

    def down(self) -> None:  # pragma: no cover - not exercised
        pass

    def get_cluster_info(self):  # pragma: no cover - not exercised
        return {}


def _make_resolver(tag):
    """Builds a resolver that tags its output so we can assert it was used."""

    def resolve_variables(stack, custom_variables, project_id, cluster_name, location):
        return {
            "_resolver": tag,
            "stack": stack,
            "custom": custom_variables,
            "project_id": project_id,
            "cluster_name": cluster_name,
            "location": location,
        }

    return resolve_variables


@pytest.fixture(autouse=True)
def fake_concrete_deployers(monkeypatch):
    """Inject test doubles for the concrete deployer modules the factory imports."""

    def install(module_path, **attrs):
        module = types.ModuleType(module_path)
        for name, value in attrs.items():
            setattr(module, name, value)
        monkeypatch.setitem(sys.modules, module_path, module)

    install("devops_bench.deployers.gcp.gcp_deployer", GCPDeployer=DummyGCPDeployer)
    install("devops_bench.deployers.tf.tf_deployer", TFDeployer=DummyTFDeployer)
    install("devops_bench.deployers.gcp.variables", resolve_variables=_make_resolver("gcp"))
    install("devops_bench.deployers.kind.variables", resolve_variables=_make_resolver("kind"))

    # Ensure deployer-selection env vars don't leak in from the host.
    monkeypatch.delenv("CLOUD_PROVIDER", raising=False)
    monkeypatch.delenv("GCP_LOCATION", raising=False)


def test_default_is_tofu_kind_stack():
    deployer = get_deployer({}, "test-project", "test-cluster", "us-central1-a")

    assert isinstance(deployer, DummyTFDeployer)
    assert deployer.tf_dir == "prebuilt/kind"
    # "kind" is in the default stack, so the kind resolver is used.
    assert deployer.variables["_resolver"] == "kind"


def test_explicit_non_tofu_returns_gcp_fallback():
    deployer = get_deployer(
        {"deployer": "kubetest2"}, "test-project", "test-cluster", "us-central1-a"
    )

    assert isinstance(deployer, DummyGCPDeployer)
    assert deployer.project == "test-project"
    assert deployer.cluster_name == "test-cluster"
    assert deployer.location == "us-central1-a"


def test_cloud_provider_env_selects_gcp(monkeypatch):
    monkeypatch.setenv("CLOUD_PROVIDER", "gcp")

    deployer = get_deployer({}, "test-project", "test-cluster", "us-central1-a")

    assert isinstance(deployer, DummyGCPDeployer)


def test_location_precedence_argument_wins(monkeypatch):
    monkeypatch.setenv("GCP_LOCATION", "us-west1-b")

    deployer = get_deployer(
        {"deployer": "kubetest2"}, "test-project", "test-cluster", "europe-west1-b"
    )

    assert deployer.location == "europe-west1-b"


def test_location_from_env_when_no_argument(monkeypatch):
    monkeypatch.setenv("GCP_LOCATION", "us-west1-b")

    deployer = get_deployer(
        {"deployer": "kubetest2"}, "test-project", "test-cluster", global_location=None
    )

    assert deployer.location == "us-west1-b"


def test_location_default_when_unset():
    deployer = get_deployer(
        {"deployer": "kubetest2"}, "test-project", "test-cluster", global_location=None
    )

    assert deployer.location == "us-central1-a"


def test_tofu_custom_stack_uses_gcp_resolver():
    infra_config = {
        "deployer": "tofu",
        "stack": "custom/stack",
        "variables": {"node_count": 5},
    }

    deployer = get_deployer(infra_config, "test-project", "test-cluster", "us-central1-a")

    assert isinstance(deployer, DummyTFDeployer)
    assert deployer.tf_dir == "custom/stack"
    # No "kind" in the stack name and no CLOUD_PROVIDER, so it deduces gcp.
    assert deployer.variables["_resolver"] == "gcp"
    assert deployer.variables["custom"] == {"node_count": 5}


def test_resolver_output_is_passed_through_to_deployer():
    deployer = get_deployer({"deployer": "tofu", "stack": "prebuilt/kind"}, "p", "c", "loc")

    # The deployer receives exactly what the resolver returned.
    assert deployer.variables == {
        "_resolver": "kind",
        "stack": "prebuilt/kind",
        "custom": {},
        "project_id": "p",
        "cluster_name": "c",
        "location": "loc",
    }
