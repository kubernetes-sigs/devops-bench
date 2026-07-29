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

"""Unit tests for entry-level mode dispatch."""

from typing import Any, Literal

from devops_bench.verification.base import VERIFIERS, BaseVerifier, VerificationResult
from devops_bench.verification.runner import VerifierAgent
from devops_bench.verification.spec import VerificationEntry, parse_entries


@VERIFIERS.register("counting")
class _Counting(BaseVerifier):
    """Test double recording every budget it was called with."""

    type: Literal["counting"]
    budgets: list[float] = []

    def verify(self, timeout_sec: float) -> VerificationResult:
        self.budgets.append(timeout_sec)
        return VerificationResult(success=False, elapsed_time=0.0, reason="stub", name=self.name)


def _entry(role: str, **extra: Any) -> VerificationEntry:
    payload = {"name": "e", "role": role, "check": {"type": "counting", "budgets": []}}
    payload.update(extra)
    entries, errors = parse_entries([payload])
    assert errors == []
    return entries[0]


def test_assert_mode_evaluates_once_with_a_zero_budget() -> None:
    entry = _entry("safeguard", severity="recoverable")
    VerifierAgent().run_entry(entry, timeout_sec=30)
    assert entry.check.budgets == [0.0]


def test_converge_mode_passes_the_remaining_budget_down() -> None:
    entry = _entry("objective")
    VerifierAgent().run_entry(entry, timeout_sec=30)
    assert len(entry.check.budgets) == 1
    assert 25 < entry.check.budgets[0] <= 30


def test_an_explicit_assert_on_an_objective_is_honoured() -> None:
    entry = _entry("objective", mode="assert")
    VerifierAgent().run_entry(entry, timeout_sec=30)
    assert entry.check.budgets == [0.0]


def test_assert_mode_recurses_into_a_combinator() -> None:
    entries, errors = parse_entries(
        [
            {
                "name": "e",
                "role": "safeguard",
                "severity": "catastrophic",
                "check": {
                    "type": "none",
                    "checks": [
                        {"type": "counting", "budgets": []},
                        {"type": "counting", "budgets": []},
                    ],
                },
            }
        ]
    )
    assert errors == []
    result = VerifierAgent().run_entry(entries[0], timeout_sec=30)
    assert result.success is True
    for child in entries[0].check.checks:
        assert child.budgets == [0.0]


def test_wait_for_condition_behaviour_is_unchanged() -> None:
    agent = VerifierAgent()
    node = {"type": "counting", "budgets": []}
    result = agent.wait_for_condition(node, timeout_sec=10)
    assert result.success is False


def test_run_entry_returns_the_check_result() -> None:
    entry = _entry("objective")
    result = VerifierAgent().run_entry(entry, timeout_sec=5)
    assert result.success is False
    assert isinstance(result, VerificationResult)
