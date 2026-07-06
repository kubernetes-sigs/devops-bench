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

"""Helpers for reading configuration from environment variables."""

from __future__ import annotations

import os
from collections.abc import Mapping

from devops_bench.core.errors import ConfigError

__all__ = [
    "get_env",
    "require_env",
    "first_env",
    "get_bool",
    "get_int",
]

_TRUE_VALUES = frozenset({"1", "true", "yes", "y", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "n", "off", ""})


def _source(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def get_env(
    name: str,
    default: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Read a string variable from the environment.

    Blank or whitespace-only values are treated as unset.

    Args:
        name: Variable to read.
        default: Returned when the variable is unset or blank.
        env: Mapping to read from instead of ``os.environ``.

    Returns:
        The variable's value, or ``default``.
    """
    value = _source(env).get(name)
    if value is None or not value.strip():
        return default
    return value


def require_env(name: str, *, env: Mapping[str, str] | None = None) -> str:
    """Read a required string variable from the environment.

    Args:
        name: Variable to read.
        env: Mapping to read from instead of ``os.environ``.

    Returns:
        The variable's value.

    Raises:
        ConfigError: If the variable is unset or blank.
    """
    value = get_env(name, env=env)
    if value is None:
        raise ConfigError(f"required environment variable {name!r} is not set")
    return value


def first_env(
    *names: str,
    default: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Return the value of the first variable in ``names`` that is set.

    Args:
        *names: Variables to try in order.
        default: Returned when none are set.
        env: Mapping to read from instead of ``os.environ``.

    Returns:
        The first set value, or ``default``.
    """
    for name in names:
        value = get_env(name, env=env)
        if value is not None:
            return value
    return default


def get_bool(
    name: str,
    default: bool = False,
    *,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Parse a boolean flag from the environment.

    Accepts ``1/true/yes/y/on`` and ``0/false/no/n/off`` (case-insensitive).

    Args:
        name: Variable to read.
        default: Returned when the variable is unset or blank.
        env: Mapping to read from instead of ``os.environ``.

    Returns:
        The parsed boolean, or ``default``.

    Raises:
        ConfigError: If the value is set but not a recognized boolean.
    """
    raw = _source(env).get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return default if normalized == "" else False
    raise ConfigError(f"environment variable {name!r} is not a valid boolean: {raw!r}")


def get_int(
    name: str,
    default: int | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> int | None:
    """Parse an integer from the environment.

    Args:
        name: Variable to read.
        default: Returned when the variable is unset or blank.
        env: Mapping to read from instead of ``os.environ``.

    Returns:
        The parsed integer, or ``default``.

    Raises:
        ConfigError: If the value is set but not a valid integer.
    """
    value = get_env(name, env=env)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(
            f"environment variable {name!r} is not a valid integer: {value!r}"
        ) from exc
