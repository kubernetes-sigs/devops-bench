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

"""Unit tests for the http_probe verifier. ``run_pod`` is patched throughout."""

from __future__ import annotations

from typing import Any

from pytest_mock import MockerFixture

from devops_bench.core import SubprocessError
from devops_bench.k8s.conditions import poll_until as _real_poll_until
from devops_bench.verification import VerificationSpec
from devops_bench.verification.verifiers.http_probe import HttpProbeVerifier


def _patch_run_pod(mocker: MockerFixture, output: str) -> Any:
    return mocker.patch(
        "devops_bench.verification.verifiers.http_probe.run_pod", return_value=output
    )


def test_registered_via_spec() -> None:
    node = VerificationSpec({"type": "http_probe", "url": "http://svc"}).root
    assert isinstance(node, HttpProbeVerifier)
    assert node.expect_status == 200


def test_probe_passes_on_expected_status(mocker: MockerFixture) -> None:
    _patch_run_pod(mocker, "hello world\n200")
    v = HttpProbeVerifier.model_validate({"type": "http_probe", "url": "http://svc"})
    result = v.verify(30)
    assert result.success is True
    assert result.status == "pass"


def test_probe_fails_on_wrong_status(mocker: MockerFixture) -> None:
    _patch_run_pod(mocker, "not found\n404")
    v = HttpProbeVerifier.model_validate({"type": "http_probe", "url": "http://svc"})
    result = v.verify(0)
    assert result.success is False
    assert result.status == "fail"
    assert "404" in result.reason


def test_probe_body_match_required(mocker: MockerFixture) -> None:
    _patch_run_pod(mocker, "unexpected\n200")
    v = HttpProbeVerifier.model_validate(
        {"type": "http_probe", "url": "http://svc", "expect_body_matches": "hello"}
    )
    result = v.verify(0)
    assert result.success is False
    assert result.status == "fail"


def test_probe_body_match_passes(mocker: MockerFixture) -> None:
    _patch_run_pod(mocker, "hello there\n200")
    v = HttpProbeVerifier.model_validate(
        {"type": "http_probe", "url": "http://svc", "expect_body_matches": "hello"}
    )
    result = v.verify(30)
    assert result.success is True
    assert result.status == "pass"


def test_probe_subprocess_error_is_an_error_not_a_fail(mocker: MockerFixture) -> None:
    mocker.patch(
        "devops_bench.verification.verifiers.http_probe.run_pod",
        side_effect=SubprocessError(["kubectl", "run"], returncode=1, stderr="boom"),
    )
    v = HttpProbeVerifier.model_validate({"type": "http_probe", "url": "http://svc"})
    result = v.verify(0)
    assert result.success is False
    assert result.status == "error"
    assert "kubectl run failed" in result.reason


def test_probe_connection_refused_is_a_fail_not_an_error(mocker: MockerFixture) -> None:
    """Connection refused is an observation curl made, not an evaluation failure."""
    mocker.patch(
        "devops_bench.verification.verifiers.http_probe.run_pod",
        side_effect=SubprocessError(
            ["kubectl", "run"],
            returncode=7,
            stderr='pod default/http-probe-abc123 terminated (Error)',
        ),
    )
    v = HttpProbeVerifier.model_validate({"type": "http_probe", "url": "http://svc"})
    result = v.verify(0)
    assert result.success is False
    assert result.status == "fail"
    assert "unreachable" in result.reason
    assert "7" in result.reason


def test_probe_connection_refused_classifies_as_fail_not_error(
    mocker: MockerFixture,
) -> None:
    """A real outage returns status "fail", not "error".

    A hold evaluator counts error samples in hold_error_count (which does not
    trigger a violation) and counts fail samples as violations. Classifying
    network unreachability as "fail" is therefore the property that prevents a
    real outage from failing a hold open. The hold machinery itself is not
    imported here because it lives outside this PR's scope, but the verifier's
    output contract is the load-bearing assertion.
    """
    mocker.patch(
        "devops_bench.verification.verifiers.http_probe.run_pod",
        side_effect=SubprocessError(
            ["kubectl", "run"],
            returncode=7,
            stderr='pod default/http-probe-abc123 terminated (Error)',
        ),
    )
    v = HttpProbeVerifier.model_validate({"type": "http_probe", "url": "http://svc"})
    result = v.verify(0.0)

    assert result.success is False
    assert result.status == "fail"
    assert "unreachable" in result.reason


def test_probe_kubectl_usage_error_stays_an_error(mocker: MockerFixture) -> None:
    """A kubectl exit code that overlaps the curl network-failure set is still an error

    when the pod never ran (no ``terminated (Error)`` marker in stderr), e.g. a bad
    flag or an API/auth failure.
    """
    mocker.patch(
        "devops_bench.verification.verifiers.http_probe.run_pod",
        side_effect=SubprocessError(
            ["kubectl", "run"],
            returncode=1,
            stderr="error: unable to connect to the server",
        ),
    )
    v = HttpProbeVerifier.model_validate({"type": "http_probe", "url": "http://svc"})
    result = v.verify(0)
    assert result.success is False
    assert result.status == "error"
    assert "kubectl run failed" in result.reason


def test_probe_parses_status_with_trailing_kubectl_noise(mocker: MockerFixture) -> None:
    """kubectl's `pod ... deleted` notice glued onto the code must not break parsing."""
    _patch_run_pod(
        mocker, 'Hello from hello-app\n200pod "http-probe-6836fa7d" deleted from default namespace'
    )
    v = HttpProbeVerifier.model_validate({"type": "http_probe", "url": "http://svc"})
    result = v.verify(0)
    assert result.success is True
    assert result.status == "pass"
    assert result.raw["status_code"] == 200


def test_probe_unparseable_output_is_an_error(mocker: MockerFixture) -> None:
    _patch_run_pod(mocker, "no-status-line")
    v = HttpProbeVerifier.model_validate({"type": "http_probe", "url": "http://svc"})
    result = v.verify(0)
    assert result.success is False
    assert result.status == "error"


def test_probe_polls_until_success(mocker: MockerFixture) -> None:
    """Converge mode retries a fresh probe until the expected status appears."""
    run_pod = mocker.patch(
        "devops_bench.verification.verifiers.http_probe.run_pod",
        side_effect=["down\n503", "down\n503", "hello world\n200"],
    )

    # Exercise the real poll loop but without real sleeping between attempts.
    def _fast_poll(predicate: Any, *, timeout_sec: float, **_: Any) -> bool:
        return _real_poll_until(
            predicate,
            timeout_sec=timeout_sec,
            initial_delay=0.0,
            max_delay=0.0,
            sleep=lambda _s: None,
        )

    mocker.patch("devops_bench.verification.base.poll_until", side_effect=_fast_poll)
    v = HttpProbeVerifier.model_validate({"type": "http_probe", "url": "http://svc"})
    result = v.verify(30)
    assert result.success is True
    assert run_pod.call_count == 3


def test_probe_assert_mode_is_single_shot(mocker: MockerFixture) -> None:
    """With no budget (assert/hold), the probe fires exactly once and does not retry."""
    run_pod = mocker.patch(
        "devops_bench.verification.verifiers.http_probe.run_pod",
        return_value="down\n503",
    )
    v = HttpProbeVerifier.model_validate({"type": "http_probe", "url": "http://svc"})
    result = v.verify(0.0)
    assert result.success is False
    assert run_pod.call_count == 1


def test_probe_host_header_added_when_set(mocker: MockerFixture) -> None:
    run_pod = _patch_run_pod(mocker, "hello world\n200")
    v = HttpProbeVerifier.model_validate(
        {"type": "http_probe", "url": "http://1.2.3.4", "host": "app.example.com"}
    )
    result = v.verify(0)
    assert result.success is True
    curl_cmd = run_pod.call_args.args[2]
    assert "-H" in curl_cmd
    assert curl_cmd[curl_cmd.index("-H") + 1] == "Host: app.example.com"
    assert curl_cmd[-1] == "http://1.2.3.4"


def test_probe_no_host_header_when_unset(mocker: MockerFixture) -> None:
    run_pod = _patch_run_pod(mocker, "hello world\n200")
    v = HttpProbeVerifier.model_validate({"type": "http_probe", "url": "http://svc"})
    result = v.verify(0)
    assert result.success is True
    curl_cmd = run_pod.call_args.args[2]
    assert "-H" not in curl_cmd
    assert curl_cmd == [
        "curl",
        "-s",
        "-w",
        r"\n%{http_code}",
        "--max-time=10",
        "http://svc",
    ]
