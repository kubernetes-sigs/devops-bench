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

"""Home-relative paths a task prompt promises the agent.

A leaf module: the harness and the cheat detector both read these paths, and
the detector must not import the harness package.
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = ["carries_cluster_token", "prompt_fixture_paths"]

# ``~/<name>`` or ``$HOME/<name>`` in prompt prose.
_HOME_PATH = re.compile(r"[~]/([\w.\-]+)|\$HOME/([\w.\-]+)")

# Sentence punctuation stripped from the tail of a captured name.
_TRAILING_PUNCT = ".,;:!?'\"`)"


def carries_cluster_token(name: str, cluster_name: str | None) -> bool:
    """True when ``name`` holds ``cluster_name`` as a ``-``/``_``/``.``-delimited token.

    The convention stacks use to name what they seed for a run; sandbox fixture
    discovery applies the same boundary rule.
    """
    if not cluster_name:
        return False
    return re.search(rf"(^|[-_.]){re.escape(cluster_name)}([-_.]|$)", name) is not None


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
