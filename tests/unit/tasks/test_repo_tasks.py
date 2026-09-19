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

"""Every task shipped under ``tasks/`` must load and parse cleanly.

The directory loader logs and skips a task whose spec fails validation, so a
schema rule that starts rejecting a shipped task would drop it from the matrix
silently. This suite is where that becomes a failing check instead.
"""

from pathlib import Path

import pytest

from devops_bench.tasks.loader import _load_yaml_task, load_from_tasks_dir
from devops_bench.verification.spec import parse_entries

_TASKS_DIR = Path(__file__).resolve().parents[3] / "tasks"
# Recursive, like the directory loader, so a task at any depth is covered.
_TASK_FILES = sorted(_TASKS_DIR.rglob("task.yaml"))


@pytest.mark.parametrize("task_file", _TASK_FILES, ids=lambda p: p.parent.name)
def test_shipped_task_loads_and_its_entries_parse(task_file: Path) -> None:
    # Raises on a schema violation, unlike the directory loader.
    task = _load_yaml_task(
        task_file, name_default=task_file.parent.name, folder=task_file.parent.name
    )
    assert task is not None
    _, errors = parse_entries(task.verification_spec)
    assert errors == []


def test_directory_loader_drops_no_shipped_task() -> None:
    # Sorted lists, not sets: two tasks sharing a basename must both survive.
    loaded = sorted(task.folder for task in load_from_tasks_dir(str(_TASKS_DIR)))
    assert loaded == sorted(p.parent.name for p in _TASK_FILES)
