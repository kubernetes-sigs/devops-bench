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

"""Unit tests for devops_bench.core.run_env."""

import os
from pathlib import Path

import pytest

from devops_bench.core.run_env import RunEnv


def test_passthrough_when_not_parallel(monkeypatch):
    monkeypatch.delenv("KUBECONFIG", raising=False)
    monkeypatch.delenv("CLOUDSDK_CONFIG", raising=False)
    monkeypatch.delenv("TF_DATA_DIR", raising=False)

    run_env = RunEnv.create(parallel=False, run_id="fixed")
    run_env.apply()

    assert run_env.isolated is False
    assert run_env.cluster_name("my-cluster") == "my-cluster"
    # No env mutation when isolation is off.
    assert "KUBECONFIG" not in os.environ
    assert "CLOUDSDK_CONFIG" not in os.environ
    assert "TF_DATA_DIR" not in os.environ


def test_explicit_run_id_preferred_over_env(monkeypatch):
    monkeypatch.setenv("RUN_ID", "from-env")
    assert RunEnv.create(parallel=False, run_id="explicit").run_id == "explicit"


def test_run_id_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("RUN_ID", "from-env")
    assert RunEnv.create(parallel=False).run_id == "from-env"


def test_apply_sets_isolated_env_and_creates_dirs(monkeypatch, tmp_path):
    # Swap in a copy so apply()'s mutations never touch the real environment.
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.delenv("KUBECONFIG", raising=False)

    run_env = RunEnv.create(parallel=True, run_id="run-1", state_root=tmp_path)
    run_env.apply()

    expected_dir = tmp_path / "run-1"
    assert run_env.isolated is True
    assert run_env.run_dir == expected_dir
    assert os.environ["KUBECONFIG"] == str(expected_dir / "kubeconfig")
    assert os.environ["CLOUDSDK_CONFIG"] == str(expected_dir / "gcloud")
    assert os.environ["TF_DATA_DIR"] == str(expected_dir / "tf-data")
    # gcloud config + tofu data dirs are created so the tools find them.
    assert (expected_dir / "gcloud").is_dir()
    assert (expected_dir / "tf-data").is_dir()


def test_apply_publishes_parallel_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Swap in a copy so apply()'s mutations never touch the real environment.
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.delenv("BENCH_PARALLEL", raising=False)

    RunEnv.create(parallel=True, run_id="run-1", state_root=tmp_path).apply()

    # Components constructed after apply() (e.g. the eval harness) read
    # BENCH_PARALLEL from env, so flag-only parallel must be published.
    assert os.environ["BENCH_PARALLEL"] == "true"


def test_apply_does_not_publish_parallel_when_not_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.delenv("BENCH_PARALLEL", raising=False)

    RunEnv.create(parallel=False, run_id="run-1").apply()

    assert "BENCH_PARALLEL" not in os.environ


def test_restore_undoes_apply_mutations(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.setenv("KUBECONFIG", "/original/kubeconfig")
    added = ("CLOUDSDK_CONFIG", "TF_DATA_DIR", "RUN_ID", "BENCH_RUN_DIR", "BENCH_PARALLEL")
    for var in added:
        monkeypatch.delenv(var, raising=False)

    run_env = RunEnv.create(parallel=True, run_id="run-1", state_root=tmp_path)
    run_env.apply()
    assert os.environ["BENCH_PARALLEL"] == "true"

    run_env.restore()

    # Pre-existing keys return to their prior values; added keys are removed.
    assert os.environ["KUBECONFIG"] == "/original/kubeconfig"
    for var in added:
        assert var not in os.environ


def test_restore_is_noop_without_apply(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(os, "environ", os.environ.copy())
    snapshot = dict(os.environ)

    # Neither the non-isolated pass-through nor an isolated env whose apply()
    # never ran may touch the environment on restore().
    RunEnv.create(parallel=False, run_id="run-1").restore()
    RunEnv.create(parallel=True, run_id="run-2", state_root=tmp_path).restore()

    assert dict(os.environ) == snapshot


def test_state_root_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("BENCH_RUN_STATE_ROOT", str(tmp_path))
    run_env = RunEnv.create(parallel=True, run_id="run-2")
    assert run_env.run_dir == tmp_path / "run-2"


def test_cluster_name_is_prefixed_and_deterministic(tmp_path):
    a = RunEnv.create(parallel=True, run_id="run-A", state_root=tmp_path)
    b = RunEnv.create(parallel=True, run_id="run-A", state_root=tmp_path)
    c = RunEnv.create(parallel=True, run_id="run-B", state_root=tmp_path)

    name_a = a.cluster_name("hpa-test")
    # Deterministic in the run id.
    assert name_a == b.cluster_name("hpa-test")
    # Distinct run ids yield distinct cluster names.
    assert name_a != c.cluster_name("hpa-test")
    # Token leads so it survives service-account name truncation (substr 0,10).
    assert name_a.startswith(a.cluster_token)


def test_cluster_token_is_dns_safe(tmp_path):
    token = RunEnv.create(
        parallel=True, run_id="20260623-150000-9999", state_root=tmp_path
    ).cluster_token
    assert token[0].isalpha()
    assert token.islower()
    assert token.isalnum()
    assert len(token) == 8


def test_cluster_name_respects_gke_length_limit(tmp_path):
    base = "a" * 60
    name = RunEnv.create(parallel=True, run_id="run-X", state_root=tmp_path).cluster_name(base)
    assert len(name) <= 40
    assert not name.endswith("-")


def test_distinct_run_ids_get_distinct_token_prefixes(tmp_path):
    a = RunEnv.create(parallel=True, run_id="run-A", state_root=tmp_path)
    c = RunEnv.create(parallel=True, run_id="run-B", state_root=tmp_path)
    assert a.cluster_name("hpa")[:10] != c.cluster_name("hpa")[:10]
