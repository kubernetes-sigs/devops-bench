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

"""Single entry point for running external commands."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence

from devops_bench.core.errors import SubprocessError
from devops_bench.core.logging import get_logger

__all__ = ["run", "CompletedProcess"]

type CompletedProcess = subprocess.CompletedProcess[str]

_log = get_logger("core.subprocess")


def _build_env(
    env: Mapping[str, str] | None,
    extra_env: Mapping[str, str] | None,
) -> dict[str, str] | None:
    if env is None and extra_env is None:
        return None
    base = dict(os.environ if env is None else env)
    if extra_env:
        base.update(extra_env)
    return base


def run(
    cmd: Sequence[str | os.PathLike[str]],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    extra_env: Mapping[str, str] | None = None,
    check: bool = True,
    capture: bool = True,
    text: bool = True,
    timeout: float | None = None,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command and return the completed process.

    Args:
        cmd: Command and arguments as a sequence, never a shell string.
        cwd: Working directory.
        env: Full child environment; defaults to the parent's.
        extra_env: Variables overlaid on ``env`` (or the parent environment).
        check: Raise on a non-zero exit code.
        capture: Capture stdout and stderr.
        text: Decode output as text.
        timeout: Seconds before terminating the command.
        input: Data written to stdin.

    Returns:
        The completed process.

    Raises:
        SubprocessError: If the command exits non-zero (when ``check``) or times out.
    """
    rendered = " ".join(str(arg) for arg in cmd)
    _log.debug("running command: %s (cwd=%s)", rendered, cwd or os.getcwd())

    try:
        completed = subprocess.run(
            list(cmd),
            cwd=cwd,
            env=_build_env(env, extra_env),
            capture_output=capture,
            text=text,
            timeout=timeout,
            input=input,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        _log.error("command timed out after %ss: %s", timeout, rendered)
        raise SubprocessError(
            cmd,
            returncode=-1,
            stdout=_as_text(exc.stdout),
            stderr=_as_text(exc.stderr),
        ) from exc

    if check and completed.returncode != 0:
        _log.error("command exited with %s: %s", completed.returncode, rendered)
        raise SubprocessError(
            cmd,
            returncode=completed.returncode,
            stdout=_as_text(completed.stdout),
            stderr=_as_text(completed.stderr),
        )

    return completed


def _as_text(value: str | bytes | None) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return value.decode("utf-8", errors="replace")
