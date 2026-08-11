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

"""Unit tests for devops_bench.core.subprocess.

These exercise the wrapper against real, portable shell-free commands
(``python -c ...``) so behavior is verified end to end without mocking out
the standard library.
"""

import os
import sys
from pathlib import Path

import pytest

from devops_bench.core import subprocess as bench_subprocess
from devops_bench.core.errors import SubprocessError


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_run_captures_stdout():
    result = bench_subprocess.run(_py("print('hello')"))
    assert result.returncode == 0
    assert result.stdout.strip() == "hello"


def test_run_raises_on_nonzero_exit():
    with pytest.raises(SubprocessError) as exc_info:
        bench_subprocess.run(_py("import sys; sys.exit(3)"))
    assert exc_info.value.returncode == 3


def test_run_check_false_returns_completed_process():
    result = bench_subprocess.run(_py("import sys; sys.exit(5)"), check=False)
    assert result.returncode == 5


def test_run_captures_stderr_in_error():
    code = "import sys; sys.stderr.write('kaboom'); sys.exit(1)"
    with pytest.raises(SubprocessError) as exc_info:
        bench_subprocess.run(_py(code))
    assert "kaboom" in exc_info.value.stderr
    assert "kaboom" in str(exc_info.value)


def test_run_extra_env_overlays_parent(monkeypatch):
    monkeypatch.setenv("BASE_VAR", "from-parent")
    result = bench_subprocess.run(
        _py("import os; print(os.environ.get('EXTRA'), os.environ.get('BASE_VAR'))"),
        extra_env={"EXTRA": "from-extra"},
    )
    assert result.stdout.strip() == "from-extra from-parent"


def test_run_env_replaces_parent():
    result = bench_subprocess.run(
        _py("import os; print(os.environ.get('ONLY'))"),
        env={"ONLY": "isolated"},
    )
    assert result.stdout.strip() == "isolated"


def test_run_cwd(tmp_path):
    result = bench_subprocess.run(_py("import os; print(os.getcwd())"), cwd=tmp_path)
    # Resolve to defeat macOS /var -> /private/var symlinks.
    assert result.stdout.strip().endswith(tmp_path.name)


def test_run_input_is_forwarded():
    result = bench_subprocess.run(
        _py("import sys; sys.stdout.write(sys.stdin.read().upper())"),
        input="abc",
    )
    assert result.stdout == "ABC"


def test_run_timeout_raises_subprocess_error():
    with pytest.raises(SubprocessError):
        bench_subprocess.run(_py("import time; time.sleep(5)"), timeout=0.2)


def test_run_accepts_path_objects_in_cmd():
    result = bench_subprocess.run([Path(sys.executable), "-c", "print('hi')"])
    assert result.stdout.strip() == "hi"


def test_run_error_renders_path_command():
    with pytest.raises(SubprocessError) as exc_info:
        bench_subprocess.run([Path(sys.executable), "-c", "import sys; sys.exit(1)"])
    assert sys.executable in str(exc_info.value)


def test_run_decodes_non_utf8_output_on_failure():
    code = r"import sys; sys.stderr.buffer.write(b'\xff\xfe'); sys.exit(1)"
    with pytest.raises(SubprocessError) as exc_info:
        bench_subprocess.run(_py(code), text=False)
    assert isinstance(exc_info.value.stderr, str)


def test_as_text_handles_bytes_and_none():
    assert bench_subprocess._as_text(None) is None
    assert bench_subprocess._as_text("text") == "text"
    assert isinstance(bench_subprocess._as_text(b"\xff\xfe"), str)


def test_child_does_not_inherit_parent_stdin() -> None:
    """A child reading stdin must see EOF immediately, not block on the terminal.

    Regression guard. With subprocess.run's default of ``stdin=None`` the child
    inherits the parent's stdin, and a CLI that inspects it can hang until the
    caller's timeout. ``cat`` blocks until EOF, so this only returns promptly when
    stdin has been closed for the child.

    The test process's own fd 0 is swapped for a pipe that is kept open (and
    never fed EOF) for the duration of the call, so the assertion actually
    depends on ``run`` closing the child's stdin rather than on whatever state
    the test runner happened to leave fd 0 in. If the child inherited the open
    pipe, ``cat`` would block and the call would time out instead of returning.
    """
    read_fd, write_fd = os.pipe()
    saved_stdin_fd = os.dup(0)
    try:
        os.dup2(read_fd, 0)
        os.close(read_fd)
        completed = bench_subprocess.run(
            ["sh", "-c", "cat > /dev/null; echo reached-eof"], check=False, timeout=10
        )
    finally:
        os.dup2(saved_stdin_fd, 0)
        os.close(saved_stdin_fd)
        os.close(write_fd)
    assert completed.returncode == 0
    assert "reached-eof" in completed.stdout
