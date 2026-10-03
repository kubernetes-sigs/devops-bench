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

"""Subprocess tests for the bastion matrix shell library (no ssh, no model calls)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BASTION = REPO_ROOT / "scripts" / "bastion"
LIB = BASTION / "_matrix_lib.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not available")

# A stand-in ``devops_bench.models`` whose client replies with $FAKE_REPLY.
_FAKE_MODELS = """
import os
class _Client:
    async def generate_content(self, contents, tools, system_instruction):
        return {"reply": os.environ.get("FAKE_REPLY", "")}
    def get_text_content(self, response):
        return response["reply"]
def get_model(provider=None, model_name=None, **kwargs):
    return _Client()
"""


def _bash(script: str, env: dict[str, str], tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Source the library in a clean environment, then run ``script``."""
    base = {
        "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "PYTHONSAFEPATH": "1",  # keep the cwd off sys.path so a PYTHONPATH shim can shadow the package
    }
    return subprocess.run(
        ["bash", "-c", f"source '{LIB}' >/dev/null 2>&1 || exit 97\n{script}"],
        env={**base, **env},
        capture_output=True,
        text=True,
        check=False,
    )


def _shim_models(tmp_path: Path) -> dict[str, str]:
    """Write a fake ``devops_bench.models`` package and return the env that selects it."""
    pkg = tmp_path / "shim" / "devops_bench"
    (pkg / "models").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "models" / "__init__.py").write_text(_FAKE_MODELS)
    return {"PYTHONPATH": str(tmp_path / "shim")}


@pytest.mark.parametrize("script", sorted(BASTION.glob("*.sh")), ids=lambda p: p.name)
def test_bastion_scripts_parse(script: Path) -> None:
    result = subprocess.run(
        ["bash", "-n", str(script)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_runner_env_exports_the_pinned_models(tmp_path: Path) -> None:
    env = {"JUDGE_MODEL": "jm", "CHAOS_MODEL": "cm", "CHAOS_PROVIDER": "cp"}
    out = _bash("_runner_env", env, tmp_path).stdout
    assert f"cd '{REPO_ROOT}'" in out
    assert "JUDGE_MODEL='jm'" in out
    assert "CHAOS_PROVIDER='cp' CHAOS_MODEL='cm'" in out
    assert "GOOGLE_GENAI_USE_VERTEXAI" not in out


def test_runner_env_vertex_mode_drops_keys_and_targets_global(tmp_path: Path) -> None:
    env = {"BENCH_VERTEX": "1", "PROJECT_ID": "proj", "BENCH_REMOTE": "1", "REMOTE_DIR": "rd"}
    out = _bash("_runner_env", env, tmp_path).stdout
    assert "cd ~/rd" in out
    assert "unset AGENT_API_KEY GEMINI_API_KEY" in out
    assert "GOOGLE_GENAI_USE_VERTEXAI=true GOOGLE_CLOUD_PROJECT='proj'" in out
    assert "GCP_VERTEX_LOCATION='global'" in out
    assert "export GCP_PROJECT_ID='proj'" in out


def test_preflight_passes_when_both_models_answer(tmp_path: Path) -> None:
    result = _bash("host_exec() { return 0; }; preflight_models", {"JUDGE_MODEL": "jm"}, tmp_path)
    assert result.returncode == 0
    assert "judge model google/jm" in result.stdout
    assert "chaos model google/" in result.stdout


def test_preflight_names_the_failing_role_and_still_probes_both(tmp_path: Path) -> None:
    # Fail only the chaos probe; match on its model_name argument, not the shared prelude.
    stub = 'host_exec() { case "$1" in *"model_name=\'cm\'"*) return 1;; *) return 0;; esac; }'
    env = {"JUDGE_MODEL": "jm", "CHAOS_MODEL": "cm"}
    result = _bash(f"{stub}; preflight_models", env, tmp_path)
    assert result.returncode == 1
    assert "ERROR: chaos model google/cm did not answer." in result.stderr
    assert "judge model google/jm" in result.stdout
    assert "judge model google/jm did not answer" not in result.stderr


def test_preflight_probe_accepts_a_text_reply(tmp_path: Path) -> None:
    env = {**_shim_models(tmp_path), "FAKE_REPLY": "ok", "JUDGE_MODEL": "jm", "CHAOS_MODEL": "cm"}
    result = _bash("preflight_models", env, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("answered: ok") == 2


def test_preflight_probe_rejects_an_empty_reply(tmp_path: Path) -> None:
    env = {**_shim_models(tmp_path), "FAKE_REPLY": "", "JUDGE_MODEL": "jm", "CHAOS_MODEL": "cm"}
    result = _bash("preflight_models", env, tmp_path)
    assert result.returncode == 1
    assert "model returned no text" in result.stderr
    assert "ERROR: judge model google/jm did not answer." in result.stderr
    assert "ERROR: chaos model google/cm did not answer." in result.stderr
