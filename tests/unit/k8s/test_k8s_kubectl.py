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

"""Unit tests for devops_bench.k8s.kubectl.

These patch the module-local ``run`` so no real ``kubectl`` is invoked, and
assert the exact argv lists and KUBECONFIG threading.
"""

import json
import subprocess

import pytest
from pytest_mock import MockerFixture

from devops_bench.core.context import ClusterInfo, RunContext
from devops_bench.core.errors import SubprocessError
from devops_bench.k8s import kubectl


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    """Build a real CompletedProcess for the patched ``run``."""
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout)


def test_wait_builds_argv_and_threads_kubeconfig(mocker: MockerFixture) -> None:
    mock_run = mocker.patch("devops_bench.k8s.kubectl.run", return_value=_completed())

    kubectl.wait(
        "pod",
        selector="app=my-app",
        timeout_sec=60,
        namespace="prod",
        kubeconfig="/tmp/kc",
    )

    argv = mock_run.call_args.args[0]
    assert argv == [
        "kubectl",
        "wait",
        "--for=condition=Ready",
        "pod",
        "-l",
        "app=my-app",
        "--timeout=60s",
        "-n",
        "prod",
    ]
    assert mock_run.call_args.kwargs["extra_env"] == {"KUBECONFIG": "/tmp/kc"}


def test_wait_custom_condition_without_optionals(mocker: MockerFixture) -> None:
    mock_run = mocker.patch("devops_bench.k8s.kubectl.run", return_value=_completed())

    kubectl.wait("deployment", for_condition="condition=Available", timeout_sec=30)

    argv = mock_run.call_args.args[0]
    assert argv == [
        "kubectl",
        "wait",
        "--for=condition=Available",
        "deployment",
        "--timeout=30s",
    ]
    # No kubeconfig supplied -> run is called without overlaying KUBECONFIG.
    assert mock_run.call_args.kwargs["extra_env"] is None


def test_get_resource_parses_output_and_builds_argv(mocker: MockerFixture) -> None:
    payload = {"items": [{"status": {"phase": "Running"}}]}
    mock_run = mocker.patch(
        "devops_bench.k8s.kubectl.run",
        return_value=_completed(stdout=json.dumps(payload)),
    )

    result = kubectl.get_resource("pods", selector="app=web", namespace="default")

    assert result == payload
    argv = mock_run.call_args.args[0]
    assert argv == [
        "kubectl",
        "get",
        "pods",
        "-l",
        "app=web",
        "-o",
        "json",
        "-n",
        "default",
    ]


def test_get_resource_with_name(mocker: MockerFixture) -> None:
    payload = {"status": {"readyReplicas": 3}}
    mock_run = mocker.patch(
        "devops_bench.k8s.kubectl.run",
        return_value=_completed(stdout=json.dumps(payload)),
    )

    result = kubectl.get_resource("deployment", "my-dep")

    assert result == payload
    argv = mock_run.call_args.args[0]
    assert argv == ["kubectl", "get", "deployment", "my-dep", "-o", "json"]


def test_apply_builds_argv(mocker: MockerFixture) -> None:
    mock_run = mocker.patch("devops_bench.k8s.kubectl.run", return_value=_completed())

    kubectl.apply("/manifests/app.yaml", namespace="staging")

    argv = mock_run.call_args.args[0]
    assert argv == ["kubectl", "apply", "-f", "/manifests/app.yaml", "-n", "staging"]


def test_rollout_status_with_timeout(mocker: MockerFixture) -> None:
    mock_run = mocker.patch("devops_bench.k8s.kubectl.run", return_value=_completed())

    kubectl.rollout_status("deployment/web", timeout_sec=120, namespace="prod")

    argv = mock_run.call_args.args[0]
    assert argv == [
        "kubectl",
        "rollout",
        "status",
        "deployment/web",
        "--timeout=120s",
        "-n",
        "prod",
    ]


def test_rollout_status_without_timeout(mocker: MockerFixture) -> None:
    mock_run = mocker.patch("devops_bench.k8s.kubectl.run", return_value=_completed())

    kubectl.rollout_status("deployment/web")

    argv = mock_run.call_args.args[0]
    assert argv == ["kubectl", "rollout", "status", "deployment/web"]


def test_kubeconfig_from_run_context(mocker: MockerFixture) -> None:
    mock_run = mocker.patch("devops_bench.k8s.kubectl.run", return_value=_completed())
    ctx = RunContext(
        task_id="t1",
        cluster=ClusterInfo(name="c1", kubeconfig_path="/ctx/kc"),
    )

    kubectl.wait("pod", timeout_sec=10, kubeconfig=ctx)

    assert mock_run.call_args.kwargs["extra_env"] == {"KUBECONFIG": "/ctx/kc"}


def test_kubeconfig_from_cluster_info(mocker: MockerFixture) -> None:
    mock_run = mocker.patch("devops_bench.k8s.kubectl.run", return_value=_completed())
    cluster = ClusterInfo(name="c1", kubeconfig_path="/cluster/kc")

    kubectl.wait("pod", timeout_sec=10, kubeconfig=cluster)

    assert mock_run.call_args.kwargs["extra_env"] == {"KUBECONFIG": "/cluster/kc"}


def test_run_context_without_cluster_omits_kubeconfig(mocker: MockerFixture) -> None:
    mock_run = mocker.patch("devops_bench.k8s.kubectl.run", return_value=_completed())
    ctx = RunContext(task_id="t1")

    kubectl.wait("pod", timeout_sec=10, kubeconfig=ctx)

    assert mock_run.call_args.kwargs["extra_env"] is None


def test_get_resource_propagates_invalid_json(mocker: MockerFixture) -> None:
    mocker.patch(
        "devops_bench.k8s.kubectl.run",
        return_value=_completed(stdout="not json"),
    )

    with pytest.raises(json.JSONDecodeError):
        kubectl.get_resource("pods")


def test_wait_propagates_subprocess_error(mocker: MockerFixture) -> None:
    mocker.patch(
        "devops_bench.k8s.kubectl.run",
        side_effect=SubprocessError(["kubectl", "wait"], returncode=1),
    )

    with pytest.raises(SubprocessError):
        kubectl.wait("pod", timeout_sec=10)


def test_port_forward_builds_argv_and_terminates_on_exit(mocker: MockerFixture) -> None:
    mock_popen = mocker.patch("devops_bench.k8s.kubectl.subprocess.Popen")
    proc = mock_popen.return_value
    proc.poll.return_value = None  # tunnel still alive after the settle window
    mock_sleep = mocker.patch("devops_bench.k8s.kubectl.time.sleep")

    with kubectl.port_forward("svc/web", 8080, namespace="prod"):
        # Tunnel is live inside the body; teardown has not run yet.
        proc.terminate.assert_not_called()

    argv = mock_popen.call_args.args[0]
    assert argv == [
        "kubectl",
        "port-forward",
        "svc/web",
        "8080:8080",
        "-n",
        "prod",
    ]
    # No kubeconfig -> child inherits the parent environment (env=None).
    assert mock_popen.call_args.kwargs["env"] is None
    # Settled before yielding, then terminated with a bounded grace period.
    mock_sleep.assert_called_once_with(kubectl._PORT_FORWARD_SETTLE_SEC)
    proc.terminate.assert_called_once()
    proc.wait.assert_called_once_with(timeout=kubectl._PORT_FORWARD_TERM_GRACE_SEC)
    proc.kill.assert_not_called()


def test_port_forward_uses_explicit_remote_port(mocker: MockerFixture) -> None:
    mock_popen = mocker.patch("devops_bench.k8s.kubectl.subprocess.Popen")
    mock_popen.return_value.poll.return_value = None
    mocker.patch("devops_bench.k8s.kubectl.time.sleep")

    with kubectl.port_forward("svc/web", 8080, remote_port=9090):
        pass

    assert mock_popen.call_args.args[0][3] == "8080:9090"


def test_port_forward_threads_kubeconfig_into_env(mocker: MockerFixture) -> None:
    mock_popen = mocker.patch("devops_bench.k8s.kubectl.subprocess.Popen")
    mock_popen.return_value.poll.return_value = None
    mocker.patch("devops_bench.k8s.kubectl.time.sleep")

    with kubectl.port_forward("svc/web", 8080, kubeconfig="/tmp/kc"):
        pass

    assert mock_popen.call_args.kwargs["env"]["KUBECONFIG"] == "/tmp/kc"


def test_port_forward_raises_when_process_exits_early(mocker: MockerFixture) -> None:
    proc = mocker.patch("devops_bench.k8s.kubectl.subprocess.Popen").return_value
    proc.poll.return_value = 1  # already exited by the time we check
    proc.returncode = 1
    mocker.patch("devops_bench.k8s.kubectl.time.sleep")

    with (
        pytest.raises(RuntimeError, match="exited early"),
        kubectl.port_forward("svc/web", 8080),
    ):
        pass

    # Early-exited child is reaped, not terminated.
    proc.wait.assert_called_once_with(timeout=kubectl._PORT_FORWARD_SETTLE_SEC)
    proc.terminate.assert_not_called()


def test_port_forward_escalates_to_kill_when_terminate_times_out(mocker: MockerFixture) -> None:
    proc = mocker.patch("devops_bench.k8s.kubectl.subprocess.Popen").return_value
    proc.poll.return_value = None
    # First wait (SIGTERM grace) times out; second wait (after SIGKILL) returns.
    proc.wait.side_effect = [
        subprocess.TimeoutExpired(cmd="kubectl", timeout=5),
        0,
    ]
    mocker.patch("devops_bench.k8s.kubectl.time.sleep")

    with kubectl.port_forward("svc/web", 8080):
        pass

    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()
    assert proc.wait.call_count == 2


def test_port_forward_abandons_process_that_survives_sigkill(mocker: MockerFixture) -> None:
    proc = mocker.patch("devops_bench.k8s.kubectl.subprocess.Popen").return_value
    proc.poll.return_value = None
    # Both the SIGTERM grace wait and the post-SIGKILL wait time out.
    proc.wait.side_effect = subprocess.TimeoutExpired(cmd="kubectl", timeout=5)
    mocker.patch("devops_bench.k8s.kubectl.time.sleep")

    # Teardown must neither raise nor hang even when the process never dies.
    with kubectl.port_forward("svc/web", 8080):
        pass

    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()
    assert proc.wait.call_count == 2
