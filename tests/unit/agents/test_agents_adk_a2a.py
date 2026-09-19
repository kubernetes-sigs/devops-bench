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

"""Unit tests for devops_bench.agents.adk.a2a.

The builder's whole job is the *shape* of what it hands ``RemoteA2aAgent`` —
one httpx client passed twice, and a factory that names GRPC. Stubbing the two
SDK entry points rather than installing them keeps that contract under test on
a plain ``uv sync``, where the ``a2a`` extra is absent.
"""

from __future__ import annotations

import importlib.util
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from devops_bench import core
from devops_bench.agents.adk import a2a
from devops_bench.agents.adk import agent as adk_mod

requires_grpc = pytest.mark.skipif(
    importlib.util.find_spec("grpc") is None,
    reason="grpcio is not installed",
)


# --------------------------------------------------------------------------
# Target recognition
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("https://agents.example.com/.well-known/agent-card.json", True),
        ("http://localhost:8080", True),
        ("my_pkg.agent:root_agent", False),
        ("my_pkg.agent", False),
        ("~/agents/my_agent", False),
        ("/abs/path/agent.py", False),
        ("  https://example.com  ", True),
        ("", False),
    ],
)
def test_is_remote_target(target: str, expected: bool) -> None:
    assert a2a.is_remote_target(target) is expected


def test_is_remote_target_does_not_mistake_a_windows_drive_for_a_scheme() -> None:
    # A single-letter scheme is not http(s), so the path spelling still wins.
    assert a2a.is_remote_target(r"C:\agents\my_agent") is False


# --------------------------------------------------------------------------
# gRPC authority
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://agents.example.com", "agents.example.com:443"),
        ("http://agents.example.com", "agents.example.com:80"),
        ("https://agents.example.com:9443", "agents.example.com:9443"),
        ("http://localhost:8080/grpc", "localhost:8080"),
        # An IPv6 literal's colons live inside the brackets, so the port check
        # must not read one of those as "already ported".
        ("https://[::1]", "[::1]:443"),
        ("https://[::1]:9443", "[::1]:9443"),
    ],
)
def test_authority_supplies_the_scheme_default_port(url: str, expected: str) -> None:
    assert a2a._authority(url) == expected


@requires_grpc
def test_grpc_channel_factory_dials_insecure_for_http(monkeypatch: pytest.MonkeyPatch) -> None:
    import grpc

    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        grpc.aio, "insecure_channel", lambda target, *a, **k: seen.setdefault("insecure", target)
    )

    a2a.grpc_channel_factory("http://localhost:8080")

    assert seen == {"insecure": "localhost:8080"}


@requires_grpc
def test_grpc_channel_factory_dials_tls_for_https(monkeypatch: pytest.MonkeyPatch) -> None:
    import grpc

    seen: dict[str, Any] = {}

    def _secure(target: str, creds: Any, *a: Any, **k: Any) -> str:
        seen["target"] = target
        seen["creds"] = creds
        return target

    monkeypatch.setattr(grpc.aio, "secure_channel", _secure)

    a2a.grpc_channel_factory("https://agents.example.com")

    assert seen["target"] == "agents.example.com:443"
    assert seen["creds"] is not None


@requires_grpc
def test_grpc_channel_factory_rejects_an_unknown_scheme() -> None:
    # Silently dialling plaintext here would be the worst way to be wrong.
    with pytest.raises(core.ConfigError, match="http:// or https://"):
        a2a.grpc_channel_factory("grpc://agents.example.com:443")


# --------------------------------------------------------------------------
# build_remote_agent
# --------------------------------------------------------------------------


class _StubClientConfig:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class _StubClientFactory:
    def __init__(self, config: Any) -> None:
        self.config = config


class _StubRemoteA2aAgent:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


@pytest.fixture
def stub_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for the two SDK entry points the builder imports."""
    client_mod = ModuleType("a2a.client")
    client_mod.ClientConfig = _StubClientConfig  # type: ignore[attr-defined]
    client_mod.ClientFactory = _StubClientFactory  # type: ignore[attr-defined]

    utils_mod = ModuleType("a2a.utils")
    utils_mod.TransportProtocol = SimpleNamespace(  # type: ignore[attr-defined]
        GRPC="GRPC", JSONRPC="JSONRPC", HTTP_JSON="HTTP+JSON"
    )

    remote_mod = ModuleType("google.adk.agents.remote_a2a_agent")
    remote_mod.RemoteA2aAgent = _StubRemoteA2aAgent  # type: ignore[attr-defined]

    for name, module in (
        ("a2a", ModuleType("a2a")),
        ("a2a.client", client_mod),
        ("a2a.utils", utils_mod),
        ("google.adk.agents.remote_a2a_agent", remote_mod),
    ):
        monkeypatch.setitem(sys.modules, name, module)


def test_build_remote_agent_hands_the_same_httpx_client_to_both(stub_sdk: None) -> None:
    """The guard against ADK's factory rebind.

    ``RemoteA2aAgent`` only rebinds (and, on a2a-sdk 1.x, discards) the factory
    when it has to create an httpx client itself. Passing the factory's own
    client is what keeps that path from ever running, so these two must be the
    identical object — not merely two equivalent clients.
    """
    agent = a2a.build_remote_agent("triage", "https://agents.example.com/card.json")

    passed_client = agent.kwargs["httpx_client"]
    factory_client = agent.kwargs["a2a_client_factory"].config.kwargs["httpx_client"]

    assert passed_client is factory_client


def test_build_remote_agent_names_grpc_first_among_the_bindings(stub_sdk: None) -> None:
    agent = a2a.build_remote_agent("triage", "https://agents.example.com/card.json")
    config = agent.kwargs["a2a_client_factory"].config.kwargs

    assert config["supported_protocol_bindings"] == ["GRPC", "JSONRPC", "HTTP+JSON"]
    assert config["grpc_channel_factory"] is a2a.grpc_channel_factory


def test_build_remote_agent_accepts_a_channel_factory_override(stub_sdk: None) -> None:
    sentinel = object()
    agent = a2a.build_remote_agent(
        "triage",
        "https://agents.example.com/card.json",
        channel_factory=lambda url: sentinel,
    )

    assert agent.kwargs["a2a_client_factory"].config.kwargs["grpc_channel_factory"](".") is sentinel


def test_build_remote_agent_threads_the_timeout(stub_sdk: None) -> None:
    agent = a2a.build_remote_agent("triage", "https://x/card.json", timeout_sec=42.0)

    assert agent.kwargs["timeout"] == 42.0
    assert agent.kwargs["httpx_client"].timeout.connect == 42.0


def test_build_remote_agent_omits_the_timeout_when_unset(stub_sdk: None) -> None:
    # ADK has no "disabled" sentinel, so ``None`` must fall through to its
    # default rather than being forwarded as a disabled timeout.
    agent = a2a.build_remote_agent("triage", "https://x/card.json", timeout_sec=None)

    assert "timeout" not in agent.kwargs


def test_build_remote_agent_reports_a_missing_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "a2a.client", None)

    with pytest.raises(core.MissingDependencyError) as excinfo:
        a2a.build_remote_agent("triage", "https://x/card.json")

    assert excinfo.value.extra == "a2a"


# --------------------------------------------------------------------------
# Harness wiring
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://triage.example.com/card.json", "triage_example_com"),
        ("http://localhost:8080", "localhost"),
        ("https://10.0.0.7:443", "a_10_0_0_7"),
        ("https://_/card.json", "remote"),
    ],
)
def test_remote_agent_name_is_a_usable_adk_identifier(url: str, expected: str) -> None:
    name = adk_mod._remote_agent_name(url)

    assert name == expected
    assert name.isidentifier()


def test_resolve_root_agent_builds_a_remote_agent_for_a_url(
    stub_sdk: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A URL target must be recognized *before* the ``module:attr`` split.

    ``"https://host".partition(":")`` yields a module spec of ``"https"``, so
    reaching the local path at all would mean importing the wrong thing.
    """
    monkeypatch.setitem(sys.modules, "google.adk.agents", SimpleNamespace(BaseAgent=object))

    resolved = adk_mod._resolve_root_agent("https://triage.example.com/card.json", timeout_sec=30.0)

    assert isinstance(resolved, _StubRemoteA2aAgent)
    assert resolved.kwargs["name"] == "triage_example_com"
    assert resolved.kwargs["agent_card"] == "https://triage.example.com/card.json"
    assert resolved.kwargs["timeout"] == 30.0
