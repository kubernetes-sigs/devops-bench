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
from devops_bench.verification.verifiers.http_probe import (
    _BODY_MATCH_LIMIT_CHARS,
    _HTTP_STATUS_MARKER,
    _KUBECTL_OVERHEAD_SEC,
    HttpProbeVerifier,
)


def _curl_output(body: str, status: int, trailer: str = "") -> str:
    """Build a curl ``-w`` write-out string the way the verifier's real curl call does."""
    return f"{body}\n{_HTTP_STATUS_MARKER}{status}{trailer}"


def _patch_run_pod(mocker: MockerFixture, output: str) -> Any:
    return mocker.patch(
        "devops_bench.verification.verifiers.http_probe.run_pod", return_value=output
    )


def test_registered_via_spec() -> None:
    node = VerificationSpec({"type": "http_probe", "url": "http://svc"}).root
    assert isinstance(node, HttpProbeVerifier)
    assert node.expect_status == 200


def test_probe_passes_on_expected_status(mocker: MockerFixture) -> None:
    _patch_run_pod(mocker, _curl_output("hello world", 200))
    v = HttpProbeVerifier.model_validate({"type": "http_probe", "url": "http://svc"})
    result = v.verify(30)
    assert result.success is True
    assert result.status == "pass"


def test_probe_fails_on_wrong_status(mocker: MockerFixture) -> None:
    _patch_run_pod(mocker, _curl_output("not found", 404))
    v = HttpProbeVerifier.model_validate({"type": "http_probe", "url": "http://svc"})
    result = v.verify(0)
    assert result.success is False
    assert result.status == "fail"
    assert "404" in result.reason


def test_probe_body_match_required(mocker: MockerFixture) -> None:
    _patch_run_pod(mocker, _curl_output("unexpected", 200))
    v = HttpProbeVerifier.model_validate(
        {"type": "http_probe", "url": "http://svc", "expect_body_matches": "hello"}
    )
    result = v.verify(0)
    assert result.success is False
    assert result.status == "fail"


def test_probe_body_match_passes(mocker: MockerFixture) -> None:
    _patch_run_pod(mocker, _curl_output("hello there", 200))
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
            stderr="pod default/http-probe-abc123 terminated (Error)",
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
            stderr="pod default/http-probe-abc123 terminated (Error)",
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
        mocker,
        _curl_output(
            "Hello from hello-app",
            200,
            trailer='pod "http-probe-6836fa7d" deleted from default namespace',
        ),
    )
    v = HttpProbeVerifier.model_validate({"type": "http_probe", "url": "http://svc"})
    result = v.verify(0)
    assert result.success is True
    assert result.status == "pass"
    assert result.raw["status_code"] == 200


def test_probe_parses_status_with_trailing_kubectl_noise_on_its_own_line(
    mocker: MockerFixture,
) -> None:
    """A deletion notice on its own line after the marker must not be read as the status line.

    This is the regression the marker guards against: a naive "last line is the
    status" parser would read the deletion notice as the status line and turn
    this valid response into an error.
    """
    _patch_run_pod(
        mocker,
        _curl_output(
            "hello world",
            200,
            trailer='\npod "http-probe-6836fa7d" deleted from default namespace',
        ),
    )
    v = HttpProbeVerifier.model_validate({"type": "http_probe", "url": "http://svc"})
    result = v.verify(0)
    assert result.success is True
    assert result.status == "pass"
    assert result.raw["status_code"] == 200
    assert result.raw["body_length"] == len("hello world")


def test_probe_marker_like_text_in_body_does_not_shadow_real_status(
    mocker: MockerFixture,
) -> None:
    """A response body containing marker-like text must not shadow the real status.

    curl's ``-w`` write-out always appends the real status last; a body that
    happens to contain text shaped like the marker must not be mistaken for
    it.
    """
    body = f"unexpected body content {_HTTP_STATUS_MARKER}200 embedded"
    _patch_run_pod(mocker, _curl_output(body, 503))
    v = HttpProbeVerifier.model_validate({"type": "http_probe", "url": "http://svc"})
    result = v.verify(0)
    assert result.success is False
    assert result.status == "fail"
    assert result.raw["status_code"] == 503


def test_probe_missing_marker_is_an_error(mocker: MockerFixture) -> None:
    """Output with no status marker at all is an error, not a misread status."""
    _patch_run_pod(mocker, "no-status-marker")
    v = HttpProbeVerifier.model_validate({"type": "http_probe", "url": "http://svc"})
    result = v.verify(0)
    assert result.success is False
    assert result.status == "error"


def test_probe_unparseable_output_is_an_error(mocker: MockerFixture) -> None:
    _patch_run_pod(mocker, "no-status-line")
    v = HttpProbeVerifier.model_validate({"type": "http_probe", "url": "http://svc"})
    result = v.verify(0)
    assert result.success is False
    assert result.status == "error"


def test_probe_body_match_is_bounded(mocker: MockerFixture) -> None:
    """Only the leading slice of the body is matched, so a huge body cannot stall the regex."""
    body = ("a" * _BODY_MATCH_LIMIT_CHARS) + "needle"
    _patch_run_pod(mocker, _curl_output(body, 200))
    v = HttpProbeVerifier.model_validate(
        {"type": "http_probe", "url": "http://svc", "expect_body_matches": "needle"}
    )
    result = v.verify(0)
    assert result.success is False
    assert result.status == "fail"
    # body_length still reports the full observed body, not the matched slice.
    assert result.raw["body_length"] == len(body)


def test_probe_polls_until_success(mocker: MockerFixture) -> None:
    """Converge mode retries a fresh probe until the expected status appears."""
    run_pod = mocker.patch(
        "devops_bench.verification.verifiers.http_probe.run_pod",
        side_effect=[
            _curl_output("down", 503),
            _curl_output("down", 503),
            _curl_output("hello world", 200),
        ],
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
    # Each retry must launch a fresh pod name: reusing one would collide with a
    # prior attempt's pod that has not finished terminating.
    pod_names = [call.args[0] for call in run_pod.call_args_list]
    assert len(set(pod_names)) == 3


def test_probe_assert_mode_is_single_shot(mocker: MockerFixture) -> None:
    """With no budget (assert/hold), the probe fires exactly once and does not retry."""
    run_pod = mocker.patch(
        "devops_bench.verification.verifiers.http_probe.run_pod",
        return_value=_curl_output("down", 503),
    )
    v = HttpProbeVerifier.model_validate({"type": "http_probe", "url": "http://svc"})
    result = v.verify(0.0)
    assert result.success is False
    assert run_pod.call_count == 1


def test_run_probe_clamps_pod_timeout_to_remaining_deadline(mocker: MockerFixture) -> None:
    """One probe attempt must not use more of the pod timeout than remains on the deadline."""
    run_pod = _patch_run_pod(mocker, _curl_output("hello world", 200))
    v = HttpProbeVerifier.model_validate({"type": "http_probe", "url": "http://svc"})
    status, _, _ = v._run_probe(3.0)
    assert status == "pass"
    assert run_pod.call_args.kwargs["timeout"] == 3.0


def test_run_probe_unclamped_when_remaining_is_none(mocker: MockerFixture) -> None:
    """``remaining=None`` (assert/hold mode) keeps the full probe_timeout + overhead budget."""
    run_pod = _patch_run_pod(mocker, _curl_output("hello world", 200))
    v = HttpProbeVerifier.model_validate({"type": "http_probe", "url": "http://svc"})
    status, _, _ = v._run_probe(None)
    assert status == "pass"
    assert run_pod.call_args.kwargs["timeout"] == v.probe_timeout + _KUBECTL_OVERHEAD_SEC


def test_probe_converge_check_clamps_pod_timeout_to_remaining_deadline(
    mocker: MockerFixture,
) -> None:
    """A converge attempt with little deadline left gets a clamped run_pod timeout."""
    run_pod = _patch_run_pod(mocker, _curl_output("hello world", 200))
    mocker.patch(
        "devops_bench.verification.verifiers.http_probe.time.monotonic",
        side_effect=[100.0, 100.0, 103.0, 103.0],
    )
    mocker.patch(
        "devops_bench.verification.base.poll_until",
        side_effect=lambda predicate, *, timeout_sec, **_: predicate(),
    )
    v = HttpProbeVerifier.model_validate({"type": "http_probe", "url": "http://svc"})
    result = v.verify(5.0)
    assert result.success is True
    assert run_pod.call_args.kwargs["timeout"] == 2.0


def test_probe_converge_check_with_small_timeout_below_floor_still_clamps(
    mocker: MockerFixture,
) -> None:
    """A small positive converge timeout, below any leaf-budget floor, is still bounded.

    A positive timeout_sec must not be mistaken for the assert/hold sentinel
    (always exactly 0.0) just because it is small; the pod timeout must still
    clamp to the remaining deadline.
    """
    run_pod = _patch_run_pod(mocker, _curl_output("hello world", 200))
    mocker.patch(
        "devops_bench.verification.verifiers.http_probe.time.monotonic",
        side_effect=[100.0, 100.0, 100.25, 100.25],
    )
    mocker.patch(
        "devops_bench.verification.base.poll_until",
        side_effect=lambda predicate, *, timeout_sec, **_: predicate(),
    )
    v = HttpProbeVerifier.model_validate({"type": "http_probe", "url": "http://svc"})
    result = v.verify(0.5)
    assert result.success is True
    assert run_pod.call_args.kwargs["timeout"] == 0.25


def test_probe_converge_check_skips_pod_when_deadline_exhausted(
    mocker: MockerFixture,
) -> None:
    """No pod is launched once the converge deadline has nothing left before an attempt."""
    run_pod = _patch_run_pod(mocker, _curl_output("hello world", 200))
    mocker.patch(
        "devops_bench.verification.verifiers.http_probe.time.monotonic",
        side_effect=[100.0, 100.0, 106.0, 106.0],
    )
    mocker.patch(
        "devops_bench.verification.base.poll_until",
        side_effect=lambda predicate, *, timeout_sec, **_: predicate(),
    )
    v = HttpProbeVerifier.model_validate({"type": "http_probe", "url": "http://svc"})
    result = v.verify(5.0)
    assert result.status == "error"
    assert "no time remaining" in result.reason
    run_pod.assert_not_called()


def test_probe_assert_mode_pod_timeout_stays_unclamped(mocker: MockerFixture) -> None:
    """Assert/hold mode keeps the full probe_timeout + overhead budget, unclamped."""
    run_pod = _patch_run_pod(mocker, _curl_output("hello world", 200))
    v = HttpProbeVerifier.model_validate({"type": "http_probe", "url": "http://svc"})
    result = v.verify(0.0)
    assert result.success is True
    assert run_pod.call_args.kwargs["timeout"] == v.probe_timeout + _KUBECTL_OVERHEAD_SEC


def test_probe_host_header_added_when_set(mocker: MockerFixture) -> None:
    run_pod = _patch_run_pod(mocker, _curl_output("hello world", 200))
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
    run_pod = _patch_run_pod(mocker, _curl_output("hello world", 200))
    v = HttpProbeVerifier.model_validate({"type": "http_probe", "url": "http://svc"})
    result = v.verify(0)
    assert result.success is True
    curl_cmd = run_pod.call_args.args[2]
    assert "-H" not in curl_cmd
    assert curl_cmd == [
        "curl",
        "-s",
        "-w",
        "\n" + _HTTP_STATUS_MARKER + "%{http_code}",
        "--max-time=10",
        "http://svc",
    ]
