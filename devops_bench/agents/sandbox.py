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

"""Run the agent-under-test inside a container with a scoped view of the world.

The container sees the per-run workspace at ``/workspace`` (``HOME`` under it),
the task's seeded fixtures, a single-cluster kubeconfig read-only at
``/creds/kubeconfig``, and a deny-filtered env overlay passed as name-only ``-e``
flags; never the operator's filesystem, cloud credentials or admin kubeconfig. A
sandbox that cannot be built raises :class:`~devops_bench.core.errors.SandboxError`
rather than running ambient. Host reachability is the host setup's
``DOCKER-USER`` rules, not this module's; the cluster credential comes from
:mod:`devops_bench.k8s.agent_credentials`, the model credential from
:mod:`devops_bench.core.model_providers`.
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from devops_bench.core import ClusterInfo, NetworkPlan, get_env, get_logger
from devops_bench.core.errors import SandboxError, SubprocessError
from devops_bench.core.subprocess import CompletedProcess, run
from devops_bench.k8s import kubectl

if TYPE_CHECKING:
    from devops_bench.providers.base import Provider

__all__ = [
    "NetworkPlan",
    "SandboxSpec",
    "SandboxExecutor",
    "spec_from_env",
    "build_network_plan",
    "container_path",
    "discover_fixture_mounts",
    "filter_boundary_env",
    "container_name_for_workspace",
    "image_digest",
    "kill_container",
    "sweep_stray_containers",
]

_log = get_logger("agents.sandbox")

# Opt-in so sandboxed runs can be A/B'd against ambient; unset is the pre-sandbox behavior.
SANDBOX_ENV = "BENCH_AGENT_SANDBOX"
IMAGE_ENV = "BENCH_SANDBOX_IMAGE"
# Optional attempt id scoping container names and the stray sweep; validated at opt-in.
OWNER_ENV = "BENCH_AGENT_SANDBOX_OWNER"
_OWNER_PATTERN = re.compile(r"[A-Za-z0-9_]{1,128}")
CONTAINER_RUNTIME = "docker"
_SANDBOX_ENABLED_VALUES = frozenset({CONTAINER_RUNTIME, "1", "true"})
# Anything outside these two sets raises rather than silently running ambient.
_SANDBOX_DISABLED_VALUES = frozenset({"", "0", "false", "no", "off"})

# ``:``-separated host paths naming this run's fixtures when their names lack the cluster token.
FIXTURES_ENV = "BENCH_AGENT_FIXTURES"

# HOME under the workspace so agent writes land where the harness collects; the
# kubeconfig outside it so the read-only bind is the only path to the credential.
CONTAINER_WORKSPACE = "/workspace"
CONTAINER_HOME = f"{CONTAINER_WORKSPACE}/home"
CONTAINER_KUBECONFIG = "/creds/kubeconfig"

# Docker refuses ``--user`` ids above int32 max (exit 125); IdP-minted ids can exceed it.
# Dropping ``--user`` would run the agent as root, so such ids are remapped (_needs_id_remap).
_MAX_CONTAINER_ID = 2**31 - 1

# In-range unprivileged id for the remap: the ``node`` user of the node:22-slim
# base the sandbox images build on.
_REMAP_UID = 1000
_REMAP_GID = 1000

# A name match authorizes a kill, so this must never match a container we did not start.
_CONTAINER_NAME_PREFIX = "devops-bench-agent-"

# Never cross: operator cloud identity by name, credential families by prefix.
# No blanket GOOGLE_/GCP_ prefix: the model-routing vars share it and must cross.
_DENIED_ENV_NAMES = frozenset(
    {
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_OAUTH_ACCESS_TOKEN",
        "GOOGLE_GHA_CREDS_PATH",
        "HOME",
        "KUBECONFIG",
        "PATH",
    }
)
_DENIED_ENV_PREFIXES = ("BENCH_", "TF_", "AWS_", "AZURE_", "ARM_", "CLOUDSDK_")

# Set by the executor inside the container; not even allowlistable.
_CONTAINER_OWNED_ENV = frozenset({"HOME", "KUBECONFIG", "PATH"})

# Resolve to the container itself once sandboxed (real kubeconfigs do carry 0.0.0.0).
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})

# Bound on docker/kubectl housekeeping so a wedged daemon cannot hang a reap.
_HOUSEKEEPING_TIMEOUT_SEC = 30

# docker-run's own launch failures: 125 daemon error, 126/127 command not invocable/found.
_DOCKER_LAUNCH_FAILURE_CODES = frozenset({125, 126, 127})

# The benchmark checkout is the answer material; fixture discovery refuses it by
# path, since a cluster named "bench" would make it a legitimate token match.
_BENCH_REPO_ROOT = Path(__file__).resolve().parents[2]


def _overlaps_bench_checkout(path: Path) -> bool:
    resolved = path.resolve()
    return resolved.is_relative_to(_BENCH_REPO_ROOT) or _BENCH_REPO_ROOT.is_relative_to(resolved)


@dataclass(frozen=True)
class SandboxSpec:
    """Everything the executor needs to wrap one run's agent in ``docker run``.

    :func:`spec_from_env` yields the image and owner; the eval harness fills in
    the rest per task. ``fixture_mounts`` maps host path -> container path (RW);
    ``env_allowlist`` lets named vars cross despite a deny rule; ``owner`` scopes
    container names and the stray sweep to one attempt.
    """

    image: str = ""
    network: NetworkPlan = field(default_factory=NetworkPlan)
    workspace: Path | None = None
    kubeconfig: Path | None = None
    fixture_mounts: Mapping[str, str] = field(default_factory=dict)
    env_allowlist: tuple[str, ...] = ()
    owner: str = ""


def spec_from_env(env: Mapping[str, str] | None = None) -> SandboxSpec | None:
    """Read the sandbox opt-in and owner; ``None`` when off, :class:`SandboxError` on a typo."""
    raw = (get_env(SANDBOX_ENV, env=env) or "").strip().lower()
    if raw in _SANDBOX_ENABLED_VALUES:
        owner = (get_env(OWNER_ENV, env=env) or "").strip()
        # Checked here so a typo refuses the batch instead of erroring every task.
        if owner and not _OWNER_PATTERN.fullmatch(owner):
            raise SandboxError(f"{OWNER_ENV}={owner!r} must be 1-128 characters of [A-Za-z0-9_]")
        return SandboxSpec(image=(get_env(IMAGE_ENV, env=env) or "").strip(), owner=owner)
    if raw in _SANDBOX_DISABLED_VALUES:
        return None
    raise SandboxError(
        f"{SANDBOX_ENV}={raw!r} is not a recognized value; use docker/1/true to "
        "sandbox, or 0/false/no/off (or unset) to run ambient — refusing to guess, "
        "because guessing wrong would silently run the agent unsandboxed"
    )


def build_network_plan(provider: Provider | None, cluster_info: ClusterInfo) -> NetworkPlan:
    """Build the :class:`NetworkPlan` for this run's cluster.

    The provider supplies the context pin (and a Docker network or in-network
    hostname when it has one); this module remaps a loopback server to
    ``host.docker.internal``. ``provider=None`` (no-op deployer) yields the
    default plan on the ambient current-context.

    Raises:
        SandboxError: An unpinned provider plan, an unknown context, or an
            unreadable server URL.
    """
    plan = provider.sandbox_network_plan(cluster_info) if provider is not None else NetworkPlan()
    if provider is not None and not plan.kubectl_context:
        # Only a provider-less run may mint on the ambient current-context.
        raise SandboxError(
            f"provider {type(provider).__name__} returned a network plan with no "
            f"kubectl context pin for cluster {cluster_info.name!r}; provisioning "
            "credentials on the ambient current-context is reserved for runs with "
            "no provider at all — pin the plan to the context this cluster wrote"
        )
    if plan.kubectl_context:
        known = (
            run(
                ["kubectl", "config", "get-contexts", "-o", "name"],
                check=False,
                timeout=_HOUSEKEEPING_TIMEOUT_SEC,
            ).stdout
            or ""
        ).split()
        if plan.kubectl_context not in known:
            raise SandboxError(
                f"kubectl has no {plan.kubectl_context!r} context for this run's cluster "
                f"{cluster_info.name!r}; this kubeconfig never saw the cluster the "
                "provider named — refusing to build a plan from the ambient context"
            )
    return _rewrite_loopback_server(plan)


def _rewrite_loopback_server(plan: NetworkPlan) -> NetworkPlan:
    """Remap a loopback apiserver URL to the host gateway, or pass the plan through.

    Keeps a declared ``tls-server-name``, else ``localhost`` (the SAN a
    loopback-published cluster has), so TLS stays verified.
    """
    if plan.rewrite_server:
        return plan
    # One read for both; the second line is empty when undeclared.
    server, _, declared = kubectl.config_value(
        '{.clusters[0].cluster.server}{"\\n"}{.clusters[0].cluster.tls-server-name}',
        context=plan.kubectl_context,
    ).partition("\n")
    declared = declared.strip()
    if not server:
        raise SandboxError(
            "could not read the cluster server URL from the run's kubectl context; "
            "refusing to build a sandbox network plan from an unknown endpoint"
        )
    parsed = urlsplit(server)
    if parsed.hostname not in _LOOPBACK_HOSTS:
        return plan
    port = f":{parsed.port}" if parsed.port else ""
    _log.info(
        "cluster apiserver is published on loopback (%s); the container will reach it "
        "at host.docker.internal%s",
        server,
        port,
    )
    return replace(
        plan,
        rewrite_server=f"https://host.docker.internal{port}",
        tls_server_name=plan.tls_server_name or declared or "localhost",
    )


def discover_fixture_mounts(cluster_name: str | None) -> dict[str, str]:
    """Find this run's seeded task fixtures and map them into the container.

    Task stacks seed inputs in the operator's home (``~/opa-repo-<cluster>.git``),
    which the container does not mount. Only top-level entries carrying
    ``cluster_name`` as a ``-``/``_``/``.``-delimited token match (dot-entries
    excluded); ``BENCH_AGENT_FIXTURES`` overrides the search; the benchmark
    checkout is refused by path. Raises :class:`SandboxError` on a
    container-path collision or an explicit fixture inside the checkout.
    """
    explicit = (get_env(FIXTURES_ENV) or "").strip()
    if explicit:
        candidates = [Path(p).expanduser() for p in explicit.split(":") if p.strip()]
    elif not cluster_name:
        return {}
    else:
        home = Path.home()
        if not home.is_dir():
            return {}
        # Bare *<name>* matches substrings and dotfiles; require a token boundary.
        token = re.compile(rf"(^|[-_.]){re.escape(cluster_name)}([-_.]|$)")
        candidates = sorted(
            p
            for p in home.glob(f"*{glob.escape(cluster_name)}*")
            if not p.name.startswith(".") and token.search(p.name)
        )

    mounts: dict[str, str] = {}
    dest_owner: dict[str, str] = {}
    for path in candidates:
        if not path.exists():
            _log.warning("declared fixture %s does not exist; not mounting it", path)
            continue
        if _overlaps_bench_checkout(path):
            if explicit:
                raise SandboxError(
                    f"fixture {path} overlaps the benchmark checkout at "
                    f"{_BENCH_REPO_ROOT}; mounting it would hand the agent the "
                    f"benchmark's own answer material — remove it from {FIXTURES_ENV}"
                )
            _log.warning(
                "fixture candidate %s overlaps the benchmark checkout; not mounting it",
                path,
            )
            continue
        host_path = str(path.resolve())
        container_path = f"{CONTAINER_HOME}/{path.name}"
        # Two sources, one destination: docker aborts with "Duplicate mount point".
        if container_path in dest_owner and dest_owner[container_path] != host_path:
            raise SandboxError(
                f"fixture name collision: {dest_owner[container_path]} and {host_path} "
                f"would both mount at {container_path}; rename one or narrow "
                f"{FIXTURES_ENV}"
            )
        dest_owner[container_path] = host_path
        mounts[host_path] = container_path
    if mounts:
        _log.info("mounting %d task fixture(s): %s", len(mounts), sorted(mounts))
    return mounts


def container_path(workspace: str | os.PathLike[str], path: str | os.PathLike[str]) -> str:
    """Map a host path under ``workspace`` to the path the container sees.

    Module-level because harnesses translate env values (e.g. ``OPENCLAW_STATE_DIR``)
    before handing them over, and must agree with the executor's ``cwd`` mapping.
    Raises :class:`SandboxError` outside the workspace: the mount set is the
    boundary and only widens through an explicit spec field.
    """
    resolved = Path(path).resolve()
    root = Path(workspace).resolve()
    if resolved == root:
        return CONTAINER_WORKSPACE
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise SandboxError(
            f"host path {resolved} is outside the sandbox workspace {root} "
            "and has no container mapping; refusing to widen the mount set"
        ) from exc
    return f"{CONTAINER_WORKSPACE}/{relative.as_posix()}"


def _env_denied(name: str) -> bool:
    return name in _DENIED_ENV_NAMES or name.startswith(_DENIED_ENV_PREFIXES)


def filter_boundary_env(
    overlay: Mapping[str, str] | None, allowlist: Sequence[str] = ()
) -> dict[str, str]:
    """Filter a resolved env overlay down to what may cross the boundary.

    Only the overlay is considered, never ``os.environ``. Denied names drop
    with a warning unless allowlisted; container-owned names never cross.
    """
    kept: dict[str, str] = {}
    for name, value in (overlay or {}).items():
        if name in _CONTAINER_OWNED_ENV:
            _log.warning(
                "env var %s is container-owned and never crosses the sandbox "
                "boundary, even allowlisted; dropped",
                name,
            )
        elif name in allowlist or not _env_denied(name):
            kept[name] = value
        else:
            _log.warning("env var %s does not cross the sandbox boundary; dropped", name)
    return kept


class SandboxExecutor:
    """Executes one run's agent commands inside ``docker run``.

    Signature-compatible with :func:`devops_bench.core.subprocess.run`: same
    return shape, ``check`` semantics and timeout behavior. One executor per
    run; the container name derives from the workspace directory so a reaper
    can find strays by name.
    """

    def __init__(self, spec: SandboxSpec) -> None:
        if not spec.image:
            raise SandboxError(
                f"{SANDBOX_ENV} is set but no sandbox image is configured; "
                f"set {IMAGE_ENV} to the image containing the agent CLI"
            )
        if spec.workspace is None or spec.kubeconfig is None:
            raise SandboxError(
                "sandbox spec is incomplete (no workspace/kubeconfig); the eval "
                "harness completes the spec after provisioning — refusing to run "
                "the agent unsandboxed"
            )
        if not Path(spec.workspace).is_dir() or not Path(spec.kubeconfig).is_file():
            raise SandboxError(
                f"sandbox spec points at a missing workspace or kubeconfig "
                f"({spec.workspace}, {spec.kubeconfig}); refusing to run the agent unsandboxed"
            )
        self.spec = spec
        self._workspace = Path(spec.workspace)
        self.container_name = container_name_for_workspace(self._workspace, spec.owner)

    def map_host_path(self, path: str | os.PathLike[str]) -> str:
        """Map a host path under this executor's workspace to its container path."""
        return container_path(self._workspace, path)

    def _needs_id_remap(self) -> bool:
        """Whether either host id exceeds Docker's int32 ``--user`` limit; always False off Linux."""
        if not sys.platform.startswith("linux"):
            return False
        return os.getuid() > _MAX_CONTAINER_ID or os.getgid() > _MAX_CONTAINER_ID

    def _remap_mounts(self) -> list[tuple[str, str]]:
        """Host -> container path for every mount a chown pass must cover.

        Fixtures live outside the workspace, so the workspace alone is not enough.
        """
        spec = self.spec
        return [
            (str(spec.workspace), CONTAINER_WORKSPACE),
            (str(spec.kubeconfig), CONTAINER_KUBECONFIG),
            *spec.fixture_mounts.items(),
        ]

    @property
    def _chown_container_name(self) -> str:
        return f"{self.container_name}-chown"

    def _chown_argv(self, uid: int, gid: int, *, from_uid: int) -> list[str]:
        """``docker run`` argv for a root container (same image, no ``--user``) that
        chowns every remap mount to ``uid:gid``; only root can chown ids past int32.

        ``--from`` limits each pass to entries the previous pass is responsible
        for, so a fixture entry owned by anyone else keeps its owner.
        """
        argv = [CONTAINER_RUNTIME, "run", "--rm", "--name", self._chown_container_name]
        targets: list[str] = []
        for host_path, mount_path in self._remap_mounts():
            argv += ["-v", f"{host_path}:{mount_path}"]
            targets.append(mount_path)
        argv += [self.spec.image, "chown", "-R", f"--from={from_uid}", f"{uid}:{gid}", *targets]
        return argv

    def _chown_before_remap(self) -> None:
        """Chown workspace and fixtures to the remap id before the agent starts.

        Fatal: an unprivileged agent cannot write a workspace it does not own.
        """
        argv = self._chown_argv(_REMAP_UID, _REMAP_GID, from_uid=os.getuid())
        try:
            run(argv, check=True, timeout=_HOUSEKEEPING_TIMEOUT_SEC)
        except (OSError, SubprocessError) as exc:
            raise SandboxError(
                f"could not chown the workspace/fixtures to {_REMAP_UID}:{_REMAP_GID} before "
                "the id-remapped agent container; refusing to hand it a workspace it cannot write"
            ) from exc

    def _chown_after_remap(self) -> None:
        """Chown workspace and fixtures back to the host uid/gid after the agent exits.

        Best-effort: a failure must not mask the agent result, so it is logged with
        the exact repair command.
        """
        uid, gid = os.getuid(), os.getgid()
        argv = self._chown_argv(uid, gid, from_uid=_REMAP_UID)
        try:
            run(argv, check=True, timeout=_HOUSEKEEPING_TIMEOUT_SEC)
        except (OSError, SubprocessError):
            _log.error(
                "could not chown the workspace/fixtures back to %s:%s; they stay owned by "
                "the remap id. Repair manually: %s",
                uid,
                gid,
                " ".join(argv),
            )

    def wrap_argv(
        self,
        cmd: Sequence[str | os.PathLike[str]],
        *,
        cwd: str | os.PathLike[str] | None = None,
        extra_env: Mapping[str, str] | None = None,
    ) -> list[str]:
        """Wrap an agent command line in ``docker run``.

        ``--rm`` plus a deterministic ``--name`` (strays are reap-able);
        ``--cap-drop=ALL`` and ``no-new-privileges``; the network plan and
        ``host.docker.internal:host-gateway``; ``--user`` on Linux so workspace
        files stay operator-owned; workspace RW, kubeconfig RO, fixtures RW;
        overlay env as name-only ``-e`` (values ride the client env, never the
        argv); ``HOME``/``KUBECONFIG`` last so they win; no ``-i``.
        """
        spec = self.spec
        argv: list[str] = [CONTAINER_RUNTIME, "run", "--rm", "--name", self.container_name]
        argv += ["--cap-drop=ALL", "--security-opt=no-new-privileges=true"]
        if spec.network.docker_network:
            argv += ["--network", spec.network.docker_network]
        argv += ["--add-host", "host.docker.internal:host-gateway"]
        for host_entry in spec.network.extra_hosts:
            argv += ["--add-host", host_entry]
        if sys.platform.startswith("linux"):
            uid, gid = (
                (_REMAP_UID, _REMAP_GID) if self._needs_id_remap() else (os.getuid(), os.getgid())
            )
            argv += ["--user", f"{uid}:{gid}"]
        argv += ["-v", f"{spec.workspace}:{CONTAINER_WORKSPACE}"]
        argv += ["-v", f"{spec.kubeconfig}:{CONTAINER_KUBECONFIG}:ro"]
        for host_path, container_path in spec.fixture_mounts.items():
            argv += ["-v", f"{host_path}:{container_path}"]
        for name in filter_boundary_env(extra_env, spec.env_allowlist):
            argv += ["-e", name]
        argv += ["-e", f"HOME={CONTAINER_HOME}", "-e", f"KUBECONFIG={CONTAINER_KUBECONFIG}"]
        argv += ["-w", self.map_host_path(cwd) if cwd is not None else CONTAINER_WORKSPACE]
        argv.append(spec.image)
        argv.extend(str(part) for part in cmd)
        return argv

    def run(
        self,
        cmd: Sequence[str | os.PathLike[str]],
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        extra_env: Mapping[str, str] | None = None,
        check: bool = True,
        capture: bool = True,
        text: bool = True,
        timeout: float | None = None,
        input: str | None = None,
    ) -> CompletedProcess:
        """Run ``cmd`` in the sandbox container; mirrors ``core.subprocess.run``.

        ``env`` is rejected (credential-inheritance channel), as is a non-empty
        ``input`` (no ``-i``, so it would be lost; an empty one is a no-op).
        Docker's own launch failures raise :class:`SandboxError` rather
        than masquerading as an agent exit code. The container is reaped by name
        on every exit path, since ``--rm`` does not fire when the client is
        killed by the host-side timeout.

        Raises:
            SandboxError: On ``env``/``input``, an unmappable ``cwd``, or a
                docker-level launch failure.
            SubprocessError: On timeout, or non-zero exit when ``check``.
        """
        if env is not None:
            raise SandboxError(
                "SandboxExecutor never forwards a full environment; pass the "
                "resolved overlay via extra_env"
            )
        if input:
            raise SandboxError(
                "the sandboxed agent runs without stdin (no -i, by design); a non-empty "
                "input= would be silently dropped"
            )
        # Filter once so the client env matches the names wrap_argv emits.
        crossing = filter_boundary_env(extra_env, self.spec.env_allowlist)
        wrapped = self.wrap_argv(cmd, cwd=cwd, extra_env=crossing)
        remap = self._needs_id_remap()
        try:
            # Inside the try so a partial or timed-out chown still gets the handback.
            if remap:
                self._chown_before_remap()
            try:
                completed = run(
                    wrapped,
                    extra_env=crossing,
                    check=check,
                    capture=capture,
                    text=text,
                    timeout=timeout,
                )
            except OSError as exc:
                raise SandboxError(f"docker is unavailable: {exc}") from exc
            except SubprocessError as exc:
                # check=True raises before the returncode test below runs.
                if exc.returncode in _DOCKER_LAUNCH_FAILURE_CODES:
                    raise SandboxError(
                        f"docker could not start the sandbox container "
                        f"(exit {exc.returncode}): {exc.stderr}"
                    ) from exc
                raise
            # With check=False these would be scored as the agent's exit code.
            if completed.returncode in _DOCKER_LAUNCH_FAILURE_CODES:
                raise SandboxError(
                    f"docker could not start the sandbox container "
                    f"(exit {completed.returncode}): {completed.stderr}"
                )
            return completed
        finally:
            try:
                kill_container(self.container_name)
            finally:
                if remap:
                    # A timed-out chown helper must not keep running during the handback.
                    kill_container(self._chown_container_name)
                    self._chown_after_remap()


def container_name_for_workspace(workspace: Path, owner: str = "") -> str:
    """Deterministic container name tied 1:1 to the run's workspace directory.

    ``owner`` (:attr:`SandboxSpec.owner`) adds a segment scoping the name to one attempt.
    """
    return f"{_CONTAINER_NAME_PREFIX}{owner + '-' if owner else ''}{workspace.name}"


def image_digest(image: str) -> str | None:
    """Resolve ``image`` to a content digest for the run manifest. Never raises.

    Prefers the first ``RepoDigests`` entry — the registry-anchored identity
    that survives across hosts — and falls back to the local image ID (the
    config hash) for an image that was only ever built locally and has no
    repo digest. Both are content-addressed; either one turns "we ran
    ``agent-sandbox:dev``" from a mutable-tag claim into evidence.

    Best-effort by design: provenance must never sink a finished run, so a
    missing docker binary, an unknown image, or malformed inspect output all
    log and return ``None`` — and the manifest records the absence honestly.

    Args:
        image: The image reference the run used (tag or digest form).

    Returns:
        A ``repo@sha256:...`` or ``sha256:...`` string, or ``None``.
    """
    completed = run(["docker", "image", "inspect", image], check=False)
    if completed.returncode != 0:
        _log.warning(
            "could not resolve a digest for sandbox image %s; the manifest will "
            "carry the tag only (%s)",
            image,
            (completed.stderr or "").strip() or "docker image inspect failed",
        )
        return None
    try:
        inspected = json.loads(completed.stdout or "[]")
        first = inspected[0]
        repo_digests = first.get("RepoDigests") or []
        digest = repo_digests[0] if repo_digests else first.get("Id")
    except (json.JSONDecodeError, IndexError, AttributeError, TypeError):
        _log.warning("unexpected docker inspect output for sandbox image %s", image)
        return None
    return digest or None


def kill_container(name: str) -> None:
    """Best-effort, time-bounded ``docker kill`` by name. Never raises."""
    try:
        result = run(
            [CONTAINER_RUNTIME, "kill", name], check=False, timeout=_HOUSEKEEPING_TIMEOUT_SEC
        )
    except (OSError, SubprocessError):
        _log.warning("could not reap sandbox container %s", name, exc_info=True)
        return
    if result.returncode == 0:
        _log.info("reaped sandbox container %s", name)


def sweep_stray_containers(*, owner: str = "", parallel: bool = False) -> None:
    """Best-effort reap of containers a prior crashed run left behind. Never raises.

    An ``owner`` scopes the sweep to that attempt. Without one the shared prefix
    cannot tell a stray from a sibling harness's live container, so ``parallel`` skips it.
    """
    if not owner and parallel:
        _log.info(
            "BENCH_PARALLEL set and no %s: skipping the stray sandbox-container "
            "sweep; reap leftovers manually with `%s ps --filter name=%s` once no "
            "benchmark is running",
            OWNER_ENV,
            CONTAINER_RUNTIME,
            _CONTAINER_NAME_PREFIX,
        )
        return
    prefix = f"{_CONTAINER_NAME_PREFIX}{owner}-" if owner else _CONTAINER_NAME_PREFIX
    try:
        listed = run(
            [CONTAINER_RUNTIME, "ps", "--format", "{{.Names}}"],
            check=False,
            timeout=_HOUSEKEEPING_TIMEOUT_SEC,
        )
    except (OSError, SubprocessError):
        _log.warning("could not list stray sandbox containers", exc_info=True)
        return
    if listed.returncode != 0:
        return
    for name in (listed.stdout or "").splitlines():
        if name.startswith(prefix):
            kill_container(name)
