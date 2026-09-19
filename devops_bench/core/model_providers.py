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

Every harness consumes the same ``AGENT_PROVIDER`` / ``AGENT_MODEL`` /
``AGENT_API_KEY`` contract through :func:`resolve_provider`, so a config that
works for one harness behaves identically for the others. A raw provider alias
resolves to a :class:`ProviderSpec` carrying both axes the harnesses need: the
adapter family (which models-layer client to build) and the backend / transport
/ key-routing details (which oc wire-provider, which API-key env var(s), and
whether the backend authenticates without a key).

Lives in ``core`` (not ``models``/``agents``) so both the models layer and the
CLI harnesses import it without an import cycle and without pulling any provider
SDK. Named ``model_providers`` to avoid confusion with
:mod:`devops_bench.providers` (the cloud/infra OpenTofu providers).

This module also owns the **mint-and-inject credential recipes** for the keyless
backends — see :func:`sandbox_credential_env`. A key-based provider needs
nothing from them (its key is already in the env overlay and crosses the sandbox
boundary by value); a keyless backend authenticates through ambient cloud
identity, which the sandbox deliberately strips, so it needs a host-side recipe
that mints a narrow short-lived credential and injects it explicitly. Per the
proposal's review rule, that cloud-specific code lives here — in the
model-credential layer — and never in :mod:`devops_bench.agents.sandbox`.
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
        api_key_envs: Env var name(s) a CLI harness sets from ``config.api_key``.
            Empty for ``anthropic-vertex`` / ``anthropic-bedrock`` / ``ollama``
            (no key is ever threaded). ``google-vertex`` is keyless-ok but still
            routes a *provided* key to ``GOOGLE_CLOUD_API_KEY``.
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


# Canonical provider id -> spec. Two axes are encoded here: ``adapter_family``
# (``google`` and ``google-vertex`` both build the ``gemini`` adapter) and the
# backend/transport/key-routing (which differ between them). Vertex and Bedrock
# are keyless-ok (ADC / AWS creds) and need no key; ``anthropic-vertex`` /
# ``anthropic-bedrock`` / ``ollama`` carry empty ``api_key_envs`` so a key is
# never forced onto them, while ``google-vertex`` still routes a provided key to
# ``GOOGLE_CLOUD_API_KEY`` (the vertex transport var, not the google-genai one).
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
        adapter_family="openai",  # no adapter module today: get_model raises NotRegisteredError
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

# Raw alias (lowercased) -> canonical id. Company/runtime names and underscore
# spellings map onto the canonical wire ids above.
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
# A sandboxed agent has no ambient cloud identity by construction: the
# operator's gcloud config is not mounted, GOOGLE_APPLICATION_CREDENTIALS and
# CLOUDSDK_CONFIG never cross the boundary, and on a cloud VM the link-local
# metadata endpoint is blocked to containers by the DOCKER-USER rule
# ``vm-setup.sh`` installs. That is the whole point — it is also why every
# Application Default Credentials lookup inside the container fails.
#
# For Vertex the recipe is a **metadata-server emulator**: a small host-side
# HTTP server speaking the subset of the GCE metadata protocol that Google's
# auth libraries use to obtain a token, backed by an impersonated token for a
# service account that holds ``roles/aiplatform.user`` and nothing else. The
# container is pointed at it with GCE_METADATA_HOST / GCE_METADATA_IP /
# METADATA_SERVER_DETECTION.
#
# Four simpler things were tried first and none of them work:
#
# * There is no supported way to hand the SDK a bare access token.
# * No ADC file format carries one — ``authorized_user`` needs a refresh token
#   and ``external_account`` needs a workload-identity pool.
# * ``GOOGLE_API_KEY`` + ``GOOGLE_GENAI_USE_VERTEXAI`` is Vertex *express mode*
#   only; a standard project answers ``401: API keys are not supported by this
#   API``.
# * Service-account key files are refused by
#   ``constraints/iam.disableServiceAccountKeyCreation``.

# Host-side env var naming the service account the emulator impersonates. It
# carries the ``BENCH_`` prefix deliberately: the sandbox deny filter drops
# that prefix, and this is operator configuration that has no business inside
# the container — only the minted token crosses.
VERTEX_SANDBOX_SA_ENV = "BENCH_VERTEX_SANDBOX_SA"

# Hostname the container uses to reach the emulator. ``SandboxExecutor.wrap_argv``
# passes ``--add-host host.docker.internal:host-gateway`` on every run, so this
# resolves to the host from any Docker network the run might join.
_EMULATOR_CONTAINER_HOST = "host.docker.internal"

# The emulator binds on all interfaces rather than loopback. It has to: on
# Linux the container reaches the host at the bridge gateway address, and a
# server bound to 127.0.0.1 does not answer there (Docker Desktop's proxy
# hides this on macOS). Exposure is bounded by the host firewall, by an
# ephemeral port, by the Metadata-Flavor header check below, and above all by
# what the token is worth: one project's ``aiplatform.user``, for an hour.
_EMULATOR_BIND_HOST = "0.0.0.0"

# Scope requested for the impersonated token. Vertex requires cloud-platform;
# the narrowing is done by the service account's IAM role, not by the scope.
_TOKEN_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

# Impersonated tokens last an hour. Refill once less than this is left, so a
# request never gets a token that expires mid-generation.
_TOKEN_LIFETIME_SEC = 3600
_TOKEN_REFRESH_MARGIN_SEC = 300

# Ceiling on one mint. The call is a single IAM round-trip, so a minute means
# something is wedged (a prompting credential helper, a dead network) — and a
# mint runs under the emulator's lock, so left unbounded it would block every
# request thread behind it for as long as gcloud cares to hang.
_MINT_TIMEOUT_SEC = 60

# Every metadata response carries this header, and (per the real server's
# contract) every request must carry it too.
_METADATA_FLAVOR_HEADER = "Metadata-Flavor"
_METADATA_FLAVOR = "Google"

_METADATA_PREFIX = "/computeMetadata/v1/"

# One emulator per (service account, project) per process, reused across the
# runs of a matrix. The server thread is a daemon, so it needs no teardown
# hook: it dies with the harness process.
_EMULATORS: dict[tuple[str, str], _VertexMetadataEmulator] = {}
_EMULATORS_LOCK = threading.Lock()


def sandbox_credential_env(
    spec: ProviderSpec, *, project: str | None = None, service_account: str | None = None
) -> dict[str, str]:
    """Build the env a *sandboxed* agent needs to authenticate to ``spec``'s backend.

    Keyed off :attr:`ProviderSpec.backend`, so a new keyless backend is one
    branch here rather than a change at every harness call site. Key-based
    providers get an empty mapping — their key is already in the overlay and
    crosses the boundary on its own.

    Callers invoke this only for a sandboxed run; on an unsandboxed run the
    ambient ADC the host process already has is the right credential and this
    must not be called, so that the sandbox flag stays byte-for-byte
    behaviour-preserving when off.

    Args:
        spec: The run's resolved provider spec.
        project: Google Cloud project the run bills to, as already resolved by
            the caller. Required for the Vertex recipe.
        service_account: Service account to impersonate, defaulting to
            ``$BENCH_VERTEX_SANDBOX_SA``. It should hold ``roles/aiplatform.user``
            and nothing else, and the host's own identity needs
            ``roles/iam.serviceAccountTokenCreator`` on it.

    Returns:
        Env vars to merge into the overlay that crosses the boundary. Empty for
        a backend that needs no recipe.

    Raises:
        ConfigError: If the backend is keyless but has no recipe yet, or the
            Vertex recipe is missing its project / service account, or the
            token mint fails. Never returns a partial answer: a sandboxed run
            that cannot authenticate must fail loud rather than degrade.
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
    return {
        # Both spellings: the Python auth library reads GCE_METADATA_HOST for
        # the base URL and GCE_METADATA_IP for its reachability probe; the
        # Node library the Gemini CLI embeds prefers GCE_METADATA_IP. Neither
        # takes a scheme, both take host:port.
        "GCE_METADATA_HOST": emulator.address,
        "GCE_METADATA_IP": emulator.address,
        # Skip the residency probe outright. Without this the library may
        # decide it is not on GCE at all (no BIOS marker in a container) and
        # never ask the emulator for a token.
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

    Only the handful of paths Google's auth libraries actually read are served;
    everything else is a 404, so this cannot be mistaken for (or used as) a
    general metadata proxy. The token is minted lazily on the first request and
    refilled in place, which means a run that never calls the model never mints
    a credential at all.
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
        """Shut the server down. Not needed for process exit (the thread is a daemon)."""
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
        """Mint a fresh access token by impersonating the scoped service account.

        Shelling out to ``gcloud`` rather than importing ``google.auth`` keeps
        this module's "pulls no provider SDK" property intact, and matches how
        the rest of the benchmark reaches GCP
        (:mod:`devops_bench.providers.gcp`, the antigravity harness).
        """
        try:
            completed = run(
                [
                    "gcloud",
                    "auth",
                    "print-access-token",
                    f"--impersonate-service-account={self.service_account}",
                    f"--scopes={_TOKEN_SCOPE}",
                ],
                check=False,
                timeout=_MINT_TIMEOUT_SEC,
            )
        except SubprocessError as exc:
            raise ConfigError(
                f"could not mint a Vertex access token by impersonating "
                f"{self.service_account}: {exc}. The mint is bounded at "
                f"{_MINT_TIMEOUT_SEC}s so a wedged gcloud (a prompting credential "
                "helper, a dead network) cannot hold the emulator's lock and block "
                "every request behind it"
            ) from exc
        token = (completed.stdout or "").strip()
        if completed.returncode != 0 or not token:
            raise ConfigError(
                f"could not mint a Vertex access token by impersonating "
                f"{self.service_account}: gcloud exited {completed.returncode}: "
                f"{(completed.stderr or '').strip() or '<no stderr>'}. The host identity "
                "needs roles/iam.serviceAccountTokenCreator on that service account "
                "(if this worked before, verify the binding still exists — some "
                "organizations expire or strip IAM grants)"
            )
        return token


def _handler_factory(emulator: _VertexMetadataEmulator) -> type[BaseHTTPRequestHandler]:
    """Build the request handler class bound to ``emulator``."""

    class _MetadataHandler(BaseHTTPRequestHandler):
        # HTTP/1.1 so the auth libraries' keep-alive connections are honoured
        # rather than being closed under them after every request.
        protocol_version = "HTTP/1.1"
        server_version = "devops-bench-metadata-emulator"

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
            if self.headers.get(_METADATA_FLAVOR_HEADER) != _METADATA_FLAVOR:
                # The real server enforces this too; here it also means a
                # stray probe on the host's network cannot lift a token.
                self._respond(403, "text/plain", "Missing Metadata-Flavor:Google header.")
                return
            path = urlsplit(self.path).path
            body = _route(emulator, path)
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
            # BaseHTTPRequestHandler writes to stderr by default, which would
            # interleave with the harness's own output on every token fetch.
            _log.debug("metadata emulator: " + format, *args)

    return _MetadataHandler


def _route(emulator: _VertexMetadataEmulator, path: str) -> tuple[str, str] | None:
    """Map a metadata path to ``(content_type, body)``, or ``None`` for a 404.

    Both the ``default`` alias and the account's own email are accepted as the
    service-account segment, because the libraries use each in different code
    paths.
    """
    if path in ("/", _METADATA_PREFIX):
        # The residency ping. The Metadata-Flavor response header is the whole
        # answer; the body is ignored.
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
