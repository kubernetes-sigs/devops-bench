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

"""Unit tests for devops_bench.core.registry."""

import threading
import time

import pytest

from devops_bench.core.errors import AlreadyRegisteredError, NotRegisteredError
from devops_bench.core.registry import Registry


class _FakeEntryPoint:
    """Minimal stand-in for importlib.metadata.EntryPoint."""

    def __init__(self, name, value, *, error=None):
        self.name = name
        self._value = value
        self._error = error
        self.loaded = False

    def load(self):
        self.loaded = True
        if self._error is not None:
            raise self._error
        return self._value


def test_register_value():
    reg: Registry[int] = Registry("nums")
    reg.register("one")(1)
    assert reg.get("one") == 1


def test_register_decorator_returns_object():
    reg: Registry[type] = Registry("agents")

    @reg.register("gemini")
    class Gemini:
        pass

    assert reg.get("gemini") is Gemini


def test_register_duplicate_raises():
    reg: Registry[int] = Registry("nums")
    reg.register("dup")(1)
    with pytest.raises(AlreadyRegisteredError):
        reg.register("dup")(2)


def test_get_missing_raises_with_available():
    reg: Registry[int] = Registry("nums")
    reg.register("a")(1)
    with pytest.raises(NotRegisteredError) as exc_info:
        reg.get("missing")
    assert "a" in str(exc_info.value)


def test_container_and_iteration_helpers():
    reg: Registry[int] = Registry("nums")
    reg.register("a")(1)
    reg.register("b")(2)
    assert "a" in reg
    assert len(reg) == 2
    assert set(reg) == {"a", "b"}
    assert set(reg.keys()) == {"a", "b"}
    assert set(reg.values()) == {1, 2}
    assert dict(reg.items()) == {"a": 1, "b": 2}
    assert reg["a"] == 1


def test_entry_points_loaded_lazily_on_miss(mocker):
    ep = _FakeEntryPoint("plugin", "loaded-value")
    mock_eps = mocker.patch("devops_bench.core.registry.metadata.entry_points", return_value=[ep])
    reg: Registry[str] = Registry("plugins", entry_point_group="devops_bench.plugins")

    assert ep.loaded is False
    mock_eps.assert_not_called()

    assert reg.get("plugin") == "loaded-value"
    assert ep.loaded is True
    mock_eps.assert_called_once_with(group="devops_bench.plugins")


def test_entry_points_loaded_only_once(mocker):
    ep = _FakeEntryPoint("plugin", "value")
    mock_eps = mocker.patch("devops_bench.core.registry.metadata.entry_points", return_value=[ep])
    reg: Registry[str] = Registry("plugins", entry_point_group="devops_bench.plugins")

    reg.get("plugin")
    with pytest.raises(NotRegisteredError):
        reg.get("still-missing")
    mock_eps.assert_called_once()


def test_explicit_registration_wins_over_entry_point(mocker):
    ep = _FakeEntryPoint("plugin", "from-entry-point")
    mocker.patch("devops_bench.core.registry.metadata.entry_points", return_value=[ep])
    reg: Registry[str] = Registry("plugins", entry_point_group="devops_bench.plugins")
    reg.register("plugin")("explicit")

    assert reg.get("plugin") == "explicit"
    assert ep.loaded is False


def test_failing_entry_point_is_skipped(mocker):
    good = _FakeEntryPoint("good", "ok")
    bad = _FakeEntryPoint("bad", None, error=RuntimeError("boom"))
    mocker.patch("devops_bench.core.registry.metadata.entry_points", return_value=[bad, good])
    reg: Registry[str] = Registry("plugins", entry_point_group="devops_bench.plugins")

    assert reg.get("good") == "ok"
    with pytest.raises(NotRegisteredError):
        reg.get("bad")


def test_entry_points_loaded_once_under_concurrency(mocker):
    load_count = {"n": 0}
    load_count_lock = threading.Lock()

    class _SlowEntryPoint:
        name = "plugin"

        def load(self):
            with load_count_lock:
                load_count["n"] += 1
            time.sleep(0.02)
            return "value"

    mock_eps = mocker.patch(
        "devops_bench.core.registry.metadata.entry_points",
        return_value=[_SlowEntryPoint()],
    )
    reg: Registry[str] = Registry("plugins", entry_point_group="devops_bench.plugins")

    results: list[str] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        results.append(reg.get("plugin"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == ["value"] * 8
    assert load_count["n"] == 1
    mock_eps.assert_called_once()
