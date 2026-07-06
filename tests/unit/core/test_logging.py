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

"""Unit tests for devops_bench.core.logging."""

import io
import logging

import pytest

from devops_bench.core import logging as bench_logging
from devops_bench.core.logging import ROOT_LOGGER_NAME, configure_logging, get_logger


@pytest.fixture
def clean_root_logger():
    """Snapshot and restore the package root logger around each test."""
    logger = logging.getLogger(ROOT_LOGGER_NAME)
    saved_handlers = logger.handlers[:]
    saved_level = logger.level
    saved_propagate = logger.propagate
    yield logger
    logger.handlers[:] = saved_handlers
    logger.level = saved_level
    logger.propagate = saved_propagate


def test_get_logger_namespaces_children():
    assert get_logger("deployers.gcp").name == "devops_bench.deployers.gcp"


def test_get_logger_no_name_returns_root():
    assert get_logger().name == ROOT_LOGGER_NAME


def test_get_logger_does_not_double_prefix():
    assert get_logger("devops_bench.core").name == "devops_bench.core"


def test_root_logger_has_null_handler_on_import():
    handlers = logging.getLogger(ROOT_LOGGER_NAME).handlers
    assert any(isinstance(h, logging.NullHandler) for h in handlers)


def test_configure_logging_attaches_stream_handler(clean_root_logger):
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream, force=True)
    get_logger("core.test").info("hello world")
    assert "hello world" in stream.getvalue()


def test_configure_logging_sets_level(clean_root_logger):
    configure_logging("WARNING", force=True)
    assert clean_root_logger.level == logging.WARNING


def test_configure_logging_is_idempotent(clean_root_logger):
    configure_logging("INFO", force=True)
    before = [h for h in clean_root_logger.handlers if not isinstance(h, logging.NullHandler)]
    configure_logging("INFO")
    after = [h for h in clean_root_logger.handlers if not isinstance(h, logging.NullHandler)]
    assert len(before) == len(after) == 1


def test_configure_logging_reads_env_level(clean_root_logger, monkeypatch):
    monkeypatch.setenv("BENCH_LOG_LEVEL", "ERROR")
    configure_logging(force=True)
    assert clean_root_logger.level == logging.ERROR


def test_configure_logging_invalid_level_raises(clean_root_logger):
    with pytest.raises(ValueError, match="unknown log level"):
        configure_logging("NOPE")


def test_configure_logging_disables_propagation(clean_root_logger):
    configure_logging("INFO", force=True)
    assert clean_root_logger.propagate is False


def test_module_exposes_default_format():
    assert "%(message)s" in bench_logging.DEFAULT_FORMAT
