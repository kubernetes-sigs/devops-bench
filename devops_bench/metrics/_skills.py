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

"""Load judge skill markdown packaged under ``devops_bench/skills``."""

from __future__ import annotations

from importlib import resources

__all__ = ["SKILLS_PACKAGE", "load_skill_text"]

# Package (not a filesystem path) holding the judge skill markdown as package
# data, so the files resolve under ``pip install`` / wheels, not just the repo.
SKILLS_PACKAGE = "devops_bench.skills"


def load_skill_text(filename: str) -> str:
    """Read a judge skill markdown file from the ``devops_bench.skills`` package.

    Args:
        filename: Skill file name, e.g. ``"outcome-validity-checklist.md"``.

    Returns:
        The full markdown text used as a GEval criteria.

    Raises:
        FileNotFoundError: If the skill file is not present in the package.
    """
    resource = resources.files(SKILLS_PACKAGE) / filename
    if not resource.is_file():
        raise FileNotFoundError(f"Judge skill {filename!r} not found in package {SKILLS_PACKAGE!r}")
    return resource.read_text(encoding="utf-8")
