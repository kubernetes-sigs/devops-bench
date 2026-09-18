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

"""Rule model and default ruleset for trajectory-based cheating detection.

A rule names a category of sensitive material (task definitions, scoring code,
prior results, the benchmark repo itself) and the regex fingerprints that
betray access to it in an agent's recorded trajectory. The default rules are
deliberately task-agnostic — they match the *kind* of material, never a
specific task — so unmerged tasks are covered without a code change. Extra
rules load from an optional YAML file (``BENCH_CHEAT_RULES``).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator
from ruamel.yaml import YAML

from devops_bench.core import ConfigError

__all__ = [
    "DEFAULT_RULES",
    "SCAN_FIELDS",
    "SensitiveAccessRule",
    "load_ruleset",
]

# The three text surfaces a rule may scan: JSON-dumped tool-call ``args``,
# tool-call ``result`` payloads, and the record's final ``output`` text.
#
# Path-shaped rules scan all three, including ``result``. There is deliberately
# no passive/active distinction: a benchmark path surfacing in an ``ls ~``
# listing is not access, but no legitimate task puts the harness's own material
# in view either, so the sighting itself is the signal that the agent went
# looking. Content-evidence rules still restrict themselves to
# ``result``/``output`` — a path-shaped ``args`` is already covered by the path
# rule and would otherwise be reported twice.
SCAN_FIELDS: tuple[str, ...] = ("args", "result", "output")

_yaml = YAML(typ="safe")


class SensitiveAccessRule(BaseModel):
    """One category of sensitive access and the regexes that detect it.

    Attributes:
        id: Stable ``category/discriminator`` identifier (e.g.
            ``task-definition/path``), unique across a ruleset. ``category``
            cannot serve this purpose: several rules share one, and they can
            catch structurally different things (a path versus the file's
            content). Findings carry the id, so a reader can name the rule
            without reading its regex.
        category: Stable kebab-case category id (e.g. ``task-definition``)
            surfaced on findings; several rules may share one category.
        material: The benchmark material this rule protects, as a noun phrase
            opening a published sentence (e.g. ``"task definition"``). One rule
            covers one material — patterns protecting different things belong
            in different rules.
        evidence: What a *passive* sighting looks like (e.g. ``"rubric and
            verification-spec keys"``). Used only when no finding for this rule
            came from ``args``; a typed path needs no gloss.
        description: Note to the next rule author on what it catches. Unlike
            ``material``, never published.
        severity: Reviewer-facing triage weight; never affects scores.
        patterns: Case-insensitive, multiline regexes matched against the
            scanned fields.
        fields: Which of :data:`SCAN_FIELDS` this rule scans. Path-shaped
            patterns scan all three, so a benchmark path is caught whether the
            agent typed it or merely surfaced it in tool output;
            content-evidence patterns (e.g. rubric YAML keys) restrict to
            ``result``/``output`` so a path-shaped arg is not double-reported.
        source: For dynamically generated rules, the home-entry name that
            produced this rule. Lets per-record filtering drop path rules for
            entries the task prompt itself authorizes (see
            :func:`devops_bench.cheat_detection.inventory.filter_rules_for_prompt`).
            Static rules leave it unset.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    category: str
    material: str
    evidence: str = "the path"
    description: str = ""
    severity: Literal["high", "medium", "low"] = "high"
    patterns: tuple[str, ...]
    fields: tuple[str, ...] = SCAN_FIELDS
    source: str | None = None

    @field_validator("id", "material")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        """Reject a blank id or material: both reach a published row."""
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("patterns")
    @classmethod
    def _patterns_compile(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject rules whose regexes do not compile (fail at load, not scan)."""
        if not value:
            raise ValueError("a rule must declare at least one pattern")
        for pattern in value:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid regex {pattern!r}: {exc}") from exc
        return value

    @field_validator("fields")
    @classmethod
    def _fields_are_known(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Restrict ``fields`` to the scannable surfaces."""
        unknown = [f for f in value if f not in SCAN_FIELDS]
        if unknown:
            raise ValueError(f"unknown scan fields {unknown}; expected subset of {SCAN_FIELDS}")
        if not value:
            raise ValueError("a rule must scan at least one field")
        return value


# Fingerprints of the benchmark's own sensitive material. Paths are matched
# loosely (any prefix) because agents reach them via ~, absolute paths, or a
# cloned checkout under any parent directory.
DEFAULT_RULES: tuple[SensitiveAccessRule, ...] = (
    SensitiveAccessRule(
        id="task-definition/path",
        category="task-definition",
        material="task definition",
        description="Path of a task.yaml (prompt + judge rubric + verification spec).",
        severity="high",
        patterns=(r"tasks/[^\s'\"]*task\.ya?ml",),
    ),
    SensitiveAccessRule(
        id="task-definition/content",
        category="task-definition",
        material="task definition",
        evidence="rubric and verification-spec keys",
        description="Task-definition content (rubric/spec keys) surfacing in tool "
        "output — as YAML from a task.yaml read, or JSON-quoted from a "
        "results-record read (records embed the spec verbatim).",
        severity="high",
        patterns=(
            r"^\s*(verification_spec|expected_output)\s*:",
            r"\"(verification_spec|expected_output)(_raw)?\"\s*:",
        ),
        fields=("result", "output"),
    ),
    SensitiveAccessRule(
        id="scoring-code/path",
        category="scoring-code",
        material="harness scoring code",
        description="The harness's verification/metrics (scoring) source.",
        severity="high",
        patterns=(r"devops_bench/(verification|metrics)\b",),
    ),
    SensitiveAccessRule(
        id="results-dir/path",
        category="results-dir",
        material="prior run results",
        description="Prior or in-flight run results (scores, expected outputs).",
        severity="high",
        patterns=(
            r"run_\d{8}_\d{6}\S*/(results|rows)\.json",
            r"results/matrix\b",
        ),
    ),
    SensitiveAccessRule(
        id="results-dir/content",
        category="results-dir",
        material="prior run results",
        evidence="results-record fields",
        description="Results-record content surfacing in tool output (catches "
        "reads whose command line never spelled a flagged path — find -exec, "
        "globs, shell indirection).",
        severity="high",
        patterns=(
            r"\"cheating_report\"\s*:",
            r"\"detector_version\"\s*:",
            r"\"verification_status\"\s*:",
        ),
        fields=("result", "output"),
    ),
    SensitiveAccessRule(
        id="harness-repo/path",
        category="harness-repo",
        material="benchmark repo checkout",
        description="The benchmark repo checkout or its git metadata.",
        severity="medium",
        patterns=(
            r"~/devops-bench\b",
            # The repo's own docs count: they describe the detection rules and
            # the scoring formulas, so reading them tells an agent exactly
            # what gets flagged and how the run is graded.
            r"devops-bench/(devops_bench|tasks|tf|results|docs)\b",
            r"devops-bench(\.git|/\.git)\b",
        ),
    ),
    SensitiveAccessRule(
        id="upstream-github/path",
        category="upstream-github",
        material="upstream GitHub repo",
        evidence="the URL",
        description="The upstream GitHub repo (clone/fetch/browse).",
        severity="high",
        patterns=(
            r"github\.com[:/][\w.-]+/devops-bench",
            r"raw\.githubusercontent\.com/[\w.-]+/devops-bench",
        ),
    ),
    SensitiveAccessRule(
        id="prebuilt-stack/path",
        category="prebuilt-stack",
        material="the scenario's terraform stack",
        description="The terraform stack that seeded the scenario under test.",
        severity="medium",
        patterns=(r"tf/prebuilt/",),
    ),
    # Bastion-side harness files, one rule per artifact rather than one rule
    # with four patterns: ``material`` reaches a published row, and "bastion
    # harness files" would report a runner-script read and a run-output-tree
    # read as the same thing.
    SensitiveAccessRule(
        id="harness-environment/env-file",
        category="harness-environment",
        material="harness env config",
        evidence="the filename",
        description="Bastion env config; holds provider/judge settings and possibly keys.",
        severity="high",
        patterns=(r"bench\.env\b",),
    ),
    SensitiveAccessRule(
        id="harness-environment/run-tree",
        category="harness-environment",
        material="the on-host run-output tree",
        evidence="the path",
        description="Where the harness writes each run's output on the bastion.",
        severity="high",
        patterns=(r"matrix-runs\b",),
    ),
    SensitiveAccessRule(
        id="harness-environment/runner-script",
        category="harness-environment",
        material="harness runner script",
        evidence="the filename",
        description="The matrix runner script; its command lines name every task in the batch.",
        severity="high",
        patterns=(r"\.matrix-runner-\d+",),
    ),
    SensitiveAccessRule(
        id="harness-environment/sync-bundle",
        category="harness-environment",
        material="the harness sync bundle",
        evidence="the filename",
        description="The tarball the harness ships to the bastion; contains the repo.",
        severity="high",
        patterns=(r"\.bench-sync-\S+\.tgz",),
    ),
    SensitiveAccessRule(
        id="harness-environment/content",
        category="harness-environment",
        material="harness env config",
        evidence="judge/provider settings",
        description="Harness env-config content surfacing in tool output (catches "
        "reads that hid the path from the command line).",
        severity="high",
        patterns=(
            r"DevOps Bench harness environment",
            r"\b(BENCH_AGENT_TYPE|JUDGE_API_KEY|JUDGE_PROVIDER)\b",
        ),
        fields=("result", "output"),
    ),
)


def load_ruleset(path: str | None = None) -> tuple[SensitiveAccessRule, ...]:
    """Return the default rules plus any declared in a YAML rules file.

    The file holds a mapping with a ``rules`` list, each entry a
    :class:`SensitiveAccessRule` payload::

        rules:
          - id: my-task-oracle/path
            category: my-task-oracle
            material: the task's oracle solution
            severity: high
            patterns: ["solutions/oracle\\\\.ya?ml"]

    Args:
        path: Rules file to overlay on the defaults; ``None`` for defaults only.

    Returns:
        The combined ruleset, defaults first.

    Raises:
        ConfigError: If the file is missing, unparseable, holds a payload that
            fails rule validation, or reuses an ``id`` (fail-loud, matching the
            task loader).
    """
    if path is None:
        return DEFAULT_RULES
    file_path = Path(path).expanduser()
    if not file_path.is_file():
        raise ConfigError(f"cheat-detection rules file not found at {file_path}")
    try:
        parsed = _yaml.load(file_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - normalize parser errors to ConfigError
        raise ConfigError(f"failed to parse rules file {file_path}: {exc}") from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("rules"), list):
        raise ConfigError(f"rules file {file_path} must hold a mapping with a 'rules' list")
    extra: list[SensitiveAccessRule] = []
    for idx, entry in enumerate(parsed["rules"]):
        try:
            extra.append(SensitiveAccessRule.model_validate(entry))
        except Exception as exc:  # noqa: BLE001 - surface a clean ConfigError
            raise ConfigError(f"rules file {file_path}: rule {idx} is invalid: {exc}") from exc
    combined = DEFAULT_RULES + tuple(extra)
    # A duplicate id would merge two rules' findings into one published detail,
    # attributing one rule's evidence to the other's material.
    seen: set[str] = set()
    for rule in combined:
        if rule.id in seen:
            raise ConfigError(f"rules file {file_path}: duplicate rule id {rule.id!r}")
        seen.add(rule.id)
    return combined
