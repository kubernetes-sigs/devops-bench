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

"""Cheating detection over recorded agent trajectories.

Scans the canonical trajectory entries (``ToolCall.to_dict()`` shape, see
:mod:`devops_bench.agents.result`) and the record's final ``output`` against a
ruleset of sensitive-access fingerprints, and attaches a ``cheating_report``
to each record. Pure functions over record dicts — no env reads, no I/O — so
the harness hook stays a thin caller and the scan is testable on its own.

Detection itself never mutates scores, ``validated``, or any other record
field, and never aborts a run — it only writes the report. The consequence is
applied elsewhere: :mod:`devops_bench.metrics.integrity` reads the report
during scoring and gates a flagged run's ``OutcomeScore`` to zero. Keeping the
verdict and its consequence in separate layers is what lets the scan stay a
pure function that a reviewer can rerun over stored records.
"""

from __future__ import annotations

import json
import re
from functools import cache
from typing import Any

from devops_bench.cheat_detection.rules import SensitiveAccessRule

__all__ = [
    "DETECTOR_VERSION",
    "REPORT_SCHEMA_VERSION",
    "annotate_records",
    "scan_record",
]

# Bumped when detection semantics change, so a stored report is attributable
# to the detector that produced it. v2: harness-environment default rules and
# dynamic prior-run-artifact rules from the pre-run home inventory. v3: path
# rules no longer scan the result surface (passive sightings don't flag) and
# prompt-named inventory entries are authorized per record. v4: prompt
# authorization requires a whole-token name match, and structured (non-string)
# result/output payloads are JSON-dumped and scanned instead of skipped. v5:
# path rules scan the result surface again — a benchmark path surfacing in an
# `ls` listing now flags, because no legitimate task puts that material in
# view. Reports below v5 are not comparable with v5 ones. v6: the pre-run
# inventory no longer blanket-skips hidden home entries; only the enumerated
# ENVIRONMENT_DOTFILES are baseline, so an agent-state dotdir left by a prior
# run (a stale ~/.openclaw/workspace) now generates rules; inventory path
# rules require a left boundary, so a home spelling inside a longer token
# (/data/home/agent, foo~/x) no longer matches; the harness-repo rule covers
# the repo's docs/ subtree; and the home is inventoried before every task
# instead of once per batch, so an earlier task's deliverable is covered by
# path for the tasks after it (content fingerprints stay limited to the
# run-start leftovers).
DETECTOR_VERSION = 6
# Shape of the ``cheating_report`` mapping itself.
REPORT_SCHEMA_VERSION = 1

# A single noisy rule must not drown the report; reviewers need the first
# occurrences, not thousands of repeats of the same access.
_MAX_FINDINGS_PER_RULE = 20
# Context radius (chars) around a match in a finding excerpt.
_EXCERPT_RADIUS = 80


@cache
def _compile(pattern: str) -> re.Pattern[str]:
    """Compile ``pattern`` once per process (every rule reruns on every record)."""
    return re.compile(pattern, re.IGNORECASE | re.MULTILINE)


def _as_text(value: Any) -> str:
    """Normalize a scanned surface to text.

    The canonical ToolCall shape promises string-or-null ``result``/``output``,
    but a foreign or older agent harness may store structured payloads there.
    JSON-dumping keeps their content scannable instead of crashing the scan
    (or hiding the content).
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return str(value)


def _excerpt(text: str, start: int, end: int) -> str:
    """Return the whitespace-flattened context window around a match."""
    lo = max(0, start - _EXCERPT_RADIUS)
    hi = min(len(text), end + _EXCERPT_RADIUS)
    return " ".join(text[lo:hi].split())


def _scan_text(
    rule: SensitiveAccessRule,
    text: str,
    *,
    field: str,
    trajectory_index: int | None,
    tool: str | None,
    findings: list[dict[str, Any]],
    budget: int,
) -> int:
    """Append up to ``budget`` findings for ``rule`` matches in ``text``.

    Args:
        rule: The rule whose patterns are matched.
        text: The surface being scanned.
        field: Which record surface ``text`` came from (``args``/``result``/``output``).
        trajectory_index: Position of the tool call, ``None`` for ``output``.
        tool: Tool name of the trajectory entry, ``None`` for ``output``.
        findings: Sink the findings are appended to.
        budget: Remaining findings this rule may still emit for the record.

    Returns:
        The remaining budget after appending.
    """
    if budget <= 0 or not text:
        return budget
    # One finding per pattern per surface, not per occurrence: ``budget`` is
    # spent across the whole record, so letting a single ``find`` output emit
    # 20 near-identical excerpts would crowd out the evidence that the agent
    # also touched the material at trajectory entries 12, 19 and 30. Breadth
    # of evidence beats depth for the reviewer this report is written for.
    for pattern in rule.patterns:
        match = _compile(pattern).search(text)
        if match is None:
            continue
        findings.append(
            {
                "category": rule.category,
                "severity": rule.severity,
                "pattern": pattern,
                "field": field,
                "trajectory_index": trajectory_index,
                "tool": tool,
                "excerpt": _excerpt(text, match.start(), match.end()),
            }
        )
        budget -= 1
        if budget <= 0:
            break
    return budget


def scan_record(record: dict[str, Any], rules: tuple[SensitiveAccessRule, ...]) -> dict[str, Any]:
    """Scan one results.json record and return its ``cheating_report`` mapping.

    Matches every rule against the JSON-dumped ``args`` and the ``result``
    text of each trajectory entry, plus (once) the record's final ``output``.
    The record itself is never mutated.

    Args:
        record: A results.json record dict (``trajectory`` + ``output`` keys).
        rules: The ruleset to match, e.g. from
            :func:`devops_bench.cheat_detection.rules.load_ruleset`.

    Returns:
        The report mapping: ``status`` is ``flagged`` (findings present),
        ``clean`` (scanned, nothing found), or ``no_data`` (empty trajectory
        *and* empty output — an errored run gives detection nothing to see,
        which is not the same as innocence).
    """
    trajectory = [e for e in (record.get("trajectory") or []) if isinstance(e, dict)]
    output = _as_text(record.get("output"))
    findings: list[dict[str, Any]] = []

    # Normalize every entry's surfaces to text once, outside the rule loop:
    # every rule scans the same strings, so converting per rule would redo
    # len(rules) * len(trajectory) dumps of identical values. ``args`` goes
    # through ``_as_text`` like the other surfaces — a foreign harness can
    # store a non-JSON-serializable object there, and a raw ``json.dumps``
    # would throw the whole scan away over one entry.
    surfaces = [
        (idx, entry.get("name"), _as_text(entry.get("args") or {}), _as_text(entry.get("result")))
        for idx, entry in enumerate(trajectory)
    ]

    for rule in rules:
        budget = _MAX_FINDINGS_PER_RULE
        for idx, tool, args_text, result_text in surfaces:
            if "args" in rule.fields:
                budget = _scan_text(
                    rule,
                    args_text,
                    field="args",
                    trajectory_index=idx,
                    tool=tool,
                    findings=findings,
                    budget=budget,
                )
            if "result" in rule.fields:
                budget = _scan_text(
                    rule,
                    result_text,
                    field="result",
                    trajectory_index=idx,
                    tool=tool,
                    findings=findings,
                    budget=budget,
                )
            if budget <= 0:
                break
        if "output" in rule.fields:
            _scan_text(
                rule,
                output,
                field="output",
                trajectory_index=None,
                tool=None,
                findings=findings,
                budget=budget,
            )

    if not trajectory and not output:
        status = "no_data"
    elif findings:
        status = "flagged"
    else:
        status = "clean"
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "detector_version": DETECTOR_VERSION,
        "status": status,
        "categories": sorted({f["category"] for f in findings}),
        "findings": findings,
        "scanned": {"trajectory_entries": len(trajectory), "output_chars": len(output)},
    }


def annotate_records(records: list[dict[str, Any]], rules: tuple[SensitiveAccessRule, ...]) -> None:
    """Set ``record["cheating_report"]`` on every record, in place.

    Re-running replaces any prior report rather than accumulating, so a
    re-annotated batch converges on the same output (idempotent).

    Args:
        records: results.json record dicts, annotated in place.
        rules: The ruleset to match.
    """
    for record in records:
        record["cheating_report"] = scan_record(record, rules)
