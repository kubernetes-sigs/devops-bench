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

"""Unit tests for the devops_bench.core curated public API."""

import devops_bench.core as core


def test_all_names_are_importable():
    for name in core.__all__:
        assert hasattr(core, name), f"{name} listed in __all__ but not exported"


def test_curated_symbols_resolve_to_submodule_definitions():
    from devops_bench.core.context import RunContext
    from devops_bench.core.registry import Registry

    assert core.Registry is Registry
    assert core.RunContext is RunContext


def test_subprocess_runner_is_not_re_exported():
    assert not hasattr(core, "run")
