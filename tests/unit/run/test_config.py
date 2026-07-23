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

"""Tests for ``BenchmarkConfig.from_env`` resolution (env passed explicitly)."""

from __future__ import annotations

from devops_bench.run import BenchmarkConfig


def test_from_env_reads_primary_aliases() -> None:
    """Primary env names populate project/cluster/limit/results_root."""
    config = BenchmarkConfig.from_env(
        "tasks/demo.yaml",
        env={
            "PROJECT_ID": "proj-primary",
            "CLUSTER_NAME": "clus-primary",
            "EVAL_LIMIT": "7",
            "RESULTS_ROOT": "/tmp/out",
        },
    )
    assert config.source == "tasks/demo.yaml"
    assert config.project_id == "proj-primary"
    assert config.cluster_name == "clus-primary"
    assert config.limit == 7
    assert config.results_root == "/tmp/out"


def test_from_env_falls_back_to_secondary_aliases() -> None:
    """GCP_PROJECT_ID / GKE_CLUSTER_NAME resolve when the primaries are unset."""
    config = BenchmarkConfig.from_env(
        "tasks/demo.yaml",
        env={
            "GCP_PROJECT_ID": "proj-fallback",
            "GKE_CLUSTER_NAME": "clus-fallback",
        },
    )
    assert config.project_id == "proj-fallback"
    assert config.cluster_name == "clus-fallback"


def test_from_env_defaults() -> None:
    """An empty env yields sensible defaults; agent_type stays None."""
    config = BenchmarkConfig.from_env("tasks/demo.yaml", env={})
    assert config.project_id is None
    assert config.cluster_name is None
    assert config.limit is None
    assert config.results_root == "results"
    assert config.agent_type is None
    assert config.judge_provider is None
    assert config.judge_model is None
    assert config.no_infra is False
    assert config.no_teardown is False


def test_from_env_agent_type_when_set() -> None:
    """BENCH_AGENT_TYPE is read when present."""
    config = BenchmarkConfig.from_env("tasks/demo.yaml", env={"BENCH_AGENT_TYPE": "api"})
    assert config.agent_type == "api"


def test_from_env_bool_flags() -> None:
    """BENCH_NO_INFRA / BENCH_NO_TEARDOWN parse as booleans."""
    config = BenchmarkConfig.from_env(
        "tasks/demo.yaml",
        env={"BENCH_NO_INFRA": "true", "BENCH_NO_TEARDOWN": "1"},
    )
    assert config.no_infra is True
    assert config.no_teardown is True
