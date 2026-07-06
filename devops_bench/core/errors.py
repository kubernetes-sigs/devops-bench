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

"""Exception types raised across devops-bench."""

from __future__ import annotations

import os
from collections.abc import Sequence

__all__ = [
    "DevOpsBenchError",
    "ConfigError",
    "RegistryError",
    "AlreadyRegisteredError",
    "NotRegisteredError",
    "MissingDependencyError",
    "SubprocessError",
]


class DevOpsBenchError(Exception):
    """Base class for all errors raised by devops-bench."""


class ConfigError(DevOpsBenchError):
    """Raised when required configuration is missing or malformed."""


class RegistryError(DevOpsBenchError):
    """Base class for registry registration and lookup failures."""


class AlreadyRegisteredError(RegistryError):
    """Raised when registering a name that is already taken in a registry."""

    def __init__(self, registry_name: str, key: str) -> None:
        self.registry_name = registry_name
        self.key = key
        super().__init__(f"{key!r} is already registered in the {registry_name!r} registry")


class NotRegisteredError(RegistryError):
    """Raised when looking up a name that is not present in a registry."""

    def __init__(self, registry_name: str, key: str, available: Sequence[str] = ()) -> None:
        self.registry_name = registry_name
        self.key = key
        self.available = tuple(available)
        message = f"{key!r} is not registered in the {registry_name!r} registry"
        if self.available:
            message += f"; available: {', '.join(sorted(self.available))}"
        super().__init__(message)


class MissingDependencyError(DevOpsBenchError):
    """Raised when a feature requires an optional dependency that is not installed."""

    def __init__(self, feature: str, extra: str) -> None:
        self.feature = feature
        self.extra = extra
        super().__init__(
            f"{feature} requires the optional dependency group {extra!r}. "
            f"Install it with: pip install devops-bench[{extra}]"
        )


class SubprocessError(DevOpsBenchError):
    """Raised when a subprocess exits non-zero or times out."""

    def __init__(
        self,
        cmd: Sequence[str | os.PathLike[str]],
        returncode: int,
        stdout: str | None = None,
        stderr: str | None = None,
    ) -> None:
        self.cmd = [str(part) for part in cmd]
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        message = f"command failed with exit code {returncode}: {' '.join(self.cmd)}"
        if stderr:
            message += f"\nstderr: {stderr.strip()}"
        super().__init__(message)
