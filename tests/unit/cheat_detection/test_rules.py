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

"""Tests for the cheat-detection rule model and ruleset loader."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from devops_bench.cheat_detection.rules import DEFAULT_RULES, SensitiveAccessRule, load_ruleset
from devops_bench.core import ConfigError


def test_default_rules_compile_and_cover_the_sensitive_categories() -> None:
    """Every default pattern compiles; the core categories are all present."""
    for rule in DEFAULT_RULES:
        for pattern in rule.patterns:
            re.compile(pattern)
    categories = {rule.category for rule in DEFAULT_RULES}
    assert {
        "task-definition",
        "scoring-code",
        "results-dir",
        "harness-repo",
        "upstream-github",
        "prebuilt-stack",
        "harness-environment",
    } <= categories


def test_harness_environment_rules_catch_bastion_files() -> None:
    """bench.env, matrix-runs, and runner scripts are harness material."""
    rules = [r for r in DEFAULT_RULES if r.category == "harness-environment"]
    for text in (
        "cat ~/report.md ~/policies.yaml ~/bench.env",
        "ls ~/matrix-runs/20260825_141829-12513",
        "bash ~/.matrix-runner-20260825_141829-12513.sh",
        "tar -xzf ~/.bench-sync-20260825.tgz",
    ):
        matched = [r for r in rules if any(re.search(p, text, re.IGNORECASE) for p in r.patterns)]
        # Exactly one: each bastion artifact is its own rule so the material it
        # names on a published row is the one that actually matched.
        assert len(matched) == 1, text


def test_default_rule_ids_are_unique() -> None:
    """A duplicate id would merge two rules' findings into one published verdict."""
    ids = [rule.id for rule in DEFAULT_RULES]
    assert len(ids) == len(set(ids))


def test_a_rule_without_an_id_or_material_is_rejected() -> None:
    """Fail at load, not at publication."""
    with pytest.raises(ValidationError):
        SensitiveAccessRule(id="", category="c", material="m", patterns=("x",))
    with pytest.raises(ValidationError):
        SensitiveAccessRule(id="c/x", category="c", material="  ", patterns=("x",))


def test_load_ruleset_none_returns_defaults() -> None:
    assert load_ruleset(None) == DEFAULT_RULES


def test_load_ruleset_overlays_yaml_rules_on_defaults(tmp_path: Path) -> None:
    """A rules file appends to (never replaces) the default ruleset."""
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(
        "rules:\n"
        "  - id: my-oracle/path\n"
        "    category: my-oracle\n"
        "    material: the task's oracle solution\n"
        "    severity: high\n"
        "    patterns: ['solutions/oracle\\.ya?ml']\n",
        encoding="utf-8",
    )
    combined = load_ruleset(str(rules_file))
    assert combined[: len(DEFAULT_RULES)] == DEFAULT_RULES
    assert combined[-1].category == "my-oracle"
    assert combined[-1].fields == ("args", "result", "output")


def test_load_ruleset_missing_file_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_ruleset(str(tmp_path / "nope.yaml"))


def test_load_ruleset_malformed_payload_raises_config_error(tmp_path: Path) -> None:
    """Fail-loud policy: a bad rules file must not silently fall back to defaults."""
    not_a_mapping = tmp_path / "bad_shape.yaml"
    not_a_mapping.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="'rules' list"):
        load_ruleset(str(not_a_mapping))

    bad_rule = tmp_path / "bad_rule.yaml"
    bad_rule.write_text(
        "rules:\n  - id: broken/path\n    category: broken\n    material: m\n"
        "    patterns: ['[unclosed']\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="rule 0 is invalid"):
        load_ruleset(str(bad_rule))


def test_rule_rejects_unknown_fields_and_empty_patterns() -> None:
    with pytest.raises(ValidationError, match="unknown scan fields"):
        SensitiveAccessRule(
            id="c/x", category="c", material="m", patterns=("x",), fields=("stdin",)
        )
    with pytest.raises(ValidationError, match="at least one pattern"):
        SensitiveAccessRule(id="c/x", category="c", material="m", patterns=())


def test_load_ruleset_rejects_a_duplicate_id(tmp_path: Path) -> None:
    """Two rules under one id would merge their findings into one verdict."""
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(
        "rules:\n"
        "  - id: task-definition/path\n"
        "    category: my-oracle\n"
        "    material: the task's oracle solution\n"
        "    patterns: ['solutions/oracle\\.ya?ml']\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="duplicate rule id"):
        load_ruleset(str(rules_file))
