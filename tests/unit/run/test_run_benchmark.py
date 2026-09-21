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

"""Tests for ``run_benchmark`` with a stubbed harness and task loader."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from devops_bench.core import ConfigError
from devops_bench.run import BenchmarkConfig, BenchmarkResult, run_benchmark

_CANNED_RESULTS = [
    {"name": "a", "status": "success"},
    {"name": "b", "status": "failed"},
]


class FakeHarness:
    """Records construction kwargs and the env at construction time."""

    instances: list[FakeHarness] = []

    def __init__(
        self,
        project_id: str,
        cluster_name: str,
        *,
        judge_model: object = None,
        results_root: str = "results",
        reporter: object = None,
        **kwargs: object,
    ) -> None:
        self.project_id = project_id
        self.cluster_name = cluster_name
        self.judge_model = judge_model
        self.results_root = results_root
        self.reporter = reporter
        # Flag overrides now arrive as explicit constructor kwargs (DI), not via
        # ``os.environ``; capture them so tests assert on what was injected.
        self.agent_type = kwargs.get("agent_type")
        self.no_infra = kwargs.get("no_infra")
        self.no_teardown = kwargs.get("no_teardown")
        self.env_at_construction = dict(os.environ)
        self.task_count: int | None = None
        FakeHarness.instances.append(self)

    def run(self, tasks: list[object]) -> list[dict[str, object]]:
        self.task_count = len(tasks)
        self.reporter.new_run_dir()  # set reporter.last_run_dir
        return list(_CANNED_RESULTS)


@dataclass(frozen=True)
class FakeTask:
    """The two attributes ``run_benchmark`` reads off a loaded task."""

    name: str
    infrastructure: dict[str, object] = field(default_factory=dict)


def _fake_load_tasks(count: int) -> Callable[[object, str], list[object]]:
    def _loader(self: object, source: str) -> list[object]:
        return [FakeTask(name=f"task-{i}") for i in range(count)]

    return _loader


def _load(*tasks: FakeTask) -> Callable[[object, str], list[object]]:
    def _loader(self: object, source: str) -> list[object]:
        return list(tasks)

    return _loader


@pytest.fixture(autouse=True)
def _reset_fake_instances() -> None:
    FakeHarness.instances = []


def _patch(monkeypatch: pytest.MonkeyPatch, *, task_count: int = 5) -> None:
    monkeypatch.setattr("devops_bench.evalharness.DefaultEvalHarness", FakeHarness)
    monkeypatch.setattr(
        "devops_bench.tasks.FileSystemTaskLoader.load_tasks", _fake_load_tasks(task_count)
    )


def test_loads_and_limits_tasks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch(monkeypatch, task_count=5)
    config = BenchmarkConfig(
        source="src",
        no_infra=True,
        limit=2,
        results_root=str(tmp_path),
    )
    run_benchmark(config)
    assert FakeHarness.instances[0].task_count == 2


def test_returns_benchmark_result(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch(monkeypatch)
    config = BenchmarkConfig(source="src", no_infra=True, results_root=str(tmp_path))
    result = run_benchmark(config)
    assert isinstance(result, BenchmarkResult)
    assert result.results == _CANNED_RESULTS
    assert result.run_dir.parent == tmp_path
    assert result.results_path == result.run_dir / "results.json"
    assert result.rows_path == result.run_dir / "rows.json"
    assert result.manifest_path == result.run_dir / "manifest.json"


def test_infra_enabled_missing_project_cluster_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch(monkeypatch)
    monkeypatch.delenv("BENCH_NO_INFRA", raising=False)
    config = BenchmarkConfig(
        source="src",
        no_infra=False,
        project_id=None,
        cluster_name=None,
        results_root=str(tmp_path),
    )
    with pytest.raises(ConfigError):
        run_benchmark(config)


def test_no_infra_uses_placeholders(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch(monkeypatch)
    config = BenchmarkConfig(source="src", no_infra=True, results_root=str(tmp_path))
    run_benchmark(config)
    harness = FakeHarness.instances[0]
    assert harness.project_id == "no-infra-project"
    assert harness.cluster_name == "no-infra-cluster"


class TestTheProjectRequirementFollowsTheTasks:
    """``PROJECT_ID`` is demanded by the tasks that bill, not by infra being on.

    Every case here runs with infra enabled and a cluster name set, so the
    only thing under test is whether the project id was required.
    """

    @staticmethod
    def _config(tmp_path: Path, **overrides: object) -> BenchmarkConfig:
        return replace(
            BenchmarkConfig(
                source="src",
                cluster_name="bench",
                results_root=str(tmp_path),
            ),
            **overrides,
        )

    @pytest.fixture(autouse=True)
    def _no_ambient_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An INFRA_PROVIDER left in the shell would answer the question these
        # tests are asking, so the tasks get to answer it instead.
        monkeypatch.delenv("INFRA_PROVIDER", raising=False)
        monkeypatch.delenv("BENCH_NO_INFRA", raising=False)

    def test_a_kind_only_run_needs_no_project(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch(monkeypatch)
        monkeypatch.setattr(
            "devops_bench.tasks.FileSystemTaskLoader.load_tasks",
            _load(FakeTask(name="opa", infrastructure={"provider": "kind"})),
        )
        run_benchmark(self._config(tmp_path))
        assert FakeHarness.instances[0].project_id == "local-kind"

    def test_a_task_that_deduces_kind_from_its_stack_needs_no_project(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """No ``provider:`` key, but ``prebuilt/kind`` deduces one."""
        _patch(monkeypatch)
        monkeypatch.setattr(
            "devops_bench.tasks.FileSystemTaskLoader.load_tasks",
            _load(FakeTask(name="opa", infrastructure={"stack": "prebuilt/kind"})),
        )
        run_benchmark(self._config(tmp_path))
        assert FakeHarness.instances[0].project_id == "local-kind"

    def test_a_noop_task_needs_no_project(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch(monkeypatch)
        monkeypatch.setattr(
            "devops_bench.tasks.FileSystemTaskLoader.load_tasks",
            _load(FakeTask(name="plumbing", infrastructure={"deployer": "noop"})),
        )
        run_benchmark(self._config(tmp_path))
        assert FakeHarness.instances[0].project_id == "local-kind"

    def test_a_cloud_task_without_a_project_is_an_error_naming_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch(monkeypatch)
        monkeypatch.setattr(
            "devops_bench.tasks.FileSystemTaskLoader.load_tasks",
            _load(
                FakeTask(name="opa", infrastructure={"provider": "kind"}),
                FakeTask(name="deploy-hello-app", infrastructure={"provider": "gcp"}),
            ),
        )
        with pytest.raises(ConfigError) as excinfo:
            run_benchmark(self._config(tmp_path))
        message = str(excinfo.value)
        assert "PROJECT_ID" in message
        # Names the task that needs it, and only that one: a mixed run should
        # not leave the operator guessing which task forced the requirement.
        assert "deploy-hello-app" in message
        assert "opa" not in message
        assert FakeHarness.instances == []

    def test_a_cloud_task_with_a_project_runs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch(monkeypatch)
        monkeypatch.setattr(
            "devops_bench.tasks.FileSystemTaskLoader.load_tasks",
            _load(FakeTask(name="deploy-hello-app", infrastructure={"provider": "gcp"})),
        )
        run_benchmark(self._config(tmp_path, project_id="real-project"))
        assert FakeHarness.instances[0].project_id == "real-project"

    def test_the_survey_covers_only_the_tasks_the_limit_keeps(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A cloud task sliced off by ``--limit`` does not demand a project."""
        _patch(monkeypatch)
        monkeypatch.setattr(
            "devops_bench.tasks.FileSystemTaskLoader.load_tasks",
            _load(
                FakeTask(name="opa", infrastructure={"provider": "kind"}),
                FakeTask(name="deploy-hello-app", infrastructure={"provider": "gcp"}),
            ),
        )
        run_benchmark(self._config(tmp_path, limit=1))
        assert FakeHarness.instances[0].task_count == 1
        assert FakeHarness.instances[0].project_id == "local-kind"

    def test_an_unresolvable_provider_does_not_demand_a_project(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Its real error is "this stack names no provider"; don't mask it.

        The task cannot provision either way, but the launcher must not answer
        with a misleading demand for a project id -- the deployer raises the
        accurate error later, before anything is applied.
        """
        _patch(monkeypatch)
        monkeypatch.setattr(
            "devops_bench.tasks.FileSystemTaskLoader.load_tasks",
            _load(FakeTask(name="mystery", infrastructure={"stack": "prebuilt/minimum"})),
        )
        run_benchmark(self._config(tmp_path))
        assert FakeHarness.instances[0].project_id == "local-kind"

    def test_an_ambient_infra_provider_is_honoured_by_the_survey(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Validation must resolve providers the way provisioning will.

        ``INFRA_PROVIDER`` still wins at deploy time, so a run it points at a
        cloud has to be asked for a project id -- otherwise the launcher waves
        the run through and the apply fails on a placeholder project.
        """
        _patch(monkeypatch)
        monkeypatch.setenv("INFRA_PROVIDER", "gcp")
        monkeypatch.setattr(
            "devops_bench.tasks.FileSystemTaskLoader.load_tasks",
            _load(FakeTask(name="opa", infrastructure={"provider": "kind"})),
        )
        with pytest.raises(ConfigError, match="PROJECT_ID"):
            run_benchmark(self._config(tmp_path))

    def test_the_cluster_name_is_required_even_for_a_local_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch(monkeypatch)
        monkeypatch.setattr(
            "devops_bench.tasks.FileSystemTaskLoader.load_tasks",
            _load(FakeTask(name="opa", infrastructure={"provider": "kind"})),
        )
        with pytest.raises(ConfigError, match="CLUSTER_NAME"):
            run_benchmark(self._config(tmp_path, cluster_name=None))
        assert FakeHarness.instances == []


def test_agent_type_flag_overrides_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch(monkeypatch)
    monkeypatch.setenv("BENCH_AGENT_TYPE", "gemini-cli")
    config = BenchmarkConfig(
        source="src",
        no_infra=True,
        agent_type="api",
        results_root=str(tmp_path),
    )
    run_benchmark(config)
    # The flag is injected into the harness constructor, not written to env.
    assert FakeHarness.instances[0].agent_type == "api"
    assert os.environ.get("BENCH_AGENT_TYPE") == "gemini-cli"


def test_config_is_authoritative_over_ambient_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Explicit config wins; env toggles fold in only via ``from_env``.

    A hand-built ``BenchmarkConfig`` (``no_infra=False``) must still require
    project/cluster even when ``BENCH_NO_INFRA=true`` is in the environment,
    so validation and the harness always observe the same values. The CLI
    path picks the env up through ``from_env`` instead.
    """
    _patch(monkeypatch)
    monkeypatch.setenv("BENCH_NO_INFRA", "true")
    monkeypatch.setenv("BENCH_NO_TEARDOWN", "true")
    monkeypatch.delenv("BENCH_PARALLEL", raising=False)

    # Hand-built config: the ambient env must NOT flip no_infra, so the
    # missing project/cluster is still a validation error.
    with pytest.raises(ConfigError):
        run_benchmark(BenchmarkConfig(source="src", results_root=str(tmp_path)))
    assert FakeHarness.instances == []

    # from_env is the one place the env folds in.
    config = replace(BenchmarkConfig.from_env("src"), results_root=str(tmp_path))
    run_benchmark(config)
    harness = FakeHarness.instances[0]
    assert harness.no_infra is True
    assert harness.no_teardown is True


def test_parallel_config_reaches_harness_via_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``parallel=True`` set only on the config is visible at harness construction.

    The harness reads ``BENCH_PARALLEL`` from env; ``RunEnv.apply`` exports it
    before the harness is built so a flag-only ``--parallel`` still routes the
    chaos port-forward onto a free per-run port.
    """
    _patch(monkeypatch)
    # Swap in a copy so apply()'s mutations never touch the real environment.
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.delenv("BENCH_PARALLEL", raising=False)
    monkeypatch.setenv("BENCH_RUN_STATE_ROOT", str(tmp_path / "state"))
    config = BenchmarkConfig(source="src", no_infra=True, parallel=True, results_root=str(tmp_path))
    run_benchmark(config)
    harness = FakeHarness.instances[0]
    assert harness.env_at_construction.get("BENCH_PARALLEL") == "true"

    # apply()'s mutations are scoped to the invocation: the env is restored
    # once the run returns...
    assert "BENCH_PARALLEL" not in os.environ
    # ...so a later serial invocation does not inherit parallel mode.
    run_benchmark(replace(config, parallel=False))
    serial = FakeHarness.instances[1]
    assert serial.env_at_construction.get("BENCH_PARALLEL") is None


def test_explicit_run_id_names_serial_run_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit run id names the artifacts even without parallel isolation."""
    _patch(monkeypatch)
    config = BenchmarkConfig(
        source="src", no_infra=True, results_root=str(tmp_path), run_id="release-07"
    )
    result = run_benchmark(config)
    assert "release-07" in result.run_dir.name
