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

"""Loading task contracts from a tasks directory or a single spec file."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from devops_bench.core import ConfigError, get_logger
from devops_bench.tasks.schema import Task

__all__ = [
    "TaskLoader",
    "FileSystemTaskLoader",
    "load_from_tasks_dir",
]

_log = get_logger("tasks.loader")

_TASK_FILE = "task.yaml"

# YAML 1.2 semantics: only ``true``/``false`` are booleans, so ``yes``/``no``/
# ``on``/``off`` parse as plain strings rather than being coerced to booleans.
_yaml = YAML(typ="safe")


def safe_parse_yaml(content: str) -> Any:
    """Parse YAML text into a Python value, treating empty documents as ``{}``.

    Args:
        content: Raw YAML text.

    Returns:
        The parsed value (typically a mapping, but possibly a list or scalar),
        or an empty dict for empty/null documents.
    """
    return _yaml.load(content) or {}


def _sort_key(task: Task) -> tuple[int, int | str]:
    """Order numeric ids by value and fall back to lexical order otherwise."""
    text = str(task.id)
    return (0, int(text)) if text.isdigit() else (1, text)


def _load_yaml_task(path: Path, name_default: str, folder: str = "") -> Task | None:
    """Read one YAML spec file into a Task, or None if it is not a mapping.

    Args:
        path: Path to the YAML spec file.
        name_default: Fallback name used when the spec omits one.
        folder: Directory name recorded on the task's :attr:`~Task.folder`.

    Returns:
        The parsed task, or ``None`` when the document is not a mapping.
    """
    content = safe_parse_yaml(path.read_text(encoding="utf-8"))
    if not isinstance(content, dict):
        return None
    return Task.from_dict(content, name_default=name_default, folder=folder)


def load_from_tasks_dir(dir_path: str) -> list[Task]:
    """Recursively load every ``task.yaml`` under a tasks directory.

    Directories are walked in sorted order for deterministic discovery, and the
    returned tasks are sorted by id (numeric ids by value). A spec that fails to
    parse is logged and skipped rather than aborting the load. A duplicate
    explicit task id is logged as a warning but still loaded, keeping directory
    loading resilient.

    Args:
        dir_path: Root directory to scan.

    Returns:
        The discovered tasks, sorted by id.

    Raises:
        ConfigError: If ``dir_path`` does not exist.
    """
    root_dir = Path(dir_path)
    if not root_dir.exists():
        raise ConfigError(f"tasks directory not found at {dir_path}")

    tasks: list[Task] = []
    seen_ids: set[str] = set()
    for current, dirs, files in root_dir.walk():
        # Sort dirs in place to ensure deterministic ordering during walk.
        dirs.sort()
        if _TASK_FILE not in files:
            continue

        yaml_path = current / _TASK_FILE
        try:
            task = _load_yaml_task(yaml_path, name_default=current.name, folder=current.name)
            if task is not None:
                if task.id and task.id in seen_ids:
                    _log.warning("duplicate task id %r at %s", task.id, yaml_path)
                elif task.id:
                    seen_ids.add(task.id)
                tasks.append(task)
        except Exception as exc:
            _log.warning("Failed to read task spec in %s: %s", yaml_path, exc)

    tasks.sort(key=_sort_key)
    return tasks


def _load_single_file(path: str) -> list[Task]:
    """Load tasks from a single ``.yaml``/``.yml``/``.json`` spec file.

    YAML files yield a single task; JSON files may hold a single object or a
    list of objects. A YAML document or JSON payload that is neither a mapping
    nor (for JSON) a list yields no tasks.

    Args:
        path: Path to the spec file.

    Returns:
        The loaded tasks.

    Raises:
        ConfigError: If the file cannot be parsed or holds a malformed payload.
            Unlike directory loads (which log and skip), single-file loads
            always surface a clean ``ConfigError``.
    """
    spec = Path(path)
    # A ``<task-dir>/task.yaml`` stem is just "task", so use the parent dir name
    # for it (mirroring the directory loader); the parallel matrix loads one
    # task.yaml per process. Other single specs fall back to the file stem.
    base = spec.parent.name if spec.name == _TASK_FILE else spec.stem
    name_default = base
    folder = base

    try:
        if spec.suffix in (".yaml", ".yml"):
            task = _load_yaml_task(spec, name_default=name_default, folder=folder)
            return [task] if task is not None else []

        raw = json.loads(spec.read_text(encoding="utf-8"))

        if isinstance(raw, dict):
            return [Task.from_dict(raw, name_default=name_default, folder=folder)]
        if isinstance(raw, list):
            tasks: list[Task] = []
            for idx, item in enumerate(raw):
                if not isinstance(item, dict):
                    raise ConfigError(f"task spec {path}: JSON list element {idx} is not an object")
                tasks.append(Task.from_dict(item, name_default=name_default, folder=folder))
            return tasks
        return []
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"failed to load task spec {path}: {exc}") from exc


class TaskLoader(ABC):
    """Loads task contracts from a source such as a directory or file."""

    @abstractmethod
    def load_tasks(self, source: str) -> list[Task]:
        """Load and parse tasks from the given source.

        Args:
            source: Location to load from (e.g. a directory or spec file path).

        Returns:
            The loaded tasks.
        """


class FileSystemTaskLoader(TaskLoader):
    """Loads tasks from a directory tree or a single YAML/JSON spec file."""

    def load_tasks(self, source: str) -> list[Task]:
        """Load tasks from a directory or a single spec file.

        A directory is scanned recursively; a file is parsed as YAML
        (``.yaml``/``.yml``) or JSON, where JSON may hold a single object or a
        list of objects.

        Args:
            source: A tasks directory or a single spec file.

        Returns:
            The loaded tasks; directory sources are sorted by id.

        Raises:
            ConfigError: If ``source`` does not exist.
        """
        spec = Path(source)
        if spec.is_dir():
            return load_from_tasks_dir(source)
        if not spec.exists():
            raise ConfigError(f"task spec not found at {source}")
        return _load_single_file(source)
