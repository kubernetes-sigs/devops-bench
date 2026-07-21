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

"""JSON Schema emission and validation for a single chaos entry."""

from __future__ import annotations

from typing import Any

from devops_bench.chaos.spec import ChaosSpec

__all__ = ["json_schema", "validate_spec"]


def json_schema() -> dict[str, Any]:
    """Return the JSON Schema for a single chaos entry.

    Returns:
        A JSON-serializable mapping derived from :class:`ChaosSpec`.
    """
    return ChaosSpec.model_json_schema()


def validate_spec(data: Any) -> ChaosSpec:
    """Validate a raw mapping against :class:`ChaosSpec`.

    Args:
        data: A mapping authored in a task file (one chaos entry).

    Returns:
        The constructed :class:`ChaosSpec` instance.

    Raises:
        pydantic.ValidationError: If ``data`` does not match the schema.
    """
    return ChaosSpec.model_validate(data)
