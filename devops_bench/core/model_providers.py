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

"""The single source of truth for agent model-provider config.

Every harness resolves ``AGENT_PROVIDER`` / ``AGENT_MODEL`` / ``AGENT_API_KEY``
through :func:`resolve_provider`, which maps a raw alias to a
:class:`ProviderSpec`: adapter family plus backend, key routing and whether the
backend works keyless. Lives in ``core`` so the models layer and the CLI
harnesses both import it without a cycle or a provider SDK; named
``model_providers`` to keep it apart from :mod:`devops_bench.providers` (cloud
infra).

Also owns the credential recipes keyless backends need under the sandbox
(:func:`sandbox_credential_env`). That cloud-specific code lives here, never in
:mod:`devops_bench.agents.sandbox`.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

from devops_bench.core.config import get_env
from devops_bench.core.errors import ConfigError, SubprocessError
from devops_bench.core.logging import get_logger
from devops_bench.core.subprocess import run

__all__ = [
    "ProviderSpec",
    "resolve_provider",
    "known_providers",
    "sandbox_credential_env",
    "VERTEX_SANDBOX_SA_ENV",
]

_log = get_logger("core.model_providers")


class ProviderSpec(BaseModel):
    """Resolved config contract for one agent model provider.

    Attributes:
        canonical: Normalized provider id (the ``_SPECS`` key).
        adapter_family: Models-layer adapter key for ``get_model`` /
            ``MODELS.get`` (e.g. ``gemini`` / ``claude`` / ``ollama``).
        oc_provider: openclaw wire-provider id used in ``provider/model`` and the
            per-run ``_PROVIDER_TRANSPORT`` lookup.
        api_key_envs: Env var name(s) a CLI harness sets from ``config.api_key``;
            empty where no key is ever threaded.
        keyless_ok: Whether the backend can authenticate without a key (Vertex
            ADC, Bedrock AWS creds, local ollama).
        backend: Adapter backend hint (``"vertex"`` / ``"bedrock"``), or ``None``
            to let the adapter infer the backend from the environment.
    """

    model_config = ConfigDict(frozen=True)

    canonical: str
    adapter_family: str
    oc_provider: str
    api_key_envs: tuple[str, ...]
    keyless_ok: bool
    backend: str | None = None


# Canonical id -> spec. ``google`` and ``google-vertex`` share the ``gemini``
# adapter but differ in backend and key routing; specs with empty
# ``api_key_envs`` never have a key forced onto them.
_SPECS: dict[str, ProviderSpec] = {
    "google": ProviderSpec(
        canonical="google",
        adapter_family="gemini",
        oc_provider="google",
        api_key_envs=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        keyless_ok=False,
        backend=None,
    ),
    "google-vertex": ProviderSpec(
        canonical="google-vertex",
        adapter_family="gemini",
        oc_provider="google-vertex",
        api_key_envs=("GOOGLE_CLOUD_API_KEY",),
        keyless_ok=True,
        backend="vertex",
    ),
    "anthropic": ProviderSpec(
        canonical="anthropic",
        adapter_family="claude",
        oc_provider="anthropic",
        api_key_envs=("ANTHROPIC_API_KEY",),
        keyless_ok=False,
        backend=None,  # claude infers api/vertex/bedrock from the environment
    ),
    "anthropic-vertex": ProviderSpec(
        canonical="anthropic-vertex",
        adapter_family="claude",
        oc_provider="anthropic-vertex",
        api_key_envs=(),
        keyless_ok=True,
        backend="vertex",
    ),
    "anthropic-bedrock": ProviderSpec(
        canonical="anthropic-bedrock",
        adapter_family="claude",
        oc_provider="anthropic-bedrock",
        api_key_envs=(),
        keyless_ok=True,
        backend="bedrock",
    ),
    "openai": ProviderSpec(
        canonical="openai",
        adapter_family="openai",
        oc_provider="openai",
        api_key_envs=("OPENAI_API_KEY",),
        keyless_ok=False,
        backend=None,
    ),
    "ollama": ProviderSpec(
        canonical="ollama",
        adapter_family="ollama",
        oc_provider="ollama",
        api_key_envs=(),  # optional key handled by the adapter via AGENT_API_KEY
        keyless_ok=True,
        backend=None,
    ),
}

# Raw alias (lowercased) -> canonical id.
_ALIASES: dict[str, str] = {
    "gemini": "google",
    "google": "google",
    "google-vertex": "google-vertex",
    "google_vertex": "google-vertex",
    "claude": "anthropic",
    "anthropic": "anthropic",
    "anthropic-vertex": "anthropic-vertex",
    "anthropic_vertex": "anthropic-vertex",
    "anthropic-bedrock": "anthropic-bedrock",
    "anthropic_bedrock": "anthropic-bedrock",
    "openai": "openai",
    "ollama": "ollama",
}


def known_providers() -> tuple[str, ...]:
    """Return the sorted raw provider aliases the contract accepts."""
    return tuple(sorted(_ALIASES))


def resolve_provider(provider: str | None, *, default: str = "google") -> ProviderSpec:
    """Resolve a raw provider alias to its :class:`ProviderSpec`.

    Matching is case-insensitive; a blank or unset value resolves to ``default``.

    Args:
        provider: Raw ``AGENT_PROVIDER`` value (or a per-call override). ``None``
            or blank resolves to ``default``.
        default: Alias used when ``provider`` is blank/unset.

    Returns:
        The :class:`ProviderSpec` for the resolved provider.

    Raises:
        ConfigError: If ``provider`` (or ``default``) is not a known alias.

    Example:
        >>> resolve_provider("gemini").canonical
        'google'
        >>> resolve_provider("google-vertex").backend
        'vertex'
    """
    raw = (provider or "").strip().lower() or default.strip().lower()
    canonical = _ALIASES.get(raw)
    if canonical is None:
        raise ConfigError(
            f"unknown agent provider {raw!r}; known providers: {', '.join(known_providers())}"
        )
    return _SPECS[canonical]


# --------------------------------------------------------------------------
# Keyless-backend credential recipes for sandboxed runs
# --------------------------------------------------------------------------
#
# A sandboxed agent has no ambient cloud identity: gcloud config is not
# mounted, the credential env vars never cross, and the bastion blocks the
# metadata endpoint to containers, so every ADC lookup inside fails.
#
# For Vertex the recipe is a metadata-server emulator: a host-side HTTP server
# speaking the slice of the GCE metadata protocol the auth libraries use, backed
# by an impersonated token for a service account holding only
# ``roles/aiplatform.user``. Simpler routes do not work: the SDK takes no bare
# token, no ADC file format carries one, ``GOOGLE_API_KEY`` on Vertex is
# express mode only, and SA key files are refused by org policy.

# Service account the emulator impersonates. BENCH_-prefixed on purpose: the
# deny filter keeps it host-side; only the minted token crosses.
VERTEX_SANDBOX_SA_ENV = "BENCH_VERTEX_SANDBOX_SA"

# ``wrap_argv`` --add-host's this to the host gateway on every run.
_EMULATOR_CONTAINER_HOST = "host.docker.internal"

# All interfaces, not loopback: on Linux the container reaches the host at the
# bridge gateway, which 127.0.0.1 does not answer. Exposure is bounded by the
# host firewall, the ephemeral port, the Metadata-Flavor check and the token's
# narrow scope.
_EMULATOR_BIND_HOST = "0.0.0.0"

# Vertex requires cloud-platform; the SA's IAM role does the narrowing.
_TOKEN_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

# gcloud mints impersonated tokens fresh (never cached) with exactly this
# lifetime, so the countdown served from our clock is the real expiry.
_TOKEN_LIFETIME_SEC = 3600
_TOKEN_REFRESH_MARGIN_SEC = 300

# A mint holds the emulator's lock, so a wedged gcloud must not block every
# request behind it.
_MINT_TIMEOUT_SEC = 60

# Required on every request and echoed on every response, as the real server does.
_METADATA_FLAVOR_HEADER = "Metadata-Flavor"
_METADATA_FLAVOR = "Google"

_METADATA_PREFIX = "/computeMetadata/v1/"

# One per (service account, project) per process; a daemon thread, no teardown.
_EMULATORS: dict[tuple[str, str], _VertexMetadataEmulator] = {}
_EMULATORS_LOCK = threading.Lock()


def sandbox_credential_env(
    spec: ProviderSpec, *, project: str | None = None, service_account: str | None = None
) -> dict[str, str]:
    """Build the env a *sandboxed* agent needs to authenticate to ``spec``'s backend.

    Keyed off :attr:`ProviderSpec.backend`. Key-based providers get ``{}``;
    their key is already in the overlay. Call this only for a sandboxed run; an
    unsandboxed process already has ambient ADC.

    Args:
        spec: The run's resolved provider spec.
        project: Cloud project the run bills to. Required for Vertex.
        service_account: Account to impersonate, defaulting to
            ``$BENCH_VERTEX_SANDBOX_SA``. It should hold ``roles/aiplatform.user``
            only; the host identity needs ``roles/iam.serviceAccountTokenCreator``
            on it.

    Returns:
        Env vars to merge into the boundary overlay; empty when no recipe is needed.

    Raises:
        ConfigError: A keyless backend with no recipe, a Vertex recipe missing
            its project or service account, or a failed mint. Never a partial
            answer.
    """
    if spec.backend == "vertex":
        return _vertex_metadata_env(project=project, service_account=service_account)
    if spec.keyless_ok and spec.backend is not None:
        raise ConfigError(
            f"provider {spec.canonical!r} authenticates through ambient cloud identity "
            f"({spec.backend}), which a sandboxed run does not have, and no "
            "mint-and-inject credential recipe exists for that backend yet; run this "
            "provider unsandboxed or use a key-based provider"
        )
    return {}


def _vertex_metadata_env(*, project: str | None, service_account: str | None) -> dict[str, str]:
    """Start (or reuse) the Vertex metadata emulator and return the container's env."""
    account = (service_account or get_env(VERTEX_SANDBOX_SA_ENV, "") or "").strip()
    if not account:
        raise ConfigError(
            "a sandboxed Vertex run needs a service account to impersonate for its model "
            f"credential; set {VERTEX_SANDBOX_SA_ENV} to an aiplatform.user-only service "
            "account the host identity can mint tokens for "
            "(roles/iam.serviceAccountTokenCreator on that account)"
        )
    if not (project or "").strip():
        raise ConfigError(
            "a sandboxed Vertex run needs GOOGLE_CLOUD_PROJECT (or GCP_PROJECT) set; the "
            "metadata emulator serves it to the agent's SDK as the run's project"
        )
    emulator = _get_emulator(account, project.strip())
    # Mint now so a missing grant fails here, before the agent starts.
    emulator.token()
    return {
        # Python auth reads GCE_METADATA_HOST, the Node library GCE_METADATA_IP;
        # both host:port, no scheme.
        "GCE_METADATA_HOST": emulator.address,
        "GCE_METADATA_IP": emulator.address,
        # Skip the GCE residency probe, which fails inside a container.
        "METADATA_SERVER_DETECTION": "assume-present",
    }


def _get_emulator(service_account: str, project: str) -> _VertexMetadataEmulator:
    """Return the process-wide emulator for this identity, starting it if needed."""
    key = (service_account, project)
    with _EMULATORS_LOCK:
        emulator = _EMULATORS.get(key)
        if emulator is None:
            emulator = _VertexMetadataEmulator(service_account, project)
            emulator.start()
            _EMULATORS[key] = emulator
    return emulator


class _VertexMetadataEmulator:
    """A GCE metadata server serving one impersonated, ``aiplatform``-scoped token.

    Serves only the paths the auth libraries read; everything else is a 404, so
    it is never a general metadata proxy. The token is minted up front and
    refilled in place.
    """

    def __init__(self, service_account: str, project: str) -> None:
        self.service_account = service_account
        self.project = project
        self._lock = threading.Lock()
        self._token = ""
        self._expires_at = 0.0
        self._server: ThreadingHTTPServer | None = None

    @property
    def port(self) -> int:
        """Ephemeral port the server bound to."""
        if self._server is None:  # pragma: no cover - start() precedes every use
            raise ConfigError("the Vertex metadata emulator was not started")
        return int(self._server.server_address[1])

    @property
    def address(self) -> str:
        """``host:port`` the *container* uses to reach this server."""
        return f"{_EMULATOR_CONTAINER_HOST}:{self.port}"

    def start(self) -> None:
        """Bind an ephemeral port and serve on a daemon thread."""
        self._server = ThreadingHTTPServer((_EMULATOR_BIND_HOST, 0), _handler_factory(self))
        threading.Thread(
            target=self._server.serve_forever,
            name="vertex-metadata-emulator",
            daemon=True,
        ).start()
        _log.info(
            "serving a Vertex metadata emulator on port %s for the sandboxed agent; "
            "impersonating %s in project %s",
            self.port,
            self.service_account,
            self.project,
        )

    def stop(self) -> None:
        """Shut the server down; only the tests call this (the thread is a daemon)."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def token(self) -> tuple[str, int]:
        """Return ``(access_token, seconds_until_expiry)``, refilling when stale."""
        with self._lock:
            remaining = self._expires_at - time.monotonic()
            if not self._token or remaining <= _TOKEN_REFRESH_MARGIN_SEC:
                self._token = self._mint()
                self._expires_at = time.monotonic() + _TOKEN_LIFETIME_SEC
                remaining = _TOKEN_LIFETIME_SEC
            return self._token, int(remaining)

    def _mint(self) -> str:
        """Mint a token by impersonating the scoped SA via ``gcloud`` (no SDK import)."""
        try:
            completed = run(
                [
                    "gcloud",
                    "auth",
                    "print-access-token",
                    f"--impersonate-service-account={self.service_account}",
                    f"--scopes={_TOKEN_SCOPE}",
                    f"--lifetime={_TOKEN_LIFETIME_SEC}s",
                ],
                check=False,
                timeout=_MINT_TIMEOUT_SEC,
            )
        except SubprocessError as exc:
            raise ConfigError(
                f"could not mint a Vertex access token by impersonating "
                f"{self.service_account}: {exc} (the mint is bounded at {_MINT_TIMEOUT_SEC}s)"
            ) from exc
        token = (completed.stdout or "").strip()
        if completed.returncode != 0 or not token:
            raise ConfigError(
                f"could not mint a Vertex access token by impersonating "
                f"{self.service_account}: gcloud exited {completed.returncode}: "
                f"{(completed.stderr or '').strip() or '<no stderr>'}. The host identity "
                "needs roles/iam.serviceAccountTokenCreator on that service account"
            )
        return token


def _handler_factory(emulator: _VertexMetadataEmulator) -> type[BaseHTTPRequestHandler]:
    """Build the request handler class bound to ``emulator``."""

    class _MetadataHandler(BaseHTTPRequestHandler):
        # HTTP/1.1 keeps the auth libraries' keep-alive connections open.
        protocol_version = "HTTP/1.1"
        server_version = "devops-bench-metadata-emulator"

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
            if self.headers.get(_METADATA_FLAVOR_HEADER) != _METADATA_FLAVOR:
                # As the real server does; also keeps a stray probe from lifting a token.
                self._respond(403, "text/plain", "Missing Metadata-Flavor:Google header.")
                return
            path = urlsplit(self.path).path
            try:
                body = _route(emulator, path)
            except ConfigError as exc:
                # A refill can fail mid-run; return the remedy, not a dropped socket.
                _log.error("metadata emulator: %s", exc)
                self._respond(500, "text/plain", str(exc))
                return
            if body is None:
                self._respond(404, "text/plain", "Not Found")
                return
            content_type, text = body
            self._respond(200, content_type, text)

        def _respond(self, status: int, content_type: str, text: str) -> None:
            payload = text.encode("utf-8")
            self.send_response(status)
            self.send_header(_METADATA_FLAVOR_HEADER, _METADATA_FLAVOR)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            # Keep per-request logging off the harness's stderr.
            _log.debug("metadata emulator: " + format, *args)

    return _MetadataHandler


def _route(emulator: _VertexMetadataEmulator, path: str) -> tuple[str, str] | None:
    """Map a metadata path to ``(content_type, body)``, or ``None`` for a 404.

    ``default`` and the account's email are both accepted as the account segment.
    """
    if path in ("/", _METADATA_PREFIX):
        # Residency ping; the response header is the whole answer.
        return "text/plain", "computeMetadata/\n"
    if not path.startswith(_METADATA_PREFIX):
        return None
    rest = path[len(_METADATA_PREFIX) :]

    if rest == "project/project-id":
        return "text/plain", emulator.project
    if rest == "universe/universe-domain":
        return "text/plain", "googleapis.com"
    if rest == "instance/service-accounts/":
        return "text/plain", f"default/\n{emulator.service_account}/\n"

    prefix = "instance/service-accounts/"
    if not rest.startswith(prefix):
        return None
    account, _, leaf = rest[len(prefix) :].partition("/")
    if account not in ("default", emulator.service_account):
        return None

    if leaf == "email":
        return "text/plain", emulator.service_account
    if leaf == "scopes":
        return "text/plain", f"{_TOKEN_SCOPE}\n"
    if leaf == "aliases":
        return "text/plain", "default\n"
    if leaf == "token":
        token, expires_in = emulator.token()
        return "application/json", json.dumps(
            {"access_token": token, "expires_in": expires_in, "token_type": "Bearer"}
        )
    if leaf == "":
        # The recursive listing the libraries fetch to enumerate accounts.
        return "application/json", json.dumps(
            {
                "aliases": ["default"],
                "email": emulator.service_account,
                "scopes": [_TOKEN_SCOPE],
            }
        )
    return None
