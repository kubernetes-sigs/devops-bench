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

"""Logging helpers for devops-bench."""

from __future__ import annotations

import logging
import os
from typing import IO

__all__ = ["ROOT_LOGGER_NAME", "DEFAULT_FORMAT", "get_logger", "configure_logging"]

ROOT_LOGGER_NAME = "devops_bench"
DEFAULT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_LEVEL_ENV_VAR = "BENCH_LOG_LEVEL"

logging.getLogger(ROOT_LOGGER_NAME).addHandler(logging.NullHandler())


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a logger namespaced under ``devops_bench``.

    Args:
        name: Dotted name relative to the package root; falsy returns the root.

    Returns:
        The namespaced logger.
    """
    if not name:
        return logging.getLogger(ROOT_LOGGER_NAME)
    if name == ROOT_LOGGER_NAME or name.startswith(f"{ROOT_LOGGER_NAME}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{name}")


def _resolve_level(level: int | str | None) -> int:
    if level is None:
        level = os.environ.get(_LEVEL_ENV_VAR, "INFO")
    if isinstance(level, str):
        resolved = logging.getLevelName(level.upper())
        if not isinstance(resolved, int):
            raise ValueError(f"unknown log level: {level!r}")
        return resolved
    return level


def configure_logging(
    level: int | str | None = None,
    *,
    stream: IO[str] | None = None,
    fmt: str = DEFAULT_FORMAT,
    datefmt: str = DEFAULT_DATE_FORMAT,
    force: bool = False,
) -> logging.Logger:
    """Attach a console handler to the ``devops_bench`` logger and set its level.

    Idempotent unless ``force`` replaces existing handlers.

    Args:
        level: Level as an int or name; defaults to ``BENCH_LOG_LEVEL`` then ``INFO``.
        stream: Handler destination; defaults to ``stderr``.
        fmt: Record format string.
        datefmt: Timestamp format string.
        force: Replace existing non-null handlers.

    Returns:
        The package root logger.

    Raises:
        ValueError: If ``level`` names no known log level.
    """
    logger = logging.getLogger(ROOT_LOGGER_NAME)
    logger.setLevel(_resolve_level(level))
    logger.propagate = False

    if force:
        for handler in [h for h in logger.handlers if not isinstance(h, logging.NullHandler)]:
            logger.removeHandler(handler)

    has_stream_handler = any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.NullHandler)
        for h in logger.handlers
    )
    if not has_stream_handler:
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter(fmt=fmt, datefmt=datefmt))
        logger.addHandler(handler)

    return logger
