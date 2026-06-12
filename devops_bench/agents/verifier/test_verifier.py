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

def test_pod_healthy_verifier_check_status_success():
    p_verifier = PodHealthyVerifier(selector="app=my-app")
    mock_details = {
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
    success = p_verifier._check_pods_status(mock_details)
    assert success is True

def test_pod_healthy_verifier_check_status_failure():
    p_verifier = PodHealthyVerifier(selector="app=my-app")
    mock_details = {
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
    success = p_verifier._check_pods_status(mock_details)
    assert success is False

@patch("devops_bench.agents.verifier.verifiers.pod_healthy_verifier.KubeWatchService")
@patch("devops_bench.agents.verifier.verifiers.pod_healthy_verifier.PodHealthyVerifier._get_pods_details")
def test_pod_healthy_verifier_verify_watch_success(mock_get_pods_details, mock_watch_class):
    # Simulate first check fails, but a watch event triggers a successful check
    mock_get_pods_details.side_effect = [
        # First check returns non-ready
        {"items": [{"status": {"phase": "Pending"}}]},
        # Event check returns ready
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
    ]

    mock_watcher = MagicMock()
    def mock_start():
        # Trigger event callback
        mock_watcher.callback({"kind": "pod", "name": "my-pod-1", "namespace": "default"})
    mock_watcher.start.side_effect = mock_start

    def mock_init(callback, *args, **kwargs):
        mock_watcher.callback = callback
        return mock_watcher
    mock_watch_class.side_effect = mock_init

    p_verifier = PodHealthyVerifier(selector="app=my-app")
    result = p_verifier.verify(timeout_sec=60)

    assert result.success is True
    assert "event-driven" in result.reason

@patch("devops_bench.agents.verifier.verifiers.pod_healthy_verifier.KubeWatchService")
@patch("devops_bench.agents.verifier.verifiers.pod_healthy_verifier.PodHealthyVerifier._get_pods_details")
def test_pod_healthy_verifier_verify_watch_failure_fallback_success(mock_get_pods_details, mock_watch_class):
    # No events triggered, but fallback check succeeds on timeout
    mock_get_pods_details.side_effect = [
        # Initial check fails
        {"items": [{"status": {"phase": "Pending"}}]},
        # Timeout fallback check succeeds
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
    ]

    p_verifier = PodHealthyVerifier(selector="app=my-app")
    result = p_verifier.verify(timeout_sec=0)

    assert result.success is True
    assert "Timeout reached" in result.reason

@patch("devops_bench.agents.verifier.verifiers.scaling_complete_verifier.KubeWatchService")
@patch("devops_bench.agents.verifier.verifiers.scaling_complete_verifier.ScalingCompleteVerifier._check_scaling")
def test_scaling_complete_verifier_verify_watch_success(mock_check_scaling, mock_watch_class):
    mock_check_scaling.side_effect = [
        # Initial check fails
        (False, {"reason": "Ready replicas < min replicas"}),
        # Event check succeeds
        (True, {"reason": "Ready replicas >= min replicas"}),
    ]

    mock_watcher = MagicMock()
    def mock_start():
        mock_watcher.callback({"kind": "deployment", "name": "my-dep", "namespace": "default"})
    mock_watcher.start.side_effect = mock_start

    def mock_init(callback, *args, **kwargs):
        mock_watcher.callback = callback
        return mock_watcher
    mock_watch_class.side_effect = mock_init

    s_verifier = ScalingCompleteVerifier(deployment="my-dep", min_replicas=2)
    result = s_verifier.verify(timeout_sec=60)

    assert result.success is True
    assert "event-driven" in result.reason

@patch("devops_bench.agents.verifier.verifiers.pod_healthy_verifier.KubeWatchService")
@patch("devops_bench.agents.verifier.verifiers.scaling_complete_verifier.KubeWatchService")
@patch("devops_bench.agents.verifier.verifiers.pod_healthy_verifier.PodHealthyVerifier._get_pods_details")
@patch("devops_bench.agents.verifier.verifiers.scaling_complete_verifier.ScalingCompleteVerifier._check_scaling")
def test_wait_for_condition_compound_success(
    mock_check_scaling, mock_get_pods_details, mock_scaling_watcher_class, mock_pod_watcher_class, verifier_agent
):
    # Pod checker: fails first, succeeds second
    mock_get_pods_details.side_effect = [
        {"items": [{"status": {"phase": "Pending"}}]},
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
    ]

    # Scaling checker: fails first, succeeds second
    mock_check_scaling.side_effect = [
        (False, {"reason": "Ready replicas < min replicas"}),
        (True, {"reason": "Ready replicas >= min replicas"}),
    ]

    # Setup pod watcher
    mock_p_watcher = MagicMock()
    def mock_p_start():
        mock_p_watcher.callback({"kind": "pod"})
    mock_p_watcher.start.side_effect = mock_p_start
    mock_pod_watcher_class.side_effect = lambda callback, *args, **kwargs: setattr(mock_p_watcher, 'callback', callback) or mock_p_watcher

    # Setup scaling watcher
    mock_s_watcher = MagicMock()
    def mock_s_start():
        mock_s_watcher.callback({"kind": "deployment", "name": "my-dep"})
    mock_s_watcher.start.side_effect = mock_s_start
    mock_scaling_watcher_class.side_effect = lambda callback, *args, **kwargs: setattr(mock_s_watcher, 'callback', callback) or mock_s_watcher

    spec = {
        "pod_spec": {"type": "pod_healthy", "selector": "app=my-app"},
        "scaling_spec": {"type": "scaling_complete", "deployment": "my-dep", "min_replicas": 2}
    }
    result = verifier_agent.wait_for_condition(spec, timeout_sec=60)

    assert result.success is True
    assert "pod_spec succeeded" in result.reason
    assert "scaling_spec succeeded" in result.reason

@patch("devops_bench.agents.verifier.verifiers.pod_healthy_verifier.KubeWatchService")
@patch("devops_bench.agents.verifier.verifiers.scaling_complete_verifier.KubeWatchService")
@patch("devops_bench.agents.verifier.verifiers.pod_healthy_verifier.PodHealthyVerifier._get_pods_details")
@patch("devops_bench.agents.verifier.verifiers.scaling_complete_verifier.ScalingCompleteVerifier._check_scaling")
@patch("threading.Event.wait")
def test_wait_for_condition_compound_failure(
    mock_event_wait, mock_check_scaling, mock_get_pods_details, mock_scaling_watcher_class, mock_pod_watcher_class, verifier_agent
):
    mock_event_wait.return_value = False
    # Pod checker always fails
    mock_get_pods_details.return_value = {"items": []}

    # Scaling checker fails first, succeeds second
    mock_check_scaling.side_effect = [
        (False, {"reason": "Ready replicas < min replicas"}),
        (True, {"reason": "Ready replicas >= min replicas"}),
    ]

    mock_p_watcher = MagicMock()
    mock_pod_watcher_class.side_effect = lambda callback, *args, **kwargs: mock_p_watcher

    mock_s_watcher = MagicMock()
    def mock_s_start():
        mock_s_watcher.callback({"kind": "deployment", "name": "my-dep"})
    mock_s_watcher.start.side_effect = mock_s_start
    mock_scaling_watcher_class.side_effect = lambda callback, *args, **kwargs: setattr(mock_s_watcher, 'callback', callback) or mock_s_watcher

    spec = {
        "scaling_spec": {"type": "scaling_complete", "deployment": "my-dep", "min_replicas": 2},
        "pod_spec": {"type": "pod_healthy", "selector": "app=my-app"}
    }
    result = verifier_agent.wait_for_condition(spec, timeout_sec=0.01)

    assert result.success is False
    assert "pod_spec failed" in result.reason
    assert "scaling_spec succeeded" in result.reason

