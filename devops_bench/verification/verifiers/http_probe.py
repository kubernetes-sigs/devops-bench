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
        expect_body_matches: Optional regex applied to the response body.
        namespace: Namespace the ephemeral pod runs in; active context when None.
        probe_timeout: Seconds the curl command may run. The ``kubectl run``
            call is bounded by this value plus :data:`_KUBECTL_OVERHEAD_SEC`.
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
        return self._poll_to_result(self._probe_check, timeout_sec)

    def _probe_check(self) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        """Run one probe attempt, folding kubectl/unexpected errors into an error status."""
        try:
            return self._run_probe()
        except SubprocessError as exc:
            stderr = (exc.stderr or "").strip()
            _log.warning("http_probe kubectl run failed for %s: %s", self.url, stderr)
            return "error", f"kubectl run failed: {stderr}", None
        except Exception as exc:  # noqa: BLE001 - surface unexpected errors as check errors
            _log.warning("http_probe unexpected error for %s: %s", self.url, exc)
            return "error", f"unexpected error: {exc}", None

    def _run_probe(self) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        """Launch an ephemeral curl pod and evaluate its output."""
        pod_name = f"http-probe-{uuid.uuid4().hex[:8]}"
        curl_cmd = [
            "curl",
            "-s",
            "-w",
            r"\n%{http_code}",
            f"--max-time={self.probe_timeout}",
        ]
        if self.host is not None:
            curl_cmd += ["-H", f"Host: {self.host}"]
        curl_cmd.append(self.url)
        # Bounded by probe_timeout plus overhead rather than single_call_timeout:
        # a converge-mode retry needs a bound tight to this one attempt so the
        # next attempt fires promptly, not single_call_timeout's near-zero floor
        # meant only to protect an assert-mode call with no real budget.
        output = run_pod(
            pod_name,
            "curlimages/curl",
            curl_cmd,
            namespace=self.namespace,
            kubeconfig=self.kubeconfig,
            timeout=self.probe_timeout + _KUBECTL_OVERHEAD_SEC,
        ).rstrip()

        lines = output.rsplit("\n", 1)
        if len(lines) < 2:
            return "error", f"unexpected curl output: {output!r}", None

        body, status_line = lines
        match = re.match(r"\s*(\d{3})", status_line)
        if not match:
            return "error", f"could not parse HTTP status from {status_line!r}", None
        status_code = int(match.group(1))

        raw: dict[str, Any] = {"status_code": status_code, "body_length": len(body)}

        if status_code != self.expect_status:
            return (
                "fail",
                f"expected HTTP {self.expect_status}, got {status_code} from {self.url}",
                raw,
            )
        if self.expect_body_matches and not re.search(self.expect_body_matches, body):
            return "fail", f"body did not match pattern {self.expect_body_matches!r}", raw
        return "pass", f"HTTP {status_code} from {self.url}", raw
