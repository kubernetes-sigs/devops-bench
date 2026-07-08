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

"""``import devops_bench.agents`` must stay light per CONVENTIONS §8."""

from __future__ import annotations

import subprocess
import sys
import textwrap


def test_agents_import_pulls_no_sdk_or_mcp():
    """Spawn a fresh interpreter so prior tests' imports do not contaminate."""
    script = textwrap.dedent(
        """
        import sys
        import devops_bench.agents  # noqa: F401
        loaded = sorted(sys.modules)
        heavy = ['mcp', 'deepeval', 'anthropic', 'google.genai', 'openai']
        hits = [m for m in loaded if any(m == h or m.startswith(h + '.') for h in heavy)]
        if hits:
            print('LEAKED:', ','.join(hits))
            sys.exit(1)
        print('OK')
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout
