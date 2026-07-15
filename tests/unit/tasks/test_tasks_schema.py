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

"""Unit tests for devops_bench.tasks.schema."""

import pytest
from pydantic import ValidationError

from devops_bench.tasks.schema import Task


def test_from_dict_full():
    raw = {
        "task_id": 7,
        "name": "explicit-name",
        "prompt": "  do the thing  ",
        "expected_output": "  done  ",
        "retrieval_context": ["ctx"],
        "chaos_spec": {"kind": "kill"},
        "verification_spec": {"check": "ok"},
        "infrastructure": {"deployer": "tofu"},
    }
    task = Task.from_dict(raw, name_default="dir-name")

    assert task.id == "7"
    assert task.name == "explicit-name"
    assert task.prompt == "do the thing"
    assert task.expected_output == "done"
    assert task.retrieval_context == ["ctx"]
    assert task.chaos_spec == {"kind": "kill"}
    assert task.verification_spec == {"check": "ok"}
    assert task.infrastructure == {"deployer": "tofu"}


def test_id_coerced_to_string():
    assert Task.from_dict({"task_id": 7}, name_default="d").id == "7"
    assert Task.from_dict({"id": "abc"}, name_default="d").id == "abc"


def test_id_zero_is_preserved():
    assert Task.from_dict({"id": 0}, name_default="d").id == "0"


def test_null_id_falls_back_to_task_id():
    assert Task.from_dict({"id": None, "task_id": 5}, name_default="d").id == "5"


def test_missing_id_defaults_to_empty():
    task = Task.from_dict({"prompt": "x"}, name_default="d")
    assert task.id == ""


def test_explicit_null_id_defaults_to_empty():
    task = Task.from_dict({"task_id": None, "prompt": "x"}, name_default="d")
    assert task.id == ""


def test_missing_name_uses_default():
    task = Task.from_dict({"prompt": "x"}, name_default="from-dir")
    assert task.name == "from-dir"


def test_prompt_is_stripped():
    task = Task.from_dict({"prompt": "  hello  "}, name_default="d")
    assert task.prompt == "hello"


def test_goal_alias_maps_to_prompt():
    task = Task.from_dict({"goal": "  goal text  "}, name_default="d")
    assert task.prompt == "goal text"


def test_prompt_takes_precedence_over_goal():
    task = Task.from_dict({"prompt": "p", "goal": "g"}, name_default="d")
    assert task.prompt == "p"


def test_defaults_for_empty_mapping():
    task = Task.from_dict({}, name_default="d")
    assert task.id == ""
    assert task.name == "d"
    assert task.prompt == ""
    assert task.expected_output == ""
    assert task.retrieval_context == []
    assert task.chaos_spec is None
    assert task.verification_spec is None
    assert task.infrastructure == {}
    assert task.documentation == []


def test_non_string_prompt_raises():
    with pytest.raises(ValidationError):
        Task.from_dict({"prompt": 123}, name_default="d")


def test_non_list_retrieval_context_raises():
    with pytest.raises(ValidationError):
        Task.from_dict({"retrieval_context": "nope"}, name_default="d")


def test_documentation_defaults_url_and_critical():
    raw = {
        "documentation": [
            {
                "doc_name": "A",
                "constraints": [
                    {"text": "needs url default"},
                    {"text": "explicit critical", "critical": True},
                ],
            }
        ]
    }
    doc = Task.from_dict(raw, name_default="d").documentation[0]
    assert doc.url == ""
    assert [(c.text, c.critical) for c in doc.constraints] == [
        ("needs url default", False),
        ("explicit critical", True),
    ]


def test_documentation_non_bool_critical_raises():
    # Strict validation: ``critical: "yes"`` (the string a YAML 1.2 parser
    # yields) is not a boolean and must raise rather than be coerced.
    raw = {"documentation": [{"doc_name": "A", "constraints": [{"text": "x", "critical": "yes"}]}]}
    with pytest.raises(ValidationError):
        Task.from_dict(raw, name_default="d")


def test_documentation_missing_constraint_text_raises():
    raw = {"documentation": [{"doc_name": "A", "constraints": [{"critical": True}]}]}
    with pytest.raises(ValidationError):
        Task.from_dict(raw, name_default="d")


def test_non_list_documentation_raises():
    with pytest.raises(ValidationError):
        Task.from_dict({"documentation": "nope"}, name_default="d")


def test_empty_yaml_blocks_become_defaults():
    # An empty block (``key:`` with no value) parses to None and must fall back
    # to the field default rather than failing strict validation.
    raw = {
        "infrastructure": None,
        "retrieval_context": None,
        "documentation": None,
        "prompt": None,
        "expected_output": None,
    }
    task = Task.from_dict(raw, name_default="d")
    assert task.infrastructure == {}
    assert task.retrieval_context == []
    assert task.documentation == []
    assert task.prompt == ""
    assert task.expected_output == ""


def test_documentation_entry_empty_nested_keys_coalesce():
    # Empty nested YAML keys parse to None; the strict schema must coalesce them
    # to defaults rather than rejecting them.
    raw = {"documentation": [{"doc_name": None, "url": None, "constraints": None}]}
    doc = Task.from_dict(raw, name_default="d").documentation[0]
    assert doc.doc_name == ""
    assert doc.url == ""
    assert doc.constraints == []


def test_constraint_empty_text_coalesces():
    raw = {"documentation": [{"doc_name": "A", "constraints": [{"text": None}]}]}
    constraint = Task.from_dict(raw, name_default="d").documentation[0].constraints[0]
    assert constraint.text == ""
    assert constraint.critical is False


def test_chaos_and_verification_specs_are_opaque():
    # These specs are parsed downstream, so the schema accepts any shape
    # (e.g. a raw JSON string from a YAML literal block, a list, or a mapping).
    raw = {"chaos_spec": '[{"name": "spike"}]', "verification_spec": [{"name": "v"}]}
    task = Task.from_dict(raw, name_default="d")
    assert task.chaos_spec == '[{"name": "spike"}]'
    assert task.verification_spec == [{"name": "v"}]


def test_to_dict_roundtrip_fields():
    task = Task.from_dict(
        {"task_id": 3, "name": "n", "prompt": "p", "expected_output": "e"},
        name_default="d",
    )
    d = task.to_dict()
    assert d["id"] == "3"
    assert d["name"] == "n"
    assert d["prompt"] == "p"
    assert d["expected_output"] == "e"
    assert set(d) == {
        "id",
        "name",
        "folder",
        "prompt",
        "expected_output",
        "retrieval_context",
        "chaos_spec",
        "verification_spec",
        "infrastructure",
        "documentation",
        "validated",
    }


def test_validated_defaults_false():
    assert Task.from_dict({"name": "n"}, name_default="d").validated is False


def test_validated_parsed_from_spec():
    assert Task.from_dict({"name": "n", "validated": True}).validated is True


def test_validated_empty_block_coalesces_false():
    # An empty YAML block (``validated:`` with no value) parses to None.
    assert Task.from_dict({"name": "n", "validated": None}).validated is False


def test_validated_roundtrips_in_to_dict():
    assert Task.from_dict({"name": "n", "validated": True}).to_dict()["validated"] is True
