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

"""Verifier that GETs a service's external address from the verifier host (off-cluster)."""

from __future__ import annotations

import re
import ssl
import urllib.error
import urllib.request
from typing import Any, Literal

from pydantic import model_validator

from devops_bench.core import SubprocessError, get_logger
from devops_bench.k8s import get_resource
from devops_bench.verification.base import (
    VERIFIERS,
    BaseVerifier,
    VerificationResult,
    VerificationStatus,
)

__all__ = ["ExternalHttpProbeVerifier"]

_log = get_logger("verification.external_http_probe")

# Fixed budget added on top of ``probe_timeout`` for the ``kubectl get`` call
# itself (process startup, API server round trip), so discovery is never left
# unbounded even though it is not the operation the caller is timing.
_KUBECTL_OVERHEAD_SEC = 5

# Each poll attempt below bounds its own I/O with probe_timeout (and the
# discovery call's small overhead on top), independent of the poll budget
# verify() receives. This is deliberately not base.py's single_call_timeout,
# which floors a caller's near-zero remaining budget up to a generous 30s:
# that is right for a converging leaf that should use whatever budget it has
# left, but wrong here, where each attempt must stay short so poll_until can
# retry frequently while a LoadBalancer address is still provisioning.


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirect handler that refuses to follow, so a probe observes the address's own status."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


# TLS context that does not verify identity: this is a reachability probe against
# a raw external IP (a LoadBalancer address has no matching certificate), so we
# check that traffic reaches the endpoint and it answers, not who it claims to be.
_UNVERIFIED_TLS = ssl.create_default_context()
_UNVERIFIED_TLS.check_hostname = False
_UNVERIFIED_TLS.verify_mode = ssl.CERT_NONE

# Opener that (a) bypasses any ambient HTTP(S)_PROXY so a direct probe of a raw
# external IP is never rerouted, (b) does not follow 3xx redirects so the reported
# status is the one the target address itself returned (a redirect elsewhere
# cannot masquerade as the app's 200), and (c) does not verify TLS identity.
_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=_UNVERIFIED_TLS),
    _NoRedirect,
)


def _fmt_host(addr: str) -> str:
    """Bracket a bare IPv6 address for use in a URL authority."""
    return f"[{addr}]" if ":" in addr and not addr.startswith("[") else addr


def _http_get(url: str, timeout: float) -> tuple[int | None, str, str]:
    """GET ``url`` from this host.

    Returns ``(status_code, body, error)``. ``status_code`` is None only on a
    transport failure (connection refused, DNS, timeout); any HTTP response,
    including a 3xx (not followed) or 4xx/5xx, returns its integer code with an
    empty error.
    """
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "devops-bench-probe"})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            body = resp.read(4096).decode("utf-8", errors="replace")
            return resp.status, body, ""
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(4096).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            body = ""
        return exc.code, body, ""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, "", str(exc)


@VERIFIERS.register("external_http_probe")
class ExternalHttpProbeVerifier(BaseVerifier):
    """Verify internet reachability by GETting a service's external address off-cluster.

    Discovers the external address assigned to a LoadBalancer Service (or Ingress)
    -- ``status.loadBalancer.ingress[0].ip`` or ``.hostname`` -- and issues an HTTP
    GET from the verifier host. Because the host is not a cluster node and cannot
    route a ClusterIP, a success proves traffic transited the external network:
    real internet exposure, independent of which mechanism assigned the address.
    Polls via ``_poll_to_result`` so it converges while the LoadBalancer provisions.

    Limitations: it grades that *the discovered external address* answered with the
    expected status, not the identity of the backend. If a caller needs to tie the
    response to a specific app, set ``expect_body_matches`` to a response marker.
    With neither ``name`` nor ``selector``, discovery scans the whole ``namespace``
    and probes the first Service carrying an external address (intended for a
    dedicated single-app namespace); a ``selector`` matching several Services
    behaves the same way. TLS identity is not verified (this is a reachability
    probe against a raw IP). Gateway API discovery (``status.addresses``) is not
    yet supported.

    Attributes:
        kind: Resource kind carrying the external address; ``"service"`` (default)
            or ``"ingress"`` (both expose ``status.loadBalancer.ingress``).
        name: Specific resource name. At most one of ``name`` / ``selector``;
            with neither set, every Service in ``namespace`` is scanned and the
            first carrying an external address is probed.
        selector: Label selector (``-l``). At most one of ``name`` / ``selector``;
            with neither set, every Service in ``namespace`` is scanned and the
            first carrying an external address is probed.
        namespace: Namespace of the resource; active context when None.
        scheme: ``"http"`` (default) or ``"https"``.
        port: External port. When None, use the scheme's conventional port
            (80/443) if the Service exposes it, else the Service's first port,
            else 80 (http) / 443 (https).
        path: Request path; default ``"/"``.
        expect_status: Expected HTTP status; default 200.
        expect_body_matches: Optional regex applied to the response body.
        probe_timeout: Per-request timeout in seconds; default 10. Also bounds
            the ``kubectl get`` discovery call, plus a small fixed overhead.
    """

    type: Literal["external_http_probe"] = "external_http_probe"
    kind: str = "service"
    name: str | None = None
    selector: str | None = None
    namespace: str | None = None
    scheme: Literal["http", "https"] = "http"
    port: int | None = None
    path: str = "/"
    expect_status: int = 200
    expect_body_matches: str | None = None
    probe_timeout: int = 10

    @model_validator(mode="after")
    def _validate(self) -> ExternalHttpProbeVerifier:
        if self.name is not None and self.selector is not None:
            raise ValueError("provide at most one of 'name' or 'selector'")
        if self.name is None and self.selector is None and not self.namespace:
            raise ValueError("provide a 'name', a 'selector', or a 'namespace' to scope discovery")
        return self

    def verify(self, timeout_sec: float) -> VerificationResult:
        """Poll until the external address serves the expected response or time runs out."""
        return self._poll_to_result(self._check, timeout_sec)

    def _objects(self) -> list[dict[str, Any]]:
        discovery_timeout = self.probe_timeout + _KUBECTL_OVERHEAD_SEC
        if self.name is not None:
            obj = get_resource(
                self.kind,
                self.name,
                namespace=self.namespace,
                kubeconfig=self.kubeconfig,
                timeout=discovery_timeout,
            )
            return [obj]
        result = get_resource(
            self.kind,
            selector=self.selector,
            namespace=self.namespace,
            kubeconfig=self.kubeconfig,
            timeout=discovery_timeout,
        )
        return result.get("items", [])

    @staticmethod
    def _address(obj: dict[str, Any]) -> str | None:
        ingress = ((obj.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
        for entry in ingress:
            if isinstance(entry, dict):
                addr = entry.get("ip") or entry.get("hostname")
                if addr:
                    return addr
        return None

    def _resolve_port(self, obj: dict[str, Any]) -> int:
        if self.port is not None:
            return self.port
        default_num = 443 if self.scheme == "https" else 80
        ports = (obj.get("spec") or {}).get("ports") or []
        numbered = [
            int(p["port"]) for p in ports if isinstance(p, dict) and p.get("port") is not None
        ]
        if default_num in numbered:
            return default_num
        return numbered[0] if numbered else default_num

    def _check(self) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        try:
            objects = self._objects()
        except SubprocessError as exc:
            stderr = (exc.stderr or "").strip()
            _log.warning("Failed to get %s: %s", self.kind, stderr)
            return "error", f"kubectl get {self.kind} failed: {stderr}", None
        except Exception as exc:  # noqa: BLE001 - an unhandled listing failure is a check error
            _log.warning("Unexpected error listing %s: %s", self.kind, exc)
            return "error", f"unexpected error listing {self.kind}: {exc}", None
        try:
            if not objects:
                return "fail", f"no {self.kind} matched", None
            target = next(((o, a) for o in objects if (a := self._address(o))), None)
            if target is None:
                return "fail", f"no external address on {self.kind} yet", None
            obj, address = target
            port = self._resolve_port(obj)
            url = f"{self.scheme}://{_fmt_host(address)}:{port}{self.path}"
        except Exception as exc:  # noqa: BLE001 - an unhandled object shape is a check error
            return "error", f"unexpected error resolving {self.kind} address: {exc}", None
        status, body, err = _http_get(url, self.probe_timeout)
        raw: dict[str, Any] = {"url": url, "address": address, "port": port, "status_code": status}
        if status is None:
            return "fail", f"{url} unreachable: {err}", raw
        if status != self.expect_status:
            return "fail", f"expected HTTP {self.expect_status}, got {status} from {url}", raw
        if self.expect_body_matches and not re.search(self.expect_body_matches, body):
            return "fail", f"body did not match {self.expect_body_matches!r} at {url}", raw
        return "pass", f"HTTP {status} from {url}", raw
