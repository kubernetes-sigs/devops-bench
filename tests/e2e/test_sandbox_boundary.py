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

"""E2E: the sandbox boundary holds against a live cluster.

Drives ``hack/sandbox_probe.py`` (controls, both observed escapes, every
review-found hole, and a clean teardown) and asserts its exit code, so a
boundary regression is a red build while the script stays runnable standalone.

Double-gated: outside ``tests/unit`` and skipped unless ``BENCH_E2E_SANDBOX=1``.
Needs a docker daemon, a built sandbox image, and a disposable cluster (the
probes create and deny privileged pods). Against kind:

    BENCH_E2E_SANDBOX=1 BENCH_SANDBOX_IMAGE=agent-sandbox:dev \\
    BENCH_E2E_PROVIDER=kind BENCH_E2E_CLUSTER_NAME=probe \\
    uv run pytest tests/e2e/test_sandbox_boundary.py -s

For vcluster set ``BENCH_E2E_PROVIDER=vcluster`` and ``BENCH_E2E_CLUSTER_KUBECONFIG``;
``BENCH_E2E_HOST_APISERVER`` also asserts the token is useless against the host.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

if os.environ.get("BENCH_E2E_SANDBOX") != "1":
    pytest.skip(
        "live boundary probes need BENCH_E2E_SANDBOX=1, a docker daemon, a built "
        "sandbox image, and a disposable cluster (see the module docstring)",
        allow_module_level=True,
    )

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _probe_argv() -> list[str]:
    """Assemble the probe script's argv from ``BENCH_E2E_*``; fails without an image."""
    image = os.environ.get("BENCH_SANDBOX_IMAGE", "")
    if not image:
        pytest.fail("BENCH_SANDBOX_IMAGE must name the built sandbox image")
    argv = [
        sys.executable,
        str(_REPO_ROOT / "hack" / "sandbox_probe.py"),
        "--image",
        image,
        "--provider",
        os.environ.get("BENCH_E2E_PROVIDER", "none"),
    ]
    for flag, env in (
        ("--cluster-name", "BENCH_E2E_CLUSTER_NAME"),
        ("--location", "BENCH_E2E_LOCATION"),
        ("--project", "BENCH_E2E_PROJECT"),
        ("--cluster-kubeconfig", "BENCH_E2E_CLUSTER_KUBECONFIG"),
        ("--context", "BENCH_E2E_CONTEXT"),
        ("--host-apiserver", "BENCH_E2E_HOST_APISERVER"),
    ):
        value = os.environ.get(env)
        if value:
            argv += [flag, value]
    return argv


def test_boundary_probes_all_green() -> None:
    """Controls admit, escapes deny, teardown leaves no trace; the transcript rides the failure."""
    completed = subprocess.run(
        _probe_argv(),
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    assert completed.returncode == 0, (
        f"boundary probes failed (exit {completed.returncode})\n"
        f"--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}"
    )
