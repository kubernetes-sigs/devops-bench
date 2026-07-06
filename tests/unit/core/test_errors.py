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

"""Unit tests for devops_bench.core.errors."""

import pytest

from devops_bench.core.errors import (
    AlreadyRegisteredError,
    ConfigError,
    DevOpsBenchError,
    MissingDependencyError,
    NotRegisteredError,
    RegistryError,
    SubprocessError,
)


@pytest.mark.parametrize(
    "error_cls",
    [
        ConfigError,
        RegistryError,
        AlreadyRegisteredError,
        NotRegisteredError,
        MissingDependencyError,
        SubprocessError,
    ],
)
def test_all_errors_derive_from_base(error_cls):
    assert issubclass(error_cls, DevOpsBenchError)


def test_registry_errors_share_registry_base():
    assert issubclass(AlreadyRegisteredError, RegistryError)
    assert issubclass(NotRegisteredError, RegistryError)


def test_already_registered_error_message_and_fields():
    err = AlreadyRegisteredError("agents", "gemini")
    assert err.registry_name == "agents"
    assert err.key == "gemini"
    assert "gemini" in str(err)
    assert "agents" in str(err)


def test_not_registered_error_lists_available_sorted():
    err = NotRegisteredError("agents", "missing", available=["beta", "alpha"])
    assert err.available == ("beta", "alpha")
    message = str(err)
    assert "missing" in message
    assert message.index("alpha") < message.index("beta")


def test_not_registered_error_without_available():
    err = NotRegisteredError("agents", "missing")
    assert "available" not in str(err)


def test_missing_dependency_error_hints_install_command():
    err = MissingDependencyError("The GCP deployer", "gcp")
    assert err.feature == "The GCP deployer"
    assert err.extra == "gcp"
    assert "pip install devops-bench[gcp]" in str(err)


def test_subprocess_error_captures_streams():
    err = SubprocessError(["kubectl", "get", "pods"], returncode=1, stdout="out", stderr="boom")
    assert err.cmd == ["kubectl", "get", "pods"]
    assert err.returncode == 1
    assert err.stdout == "out"
    assert err.stderr == "boom"
    rendered = str(err)
    assert "kubectl get pods" in rendered
    assert "exit code 1" in rendered
    assert "boom" in rendered
