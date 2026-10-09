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
import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from devops_bench.agents import AGENTS, AgentConfig, AgentResult
from devops_bench.agents import sandbox as agent_sandbox
from devops_bench.agents.capabilities import (
    AgentRules,
    AllCapabilities,
    McpBinding,
    SkillBinding,
)
from devops_bench.chaos import ChaosSpec
from devops_bench.cheat_detection import (
    DEFAULT_BASELINE,
    SensitiveAccessRule,
    annotate_records,
    baseline_from_granted_paths,
    build_inventory_rules,
    build_mount_rules,
    filter_rules_for_prompt,
    load_ruleset,
)
from devops_bench.core import (
    ClusterInfo,
    ConfigError,
    MissingDependencyError,
    NotRegisteredError,
    RunContext,
    SandboxError,
    get_bool,
    get_env,
    get_logger,
)
from devops_bench.deployers.factory import get_deployer
from devops_bench.evalharness.artifacts import collect_generated_files, snapshot_dir
from devops_bench.evalharness.base import Harness
from devops_bench.evalharness.hold import (
    HoldObservation,
    SafeguardMonitor,
    hold_verdict,
    run_hold_window,
)
from devops_bench.evalharness.reporter import ResultReporter
from devops_bench.evalharness.scenario import (
    VERIFICATION_TIMEOUT_SEC,
    ScenarioManager,
    pick_free_port,
    verification_budget_sec,
)
from devops_bench.k8s import agent_credentials
from devops_bench.tasks import Task
from devops_bench.verification import (
    MIN_LEAF_BUDGET_SECONDS,
    VerificationEntry,
    VerifierAgent,
    parse_entries,
)
from devops_bench.verification.hold_defaults import effective_poll_interval

if TYPE_CHECKING:
    from devops_bench.providers.base import Provider

__all__ = ["DefaultEvalHarness"]

_log = get_logger("evalharness.default")

# Imported at call time so their ``@AGENTS.register`` decorators run; external
# packages register with the same registry.
_BUILTIN_AGENT_MODULES: tuple[str, ...] = (
    "devops_bench.agents.cli.gemini_cli",
    "devops_bench.agents.cli.claude_code",
    "devops_bench.agents.cli.openclaw",
    "devops_bench.agents.cli.antigravity",
    "devops_bench.agents.api.agent",
    "devops_bench.agents.adk.agent",
)

# Aliases normalized to canonical agent keys before registry lookup.
_AGENT_TYPE_ALIASES: dict[str, str] = {
    "gemini-cli": "gemini",
    "claude-code": "claude",
}

# Default agent type when neither --agent-type nor BENCH_AGENT_TYPE is set.
_DEFAULT_AGENT_TYPE = "gemini-cli"

# Defaults shared by prompt placeholders and the chaos port-forward target, so the
# agent and the injector address the same workload when env is unset.
_DEFAULT_TARGET_DEPLOYMENT = "hypercomputer-d1-frontend"
_DEFAULT_NAMESPACE = "default"

# Wait for the chaos agent to establish its load spike before starting the operator agent.
_CHAOS_ACTIVE_WAIT_SEC = 45

# Scenario-thread drain budget; above the verification budget so a slow but
# completing verification is not cut off into a partial report racing teardown.
_SCENARIO_JOIN_SEC = VERIFICATION_TIMEOUT_SEC + 60


def _ensure_builtin_agents_registered() -> None:
    """Import the builtin agent modules so their registrations fire (re-imports are no-ops).

    Only missing-dependency / import errors are swallowed (an optional SDK may be
    absent); a real bug in an agent module re-raises rather than hiding in a debug log.
    """
    for module in _BUILTIN_AGENT_MODULES:
        try:
            importlib.import_module(module)
        except (ImportError, MissingDependencyError) as exc:
            # ``AGENTS.get`` still raises NotRegisteredError if this agent is selected.
            _log.debug("optional agent module %s not importable: %s", module, exc)


def _canonical_agent_type(agent_type: str) -> str:
    """Normalize an agent-type alias to its canonical registry key.

    Used for both registry lookup and result recording, so an alias aggregates
    under the same ``harness`` / ``setup_id`` as the canonical key.
    """
    return _AGENT_TYPE_ALIASES.get(agent_type, agent_type)


def _entry_display_fields(entry: VerificationEntry) -> dict[str, Any]:
    """Display fields snapshotted from an entry onto its report item.

    Undeclared fields land as ``None`` (unlike task-level fields, which default
    to ``""``); the row normalizer maps both to ``""``.
    """
    return {
        "title": entry.title,
        "description": entry.description,
        "group": entry.group,
        "failure_hint": entry.failure_hint,
    }


def _task_metadata(task: Task) -> dict[str, Any]:
    """The task-level display metadata snapshotted onto every record."""
    return {
        "title": task.title,
        "summary": task.summary,
        "category": task.category,
        "tags": list(task.tags),
        "check_groups": {key: group.model_dump() for key, group in task.check_groups.items()},
    }


class DefaultEvalHarness(Harness):
    """Standard harness wiring every component into one pipeline.

    Each task flows through provisioning, optional background chaos, agent
    execution, artifact collection, teardown, and batch scoring, each layer
    consumed through its typed contract and serialized so the on-disk
    ``results.json`` schema stays stable.

    Args:
        project_id: Default cloud project id for provisioning and placeholders.
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
        # Off by default: the flag alone adds the mcp token to setup_id, moving the row to another arm.
        self.use_mcp: bool = get_bool("BENCH_USE_MCP", False)
        # Cheating detection writes ``cheating_report`` (read by IntegrityMetric,
        # which gates a flagged run to zero). Rules load here so a bad path fails loud.
        self.cheat_detect: bool = get_bool("BENCH_CHEAT_DETECT", True)
        self.cheat_rules_path: str | None = get_env("BENCH_CHEAT_RULES")
        self._cheat_rules: tuple[SensitiveAccessRule, ...] = (
            load_ruleset(self.cheat_rules_path) if self.cheat_detect else ()
        )
        # Also snapshot the agent home and flag access to prior-run leftovers.
        self.cheat_inventory: bool = get_bool("BENCH_CHEAT_INVENTORY", True)
        # Concurrent processes on one host get a free chaos port-forward port each.
        self.parallel: bool = get_bool("BENCH_PARALLEL", False)
        # Built once so every agent run and every record's ``capabilities_granted``
        # read the same snapshot.
        self._agent_config: AgentConfig = self._build_agent_config_snapshot()
        self.default_target_deployment = default_target_deployment
        self.default_namespace = default_namespace
        # Run-level placeholder inputs, read by replace_placeholders / start_scenario.
        self.app_location = get_env("APP_LOCATION", "") or ""
        self.target_deployment = (
            get_env("TARGET_DEPLOYMENT_NAME", self.default_target_deployment)
            or self.default_target_deployment
        )
        self.namespace = get_env("NAMESPACE", self.default_namespace) or self.default_namespace
        self.reporter = reporter or ResultReporter(results_root)

    @property
    def _granted_skill_paths(self) -> tuple[str, ...]:
        """Skill paths the harness granted, derived (not copied) from the config snapshot."""
        return self._agent_config.capabilities.skills.paths

    # -- agent resolution (model/provider-agnostic) -----------------------

    def resolve_agent(
        self,
        agent_type: str,
        sandbox_spec: agent_sandbox.SandboxSpec | None = None,
        *,
        sandbox_exempt: bool = False,
    ) -> Any:
        """Resolve and instantiate the agent under test from the registry.

        Args:
            agent_type: Configured agent type or alias.

        Returns:
            An agent built with the harness-resolved :class:`AgentConfig`.

        Raises:
            NotRegisteredError: If nothing is registered under the canonical key.
        """
        _ensure_builtin_agents_registered()
        key = _canonical_agent_type(agent_type)
        agent_cls = AGENTS.get(key)
        if agent_cls is None:
            raise NotRegisteredError(AGENTS.name, key, AGENTS.keys())
        return agent_cls(self.build_agent_config(sandbox_spec, sandbox_exempt=sandbox_exempt))

    # -- agent config + capabilities (explicit; no env detour) ------------

    def build_agent_config(
        self,
        sandbox_spec: agent_sandbox.SandboxSpec | None = None,
        *,
        sandbox_exempt: bool = False,
    ) -> AgentConfig:
        """Return the snapshotted :class:`AgentConfig`, built once in ``__init__``.

        ``sandbox_spec`` (the task-completed spec ``_run_one`` prepared)
        replaces the snapshot's skeletal ``sandbox`` field for that one
        agent; ``sandbox_exempt`` (a ``requires_unsandboxed`` task) clears
        it instead. Passing both is a caller bug and raises.
        """
        if sandbox_exempt and sandbox_spec is not None:
            raise ValueError("sandbox_exempt and sandbox_spec are mutually exclusive")
        if sandbox_exempt:
            # Clear the field rather than leave the skeletal spec: the agent's
            # own gate reads ``config.sandbox is not None``.
            return replace(self._agent_config, sandbox=None)
        if sandbox_spec is not None:
            return replace(self._agent_config, sandbox=sandbox_spec)
        return self._agent_config

    def _build_agent_config_snapshot(self) -> AgentConfig:
        """Build the gated :class:`AgentConfig` from the env layer, once, in ``__init__``.

        Starts from :meth:`AgentConfig.from_env` and replaces capabilities with
        the gated aggregate, so a granted MCP binding is invisible when ``use_mcp`` is False.
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
            # Rebuilding field-by-field silently drops any field not named here.
            extra_flags=base.extra_flags,
            sandbox=base.sandbox,
        )

    @staticmethod
    def _gate_capabilities(env_caps: AllCapabilities, use_mcp: bool) -> AllCapabilities:
        """Apply the ``use_mcp`` gate to an env-derived capability set.

        Only the MCP binding is dropped when ``use_mcp`` is False; skills and
        rules pass through. Always returns a fresh aggregate.

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
            # Dropped so the agent's tools gate and the metrics' ``use_mcp`` agree.
            mcp_servers = ()
            if env_caps.mcp_servers:
                _log.warning(
                    "AGENT_MCP_SERVER is set but BENCH_USE_MCP is off; "
                    "running the baseline arm without MCP"
                )

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
            .replace("{{CLUSTER_NAME}}", cluster_name)
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
        """Substitute placeholders in every string leaf of a raw spec, before it is parsed.

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
        # A JSON string round-trips through json.loads to a validatable list/dict.
        if isinstance(resolved, str):
            try:
                resolved = json.loads(resolved)
            except json.JSONDecodeError as exc:
                # Fail loud: dropping it would score a run without the intended disruption.
                raise ConfigError(f"could not parse chaos_spec JSON string: {exc}") from exc
        entries = resolved if isinstance(resolved, list) else [resolved]
        return [ChaosSpec.model_validate(entry) for entry in entries if entry]

    def _run_verification(
        self,
        entries: list[VerificationEntry],
        timeout_sec: float = VERIFICATION_TIMEOUT_SEC,
        *,
        hold_observations: dict[str, HoldObservation] | None = None,
        total_budget_sec: float | None = None,
    ) -> list[dict[str, Any]]:
        """Evaluate every entry against the live cluster after the agent finishes.

        Every entry runs; one that raises is recorded as a failure and the rest
        continue. Converging entries and objective soaks draw on one budget
        sized by :func:`~devops_bench.evalharness.scenario.verification_budget_sec`:
        each is granted ``min(its cap, budget left)`` and charged only the time
        it actually spent, up to that grant, so overruns past a cap and harness
        work between entries never starve a later entry. One granted under
        :data:`MIN_LEAF_BUDGET_SECONDS` is recorded as budget-exhausted. Assert
        entries cost nothing. Safeguard hold entries take their outcome from
        ``hold_observations`` (sampled during the agent's turn); objective hold
        entries are soaked here, last. A hold with zero samples is an error,
        never a pass.

        Args:
            entries: The task's parsed verification entries.
            timeout_sec: Per-entry budget for converging entries.
            hold_observations: Name-keyed safeguard-hold observations; ``None``
                or a missing name counts as zero samples.
            total_budget_sec: Overrides the spec-sized total budget.

        Returns:
            One mapping per entry, in declaration order, in the shape
            :func:`devops_bench.verification.rollup.rollup` consumes.
        """
        agent = VerifierAgent()
        report: list[dict[str, Any]] = []
        budget_left = (
            verification_budget_sec(entries, timeout_sec)
            if total_budget_sec is None
            else total_budget_sec
        )
        hold_observations = hold_observations or {}

        # Objective holds soak last so converging entries claim the budget first.
        rows: list[dict[str, Any] | None] = [None] * len(entries)
        objective_holds: list[int] = []

        for index, entry in enumerate(entries):
            if entry.resolved_mode == "hold" and entry.role == "safeguard":
                rows[index] = self._hold_report_entry(entry, hold_observations.get(entry.name))
                continue
            if entry.resolved_mode == "hold" and entry.role == "objective":
                objective_holds.append(index)
                continue
            if entry.resolved_mode == "assert":
                rows[index] = self._evaluate_entry(agent, entry, timeout_sec)
                continue
            granted = min(timeout_sec, budget_left)
            started = time.monotonic()
            rows[index] = self._evaluate_entry(agent, entry, granted)
            budget_left -= min(granted, time.monotonic() - started)

        for index in objective_holds:
            entry = entries[index]
            # Required by VerificationEntry validation; missing here is a validation bug.
            if entry.hold_window_sec is None:
                raise ValueError(
                    f"objective hold entry {entry.name!r} reached verification without "
                    "hold_window_sec set; this should have been rejected at "
                    "spec-validation time"
                )
            granted = min(entry.hold_window_sec, budget_left)
            started = time.monotonic()
            obs = run_hold_window(
                entry,
                entry.hold_window_sec,
                interval_sec=effective_poll_interval(entry.hold_poll_interval_sec),
                deadline=started + granted,
            )
            rows[index] = self._hold_report_entry(entry, obs)
            budget_left -= min(granted, time.monotonic() - started)

        report.extend(row for row in rows if row is not None)
        return report

    def _evaluate_entry(
        self,
        agent: VerifierAgent,
        entry: VerificationEntry,
        timeout_sec: float,
    ) -> dict[str, Any]:
        """Evaluate one converge or assert entry within its granted ``timeout_sec``."""
        if entry.resolved_mode != "assert" and timeout_sec < MIN_LEAF_BUDGET_SECONDS:
            # Never evaluated, not a condition observed false.
            return {
                "name": entry.name,
                **_entry_display_fields(entry),
                "role": entry.role,
                "severity": entry.severity,
                "weight": entry.weight,
                "mode": entry.resolved_mode,
                "success": False,
                "status": "error",
                "reason": "verification total budget exhausted before evaluation",
                "elapsed_time": 0.0,
                "children": [],
            }

        try:
            result = agent.run_entry(entry, timeout_sec=timeout_sec)
            success = result.success
            status = result.status
            reason = result.reason
            elapsed = result.elapsed_time
            children = [child.model_dump() for child in result.children]
        except Exception as exc:  # noqa: BLE001 - one entry must not abort the rest
            _log.exception("verification entry %r failed to evaluate", entry.name)
            success, status, reason, elapsed, children = (
                False,
                "error",
                f"evaluation error: {exc}",
                0.0,
                [],
            )

        return {
            "name": entry.name,
            **_entry_display_fields(entry),
            "role": entry.role,
            "severity": entry.severity,
            "weight": entry.weight,
            "mode": entry.resolved_mode,
            "success": success,
            "status": status,
            "reason": reason,
            "elapsed_time": elapsed,
            "children": children,
        }

    @staticmethod
    def _hold_report_entry(entry: VerificationEntry, obs: HoldObservation | None) -> dict[str, Any]:
        """Build one hold entry's report row from its driver's observation.

        The verdict comes from :func:`~devops_bench.evalharness.hold.hold_verdict`
        so both hold drivers are scored by one rule; ``obs is None`` counts as a
        zero-sample observation.

        Args:
            entry: The hold-mode entry being reported.
            obs: The driver's observation, or ``None`` when the name was missing.

        Returns:
            The rollup-shaped row plus the ``hold_*`` fields that make the
            outcome auditable from the report alone.
        """
        success, status, reason = hold_verdict(obs if obs is not None else HoldObservation())

        return {
            "name": entry.name,
            **_entry_display_fields(entry),
            "role": entry.role,
            "severity": entry.severity,
            "weight": entry.weight,
            "mode": entry.resolved_mode,
            "success": success,
            "status": status,
            "reason": reason,
            "elapsed_time": obs.observed_window_sec if obs is not None else 0.0,
            "children": [],
            "hold_observed_window_sec": obs.observed_window_sec if obs is not None else 0.0,
            "hold_sample_count": obs.sample_count if obs is not None else 0,
            "hold_error_count": obs.error_count if obs is not None else 0,
            "hold_first_violation_reason": obs.first_violation_reason if obs is not None else None,
            "hold_first_violation_at_sec": obs.first_violation_at_sec if obs is not None else None,
        }

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

        # Only the first spec is scheduled; warn so extra entries are not silently dropped.
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

    def execute_agent(
        self,
        prompt: str,
        ctx: RunContext,
        sandbox_spec: agent_sandbox.SandboxSpec | None = None,
        *,
        sandbox_exempt: bool = False,
    ) -> AgentResult:
        """Run the configured agent against ``prompt`` through the registry.

        ``ctx.workspace_path`` becomes the agent's working directory;
        ``sandbox_spec``, when given, is the task-completed sandbox spec the
        agent runs under, and ``sandbox_exempt`` runs a ``requires_unsandboxed``
        task outside the boundary.
        """
        agent = self.resolve_agent(self.agent_type, sandbox_spec, sandbox_exempt=sandbox_exempt)
        return agent.run(prompt, workspace_path=ctx.workspace_path)

    # -- pipeline ---------------------------------------------------------

    def _inventory_home(
        self, *, fingerprint_only: frozenset[str] | None = None
    ) -> tuple[SensitiveAccessRule, ...]:
        """Snapshot the agent home into prior-run-artifact rules.

        Best-effort: a failure logs and yields nothing, as does either
        cheat-detection toggle being off.

        Args:
            fingerprint_only: Entry names still allowed to produce content rules.

        Returns:
            The generated ruleset, empty on failure or when disabled.
        """
        if not (self.cheat_detect and self.cheat_inventory):
            return ()
        try:
            home = Path.home()
            # Granted skills are material the agent is told to read, not leftovers.
            return build_inventory_rules(
                home,
                baseline=DEFAULT_BASELINE
                | baseline_from_granted_paths(home, self._granted_skill_paths),
                fingerprint_only=fingerprint_only,
            )
        except Exception:  # noqa: BLE001 - detection must never block execution
            _log.exception("home inventory failed; static cheat rules only")
            return ()

    def run(self, tasks: list[Task]) -> list[dict[str, Any]]:
        """Run the full pipeline over ``tasks`` and return scored results.

        Args:
            tasks: Typed :class:`Task` objects produced by
                :func:`~devops_bench.tasks.load_tasks`.

        Returns:
            The detailed per-task result dicts, scored in place, in the
            ``results.json`` schema.
        """
        sandboxed = self._agent_config.sandbox is not None
        if sandboxed and any(not task.requires_unsandboxed for task in tasks):
            # Fail before any cluster exists rather than once per provisioned task;
            # a batch of only exempt tasks never needs the seam.
            _ensure_builtin_agents_registered()
            agent_cls = AGENTS.get(_canonical_agent_type(self.agent_type))
            if agent_cls is not None and not getattr(agent_cls, "supports_sandbox", False):
                raise SandboxError(
                    f"agent harness {agent_cls.__name__} has not been migrated onto "
                    "the sandbox seam; refusing the whole batch rather than "
                    "provisioning a cluster per task just to fail each one"
                )
        if sandboxed:
            # Reap containers a killed harness never got to reap, before this
            # batch's own exist; the sweep itself applies the BENCH_PARALLEL gate.
            try:
                agent_sandbox.sweep_stray_containers(
                    owner=self._agent_config.sandbox.owner, parallel=self.parallel
                )
            except Exception:  # noqa: BLE001 - a sweep failure must not block the run
                _log.exception("stray sandbox container sweep failed; continuing")

        run_dir = self.reporter.new_run_dir()

        # Entries that predate the batch may always fingerprint. Skipped unless some
        # task runs ambient (a ``requires_unsandboxed`` task does, even in a sandboxed batch).
        pre_existing: frozenset[str] = frozenset()
        if not sandboxed or any(task.requires_unsandboxed for task in tasks):
            pre_existing = frozenset(rule.source for rule in self._inventory_home() if rule.source)

        # One inventory per task iteration, paired positionally with ``detailed_results``.
        # Ambient rules are scanned before each run; a sandboxed task's come back from _run_one.
        created_by: dict[str, str] = {}
        prev_task_name: str | None = None
        task_inventories: list[tuple[SensitiveAccessRule, ...]] = []
        detailed_results: list[dict[str, Any]] = []
        for task in tasks:
            task_sandboxed = sandboxed and not task.requires_unsandboxed
            ambient_rules: tuple[SensitiveAccessRule, ...] = ()
            if not task_sandboxed:
                ambient_rules = self._ambient_inventory_rules(
                    task.name, pre_existing, created_by, prev_task_name
                )
            record, sandbox_rules = self._run_one(task, run_dir)
            task_inventories.append(sandbox_rules if task_sandboxed else ambient_rules)
            detailed_results.append(record)
            prev_task_name = task.name

        # Annotated before the first write so both results.json copies carry the
        # report and ``_score`` can read it. A detector failure leaves that record ungated.
        if self.cheat_detect:
            # A home entry the prompt itself names is authorized for that record;
            # content fingerprints always apply.
            for record, inventory_rules in zip(detailed_results, task_inventories, strict=True):
                try:
                    annotate_records(
                        [record],
                        self._cheat_rules
                        + filter_rules_for_prompt(inventory_rules, record.get("input") or ""),
                    )
                except Exception:  # noqa: BLE001 - detection must never sink a completed run
                    _log.exception(
                        "cheating detection failed for %r; record keeps empty cheating_report",
                        record.get("name"),
                    )

        # Persist raw execution outputs before the (slower) scoring pass.
        self.reporter.write(run_dir, detailed_results)
        _log.info("execution complete; results saved to %s/results.json", run_dir)

        # Best-effort: a judge or metric failure must not sink the execution pass.
        try:
            self._score(detailed_results)
            self.reporter.write(run_dir, detailed_results)
            _log.info(
                "post-processing evaluation complete; updated results saved to %s/results.json",
                run_dir,
            )
        except Exception:  # noqa: BLE001 - execution results must survive scoring errors
            _log.exception("scoring failed; returning unscored execution results from %s", run_dir)

        # Flattened rows + manifest are derived; a failure here must not sink the run.
        try:
            self._write_run_artifacts(run_dir, detailed_results)
        except Exception:  # noqa: BLE001 - rows/manifest are derived, never load-bearing
            _log.exception("failed to write rows.json/manifest.json for %s", run_dir)
        return detailed_results

    def _write_run_artifacts(self, run_dir: Path, detailed_results: list[dict[str, Any]]) -> None:
        """Flatten ``detailed_results`` into ``rows.json`` + ``manifest.json``.

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
        # Canonical key, so an alias aggregates with it instead of as a second setup.
        harness = _canonical_agent_type(self.agent_type)
        model = self._agent_config.model or self._agent_config.provider or harness
        manifest = Manifest(
            schema_version=SCHEMA_VERSION,
            run_id=run_dir.name,
            t=datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            setup_id=results_setup_id(model, harness, augmentation),
            model=model,
            harness=harness,
            augmentation=augmentation,
        )
        rows = build_rows(detailed_results, manifest)
        self.reporter.write_rows(run_dir, [row.to_dict() for row in rows])
        self.reporter.write_manifest(run_dir, manifest.to_dict())

    def _ambient_inventory_rules(
        self,
        task_name: str,
        pre_existing: frozenset[str],
        created_by: dict[str, str],
        prev_task_name: str | None,
    ) -> tuple[SensitiveAccessRule, ...]:
        """Pre-task inventory of the operator home for one ambient iteration.

        Mid-batch entries are attributed to the task that created them
        (``created_by``, mutated here) and fingerprint only for other task
        names, so an honest repeat is not flagged for its own deliverable.
        """
        if prev_task_name is not None:
            # Empty fingerprint_only skips every file read: a bare listing.
            current = {
                rule.source
                for rule in self._inventory_home(fingerprint_only=frozenset())
                if rule.source
            }
            for name in current - pre_existing - created_by.keys():
                created_by[name] = prev_task_name
        fingerprintable = pre_existing | frozenset(
            name for name, creator in created_by.items() if creator != task_name
        )
        rules = self._inventory_home(fingerprint_only=fingerprintable)
        appeared = {rule.source for rule in rules if rule.source} - pre_existing
        if appeared:
            _log.info(
                "cheat detection: %d home entr(ies) appeared during this batch and "
                "are covered for %s: %s",
                len(appeared),
                task_name,
                ", ".join(sorted(appeared)),
            )
        return rules

    def _run_one(
        self, task: Task, run_dir: Path
    ) -> tuple[dict[str, Any], tuple[SensitiveAccessRule, ...]]:
        """Provision, run the agent, collect artifacts, tear down for one task.

        Returns:
            ``(record, sandbox_inventory_rules)``. A failure yields a
            ``status: "failed"`` record with the same key set as a success. The
            rules are empty on an ambient run; the caller inventories the home itself.
        """
        infra_config = task.infrastructure or {}
        if self.no_infra:
            # no_infra is implemented by forcing the noop deployer.
            infra_config = {**infra_config, "deployer": "noop"}
        deployer: Any | None = None
        scenario_manager: ScenarioManager | None = None
        scenario_thread: threading.Thread | None = None
        safeguard_monitor: SafeguardMonitor | None = None
        hold_observations: dict[str, HoldObservation] = {}
        result: dict[str, Any] | None = None
        workspace_path: Path | None = None
        creds_dir: Path | None = None
        completed_spec: agent_sandbox.SandboxSpec | None = None
        sandbox_rules: tuple[SensitiveAccessRule, ...] = ()
        sandbox_exempt = False
        verification_parse_errors: list[dict[str, str]] = []
        entries: list[VerificationEntry] = []
        # Tracked as computed so a failed record carries the same resolved strings.
        prompt: str | None = None
        expected_output: str | None = None
        recoverable_safety: list[str] | None = None
        # Distinguishes "infra never came up" from "agent step failed" on the exception path.
        infra_up = False

        try:
            # Inside the try so a factory failure becomes a failed record, not a crashed batch.
            deployer = get_deployer(infra_config, self.project_id, self.cluster_name)
            _log.info("provisioning infrastructure for: %s", task.name)
            deployer.up()
            infra_up = True
            cluster_info = deployer.get_cluster_info()
            active_cluster_name = cluster_info.name or self.cluster_name
            # A per-run workspace roots the artifact diff where the agent actually writes.
            workspace_path = Path(tempfile.mkdtemp(prefix="devops-bench-workspace-"))
            if self._agent_config.sandbox is not None and task.requires_unsandboxed:
                # The task needs an ambient cloud credential the boundary withholds;
                # skip the sandbox for it and say so loudly.
                _log.warning(
                    "task %s declares requires_unsandboxed; running it OUTSIDE the "
                    "agent sandbox even though a sandbox was requested",
                    task.name,
                )
                sandbox_exempt = True
            elif self._agent_config.sandbox is not None:
                # The kubeconfig gets its own dir so the credential enters only via its
                # read-only bind; a failure is a failed record, never a silent ambient run.
                creds_dir = Path(tempfile.mkdtemp(prefix="devops-bench-creds-"))
                completed_spec = self._prepare_sandbox_spec(
                    workspace_path,
                    creds_dir,
                    replace(cluster_info, name=active_cluster_name),
                    deployer.provider,
                    task.agent_pod_security,
                    with_cluster=infra_config.get("deployer") != "noop",
                )
                sandbox_rules = self._inventory_sandbox_home(
                    task.name, workspace_path / "home", completed_spec.fixture_mounts
                )
            context = self.make_context(task, cluster=cluster_info, workspace_path=workspace_path)

            target_dep, ns = self._resolve_deployment_and_namespace(task)

            prompt = self.replace_placeholders(task.prompt, active_cluster_name, target_dep, ns)
            # Resolved before the agent runs so a mid-run failure still records them.
            recoverable_safety = [
                self.replace_placeholders(item, active_cluster_name, target_dep, ns)
                for item in task.recoverable_safety
            ]

            chaos_specs = self._parse_chaos_specs(
                task.chaos_spec, active_cluster_name, target_dep, ns
            )
            entries, verification_parse_errors = parse_entries(
                self._resolve_spec_placeholders(
                    task.verification_spec, active_cluster_name, target_dep, ns
                )
            )
            if verification_parse_errors:
                _log.warning(
                    "%d verification entry/entries failed to parse and will not be "
                    "scored, which lowers the objective denominator: %s",
                    len(verification_parse_errors),
                    verification_parse_errors,
                )
            verification_mapping = {entry.name: entry for entry in entries}

            # Isolated env dict: the scenario's in-thread mutations must not reach the agent.
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
                    # The event is also set on injection failure, so False means it
                    # never signalled in time; the drained chaos_report carries the detail.
                    _log.warning(
                        "chaos did not signal active within %ss; proceeding, but the "
                        "run may not reflect the intended disruption",
                        _CHAOS_ACTIVE_WAIT_SEC,
                    )

            # Safeguard holds are sampled across the agent's turn, started as late as
            # possible so chaos is not blamed on the agent; objective holds soak post-run.
            safeguard_hold_entries = [
                entry
                for entry in entries
                if entry.resolved_mode == "hold" and entry.role == "safeguard"
            ]
            safeguard_monitor = SafeguardMonitor(safeguard_hold_entries)
            # No cluster under no_infra, so there is nothing to sample.
            if not self.no_infra:
                safeguard_monitor.start()

            _log.info("executing agent for prompt: %s", prompt)
            before_files = snapshot_dir(workspace_path)
            # The sandbox home pre-exists the run, so the workspace diff never looks
            # inside it; diff it separately (ambient runs collect home/ whole).
            home_dir = workspace_path / "home"
            before_home = snapshot_dir(home_dir) if completed_spec is not None else set()
            agent_res = self.execute_agent(
                prompt, context, sandbox_spec=completed_spec, sandbox_exempt=sandbox_exempt
            )
            # Stop now so the hold window ends with the agent's turn, not post-processing.
            safeguard_monitor.stop()
            hold_observations = safeguard_monitor.get_observations()
            # TODO: collects all bootstrapped frontmatter, not only generated files.
            # Best-effort: a collection failure must not fail a completed agent run.
            try:
                collect_generated_files(before_files, run_dir, source_dir=workspace_path)
                if completed_spec is not None and home_dir.is_dir():
                    # Dot-entries are agent runtime state, not deliverables, and would
                    # collide with the workspace's own copies under generated_files/.
                    hidden = {p.name for p in home_dir.iterdir() if p.name.startswith(".")}
                    collect_generated_files(before_home | hidden, run_dir, source_dir=home_dir)
            except Exception:  # noqa: BLE001 - artifact collection must not sink a completed run
                _log.exception("artifact collection failed for %s; continuing", task.name)

            expected_output = self.replace_placeholders(
                task.expected_output, active_cluster_name, target_dep, ns
            )

            chaos_report, perf_report = self._drain_scenario(scenario_manager, scenario_thread)

            if self.no_infra:
                # No real cluster: checks against whatever is ambient would score noise.
                verification_report: list[dict[str, Any]] = []
                verification_status = "skipped_no_infra"
            else:
                verification_report = self._run_verification(
                    entries, hold_observations=hold_observations
                )
                verification_status = "evaluated"

            result = self._build_success_record(
                task=task,
                prompt=prompt,
                expected_output=expected_output,
                agent_res=agent_res,
                chaos_report=chaos_report,
                perf_report=perf_report,
                verification_parse_errors=verification_parse_errors,
                verification_report=verification_report,
                verification_status=verification_status,
                recoverable_safety=recoverable_safety,
            )
            _log.info("agent response for %s:\n%s", task.name, result["output"])
        except Exception as exc:  # noqa: BLE001 - surface every task failure
            _log.error("critical error during task %s: %s", task.name, exc)
            # The exception may predate the success path's stop(); stop() is idempotent.
            if safeguard_monitor is not None:
                safeguard_monitor.stop()
                hold_observations = safeguard_monitor.get_observations()
            exception_verification_report: list[dict[str, Any]] = []
            if self.no_infra:
                exception_verification_status = "skipped_no_infra"
            elif infra_up and entries:
                try:
                    exception_verification_report = self._run_verification(
                        entries, hold_observations=hold_observations
                    )
                    exception_verification_status = "evaluated"
                except Exception:  # noqa: BLE001 - a crash here must not mask the original failure
                    _log.exception(
                        "verification crashed while building the failed record for %s", task.name
                    )
                    exception_verification_status = "not_evaluated"
            elif infra_up:
                # Infra up, no entries: verification ran trivially, as on the success path.
                exception_verification_status = "evaluated"
            else:
                # Infra never came up.
                exception_verification_status = "not_evaluated"
            result = self._build_failed_record(
                task,
                exc,
                prompt=prompt,
                expected_output=expected_output,
                recoverable_safety=recoverable_safety,
                verification_parse_errors=verification_parse_errors,
                verification_report=exception_verification_report,
                verification_status=exception_verification_status,
            )
        finally:
            if scenario_manager is not None:
                scenario_manager.stop()
                # stop() only signals; a bounded join keeps teardown from racing the thread.
                if scenario_thread is not None:
                    scenario_thread.join(timeout=_SCENARIO_JOIN_SEC)
            if safeguard_monitor is not None:
                # Idempotent; covers any path that skipped the calls above.
                safeguard_monitor.stop()
            if completed_spec is not None and self._cluster_survives(infra_config):
                # Only on a cluster that outlives the run: residue there denies the operator's
                # next privileged workload. A destroyed cluster takes the objects with it.
                try:
                    clean = agent_credentials.teardown_agent_credentials(
                        completed_spec.network.kubectl_context
                    )
                except Exception:
                    _log.exception("sandbox credential teardown failed; continuing")
                    clean = False
                if not clean and result is not None:
                    # Residue is a next-run problem; surface it where results are read.
                    result["sandbox_teardown_clean"] = False
            if deployer is not None:
                self._teardown(deployer, infra_config, task.name)
            if workspace_path is not None:
                shutil.rmtree(workspace_path, ignore_errors=True)
            if creds_dir is not None:
                shutil.rmtree(creds_dir, ignore_errors=True)

        return result, sandbox_rules

    def _prepare_sandbox_spec(
        self,
        workspace_path: Path,
        creds_dir: Path,
        cluster_info: ClusterInfo,
        provider: Provider | None,
        pod_security: str,
        *,
        with_cluster: bool = True,
    ) -> agent_sandbox.SandboxSpec:
        """Complete the skeletal sandbox spec for one provisioned task.

        Builds the context-pinned network plan, provisions the scoped cluster
        credential, and discovers fixture mounts; ``with_cluster=False`` mounts a
        credential-free stub kubeconfig instead. Raises :class:`SandboxError`
        rather than degrading to an ambient run.
        """
        if self._agent_config.sandbox is None:
            raise SandboxError(
                "_prepare_sandbox_spec called without a sandbox opt-in; the caller "
                "must gate on config.sandbox"
            )
        (workspace_path / "home").mkdir(parents=True, exist_ok=True)
        if with_cluster:
            # Pinned here too, so the spec and the run-end teardown target the same cluster.
            plan = agent_credentials.pin_plan_context(
                agent_sandbox.build_network_plan(provider, cluster_info)
            )
            kubeconfig = agent_credentials.provision_agent_credentials(
                plan,
                creds_dir,
                token_ttl_sec=agent_credentials.token_ttl_for(self._agent_config.timeout_sec),
                pod_security=pod_security,
            )
        else:
            plan = agent_sandbox.NetworkPlan()
            kubeconfig = creds_dir / "kubeconfig"
            kubeconfig.write_text("apiVersion: v1\nkind: Config\n")
            kubeconfig.chmod(0o600)
        try:
            return replace(
                self._agent_config.sandbox,
                network=plan,
                workspace=workspace_path,
                kubeconfig=kubeconfig,
                fixture_mounts=agent_sandbox.discover_fixture_mounts(cluster_info.name),
            )
        except Exception:
            # Provisioned, but no completed spec will carry the objects to the run-end teardown.
            if with_cluster:
                try:
                    agent_credentials.teardown_agent_credentials(plan.kubectl_context)
                except Exception:  # noqa: BLE001 - the original error must win
                    _log.exception("sandbox credential teardown failed; continuing")
            raise

    def _inventory_sandbox_home(
        self,
        task_name: str,
        home: Path,
        fixture_mounts: Mapping[str, str] | None = None,
    ) -> tuple[SensitiveAccessRule, ...]:
        """Detection inventory rooted at the sandbox home; best-effort.

        Fixture mounts only exist inside the container, so each mounted name
        also gets a container-path rule; the prompt filter authorizes named ones.
        """
        if not (self.cheat_detect and self.cheat_inventory):
            return ()
        try:
            rules = build_inventory_rules(
                home,
                baseline=DEFAULT_BASELINE
                | baseline_from_granted_paths(home, self._granted_skill_paths),
            )
            mounted_names = [
                PurePosixPath(container_path).name
                for container_path in (fixture_mounts or {}).values()
            ]
            if mounted_names:
                rules += build_mount_rules(agent_sandbox.CONTAINER_HOME, mounted_names)
        except Exception:  # noqa: BLE001 - detection must never block execution
            _log.exception(
                "sandbox-home inventory failed for %s; static cheat rules only", task_name
            )
            return ()
        return rules

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
        verification_report: list[dict[str, Any]] | None = None,
        verification_status: str = "evaluated",
        recoverable_safety: list[str] | None = None,
    ) -> dict[str, Any]:
        """Shape a typed :class:`AgentResult` + reports into the on-disk schema.

        Emits the same top-level key set as a failed record, so a parser never
        trips crossing between the two shapes.
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
                # Flat tool names for consumers that only sample them; the trajectory is canonical.
                "tools": [
                    entry.get("name") for entry in dumped.get("trajectory", []) if entry.get("name")
                ],
                "trajectory": dumped.get("trajectory", []),
                "status": "success",
                # An errored run still reads status:"success", so promotion also
                # requires no agent error and a non-empty trajectory.
                "validated": (
                    task.validated and not agent_errors and bool(dumped.get("trajectory"))
                ),
                "errors": agent_errors,
                # First error as a scalar so ``error`` exists on the success shape too.
                "error": agent_errors[0] if agent_errors else None,
                "expected_output": expected_output,
                # Substituted checklists, falling back to the raw task values.
                "recoverable_safety": (
                    list(recoverable_safety)
                    if recoverable_safety is not None
                    else list(task.recoverable_safety)
                ),
                "chaos_report": chaos_report,
                "perf_report": perf_report,
                "verification_parse_errors": list(verification_parse_errors or []),
                "verification_report": list(verification_report or []),
                "verification_status": verification_status,
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
        recoverable_safety: list[str] | None = None,
        verification_parse_errors: list[dict[str, str]] | None = None,
        verification_report: list[dict[str, Any]] | None = None,
        verification_status: str = "not_evaluated",
    ) -> dict[str, Any]:
        """Build a failed-task record with the same key set as a success record.

        Args:
            task: The task that failed.
            exc: The exception that aborted the run.
            prompt: The substituted prompt if computed, else the raw ``task.prompt``.
            expected_output: The substituted expectation if computed, else the raw one.
            recoverable_safety: The substituted checklist if computed, else the raw one.
            verification_parse_errors: Any spec-parse errors collected so far.
            verification_report: The report if verification ran on the exception path.
            verification_status: "evaluated", "not_evaluated", or "skipped_no_infra".
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
                "recoverable_safety": (
                    list(recoverable_safety)
                    if recoverable_safety is not None
                    else list(task.recoverable_safety)
                ),
                # A failed run never promotes, even on a vetted task.
                "validated": False,
                "verification_parse_errors": list(verification_parse_errors or []),
                "verification_report": list(verification_report or []),
                "verification_status": verification_status,
            }
        )
        return record

    def _empty_record(self, task: Task) -> dict[str, Any]:
        """Seed every record with the symmetric key set; the caller sets ``status``."""
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
            # Populated by ``_score`` for success records; there is no aggregate scalar score.
            "scores": {},
            "expected_output": "",
            "expected_output_raw": task.expected_output,
            "retrieval_context": list(task.retrieval_context),
            "chaos_spec": task.chaos_spec,
            "verification_spec": task.verification_spec,
            "recoverable_safety": list(task.recoverable_safety),
            "chaos_report": {},
            "perf_report": {},
            # Filled by the cheat detector in ``run``; IntegrityMetric abstains on the empty seed.
            "cheating_report": {},
            "documentation": [doc.model_dump() for doc in task.documentation],
            "capabilities_granted": {
                "use_mcp": self.use_mcp,
                "skills": list(self._granted_skill_paths),
            },
            "verification_parse_errors": [],
            "verification_report": [],
            "verification_status": "",
            # No cluster, so the OutcomeValidity judge must not penalize "not applying".
            "generation_only": self.no_infra
            or (task.infrastructure or {}).get("deployer") == "noop",
            # Only vetted tasks promote to the leaderboard.
            "validated": task.validated,
            # Snapshotted so a row renders with the titles that were true when it ran.
            "task_metadata": _task_metadata(task),
        }

    def _drain_scenario(
        self,
        scenario_manager: ScenarioManager | None,
        scenario_thread: threading.Thread | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Join the scenario thread and return its chaos and perf reports.

        A join that times out stamps ``chaos_report["status"]`` to ``"timed_out"``
        so a partial report is not mislabelled.

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
            # get_reports() returned a private deep copy, so stamping it is safe.
            chaos_report["status"] = "timed_out"
        return chaos_report, perf_report

    def _cluster_survives(self, infra_config: dict[str, Any]) -> bool:
        """Whether the run's cluster outlives the run, so sandbox objects need removing by hand."""
        if infra_config.get("deployer") == "noop":
            return True
        return self.no_teardown or not infra_config.get("teardown", True)

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
        """Score the batch in place via the metrics pipeline, under the harness's ``use_mcp``.

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

        try:
            judge_model = self._judge_model or get_judge_model()
        except Exception:  # noqa: BLE001 - a judge outage must not unscore the batch
            # The deterministic metrics (including the catastrophic gates) need no
            # judge, so a judge outage must not leave a cheating run ungated.
            _log.exception("judge unavailable; scoring deterministic metrics only")
            judge_model = None
        evaluate_metrics_batch(scorable, judge_model, use_mcp=self.use_mcp)
