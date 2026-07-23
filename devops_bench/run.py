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

"""Library entrypoint: load config + tasks, run the harness, return results.

A bare ``import devops_bench.run`` must not pull ``deepeval`` / provider SDKs /
``mcp``; the harness, task loader, and judge factory are imported inside
:func:`run_benchmark`, not at module top.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from devops_bench.core import (
    ConfigError,
    RunEnv,
    first_env,
    get_bool,
    get_env,
    get_int,
    get_logger,
)

__all__ = ["BenchmarkConfig", "BenchmarkResult", "run_benchmark"]

_log = get_logger("run")


@dataclass(frozen=True)
class BenchmarkConfig:
    """Resolved configuration for a single benchmark run.

    Attributes:
        source: Tasks directory or task spec file (``.yaml`` / ``.yml`` / ``.json``).
        project_id: GCP project id; required unless infra is disabled.
        cluster_name: GKE cluster name; required unless infra is disabled.
        limit: Optional cap on the number of tasks to run (slice from the front).
        results_root: Root directory under which per-run subdirectories are created.
        agent_type: Override for ``BENCH_AGENT_TYPE``; ``None`` leaves env in control.
        judge_provider: Override for ``JUDGE_PROVIDER`` used to build the judge.
        judge_model: Override for ``JUDGE_MODEL`` used to build the judge.
        no_infra: Skip infrastructure provisioning (no project/cluster required).
        no_teardown: Skip teardown of provisioned infrastructure.
        parallel: Enable per-run isolation (own kubeconfig / gcloud config /
            tofu data dir and a run-unique cluster name) so multiple benchmark
            processes can run concurrently on one host.
        run_id: Explicit run id used for isolation and artifact naming; ``None``
            falls back to ``RUN_ID`` env, then a generated PID/timestamp id.
    """

    source: str
    project_id: str | None = None
    cluster_name: str | None = None
    limit: int | None = None
    results_root: str = "results"
    agent_type: str | None = None
    judge_provider: str | None = None
    judge_model: str | None = None
    no_infra: bool = False
    no_teardown: bool = False
    parallel: bool = False
    run_id: str | None = None

    @classmethod
    def from_env(cls, source: str, *, env: Mapping[str, str] | None = None) -> BenchmarkConfig:
        """Build a config from ``source`` plus environment variables.

        Args:
            source: Tasks directory or task spec file.
            env: Optional mapping to read from instead of ``os.environ``.

        Returns:
            A :class:`BenchmarkConfig` with fields resolved from the environment.
        """
        return cls(
            source=source,
            project_id=first_env("PROJECT_ID", "GCP_PROJECT_ID", env=env),
            cluster_name=first_env("CLUSTER_NAME", "GKE_CLUSTER_NAME", env=env),
            limit=get_int("EVAL_LIMIT", env=env),
            results_root=get_env("RESULTS_ROOT", "results", env=env),
            agent_type=get_env("BENCH_AGENT_TYPE", env=env),
            judge_provider=get_env("JUDGE_PROVIDER", env=env),
            judge_model=get_env("JUDGE_MODEL", env=env),
            no_infra=get_bool("BENCH_NO_INFRA", env=env),
            no_teardown=get_bool("BENCH_NO_TEARDOWN", env=env),
            parallel=get_bool("BENCH_PARALLEL", env=env),
            run_id=get_env("RUN_ID", env=env),
        )


@dataclass(frozen=True)
class BenchmarkResult:
    """Outcome of a benchmark run.

    Attributes:
        results: Per-task result dicts.
        run_dir: Directory holding the run's artifacts.
        results_path: Path of the written ``results.json``.
        rows_path: Path of the flattened, ingest-ready ``rows.json``. The file is
            written best-effort, so it may be absent if row emission failed.
        manifest_path: Path of the run-level ``manifest.json`` (same best-effort
            caveat as ``rows_path``).
    """

    results: list[dict[str, Any]]
    run_dir: Path
    results_path: Path
    rows_path: Path
    manifest_path: Path


def run_benchmark(config: BenchmarkConfig) -> BenchmarkResult:
    """Run the benchmark pipeline described by ``config``.

    Args:
        config: Resolved run configuration.

    Returns:
        A :class:`BenchmarkResult` carrying the results, run directory, and the
        ``results.json`` path.

    Raises:
        ConfigError: If infrastructure is enabled but project id / cluster name
            are missing, or if ``config.source`` does not exist.
    """
    infra_disabled = config.no_infra or get_bool("BENCH_NO_INFRA")
    if not infra_disabled and (not config.project_id or not config.cluster_name):
        raise ConfigError(
            "PROJECT_ID/GCP_PROJECT_ID and CLUSTER_NAME/GKE_CLUSTER_NAME must be "
            "set (or pass --no-infra / BENCH_NO_INFRA=true)"
        )
    project_id = config.project_id or "no-infra-project"
    cluster_name = config.cluster_name or "no-infra-cluster"

    # Establish per-run isolation BEFORE any provisioning so every gcloud /
    # kubectl / tofu / agent subprocess inherits the run-scoped kubeconfig,
    # gcloud config, and tofu data dir. A no-op unless ``parallel`` is set.
    run_env = RunEnv.create(parallel=config.parallel, run_id=config.run_id)
    run_env.apply()
    cluster_name = run_env.cluster_name(cluster_name)

    from devops_bench.evalharness import DefaultEvalHarness, ResultReporter
    from devops_bench.tasks import FileSystemTaskLoader

    judge = None
    if config.judge_provider or config.judge_model:
        from devops_bench.metrics import get_judge_model

        judge = get_judge_model(provider=config.judge_provider, model_name=config.judge_model)

    tasks = FileSystemTaskLoader().load_tasks(config.source)
    if config.limit is not None:
        tasks = tasks[: config.limit]

    reporter = ResultReporter(
        config.results_root, run_id=run_env.run_id if run_env.isolated else None
    )
    harness = DefaultEvalHarness(
        project_id,
        cluster_name,
        judge_model=judge,
        results_root=config.results_root,
        reporter=reporter,
        agent_type=config.agent_type,
        no_infra=config.no_infra,
        no_teardown=config.no_teardown,
    )
    results = harness.run(tasks)

    run_dir = reporter.last_run_dir
    if run_dir is None:  # pragma: no cover - defensive; harness always creates one
        run_dir = Path(config.results_root)
    results_path = run_dir / "results.json"
    _log.info("benchmark results written to %s", results_path)
    return BenchmarkResult(
        results=results,
        run_dir=run_dir,
        results_path=results_path,
        rows_path=run_dir / "rows.json",
        manifest_path=run_dir / "manifest.json",
    )
