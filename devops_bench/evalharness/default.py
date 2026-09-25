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
    VERIFICATION_TOTAL_BUDGET_SEC,
    ScenarioManager,
    pick_free_port,
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

# Builtin agent modules imported at call time so their ``@AGENTS.register``
# decorators run. External packages add agents by registering with the same
# registry, with no edit here.
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


def _canonical_agent_type(agent_type: str) -> str:
    """Normalize an agent-type alias to its canonical registry key.

    The single source of truth for both registry lookup and result recording,
    so an arm selected via a friendly alias (``claude-code`` / ``gemini-cli``)
    aggregates under the same ``harness`` / ``setup_id`` as the canonical key
    instead of splitting into a second dashboard setup.
    """
    return _AGENT_TYPE_ALIASES.get(agent_type, agent_type)


def _entry_display_fields(entry: VerificationEntry) -> dict[str, Any]:
    """The display fields copied verbatim from an entry onto its report item.

    Snapshotted onto the record so a result renders with the titles that were
    true when it ran, without joining back to the task file at that revision.
    An undeclared field lands as ``None``, meaning the author wrote nothing,
    unlike the task-level fields, which default to ``""`` on the schema. The
    row normalizer maps both to ``""``.
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
        # Trajectory-based cheating detection annotates each record with a
        # ``cheating_report`` and never touches ``validated``. The report is
        # not inert, though: ``IntegrityMetric`` reads it during the later
        # scoring pass and gates a flagged run's ``OutcomeScore`` to zero.
        # Extra rules load from an optional YAML file — loaded
        # here so a bad BENCH_CHEAT_RULES path fails loud at construction
        # (an operator config error) instead of being swallowed by the
        # best-effort scan at the end of the run.
        self.cheat_detect: bool = get_bool("BENCH_CHEAT_DETECT", True)
        self.cheat_rules_path: str | None = get_env("BENCH_CHEAT_RULES")
        self._cheat_rules: tuple[SensitiveAccessRule, ...] = (
            load_ruleset(self.cheat_rules_path) if self.cheat_detect else ()
        )
        # Also snapshot the agent home before the first agent runs and flag
        # access to anything already lying there (prior-run leftovers).
        self.cheat_inventory: bool = get_bool("BENCH_CHEAT_INVENTORY", True)
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

    def resolve_agent(
        self,
        agent_type: str,
        sandbox_spec: agent_sandbox.SandboxSpec | None = None,
        *,
        sandbox_exempt: bool = False,
    ) -> Any:
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
        it instead. Everything else is unchanged.
        """
        if sandbox_exempt:
            # A task that declared ``requires_unsandboxed``. Clearing the field
            # rather than leaving the skeletal spec in place is the whole point:
            # the agent's own gate reads ``config.sandbox is not None``, so a
            # leftover spec would either refuse the run or hand the executor an
            # incomplete boundary.
            return replace(self._agent_config, sandbox=None)
        if sandbox_spec is not None:
            return replace(self._agent_config, sandbox=sandbox_spec)
        return self._agent_config

    def _build_agent_config_snapshot(self) -> AgentConfig:
        """Build the gated :class:`AgentConfig` from the env layer.

        Called exactly once, from :meth:`__init__`. Starts from
        :meth:`AgentConfig.from_env` so existing ``AGENT_*`` knobs continue
        to flow through (``model``, ``provider``, ``api_key``, ``target``,
        ``timeout``, ``max_turns``, ``extra_env``, ``extra_flags``), then
        replaces
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
            # Rebuilding field-by-field silently drops anything not named
            # here. Omitting extra_flags meant AGENT_EXTRA_FLAGS parsed fine
            # and then never reached the binary, so agy kept its 5m default
            # --print-timeout and every run longer than that died mid-task
            # with "timeout waiting for response".
            extra_flags=base.extra_flags,
            sandbox=base.sandbox,
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
        """Walk a nested spec and substitute placeholders in every string leaf.

        Substitution runs before parsing because a template string like
        ``{{NAMESPACE}}`` is not a valid value for a typed field, so
        placeholders are resolved on the raw payload before the caller parses
        it into a typed structure.

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

    def _run_verification(
        self,
        entries: list[VerificationEntry],
        timeout_sec: float = VERIFICATION_TIMEOUT_SEC,
        *,
        hold_observations: dict[str, HoldObservation] | None = None,
    ) -> list[dict[str, Any]]:
        """Evaluate every entry against the live cluster after the agent finishes.

        Every entry runs, unconditionally, whether or not a chaos fault
        references it. One entry that raises is recorded as a failure and the
        rest still run, matching how the metrics pipeline isolates a failing
        evaluator.

        Two budgets apply. ``timeout_sec`` is the per-entry cap for a single
        converging entry's checks. :data:`VERIFICATION_TOTAL_BUDGET_SEC` is
        the wall-clock cap for this whole pass across every entry; without it
        a task with many failing converge objectives burns entries x
        ``timeout_sec`` (12 entries x 120s is 22+ minutes). A single monotonic
        deadline is computed from the total budget once at the top, and each
        converging entry gets ``min(timeout_sec, remaining)``. Assert entries
        ignore the total budget and always run: they are single evaluations,
        and a safeguard that goes unchecked defeats the point of having it.
        A converging entry with less than :data:`MIN_LEAF_BUDGET_SECONDS`
        remaining is recorded here as budget-exhausted rather than handed to
        ``run_entry``: the runner's own leaf guard uses that same threshold
        to short-circuit an under-budget leaf as a definite "deadline
        exhausted" outcome, and this entry was never observed either way.

        A ``hold`` entry is never evaluated with a single ``run_entry`` call
        here, but the two roles reach their observation differently.  A
        ``safeguard`` hold entry was already sampled on a background thread
        across the agent's turn (see
        ``devops_bench.evalharness.hold.SafeguardMonitor``), and its outcome
        comes entirely from ``hold_observations``. An ``objective`` hold
        entry is soaked right here instead, via
        :func:`~devops_bench.evalharness.hold.run_hold_window`, against this
        same total-budget deadline: an objective starts false and must
        become true and stay true, which can only be observed after the
        agent's turn ends. A hold entry with zero samples either way is
        recorded as an error, not a silent pass: a hold nobody watched must
        not read as one that held.

        Args:
            entries: The task's parsed verification entries.
            timeout_sec: Per-entry budget for converging entries.
            hold_observations: Name-keyed monitor observations for every
                ``safeguard``-role ``hold`` entry, as returned by
                :meth:`~devops_bench.evalharness.hold.SafeguardMonitor.get_observations`.
                ``None`` (or a missing name) is treated the same as zero
                samples. Never consulted for ``objective``-role hold entries,
                which are soaked in this same pass instead.

        Returns:
            One raw mapping per entry, in declaration order, carrying the
            scoring vocabulary alongside the outcome. This is the exact shape
            :func:`devops_bench.verification.rollup.rollup` consumes.
        """
        agent = VerifierAgent()
        report: list[dict[str, Any]] = []
        total_deadline = time.monotonic() + VERIFICATION_TOTAL_BUDGET_SEC
        hold_observations = hold_observations or {}

        # Objective holds soak last, so converging objectives claim the shared
        # budget before any soak can consume it. Rows keep declaration order.
        rows: list[dict[str, Any] | None] = [None] * len(entries)
        objective_holds: list[int] = []

        for index, entry in enumerate(entries):
            if entry.resolved_mode == "hold" and entry.role == "safeguard":
                rows[index] = self._hold_report_entry(entry, hold_observations.get(entry.name))
                continue
            if entry.resolved_mode == "hold" and entry.role == "objective":
                objective_holds.append(index)
                continue
            rows[index] = self._evaluate_entry(agent, entry, timeout_sec, total_deadline)

        for index in objective_holds:
            entry = entries[index]
            # hold_window_sec is required for an objective hold entry;
            # normally enforced by VerificationEntry's own validation, so
            # reaching here without it means a spec-validation bug let an
            # invalid entry through to verification.
            if entry.hold_window_sec is None:
                raise ValueError(
                    f"objective hold entry {entry.name!r} reached verification without "
                    "hold_window_sec set; this should have been rejected at "
                    "spec-validation time"
                )
            obs = run_hold_window(
                entry,
                entry.hold_window_sec,
                interval_sec=effective_poll_interval(entry.hold_poll_interval_sec),
                deadline=total_deadline,
            )
            rows[index] = self._hold_report_entry(entry, obs)

        report.extend(row for row in rows if row is not None)
        return report

    def _evaluate_entry(
        self,
        agent: VerifierAgent,
        entry: VerificationEntry,
        timeout_sec: float,
        total_deadline: float,
    ) -> dict[str, Any]:
        """Evaluate one converge or assert entry against the shared deadline."""
        remaining = total_deadline - time.monotonic()
        if entry.resolved_mode != "assert" and remaining < MIN_LEAF_BUDGET_SECONDS:
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
            result = agent.run_entry(entry, timeout_sec=min(timeout_sec, remaining))
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

        The verdict itself (pass / fail / error, and why) is delegated to
        :func:`~devops_bench.evalharness.hold.hold_verdict` so both hold
        drivers (the live safeguard monitor and the post-run objective
        window) are scored by exactly one rule. ``obs is None`` (the entry's
        name was missing from ``hold_observations`` entirely) is treated the
        same as a fresh, zero-sample observation.

        Args:
            entry: The hold-mode entry being reported.
            obs: The driver's observation for this entry, or ``None`` if the
                entry's name was missing from ``hold_observations`` entirely.

        Returns:
            The report row for this entry, in the same shape
            :func:`devops_bench.verification.rollup.rollup` consumes, plus
            ``hold_sample_count`` / ``hold_error_count`` /
            ``hold_first_violation_reason`` / ``hold_first_violation_at_sec``
            so the outcome is auditable from the report alone.
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

        Best-effort by contract: detection must never block execution, so a
        snapshot failure logs and yields nothing, leaving the caller with the
        static ruleset alone. Returns nothing too when either cheat-detection
        toggle is off, which keeps the toggle check in one place.

        Args:
            fingerprint_only: Passed through to
                :func:`~devops_bench.cheat_detection.build_inventory_rules` — the
                entry names still allowed to produce content rules.

        Returns:
            The generated ruleset, empty on failure or when disabled.
        """
        if not (self.cheat_detect and self.cheat_inventory):
            return ()
        try:
            home = Path.home()
            # Skills granted to the agent are material it is told to read,
            # so the home entry holding them is environment, not leftover.
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
        if sandboxed:
            # Fail before any run dir or cluster exists: an unmigrated agent
            # would otherwise provision a cluster per task only to fail each
            # one with the per-task SandboxError (which stays as depth).
            _ensure_builtin_agents_registered()
            agent_cls = AGENTS.get(_canonical_agent_type(self.agent_type))
            if agent_cls is not None and not getattr(agent_cls, "supports_sandbox", False):
                raise SandboxError(
                    f"agent harness {agent_cls.__name__} has not been migrated onto "
                    "the sandbox seam; refusing the whole batch rather than "
                    "provisioning a cluster per task just to fail each one"
                )
            if self.parallel:
                # The sweep matches on the shared name prefix and cannot tell a
                # crashed run's stray from a sibling harness's *live* agent
                # container, so under BENCH_PARALLEL it would reap a concurrent
                # run mid-task.
                _log.info(
                    "BENCH_PARALLEL set: skipping the stray sandbox-container "
                    "sweep; reap leftovers manually with `docker ps --filter "
                    "name=devops-bench-agent-` once no benchmark is running"
                )
            else:
                # A container the harness starts is normally reaped around its
                # own run, but a harness process killed outright (Ctrl-C, OOM,
                # a host reboot) never gets to run that ``finally``. Sweeping
                # once here, before this batch's own containers exist, catches
                # exactly that leak without risking a live container from the
                # run in progress.
                try:
                    agent_sandbox.sweep_stray_containers()
                except Exception:  # noqa: BLE001 - a sweep failure must not block the run
                    _log.exception("stray sandbox container sweep failed; continuing")

        run_dir = self.reporter.new_run_dir()

        # Snapshot the home once before anything runs, purely to record which
        # leftovers predate the batch. Those are genuine prior-run artifacts
        # and may always fingerprint. Skipped entirely when sandboxed: the
        # operator home is not what the agent sees.
        pre_existing: frozenset[str] = frozenset()
        if not sandboxed:
            pre_existing = frozenset(rule.source for rule in self._inventory_home() if rule.source)

        # One inventory per task iteration, paired positionally with
        # ``detailed_results`` (a batch may run the same task twice). Ambient
        # rules come from re-scanning the operator home before each agent
        # runs; a sandboxed task's home does not exist until ``_run_one``
        # builds the workspace, so its rules come back from that call.
        created_by: dict[str, str] = {}
        prev_task_name: str | None = None
        task_inventories: list[tuple[SensitiveAccessRule, ...]] = []
        detailed_results: list[dict[str, Any]] = []
        for task in tasks:
            ambient_rules: tuple[SensitiveAccessRule, ...] = ()
            if not sandboxed:
                ambient_rules = self._ambient_inventory_rules(
                    task.name, pre_existing, created_by, prev_task_name
                )
            record, sandbox_rules = self._run_one(task, run_dir)
            task_inventories.append(sandbox_rules if sandboxed else ambient_rules)
            detailed_results.append(record)
            prev_task_name = task.name

        # Annotate sensitive-access flags before the first write so both the
        # raw and the scored results.json carry the report, and because
        # ``_score`` below reads it. Best-effort per record: a detector failure
        # leaves that record's seeded empty report and moves on to the next —
        # which also leaves that record ungated, since an absent verdict is an
        # abstention rather than a zero.
        if self.cheat_detect:
            # Per record: a home entry the task prompt itself names (the
            # GitOps repo to push to, the deliverable to write) is
            # authorized for that record, so its inventory path rule is
            # dropped. Content fingerprints always apply.
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

        # ``sandboxed`` is the ARM's property (the run was requested sandboxed),
        # so it belongs in the setup id: a sandboxed arm aggregates as its own
        # dashboard setup and the A/B soak is a plain group-by. Per-task
        # divergence (a requires_unsandboxed exemption) is carried on each
        # row's own ``sandboxed`` field instead.
        sandbox = self._agent_config.sandbox
        augmentation = derive_augmentation(
            {
                "use_mcp": self.use_mcp,
                "skills": list(self._granted_skill_paths),
                "sandboxed": sandbox is not None,
            }
        )
        # Record the canonical harness key so an arm selected via a friendly
        # alias (e.g. ``claude-code`` / ``gemini-cli``) aggregates with the
        # canonical key rather than splitting into a second dashboard setup.
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
            # A mutable tag is not provenance; the digest is. Resolved at
            # report time (best-effort, None recorded honestly on failure) so
            # an A/B pair claiming "the same image" is checkable after the
            # fact.
            sandbox_image=sandbox.image if sandbox is not None else None,
            sandbox_image_digest=(
                agent_sandbox.image_digest(sandbox.image) if sandbox is not None else None
            ),
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

        Mid-batch entries are attributed to the task running when they
        appeared (``created_by``, mutated here) and fingerprint only for
        tasks with a *different* name: an honest repeat must not be flagged
        for rewording its own deliverable, while a colliding deliverable
        filename keeps fingerprint coverage after the prompt filter drops
        its path rule.
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
            ``(record, sandbox_inventory_rules)``. On any failure a
            ``status: "failed"`` record is returned instead of being dropped,
            with the same top-level key set as a success record. The rules are
            the sandbox-home detection inventory for this task (empty on an
            ambient run — the caller inventories the operator home itself).
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
        # Track the substituted prompt / expectation / safety checklists as they
        # are computed so a failed record can carry the same resolved strings a
        # success record would, falling back to the raw task fields before
        # substitution.
        prompt: str | None = None
        expected_output: str | None = None
        recoverable_safety: list[str] | None = None
        # Whether deployer.up() returned, i.e. there is a cluster verification
        # could target. Distinguishes "infra never came up" from "infra came
        # up but the agent step itself failed" on the exception path below.
        infra_up = False

        try:
            # Build the deployer inside the try so a factory failure (e.g. an
            # unknown deployer type) becomes a failed record for this task
            # rather than crashing the whole batch.
            deployer = get_deployer(infra_config, self.project_id, self.cluster_name)
            _log.info("provisioning infrastructure for: %s", task.name)
            deployer.up()
            infra_up = True
            cluster_info = deployer.get_cluster_info()
            active_cluster_name = cluster_info.name or self.cluster_name
            # Own a real per-run workspace so the artifact diff is rooted at
            # the directory the agent actually writes to (its CLI wrapper's
            # working directory), not the harness process's launch cwd.
            workspace_path = Path(tempfile.mkdtemp(prefix="devops-bench-workspace-"))
            if self._agent_config.sandbox is not None and task.requires_unsandboxed:
                # The task declared that it cannot run behind the boundary —
                # secret-rotation drives Secret Manager through ADC, and ADC is
                # exactly what the sandbox strips. Skip the sandbox for this
                # task instead of failing it, and say so: an operator who asked
                # for a sandboxed matrix must be able to see which tasks did not
                # get one, rather than discovering it in the manifest later.
                _log.warning(
                    "task %s declares requires_unsandboxed; running it OUTSIDE the "
                    "agent sandbox even though a sandbox was requested",
                    task.name,
                )
                sandbox_exempt = True
            elif self._agent_config.sandbox is not None:
                # First moment both the cluster endpoint and the workspace
                # exist. The kubeconfig gets its own temp dir so the
                # credential only enters through its read-only bind; a
                # failure here becomes a failed record, never a silent
                # unsandboxed run. A no-cluster run (noop deployer) skips
                # the plan and cluster credential — a stale context
                # matching the configured name must not leak in.
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
            # Resolved here, before the agent runs, so a failure mid-execution
            # still records the substituted checklists rather than raw
            # placeholders.
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

            # Safeguard hold entries must be observed continuously from here
            # through the end of the agent's turn, not just at the moment
            # verification runs after the agent exits (see hold's module
            # docstring for the failure this closes). Started as close to
            # the agent's turn as possible so a chaos-induced state change is
            # not mistaken for an agent-caused violation. Objective hold
            # entries are deliberately excluded here: an objective starts
            # false and must become true, so sampling it live would latch a
            # spurious violation before the agent has done anything. Those
            # are soaked instead in the post-run verification pass (see
            # ``_run_verification``).
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
            # The sandbox home pre-exists the run, so the top-level workspace
            # diff never sees inside it; diff it separately or ~ writes are
            # silently dropped from generated_files.
            home_dir = workspace_path / "home"
            before_home = snapshot_dir(home_dir)
            agent_res = self.execute_agent(
                prompt, context, sandbox_spec=completed_spec, sandbox_exempt=sandbox_exempt
            )
            # The agent's turn just ended; stop sampling immediately so the
            # hold window is exactly "seed through the end of the agent's
            # turn" rather than continuing to sample through the (potentially
            # slow) post-processing below.
            safeguard_monitor.stop()
            hold_observations = safeguard_monitor.get_observations()
            # NOTE/TODO: This collects ALL frontmatter from bootstrapping, not just generated files.
            # Consider a more targeted filter in a future iteration.
            # Best-effort: a collection failure (I/O, permissions, a bad link in the
            # workspace) must not turn an already-completed agent run into a failed,
            # unscored record, so isolate it like the other non-critical steps.
            try:
                collect_generated_files(before_files, run_dir, source_dir=workspace_path)
                if home_dir.is_dir():
                    collect_generated_files(before_home, run_dir, source_dir=home_dir)
            except Exception:  # noqa: BLE001 - artifact collection must not sink a completed run
                _log.exception("artifact collection failed for %s; continuing", task.name)

            expected_output = self.replace_placeholders(
                task.expected_output, active_cluster_name, target_dep, ns
            )

            chaos_report, perf_report = self._drain_scenario(scenario_manager, scenario_thread)

            if self.no_infra:
                # no_infra means no real cluster to check; issuing kubectl
                # calls against whatever is ambient would score noise, not
                # this task.
                verification_report: list[dict[str, Any]] = []
                verification_status = "skipped_no_infra"
            else:
                verification_report = self._run_verification(
                    entries, hold_observations=hold_observations
                )
                verification_status = "evaluated"

            result = self._build_success_record(
                task=task,
                sandboxed=completed_spec is not None,
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
            # The exception may have landed before the success path's own
            # stop()+get_observations() ran (e.g. the agent call itself
            # raised), so stop here too. Idempotent: a second stop() on an
            # already-stopped monitor is a no-op, mirroring how
            # scenario_manager.stop() is already called from both the success
            # path (via _drain_scenario) and this finally-adjacent path below.
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
                # Infra came up but the task declared no entries: verification
                # ran trivially over nothing, the same as the success path
                # records for this case, rather than reading as "never ran".
                exception_verification_status = "evaluated"
            else:
                # Infra never came up.
                exception_verification_status = "not_evaluated"
            result = self._build_failed_record(
                task,
                exc,
                sandboxed=completed_spec is not None,
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
                # stop() only signals the abort flag; join the daemon thread with
                # a bounded timeout so teardown does not race a still-running
                # background scenario (the success path joins via _drain_scenario,
                # but the exception path reaches here without draining).
                if scenario_thread is not None:
                    scenario_thread.join(timeout=_SCENARIO_JOIN_SEC)
            if safeguard_monitor is not None:
                # stop() is idempotent and never raises; this covers any path
                # that skipped the two calls above.
                safeguard_monitor.stop()
            if completed_spec is not None:
                # Before the infra teardown, while there is still a cluster to
                # accept the deletes. On a cluster the deployer is about to
                # destroy this is redundant but cheap; on a REUSED one
                # (BENCH_NO_TEARDOWN, a kind dev loop, the vcluster host) it
                # is correctness: the pod-security policy is not
                # username-scoped, so left behind it denies the OPERATOR's own
                # privileged workloads on the next run. Teardown never raises
                # by design; the guard is for the finally block's sake.
                try:
                    agent_credentials.teardown_agent_credentials(
                        completed_spec.network.kubectl_context
                    )
                except Exception:
                    _log.exception("sandbox credential teardown failed; continuing")
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

        Builds the provider's network plan (context-pinned, so a later
        current-context switch cannot swap clusters), provisions the scoped
        ServiceAccount credential, and discovers fixture mounts.
        ``with_cluster=False`` (noop deployer / no_infra) skips both and
        mounts a credential-free stub kubeconfig so a stale context cannot
        leak in. Raises :class:`SandboxError` when a plan or credential
        cannot be built; the caller records a failed task, never degrades.
        """
        (workspace_path / "home").mkdir(parents=True, exist_ok=True)
        if with_cluster:
            plan = agent_sandbox.build_network_plan(provider, cluster_info)
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
            # The cluster objects are already provisioned, but the completed
            # spec that would carry their context to the run-end teardown
            # never comes to exist — so remove them here, keeping the original
            # error as the one the caller sees (teardown never raises).
            if with_cluster:
                agent_credentials.teardown_agent_credentials(plan.kubectl_context)
            raise

    def _inventory_sandbox_home(
        self,
        task_name: str,
        home: Path,
        fixture_mounts: Mapping[str, str] | None = None,
    ) -> tuple[SensitiveAccessRule, ...]:
        """Detection inventory rooted at the sandbox home; returns the rules.

        Same tripwire as the operator-home inventory, different root. A
        freshly-created home correctly yields the empty ruleset. Fixture
        mounts only materialize inside the container, so each mounted name
        additionally gets a container-path rule; the per-record prompt filter
        authorizes the ones the task itself names. Best-effort: a scan
        failure returns () rather than blocking the run.
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
        sandboxed: bool = False,
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

        Routes every typed value through ``to_dict()`` / ``model_dump()`` and
        emits the **symmetric** key union (every key is present on every
        record), so success and failed records never differ in top-level
        shape — a downstream parser iterating one shape can never ``KeyError``
        crossing into the other.

        Capability metadata (``capabilities_granted``) is recorded so metrics
        / downstream consumers can read what the agent was actually granted
        rather than re-reading ``BENCH_USE_MCP``.
        """
        dumped = agent_res.to_dict()
        agent_errors = list(dumped.get("errors") or [])
        record = self._empty_record(task, sandboxed=sandboxed)
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
                # Placeholder-substituted safety checklists, falling back to the
                # raw task values seeded by ``_empty_record`` when unresolved.
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
        sandboxed: bool = False,
        prompt: str | None = None,
        expected_output: str | None = None,
        recoverable_safety: list[str] | None = None,
        verification_parse_errors: list[dict[str, str]] | None = None,
        verification_report: list[dict[str, Any]] | None = None,
        verification_status: str = "not_evaluated",
    ) -> dict[str, Any]:
        """Build a failed-task record so the failure stays visible.

        Emits the **same** top-level key set as :meth:`_build_success_record`:
        a downstream parser iterating either shape never trips a ``KeyError``
        crossing between them. The differences are values only —
        ``status=\"failed\"``, ``error`` carries the exception text, ``scores``
        stays empty.

        Args:
            task: The task that failed.
            exc: The exception that aborted the run.
            prompt: The placeholder-substituted prompt if it was computed before
                the failure; falls back to the raw ``task.prompt`` otherwise, so
                the record matches the success shape when substitution had run.
            expected_output: The substituted expectation if computed; falls back
                to the raw ``task.expected_output``.
            recoverable_safety: The substituted recoverable-safety checklist if
                computed; falls back to the raw ``task.recoverable_safety``.
            verification_parse_errors: Any spec-parse errors collected so far.
            verification_report: The verification report, if verification ran
                on the exception path (infra was up and entries existed).
                Empty when it did not run.
            verification_status: "evaluated" when the report above is real,
                "not_evaluated" when it could not run, "skipped_no_infra"
                under ``no_infra``.
        """
        error_text = str(exc)
        record = self._empty_record(task, sandboxed=sandboxed)
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

    def _empty_record(self, task: Task, *, sandboxed: bool = False) -> dict[str, Any]:
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
            "recoverable_safety": list(task.recoverable_safety),
            "chaos_report": {},
            "perf_report": {},
            # Populated by the cheat detector in ``run`` (empty when detection
            # is disabled or fails). Read by ``IntegrityMetric``, which gates a
            # flagged run to zero and abstains on this empty seed.
            "cheating_report": {},
            "documentation": [doc.model_dump() for doc in task.documentation],
            "capabilities_granted": {
                "use_mcp": self.use_mcp,
                "skills": list(self._granted_skill_paths),
            },
            # Whether THIS task's agent actually ran inside the container
            # boundary — per-record, not per-run: a ``requires_unsandboxed``
            # task inside a sandboxed run legitimately differs from its arm.
            "sandboxed": sandboxed,
            "verification_parse_errors": [],
            "verification_report": [],
            "verification_status": "",
            # Generation-only tasks have no cluster, so the OutcomeValidity judge
            # must not penalize them for "not applying". This holds both when the
            # task declares ``deployer: noop`` and when ``BENCH_NO_INFRA`` skips
            # provisioning for the whole run (mirrors get_deployer's own gate).
            "generation_only": self.no_infra
            or (task.infrastructure or {}).get("deployer") == "noop",
            # Only tasks vetted as correct promote to the leaderboard; downstream
            # ingest gates inclusion on this flag (default False until vetted).
            "validated": task.validated,
            # Display metadata, snapshotted so a row renders with the titles
            # that were true when it ran.
            "task_metadata": _task_metadata(task),
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

        try:
            judge_model = self._judge_model or get_judge_model()
        except Exception:  # noqa: BLE001 - a judge outage must not unscore the batch
            # Building the judge reads provider config and constructs a client,
            # so a bad JUDGE_PROVIDER or a missing key raises here. Letting that
            # propagate would abort scoring for the whole batch — including the
            # deterministic metrics, which need no judge at all. That matters
            # beyond convenience: the catastrophic gates (task safeguards and
            # the benchmark-integrity check) are deterministic, so an unrelated
            # judge outage would otherwise leave a cheating run ungated and its
            # ``outcomeScore`` null, dropping it out of leaderboard aggregates.
            # Judge-backed metrics fail individually on the ``None`` and are
            # isolated by the pipeline's per-metric guard.
            _log.exception("judge unavailable; scoring deterministic metrics only")
            judge_model = None
        evaluate_metrics_batch(scorable, judge_model, use_mcp=self.use_mcp)
