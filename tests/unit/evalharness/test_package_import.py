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

"""``import devops_bench.evalharness`` keeps the provider SDK chain lazy (§8 revised).

The orchestrator is the only layer allowed to consume every component, so it
sits structurally close to the heavy optional dependencies. Light init is now a
guideline, not a rule: importing the package may pull ``deepeval`` / ``mcp``.
What it must **never** pull is a provider model SDK: the metrics judge is
imported lazily inside ``DefaultEvalHarness._score``, the API agent's SDK loads at
construction, and the chaos fault's agent + models chain loads only when the
fault actually injects.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import textwrap


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


def test_importing_harness_pulls_no_provider_sdk() -> None:
    """A bare ``import devops_bench.evalharness`` does not pull a provider SDK.

    ``deepeval`` / ``mcp`` are permitted eagerly now; only the provider model
    SDKs must stay out until the judge / agent actually run.
    """
    output = _run_in_subprocess(
        """
        import sys
        import devops_bench.evalharness  # noqa: F401
        forbidden = {
            "anthropic",
            "google.genai",
            "google.generativeai",
            "openai",
            "ollama",
        }
        leaked = sorted(name for name in forbidden if name in sys.modules)
        print(",".join(leaked))
        """
    )
    leaked = [name for name in output.strip().split(",") if name]
    assert leaked == [], f"importing devops_bench.evalharness pulled: {leaked}"


def test_harness_exports_present() -> None:
    """The eager package exposes every public harness symbol."""
    pkg = importlib.import_module("devops_bench.evalharness")
    # All four public symbols are eagerly imported now (no ``__getattr__``).
    assert pkg.Harness.__name__ == "Harness"
    assert pkg.ResultReporter.__name__ == "ResultReporter"
    assert pkg.DefaultEvalHarness.__name__ == "DefaultEvalHarness"
    assert pkg.ScenarioManager.__name__ == "ScenarioManager"
