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
    get_bool,
    get_env,
    get_int,
    get_logger,
)

__all__ = ["BenchmarkConfig", "BenchmarkResult", "run_benchmark"]

_log = get_logger("run")

# Placeholders for the identifiers a run still has to name but never uses.
_NO_INFRA_PROJECT = "no-infra-project"
_NO_INFRA_CLUSTER = "no-infra-cluster"
# Stand-in project id for a run that provisions only local clusters. Matches
# the default the kind stacks already carry for their ignored project_id.
_LOCAL_PROJECT_ID = "local-kind"


@dataclass(frozen=True)
class BenchmarkConfig:
    """Resolved configuration for a single benchmark run.

    Attributes:
        source: Tasks directory or task spec file (``.yaml`` / ``.yml`` / ``.json``).
        project_id: Cloud project id; required only when a task in the run
            resolves to a provider that bills to one.
        cluster_name: Name of the target Kubernetes cluster; required unless
            infra is disabled.
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

        This is the only place environment variables are folded into a config;
        :func:`run_benchmark` treats the config it receives as authoritative.
        Only vendor-neutral names are read here — provider-specific variables
        (e.g. ``GCP_PROJECT_ID``) are resolved by the provider layer itself
        (GCP provisioning and Vertex AI model auth read it directly).

        Args:
            source: Tasks directory or task spec file.
            env: Optional mapping to read from instead of ``os.environ``.

        Returns:
            A :class:`BenchmarkConfig` with fields resolved from the environment.
        """
        return cls(
            source=source,
            project_id=get_env("PROJECT_ID", env=env),
            cluster_name=get_env("CLUSTER_NAME", env=env),
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


def _cloud_task_names(tasks: list[Any]) -> list[str]:
    """Name the tasks in ``tasks`` whose provider bills to a cloud project.

    Args:
        tasks: Loaded task specs.

    Returns:
        The names of the cloud-backed tasks, in load order.
    """
    from devops_bench.deployers.factory import needs_cloud_project

    return [task.name for task in tasks if needs_cloud_project(task.infrastructure or {})]


def _resolve_project_and_cluster(config: BenchmarkConfig, tasks: list[Any]) -> tuple[str, str]:
    """Validate the run's project / cluster settings and fill in the defaults.

    The cluster name is always required with infra on: every provider
    provisions a cluster and names it. The project id is required only when a
    task in the run actually targets a cloud -- a run of kind-only tasks bills
    nothing to a project, so demanding one is a barrier with no purpose behind
    it. Local-only runs get :data:`_LOCAL_PROJECT_ID`, which is what the kind
    stacks already default their (ignored) ``project_id`` variable to.

    The survey resolves each task's provider exactly the way provisioning
    later will, so validation and the deployer can never disagree about
    whether a cloud is involved. That includes an ambient ``INFRA_PROVIDER``
    export, which the factory honours and warns about; the point of the check
    is to spare a task that declares a local provider, not to second-guess the
    resolution.

    Args:
        config: Resolved run configuration.
        tasks: The tasks this run will execute, already limited.

    Returns:
        The ``(project_id, cluster_name)`` pair to hand the harness.

    Raises:
        ConfigError: If infra is enabled and the cluster name is missing, or a
            cloud-backed task is in the run and the project id is missing.
    """
    if config.no_infra:
        return (config.project_id or _NO_INFRA_PROJECT, config.cluster_name or _NO_INFRA_CLUSTER)

    if not config.cluster_name:
        raise ConfigError("CLUSTER_NAME must be set (or pass --no-infra / BENCH_NO_INFRA=true)")

    cloud_tasks = _cloud_task_names(tasks)
    if cloud_tasks and not config.project_id:
        raise ConfigError(
            "PROJECT_ID must be set: "
            f"{', '.join(cloud_tasks)} target a cloud provider "
            "(or pass --no-infra / BENCH_NO_INFRA=true)"
        )
    if not cloud_tasks and not config.project_id:
        _log.info(
            "no task in this run targets a cloud provider; using project id %r",
            _LOCAL_PROJECT_ID,
        )
    return (config.project_id or _LOCAL_PROJECT_ID, config.cluster_name)


def run_benchmark(config: BenchmarkConfig) -> BenchmarkResult:
    """Run the benchmark pipeline described by ``config``.

    Args:
        config: Resolved run configuration. Taken as authoritative: values are
            used as-is, with environment folding done only by
            :meth:`BenchmarkConfig.from_env`.

    Returns:
        A :class:`BenchmarkResult` carrying the results, run directory, and the
        ``results.json`` path.

    Raises:
        ConfigError: If ``config.source`` does not exist, if infrastructure is
            enabled and no cluster name is set, or if a task in the run targets
            a cloud provider and no project id is set.
    """
    from devops_bench.tasks import FileSystemTaskLoader

    # Load the tasks before validating the run, because what a run requires is
    # a property of the tasks in it: only a task whose provider bills to a
    # cloud needs a project id. Slice to ``limit`` first so the survey covers
    # the tasks that will actually run.
    tasks = FileSystemTaskLoader().load_tasks(config.source)
    if config.limit is not None:
        tasks = tasks[: config.limit]

    # The config is authoritative: env resolution happens only in
    # ``BenchmarkConfig.from_env``, so validation and the harness always
    # observe the same values and an explicit setting is never overridden
    # by ambient environment variables.
    project_id, cluster_name = _resolve_project_and_cluster(config, tasks)

    # Establish per-run isolation BEFORE any provisioning so every gcloud /
    # kubectl / tofu / agent subprocess inherits the run-scoped kubeconfig,
    # gcloud config, and tofu data dir. A no-op unless ``parallel`` is set.
    run_env = RunEnv.create(parallel=config.parallel, run_id=config.run_id)
    run_env.apply()
    cluster_name = run_env.cluster_name(cluster_name)

    # restore() in the finally keeps apply()'s process-env mutations scoped to
    # this invocation, so an isolated run does not leak BENCH_PARALLEL /
    # KUBECONFIG / etc. into a later serial call from the same process.
    try:
        from devops_bench.evalharness import DefaultEvalHarness, ResultReporter

        judge = None
        if config.judge_provider or config.judge_model:
            from devops_bench.metrics import get_judge_model

            judge = get_judge_model(provider=config.judge_provider, model_name=config.judge_model)

        # An explicitly supplied run id names the artifacts even in serial
        # mode; the auto-generated id stays out of serial dir names so the
        # default timestamped naming is unchanged.
        reporter = ResultReporter(
            config.results_root,
            run_id=config.run_id or (run_env.run_id if run_env.isolated else None),
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
    finally:
        run_env.restore()
