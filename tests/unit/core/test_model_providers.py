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

"""Unit tests for the model-provider contract."""

import json
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Sequence
from http.client import HTTPMessage

import pytest
from pydantic import ValidationError

from devops_bench.core import model_providers
from devops_bench.core.errors import ConfigError, SubprocessError
from devops_bench.core.model_providers import (
    VERTEX_SANDBOX_SA_ENV,
    known_providers,
    resolve_provider,
    sandbox_credential_env,
)

# (raw alias, canonical, adapter_family, oc_provider, api_key_envs, keyless_ok, backend)
_ROWS = [
    ("gemini", "google", "gemini", "google", ("GEMINI_API_KEY", "GOOGLE_API_KEY"), False, None),
    ("google", "google", "gemini", "google", ("GEMINI_API_KEY", "GOOGLE_API_KEY"), False, None),
    (
        "google-vertex",
        "google-vertex",
        "gemini",
        "google-vertex",
        ("GOOGLE_CLOUD_API_KEY",),
        True,
        "vertex",
    ),
    (
        "google_vertex",
        "google-vertex",
        "gemini",
        "google-vertex",
        ("GOOGLE_CLOUD_API_KEY",),
        True,
        "vertex",
    ),
    ("anthropic", "anthropic", "claude", "anthropic", ("ANTHROPIC_API_KEY",), False, None),
    ("claude", "anthropic", "claude", "anthropic", ("ANTHROPIC_API_KEY",), False, None),
    ("anthropic-vertex", "anthropic-vertex", "claude", "anthropic-vertex", (), True, "vertex"),
    ("anthropic_vertex", "anthropic-vertex", "claude", "anthropic-vertex", (), True, "vertex"),
    ("anthropic-bedrock", "anthropic-bedrock", "claude", "anthropic-bedrock", (), True, "bedrock"),
    ("anthropic_bedrock", "anthropic-bedrock", "claude", "anthropic-bedrock", (), True, "bedrock"),
    ("openai", "openai", "openai", "openai", ("OPENAI_API_KEY",), False, None),
    ("ollama", "ollama", "ollama", "ollama", (), True, None),
]


@pytest.mark.parametrize("raw,canonical,family,oc_provider,api_key_envs,keyless,backend", _ROWS)
def test_resolve_provider_table(
    raw, canonical, family, oc_provider, api_key_envs, keyless, backend
):
    spec = resolve_provider(raw)
    assert spec.canonical == canonical
    assert spec.adapter_family == family
    assert spec.oc_provider == oc_provider
    assert spec.api_key_envs == api_key_envs
    assert spec.keyless_ok is keyless
    assert spec.backend == backend


def test_resolve_provider_is_case_insensitive():
    assert resolve_provider("GEMINI").canonical == "google"
    assert resolve_provider("Google-Vertex").canonical == "google-vertex"


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_blank_resolves_to_default(blank):
    assert resolve_provider(blank).canonical == "google"
    assert resolve_provider(blank, default="anthropic").canonical == "anthropic"


def test_unknown_provider_raises_with_known_list():
    with pytest.raises(ConfigError) as exc:
        resolve_provider("mystery")
    msg = str(exc.value)
    assert "mystery" in msg
    assert "gemini" in msg and "anthropic" in msg


def test_only_vertex_bedrock_ollama_are_keyless():
    keyless = {raw for raw, *_ in _ROWS if resolve_provider(raw).keyless_ok}
    assert keyless == {
        "google-vertex",
        "google_vertex",
        "anthropic-vertex",
        "anthropic_vertex",
        "anthropic-bedrock",
        "anthropic_bedrock",
        "ollama",
    }


def test_provider_spec_is_frozen():
    spec = resolve_provider("google")
    with pytest.raises(ValidationError):  # frozen pydantic model rejects mutation
        spec.canonical = "other"


def test_known_providers_sorted_and_complete():
    known = known_providers()
    assert known == tuple(sorted(known))
    assert "google-vertex" in known and "ollama" in known


# --------------------------------------------------------------------------
# Sandboxed keyless-backend credentials (the Vertex metadata emulator)
# --------------------------------------------------------------------------

_SA = "bench-agent-vertex@example.iam.gserviceaccount.com"
_PROJECT = "example-project"


@pytest.fixture(autouse=True)
def _no_leaked_emulators() -> Iterator[None]:
    """Keep the process-wide emulator cache from leaking between tests."""
    yield
    for emulator in model_providers._EMULATORS.values():
        emulator.stop()
    model_providers._EMULATORS.clear()


@pytest.fixture
def minted(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Patch the module's ``run`` with a fake gcloud; record the argv it was given."""
    calls: list[list[str]] = []

    def fake_run(cmd: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append([str(part) for part in cmd])
        return subprocess.CompletedProcess(
            args=list(cmd), returncode=0, stdout="ya29.fake-token\n", stderr=""
        )

    monkeypatch.setattr(model_providers, "run", fake_run)
    return calls


def _get(
    emulator: model_providers._VertexMetadataEmulator, path: str, *, flavor: bool = True
) -> tuple[int, HTTPMessage, str]:
    """GET ``path`` off a running emulator over the loopback interface."""
    request = urllib.request.Request(f"http://127.0.0.1:{emulator.port}{path}")
    if flavor:
        request.add_header("Metadata-Flavor", "Google")
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, response.headers, response.read().decode("utf-8")


@pytest.mark.parametrize("raw", ["google", "anthropic", "openai"])
def test_key_based_providers_need_no_recipe(raw: str) -> None:
    # Their key is already in the overlay and crosses the boundary by value.
    assert sandbox_credential_env(resolve_provider(raw)) == {}


def test_ollama_needs_no_recipe() -> None:
    # Keyless, but it authenticates against a local endpoint, not a cloud identity.
    assert sandbox_credential_env(resolve_provider("ollama")) == {}


def test_bedrock_fails_loud_rather_than_silently_unauthenticated() -> None:
    with pytest.raises(ConfigError) as exc:
        sandbox_credential_env(resolve_provider("anthropic-bedrock"))
    assert "bedrock" in str(exc.value)
    assert "recipe" in str(exc.value)


def test_vertex_without_a_service_account_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(VERTEX_SANDBOX_SA_ENV, raising=False)
    with pytest.raises(ConfigError) as exc:
        sandbox_credential_env(resolve_provider("google-vertex"), project=_PROJECT)
    assert VERTEX_SANDBOX_SA_ENV in str(exc.value)


def test_vertex_without_a_project_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(VERTEX_SANDBOX_SA_ENV, _SA)
    with pytest.raises(ConfigError) as exc:
        sandbox_credential_env(resolve_provider("google-vertex"), project=None)
    assert "GOOGLE_CLOUD_PROJECT" in str(exc.value)


def test_vertex_env_points_the_container_at_the_emulator(
    minted: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(VERTEX_SANDBOX_SA_ENV, _SA)
    env = sandbox_credential_env(resolve_provider("google-vertex"), project=_PROJECT)

    assert set(env) == {"GCE_METADATA_HOST", "GCE_METADATA_IP", "METADATA_SERVER_DETECTION"}
    assert env["METADATA_SERVER_DETECTION"] == "assume-present"
    # host.docker.internal, because wrap_argv always --add-host's it to the
    # host gateway; no scheme, because neither library accepts one.
    host, _, port = env["GCE_METADATA_HOST"].partition(":")
    assert host == "host.docker.internal"
    assert port.isdigit()
    assert env["GCE_METADATA_IP"] == env["GCE_METADATA_HOST"]
    # Nothing is minted until the agent actually asks for a token.
    assert minted == []


def test_the_service_account_argument_overrides_the_env(
    minted: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(VERTEX_SANDBOX_SA_ENV, "wrong@example.iam.gserviceaccount.com")
    sandbox_credential_env(resolve_provider("google-vertex"), project=_PROJECT, service_account=_SA)
    (emulator,) = model_providers._EMULATORS.values()
    assert emulator.service_account == _SA


def test_one_emulator_is_reused_across_runs(
    minted: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(VERTEX_SANDBOX_SA_ENV, _SA)
    first = sandbox_credential_env(resolve_provider("google-vertex"), project=_PROJECT)
    second = sandbox_credential_env(resolve_provider("google-vertex"), project=_PROJECT)
    assert first == second
    assert len(model_providers._EMULATORS) == 1


def test_token_endpoint_serves_an_impersonated_token(
    minted: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(VERTEX_SANDBOX_SA_ENV, _SA)
    sandbox_credential_env(resolve_provider("google-vertex"), project=_PROJECT)
    (emulator,) = model_providers._EMULATORS.values()

    status, headers, body = _get(
        emulator, "/computeMetadata/v1/instance/service-accounts/default/token"
    )
    assert status == 200
    assert headers["Metadata-Flavor"] == "Google"
    payload = json.loads(body)
    assert payload["access_token"] == "ya29.fake-token"
    assert payload["token_type"] == "Bearer"
    assert payload["expires_in"] > 0

    # The mint is an impersonation, scoped by the SA's own IAM role.
    assert minted == [
        [
            "gcloud",
            "auth",
            "print-access-token",
            f"--impersonate-service-account={_SA}",
            "--scopes=https://www.googleapis.com/auth/cloud-platform",
        ]
    ]


def test_the_token_is_cached_across_requests(
    minted: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(VERTEX_SANDBOX_SA_ENV, _SA)
    sandbox_credential_env(resolve_provider("google-vertex"), project=_PROJECT)
    (emulator,) = model_providers._EMULATORS.values()
    path = "/computeMetadata/v1/instance/service-accounts/default/token"
    _get(emulator, path)
    _get(emulator, path)
    assert len(minted) == 1


def test_a_token_close_to_expiry_is_refilled(
    minted: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(VERTEX_SANDBOX_SA_ENV, _SA)
    sandbox_credential_env(resolve_provider("google-vertex"), project=_PROJECT)
    (emulator,) = model_providers._EMULATORS.values()
    emulator.token()
    # Inside the refresh margin: the next caller must not be handed a token
    # that could expire mid-generation.
    emulator._expires_at = time.monotonic() + 10
    emulator.token()
    assert len(minted) == 2


def test_a_failed_mint_is_reported_with_the_iam_remedy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(VERTEX_SANDBOX_SA_ENV, _SA)

    def failing_run(cmd: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=list(cmd), returncode=1, stdout="", stderr="PERMISSION_DENIED"
        )

    monkeypatch.setattr(model_providers, "run", failing_run)
    sandbox_credential_env(resolve_provider("google-vertex"), project=_PROJECT)
    (emulator,) = model_providers._EMULATORS.values()
    with pytest.raises(ConfigError) as exc:
        emulator.token()
    assert "PERMISSION_DENIED" in str(exc.value)
    assert "serviceAccountTokenCreator" in str(exc.value)


def test_the_mint_is_bounded_and_a_hang_is_a_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mint runs under the emulator's lock, so a hung gcloud must surface as
    a loud failure with the timeout on the call, never an indefinite block."""
    timeouts: list[object] = []

    def hanging_run(cmd: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        timeouts.append(kwargs.get("timeout"))
        # What core.subprocess.run raises on TimeoutExpired: returncode -1.
        raise SubprocessError(cmd, returncode=-1, stderr="")

    monkeypatch.setenv(VERTEX_SANDBOX_SA_ENV, _SA)
    monkeypatch.setattr(model_providers, "run", hanging_run)
    sandbox_credential_env(resolve_provider("google-vertex"), project=_PROJECT)
    (emulator,) = model_providers._EMULATORS.values()
    with pytest.raises(ConfigError) as exc:
        emulator.token()
    assert _SA in str(exc.value)
    assert "bounded at" in str(exc.value)
    assert timeouts == [model_providers._MINT_TIMEOUT_SEC]


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/computeMetadata/v1/project/project-id", _PROJECT),
        ("/computeMetadata/v1/instance/service-accounts/default/email", _SA),
        (f"/computeMetadata/v1/instance/service-accounts/{_SA}/email", _SA),
        ("/computeMetadata/v1/instance/service-accounts/default/aliases", "default\n"),
        ("/computeMetadata/v1/universe/universe-domain", "googleapis.com"),
        ("/computeMetadata/v1/instance/service-accounts/", f"default/\n{_SA}/\n"),
    ],
)
def test_served_metadata_paths(
    minted: list[list[str]], monkeypatch: pytest.MonkeyPatch, path: str, expected: str
) -> None:
    monkeypatch.setenv(VERTEX_SANDBOX_SA_ENV, _SA)
    sandbox_credential_env(resolve_provider("google-vertex"), project=_PROJECT)
    (emulator,) = model_providers._EMULATORS.values()
    status, headers, body = _get(emulator, path)
    assert status == 200
    assert headers["Metadata-Flavor"] == "Google"
    assert body == expected


def test_the_recursive_account_listing_is_served(
    minted: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(VERTEX_SANDBOX_SA_ENV, _SA)
    sandbox_credential_env(resolve_provider("google-vertex"), project=_PROJECT)
    (emulator,) = model_providers._EMULATORS.values()
    _, _, body = _get(
        emulator, "/computeMetadata/v1/instance/service-accounts/default/?recursive=true"
    )
    assert json.loads(body) == {
        "aliases": ["default"],
        "email": _SA,
        "scopes": ["https://www.googleapis.com/auth/cloud-platform"],
    }


def test_the_residency_ping_answers_with_the_flavor_header(
    minted: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(VERTEX_SANDBOX_SA_ENV, _SA)
    sandbox_credential_env(resolve_provider("google-vertex"), project=_PROJECT)
    (emulator,) = model_providers._EMULATORS.values()
    for path in ("/", "/computeMetadata/v1/"):
        status, headers, _ = _get(emulator, path)
        assert status == 200
        assert headers["Metadata-Flavor"] == "Google"


@pytest.mark.parametrize(
    "path",
    [
        "/computeMetadata/v1/instance/attributes/kube-env",
        "/computeMetadata/v1/instance/service-accounts/other@example.com/token",
        "/computeMetadata/v1/project/attributes/ssh-keys",
        "/computeMetadata/v1/instance/disks/",
        "/anything-else",
    ],
)
def test_unserved_paths_are_404_not_proxied(
    minted: list[list[str]], monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    # This is an emulator of three endpoints, never a metadata proxy: the
    # container must not be able to read anything the real server would serve.
    monkeypatch.setenv(VERTEX_SANDBOX_SA_ENV, _SA)
    sandbox_credential_env(resolve_provider("google-vertex"), project=_PROJECT)
    (emulator,) = model_providers._EMULATORS.values()
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(emulator, path)
    assert exc.value.code == 404


def test_a_request_without_the_flavor_header_is_refused(
    minted: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(VERTEX_SANDBOX_SA_ENV, _SA)
    sandbox_credential_env(resolve_provider("google-vertex"), project=_PROJECT)
    (emulator,) = model_providers._EMULATORS.values()
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(
            emulator,
            "/computeMetadata/v1/instance/service-accounts/default/token",
            flavor=False,
        )
    assert exc.value.code == 403
    assert minted == []
