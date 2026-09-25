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

A passing *task* run proves the agent can WORK inside the sandbox; it says
nothing about whether the boundary HOLDS — the pod-security policy could be
absent entirely and the run would look identical. This test drives
``hack/sandbox_probe.py``, which asserts each channel directly: the controls
(the credential works, ordinary workloads still run), the two observed
escapes (privileged pod + hostPath = incident 1, the metadata server =
incident 2), every hole the code review found (ephemeral containers,
bench-system, exempt-namespace claims by name and by label, exec into
non-conformant pods), and — since the lifecycle PR — that teardown leaves the
cluster clean.

The script stays runnable standalone on purpose (an operator debugging a
boundary wants the per-probe transcript, not a pytest traceback); this wrapper
is what makes a regression a red build instead of a doc note.

DOUBLE-GATED: this file is outside ``tests/unit`` (the CI gate's pytest path),
and it skips unless ``BENCH_E2E_SANDBOX=1``. It needs a real docker daemon, a
built sandbox image, and a disposable live cluster — on this project that
means the bastion, never a laptop (docker is blocked there) and never a
cluster anyone else is using (the probes create and deny privileged pods).

Run it from the repo root on the bastion, e.g. against kind:

    BENCH_E2E_SANDBOX=1 \\
    BENCH_SANDBOX_IMAGE=agent-sandbox:dev \\
    BENCH_E2E_PROVIDER=kind BENCH_E2E_CLUSTER_NAME=probe \\
    uv run pytest tests/e2e/test_sandbox_boundary.py -s

or against a vcluster (add the host apiserver to also assert the token is
useless against the host cluster):

    BENCH_E2E_SANDBOX=1 BENCH_SANDBOX_IMAGE=agent-sandbox:dev \\
    BENCH_E2E_PROVIDER=vcluster \\
    BENCH_E2E_CLUSTER_KUBECONFIG=$TMPDIR/vcluster-x-kubeconfig.yaml \\
    BENCH_E2E_HOST_APISERVER=https://<host>:443 \\
    uv run pytest tests/e2e/test_sandbox_boundary.py -s
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
    """Assemble the probe script's argv from the ``BENCH_E2E_*`` environment.

    Raises:
        pytest.fail.Exception: When the image is missing — the one input that
            has no default and no provider to derive it from.
    """
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
    """Every probe passes: controls admit, escapes deny, teardown leaves no trace.

    The script already prints a per-probe PASS/FAIL transcript and returns
    non-zero on any failure, so the assertion here is deliberately just the
    exit code — with the full transcript in the failure message, because a red
    probe's name and stderr ARE the diagnosis.
    """
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
