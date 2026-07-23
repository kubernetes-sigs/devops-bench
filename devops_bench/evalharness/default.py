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

"""DefaultEvalHarness: wires agents, chaos, verification, and metrics into one pipeline."""

from __future__ import annotations

import datetime
import importlib
import json
import shutil
import tempfile
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from devops_bench.agents import AGENTS, AgentConfig, AgentResult
from devops_bench.agents.capabilities import (
    AgentRules,
    AllCapabilities,
    McpBinding,
    SkillBinding,
)
from devops_bench.chaos import ChaosSpec
from devops_bench.core import (
    ConfigError,
    MissingDependencyError,
    NotRegisteredError,
    RunContext,
    get_bool,
    get_env,
    get_logger,
)
from devops_bench.deployers.factory import get_deployer
from devops_bench.evalharness.artifacts import collect_generated_files, snapshot_dir
from devops_bench.evalharness.base import Harness
from devops_bench.evalharness.reporter import ResultReporter
from devops_bench.evalharness.scenario import (
    VERIFICATION_TIMEOUT_SEC,
    ScenarioManager,
    pick_free_port,
)
from devops_bench.tasks import Task
from devops_bench.verification import VerificationSpec

__all__ = ["DefaultEvalHarness"]

_log = get_logger("evalharness.default")

# Builtin agent modules imported at call time so their ``@AGENTS.register``
# decorators run. External packages add agents by registering with the same
# registry, with no edit here.
_BUILTIN_AGENT_MODULES: tuple[str, ...] = (
    "devops_bench.agents.cli.gemini_cli",
    "devops_bench.agents.cli.openclaw",
    "devops_bench.agents.cli.antigravity",
    "devops_bench.agents.api.agent",
)

# Aliases normalized to canonical agent keys before registry lookup.
_AGENT_TYPE_ALIASES: dict[str, str] = {
    "gemini-cli": "gemini",
}

# Default agent type when neither --agent-type nor BENCH_AGENT_TYPE is set.
_DEFAULT_AGENT_TYPE = "gemini-cli"

# Default target deployment + namespace used both for placeholder
# substitution in the agent prompt and as the chaos port-forward target, so the
# operator agent and the chaos injector address the same workload when env is
# unset.
_DEFAULT_TARGET_DEPLOYMENT = "hypercomputer-d1-frontend"
_DEFAULT_NAMESPACE = "default"

# How long to wait for the chaos agent to establish its load spike before
# starting the operator agent.
_CHAOS_ACTIVE_WAIT_SEC = 45

# Budget for draining the scenario thread. Kept above the verification budget
# so a slow-but-completing verification is not cut off, which would otherwise
# yield partial reports and race teardown.
_SCENARIO_JOIN_SEC = VERIFICATION_TIMEOUT_SEC + 60


def _ensure_builtin_agents_registered() -> None:
    """Import the builtin agent modules so their registrations fire.

    The registry is the only source of truth — this function exists so the
    harness can resolve canonical keys at call time without naming any module
    path in ``AGENTS.get``. Re-imports are no-ops thanks to ``sys.modules``.

    Catches **only** missing-dependency / import errors (an agent module may
    pull an optional SDK like ``anthropic`` that is absent on the host) — a
    real bug in an agent module (``SyntaxError``, an ``AttributeError`` at
    module top) re-raises so it cannot hide behind a silent ``debug`` log.
    """
    for module in _BUILTIN_AGENT_MODULES:
        try:
            importlib.import_module(module)
        except (ImportError, MissingDependencyError) as exc:
            # Optional SDK absent on this host. ``AGENTS.get`` will still
            # raise a clear ``NotRegisteredError`` later if the user selects
            # an agent whose module did not load.
            _log.debug("optional agent module %s not importable: %s", module, exc)


class DefaultEvalHarness(Harness):
    """Standard harness wiring every component into one pipeline.

    Each task flows through provisioning, optional background chaos, agent
    execution, artifact collection, teardown, and batch scoring. Every layer
    is consumed through its typed contract: ``Task`` in, ``AgentResult`` from
    the agent, ``ChaosResult`` / ``VerificationResult`` from the scenario,
    ``MetricScore`` from each metric. The harness routes those typed values
    through ``to_dict()`` / ``to_entry()`` / ``model_dump()`` so the on-disk
    ``results.json`` schema stays byte-stable.

    Args:
        project_id: Default GCP project ID for provisioning and placeholders.
        cluster_name: Default cluster name for provisioning and placeholders.
        judge_model: A ``DeepEvalBaseLLM`` judge used for scoring; when ``None``
            one is built from ``JUDGE_PROVIDER`` / ``JUDGE_MODEL`` on first use.
        results_root: Directory under which timestamped run dirs are created.
        reporter: Optional explicit result reporter. A default
            :class:`ResultReporter` rooted at ``results_root`` is built when
            omitted.
        default_target_deployment: Fallback deployment name used both for
            placeholder substitution and as the chaos port-forward target when
            ``TARGET_DEPLOYMENT_NAME`` is unset.
        default_namespace: Fallback namespace used for the same two purposes
            when ``NAMESPACE`` is unset.
    """

    def __init__(
        self,
        project_id: str,
        cluster_name: str,
        judge_model: Any | None = None,
        results_root: str = "results",
        *,
        reporter: ResultReporter | None = None,
        default_target_deployment: str = _DEFAULT_TARGET_DEPLOYMENT,
        default_namespace: str = _DEFAULT_NAMESPACE,
        agent_type: str | None = None,
        no_infra: bool | None = None,
        no_teardown: bool | None = None,
    ) -> None:
        self.project_id = project_id
        self.cluster_name = cluster_name
        self._judge_model = judge_model
        self.results_root = results_root
        resolved_agent_type = (
            agent_type
            if agent_type is not None
            else get_env("BENCH_AGENT_TYPE", _DEFAULT_AGENT_TYPE)
        )
        self.agent_type = (resolved_agent_type or _DEFAULT_AGENT_TYPE).lower()
        self.no_infra = no_infra if no_infra is not None else get_bool("BENCH_NO_INFRA")
        self.no_teardown = no_teardown if no_teardown is not None else get_bool("BENCH_NO_TEARDOWN")
        # Resolved once so capabilities and scoring observe the same value.
        self.use_mcp: bool = get_bool("BENCH_USE_MCP", True)
        # When running concurrently with other benchmark processes, allocate a
        # free local port for the chaos port-forward instead of the fixed
        # default so two scenarios on one host do not contend for the same port.
        self.parallel: bool = get_bool("BENCH_PARALLEL", False)
        # Build the gated :class:`AgentConfig` once and hold the snapshot for
        # the lifetime of this harness, so every agent run and every record's
        # ``capabilities_granted`` field reads the same object.
        self._agent_config: AgentConfig = self._build_agent_config_snapshot()
        self.default_target_deployment = default_target_deployment
        self.default_namespace = default_namespace
        # Resolve the run-level placeholder inputs once into instance
        # attributes that ``replace_placeholders`` / ``start_scenario`` read.
        self.app_location = get_env("APP_LOCATION", "") or ""
        self.target_deployment = (
            get_env("TARGET_DEPLOYMENT_NAME", self.default_target_deployment)
            or self.default_target_deployment
        )
        self.namespace = get_env("NAMESPACE", self.default_namespace) or self.default_namespace
        self.reporter = reporter or ResultReporter(results_root)

    @property
    def _granted_skill_paths(self) -> tuple[str, ...]:
        """Skill paths the harness granted, derived from the config snapshot.

        Single source of truth: the same tuple lives on
        ``self._agent_config.capabilities.skills.paths`` and is read by every
        agent the harness constructs. Keeping it as a derived property (not a
        second copy) makes it structurally impossible for the recorded
        ``skills`` to disagree with what the agent saw.
        """
        return self._agent_config.capabilities.skills.paths

    # -- agent resolution (model/provider-agnostic) -----------------------

    def resolve_agent(self, agent_type: str) -> Any:
        """Resolve and instantiate the agent under test from the registry.

        The builtin agent modules are imported once so their
        ``@AGENTS.register`` decorators run, the alias is normalized to the
        canonical key, and the class is fetched from
        :data:`~devops_bench.agents.AGENTS`. An externally-registered agent
        resolves the same way with no harness edit.

        Args:
            agent_type: Configured agent type (e.g. ``gemini-cli`` / ``api`` /
                ``gemini`` / ``openclaw``).

        Returns:
            An instantiated agent harness. The instance is built with the
            harness-resolved :class:`AgentConfig` so capabilities (MCP / skills /
            rules) reflect the orchestrator's catalog × run-arm decision.

        Raises:
            NotRegisteredError: If no agent is registered under the resolved
                canonical key.
        """
        _ensure_builtin_agents_registered()
        key = _AGENT_TYPE_ALIASES.get(agent_type, agent_type)
        agent_cls = AGENTS.get(key)
        if agent_cls is None:
            raise NotRegisteredError(AGENTS.name, key, AGENTS.keys())
        return agent_cls(self.build_agent_config())

    # -- agent config + capabilities (explicit; no env detour) ------------

    def build_agent_config(self) -> AgentConfig:
        """Return the harness's snapshotted :class:`AgentConfig`.

        The config is built once in :meth:`__init__` and reused for every agent
        run plus every record's ``capabilities_granted`` field.

        Returns:
            The :class:`AgentConfig` snapshot. The same object is handed to
            every agent the harness constructs.
        """
        return self._agent_config

    def _build_agent_config_snapshot(self) -> AgentConfig:
        """Build the gated :class:`AgentConfig` from the env layer.

        Called exactly once, from :meth:`__init__`. Starts from
        :meth:`AgentConfig.from_env` so existing ``AGENT_*`` knobs continue
        to flow through (``model``, ``provider``, ``api_key``, ``target``,
        ``timeout``, ``max_turns``, ``extra_env``), then replaces
        capabilities with the orchestrator-owned aggregate so the agent
        cannot see a granted MCP binding when ``use_mcp`` is False.
        """
        base = AgentConfig.from_env()
        capabilities = self._gate_capabilities(base.capabilities, self.use_mcp)
        return AgentConfig(
            model=base.model,
            provider=base.provider,
            api_key=base.api_key,
            target=base.target,
            timeout_sec=base.timeout_sec,
            max_turns=base.max_turns,
            capabilities=capabilities,
            extra_env=base.extra_env,
        )

    @staticmethod
    def _gate_capabilities(env_caps: AllCapabilities, use_mcp: bool) -> AllCapabilities:
        """Apply the harness's ``use_mcp`` gate to an env-derived capability set.

        Skills and rules are independent of MCP and pass through unchanged;
        only the MCP binding is dropped when ``use_mcp`` is False. The
        returned aggregate is always a fresh frozen dataclass so the caller
        does not mutate the input.

        Args:
            env_caps: Capabilities derived from the ``AGENT_*`` env layer.
            use_mcp: Whether the orchestrator granted MCP for this run.

        Returns:
            The gated :class:`AllCapabilities` to attach to the next
            :class:`AgentConfig`.
        """
        if use_mcp:
            mcp_servers: tuple[McpBinding, ...] = env_caps.mcp_servers
        else:
            # MCP gated off: drop the binding so the agent's tools-enabled gate
            # is False and metrics' ``use_mcp`` agrees with what ran.
            mcp_servers = ()

        return AllCapabilities(
            mcp_servers=mcp_servers,
            skills=env_caps.skills if env_caps.skills.paths else SkillBinding(),
            rules=env_caps.rules if env_caps.rules.text else AgentRules(),
        )

    def _resolve_deployment_and_namespace(self, task: Task | None = None) -> tuple[str, str]:
        """Resolve the target deployment name and namespace.

        Precedence: env var → task variables → harness default.
        """
        infra_vars = {}
        if task and task.infrastructure:
            infra_vars = task.infrastructure.get("variables") or {}

        target_dep = (
            get_env("TARGET_DEPLOYMENT_NAME", "")
            or infra_vars.get("target_deployment_name", "")
            or self.target_deployment
        )
        ns = get_env("NAMESPACE", "") or infra_vars.get("namespace", "") or self.namespace
        return (
            str(target_dep) if target_dep is not None else "",
            str(ns) if ns is not None else "",
        )

    # -- placeholder substitution -----------------------------------------

    def replace_placeholders(
        self,
        text: str,
        cluster_name: str,
        target_deployment: str | None = None,
        namespace: str | None = None,
    ) -> str:
        """Substitute infrastructure placeholders in a prompt or expectation.

        ``TARGET_DEPLOYMENT_NAME`` and ``NAMESPACE`` form the integration
        contract supplied by the provisioning layer after cluster bring-up;
        their fallbacks come from the constructor's
        :attr:`default_target_deployment` / :attr:`default_namespace`.

        Args:
            text: Text containing ``{{...}}`` placeholders.
            cluster_name: Active cluster name to substitute.
            target_deployment: Optional target deployment name override.
            namespace: Optional namespace override.

        Returns:
            The text with all known placeholders replaced.
        """
        target_dep = target_deployment or self.target_deployment
        ns = namespace or self.namespace
        return (
            text.replace("{{PROJECT_ID}}", self.project_id)
            .replace("{{GCP_PROJECT_ID}}", self.project_id)
            .replace("{{CLUSTER_NAME}}", cluster_name)
            .replace("{{GKE_CLUSTER_NAME}}", cluster_name)
            .replace("{{APP_LOCATION}}", self.app_location)
            .replace("{{TARGET_DEPLOYMENT_NAME}}", target_dep)
            .replace("{{NAMESPACE}}", ns)
        )

    def _resolve_spec_placeholders(
        self,
        spec: Any,
        cluster_name: str,
        target_deployment: str | None = None,
        namespace: str | None = None,
    ) -> Any:
        """Walk a nested spec and substitute placeholders in every string leaf.

        Args:
            spec: An opaque chaos / verification spec value (mapping, list,
                scalar, or ``None``).
            cluster_name: Active cluster name passed through to
                :meth:`replace_placeholders`.
            target_deployment: Optional target deployment name override.
            namespace: Optional namespace override.

        Returns:
            A new structure with placeholders resolved. ``None`` round-trips
            unchanged so a missing spec stays missing.
        """
        if spec is None:
            return None
        if isinstance(spec, str):
            return self.replace_placeholders(spec, cluster_name, target_deployment, namespace)
        if isinstance(spec, list):
            return [
                self._resolve_spec_placeholders(item, cluster_name, target_deployment, namespace)
                for item in spec
            ]
        if isinstance(spec, dict):
            return {
                key: self._resolve_spec_placeholders(
                    value, cluster_name, target_deployment, namespace
                )
                for key, value in spec.items()
            }
        return spec

    # -- spec parsing (typed contracts at every seam) ---------------------

    def _parse_chaos_specs(
        self,
        raw: Any,
        cluster_name: str,
        target_deployment: str | None = None,
        namespace: str | None = None,
    ) -> list[ChaosSpec]:
        """Parse the raw task ``chaos_spec`` blob into typed :class:`ChaosSpec` list.

        Accepts either a JSON-in-YAML string or a native-YAML list. Each entry
        is placeholder-substituted, then validated through :class:`ChaosSpec`.
        """
        if not raw:
            return []
        resolved = self._resolve_spec_placeholders(raw, cluster_name, target_deployment, namespace)
        # A placeholder-substituted JSON string round-trips through
        # ``json.loads`` to a list/dict the discriminated union can validate.
        if isinstance(resolved, str):
            try:
                resolved = json.loads(resolved)
            except json.JSONDecodeError as exc:
                # A task that declares chaos but whose spec fails to parse must
                # fail loudly: silently dropping it would run the eval without the
                # intended disruption and score a quietly-invalid result.
                raise ConfigError(f"could not parse chaos_spec JSON string: {exc}") from exc
        entries = resolved if isinstance(resolved, list) else [resolved]
        return [ChaosSpec.model_validate(entry) for entry in entries if entry]

    def _build_verification_mapping(
        self,
        raw: Any,
        cluster_name: str,
        target_deployment: str | None = None,
        namespace: str | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, str]]]:
        """Build a name-keyed verification mapping the chaos seam consumes.

        Canonical authoring shape is a list of wrapped entries::

            verification_spec:
              - name: "Planned Load Spike Verification"
                spec:
                  type: parallel
                  checks: [...]

        ``name`` is the cross-reference key the chaos ``verify:`` field
        resolves against; ``spec`` carries the typed verification node. When no
        ``spec`` key is present the entry mapping itself is accepted as the
        node. Every authoring failure is **surfaced** — both warn-logged and
        accumulated into the returned ``errors`` list so the harness can
        record it on the run result, instead of silently dropping a
        verification (which would make a typo'd cross-reference invisible to
        the operator).

        Args:
            raw: The task's ``verification_spec`` blob.
            cluster_name: Active cluster name for placeholder substitution.
            target_deployment: Optional target deployment name override.
            namespace: Optional namespace override.

        Returns:
            A pair ``(mapping, errors)``:

            * ``mapping`` — ``{name -> parsed VerificationSpec}``.
            * ``errors`` — one ``{"name", "reason"}`` entry per authoring
              failure (a non-mapping entry, a missing ``name``, a JSON
              parse failure on a string blob, or a ``VerificationSpec``
              validation failure). Empty when every entry parsed.
        """
        if not raw:
            return {}, []

        errors: list[dict[str, str]] = []
        resolved = self._resolve_spec_placeholders(raw, cluster_name, target_deployment, namespace)
        if isinstance(resolved, str):
            try:
                resolved = json.loads(resolved)
            except json.JSONDecodeError as exc:
                _log.warning("could not parse verification_spec JSON string: %s", exc)
                errors.append({"name": "<root>", "reason": f"json parse: {exc}"})
                return {}, errors
        entries = resolved if isinstance(resolved, list) else [resolved]

        mapping: dict[str, Any] = {}
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                msg = (
                    f"verification_spec entry [{index}] must be a mapping, "
                    f"got {type(entry).__name__}"
                )
                _log.warning(msg)
                errors.append({"name": f"<index {index}>", "reason": msg})
                continue
            name = entry.get("name")
            if not name:
                msg = f"verification_spec entry [{index}] missing required ``name`` key"
                _log.warning(msg)
                errors.append({"name": f"<index {index}>", "reason": msg})
                continue
            if name in mapping:
                msg = (
                    f"verification_spec entry [{index}] has a duplicate name {name!r}; "
                    "names must be unique so a chaos ``verify`` reference is unambiguous"
                )
                _log.warning(msg)
                errors.append({"name": str(name), "reason": msg})
                continue
            # Canonical shape ``{name, spec: <typed-node>}``; an entry without
            # a ``spec`` key is itself treated as the typed node.
            node = entry.get("spec") if "spec" in entry else entry
            try:
                mapping[name] = VerificationSpec(node)
            except Exception as exc:  # noqa: BLE001 - surface every failure
                _log.warning(
                    "verification entry %r failed to validate; skipping: %s",
                    name,
                    exc,
                )
                errors.append({"name": str(name), "reason": str(exc)})
        return mapping, errors

    # -- scenario (background chaos) --------------------------------------

    def start_scenario(
        self,
        chaos_specs: list[ChaosSpec],
        verification_mapping: dict[str, Any],
        ctx: RunContext,
        target_deployment: str | None = None,
        namespace: str | None = None,
        *,
        skip_port_forward: bool = False,
    ) -> tuple[ScenarioManager, threading.Thread] | None:
        """Start a background chaos+verification scenario on a daemon thread.

        Args:
            chaos_specs: Typed chaos entries. Only the first spec is driven.
            verification_mapping: Name-keyed mapping of typed verification
                specs the chaos ``verify:`` key is resolved against.
            ctx: Per-task run context handed to triggers / faults.
            target_deployment: Optional resolved target deployment name.
            namespace: Optional resolved namespace.
            skip_port_forward: When True, do not open ``kubectl port-forward``;
                used by the E2E smoke harness when running against the
                :class:`~devops_bench.deployers.NoOpDeployer`.

        Returns:
            A ``(scenario_manager, thread)`` pair, or ``None`` when no chaos
            specs were provided.
        """
        if not chaos_specs:
            return None

        # Only the first spec is scheduled today; the field is a list to leave
        # room for multiple planned disruptions. Warn rather than silently drop
        # the rest so a task authored with several is not quietly under-run.
        if len(chaos_specs) > 1:
            _log.warning(
                "chaos_spec declares %d entries but only the first is scheduled; "
                "the remaining %d are ignored",
                len(chaos_specs),
                len(chaos_specs) - 1,
            )

        spec = chaos_specs[0]
        local_port = pick_free_port() if self.parallel else None
        target_dep = target_deployment or self.target_deployment
        ns = namespace or self.namespace
        scenario_manager = ScenarioManager(
            target_dep,
            ns,
            verification_mapping=verification_mapping,
            skip_port_forward=skip_port_forward,
            local_port=local_port,
        )
        thread = threading.Thread(
            target=scenario_manager.run_chaos_and_verification,
            args=(spec, ctx),
            daemon=True,
        )
        thread.start()
        return scenario_manager, thread

    # -- agent execution --------------------------------------------------

    def execute_agent(self, prompt: str, ctx: RunContext) -> AgentResult:
        """Run the configured agent against ``prompt`` through the registry.

        Args:
            prompt: The (placeholder-resolved) task prompt.
            ctx: The per-task run context. ``ctx.workspace_path`` is handed to
                the agent so a CLI wrapper executes in the harness-owned
                workspace instead of a throwaway directory the harness never
                inspects.

        Returns:
            The typed :class:`AgentResult` the agent emitted.
        """
        agent = self.resolve_agent(self.agent_type)
        return agent.run(prompt, workspace_path=ctx.workspace_path)

    # -- pipeline ---------------------------------------------------------

    def run(self, tasks: list[Task]) -> list[dict[str, Any]]:
        """Run the full pipeline over ``tasks`` and return scored results.

        Args:
            tasks: Typed :class:`Task` objects produced by
                :func:`~devops_bench.tasks.load_tasks`.

        Returns:
            The detailed per-task result dicts, scored in place, in the
            ``results.json`` schema.
        """
        run_dir = self.reporter.new_run_dir()
        detailed_results: list[dict[str, Any]] = [self._run_one(task, run_dir) for task in tasks]

        # Persist raw execution outputs before the (slower) scoring pass.
        self.reporter.write(run_dir, detailed_results)
        _log.info("execution complete; results saved to %s/results.json", run_dir)

        # Scoring is best-effort: a judge/config failure (e.g. get_judge_model()
        # or an unexpected error in a metric) must not sink an otherwise
        # successful execution pass, whose raw results are already on disk above.
        try:
            self._score(detailed_results)
            self.reporter.write(run_dir, detailed_results)
            _log.info(
                "post-processing evaluation complete; updated results saved to %s/results.json",
                run_dir,
            )
        except Exception:  # noqa: BLE001 - execution results must survive scoring errors
            _log.exception("scoring failed; returning unscored execution results from %s", run_dir)

        # Emit the flattened, ingest-ready rows + run manifest. Best-effort: the
        # detailed results.json is already on disk, so a failure here must not
        # sink the run.
        try:
            self._write_run_artifacts(run_dir, detailed_results)
        except Exception:  # noqa: BLE001 - rows/manifest are derived, never load-bearing
            _log.exception("failed to write rows.json/manifest.json for %s", run_dir)
        return detailed_results

    def _write_run_artifacts(self, run_dir: Path, detailed_results: list[dict[str, Any]]) -> None:
        """Flatten ``detailed_results`` into ``rows.json`` + ``manifest.json``.

        Assembles the run-level :class:`~devops_bench.results.Manifest` from the
        harness's resolved model / harness key / capabilities, flattens every
        record through :func:`~devops_bench.results.build_rows`, and writes both
        artifacts via the reporter.

        Args:
            run_dir: The run directory the artifacts are written under.
            detailed_results: The scored per-task records.
        """
        from devops_bench.results import (
            SCHEMA_VERSION,
            Manifest,
            build_rows,
            derive_augmentation,
        )
        from devops_bench.results import setup_id as results_setup_id

        augmentation = derive_augmentation(
            {"use_mcp": self.use_mcp, "skills": list(self._granted_skill_paths)}
        )
        model = self._agent_config.model or self._agent_config.provider or self.agent_type
        manifest = Manifest(
            schema_version=SCHEMA_VERSION,
            run_id=run_dir.name,
            t=datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            setup_id=results_setup_id(model, self.agent_type, augmentation),
            model=model,
            harness=self.agent_type,
            augmentation=augmentation,
        )
        rows = build_rows(detailed_results, manifest)
        self.reporter.write_rows(run_dir, [row.to_dict() for row in rows])
        self.reporter.write_manifest(run_dir, manifest.to_dict())

    def _run_one(self, task: Task, run_dir: Path) -> dict[str, Any]:
        """Provision, run the agent, collect artifacts, tear down for one task.

        Args:
            task: The typed task being evaluated.
            run_dir: The run output directory for generated artifacts.

        Returns:
            The detailed result dict. On any failure a ``status: "failed"``
            record is returned instead of being dropped, so failures stay
            visible to downstream parsers. Success and failed records carry
            the same top-level key set so a parser can iterate either shape
            without a ``KeyError``.
        """
        infra_config = task.infrastructure or {}
        if self.no_infra:
            # no_infra is implemented by forcing the noop deployer.
            infra_config = {**infra_config, "deployer": "noop"}
        deployer: Any | None = None
        scenario_manager: ScenarioManager | None = None
        scenario_thread: threading.Thread | None = None
        result: dict[str, Any] | None = None
        workspace_path: Path | None = None
        verification_parse_errors: list[dict[str, str]] = []
        # Track the substituted prompt / expectation as they are computed so a
        # failed record can carry the same resolved strings a success record
        # would, falling back to the raw task fields before substitution.
        prompt: str | None = None
        expected_output: str | None = None

        try:
            # Build the deployer inside the try so a factory failure (e.g. an
            # unknown deployer type) becomes a failed record for this task
            # rather than crashing the whole batch.
            deployer = get_deployer(infra_config, self.project_id, self.cluster_name)
            _log.info("provisioning infrastructure for: %s", task.name)
            deployer.up()
            cluster_info = deployer.get_cluster_info()
            active_cluster_name = cluster_info.name or self.cluster_name
            # Own a real per-run workspace so the artifact diff is rooted at
            # the directory the agent actually writes to (its CLI wrapper's
            # working directory), not the harness process's launch cwd.
            workspace_path = Path(tempfile.mkdtemp(prefix="devops-bench-workspace-"))
            context = self.make_context(task, cluster=cluster_info, workspace_path=workspace_path)

            target_dep, ns = self._resolve_deployment_and_namespace(task)

            prompt = self.replace_placeholders(task.prompt, active_cluster_name, target_dep, ns)

            chaos_specs = self._parse_chaos_specs(
                task.chaos_spec, active_cluster_name, target_dep, ns
            )
            verification_mapping, verification_parse_errors = self._build_verification_mapping(
                task.verification_spec, active_cluster_name, target_dep, ns
            )

            # Hand the background scenario its own context with an isolated
            # env dict so its in-thread env mutations never touch the context
            # the agent runs against.
            scenario = self.start_scenario(
                chaos_specs,
                verification_mapping,
                replace(context, env=dict(context.env)),
                target_deployment=target_dep,
                namespace=ns,
            )
            if scenario is not None:
                scenario_manager, scenario_thread = scenario
                _log.info("waiting for chaos agent to establish the cluster load spike...")
                chaos_active = scenario_manager.chaos_active_event.wait(
                    timeout=_CHAOS_ACTIVE_WAIT_SEC
                )
                if chaos_active:
                    _log.info("cluster load spike active; proceeding with operator agent...")
                else:
                    # The event is also set when injection fails (to unblock us), so
                    # a False here means it never signalled within the budget. The
                    # agent still runs, but flag it: the run may not reflect the
                    # intended disruption. The drained chaos_report carries the detail.
                    _log.warning(
                        "chaos did not signal active within %ss; proceeding, but the "
                        "run may not reflect the intended disruption",
                        _CHAOS_ACTIVE_WAIT_SEC,
                    )

            _log.info("executing agent for prompt: %s", prompt)
            before_files = snapshot_dir(workspace_path)
            agent_res = self.execute_agent(prompt, context)
            # NOTE/TODO: This collects ALL frontmatter from bootstrapping, not just generated files.
            # Consider a more targeted filter in a future iteration.
            # Best-effort: a collection failure (I/O, permissions, a bad link in the
            # workspace) must not turn an already-completed agent run into a failed,
            # unscored record, so isolate it like the other non-critical steps.
            try:
                collect_generated_files(before_files, run_dir, source_dir=workspace_path)
            except Exception:  # noqa: BLE001 - artifact collection must not sink a completed run
                _log.exception("artifact collection failed for %s; continuing", task.name)

            expected_output = self.replace_placeholders(
                task.expected_output, active_cluster_name, target_dep, ns
            )

            chaos_report, perf_report = self._drain_scenario(scenario_manager, scenario_thread)

            result = self._build_success_record(
                task=task,
                prompt=prompt,
                expected_output=expected_output,
                agent_res=agent_res,
                chaos_report=chaos_report,
                perf_report=perf_report,
                verification_parse_errors=verification_parse_errors,
            )
            _log.info("agent response for %s:\n%s", task.name, result["output"])
        except Exception as exc:  # noqa: BLE001 - surface every task failure
            _log.error("critical error during task %s: %s", task.name, exc)
            result = self._build_failed_record(
                task,
                exc,
                prompt=prompt,
                expected_output=expected_output,
                verification_parse_errors=verification_parse_errors,
            )
        finally:
            if scenario_manager is not None:
                scenario_manager.stop()
                # stop() only signals the abort flag; join the daemon thread with
                # a bounded timeout so teardown does not race a still-running
                # background scenario (the success path joins via _drain_scenario,
                # but the exception path reaches here without draining).
                if scenario_thread is not None:
                    scenario_thread.join(timeout=_SCENARIO_JOIN_SEC)
            if deployer is not None:
                self._teardown(deployer, infra_config, task.name)
            if workspace_path is not None:
                shutil.rmtree(workspace_path, ignore_errors=True)

        return result

    #: Top-level keys present on **every** record (success and failed alike).
    #: Exposed so downstream parsers / tests can pin the symmetric schema
    #: without reproducing the literal in every spot.
    _RECORD_KEYS: frozenset[str] = frozenset(
        {
            "input",
            "output",
            "latency",
            "tokens",
            "tools",
            "trajectory",
            "skills",
            "name",
            "folder",
            "status",
            "error",
            "errors",
            "scores",
            "expected_output",
            "expected_output_raw",
            "retrieval_context",
            "chaos_spec",
            "verification_spec",
            "chaos_report",
            "perf_report",
            "documentation",
            "capabilities_granted",
            "verification_parse_errors",
            "generation_only",
            "validated",
        }
    )

    def _build_success_record(
        self,
        *,
        task: Task,
        prompt: str,
        expected_output: str,
        agent_res: AgentResult,
        chaos_report: dict[str, Any],
        perf_report: dict[str, Any],
        verification_parse_errors: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Shape a typed :class:`AgentResult` + reports into the on-disk schema.

        Routes every typed value through ``to_dict()`` / ``model_dump()`` and
        emits the **symmetric** key union (every key in :attr:`_RECORD_KEYS`
        is present on every record), so success and failed records never differ
        in top-level shape — a downstream parser iterating one shape can never
        ``KeyError`` crossing into the other.

        Capability metadata (``capabilities_granted``) is recorded so metrics
        / downstream consumers can read what the agent was actually granted
        rather than re-reading ``BENCH_USE_MCP``.
        """
        dumped = agent_res.to_dict()
        agent_errors = list(dumped.get("errors") or [])
        record = self._empty_record(task)
        record.update(
            {
                "input": prompt,
                "output": dumped.get("output", ""),
                "latency": dumped.get("latency", 0.0),
                "tokens": dumped.get("tokens", {}),
                # Expose a flat ``tools`` key alongside the typed trajectory
                # for consumers that only sample tool names; the trajectory is
                # the source of truth.
                "tools": [
                    entry.get("name") for entry in dumped.get("trajectory", []) if entry.get("name")
                ],
                "trajectory": dumped.get("trajectory", []),
                "status": "success",
                # Run-level validity gate: a vetted task only promotes to the
                # leaderboard when this run actually produced a usable result.
                # ``AgentResult.errored()`` (429 / SDK fault / agent timeout)
                # yields populated ``errors`` + an empty trajectory while the
                # record still reads ``status:"success"``, so gating on the task
                # flag alone would let an empty/errored run pass as a genuine low
                # score. Require no agent error *and* a non-empty trajectory.
                "validated": (
                    task.validated and not agent_errors and bool(dumped.get("trajectory"))
                ),
                "errors": agent_errors,
                # First-error scalar so a parser reading ``error`` finds the
                # same key on the success shape (None when nothing went wrong).
                "error": agent_errors[0] if agent_errors else None,
                "expected_output": expected_output,
                "chaos_report": chaos_report,
                "perf_report": perf_report,
                "verification_parse_errors": list(verification_parse_errors or []),
            }
        )
        return record

    def _build_failed_record(
        self,
        task: Task,
        exc: Exception,
        *,
        prompt: str | None = None,
        expected_output: str | None = None,
        verification_parse_errors: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Build a failed-task record so the failure stays visible.

        Emits the **same** top-level key set as :meth:`_build_success_record`
        (pinned in :attr:`_RECORD_KEYS`): a downstream parser iterating either
        shape never trips a ``KeyError`` crossing between them. The differences
        are values only — ``status=\"failed\"``, ``error`` carries the
        exception text, ``scores`` stays empty.

        Args:
            task: The task that failed.
            exc: The exception that aborted the run.
            prompt: The placeholder-substituted prompt if it was computed before
                the failure; falls back to the raw ``task.prompt`` otherwise, so
                the record matches the success shape when substitution had run.
            expected_output: The substituted expectation if computed; falls back
                to the raw ``task.expected_output``.
            verification_parse_errors: Any spec-parse errors collected so far.
        """
        error_text = str(exc)
        record = self._empty_record(task)
        record.update(
            {
                "input": prompt if prompt is not None else task.prompt,
                "expected_output": (
                    expected_output if expected_output is not None else task.expected_output
                ),
                "status": "failed",
                "error": error_text,
                "errors": [error_text],
                # A failed run never promotes, even on a vetted task.
                "validated": False,
                "verification_parse_errors": list(verification_parse_errors or []),
            }
        )
        return record

    def _empty_record(self, task: Task) -> dict[str, Any]:
        """Seed every record with the symmetric key set.

        Centralizes the default values for the keys that match across
        success/failed records (task identifying fields, opaque blobs, empty
        containers for ``scores`` / ``tools`` / ``trajectory`` etc.). Both
        builder methods overlay the differing keys on top of this seed; the
        seed itself never contains a ``status`` value so the caller must set
        it explicitly.
        """
        return {
            "input": task.prompt,
            "output": "",
            "latency": 0.0,
            "tokens": {},
            "tools": [],
            "trajectory": [],
            "skills": list(self._granted_skill_paths),
            "name": task.name,
            "folder": task.folder,
            "status": "",
            "error": None,
            "errors": [],
            # ``scores`` (the per-metric mapping) is populated by ``_score`` for
            # success records; failed records leave it as the empty dict so the
            # key is always present. There is no aggregate scalar score: the
            # per-metric map is the source of truth.
            "scores": {},
            "expected_output": "",
            "expected_output_raw": task.expected_output,
            "retrieval_context": list(task.retrieval_context),
            "chaos_spec": task.chaos_spec,
            "verification_spec": task.verification_spec,
            "chaos_report": {},
            "perf_report": {},
            "documentation": [doc.model_dump() for doc in task.documentation],
            "capabilities_granted": {
                "use_mcp": self.use_mcp,
                "skills": list(self._granted_skill_paths),
            },
            "verification_parse_errors": [],
            # Generation-only tasks have no cluster, so the OutcomeValidity judge
            # must not penalize them for "not applying". This holds both when the
            # task declares ``deployer: noop`` and when ``BENCH_NO_INFRA`` skips
            # provisioning for the whole run (mirrors get_deployer's own gate).
            "generation_only": self.no_infra
            or (task.infrastructure or {}).get("deployer") == "noop",
            # Only tasks vetted as correct promote to the leaderboard; downstream
            # ingest gates inclusion on this flag (default False until vetted).
            "validated": task.validated,
        }

    def _drain_scenario(
        self,
        scenario_manager: ScenarioManager | None,
        scenario_thread: threading.Thread | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Join the scenario thread and return its chaos and perf reports.

        If the join times out (i.e. ``thread.is_alive()`` after the budget),
        a warning is logged and the returned ``chaos_report["status"]`` is
        stamped to ``"timed_out"`` so a partial report is flagged on the
        record rather than silently mislabelled as the last status the
        scenario reached before the cutoff.

        Args:
            scenario_manager: The running scenario, or None.
            scenario_thread: The scenario's daemon thread, or None.

        Returns:
            A ``(chaos_report, perf_report)`` pair; both empty when no chaos
            was scheduled for the task.
        """
        if scenario_manager is None or scenario_thread is None:
            return {}, {}
        _log.info("waiting for background metrics collection to complete...")
        scenario_thread.join(timeout=_SCENARIO_JOIN_SEC)
        chaos_report, perf_report = scenario_manager.get_reports()
        if scenario_thread.is_alive():
            _log.warning(
                "scenario thread still alive after %ss join budget; "
                "stamping chaos_report.status='timed_out'",
                _SCENARIO_JOIN_SEC,
            )
            # get_reports() already handed back a locked deep copy, so this
            # snapshot is private and safe to stamp even though the daemon thread
            # is still writing. It preserves any partial fields populated before
            # the cutoff (injected_fault / name / output) so the operator sees
            # how far it got.
            chaos_report["status"] = "timed_out"
        return chaos_report, perf_report

    def _teardown(self, deployer: Any, infra_config: dict[str, Any], name: str) -> None:
        """Tear down infrastructure unless disabled by config or env.

        Args:
            deployer: The deployer to tear down.
            infra_config: Task infrastructure config (``teardown`` flag).
            name: Task name, for logging.
        """
        if self.no_teardown:
            return
        if not infra_config.get("teardown", True):
            return
        _log.info("tearing down infrastructure for: %s", name)
        try:
            deployer.down()
        except Exception as exc:  # noqa: BLE001 - never raise during teardown
            _log.error("teardown failed (potential resource leak): %s", exc)

    def _score(self, detailed_results: list[dict[str, Any]]) -> None:
        """Score the batch in place via the metrics pipeline.

        The harness threads its single resolved ``use_mcp`` boolean into the
        metrics call, so the agent and the judge cannot disagree on whether
        tools were enabled.

        Args:
            detailed_results: Execution results to score; ``scores`` is written
                into each in place. Records marked ``status: "failed"`` are
                skipped, since there is no agent output to judge.
        """
        scorable = [r for r in detailed_results if r.get("status") != "failed"]
        if not scorable:
            return
        # Lazy import keeps ``deepeval`` / provider SDKs out of harness import.
        from devops_bench.metrics import evaluate_metrics_batch, get_judge_model

        judge_model = self._judge_model or get_judge_model()
        evaluate_metrics_batch(scorable, judge_model, use_mcp=self.use_mcp)
