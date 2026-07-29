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

"""Registry-driven schema for verification specs."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from devops_bench.core import NotRegisteredError
from devops_bench.verification import verifiers as _verifiers  # noqa: F401
from devops_bench.verification.base import VERIFIERS

__all__ = [
    "AllSpec",
    "AnySpec",
    "NoneSpec",
    "ParallelSpec",
    "SequenceSpec",
    "VerificationEntry",
    "VerificationNode",
    "VerificationSpec",
    "json_schema",
    "parse_entries",
    "parse_node",
]

# Union of every registered verifier; aliased to ``Any`` to satisfy type-checkers.
VerificationNode = Any


def json_schema() -> dict[str, Any]:
    """Return the JSON Schema for a verification spec.

    The schema is an ``anyOf`` over every model registered in :data:`VERIFIERS`,
    discriminated by each model's ``type`` literal.

    Returns:
        A JSON-serializable mapping describing the spec union.
    """
    members = list(VERIFIERS.values())
    return {
        "title": "VerificationSpec",
        "anyOf": [m.model_json_schema() for m in members],
        "discriminator": {"propertyName": "type"},
    }


def _parse_compound_children(cls: type, data: Any) -> Any:
    """Recurse into ``checks`` so each child is parsed through the registry.

    Args:
        cls: The compound model class (provided by the pydantic validator).
        data: The raw payload pydantic is about to validate.

    Returns:
        The payload unchanged when no ``checks`` key is present; otherwise a
        shallow copy with each child already parsed through :func:`parse_node`.
    """
    if isinstance(data, dict) and "checks" in data:
        data = {**data, "checks": [parse_node(c) for c in data["checks"]]}
    return data


@VERIFIERS.register("sequence")
class SequenceSpec(BaseModel):
    """Ordered, fail-fast group: members run in sequence; stop at first failure.

    Attributes:
        type: Discriminator literal, always ``"sequence"``.
        name: Optional label echoed onto the result; metadata, never structural.
        checks: Ordered child nodes; each is itself a parsed verifier node.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["sequence"]
    name: str | None = None
    checks: list[Any] = Field(min_length=1)

    _parse_children = model_validator(mode="before")(_parse_compound_children)


@VERIFIERS.register("parallel")
class ParallelSpec(BaseModel):
    """Independent group: members run concurrently; all must pass.

    Attributes:
        type: Discriminator literal, always ``"parallel"``.
        name: Optional label echoed onto the result; metadata, never structural.
        checks: Sibling child nodes; each is itself a parsed verifier node.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["parallel"]
    name: str | None = None
    checks: list[Any] = Field(min_length=1)

    _parse_children = model_validator(mode="before")(_parse_compound_children)


@VERIFIERS.register("all")
class AllSpec(ParallelSpec):
    """Vocabulary-correct alias of ``parallel``: every member must pass.

    Subclassing ``ParallelSpec`` is deliberate. The runner dispatches on
    ``isinstance``, so an ``all`` node routes to the existing parallel branch
    with no change to the dispatcher.
    """

    type: Literal["all"]  # type: ignore[assignment]


@VERIFIERS.register("any")
class AnySpec(BaseModel):
    """Disjunction: passes when at least one member passes.

    Members run in declaration order and evaluation stops at the first success,
    so cheap checks should be listed first.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["any"]
    name: str | None = None
    checks: list[Any] = Field(min_length=1)

    _parse_children = model_validator(mode="before")(_parse_compound_children)


@VERIFIERS.register("none")
class NoneSpec(BaseModel):
    """Negation: passes when no member passes.

    Evaluation stops at the first member that passes, since one success is
    enough to fail the group.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["none"]
    name: str | None = None
    checks: list[Any] = Field(min_length=1)

    _parse_children = model_validator(mode="before")(_parse_compound_children)


def parse_node(data: Any) -> BaseModel:
    """Parse one verifier-spec node dict through the registry.

    Args:
        data: A node mapping carrying a ``type`` discriminator, or an already
            parsed :class:`pydantic.BaseModel` (returned as-is).

    Returns:
        The concrete verifier or compound spec selected by ``data["type"]``.

    Raises:
        pydantic.ValidationError: If ``data`` is not a valid spec node.

    Example:
        A bare leaf dict discriminates to its concrete model:

        >>> node = parse_node({"type": "pod_healthy", "selector": "app=web"})
        >>> node.type
        'pod_healthy'

        A bare list is rejected:

        >>> parse_node(["pod_healthy"])
        Traceback (most recent call last):
        ...
        pydantic_core._pydantic_core.ValidationError: ...
    """
    if isinstance(data, BaseModel):
        type_key = getattr(data, "type", None)
        if (
            isinstance(type_key, str)
            and type_key in VERIFIERS
            and VERIFIERS.get(type_key) is type(data)
        ):
            return data
        raise _validation_error(
            "verification_spec_unregistered_model",
            (f"verification spec node {type(data).__name__!r} is not a registered verifier"),
            input_value=data,
        )
    if not isinstance(data, dict):
        # Surface as a ValidationError even when the entry is the wrong shape.
        raise _validation_error(
            "verification_spec_not_mapping",
            f"verification spec node must be a mapping, got {type(data).__name__}",
            input_value=data,
        )

    type_key = data.get("type")
    if not isinstance(type_key, str):
        raise _validation_error(
            "verification_spec_missing_type",
            "verification spec node is missing required ``type`` discriminator",
            input_value=data,
        )

    try:
        model_cls = VERIFIERS.get(type_key)
    except NotRegisteredError as exc:
        raise _validation_error(
            "verification_spec_unknown_type",
            (f"unknown verifier type {type_key!r}; registered: {sorted(VERIFIERS.keys())}"),
            input_value=data,
        ) from exc

    return model_cls.model_validate(data)


def _validation_error(code: str, message: str, *, input_value: Any) -> ValidationError:
    """Build a ``ValidationError`` around a single ``PydanticCustomError``."""
    custom = PydanticCustomError(code, message)
    return ValidationError.from_exception_data(
        title=code,
        line_errors=[
            {
                "type": custom,
                "loc": (),
                "input": input_value,
            }
        ],
    )


_VALUE_ERROR_PREFIX = "Value error, "


def _clean_validation_message(exc: ValidationError) -> str:
    """Extract a readable message from a ``ValidationError``, dropping the boilerplate.

    ``str(exc)`` stringifies the full pydantic error report: a header naming the
    model, a per-field trailer with error codes and input echoes, and a
    "For further information" URL. None of that is useful to a task author; only
    the first error's own message is. Pydantic prefixes that message with
    ``"Value error, "`` when it wraps a plain ``ValueError`` (as opposed to a
    ``PydanticCustomError``), so that prefix is stripped when present.
    """
    details = exc.errors()
    if not details:
        return str(exc)
    detail = details[0]
    message = detail.get("msg") or str(exc)
    if message.startswith(_VALUE_ERROR_PREFIX):
        message = message[len(_VALUE_ERROR_PREFIX) :]
    if detail.get("type") == "extra_forbidden" and detail.get("loc"):
        message = f"{message}: {detail['loc'][-1]!r}"
    return message


class VerificationSpec(RootModel[Any]):
    """Entry-point wrapper; ``VerificationSpec(data).root`` yields a concrete node.

    Example:
        >>> spec = VerificationSpec({"type": "pod_healthy", "selector": "app=web"})
        >>> spec.root.type
        'pod_healthy'
    """

    root: Any

    @model_validator(mode="before")
    @classmethod
    def _parse(cls, data: Any) -> Any:
        """Route the raw root payload through the registry parser."""
        return parse_node(data)


class VerificationEntry(BaseModel):
    """One named, scored unit of a task's ``verification_spec``.

    An entry pairs a check subtree with the scoring vocabulary: what the check
    is for (``role``), how badly it matters when it fails (``severity``), how
    much it counts (``weight``), and how it is evaluated (``mode``).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    role: Literal["objective", "safeguard"]
    severity: Literal["recoverable", "catastrophic"] | None = None
    mode: Literal["converge", "assert", "hold"] | None = None
    weight: float = Field(default=1.0, gt=0)
    check: Any

    @field_validator("check", mode="before")
    @classmethod
    def _parse_check(cls, value: Any) -> Any:
        """Route the check subtree through the registry parser."""
        try:
            return parse_node(value)
        except ValidationError as exc:
            raise ValueError(_clean_validation_message(exc)) from exc

    @model_validator(mode="after")
    def _check_role_and_mode(self) -> VerificationEntry:
        """Enforce the role/severity pairing and reject the unbuilt mode."""
        if self.role == "safeguard" and self.severity is None:
            raise ValueError("severity is required when role is 'safeguard'")
        if self.role == "objective" and self.severity is not None:
            raise ValueError("severity is not allowed when role is 'objective'")
        if self.mode == "hold":
            raise ValueError("mode 'hold' is not yet supported; use 'converge' or 'assert'")
        return self

    @property
    def resolved_mode(self) -> str:
        """The effective mode: explicit if set, otherwise derived from role.

        Objectives converge because they describe a state the agent is working
        toward. Safeguards assert because they describe a state that must never
        have been entered, and polling one would just wait for a violation to
        heal.
        """
        if self.mode is not None:
            return self.mode
        return "converge" if self.role == "objective" else "assert"


def parse_entries(raw: Any) -> tuple[list[VerificationEntry], list[dict[str, str]]]:
    """Parse a task's raw ``verification_spec`` into entries plus per-entry errors.

    Parsing is per entry and never raises. One malformed entry is reported and
    skipped while its siblings still load, because a single bad entry must not
    sink a whole benchmark run. Callers surface the errors on the result record
    as ``verification_parse_errors``.

    Args:
        raw: The task's ``verification_spec`` value, normally a list of mappings.

    Returns:
        A ``(entries, errors)`` pair. Each error is a ``{"name", "reason"}``
        mapping, matching the shape already written to result records.
    """
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [], [
            {
                "name": "<root>",
                "reason": (
                    f"verification_spec must be a list of entries, got {type(raw).__name__}"
                ),
            }
        ]

    entries: list[VerificationEntry] = []
    errors: list[dict[str, str]] = []
    seen: set[str] = set()

    for index, item in enumerate(raw):
        label = item.get("name") if isinstance(item, dict) else None
        if not isinstance(label, str) or not label:
            label = f"<index {index}>"
        try:
            entry = VerificationEntry.model_validate(item)
        except ValidationError as exc:
            errors.append({"name": label, "reason": _clean_validation_message(exc)})
            continue
        if entry.name in seen:
            errors.append(
                {
                    "name": entry.name,
                    "reason": f"duplicate verification entry name {entry.name!r}",
                }
            )
            continue
        seen.add(entry.name)
        entries.append(entry)

    return entries, errors
