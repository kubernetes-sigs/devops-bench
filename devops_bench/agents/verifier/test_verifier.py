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

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from devops_bench.agents.verifier.verifier import VerifierAgent
from devops_bench.agents.verifier.verifiers.pod_healthy_verifier import PodHealthyVerifier
from devops_bench.agents.verifier.verifiers.scaling_complete_verifier import ScalingCompleteVerifier

@pytest.fixture(autouse=True)
def fake_time():
    """Fixture to mock time.perf_counter and time.sleep globally to avoid real-time waiting/busy loops."""
    current_time = [0.0]

    def mock_perf_counter():
        current_time[0] += 0.001
        return current_time[0]

    def mock_sleep(seconds):
        current_time[0] += seconds

    with patch("time.perf_counter", side_effect=mock_perf_counter), \
         patch("time.sleep", side_effect=mock_sleep):
        yield

@pytest.fixture
def verifier_agent():
    return VerifierAgent()

@patch("subprocess.run")
def test_pod_healthy_verifier_check_status_success(mock_run):
    mock_output = json.dumps(
        {
            "items": [
                {
                    "status": {
                        "phase": "Running",
                        "conditions": [{"type": "Ready", "status": "True"}]
                    }
                },
                {
                    "status": {
                        "phase": "Running",
                        "conditions": [{"type": "Ready", "status": "True"}]
                    }
                },
            ]
        }
    )
    mock_run.return_value = MagicMock(
        stdout=mock_output, returncode=0
    )

    p_verifier = PodHealthyVerifier(selector="app=my-app")
    details = p_verifier._get_pods_details()
    success = p_verifier._check_pods_status(details)

    assert success is True
    assert len(details["items"]) == 2

@patch("subprocess.run")
def test_pod_healthy_verifier_check_status_failure(mock_run):
    mock_output = json.dumps(
        {
            "items": [
                {
                    "status": {
                        "phase": "Running",
                        "conditions": [{"type": "Ready", "status": "True"}]
                    }
                },
                {
                    "status": {
                        "phase": "Pending",
                        "conditions": [{"type": "Ready", "status": "False"}]
                    }
                },
            ]
        }
    )
    mock_run.return_value = MagicMock(
        stdout=mock_output, returncode=0
    )

    p_verifier = PodHealthyVerifier(selector="app=my-app")
    details = p_verifier._get_pods_details()
    success = p_verifier._check_pods_status(details)

    assert success is False

@patch("subprocess.run")
def test_scaling_complete_verifier_check_scaling_success(mock_run):
    mock_output = json.dumps({"status": {"readyReplicas": 3}})
    mock_run.return_value = MagicMock(
        stdout=mock_output, returncode=0
    )

    s_verifier = ScalingCompleteVerifier(deployment="my-dep", min_replicas=3)
    success, details = s_verifier._check_scaling()

    assert success is True
    assert "Ready replicas (3) >= min replicas (3)" in details["reason"]

@patch("subprocess.run")
def test_pod_healthy_verifier_verify_wait_success(mock_run):
    mock_run.return_value = MagicMock(
        stdout="pod/my-pod condition met", returncode=0
    )

    p_verifier = PodHealthyVerifier(selector="app=my-app")
    result = p_verifier.verify(timeout_sec=60)

    assert result.success is True
    assert result.reason == "Condition met via kubectl wait"

@patch("subprocess.run")
def test_pod_healthy_verifier_verify_wait_failure_fallback_success(mock_run):
    # Mock wait fails, but get pods status succeeds (running fallback)
    mock_run.side_effect = [
        subprocess.CalledProcessError(1, "kubectl wait", stderr="timed out"),
        MagicMock(
            stdout=json.dumps(
                {
                    "items": [
                        {
                            "status": {
                                "phase": "Running",
                                "conditions": [{"type": "Ready", "status": "True"}]
                            }
                        }
                    ]
                }
            ),
            returncode=0
        )
    ]

    p_verifier = PodHealthyVerifier(selector="app=my-app")
    result = p_verifier.verify(timeout_sec=60)

    assert result.success is True
    assert result.reason == "Condition met via polling fallback"

@patch("devops_bench.agents.verifier.verifiers.scaling_complete_verifier.ScalingCompleteVerifier._check_scaling")
def test_scaling_complete_verifier_verify_polling_success(mock_check):
    mock_check.side_effect = [
        (False, {"reason": "not yet"}),
        (True, {"reason": "done"}),
    ]

    s_verifier = ScalingCompleteVerifier(deployment="my-dep", min_replicas=2)
    result = s_verifier.verify(timeout_sec=60)

    assert result.success is True
    assert result.reason == "Scaling complete: done"

@patch("subprocess.run")
def test_wait_for_condition_compound_success(mock_run, verifier_agent):
    def run_side_effect(cmd, *args, **kwargs):
        if "wait" in cmd:
            return MagicMock(stdout="pod/my-pod condition met", returncode=0)
        elif "deployment" in cmd:
            return MagicMock(stdout=json.dumps({"status": {"readyReplicas": 2}}), returncode=0)
        return MagicMock(stdout="", returncode=0)
        
    mock_run.side_effect = run_side_effect

    spec = {
        "pod_spec": {"type": "pod_healthy", "selector": "app=my-app"},
        "scaling_spec": {"type": "scaling_complete", "deployment": "my-dep", "min_replicas": 2}
    }
    result = verifier_agent.wait_for_condition(spec, timeout_sec=60)

    assert result.success is True
    assert "pod_spec succeeded" in result.reason
    assert "scaling_spec succeeded" in result.reason

@patch("subprocess.run")
def test_wait_for_condition_compound_failure(mock_run, verifier_agent):
    def run_side_effect(cmd, *args, **kwargs):
        if "wait" in cmd:
            raise subprocess.CalledProcessError(1, "kubectl wait", stderr="timed out")
        elif "deployment" in cmd:
            return MagicMock(stdout=json.dumps({"status": {"readyReplicas": 2}}), returncode=0)
        elif "pods" in cmd:
            return MagicMock(stdout=json.dumps({"items": []}), returncode=0)
        return MagicMock(stdout="", returncode=0)

    mock_run.side_effect = run_side_effect

    spec = {
        "pod_spec": {"type": "pod_healthy", "selector": "app=my-app"},
        "scaling_spec": {"type": "scaling_complete", "deployment": "my-dep", "min_replicas": 2}
    }
    result = verifier_agent.wait_for_condition(spec, timeout_sec=60)

    assert result.success is False
    assert "pod_spec failed" in result.reason
    assert "scaling_spec succeeded" in result.reason


