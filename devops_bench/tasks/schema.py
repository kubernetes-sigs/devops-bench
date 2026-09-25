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

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = ["Task", "DocumentationEntry", "Constraint", "CheckGroup", "CATEGORIES"]

# Strict validation: reject implicit type coercion (e.g. the string ``"yes"``
# is not a bool), and ignore unknown keys in source specs.
_STRICT = ConfigDict(strict=True, extra="ignore")

# Display text must be run-invariant: it is rendered across runs, so a
# per-run value such as ``{{CLUSTER_NAME}}`` has no stable meaning in it.
_PLACEHOLDER_MARKER = "{{"

# Display fields a verification entry may carry. Their types live on
# ``VerificationEntry``; the task-level checks below only need the names.
_ENTRY_DISPLAY_FIELDS = ("title", "description", "failure_hint")

# The primary buckets a task may declare as ``category``. Closed so filters
# downstream see one spelling per bucket; extend here when none fits.
CATEGORIES = ("deploy", "remediate", "scale", "secure", "incident", "migrate", "generate")


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


class CheckGroup(BaseModel):
    """A named bucket of verification entries, for display only.

    Entries opt in with ``group: <key>``; the key is the mapping key under the
    task's ``check_groups``. Grouping never affects scoring.

    Attributes:
        title: Short human label for the group.
        description: What a run that passes every entry in the group achieved.
    """

    model_config = _STRICT

    title: str
    description: str = ""

    @model_validator(mode="before")
    @classmethod
    def _coalesce_and_strip(cls, data: Any) -> Any:
        """Coalesce an empty ``description:`` and strip both texts, as task fields are."""
        if not isinstance(data, dict):
            return data
        data = _coalesce_none(data, {"description": ""})
        return {k: _text(v) if k in ("title", "description") else v for k, v in data.items()}

    @model_validator(mode="after")
    def _require_title(self) -> "CheckGroup":
        """A group exists to be shown, so a blank title is a mistake at any stage."""
        if not self.title:
            raise ValueError("check group title must not be blank")
        return self


class Task(BaseModel):
    """Standardized representation of an evaluation task.

    Attributes:
        id: Task identifier.
        name: Human-readable task name (the ``name:`` field from the spec).
        folder: Name of the directory the task spec was loaded from; ``""`` when
            the source is not a directory-backed spec.
        title: Display name for the task; free to change, unlike ``name``.
        summary: A few plain sentences on the starting state, what the agent
            must do, and what done looks like. Run-invariant, like every
            display field: no placeholders.
        category: Primary bucket for filtering; one of :data:`CATEGORIES`.
        tags: Secondary facets for filtering.
        check_groups: Display groups that ``verification_spec`` entries may
            reference via ``group``; keyed by the group slug.
        prompt: Instruction text driving the agent.
        expected_output: Reference output the result is judged against.
        retrieval_context: Supporting passages for retrieval-based scoring.
        chaos_spec: Opaque chaos-injection specification parsed by the chaos
            subsystem; may be a mapping, list, or raw JSON string.
        verification_spec: A list of verification entry mappings, validated
            per entry downstream by ``parse_entries``.
        recoverable_safety: "Must-not-do" constraints whose violation is contained
            /reversible; judged like the correctness checklist and rolled into
            ``rec_v``. Catastrophic safeguards have no judged form and are
            declared deterministically in ``verification_spec`` instead.
        infrastructure: Deployer and stack settings for the task environment.
        documentation: Documentation entries, each with per-constraint criticality.
        agent_pod_security: Pod-security level enforced on the namespaces a
            sandboxed agent can reach: ``"baseline"`` (default) or
            ``"privileged"`` to opt out. Reserve ``"privileged"`` for tasks
            about privileged workloads -- it removes the control that denies
            the privileged-pod and hostPath escape.
        validated: Whether the task has been vetted as correct and is eligible to
            promote to the leaderboard. Defaults to ``False`` so an unvetted task
            never counts until explicitly marked. A validated task must carry
            the display metadata (``title``, ``summary``, ``category``, and a
            ``title`` and ``description`` on every verification entry), because
            the leaderboard renders validated tasks and nothing else.
    """

    model_config = _STRICT

    id: str = ""
    name: str = ""
    folder: str = ""
    title: str = ""
    summary: str = ""
    category: str = ""
    tags: list[str] = Field(default_factory=list)
    check_groups: dict[str, CheckGroup] = Field(default_factory=dict)
    prompt: str = ""
    expected_output: str = ""
    retrieval_context: list[str] = Field(default_factory=list)
    chaos_spec: Any = None
    verification_spec: list[dict[str, Any]] | None = None
    recoverable_safety: list[str] = Field(default_factory=list)
    infrastructure: dict[str, Any] = Field(default_factory=dict)
    documentation: list[DocumentationEntry] = Field(default_factory=list)
    # Closed set: a typo'd level must fail validation, not silently enforce.
    agent_pod_security: Literal["baseline", "privileged"] = "baseline"
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
                "title": "",
                "summary": "",
                "category": "",
                "tags": [],
                "check_groups": {},
                "prompt": "",
                "expected_output": "",
                "retrieval_context": [],
                "recoverable_safety": [],
                "infrastructure": {},
                "documentation": [],
                "agent_pod_security": "baseline",
                "validated": False,
            },
        )

    @model_validator(mode="after")
    def _check_display_metadata(self) -> "Task":
        """Enforce the display-metadata rules that only the task as a whole can see.

        Entries are validated individually downstream by ``parse_entries``, which
        cannot see the task's ``check_groups`` or its ``validated`` flag, so the
        cross-cutting rules live here and run over the raw entry mappings:

        * No display field carries a ``{{placeholder}}``: display text is
          rendered across runs, so a per-run value has no stable meaning in it.
        * ``category`` is one of :data:`CATEGORIES`.
        * Every ``group`` an entry names is declared under ``check_groups``.
        * A validated task carries ``title``, ``summary``, ``category``, and a
          ``title`` and ``description`` on every entry. Unvalidated tasks may
          omit all of it, so a task stays loadable until it is promoted.
        """
        for field in ("title", "summary", "category"):
            if _PLACEHOLDER_MARKER in getattr(self, field):
                raise ValueError(f"{field} must not contain a placeholder")
        if any(_PLACEHOLDER_MARKER in tag for tag in self.tags):
            raise ValueError("tags must not contain a placeholder")
        for key, group in self.check_groups.items():
            if _PLACEHOLDER_MARKER in group.title or _PLACEHOLDER_MARKER in group.description:
                raise ValueError(f"check_groups[{key!r}] must not contain a placeholder")
        if self.category and self.category not in CATEGORIES:
            raise ValueError(f"category {self.category!r} is not one of {', '.join(CATEGORIES)}")

        entries = self.verification_spec or []
        for entry in entries:
            label = entry.get("name", "<unnamed>")
            for field in _ENTRY_DISPLAY_FIELDS:
                value = entry.get(field)
                if isinstance(value, str) and _PLACEHOLDER_MARKER in value:
                    raise ValueError(
                        f"verification entry {label!r}: {field} must not contain a placeholder"
                    )
            group = entry.get("group")
            if group is None:
                continue
            # Raw mappings, so the value can be anything YAML produced. A
            # non-string is unhashable or meaningless as a key, and would be
            # rejected by parse_entries anyway; say so here instead of raising
            # a TypeError from the membership test.
            if not isinstance(group, str):
                raise ValueError(f"verification entry {label!r}: group must be a string")
            # Stripped here as VerificationEntry strips it, so the two agree.
            group = group.strip()
            if group not in self.check_groups:
                raise ValueError(
                    f"verification entry {label!r} names group {group!r}, "
                    f"which is not declared under check_groups"
                )

        if not self.validated:
            return self
        missing = [f for f in ("title", "summary", "category") if not getattr(self, f).strip()]
        if missing:
            raise ValueError(f"a validated task requires {', '.join(missing)}")
        for entry in entries:
            label = entry.get("name", "<unnamed>")
            for field in ("title", "description"):
                if not isinstance(entry.get(field), str) or not entry[field].strip():
                    raise ValueError(
                        f"a validated task requires {field} on verification entry {label!r}"
                    )
        return self

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
        recoverable_safety = raw.get("recoverable_safety", [])
        infrastructure = raw.get("infrastructure", {})
        documentation = raw.get("documentation", [])
        agent_pod_security = raw.get("agent_pod_security", "baseline")
        validated = raw.get("validated", False)
        tags = raw.get("tags", [])
        check_groups = raw.get("check_groups", {})

        return cls.model_validate(
            {
                "id": "" if raw_id is None else _text(str(raw_id)),
                "name": _text(name_default if name is None else name),
                "folder": folder,
                "title": _text(raw.get("title", "")),
                "summary": _text(raw.get("summary", "")),
                "category": _text(raw.get("category", "")),
                "tags": [] if tags is None else tags,
                "check_groups": {} if check_groups is None else check_groups,
                "prompt": _text(prompt),
                "expected_output": _text(raw.get("expected_output", "")),
                # An empty YAML block (``key:`` with no value) parses to None;
                # treat it as the field's empty default rather than rejecting it.
                "retrieval_context": [] if retrieval is None else retrieval,
                "chaos_spec": raw.get("chaos_spec"),
                "verification_spec": raw.get("verification_spec"),
                "recoverable_safety": ([] if recoverable_safety is None else recoverable_safety),
                "infrastructure": {} if infrastructure is None else infrastructure,
                "documentation": [] if documentation is None else documentation,
                "agent_pod_security": (
                    "baseline" if agent_pod_security is None else _text(str(agent_pod_security))
                ),
                "validated": False if validated is None else validated,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the task as a plain serializable mapping.

        Returns:
            A mapping of every field name to its value.
        """
        return self.model_dump()
