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

"""Unit tests for devops_bench.tasks.loader using real temp directories."""

import json
import logging
from pathlib import Path

import pytest

from devops_bench.core.errors import ConfigError
from devops_bench.tasks.loader import (
    FileSystemTaskLoader,
    TaskLoader,
    load_from_tasks_dir,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_load_from_tasks_dir_recursive_and_ordered(tmp_path):
    # id 2 lives under a directory that sorts before id 1, so a correct
    # loader must sort by id rather than discovery order.
    _write(
        tmp_path / "aaa" / "task-two" / "task.yaml",
        'task_id: 2\nname: "task-two"\nprompt: "Two"\nexpected_output: "E2"\n',
    )
    _write(
        tmp_path / "zzz" / "task-one" / "task.yaml",
        'task_id: 1\nname: "task-one"\nprompt: "One"\nexpected_output: "E1"\n',
    )

    tasks = FileSystemTaskLoader().load_tasks(str(tmp_path))
    assert len(tasks) == 2
    assert tasks[0].id == "1"
    assert tasks[0].name == "task-one"
    assert tasks[1].id == "2"
    assert tasks[1].name == "task-two"


def test_numeric_ids_sort_by_value_not_lexically(tmp_path):
    _write(tmp_path / "a" / "task.yaml", 'task_id: 10\nname: "ten"\n')
    _write(tmp_path / "b" / "task.yaml", 'task_id: 2\nname: "two"\n')

    tasks = load_from_tasks_dir(str(tmp_path))
    assert [t.name for t in tasks] == ["two", "ten"]


def test_load_from_tasks_dir_subdir_scope(tmp_path):
    _write(
        tmp_path / "gcp" / "task-gcp" / "task.yaml",
        'task_id: 1\nname: "task-gcp"\nprompt: "GCP"\nexpected_output: "G"\n',
    )
    _write(
        tmp_path / "generic" / "task-generic" / "task.yaml",
        'task_id: 2\nname: "task-generic"\nprompt: "Generic"\nexpected_output: "X"\n',
    )

    scoped = load_from_tasks_dir(str(tmp_path / "generic"))
    assert len(scoped) == 1
    assert scoped[0].name == "task-generic"


def test_field_defaults_missing_id_and_name(tmp_path):
    # No task_id and no name -> empty id and the directory basename.
    _write(
        tmp_path / "the-dir-name" / "task.yaml",
        'prompt: "  padded prompt  "\nexpected_output: "  padded  "\n',
    )

    tasks = load_from_tasks_dir(str(tmp_path))
    assert len(tasks) == 1
    assert tasks[0].id == ""
    assert tasks[0].name == "the-dir-name"
    # folder is the directory holding task.yaml, independent of the name field.
    assert tasks[0].folder == "the-dir-name"
    assert tasks[0].prompt == "padded prompt"
    assert tasks[0].expected_output == "padded"


def test_folder_is_task_directory_not_name(tmp_path):
    # An explicit name does not change folder; folder tracks the directory.
    _write(
        tmp_path / "group" / "task_001" / "task.yaml",
        'task_id: 7\nname: "Human Readable"\nprompt: "p"\n',
    )
    tasks = load_from_tasks_dir(str(tmp_path))
    assert tasks[0].name == "Human Readable"
    assert tasks[0].folder == "task_001"


def test_goal_alias_in_dir_load(tmp_path):
    _write(
        tmp_path / "alias" / "task.yaml",
        'task_id: 5\ngoal: "  goal driven  "\n',
    )
    tasks = load_from_tasks_dir(str(tmp_path))
    assert tasks[0].prompt == "goal driven"


_DOC_YAML = """\
task_id: 1
name: "doc-task"
prompt: "p"
expected_output: "e"
documentation:
  - doc_name: "Guide A"
    url: "https://example.com/a"
    constraints:
      - text: "Must use TLS"
        critical: true
      - text: "Prefer caching"
        critical: false
  - doc_name: "Guide B"
    url: "https://example.com/b"
    constraints:
      - text: "Optional thing"
"""


def test_documentation_parsed_on_load(tmp_path):
    _write(tmp_path / "doc" / "task.yaml", _DOC_YAML)
    docs = load_from_tasks_dir(str(tmp_path))[0].documentation
    assert len(docs) == 2

    assert docs[0].doc_name == "Guide A"
    assert docs[0].url == "https://example.com/a"
    assert [(c.text, c.critical) for c in docs[0].constraints] == [
        ("Must use TLS", True),
        ("Prefer caching", False),
    ]

    assert docs[1].doc_name == "Guide B"
    assert docs[1].url == "https://example.com/b"
    # A constraint without an explicit critical flag defaults to False.
    assert [(c.text, c.critical) for c in docs[1].constraints] == [("Optional thing", False)]


def test_invalid_task_is_skipped_with_warning(tmp_path, caplog):
    # Under YAML 1.2 ``critical: yes`` is the string "yes"; the strict schema
    # rejects it, so the task is skipped with a warning rather than loaded.
    yaml_text = (
        "task_id: 1\n"
        'name: "n"\n'
        "documentation:\n"
        '  - doc_name: "A"\n'
        "    constraints:\n"
        '      - text: "x"\n'
        "        critical: yes\n"
    )
    _write(tmp_path / "d" / "task.yaml", yaml_text)
    with caplog.at_level(logging.WARNING, logger="devops_bench.tasks.loader"):
        tasks = load_from_tasks_dir(str(tmp_path))
    assert tasks == []
    assert any("Failed to read task spec" in rec.message for rec in caplog.records)


def test_yaml_1_2_booleans_stay_strings(tmp_path):
    # ``yes``/``no``/``off`` are plain strings under YAML 1.2; only
    # ``true``/``false`` are booleans.
    _write(
        tmp_path / "t" / "task.yaml",
        'task_id: 1\ninfrastructure:\n  a: yes\n  b: "no"\n  c: true\n',
    )
    infra = load_from_tasks_dir(str(tmp_path))[0].infrastructure
    assert infra["a"] == "yes"
    assert infra["b"] == "no"
    assert infra["c"] is True


def test_load_single_yaml_file(tmp_path):
    path = tmp_path / "case.yaml"
    _write(path, 'task_id: 11\nname: "single"\nprompt: "  hi  "\nexpected_output: "out"\n')
    tasks = FileSystemTaskLoader().load_tasks(str(path))
    assert len(tasks) == 1
    assert tasks[0].id == "11"
    assert tasks[0].name == "single"
    # A single spec file has no task directory; folder falls back to the stem.
    assert tasks[0].folder == "case"
    assert tasks[0].prompt == "hi"


def test_single_task_yaml_file_folder_is_parent_dir(tmp_path):
    # Loading a single ``<task-dir>/task.yaml`` (how the parallel matrix runs one
    # task per process) must report the parent directory as the folder, not the
    # literal ``"task"`` stem — mirroring the directory loader.
    path = tmp_path / "secret-rotation" / "task.yaml"
    _write(path, 'task_id: 7\nprompt: "p"\n')
    tasks = FileSystemTaskLoader().load_tasks(str(path))
    assert len(tasks) == 1
    assert tasks[0].folder == "secret-rotation"
    # name also falls back to the parent dir (not "task") when the spec omits one.
    assert tasks[0].name == "secret-rotation"


def test_load_single_json_file_object_with_goal_alias(tmp_path):
    path = tmp_path / "case.json"
    _write(
        path,
        json.dumps({"task_id": 4, "name": "json-case", "goal": "json goal"}),
    )
    tasks = FileSystemTaskLoader().load_tasks(str(path))
    assert len(tasks) == 1
    assert tasks[0].id == "4"
    assert tasks[0].name == "json-case"
    assert tasks[0].prompt == "json goal"


def test_load_single_json_file_list(tmp_path):
    path = tmp_path / "cases.json"
    _write(
        path,
        json.dumps(
            [
                {"task_id": 1, "name": "a", "input": "ia"},
                {"task_id": 2, "name": "b", "goal": "gb"},
            ]
        ),
    )
    tasks = FileSystemTaskLoader().load_tasks(str(path))
    assert [t.name for t in tasks] == ["a", "b"]
    assert tasks[0].prompt == "ia"
    assert tasks[1].prompt == "gb"


def test_single_file_malformed_yaml_raises_config_error(tmp_path):
    # A single malformed YAML spec surfaces a clean ConfigError rather than
    # leaking the underlying parser error.
    path = tmp_path / "broken.yaml"
    _write(path, "[unterminated")
    with pytest.raises(ConfigError):
        FileSystemTaskLoader().load_tasks(str(path))


def test_single_file_json_list_non_dict_element_raises_config_error(tmp_path):
    # A JSON list whose elements are not all objects is rejected with a clean
    # ConfigError instead of crashing inside Task.from_dict.
    path = tmp_path / "cases.json"
    _write(path, json.dumps([{"task_id": 1}, 5]))
    with pytest.raises(ConfigError):
        FileSystemTaskLoader().load_tasks(str(path))


def test_missing_directory_raises_config_error(tmp_path):
    missing = tmp_path / "definitely-does-not-exist-xyz-123"
    with pytest.raises(ConfigError):
        load_from_tasks_dir(str(missing))


def test_missing_directory_via_loader_raises_config_error(tmp_path):
    missing = tmp_path / "definitely-does-not-exist-xyz-456"
    with pytest.raises(ConfigError):
        FileSystemTaskLoader().load_tasks(str(missing))


def test_parse_error_is_logged_and_skipped(tmp_path, caplog):
    # A valid task plus one with malformed YAML; the bad one is skipped.
    _write(
        tmp_path / "good" / "task.yaml",
        'task_id: 1\nname: "good"\nprompt: "p"\nexpected_output: "e"\n',
    )
    _write(
        tmp_path / "bad" / "task.yaml",
        "task_id: 2\nname: [unterminated\n",
    )

    with caplog.at_level(logging.WARNING, logger="devops_bench.tasks.loader"):
        tasks = load_from_tasks_dir(str(tmp_path))

    assert [t.name for t in tasks] == ["good"]
    assert any("Failed to read task spec" in rec.message for rec in caplog.records)


def test_duplicate_task_id_logs_warning(tmp_path, caplog):
    # Two task.yaml with the same explicit task_id: the duplicate is logged as
    # a warning but both are still loaded (directory loading stays resilient).
    _write(
        tmp_path / "first" / "task.yaml",
        'task_id: 7\nname: "first"\nprompt: "p"\n',
    )
    _write(
        tmp_path / "second" / "task.yaml",
        'task_id: 7\nname: "second"\nprompt: "q"\n',
    )

    with caplog.at_level(logging.WARNING, logger="devops_bench.tasks.loader"):
        tasks = load_from_tasks_dir(str(tmp_path))

    assert len(tasks) == 2
    assert any("duplicate task id" in rec.message for rec in caplog.records)


def test_filesystem_task_loader_is_a_task_loader():
    assert isinstance(FileSystemTaskLoader(), TaskLoader)


def test_task_loader_cannot_be_instantiated():
    with pytest.raises(TypeError):
        TaskLoader()


def test_filesystem_task_loader_loads_directory(tmp_path):
    _write(tmp_path / "t" / "task.yaml", 'task_id: 1\nname: "t"\nprompt: "p"\n')
    tasks = FileSystemTaskLoader().load_tasks(str(tmp_path))
    assert [t.name for t in tasks] == ["t"]
