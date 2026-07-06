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

"""Curated public API for the core primitives."""

from devops_bench.core.config import first_env, get_bool, get_env, get_int, require_env
from devops_bench.core.context import ClusterInfo, RunContext
from devops_bench.core.errors import (
    AlreadyRegisteredError,
    ConfigError,
    DevOpsBenchError,
    MissingDependencyError,
    NotRegisteredError,
    RegistryError,
    SubprocessError,
)
from devops_bench.core.logging import configure_logging, get_logger
from devops_bench.core.registry import Registry
from devops_bench.core.results import Result, Status
from devops_bench.core.run_env import RunEnv

__all__ = [
    "ClusterInfo",
    "RunContext",
    "RunEnv",
    "Registry",
    "Result",
    "Status",
    "get_logger",
    "configure_logging",
    "get_env",
    "require_env",
    "first_env",
    "get_bool",
    "get_int",
    "DevOpsBenchError",
    "ConfigError",
    "RegistryError",
    "AlreadyRegisteredError",
    "NotRegisteredError",
    "MissingDependencyError",
    "SubprocessError",
]
