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

"""Contract tests over the task specs committed to this repository.

Every other test in ``tests/unit`` builds its own fixture. This module is the
exception: it reads the real ``tasks/**/task.yaml`` tree, because the defects it
exists to catch only appear in committed specs. A spec that names no provider
strands a run partway through ``tofu apply``; a spec whose ``verification_spec``
does not parse silently loses the entries it failed on; two specs sharing a name
make a leaderboard row ambiguous.

Two rules keep this file honest:

* **Never load through** :func:`~devops_bench.tasks.loader.load_from_tasks_dir`.
  That function deliberately logs and skips a spec it cannot parse, so a broken
  task simply disappears from its result and every assertion here would pass on
  a tree that cannot be run. Each spec is read and validated individually.
* **Every check has a negative test.** Assertions over two well-formed specs
  prove nothing about detection, so each rule is also fired at a deliberately
  bad input below (see ``TestChecksDetectTheDefectsTheyClaimTo``).
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from devops_bench.core import ConfigError
from devops_bench.deployers.factory import _select_provider
from devops_bench.providers import PROVIDERS
from devops_bench.tasks.loader import safe_parse_yaml
from devops_bench.tasks.schema import Task
from devops_bench.verification.spec import parse_entries

# tests/unit/tasks/test_task_specs.py -> repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_TASKS_DIR = _REPO_ROOT / "tasks"

# D-09: a task's declared verification budget may not exceed the standard run
# ceiling. Tickets 019/020 add the per-task field itself; this is the bound it
# will be checked against, kept here so there is one place to change it.
_VERIFICATION_STANDARD_SEC = 1800

# The per-task field from tickets 019/020, which has not landed yet. Both
# spellings are probed so this test starts enforcing the moment the field
# appears, rather than waiting for someone to remember to come back here.
_MINIMUM_FIELDS = ("verification_minimum_seconds", "verification_minimum")

# A well-formed check node, used by the negative tests below so that a rejection
# there is attributable to the defect under test rather than to the check node.
_VALID_CHECK = {
    "type": "resource_property",
    "kind": "deployment",
    "resource_name": "web",
    "namespace": "default",
    "path": "spec.replicas",
    "op": "eq",
    "value": 1,
}


def _spec_paths() -> list[Path]:
    """Return every committed task spec, sorted for deterministic test ids.

    Recursive, not a fixed ``*/*`` depth: the loader walks the whole tree, so a
    task nested one level deeper must be checked rather than skipped.
    """
    return sorted(_TASKS_DIR.rglob("task.yaml"))


def _spec_id(path: Path) -> str:
    """Return the ``<group>/<slug>`` label used as the parametrized test id."""
    return str(path.parent.relative_to(_TASKS_DIR))


_SPEC_PATHS = _spec_paths()
_SPEC_IDS = [_spec_id(p) for p in _SPEC_PATHS]


def _load_raw(path: Path) -> dict[str, Any]:
    """Parse one spec to a mapping, failing the test rather than skipping it."""
    raw = safe_parse_yaml(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict), f"{_spec_id(path)}: task.yaml is not a mapping"
    return raw


def _duplicates(values: list[str]) -> list[str]:
    """Return the values appearing more than once, sorted.

    Empty values are ignored: a spec that omits the field is a separate failure
    with a clearer message, and counting several omissions as "duplicates of
    each other" would bury it.
    """
    counts = Counter(v for v in values if v)
    return sorted(v for v, n in counts.items() if n > 1)


def _declared_minimum(raw: dict[str, Any]) -> int | None:
    """Return the spec's declared verification minimum, or ``None`` if absent."""
    for field in _MINIMUM_FIELDS:
        if raw.get(field) is not None:
            return int(raw[field])
    return None


def _minimum_is_required() -> bool:
    """Report whether the verification-minimum field has landed on the schema.

    Until tickets 019/020 add it, a spec that declares no minimum is legal and
    only a declared-but-oversized value is an error. Once the field exists on
    :class:`~devops_bench.tasks.schema.Task`, its absence becomes an error too —
    without an edit here.
    """
    return any(field in Task.model_fields for field in _MINIMUM_FIELDS)


def test_the_task_tree_is_not_empty():
    # Without this, a glob that stops matching (a moved tasks/ directory, a
    # renamed task.yaml) would parametrize zero cases and report a clean sweep.
    assert _SPEC_PATHS, f"no task.yaml found under {_TASKS_DIR}"


@pytest.mark.parametrize("path", _SPEC_PATHS, ids=_SPEC_IDS)
def test_spec_validates_against_the_task_schema(path: Path):
    # Task.from_dict raises on a wrong type; the directory loader would have
    # swallowed that and returned a shorter list.
    task = Task.from_dict(_load_raw(path), name_default=path.parent.name, folder=path.parent.name)
    assert task.id, f"{_spec_id(path)}: spec declares no task_id"
    assert task.name, f"{_spec_id(path)}: spec declares no name"
    assert task.prompt, f"{_spec_id(path)}: spec declares no prompt"


@pytest.mark.parametrize("path", _SPEC_PATHS, ids=_SPEC_IDS)
def test_spec_slug_matches_its_declared_name(path: Path):
    # The directory slug is what a run is invoked by and what artifacts are
    # filed under; the declared name is what a leaderboard row shows. They are
    # written in two places, so they drift unless something checks.
    slug = path.parent.name
    declared = str(_load_raw(path).get("name") or "").strip()
    assert declared == slug, (
        f"{_spec_id(path)}: directory slug {slug!r} does not match declared name {declared!r}"
    )


def test_spec_slugs_are_unique_across_groups():
    # Slugs are unique per directory by construction, but not across gcp/,
    # common/ and kind/ — and the slug alone is what run artifacts are keyed on.
    dupes = _duplicates([p.parent.name for p in _SPEC_PATHS])
    assert not dupes, f"task directory basenames used more than once: {dupes}"


def test_spec_names_are_unique():
    dupes = _duplicates([str(_load_raw(p).get("name") or "").strip() for p in _SPEC_PATHS])
    assert not dupes, f"task names used more than once: {dupes}"


def test_spec_task_ids_are_unique():
    # load_from_tasks_dir only *warns* on a duplicate id and loads both, so a
    # collision reaches scoring intact unless it is caught here.
    raws = [_load_raw(p) for p in _SPEC_PATHS]
    dupes = _duplicates([str(r.get("task_id") or r.get("id") or "").strip() for r in raws])
    assert not dupes, f"task ids used more than once: {dupes}"


@pytest.mark.parametrize("path", _SPEC_PATHS, ids=_SPEC_IDS)
def test_spec_resolves_a_known_provider(path: Path, monkeypatch):
    # INFRA_PROVIDER currently outranks the spec's own 'provider' key, so a
    # developer who has it exported would see every spec "resolve" regardless of
    # what it declares. Clear it: this test is about the committed file.
    monkeypatch.delenv("INFRA_PROVIDER", raising=False)

    infra = _load_raw(path).get("infrastructure") or {}
    if (infra.get("deployer") or "").strip().lower() == "noop":
        pytest.skip("generation-only task: declares no infrastructure to provision")

    stack = infra.get("stack") or "prebuilt/kind"
    try:
        provider = _select_provider(infra, stack)
    except ConfigError as exc:
        pytest.fail(f"{_spec_id(path)}: provider does not resolve before apply: {exc}")
    assert provider in PROVIDERS, (
        f"{_spec_id(path)}: resolved provider {provider!r} is not registered; "
        f"known: {sorted(PROVIDERS)}"
    )
    # Deliberately *not* asserting the stack directory exists. A stack may
    # legitimately live outside this repo (the deployer accepts an absolute
    # path), and a missing in-repo one already fails loudly and accurately at
    # apply time. Provider resolution is the part that has to hold *before*
    # then, because an unresolved provider is what silently picks the wrong
    # cloud or none at all.


@pytest.mark.parametrize("path", _SPEC_PATHS, ids=_SPEC_IDS)
def test_spec_verification_entries_parse_without_errors(path: Path):
    # parse_entries never raises: it drops the bad entry and reports it. A spec
    # with errors here still runs, just with fewer checks than it claims.
    entries, errors = parse_entries(_load_raw(path).get("verification_spec"))
    assert not errors, f"{_spec_id(path)}: verification_spec has parse errors: {errors}"
    assert entries, f"{_spec_id(path)}: verification_spec declares no entries"


@pytest.mark.parametrize("path", _SPEC_PATHS, ids=_SPEC_IDS)
def test_spec_verification_minimum_is_within_the_standard(path: Path):
    raw = _load_raw(path)
    declared = _declared_minimum(raw)
    if declared is None:
        if _minimum_is_required():
            pytest.fail(
                f"{_spec_id(path)}: spec declares no verification minimum; "
                f"expected one of {_MINIMUM_FIELDS}"
            )
        pytest.skip("per-task verification minimum not in the schema yet (tickets 019/020)")
    assert 0 < declared <= _VERIFICATION_STANDARD_SEC, (
        f"{_spec_id(path)}: declared verification minimum {declared}s is outside "
        f"the standard budget of {_VERIFICATION_STANDARD_SEC}s"
    )


class TestChecksDetectTheDefectsTheyClaimTo:
    """Fire each rule at a bad input, so a green run above means something.

    The committed tree is expected to be clean, which makes every assertion
    above pass whether or not the check works. These tests are the evidence
    that it does.
    """

    def test_a_repeated_name_is_reported(self):
        assert _duplicates(["deploy-hello-app", "opa-remediation", "deploy-hello-app"]) == [
            "deploy-hello-app"
        ]

    def test_distinct_names_are_not_reported(self):
        assert _duplicates(["deploy-hello-app", "opa-remediation"]) == []

    def test_omitted_names_are_not_mistaken_for_duplicates(self):
        # Two specs missing a name are two missing-name failures, not a clash.
        assert _duplicates(["", "", "opa-remediation"]) == []

    def test_a_spec_with_no_provider_line_fails_before_apply(self, monkeypatch):
        monkeypatch.delenv("INFRA_PROVIDER", raising=False)
        # A real stack name that deduces to no provider: 'opa-remediation' is
        # not one of the local, non-billable directory names.
        with pytest.raises(ConfigError, match="requires an explicit provider"):
            _select_provider({"deployer": "tofu"}, "prebuilt/opa-remediation")

    def test_a_declared_provider_resolves(self, monkeypatch):
        monkeypatch.delenv("INFRA_PROVIDER", raising=False)
        assert _select_provider({"provider": "kind"}, "prebuilt/opa-remediation") == "kind"

    def test_a_duplicate_verification_entry_name_is_an_error(self):
        entry = {"name": "workload-running", "role": "objective", "check": _VALID_CHECK}
        entries, errors = parse_entries([entry, dict(entry)])
        # The first copy loads; only the second is rejected, which is what makes
        # a duplicate name quietly halve a task's coverage instead of failing it.
        assert len(entries) == 1
        assert [e["name"] for e in errors] == ["workload-running"]

    def test_a_malformed_verification_entry_is_an_error(self):
        _, errors = parse_entries([{"name": "no-role", "check": _VALID_CHECK}])
        assert errors, "an entry missing its role should be reported, not silently dropped"

    def test_a_verification_spec_that_is_not_a_list_is_an_error(self):
        _, errors = parse_entries({"name": "not-a-list"})
        assert [e["name"] for e in errors] == ["<root>"]

    @pytest.mark.parametrize("seconds", [1801, 3600, 86_400])
    def test_a_minimum_above_the_standard_is_rejected(self, seconds: int):
        assert not 0 < seconds <= _VERIFICATION_STANDARD_SEC

    @pytest.mark.parametrize("seconds", [1, 600, 1560, 1800])
    def test_a_minimum_within_the_standard_is_accepted(self, seconds: int):
        # 1560s is the largest real requirement today (spot-rebalancing,
        # deploy-hello-app), so the bound must leave it room.
        assert 0 < seconds <= _VERIFICATION_STANDARD_SEC

    def test_the_declared_minimum_is_read_from_either_spelling(self):
        assert _declared_minimum({"verification_minimum_seconds": 900}) == 900
        assert _declared_minimum({"verification_minimum": 900}) == 900
        assert _declared_minimum({"name": "no-minimum-here"}) is None
