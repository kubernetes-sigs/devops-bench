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

"""Pre-flight check that the home fixtures a prompt promises reached the agent."""

from __future__ import annotations

import os
import re
from pathlib import Path

from devops_bench.core import get_bool, get_logger

__all__ = ["REQUIRE_FIXTURES_ENV", "check_prompt_fixtures", "prompt_fixture_paths"]

_log = get_logger("evalharness.fixtures")

#: ``0`` downgrades a missing fixture to a warning, for a deliberately degraded arm.
REQUIRE_FIXTURES_ENV = "BENCH_REQUIRE_FIXTURES"

# ``~/<name>`` or ``$HOME/<name>`` in prompt prose.
_HOME_PATH = re.compile(r"[~]/([\w.\-]+)|\$HOME/([\w.\-]+)")

# Sentence punctuation stripped from the tail of a captured name.
_TRAILING_PUNCT = ".,;:!?'\"`)"


def prompt_fixture_paths(prompt: str, home: Path | None = None) -> list[Path]:
    """Extract the home-relative fixture paths a prompt promises the agent.

    Args:
        prompt: The fully placeholder-substituted prompt text.
        home: Home directory to resolve against; defaults to the current
            process's, which is the agent's when it runs unsandboxed.

    Returns:
        Absolute paths, de-duplicated, in first-mention order.
    """
    base = home or Path.home()
    seen: dict[str, Path] = {}
    for match in _HOME_PATH.finditer(prompt):
        name = (match.group(1) or match.group(2) or "").rstrip(_TRAILING_PUNCT)
        if not name or name in seen:
            continue
        if prompt[match.end() : match.end() + 2] == "{{":
            # Unsubstituted placeholder: the name is a truncated prefix.
            continue
        seen[name] = base / name
    return list(seen.values())


def _unreadable_reason(path: Path) -> str | None:
    """Return why ``path`` is unusable as a fixture (for this process), or ``None``."""
    if not path.exists():
        return "does not exist"
    if not os.access(path, os.R_OK):
        return "exists but is not readable by this user"
    if path.is_dir() and not os.access(path, os.X_OK):
        return "exists but is not traversable by this user"
    return None


def check_prompt_fixtures(
    prompt: str,
    task_name: str,
    home: Path | None = None,
    *,
    mounted: bool = False,
) -> list[str]:
    """Fail loudly when a promised fixture did not reach the agent.

    Args:
        prompt: The substituted prompt handed to the agent.
        task_name: Task name, for the message.
        home: Home directory the prompt's ``~`` resolves to for the agent.
        mounted: ``True`` when a sandbox mount plan already carried this run's
            fixtures into the container, in which case the host-side paths say
            nothing about what the agent can see and the check is skipped.

    Returns:
        One human-readable problem per unusable fixture; empty when every
        promised path is present and readable, or when there are none.

    Raises:
        RuntimeError: When a fixture is unusable and
            :data:`REQUIRE_FIXTURES_ENV` is not set to a false value.
    """
    if mounted:
        return []
    problems = [
        f"{path} ({reason})"
        for path in prompt_fixture_paths(prompt, home)
        if (reason := _unreadable_reason(path)) is not None
    ]
    if not problems:
        return []

    detail = "; ".join(problems)
    message = (
        f"task {task_name!r} promises the agent {len(problems)} home fixture(s) "
        f"it cannot read: {detail}. The stack did not seed them where this "
        f"agent looks, so the agent would hunt the filesystem and be graded on "
        f"whatever it could reconstruct. Fix the stack's seeding, or set "
        f"{REQUIRE_FIXTURES_ENV}=0 to run the task without its input on purpose."
    )
    if not get_bool(REQUIRE_FIXTURES_ENV, True):
        _log.warning("%s (continuing: %s is off)", message, REQUIRE_FIXTURES_ENV)
        return problems
    raise RuntimeError(message)
