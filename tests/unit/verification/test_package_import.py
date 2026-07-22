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

"""Lightweight-import guard: ``devops_bench.verification`` pulls no heavy SDKs.

The harness/agents tier may import the verification package eagerly; it must
not transitively load provider SDKs, ``deepeval``, or ``mcp``.
"""

from __future__ import annotations

import subprocess
import sys


def test_import_pulls_no_heavy_sdks():
    # Run in a fresh interpreter so other tests cannot pollute sys.modules.
    code = (
        "import sys\n"
        "import devops_bench.verification  # noqa: F401\n"
        "loaded = set(sys.modules)\n"
        "for forbidden in ('deepeval', 'mcp', 'anthropic', 'google.genai', 'openai'):\n"
        "    assert forbidden not in loaded, forbidden\n"
        "print('ok')\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


def test_parse_node_finds_leaf_verifiers_without_importing_them_directly():
    # A fresh interpreter that only imports devops_bench.verification: nothing
    # else has imported devops_bench.verification.verifiers yet, so this fails
    # unless the package itself registers the leaf verifiers on import.
    code = (
        "from devops_bench.verification.spec import parse_node\n"
        "node = parse_node({'type': 'pod_healthy', 'selector': 'app=web'})\n"
        "assert node.type == 'pod_healthy'\n"
        "print('ok')\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout
