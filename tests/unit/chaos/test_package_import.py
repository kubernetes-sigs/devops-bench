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

"""Import-hygiene guard: ``devops_bench.chaos`` keeps the SDK chain lazy.

CONVENTIONS §8 (revised) — light init is a guideline, not a rule: importing
the chaos package may now pull ``deepeval`` / ``mcp`` and the concrete
fault/trigger modules (they register eagerly). What it must **never** pull is a
provider model SDK or the agent + models chain — those stay lazy, gated inside
:meth:`GenerateLoadFault.inject`.
"""

from __future__ import annotations

import subprocess
import sys


def test_import_pulls_no_provider_sdk() -> None:
    # Fresh interpreter so other tests cannot pollute sys.modules. Provider
    # SDKs are the only modules that must stay out — ``deepeval`` / ``mcp`` are
    # now permitted eagerly.
    code = (
        "import sys\n"
        "import devops_bench.chaos  # noqa: F401\n"
        "loaded = set(sys.modules)\n"
        "for forbidden in ('anthropic', 'google.genai', 'google.generativeai', 'openai', 'ollama'):\n"
        "    assert forbidden not in loaded, forbidden\n"
        "print('ok')\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


def test_import_does_not_pull_chaos_agent_or_models_chain() -> None:
    # Acceptance: importing the chaos package must NOT pull ``chaos.agent``
    # (the LLM driver) or any ``models.*`` module — the only consumer,
    # :meth:`Fault.inject`, lazy-imports them. The concrete fault / trigger
    # modules, by contrast, DO load eagerly now (registry registration), so we
    # assert their presence to pin the new eager behavior.
    code = (
        "import sys\n"
        "import devops_bench.chaos  # noqa: F401\n"
        "loaded = set(sys.modules)\n"
        "for forbidden in (\n"
        "    'devops_bench.chaos.agent',\n"
        "    'devops_bench.models',\n"
        "    'devops_bench.models.utils.loop',\n"
        "    'devops_bench.models.base',\n"
        "):\n"
        "    assert forbidden not in loaded, forbidden\n"
        "# The concrete fault / trigger modules now register eagerly on import.\n"
        "for expected in (\n"
        "    'devops_bench.chaos.faults.generate_load',\n"
        "    'devops_bench.chaos.triggers.time_delay',\n"
        "):\n"
        "    assert expected in loaded, expected\n"
        "print('ok')\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


def test_parsing_a_spec_does_not_pull_the_agent_or_models_chain() -> None:
    # The stronger Phase-4 invariant: even after ``ChaosSpec.model_validate``
    # has loaded the concrete fault/trigger modules (and exercised the
    # registry-driven parser), the agent + models chain stays out of
    # ``sys.modules`` until a fault actually injects.
    code = (
        "import sys\n"
        "from devops_bench.chaos import ChaosSpec\n"
        "spec = ChaosSpec.model_validate({\n"
        "    'trigger': {'type': 'time', 'delay_seconds': 0},\n"
        "    'action': {\n"
        "        'type': 'generate_load',\n"
        "        'target': {'service_url': 'http://x', 'qps': 1},\n"
        "    },\n"
        "})\n"
        "loaded = set(sys.modules)\n"
        "for forbidden in (\n"
        "    'devops_bench.chaos.agent',\n"
        "    'devops_bench.models.utils.loop',\n"
        "    'devops_bench.models.base',\n"
        "):\n"
        "    assert forbidden not in loaded, forbidden\n"
        "print('ok')\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


def test_chaos_init_exports_match_handoff() -> None:
    import devops_bench.chaos as chaos

    # Handoff §5.5: export only Fault, Trigger, ChaosResult, FAULTS, TRIGGERS,
    # ChaosSpec. ChaosAgent is NOT exported.
    assert set(chaos.__all__) == {
        "ChaosResult",
        "ChaosSpec",
        "FAULTS",
        "Fault",
        "TRIGGERS",
        "Trigger",
    }
    assert not hasattr(chaos, "ChaosAgent")
