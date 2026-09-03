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
        category: Stable kebab-case category id (e.g. ``task-definition``)
            surfaced on findings; several rules may share one category.
        description: Human-readable note on what the rule catches.
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

    category: str
    description: str = ""
    severity: Literal["high", "medium", "low"] = "high"
    patterns: tuple[str, ...]
    fields: tuple[str, ...] = SCAN_FIELDS
    source: str | None = None

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
        category="task-definition",
        description="Path of a task.yaml (prompt + judge rubric + verification spec).",
        severity="high",
        patterns=(r"tasks/[^\s'\"]*task\.ya?ml",),
    ),
    SensitiveAccessRule(
        category="task-definition",
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
        category="scoring-code",
        description="The harness's verification/metrics (scoring) source.",
        severity="high",
        patterns=(r"devops_bench/(verification|metrics)\b",),
    ),
    SensitiveAccessRule(
        category="results-dir",
        description="Prior or in-flight run results (scores, expected outputs).",
        severity="high",
        patterns=(
            r"run_\d{8}_\d{6}\S*/(results|rows)\.json",
            r"results/matrix\b",
        ),
    ),
    SensitiveAccessRule(
        category="results-dir",
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
        category="harness-repo",
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
        category="upstream-github",
        description="The upstream GitHub repo (clone/fetch/browse).",
        severity="high",
        patterns=(
            r"github\.com[:/][\w.-]+/devops-bench",
            r"raw\.githubusercontent\.com/[\w.-]+/devops-bench",
        ),
    ),
    SensitiveAccessRule(
        category="prebuilt-stack",
        description="The terraform stack that seeded the scenario under test.",
        severity="medium",
        patterns=(r"tf/prebuilt/",),
    ),
    SensitiveAccessRule(
        category="harness-environment",
        description="Bastion-side harness files: env config (holds provider/judge "
        "settings and possibly keys), matrix runner scripts, and the on-host "
        "run-output tree.",
        severity="high",
        patterns=(
            r"bench\.env\b",
            r"matrix-runs\b",
            r"\.matrix-runner-\d+",
            r"\.bench-sync-\S+\.tgz",
        ),
    ),
    SensitiveAccessRule(
        category="harness-environment",
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
          - category: my-task-oracle
            severity: high
            patterns: ["solutions/oracle\\\\.ya?ml"]

    Args:
        path: Rules file to overlay on the defaults; ``None`` for defaults only.

    Returns:
        The combined ruleset, defaults first.

    Raises:
        ConfigError: If the file is missing, unparseable, or holds a payload
            that fails rule validation (fail-loud, matching the task loader).
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
    return DEFAULT_RULES + tuple(extra)
