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

"""Unit tests for verification entry parsing."""

from typing import Any

from devops_bench.verification.spec import parse_entries
from devops_bench.verification.verifiers import PodHealthyVerifier

_CHECK = {"type": "pod_healthy", "selector": "app=web", "namespace": "shop"}


def _entry(**overrides: Any) -> dict[str, Any]:
    base = {"name": "e1", "role": "objective", "check": dict(_CHECK)}
    base.update(overrides)
    return base


def test_objective_defaults_to_converge_mode() -> None:
    entries, errors = parse_entries([_entry()])
    assert errors == []
    assert entries[0].resolved_mode == "converge"
    assert entries[0].weight == 1.0
    assert entries[0].severity is None


def test_safeguard_defaults_to_assert_mode() -> None:
    entries, errors = parse_entries([_entry(role="safeguard", severity="recoverable")])
    assert errors == []
    assert entries[0].resolved_mode == "assert"


def test_explicit_mode_overrides_the_role_default() -> None:
    entries, _ = parse_entries([_entry(mode="assert")])
    assert entries[0].resolved_mode == "assert"


def test_safeguard_without_severity_is_an_error() -> None:
    entries, errors = parse_entries([_entry(role="safeguard")])
    assert entries == []
    assert errors[0]["name"] == "e1"
    assert "severity is required" in errors[0]["reason"]


def test_objective_with_severity_is_an_error() -> None:
    entries, errors = parse_entries([_entry(severity="catastrophic")])
    assert entries == []
    assert "severity is not allowed" in errors[0]["reason"]


def test_mode_hold_is_rejected_with_a_specific_message() -> None:
    entries, errors = parse_entries([_entry(mode="hold")])
    assert entries == []
    assert "not yet supported" in errors[0]["reason"]


def test_duplicate_names_keep_the_first_and_report_the_second() -> None:
    entries, errors = parse_entries([_entry(), _entry()])
    assert len(entries) == 1
    assert "duplicate" in errors[0]["reason"]


def test_a_bad_entry_does_not_discard_its_siblings() -> None:
    entries, errors = parse_entries([_entry(name="good"), _entry(name="bad", role="safeguard")])
    assert [e.name for e in entries] == ["good"]
    assert len(errors) == 1


def test_unknown_check_type_is_reported_against_the_entry_name() -> None:
    entries, errors = parse_entries([_entry(check={"type": "no_such_verifier"})])
    assert entries == []
    assert errors[0]["name"] == "e1"


def test_unknown_check_type_reason_is_a_clean_message() -> None:
    entries, errors = parse_entries([_entry(check={"type": "no_such_verifier"})])
    assert entries == []
    reason = errors[0]["reason"]
    assert "no_such_verifier" in reason
    assert "1 validation error for VerificationEntry" not in reason
    assert reason.count("errors.pydantic.dev") < 2


def test_unnamed_entry_error_is_labelled_by_index() -> None:
    entries, errors = parse_entries([{"role": "objective", "check": dict(_CHECK)}])
    assert entries == []
    assert errors[0]["name"] == "<index 0>"


def test_none_and_empty_parse_to_nothing() -> None:
    assert parse_entries(None) == ([], [])
    assert parse_entries([]) == ([], [])


def test_non_list_input_is_a_single_root_error() -> None:
    entries, errors = parse_entries({"name": "e1"})
    assert entries == []
    assert errors[0]["name"] == "<root>"
    assert "must be a list" in errors[0]["reason"]


def test_weight_must_be_positive() -> None:
    entries, errors = parse_entries([_entry(weight=0)])
    assert entries == []
    assert errors[0]["name"] == "e1"


def test_extra_keys_are_rejected() -> None:
    entries, errors = parse_entries([_entry(rolle="objective")])
    assert entries == []
    assert "rolle" in errors[0]["reason"]


def test_leaf_check_rejects_an_unknown_key() -> None:
    entries, errors = parse_entries([_entry(check={**_CHECK, "bogus_key": "x"})])
    assert entries == []
    assert errors[0]["name"] == "e1"


def test_check_is_parsed_into_a_verifier_instance() -> None:
    entries, _ = parse_entries([_entry()])
    assert isinstance(entries[0].check, PodHealthyVerifier)
    assert entries[0].check.selector == "app=web"
    assert entries[0].check.namespace == "shop"


def test_display_fields_are_accepted_and_default_to_none() -> None:
    entries, errors = parse_entries([_entry()])
    assert errors == []
    entry = entries[0]
    assert (entry.title, entry.description, entry.group, entry.failure_hint) == (
        None,
        None,
        None,
        None,
    )


def test_display_fields_are_parsed_verbatim() -> None:
    entries, errors = parse_entries(
        [
            _entry(
                title="web is healthy",
                description="Every web pod in shop is Ready.",
                group="workload",
                failure_hint="The image tag is usually wrong.",
            )
        ]
    )
    assert errors == []
    entry = entries[0]
    assert entry.title == "web is healthy"
    assert entry.description == "Every web pod in shop is Ready."
    assert entry.group == "workload"
    assert entry.failure_hint == "The image tag is usually wrong."


def test_parse_errors_carry_the_declared_role_and_severity() -> None:
    bad_check = {**_CHECK, "bogus_key": "x"}
    _, errors = parse_entries(
        [
            _entry(role="safeguard", severity="catastrophic", check=bad_check),
            _entry(name="e2", check=bad_check),
            _entry(name="e3", role=7, check=bad_check),
        ]
    )
    assert [(e["name"], e.get("role"), e.get("severity")) for e in errors] == [
        ("e1", "safeguard", "catastrophic"),
        ("e2", "objective", None),
        ("e3", None, None),  # a non-string declaration is left out, not guessed
    ]


def test_duplicate_name_error_carries_the_role_of_the_dropped_entry() -> None:
    _, errors = parse_entries([_entry(), _entry(role="safeguard", severity="recoverable")])
    assert errors == [
        {
            "name": "e1",
            "reason": "duplicate verification entry name 'e1'",
            "role": "safeguard",
            "severity": "recoverable",
        }
    ]


def test_display_fields_are_stripped() -> None:
    entries, errors = parse_entries(
        [_entry(title="  web is healthy ", description=" d ", group=" g ", failure_hint=" h ")]
    )
    assert errors == []
    entry = entries[0]
    assert (entry.title, entry.description, entry.group, entry.failure_hint) == (
        "web is healthy",
        "d",
        "g",
        "h",
    )


def test_display_fields_do_not_change_scoring_defaults() -> None:
    entries, _ = parse_entries([_entry(title="t", description="d", group="g")])
    assert entries[0].weight == 1.0
    assert entries[0].resolved_mode == "converge"
