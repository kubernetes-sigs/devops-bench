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

"""``import devops_bench`` / ``.run`` / ``.cli`` pull no provider SDK (CONVENTIONS.md §8).

Under the current policy only the provider model SDKs must stay lazy (``deepeval``/``mcp``
are allowed eager). The entrypoint happens to stay free of those too, but the contract it
must keep is SDK-absence.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

_FORBIDDEN = ("anthropic", "google.genai", "google.generativeai", "openai", "ollama")


def _run_in_subprocess(snippet: str) -> str:
    """Run ``snippet`` in a clean Python process and return stdout.

    A fresh interpreter prevents leaks from earlier test imports (the wider
    test session has often already pulled ``deepeval``).
    """
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(snippet)],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _leak_check(import_line: str) -> list[str]:
    output = _run_in_subprocess(
        f"""
        import sys
        {import_line}  # noqa: F401
        forbidden = {_FORBIDDEN!r}
        leaked = sorted(name for name in forbidden if name in sys.modules)
        print(",".join(leaked))
        """
    )
    return [name for name in output.strip().split(",") if name]


def test_import_package_pulls_no_sdk() -> None:
    """A bare ``import devops_bench`` does not pull heavy modules."""
    assert _leak_check("import devops_bench") == []


def test_import_run_pulls_no_sdk() -> None:
    """``import devops_bench.run`` does not pull heavy modules."""
    assert _leak_check("import devops_bench.run") == []


def test_import_cli_pulls_no_sdk() -> None:
    """``import devops_bench.cli`` does not pull heavy modules."""
    assert _leak_check("import devops_bench.cli") == []


def test_top_level_exports_resolve() -> None:
    """Top-level public exports resolve from the package."""
    import devops_bench

    assert devops_bench.run_benchmark.__name__ == "run_benchmark"
    assert devops_bench.BenchmarkConfig.__name__ == "BenchmarkConfig"
    assert devops_bench.BenchmarkResult.__name__ == "BenchmarkResult"
