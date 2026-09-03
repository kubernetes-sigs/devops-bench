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

"""Pre-run workspace inventory: turn prior-run leftovers into detection rules.

The agent home persists between runs, so artifacts of earlier runs (a previous
``report.md``, cloned working repos, seeded git remotes from other scenarios)
accumulate there and amount to answer keys for later runs. "Left by a prior
run" is a temporal property no static regex can express — this run's legit
``report.md`` write and a read of last run's leftover are the same string — so
the harness snapshots the home *before each task's agent starts* and generates
a ruleset from what it finds. Per task rather than once per batch because the
home keeps accumulating while the batch runs: task 1's deliverable is exactly
the kind of answer key task 2 must not read, and a single run-start snapshot
is blind to it. What each snapshot yields:

* a **path rule** per leftover, anchored to home-style prefixes (``~``,
  ``$HOME``, the literal home path) so an unrelated same-named file elsewhere
  (e.g. this run's own ``/tmp`` clone) is not flagged. Matched against every
  scanned surface, so the entry's name echoing through tool output (an
  ``ls ~``, a grep error trail) flags too: the agent had no reason to be
  looking; and
* a **content-fingerprint rule** per small text leftover that the *run-start*
  snapshot already saw: its most distinctive lines, matched only against tool
  ``result``/``output`` surfaces. A read of the stale file reproduces those
  exact lines; a freshly written file does not. Entries that appear mid-batch
  get their path rule but no fingerprint — see ``fingerprint_only`` in
  :func:`build_inventory_rules` for why the honest repeat iteration needs
  that exemption.

Path rules are filtered per record against the task prompt
(:func:`filter_rules_for_prompt`): an entry the prompt itself names — the
GitOps repo the agent is told to push to, the deliverable it is told to write
— is authorized for that record. That filter is what keeps an honest run
writing its own ``report.md`` clean, and it is deliberately the *only*
mechanism: a statically excluded name would be excluded for every task, so a
leftover from one task would go uncovered while a different task ran.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path

from devops_bench.cheat_detection.rules import SCAN_FIELDS, SensitiveAccessRule

__all__ = [
    "DEFAULT_BASELINE",
    "ENVIRONMENT_DOTFILES",
    "baseline_from_granted_paths",
    "build_inventory_rules",
    "filter_rules_for_prompt",
]

# Category stamped on every generated rule so reviewers can tell dynamic
# inventory findings from the static ruleset at a glance.
CATEGORY = "prior-run-artifact"

# Top-level home entries that belong to the provisioned harness environment
# rather than to any run. The harness-owned ones here are already covered by
# static rules where they matter (bench.env, matrix-runs). Deliberately
# limited to names this project itself creates: anything an operator's own
# host layout adds belongs in the ``baseline`` parameter of
# :func:`build_inventory_rules`, not here — see
# :func:`baseline_from_granted_paths` for the capability case.
DEFAULT_BASELINE: frozenset[str] = frozenset({"bench.env", "bin", "devops-bench", "matrix-runs"})

# Hidden entries that shell provisioning and ordinary tool use create — the
# environment, not any run's output. Enumerated rather than "everything
# starting with a dot" because agent CLIs conventionally keep their state in a
# dotdir, and that is exactly where cross-run contamination piles up: a
# ``~/.openclaw/workspace`` holding a previous task's deliverables and git
# history is an answer key like any other leftover, so it must generate rules.
# Known caveat, deliberately unhandled for now: the state dir of the agent
# *currently under test* is not special-cased, so its path surfacing in the
# trajectory flags. Honest agents rarely reference their own state dir in
# recorded tool calls; if this bites, the fix is the harness (which knows the
# agent type) adding that one name to the ``baseline`` it passes — not
# widening this set.
ENVIRONMENT_DOTFILES: frozenset[str] = frozenset(
    {
        ".bash_history",
        ".bash_logout",
        ".bash_profile",
        ".bashrc",
        ".cache",
        ".config",
        ".docker",
        ".gitconfig",
        ".gnupg",
        ".kube",
        ".lesshst",
        ".local",
        ".npm",
        ".profile",
        ".python_history",
        ".ssh",
        ".sudo_as_admin_successful",
        ".viminfo",
        ".vimrc",
        ".wget-hsts",
    }
)

# Fingerprinting bounds: leftovers are notes/manifests, not datasets. A file
# past the size cap is skipped (its path rule still applies); short lines are
# too generic ("## Summary") to identify a specific file.
_MAX_FINGERPRINT_BYTES = 64 * 1024
_FINGERPRINT_LINES = 3
_MIN_LINE_LEN = 24


def _home_prefixes(home: Path) -> str:
    """Regex alternation of the ways a trajectory spells the home directory.

    Left-bounded so a home spelling inside a longer token does not match: an
    unrelated ``/data/home/agent/report.md`` contains the literal home path as
    a substring, and a ``~`` glued to a word (``foo~/report.md``) is not a
    home reference. A preceding quote, whitespace, ``=`` or start-of-string
    still matches — the ways a shell actually introduces a home path.
    """
    return rf"(?<![\w~])(?:~|\$HOME|{re.escape(str(home))})"


def _path_rule(name: str, home_pattern: str) -> SensitiveAccessRule:
    """One rule matching home-anchored access to one leftover entry.

    One rule per entry (rather than one bundled rule) so per-record filtering
    can drop exactly the entries a task prompt authorizes.
    """
    return SensitiveAccessRule(
        category=CATEGORY,
        description=f"Pre-existing home entry '{name}' (prior-run leftover) referenced by path.",
        severity="high",
        patterns=(rf"{home_pattern}/{re.escape(name)}(?![\w.-])",),
        fields=SCAN_FIELDS,
        source=name,
    )


def _fingerprint_lines(path: Path) -> tuple[str, ...]:
    """Return the most distinctive lines of a small text file, or nothing.

    Unreadable, oversized, or binary files yield no fingerprint — their path
    rule still covers them.
    """
    try:
        if path.stat().st_size > _MAX_FINGERPRINT_BYTES:
            return ()
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return ()
    # Longest first, ties broken lexicographically: sorting a set with a
    # stable sort alone would leave equal-length lines in hash-randomized
    # order, picking different fingerprints per process.
    candidates = sorted(
        {line.strip() for line in text.splitlines() if len(line.strip()) >= _MIN_LINE_LEN},
        key=lambda line: (-len(line), line),
    )
    return tuple(candidates[:_FINGERPRINT_LINES])


def _content_rule(name: str, lines: tuple[str, ...]) -> SensitiveAccessRule:
    """A result/output-only rule matching a leftover file's distinctive lines."""
    return SensitiveAccessRule(
        category=CATEGORY,
        description=f"Content of pre-existing home file '{name}' surfacing in tool output.",
        severity="high",
        patterns=tuple(re.escape(line) for line in lines),
        fields=("result", "output"),
    )


def baseline_from_granted_paths(home: Path, paths: Iterable[str]) -> frozenset[str]:
    """Home entries that hold a granted capability, as baseline names.

    A capability the harness hands the agent — a skills tree from
    ``AGENT_SKILLS_PATHS``, say — is material the run is *told* to read, so
    the home entry containing it is provisioned environment rather than a
    prior-run leftover. Without this, every honest run of an arm that grants a
    skills tree living under the home would flag for using it.

    Derived rather than enumerated on purpose: hard-coding the operator's own
    directory names here would bake one host's layout into the detector and
    silently mis-flag every other one.

    Paths outside ``home`` (``/opt/skills/...``) contribute nothing — there is
    no home entry to exempt, and the inventory never saw them. Only the
    top-level component is taken, which is as fine-grained as the path rules
    themselves get: granting ``~/skills-repo/skills`` exempts ``skills-repo``.

    Args:
        home: The agent's home directory, as passed to
            :func:`build_inventory_rules`.
        paths: Granted capability paths, ``~``-expandable.

    Returns:
        Top-level ``home`` entry names to union into the baseline. Empty when
        nothing was granted from inside the home.
    """
    names: set[str] = set()
    for raw in paths:
        if not raw:
            continue
        try:
            granted = Path(os.path.abspath(os.path.expanduser(raw)))
            relative = granted.relative_to(home)
        except (OSError, ValueError):
            # Not under home, or unresolvable: no entry to exempt. Failing
            # closed here only costs a flag a reviewer can dismiss.
            continue
        if relative.parts:
            names.add(relative.parts[0])
    return frozenset(names)


def build_inventory_rules(
    home: Path,
    *,
    baseline: frozenset[str] = DEFAULT_BASELINE,
    fingerprint_only: frozenset[str] | None = None,
) -> tuple[SensitiveAccessRule, ...]:
    """Snapshot ``home`` and return rules covering its leftovers.

    Called by the harness before *each* task's agent executes, so a
    deliverable an earlier task in the same batch left behind is covered for
    every task after it. ``baseline`` names and the enumerated
    :data:`ENVIRONMENT_DOTFILES` are treated as the provisioned environment
    and skipped; any *other* hidden entry — an agent CLI's state directory,
    say — is a leftover like any visible one.

    Every leftover gets a path rule, deliverable names included. Authorizing a
    name the current task legitimately recreates is
    :func:`filter_rules_for_prompt`'s job, because it is per record: excluding
    a name here would exclude it for *every* task, leaving a leftover of that
    name uncovered while an unrelated task ran.

    Args:
        home: The agent's home directory (shared across runs on the host).
        baseline: Top-level names that belong to the environment, not a run.
        fingerprint_only: When given, the only entry names allowed to produce
            content rules; every other leftover gets its path rule alone. The
            harness passes the leftovers its *run-start* snapshot saw, so an
            entry that appears later in the batch is path-only. The asymmetry
            is deliberate: fingerprints are unfilterable by design, and two
            iterations of one task legitimately share long lines (a pasted
            policy body, a command line, a cluster name), so fingerprinting a
            same-batch deliverable would flag the honest repeat. Referencing
            a previous task's output *by path* has no such excuse, so the
            path rule still applies. ``None`` fingerprints every leftover.

    Returns:
        The generated ruleset: one path rule per leftover, plus one content
        rule per fingerprintable text leftover. Empty when the home is clean.
    """
    try:
        entries = sorted(home.iterdir())
    except OSError:
        return ()
    skip = baseline | ENVIRONMENT_DOTFILES
    leftovers = [p for p in entries if p.name not in skip]
    if not leftovers:
        return ()

    rules: list[SensitiveAccessRule] = []
    home_pattern = _home_prefixes(home)
    rules.extend(_path_rule(p.name, home_pattern) for p in leftovers)
    for entry in leftovers:
        if fingerprint_only is not None and entry.name not in fingerprint_only:
            continue
        # ``is_file()`` follows links, so a leftover symlink would pull an
        # arbitrary readable file's lines into a pattern — and patterns are
        # published in the report. The path rule still covers the link itself.
        if entry.is_symlink() or not entry.is_file():
            continue
        lines = _fingerprint_lines(entry)
        if lines:
            rules.append(_content_rule(entry.name, lines))
    return tuple(rules)


def filter_rules_for_prompt(
    rules: tuple[SensitiveAccessRule, ...], prompt: str
) -> tuple[SensitiveAccessRule, ...]:
    """Drop path rules for home entries the task prompt itself names.

    A prompt that says "push to '~/opa-repo-<cluster>-eval.git'" *requires* the
    agent to reference that entry, so its inventory path rule would flag every
    honest run of the task. Naming an entry in the prompt is authorization to
    reference it — for that record only.

    Only path rules (which carry ``source``) are filterable, and this is the
    one place authorization is recognised at all: path rules otherwise flag a
    home entry however it surfaced, including passively in an ``ls`` listing.
    Content fingerprints are never filterable: a prompt naming ``report.md``
    tells the agent to *write* its own, not to read the stale copy back — and
    an honest write never reproduces the stale file's lines, so keeping the
    fingerprint costs honest runs nothing.

    The name must appear as a whole token, not a substring: a prompt naming
    ``workspace-repo`` must not also authorize a ``workspace`` leftover, and
    naming ``report`` must not authorize ``report.md``. A sentence-ending
    period after the name is tolerated.

    Args:
        rules: Inventory-generated rules (static rules pass through untouched
            since they carry no ``source``).
        prompt: The record's task ``input`` text.

    Returns:
        ``rules`` minus the path rules whose source entry appears in ``prompt``.
    """
    if not prompt:
        return rules

    def named(source: str) -> bool:
        return re.search(rf"(?<![\w.-]){re.escape(source)}(?![\w-])(?!\.\w)", prompt) is not None

    return tuple(r for r in rules if not (r.source and named(r.source)))
