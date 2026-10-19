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

"""Build a ``RemoteA2aAgent`` that can actually reach a gRPC endpoint.

ADK can talk to a remote agent over A2A, but the default client it builds
supports only the HTTP transports. Reaching a gRPC endpoint takes a
``ClientFactory`` whose config names the GRPC binding *and* supplies a
``grpc_channel_factory``. :func:`build_remote_agent` assembles that from bench
config so callers don't hand-roll a wrapper module — and, more to the point, so
they don't hand-roll one that silently loses its gRPC binding.

.. warning::

   The one non-obvious rule: a custom factory must be handed over *together
   with* the httpx client it was built on. ``RemoteA2aAgent`` lazily creates an
   httpx client on first use, and when it does so while already holding a
   factory it calls ``_compat.rebind_client_factory_httpx``. On a2a-sdk 1.x
   that helper discards the caller's factory and returns a fresh one carrying
   only ``[JSONRPC, HTTP+JSON]`` — its docstring calls dropping custom
   transports "intended behavior". The GRPC binding and the channel factory go
   with it, and the failure surfaces later as a transport-negotiation error
   pointing nowhere near the cause.

   Passing ``httpx_client=`` means the lazy path never runs, so the factory
   survives. :func:`build_remote_agent` always passes both.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from devops_bench import core

if TYPE_CHECKING:  # pragma: no cover - import-time typing only
    from google.adk.agents.remote_a2a_agent import RemoteA2aAgent

__all__ = ["build_remote_agent", "grpc_channel_factory", "is_remote_target"]

_log = core.get_logger("agents.adk.a2a")

#: Optional dependency group carrying a2a-sdk *with* its gRPC transport.
_EXTRA = "a2a"

#: URL schemes that name a remote agent card rather than a local module or path.
_REMOTE_SCHEMES: frozenset[str] = frozenset({"http", "https"})

#: Default ports, by scheme, for a gRPC target that names no port. grpc's own
#: resolver does not infer one, so an unported authority would fail to dial.
_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}


def is_remote_target(target: str) -> bool:
    """Report whether ``AGENT_TARGET`` names a remote agent card over HTTP(S).

    Every other accepted spelling is a module path or a filesystem path, and
    neither can carry a scheme — so the scheme alone decides.
    """
    return urlparse(target.strip()).scheme in _REMOTE_SCHEMES


def _authority(url: str) -> str:
    """Render ``url`` as a ``host:port`` gRPC target.

    A URL with no explicit port takes the scheme's default, because grpc dials
    an authority rather than a URL and will not infer one.
    """
    parsed = urlparse(url)
    # A bare ``host:port`` parses with an empty netloc, so fall back to the path.
    authority = parsed.netloc or parsed.path
    if ":" not in authority.rsplit("]", 1)[-1]:
        port = _DEFAULT_PORTS.get(parsed.scheme)
        if port is not None:
            authority = f"{authority}:{port}"
    return authority


def grpc_channel_factory(url: str) -> Any:
    """Open an aio gRPC channel to ``url``, with TLS decided by its scheme.

    ``https`` dials a secure channel against the system trust store; ``http``
    dials an insecure one, which is what an in-cluster or port-forwarded
    endpoint needs. Anything else is rejected rather than guessed at — silently
    falling back to plaintext would be the wrong way to be wrong.

    Args:
        url: The gRPC endpoint the AgentCard advertises.

    Returns:
        A ``grpc.aio.Channel`` connected to ``url``.

    Raises:
        MissingDependencyError: When grpcio is not installed.
        ConfigError: When the URL carries a scheme that is not http(s).
    """
    try:
        import grpc
    except ImportError as exc:  # pragma: no cover - exercised via the extra
        raise core.MissingDependencyError("reaching an A2A agent over gRPC", _EXTRA) from exc

    scheme = urlparse(url).scheme
    if scheme not in _REMOTE_SCHEMES:
        raise core.ConfigError(
            f"cannot open a gRPC channel to {url!r}: expected an http:// or https:// URL"
        )

    authority = _authority(url)
    if scheme == "http":
        return grpc.aio.insecure_channel(authority)
    return grpc.aio.secure_channel(authority, grpc.ssl_channel_credentials())


def _transport_protocols() -> tuple[Any, Any, Any]:
    """Return the ``(GRPC, JSONRPC, HTTP_JSON)`` binding constants.

    ``TransportProtocol`` moved from ``a2a.types`` to ``a2a.utils`` in a2a-sdk
    1.0, and the ``a2a`` extra admits both majors, so both spellings are tried.
    """
    try:
        from a2a.utils import TransportProtocol
    except ImportError:
        from a2a.types import TransportProtocol  # type: ignore[no-redef]

    return TransportProtocol.GRPC, TransportProtocol.JSONRPC, TransportProtocol.HTTP_JSON


def build_remote_agent(
    name: str,
    agent_card: Any,
    *,
    timeout_sec: float | None = None,
    channel_factory: Callable[[str], Any] | None = None,
) -> RemoteA2aAgent:
    """Build a ``RemoteA2aAgent`` wired for gRPC as well as the HTTP transports.

    The returned agent drops into an ADK tree like any other ``BaseAgent``, so
    a caller can hand it straight to the harness or nest it under a parent.

    Transport choice is left to the server: the bindings are listed
    gRPC-first but ``use_client_preference`` stays off, so a card advertising
    only JSON-RPC still connects. Naming GRPC costs nothing when it is unused
    and is the whole point when it is not.

    Args:
        name: Agent name, used as ADK's node identifier for the remote agent.
        agent_card: An ``AgentCard``, a URL to one, or a path to one — whatever
            ``RemoteA2aAgent`` itself accepts.
        timeout_sec: HTTP timeout in seconds. ``None`` keeps ADK's default
            rather than disabling the timeout, since ADK has no "off" sentinel.
        channel_factory: Override for the gRPC channel opener. Defaults to
            :func:`grpc_channel_factory`; tests inject a stub.

    Returns:
        A configured ``RemoteA2aAgent``.

    Raises:
        MissingDependencyError: When the ``a2a`` extra is not installed.
    """
    try:
        from a2a.client import ClientConfig, ClientFactory
        from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
    except ImportError as exc:
        raise core.MissingDependencyError("remote A2A agents", _EXTRA) from exc

    import httpx

    grpc_binding, jsonrpc, http_json = _transport_protocols()

    # One client, shared deliberately: the factory is built on it and the agent
    # is handed the same instance, which is what stops the rebind described in
    # this module's docstring from firing.
    httpx_client = httpx.AsyncClient(
        timeout=httpx.Timeout(timeout=timeout_sec) if timeout_sec is not None else None
    )
    factory = ClientFactory(
        ClientConfig(
            httpx_client=httpx_client,
            grpc_channel_factory=channel_factory or grpc_channel_factory,
            supported_protocol_bindings=[grpc_binding, jsonrpc, http_json],
        )
    )

    kwargs: dict[str, Any] = {}
    if timeout_sec is not None:
        kwargs["timeout"] = timeout_sec

    _log.debug("building remote A2A agent %r against %r", name, agent_card)
    return RemoteA2aAgent(
        name=name,
        agent_card=agent_card,
        httpx_client=httpx_client,
        a2a_client_factory=factory,
        **kwargs,
    )
