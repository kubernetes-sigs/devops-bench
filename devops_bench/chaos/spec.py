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

"""Registry-driven schema for chaos specs.

A chaos entry is a trigger, an action (fault), and an opaque ``verify`` key
referencing a verification entry by name. :class:`ChaosSpec` parses the trigger
and action payloads through the :data:`FAULTS` / :data:`TRIGGERS` registries.
"""

from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic_core import PydanticCustomError

# Importing the leaf packages fires their ``@FAULTS.register`` /
# ``@TRIGGERS.register`` decorators so the registries are populated before parse.
from devops_bench.chaos import faults as _faults  # noqa: F401
from devops_bench.chaos import triggers as _triggers  # noqa: F401
from devops_bench.chaos.base import FAULTS, TRIGGERS
from devops_bench.core import NotRegisteredError

__all__ = [
    "ChaosSpec",
    "parse_fault",
    "parse_trigger",
]


def parse_fault(data: Any) -> BaseModel:
    """Parse a fault (``action``) payload through :data:`FAULTS`.

    Args:
        data: A mapping carrying a ``type`` discriminator, or an already-parsed
            :class:`pydantic.BaseModel` (returned as-is when registered).

    Returns:
        The concrete fault selected by ``data["type"]``.

    Raises:
        pydantic.ValidationError: If ``data`` is not a mapping, omits ``type``,
            names an unregistered fault, or fails the target model's validation.
    """
    return _parse_through(data, FAULTS, axis="fault")


def parse_trigger(data: Any) -> BaseModel:
    """Parse a trigger payload through :data:`TRIGGERS`.

    Args:
        data: A mapping carrying a ``type`` discriminator, or an already-parsed
            :class:`pydantic.BaseModel` (returned as-is when registered).

    Returns:
        The concrete trigger selected by ``data["type"]``.

    Raises:
        pydantic.ValidationError: If ``data`` is not a mapping, omits ``type``,
            names an unregistered trigger, or fails the target model's validation.
    """
    return _parse_through(data, TRIGGERS, axis="trigger")


def _parse_through(data: Any, registry: Any, *, axis: str) -> BaseModel:
    """Resolve ``data["type"]`` against ``registry`` and validate the payload.

    Args:
        data: Authored payload or an already-parsed model instance.
        registry: The :data:`FAULTS` or :data:`TRIGGERS` registry to consult.
        axis: ``"fault"`` or ``"trigger"`` — used to label error messages.

    Returns:
        The validated concrete model instance.

    Raises:
        pydantic.ValidationError: If ``data`` cannot be resolved or validated.
    """
    if isinstance(data, BaseModel):
        type_key = getattr(data, "type", None)
        if (
            isinstance(type_key, str)
            and type_key in registry
            and registry.get(type_key) is type(data)
        ):
            return data
        raise _validation_error(
            f"chaos_spec_unregistered_{axis}_model",
            (f"chaos {axis} node {type(data).__name__!r} is not a registered {axis}"),
            input_value=data,
        )
    if not isinstance(data, dict):
        raise _validation_error(
            f"chaos_spec_{axis}_not_mapping",
            f"chaos {axis} node must be a mapping, got {type(data).__name__}",
            input_value=data,
        )

    type_key = data.get("type")
    if not isinstance(type_key, str):
        raise _validation_error(
            f"chaos_spec_{axis}_missing_type",
            f"chaos {axis} node is missing required ``type`` discriminator",
            input_value=data,
        )

    try:
        model_cls = registry.get(type_key)
    except NotRegisteredError as exc:
        raise _validation_error(
            f"chaos_spec_unknown_{axis}_type",
            (f"unknown chaos {axis} type {type_key!r}; registered: {sorted(registry.keys())}"),
            input_value=data,
        ) from exc

    return model_cls.model_validate(data)


def _validation_error(code: str, message: str, *, input_value: Any) -> ValidationError:
    """Build a :class:`ValidationError` around a single :class:`PydanticCustomError`.

    The registry's :class:`NotRegisteredError` carries useful context, but the
    rest of the codebase catches :class:`pydantic.ValidationError` for spec
    parsing; wrapping keeps that single error surface.
    """
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


class ChaosSpec(BaseModel):
    """One authored chaos entry: a trigger, an action, and an optional verify ref.

    ``verify`` names a verification entry by key; the ``verification`` alias is
    also accepted. The key is never an inline verification node — the harness
    resolves it against its verification registry.

    Attributes:
        name: Human-readable label echoed onto the chaos report.
        trigger: ``type``-tagged firing condition (e.g. :class:`TimeTrigger`).
        action: ``type``-tagged disruption (e.g. :class:`GenerateLoadFault`).
        verify: Optional verification-key reference; ``None`` skips post-fault
            verification. Accepts the ``verification`` alias.
    """

    # Accept the ``verification`` alias; forbid unknown keys.
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    name: str = "Planned Disruption"
    trigger: Any
    action: Any
    verify: str | None = Field(
        default=None,
        validation_alias=AliasChoices("verify", "verification"),
    )

    @model_validator(mode="before")
    @classmethod
    def _parse_nodes(cls, data: Any) -> Any:
        """Route ``trigger`` / ``action`` payloads through the registry parsers."""
        if not isinstance(data, dict):
            return data
        out = dict(data)
        if "trigger" in out:
            out["trigger"] = parse_trigger(out["trigger"])
        if "action" in out:
            out["action"] = parse_fault(out["action"])
        return out
