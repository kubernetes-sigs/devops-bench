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

"""Unit tests for devops_bench.core.config."""

import pytest

from devops_bench.core import config
from devops_bench.core.errors import ConfigError


def test_get_env_returns_value():
    assert config.get_env("FOO", env={"FOO": "bar"}) == "bar"


def test_get_env_missing_returns_default():
    assert config.get_env("FOO", "fallback", env={}) == "fallback"


def test_get_env_blank_treated_as_unset():
    assert config.get_env("FOO", "fallback", env={"FOO": "   "}) == "fallback"


def test_get_env_reads_os_environ_by_default(monkeypatch):
    monkeypatch.setenv("BENCH_TEST_VAR", "live")
    assert config.get_env("BENCH_TEST_VAR") == "live"


def test_require_env_returns_value():
    assert config.require_env("FOO", env={"FOO": "bar"}) == "bar"


def test_require_env_raises_when_missing():
    with pytest.raises(ConfigError, match="FOO"):
        config.require_env("FOO", env={})


def test_first_env_returns_first_set():
    env = {"GCP_PROJECT_ID": "secondary"}
    assert config.first_env("PROJECT_ID", "GCP_PROJECT_ID", env=env) == "secondary"


def test_first_env_prefers_earlier_name():
    env = {"PROJECT_ID": "primary", "GCP_PROJECT_ID": "secondary"}
    assert config.first_env("PROJECT_ID", "GCP_PROJECT_ID", env=env) == "primary"


def test_first_env_default_when_none_set():
    assert config.first_env("A", "B", default="d", env={}) == "d"


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "Yes", "on", "  y "])
def test_get_bool_truthy(raw):
    assert config.get_bool("FLAG", env={"FLAG": raw}) is True


@pytest.mark.parametrize("raw", ["0", "false", "No", "off", "n"])
def test_get_bool_falsy(raw):
    assert config.get_bool("FLAG", default=True, env={"FLAG": raw}) is False


def test_get_bool_unset_returns_default():
    assert config.get_bool("FLAG", default=True, env={}) is True


def test_get_bool_blank_returns_default():
    assert config.get_bool("FLAG", default=True, env={"FLAG": ""}) is True


def test_get_bool_invalid_raises():
    with pytest.raises(ConfigError, match="boolean"):
        config.get_bool("FLAG", env={"FLAG": "maybe"})


def test_get_int_parses():
    assert config.get_int("N", env={"N": "42"}) == 42


def test_get_int_default_when_missing():
    assert config.get_int("N", default=7, env={}) == 7


def test_get_int_blank_returns_default():
    assert config.get_int("N", default=7, env={"N": "   "}) == 7


def test_get_int_invalid_raises():
    with pytest.raises(ConfigError, match="integer"):
        config.get_int("N", env={"N": "not-a-number"})
