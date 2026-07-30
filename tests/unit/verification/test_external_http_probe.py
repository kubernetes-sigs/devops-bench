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

"""Unit tests for the external_http_probe verifier.

``get_resource`` and ``_http_get`` are patched throughout so no real kubectl or
network call runs. The verifier polls via ``_poll_to_result``; a single
immediate success needs no sleep.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError
from pytest_mock import MockerFixture

from devops_bench.core import SubprocessError
from devops_bench.k8s.conditions import poll_until as _real_poll_until
from devops_bench.verification import VerificationSpec
from devops_bench.verification.verifiers.external_http_probe import (
    ExternalHttpProbeVerifier,
    _NoRedirect,
)

_SERVICE_WITH_IP = {
    "spec": {"ports": [{"port": 80}]},
    "status": {"loadBalancer": {"ingress": [{"ip": "203.0.113.10"}]}},
}

_SERVICE_WITH_HOSTNAME = {
    "spec": {"ports": [{"port": 80}]},
    "status": {"loadBalancer": {"ingress": [{"hostname": "lb.example.com"}]}},
}

_SERVICE_NO_INGRESS = {
    "spec": {"ports": [{"port": 80}]},
    "status": {"loadBalancer": {"ingress": []}},
}

_SERVICE_NO_PORTS = {
    "spec": {},
    "status": {"loadBalancer": {"ingress": [{"ip": "203.0.113.10"}]}},
}


def _patch_get_resource(mocker: MockerFixture, payload: dict[str, Any]) -> Any:
    return mocker.patch(
        "devops_bench.verification.verifiers.external_http_probe.get_resource",
        return_value=payload,
    )


def _patch_http_get(
    mocker: MockerFixture,
    return_value: tuple[int | None, str, str] | None = None,
    side_effect: Any = None,
) -> Any:
    return mocker.patch(
        "devops_bench.verification.verifiers.external_http_probe._http_get",
        return_value=return_value,
        side_effect=side_effect,
    )


def test_registered_via_spec_with_defaults() -> None:
    node = VerificationSpec({"type": "external_http_probe", "selector": "app=web"}).root
    assert isinstance(node, ExternalHttpProbeVerifier)
    assert node.kind == "service"
    assert node.scheme == "http"
    assert node.port is None
    assert node.path == "/"
    assert node.expect_status == 200


def test_target_validation() -> None:
    with pytest.raises(ValidationError):
        ExternalHttpProbeVerifier.model_validate({"type": "external_http_probe"})
    with pytest.raises(ValidationError):
        ExternalHttpProbeVerifier.model_validate(
            {
                "type": "external_http_probe",
                "name": "web",
                "selector": "app=web",
            }
        )
    ExternalHttpProbeVerifier.model_validate(
        {"type": "external_http_probe", "namespace": "hello-app"}
    )


def test_ip_ingress_success(mocker: MockerFixture) -> None:
    _patch_get_resource(mocker, {"items": [_SERVICE_WITH_IP]})
    _patch_http_get(mocker, return_value=(200, "", ""))
    v = ExternalHttpProbeVerifier.model_validate(
        {"type": "external_http_probe", "selector": "app=web"}
    )
    result = v.verify(0)
    assert result.success is True
    assert result.status == "pass"
    assert result.raw["url"] == "http://203.0.113.10:80/"


def test_hostname_ingress_success(mocker: MockerFixture) -> None:
    _patch_get_resource(mocker, {"items": [_SERVICE_WITH_HOSTNAME]})
    _patch_http_get(mocker, return_value=(200, "", ""))
    v = ExternalHttpProbeVerifier.model_validate(
        {"type": "external_http_probe", "selector": "app=web"}
    )
    result = v.verify(0)
    assert result.success is True
    assert result.raw["url"] == "http://lb.example.com:80/"


def test_no_address_yet(mocker: MockerFixture) -> None:
    _patch_get_resource(mocker, {"items": [_SERVICE_NO_INGRESS]})
    v = ExternalHttpProbeVerifier.model_validate(
        {"type": "external_http_probe", "selector": "app=web"}
    )
    result = v.verify(0)
    assert result.success is False
    assert result.status == "fail"
    assert "no external address" in result.reason


def test_no_service_matched(mocker: MockerFixture) -> None:
    _patch_get_resource(mocker, {"items": []})
    v = ExternalHttpProbeVerifier.model_validate(
        {"type": "external_http_probe", "selector": "app=web"}
    )
    result = v.verify(0)
    assert result.success is False
    assert result.status == "fail"
    assert result.reason == "no service matched"


def test_http_get_returns_unexpected_status(mocker: MockerFixture) -> None:
    _patch_get_resource(mocker, {"items": [_SERVICE_WITH_IP]})
    _patch_http_get(mocker, return_value=(503, "", ""))
    v = ExternalHttpProbeVerifier.model_validate(
        {"type": "external_http_probe", "selector": "app=web"}
    )
    result = v.verify(0)
    assert result.success is False
    assert "503" in result.reason


def test_http_get_unreachable(mocker: MockerFixture) -> None:
    _patch_get_resource(mocker, {"items": [_SERVICE_WITH_IP]})
    _patch_http_get(mocker, return_value=(None, "", "Connection refused"))
    v = ExternalHttpProbeVerifier.model_validate(
        {"type": "external_http_probe", "selector": "app=web"}
    )
    result = v.verify(0)
    assert result.success is False
    assert "unreachable" in result.reason


def test_explicit_port_and_https_scheme(mocker: MockerFixture) -> None:
    _patch_get_resource(mocker, {"items": [_SERVICE_WITH_IP]})
    _patch_http_get(mocker, return_value=(200, "", ""))
    v = ExternalHttpProbeVerifier.model_validate(
        {
            "type": "external_http_probe",
            "selector": "app=web",
            "port": 8443,
            "scheme": "https",
        }
    )
    result = v.verify(0)
    assert result.success is True
    assert result.raw["url"] == "https://203.0.113.10:8443/"


def test_port_defaults_to_80_for_http_when_no_spec_ports(mocker: MockerFixture) -> None:
    _patch_get_resource(mocker, {"items": [_SERVICE_NO_PORTS]})
    _patch_http_get(mocker, return_value=(200, "", ""))
    v = ExternalHttpProbeVerifier.model_validate(
        {"type": "external_http_probe", "selector": "app=web"}
    )
    result = v.verify(0)
    assert result.success is True
    assert result.raw["url"] == "http://203.0.113.10:80/"


def test_port_defaults_to_443_for_https_when_no_spec_ports(mocker: MockerFixture) -> None:
    _patch_get_resource(mocker, {"items": [_SERVICE_NO_PORTS]})
    _patch_http_get(mocker, return_value=(200, "", ""))
    v = ExternalHttpProbeVerifier.model_validate(
        {"type": "external_http_probe", "selector": "app=web", "scheme": "https"}
    )
    result = v.verify(0)
    assert result.success is True
    assert result.raw["url"] == "https://203.0.113.10:443/"


def test_expect_body_matches_success(mocker: MockerFixture) -> None:
    _patch_get_resource(mocker, {"items": [_SERVICE_WITH_IP]})
    _patch_http_get(mocker, return_value=(200, "hello world", ""))
    v = ExternalHttpProbeVerifier.model_validate(
        {
            "type": "external_http_probe",
            "selector": "app=web",
            "expect_body_matches": "hello",
        }
    )
    result = v.verify(0)
    assert result.success is True


def test_expect_body_matches_failure(mocker: MockerFixture) -> None:
    _patch_get_resource(mocker, {"items": [_SERVICE_WITH_IP]})
    _patch_http_get(mocker, return_value=(200, "goodbye", ""))
    v = ExternalHttpProbeVerifier.model_validate(
        {
            "type": "external_http_probe",
            "selector": "app=web",
            "expect_body_matches": "hello",
        }
    )
    result = v.verify(0)
    assert result.success is False


def test_ipv6_address_is_bracketed(mocker: MockerFixture) -> None:
    service = {
        "spec": {"ports": [{"port": 80}]},
        "status": {"loadBalancer": {"ingress": [{"ip": "2001:db8::1"}]}},
    }
    _patch_get_resource(mocker, {"items": [service]})
    _patch_http_get(mocker, return_value=(200, "", ""))
    v = ExternalHttpProbeVerifier.model_validate(
        {"type": "external_http_probe", "selector": "app=web"}
    )
    result = v.verify(0)
    assert result.success is True
    assert result.raw["url"] == "http://[2001:db8::1]:80/"


def test_name_based_lookup(mocker: MockerFixture) -> None:
    get_resource = _patch_get_resource(mocker, _SERVICE_WITH_IP)
    _patch_http_get(mocker, return_value=(200, "", ""))
    v = ExternalHttpProbeVerifier.model_validate(
        {"type": "external_http_probe", "name": "hello-app"}
    )
    result = v.verify(0)
    assert result.success is True
    get_resource.assert_called_once_with(
        "service", "hello-app", namespace=None, kubeconfig=None, timeout=15
    )


def test_namespace_scoped_discovery(mocker: MockerFixture) -> None:
    """Namespace-only discovery must find a Service with no metadata labels (the false-negative regression)."""
    _patch_get_resource(
        mocker,
        {
            "items": [
                {
                    "metadata": {"name": "whatever-service"},
                    "spec": {"ports": [{"port": 80}]},
                    "status": {"loadBalancer": {"ingress": [{"ip": "203.0.113.9"}]}},
                }
            ]
        },
    )
    _patch_http_get(mocker, return_value=(200, "", ""))
    v = ExternalHttpProbeVerifier.model_validate(
        {"type": "external_http_probe", "kind": "service", "namespace": "hello-app"}
    )
    result = v.verify(0)
    assert result.success is True
    assert result.raw["url"] == "http://203.0.113.9:80/"


def test_get_resource_subprocess_error_is_a_check_error(mocker: MockerFixture) -> None:
    mocker.patch(
        "devops_bench.verification.verifiers.external_http_probe.get_resource",
        side_effect=SubprocessError(["kubectl", "get"], returncode=1, stderr="boom"),
    )
    v = ExternalHttpProbeVerifier.model_validate(
        {"type": "external_http_probe", "selector": "app=web"}
    )
    result = v.verify(0)
    assert result.success is False
    assert result.status == "error"
    assert "kubectl get service failed" in result.reason


def test_probe_polls_until_address_appears(mocker: MockerFixture) -> None:
    """Converge mode retries until the LoadBalancer address is assigned."""
    get_resource = mocker.patch(
        "devops_bench.verification.verifiers.external_http_probe.get_resource",
        side_effect=[
            {"items": [_SERVICE_NO_INGRESS]},
            {"items": [_SERVICE_NO_INGRESS]},
            {"items": [_SERVICE_WITH_IP]},
        ],
    )
    _patch_http_get(mocker, return_value=(200, "", ""))

    def _fast_poll(predicate: Any, *, timeout_sec: float, **_: Any) -> bool:
        return _real_poll_until(
            predicate,
            timeout_sec=timeout_sec,
            initial_delay=0.0,
            max_delay=0.0,
            sleep=lambda _s: None,
        )

    mocker.patch("devops_bench.verification.base.poll_until", side_effect=_fast_poll)
    v = ExternalHttpProbeVerifier.model_validate(
        {"type": "external_http_probe", "selector": "app=web"}
    )
    result = v.verify(30)
    assert result.success is True
    assert get_resource.call_count == 3


def test_does_not_follow_redirects(mocker: MockerFixture) -> None:
    _patch_get_resource(mocker, {"items": [_SERVICE_WITH_IP]})
    _patch_http_get(mocker, return_value=(302, "", ""))
    v = ExternalHttpProbeVerifier.model_validate(
        {"type": "external_http_probe", "selector": "app=web"}
    )
    result = v.verify(0)
    assert result.success is False
    assert "302" in result.reason


def test_no_redirect_handler_returns_none() -> None:
    assert _NoRedirect().redirect_request(None, None, 302, "Found", {}, "http://elsewhere/") is None


def test_multi_port_prefers_scheme_conventional(mocker: MockerFixture) -> None:
    service = {
        "spec": {"ports": [{"name": "https", "port": 443}, {"name": "http", "port": 80}]},
        "status": {"loadBalancer": {"ingress": [{"ip": "203.0.113.9"}]}},
    }
    _patch_get_resource(mocker, {"items": [service]})
    _patch_http_get(mocker, return_value=(200, "", ""))
    v = ExternalHttpProbeVerifier.model_validate(
        {"type": "external_http_probe", "selector": "app=web"}
    )
    result = v.verify(0)
    assert result.success is True
    assert result.raw["url"].endswith(":80/")


def test_null_loadbalancer_does_not_raise(mocker: MockerFixture) -> None:
    _patch_get_resource(
        mocker,
        {
            "items": [
                {
                    "metadata": {"name": "x"},
                    "spec": {"ports": [{"port": 80}]},
                    "status": {"loadBalancer": None},
                }
            ]
        },
    )
    v = ExternalHttpProbeVerifier.model_validate(
        {"type": "external_http_probe", "selector": "app=web"}
    )
    result = v.verify(0)
    assert result.success is False
    assert "no external address" in result.reason
