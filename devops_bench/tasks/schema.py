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

"""Typed schema for benchmark task contracts."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = ["Task", "DocumentationEntry", "Constraint"]

# Strict validation: reject implicit type coercion (e.g. the string ``"yes"``
# is not a bool), and ignore unknown keys in source specs.
_STRICT = ConfigDict(strict=True, extra="ignore")


def _text(value: Any) -> Any:
    """Coalesce an empty (``None``) text value to ``""`` and strip strings.

    A non-string, non-None value is returned unchanged so strict validation can
    reject it.

    Args:
        value: The raw text value from a parsed spec.

    Returns:
        ``""`` for ``None``, the stripped string for a string, else ``value``.
    """
    if value is None:
        return ""
    return value.strip() if isinstance(value, str) else value


def _coalesce_none(data: Any, defaults: dict[str, Any]) -> Any:
    """Coalesce empty (``None``) mapping keys to field defaults.

    An empty YAML block (``key:`` with no value) parses to ``None``; treat it as
    the field's empty default rather than rejecting it under strict validation.
    Only ``None`` values are coalesced, so genuinely wrong types still fail.

    Args:
        data: The raw value passed to a model validator.
        defaults: Field-name to empty-default mapping to apply.

    Returns:
        ``data`` with each listed ``None`` field replaced by its default;
        returned unchanged when it is not a mapping.
    """
    if not isinstance(data, dict):
        return data
    coalesced = dict(data)
    for key, default in defaults.items():
        if key in coalesced and coalesced[key] is None:
            coalesced[key] = default
    return coalesced


class Constraint(BaseModel):
    """A single documented requirement the agent's solution must satisfy.

    Attributes:
        text: The requirement description.
        critical: Whether failing this requirement fails the task outright.
    """

    model_config = _STRICT

    text: str
    critical: bool = False

    @model_validator(mode="before")
    @classmethod
    def _coalesce_empty(cls, data: Any) -> Any:
        """Coalesce empty (``None``) keys to defaults (e.g. ``critical:`` alone)."""
        return _coalesce_none(data, {"text": "", "critical": False})


class DocumentationEntry(BaseModel):
    """A reference document and the constraints derived from it.

    Attributes:
        doc_name: Human-readable document name.
        url: Source URL for the document.
        constraints: Requirements drawn from the document.
    """

    model_config = _STRICT

    doc_name: str = ""
    url: str = ""
    constraints: list[Constraint] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _coalesce_empty(cls, data: Any) -> Any:
        """Coalesce empty (``None``) keys to defaults (e.g. ``constraints:`` alone)."""
        return _coalesce_none(data, {"doc_name": "", "url": "", "constraints": []})


class Task(BaseModel):
    """Standardized representation of an evaluation task.

    Attributes:
        id: Task identifier.
        name: Human-readable task name (the ``name:`` field from the spec).
        folder: Name of the directory the task spec was loaded from; ``""`` when
            the source is not a directory-backed spec.
        prompt: Instruction text driving the agent.
        expected_output: Reference output the result is judged against.
        retrieval_context: Supporting passages for retrieval-based scoring.
        chaos_spec: Opaque chaos-injection specification parsed by the chaos
            subsystem; may be a mapping, list, or raw JSON string.
        verification_spec: Opaque verification specification parsed by the
            verification subsystem; may be a mapping, list, or raw JSON string.
        infrastructure: Deployer and stack settings for the task environment.
        documentation: Documentation entries, each with per-constraint criticality.
        validated: Whether the task has been vetted as correct and is eligible to
            promote to the leaderboard. Defaults to ``False`` so an unvetted task
            never counts until explicitly marked.
    """

    model_config = _STRICT

    id: str = ""
    name: str = ""
    folder: str = ""
    prompt: str = ""
    expected_output: str = ""
    retrieval_context: list[str] = Field(default_factory=list)
    chaos_spec: Any = None
    verification_spec: Any = None
    infrastructure: dict[str, Any] = Field(default_factory=dict)
    documentation: list[DocumentationEntry] = Field(default_factory=list)
    validated: bool = False

    @model_validator(mode="before")
    @classmethod
    def _coalesce_empty(cls, data: Any) -> Any:
        """Coalesce empty (``None``) keys to field defaults.

        Mirrors the defaulting in :meth:`from_dict` so a task built directly via
        ``model_validate``/``__init__`` handles empty blocks (e.g. ``documentation:``
        with no value) the same way, covering every scalar and collection field.
        """
        return _coalesce_none(
            data,
            {
                "id": "",
                "name": "",
                "folder": "",
                "prompt": "",
                "expected_output": "",
                "retrieval_context": [],
                "infrastructure": {},
                "documentation": [],
                "validated": False,
            },
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, name_default: str = "", folder: str = "") -> "Task":
        """Build a task from a parsed spec mapping, validating types strictly.

        Adapts the source naming before validation: ``task_id`` is accepted as an
        alias for ``id`` (and coerced to a string), and ``goal``/``input`` are
        accepted as aliases for ``prompt``. Text fields are stripped. Malformed
        values (e.g. a non-boolean ``critical``) raise ``pydantic.ValidationError``.

        Args:
            raw: Parsed mapping for a single task.
            name_default: Name used when the mapping omits ``name``.
            folder: Directory name the spec was loaded from, recorded on
                :attr:`folder`.

        Returns:
            The validated task.

        Raises:
            ValidationError: If a field has the wrong type.
        """
        raw_id = raw.get("id")
        if raw_id is None:
            raw_id = raw.get("task_id")
        name = raw.get("name")
        prompt = raw.get("prompt")
        if prompt is None:
            prompt = raw.get("goal")
        if prompt is None:
            prompt = raw.get("input")
        retrieval = raw.get("retrieval_context", [])
        infrastructure = raw.get("infrastructure", {})
        documentation = raw.get("documentation", [])
        validated = raw.get("validated", False)

        return cls.model_validate(
            {
                "id": "" if raw_id is None else _text(str(raw_id)),
                "name": _text(name_default if name is None else name),
                "folder": folder,
                "prompt": _text(prompt),
                "expected_output": _text(raw.get("expected_output", "")),
                # An empty YAML block (``key:`` with no value) parses to None;
                # treat it as the field's empty default rather than rejecting it.
                "retrieval_context": [] if retrieval is None else retrieval,
                "chaos_spec": raw.get("chaos_spec"),
                "verification_spec": raw.get("verification_spec"),
                "infrastructure": {} if infrastructure is None else infrastructure,
                "documentation": [] if documentation is None else documentation,
                "validated": False if validated is None else validated,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the task as a plain serializable mapping.

        Returns:
            A mapping of every field name to its value.
        """
        return self.model_dump()
