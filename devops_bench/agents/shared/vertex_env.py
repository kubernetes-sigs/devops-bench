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

Both harnesses resolve the same questions — which Vertex project and location
to route a keyless run at — and drifted apart while doing it. One
implementation here so they cannot drift again. Importing this module pulls no
provider SDK.
"""

from __future__ import annotations

import os

__all__ = [
    "DEFAULT_VERTEX_LOCATION",
    "VERTEX_LOCATION_ENVS",
    "VERTEX_PROJECT_ENVS",
    "vertex_location",
    "vertex_project",
]

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
# here could only ever route a run at an endpoint that does not exist. The same
# goes for gcloud's ``compute/region``: a Compute setting, not a Vertex one, and
# a regional value 404s the ``-preview`` ids. Operators who want to pin a Vertex
# region set ``GCP_VERTEX_LOCATION``.
VERTEX_LOCATION_ENVS = (
    "GOOGLE_CLOUD_LOCATION",
    "GCP_VERTEX_LOCATION",
)

# Highest precedence first: the native google-genai variable, then the
# repo-wide ``GCP_PROJECT_ID`` (models/gemini.py, models/claude.py,
# providers/gcp.py, the claude_code harness), then the legacy ``GCP_PROJECT``
# these harnesses have always read.
VERTEX_PROJECT_ENVS = (
    "GOOGLE_CLOUD_PROJECT",
    "GCP_PROJECT_ID",
    "GCP_PROJECT",
)


def _first_set(names: tuple[str, ...]) -> str | None:
    """Return the first non-blank value among ``names``, stripped."""
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


def vertex_location(*, default: str = DEFAULT_VERTEX_LOCATION) -> str:
    """Resolve the Vertex location for a keyless run.

    Args:
        default: Value returned when no variable in the chain is set.

    Returns:
        The first non-empty value from :data:`VERTEX_LOCATION_ENVS`, else
        ``default``. Values are stripped, so a variable set to whitespace is
        treated as unset rather than routing traffic at ``" "``.
    """
    return _first_set(VERTEX_LOCATION_ENVS) or default


def vertex_project() -> str | None:
    """Resolve the Vertex project for a keyless run.

    Returns:
        The first non-empty value from :data:`VERTEX_PROJECT_ENVS` (stripped),
        or ``None`` when none is set. There is no default: a project is
        account-specific, so the caller decides whether to fall back further or
        fail.
    """
    return _first_set(VERTEX_PROJECT_ENVS)
