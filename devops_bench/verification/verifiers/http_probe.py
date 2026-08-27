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

"""Verifier that issues an HTTP request from inside the cluster via an ephemeral pod."""

from __future__ import annotations

import re
import time
import uuid
from typing import Any, Literal

from devops_bench.core import SubprocessError, get_logger
from devops_bench.k8s import run_pod
from devops_bench.verification.base import (
    VERIFIERS,
    BaseVerifier,
    VerificationResult,
    VerificationStatus,
)

__all__ = ["HttpProbeVerifier"]

_log = get_logger("verification.http_probe")

# Seconds of slack added to ``probe_timeout`` when bounding the ``kubectl run``
# call, so the pod launch/attach/delete round trip has room beyond the curl
# request itself without leaving the call unbounded.
_KUBECTL_OVERHEAD_SEC = 30

# curl exit codes that mean the target was unreachable over the network (DNS
# failure, connection refused, timeout, TLS connect failure, empty reply,
# recv failure). These are observations, not evaluation errors: the probe pod
# ran successfully and curl itself reported the target could not be reached.
# See https://curl.se/libcurl/c/libcurl-errors.html.
_CURL_NETWORK_FAILURE_EXIT_CODES: dict[int, str] = {
    6: "could not resolve host",
    7: "connection refused",
    28: "operation timed out",
    35: "TLS connect error",
    52: "empty reply from server",
    56: "failure in receiving network data",
}

# Substring kubectl's stderr carries when an ephemeral ``--rm`` pod's
# container ran to completion and exited non-zero, as opposed to kubectl
# itself failing before the pod ever ran (bad flags, API/auth errors). Only
# in the former case is the container's exit code (surfaced as kubectl's own
# returncode) meaningful as a curl exit code.
_POD_TERMINATED_ERROR_MARKER = "terminated (Error)"

# Upper bound on the response-body characters handed to ``re.search``. The
# probed service is deployed by the agent under evaluation, so its body is
# untrusted input: an adversarial body paired with a backtracking-prone
# pattern could otherwise burn evaluator CPU well past the verification
# budget. A match that only appears past this prefix is not seen; the
# reported ``body_length`` is still the full observed length.
_BODY_MATCH_LIMIT_CHARS = 64 * 1024

# Unique marker curl's ``-w`` write-out prefixes the HTTP status with, so the
# status can be found by searching for the marker rather than trusting that
# the final line of output is the status. ``kubectl run --rm`` sometimes
# appends its own pod-deletion notice after curl's output, which would
# otherwise be mistaken for the status line.
_HTTP_STATUS_MARKER = "__HTTP_STATUS__:"
_STATUS_MARKER_RE = re.compile(re.escape(_HTTP_STATUS_MARKER) + r"(\d{3})")


@VERIFIERS.register("http_probe")
class HttpProbeVerifier(BaseVerifier):
    """Verify HTTP reachability by curling the URL from inside the cluster.

    Launches an ephemeral ``curlimages/curl`` pod per attempt via
    ``kubectl run --rm`` and checks status code and, optionally, body content.
    In converge mode it retries a fresh pod until the expected response appears
    or the deadline passes; in assert and hold mode it fires exactly once. The
    request originates from inside the cluster, so this verifier validates
    ClusterIP/DNS reachability that a probe run from outside the cluster
    cannot reach. Reach for it when the service is not externally accessible;
    every use documents that the service can only be probed from inside the
    cluster.

    Attributes:
        type: Discriminator literal, always ``"http_probe"``.
        url: URL to probe (must be reachable from inside the cluster).
        expect_status: Expected HTTP status code; default 200.
        expect_body_matches: Optional regex applied to the response body. Only
            the first :data:`_BODY_MATCH_LIMIT_CHARS` characters are matched
            against, since the body is untrusted input.
        namespace: Namespace the ephemeral pod runs in; active context when None.
        probe_timeout: Seconds the curl command may run. The ``kubectl run``
            call is bounded by this value plus :data:`_KUBECTL_OVERHEAD_SEC`,
            clamped further to whatever remains on the converge deadline.
        host: Optional ``Host`` header value. Set this to probe a host-routed
            ingress path: ``url`` targets the edge/controller address while
            this header selects the virtual host. ``None`` (default) omits
            the header and preserves plain URL-based routing.
    """

    type: Literal["http_probe"] = "http_probe"
    url: str
    expect_status: int = 200
    expect_body_matches: str | None = None
    namespace: str | None = None
    probe_timeout: int = 10
    host: str | None = None

    def verify(self, timeout_sec: float) -> VerificationResult:
        """Probe the URL, retrying until it responds as expected or time runs out.

        In converge mode ``timeout_sec`` is the remaining budget, so a fresh
        curl pod is launched per attempt until the expected status (and body)
        is seen or the deadline passes. In assert and hold mode ``timeout_sec``
        is ``0``, so the probe fires exactly once.

        Args:
            timeout_sec: Maximum seconds to keep retrying the probe.

        Returns:
            The verification result carrying the last observed outcome.
        """
        # A positive timeout_sec, however small, is a shrinking converge
        # deadline; only the assert/hold sentinel (always exactly 0.0) keeps
        # the single attempt below at its full probe_timeout + overhead
        # budget, unclamped.
        bounded = timeout_sec > 0
        start = time.monotonic()

        def check() -> tuple[VerificationStatus, str, dict[str, Any] | None]:
            if not bounded:
                return self._probe_check(None)
            remaining = timeout_sec - (time.monotonic() - start)
            if remaining <= 0:
                return (
                    "error",
                    "no time remaining for probe attempt before converge deadline",
                    None,
                )
            return self._probe_check(remaining)

        return self._poll_to_result(check, timeout_sec)

    def _probe_check(
        self, remaining: float | None
    ) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        """Run one probe attempt, folding kubectl/unexpected errors into an error status.

        Args:
            remaining: Seconds left on the converge deadline, used to clamp
                this attempt's pod timeout; ``None`` in assert/hold mode,
                where the attempt keeps its full unclamped budget.
        """
        try:
            return self._run_probe(remaining)
        except SubprocessError as exc:
            stderr = (exc.stderr or "").strip()
            unreachable = self._classify_unreachable(exc)
            if unreachable is not None:
                curl_exit, description = unreachable
                _log.warning(
                    "http_probe found %s unreachable: curl exit %d (%s)",
                    self.url,
                    curl_exit,
                    description,
                )
                return (
                    "fail",
                    f"unreachable: curl exit {curl_exit} ({description})",
                    {"curl_exit": curl_exit},
                )
            _log.warning("http_probe kubectl run failed for %s: %s", self.url, stderr)
            return (
                "error",
                f"kubectl run failed (exit {exc.returncode}): {stderr}",
                {"kubectl_returncode": exc.returncode, "kubectl_stdout": exc.stdout},
            )
        except Exception as exc:  # noqa: BLE001 - surface unexpected errors as check errors
            _log.warning("http_probe unexpected error for %s: %s", self.url, exc)
            return "error", f"unexpected error: {exc}", None

    @staticmethod
    def _classify_unreachable(exc: SubprocessError) -> tuple[int, str] | None:
        """Return ``(curl_exit_code, description)`` when ``exc`` is a network-level probe miss.

        Distinguishes the ephemeral pod's container exiting non-zero (curl
        itself observed the target was unreachable) from kubectl failing to
        even run the pod (bad flags, API/auth errors). Only the former is a
        real observation the probe made; the latter stays an evaluation
        error, since we cannot say anything about the target's reachability.

        Args:
            exc: The ``SubprocessError`` raised by ``run_pod``.

        Returns:
            The matched curl exit code and its description, or ``None`` if
            ``exc`` does not represent a network-level unreachability.
        """
        stderr = exc.stderr or ""
        if _POD_TERMINATED_ERROR_MARKER not in stderr:
            return None
        description = _CURL_NETWORK_FAILURE_EXIT_CODES.get(exc.returncode)
        if description is None:
            return None
        return exc.returncode, description

    def _run_probe(
        self, remaining: float | None
    ) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        """Launch an ephemeral curl pod and evaluate its output.

        Args:
            remaining: Seconds left on the converge deadline; the pod's
                timeout is clamped to this when it is smaller than
                ``probe_timeout + _KUBECTL_OVERHEAD_SEC``. ``None`` leaves the
                pod timeout unclamped (assert/hold mode).
        """
        pod_name = f"http-probe-{uuid.uuid4().hex[:8]}"
        curl_cmd = [
            "curl",
            "-s",
            "-w",
            "\n" + _HTTP_STATUS_MARKER + "%{http_code}",
            f"--max-time={self.probe_timeout}",
        ]
        if self.host is not None:
            curl_cmd += ["-H", f"Host: {self.host}"]
        curl_cmd.append(self.url)
        # Bounded by probe_timeout plus overhead rather than single_call_timeout:
        # a converge-mode retry needs a bound tight to this one attempt so the
        # next attempt fires promptly, not single_call_timeout's near-zero floor
        # meant only to protect an assert-mode call with no real budget. Also
        # clamped to whatever remains on the converge deadline, so this one
        # attempt cannot itself run past the deadline the caller budgeted.
        pod_timeout = self.probe_timeout + _KUBECTL_OVERHEAD_SEC
        if remaining is not None:
            pod_timeout = min(pod_timeout, remaining)
        # kubeconfig and context are both pinned to the run's cluster: the
        # ambient current-context is a mutable global any run can rewrite, and
        # an in-cluster probe pod has to launch against the cluster under
        # test, not whichever one happens to be ambient.
        output = run_pod(
            pod_name,
            "curlimages/curl",
            curl_cmd,
            namespace=self.namespace,
            kubeconfig=self.kubeconfig,
            context=self.context,
            timeout=pod_timeout,
        ).rstrip()

        matches = list(_STATUS_MARKER_RE.finditer(output))
        if not matches:
            return "error", f"unexpected curl output: {output!r}", None
        match = matches[-1]
        status_code = int(match.group(1))

        body = output[: match.start()]
        if body.endswith("\n"):
            body = body[:-1]

        raw: dict[str, Any] = {"status_code": status_code, "body_length": len(body)}

        if status_code != self.expect_status:
            return (
                "fail",
                f"expected HTTP {self.expect_status}, got {status_code} from {self.url}",
                raw,
            )
        if self.expect_body_matches and not re.search(
            self.expect_body_matches, body[:_BODY_MATCH_LIMIT_CHARS]
        ):
            return "fail", f"body did not match pattern {self.expect_body_matches!r}", raw
        return "pass", f"HTTP {status_code} from {self.url}", raw
