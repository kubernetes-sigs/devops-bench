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

from pydantic import BaseModel, RootModel, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from devops_bench.core import NotRegisteredError
from devops_bench.verification import verifiers as _verifiers  # noqa: F401
from devops_bench.verification.base import VERIFIERS

__all__ = [
    "ParallelSpec",
    "SequenceSpec",
    "VerificationNode",
    "VerificationSpec",
    "json_schema",
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

    type: Literal["sequence"]
    name: str | None = None
    checks: list[Any]

    _parse_children = model_validator(mode="before")(_parse_compound_children)


@VERIFIERS.register("parallel")
class ParallelSpec(BaseModel):
    """Independent group: members run concurrently; all must pass.

    Attributes:
        type: Discriminator literal, always ``"parallel"``.
        name: Optional label echoed onto the result; metadata, never structural.
        checks: Sibling child nodes; each is itself a parsed verifier node.
    """

    type: Literal["parallel"]
    name: str | None = None
    checks: list[Any]

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
