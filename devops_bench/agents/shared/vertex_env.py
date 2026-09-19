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

"""Vertex routing env shared by the CLI agents (Gemini, antigravity).

Both harnesses resolve the same question — which Vertex location to route a
keyless run at — and drifted apart while doing it. One implementation here so
they cannot drift again. Importing this module pulls no provider SDK.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from collections.abc import Callable

__all__ = ["DEFAULT_VERTEX_LOCATION", "VERTEX_LOCATION_ENVS", "vertex_location"]

# ``global`` rather than a region: the ``-preview`` Gemini ids this benchmark
# defaults to (``gemini-3.1-pro-preview``) are only published on the global
# endpoint and return 404 from a regional one. Matches the default already used
# by devops_bench/models/{gemini,claude}.py and the claude_code harness.
DEFAULT_VERTEX_LOCATION = "global"

# Highest precedence first.
#
# ``GOOGLE_CLOUD_LOCATION`` is the native google-genai variable and stays on top
# so an operator's own export always wins.
#
# ``GCP_VERTEX_LOCATION`` is the repo-wide spelling (models/gemini.py,
# models/claude.py, the claude_code harness); it was missing from this chain, so
# an operator who set the documented variable had it silently ignored here.
#
# ``GCP_LOCATION`` is deliberately NOT read. It belongs to the deployers, which
# resolve it as a cluster **zone** — every place this repo sets it sets a zone
# (scripts/bastion/vm-setup.sh, scripts/bastion/_matrix_lib.sh,
# deployers/factory.py's ``us-central1-a`` default) and docs/components/infra.md
# documents it as such. A zone is never a valid Vertex location, so honoring it
# here could only ever route a run at an endpoint that does not exist. Operators
# who want to pin a Vertex region set ``GCP_VERTEX_LOCATION``.
VERTEX_LOCATION_ENVS = (
    "GOOGLE_CLOUD_LOCATION",
    "GCP_VERTEX_LOCATION",
)


def vertex_location(
    *,
    fallback: Callable[[], str | None] | None = None,
    default: str = DEFAULT_VERTEX_LOCATION,
) -> str:
    """Resolve the Vertex location for a keyless run.

    Args:
        fallback: Optional last-resort lookup (e.g. querying gcloud), consulted
            only when every variable in :data:`VERTEX_LOCATION_ENVS` is unset.
            Passed as a callable rather than a value so a caller whose lookup
            shells out does not pay for it on the common configured path.
        default: Value returned when neither the env chain nor ``fallback``
            yields anything.

    Returns:
        The first non-empty value from :data:`VERTEX_LOCATION_ENVS`, else
        ``fallback()``, else ``default``. Values are stripped, so a variable set
        to whitespace is treated as unset rather than routing traffic at ``" "``.
    """
    for name in VERTEX_LOCATION_ENVS:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    if fallback is not None:
        value = (fallback() or "").strip()
        if value:
            return value
    return default
