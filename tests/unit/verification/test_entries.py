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


def test_mode_hold_parses() -> None:
    entries, errors = parse_entries([_entry(mode="hold", hold_window_sec=30.0)])
    assert errors == []
    assert entries[0].resolved_mode == "hold"


def test_hold_poll_interval_defaults_to_none() -> None:
    entries, errors = parse_entries([_entry(mode="hold", hold_window_sec=30.0)])
    assert errors == []
    assert entries[0].hold_poll_interval_sec is None


def test_hold_poll_interval_accepts_an_explicit_value() -> None:
    entries, errors = parse_entries(
        [_entry(mode="hold", hold_poll_interval_sec=2.5, hold_window_sec=30.0)]
    )
    assert errors == []
    assert entries[0].hold_poll_interval_sec == 2.5


def test_hold_poll_interval_must_be_positive() -> None:
    entries, errors = parse_entries(
        [_entry(mode="hold", hold_poll_interval_sec=0, hold_window_sec=30.0)]
    )
    assert entries == []
    assert errors[0]["name"] == "e1"


def test_hold_poll_interval_rejects_infinity() -> None:
    entries, errors = parse_entries(
        [_entry(mode="hold", hold_poll_interval_sec=float("inf"), hold_window_sec=30.0)]
    )
    assert entries == []
    assert "finite" in errors[0]["reason"]


def test_hold_poll_interval_rejects_nan() -> None:
    entries, errors = parse_entries(
        [_entry(mode="hold", hold_poll_interval_sec=float("nan"), hold_window_sec=30.0)]
    )
    assert entries == []
    assert "finite" in errors[0]["reason"]


def test_hold_poll_interval_sec_is_rejected_on_an_assert_entry() -> None:
    entries, errors = parse_entries(
        [_entry(role="safeguard", severity="recoverable", mode="assert", hold_poll_interval_sec=5)]
    )
    assert entries == []
    assert "hold_poll_interval_sec" in errors[0]["reason"]


def test_hold_window_sec_is_rejected_on_an_assert_entry() -> None:
    entries, errors = parse_entries(
        [_entry(role="safeguard", severity="recoverable", mode="assert", hold_window_sec=30.0)]
    )
    assert entries == []
    assert "hold_window_sec" in errors[0]["reason"]


def test_resolved_mode_never_derives_hold_from_role_defaults() -> None:
    entries, errors = parse_entries([_entry()])
    assert errors == []
    assert entries[0].resolved_mode != "hold"
    entries, errors = parse_entries([_entry(role="safeguard", severity="recoverable")])
    assert errors == []
    assert entries[0].resolved_mode != "hold"


def test_objective_hold_without_window_is_an_error() -> None:
    entries, errors = parse_entries([_entry(mode="hold")])
    assert entries == []
    assert "hold_window_sec is required" in errors[0]["reason"]


def test_objective_hold_with_window_parses() -> None:
    entries, errors = parse_entries([_entry(mode="hold", hold_window_sec=30.0)])
    assert errors == []
    assert entries[0].hold_window_sec == 30.0


def test_hold_window_sec_rejects_infinity() -> None:
    entries, errors = parse_entries([_entry(mode="hold", hold_window_sec=float("inf"))])
    assert entries == []
    assert "finite" in errors[0]["reason"]


def test_hold_window_sec_rejects_nan() -> None:
    entries, errors = parse_entries([_entry(mode="hold", hold_window_sec=float("nan"))])
    assert entries == []
    assert "finite" in errors[0]["reason"]


def test_safeguard_hold_with_window_is_an_error() -> None:
    entries, errors = parse_entries(
        [_entry(role="safeguard", severity="catastrophic", mode="hold", hold_window_sec=30.0)]
    )
    assert entries == []
    assert "hold_window_sec is not allowed" in errors[0]["reason"]


def test_safeguard_hold_without_window_parses() -> None:
    entries, errors = parse_entries(
        [_entry(role="safeguard", severity="catastrophic", mode="hold")]
    )
    assert errors == []
    assert entries[0].hold_window_sec is None


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
