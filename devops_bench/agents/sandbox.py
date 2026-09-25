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

Ambient CLI agents inherit the operator's filesystem and environment — the
benchmark's own answer material (task rubrics, scoring code, prior results)
and the operator's cloud credentials and admin kubeconfig. Detection is a
tripwire; this module is the boundary. The container sees exactly four
things: the per-run workspace at ``/workspace`` (``HOME`` repointed under
it), the task's seeded fixtures, a generated single-cluster kubeconfig
read-only at ``/creds/kubeconfig``, and a deny-filtered env overlay passed
as name-only ``-e`` flags so no secret ever sits in the argv. A sandbox that
cannot be built raises :class:`~devops_bench.core.errors.SandboxError`
instead of quietly running the agent on the host.

Known limits of this seam, closed by follow-ups in the same stack: the
kubeconfig still carries the operator's cluster-admin certificate (the
credential-scoping follow-up replaces it with a namespace-scoped
ServiceAccount token), and on a cloud VM the link-local metadata endpoint is
still routable from the container (the metadata-credential follow-up removes
the reason to reach it).
"""

from __future__ import annotations

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

# Opt-in (not default) so sandboxed runs can be A/B'd against ambient ones;
# unset must be byte-for-byte the pre-sandbox behavior.
SANDBOX_ENV = "BENCH_AGENT_SANDBOX"
IMAGE_ENV = "BENCH_SANDBOX_IMAGE"
_SANDBOX_ENABLED_VALUES = frozenset({"docker", "1", "true"})
# Explicit off-values; anything else raises rather than silently running
# ambient under an operator who typed e.g. ``yes``.
_SANDBOX_DISABLED_VALUES = frozenset({"", "0", "false", "no", "off"})

# ``:``-separated host paths naming this run's fixtures explicitly, for
# stacks whose fixture names don't carry the cluster token.
FIXTURES_ENV = "BENCH_AGENT_FIXTURES"

# HOME lives under the workspace so agent writes land in the one host
# directory the harness diffs and collects; the kubeconfig lives outside it
# so the read-only bind is the only path to the credential.
CONTAINER_WORKSPACE = "/workspace"
CONTAINER_HOME = f"{CONTAINER_WORKSPACE}/home"
CONTAINER_KUBECONFIG = "/creds/kubeconfig"

# Docker rejects ``--user`` ids above int32 max ("uids and gids must be in
# range 0-2147483647") and refuses to start the container AT ALL: no ``start``
# event, exit code 125. Identities minted by an external IdP (GCP Cloud
# Identity, Workspace external users, most LDAP setups) routinely exceed this,
# so it is not an exotic case to guard against.
#
# Dropping ``--user`` when this happens (running the agent container as its
# image's default user, root) was considered and rejected: it trades a
# boundary failure for a silent containment downgrade. The untrusted agent
# under test would gain root inside the very boundary this module exists to
# enforce, and it would leave root-owned files in the operator's home and
# workspace that teardown cannot remove without sudo (observed live). So when
# an id is out of range we remap instead of degrading; see ``_REMAP_UID`` /
# ``_REMAP_GID`` and ``SandboxExecutor._needs_id_remap``.
_MAX_CONTAINER_ID = 2**31 - 1

# The unprivileged, in-range id the agent container runs as when the caller's
# real uid or gid cannot be passed to ``--user``. 1000 is not arbitrary: it is
# the ``node`` user baked into the ``node:22-slim`` base these sandbox images
# build on, so the remap lands on a real named unprivileged user rather than
# an anonymous id. The agent stays unprivileged either way, which is the
# property this module protects; only the specific id changes.
_REMAP_UID = 1000
_REMAP_GID = 1000

# A name match is the entire authorization to kill a container, so this
# prefix must never match a container this harness did not start.
_CONTAINER_NAME_PREFIX = "devops-bench-agent-"

# Never cross the boundary even when present in the caller's overlay:
# operator cloud identity by exact name, benchmark/Terraform/cloud-credential
# families by prefix (vendor-neutral deny list for a vendor-neutral boundary).
_DENIED_ENV_NAMES = frozenset(
    {
        "CLOUDSDK_CONFIG",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "HOME",
        "KUBECONFIG",
        "PATH",
    }
)
_DENIED_ENV_PREFIXES = ("BENCH_", "TF_", "AWS_", "AZURE_", "ARM_")

# Executor-owned inside the container; not even allowlistable, since a
# crossing value would repoint HOME/KUBECONFIG/PATH inside the boundary.
_CONTAINER_OWNED_ENV = frozenset({"HOME", "KUBECONFIG", "PATH"})

# Apiserver hosts that resolve to the container itself once sandboxed;
# ``0.0.0.0`` is a bind address, but real kubeconfigs carry it and it is
# equally unroutable from the container.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})

# Bound on the module's own docker/kubectl housekeeping calls, so a wedged
# daemon cannot hang a reap (and with it the whole batch).
_HOUSEKEEPING_TIMEOUT_SEC = 30


@dataclass(frozen=True)
class SandboxSpec:
    """Everything the executor needs to wrap one run's agent in ``docker run``.

    :func:`spec_from_env` yields a skeletal spec (image only); the eval
    harness completes it per task once the workspace and cluster exist.
    ``fixture_mounts`` maps host path -> container path, mounted read-write.
    ``env_allowlist`` names vars permitted to cross despite a deny rule
    (container-owned ``HOME``/``KUBECONFIG``/``PATH`` excepted — never
    crossable).
    """

    image: str = ""
    network: NetworkPlan = field(default_factory=NetworkPlan)
    workspace: Path | None = None
    kubeconfig: Path | None = None
    fixture_mounts: Mapping[str, str] = field(default_factory=dict)
    env_allowlist: tuple[str, ...] = ()


def spec_from_env(env: Mapping[str, str] | None = None) -> SandboxSpec | None:
    """Read the sandbox opt-in; ``None`` when unset or explicitly off.

    Raises :class:`SandboxError` on an unrecognized value — a typo must not
    silently run the agent ambient.
    """
    raw = (get_env(SANDBOX_ENV, env=env) or "").strip().lower()
    if raw in _SANDBOX_ENABLED_VALUES:
        return SandboxSpec(image=(get_env(IMAGE_ENV, env=env) or "").strip())
    if raw in _SANDBOX_DISABLED_VALUES:
        return None
    raise SandboxError(
        f"{SANDBOX_ENV}={raw!r} is not a recognized value; use docker/1/true to "
        "sandbox, or 0/false/no/off (or unset) to run ambient — refusing to guess, "
        "because guessing wrong would silently run the agent unsandboxed"
    )


def build_network_plan(provider: Provider | None, cluster_info: ClusterInfo) -> NetworkPlan:
    """Build the :class:`NetworkPlan` for this run's cluster.

    The provider contributes what only it knows (network, hostname, context
    pin); this module then applies the one provider-agnostic rewrite: a host
    loopback server is remapped to ``host.docker.internal``, which is why
    most providers need no override.

    Args:
        provider: ``None`` (no-op deployer) yields the default plan against
            the ambient current-context.

    Raises:
        SandboxError: A provider-backed plan carries no context pin, the
            provider named a context kubectl does not know, or no server URL
            could be read for a plan without its own rewrite.
    """
    plan = provider.sandbox_network_plan(cluster_info) if provider is not None else NetworkPlan()
    if provider is not None and not plan.kubectl_context:
        # An unpinned plan mints the agent's identity and token on the ambient
        # current-context — whatever the operator's kubeconfig last selected.
        # That is tolerable only for a run with no cluster identity of its own
        # (provider ``None``, gated separately behind an explicit env opt-in);
        # a provider knows which cluster it provisioned, so an unpinned answer
        # here is a bug in the provider, not a state to run in.
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

    Loopback inside a container is the container; ``host.docker.internal``
    reaches the same host listener. ``tls-server-name`` becomes the override
    the source kubeconfig already declared, else ``localhost`` — the SAN a
    loopback-published cluster does have — so TLS stays verified rather than
    disabled. A plan already carrying ``rewrite_server`` is left untouched.
    """
    if plan.rewrite_server:
        return plan
    server = kubectl.config_value("{.clusters[0].cluster.server}", context=plan.kubectl_context)
    if not server:
        raise SandboxError(
            "could not read the cluster server URL from the run's kubectl context; "
            "refusing to build a sandbox network plan from an unknown endpoint"
        )
    parsed = urlsplit(server)
    if parsed.hostname not in _LOOPBACK_HOSTS:
        return plan
    port = f":{parsed.port}" if parsed.port else ""
    declared = kubectl.config_value(
        "{.clusters[0].cluster.tls-server-name}", context=plan.kubectl_context
    )
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

    Task stacks seed inputs in the operator's home (``~/opa-repo-<cluster>.git``)
    and the prompt points the agent at ``~/<name>``; the container mounts
    neither the real home nor the repo, so without this the task is broken —
    and an under-provisioned agent hunts the filesystem instead of giving up.
    Only top-level entries whose name carries ``cluster_name`` as a
    ``-``/``_``/``.``-delimited token match (dot-entries excluded), so a short
    or reused name cannot sweep in the operator's unrelated files.
    ``BENCH_AGENT_FIXTURES`` overrides the search. Raises
    :class:`SandboxError` on a container-path collision.
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
        # pathlib's glob matches dotfiles and substrings: bare *<name>* with
        # cluster "dev" would RW-mount ~/devops-bench. Require a separator
        # boundary and skip hidden entries.
        token = re.compile(rf"(^|[-_.]){re.escape(cluster_name)}([-_.]|$)")
        candidates = sorted(
            p
            for p in home.glob(f"*{cluster_name}*")
            if not p.name.startswith(".") and token.search(p.name)
        )

    mounts: dict[str, str] = {}
    dest_owner: dict[str, str] = {}
    for path in candidates:
        if not path.exists():
            _log.warning("declared fixture %s does not exist; not mounting it", path)
            continue
        host_path = str(path.resolve())
        container_path = f"{CONTAINER_HOME}/{path.name}"
        # Same basename twice would emit two -v flags with one destination,
        # which docker aborts on with a cryptic "Duplicate mount point".
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

    Module-level, not just a method, because a harness has to translate paths
    *before* it hands them over: a value like ``OPENCLAW_STATE_DIR`` crosses the
    boundary inside the env overlay, and the host spelling means nothing on the
    other side. The executor's ``cwd`` mapping and these value translations must
    agree, so they share one implementation.

    Anything outside the workspace raises: the alternative would be to grow the
    mount set to make the path exist, and the mount set is the boundary — it
    only ever widens through an explicit spec field, never as a side effect of a
    call site's ``cwd`` or an env value.

    Args:
        workspace: The run's host workspace, mounted at ``/workspace``.
        path: A host path expected to live under it.

    Returns:
        The container-side absolute path.

    Raises:
        SandboxError: When ``path`` is not under ``workspace``.
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

    Only the caller's overlay is considered — never ``os.environ``, which
    would reinstate credential inheritance. Denied names drop with a warning
    unless allowlisted; container-owned names never cross. The returned
    names become name-only ``-e`` flags, the values ride the docker client's
    environment (never the argv).
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

    Signature-compatible with :func:`devops_bench.core.subprocess.run`, so a
    harness swaps a direct subprocess call for ``run_agent_cmd`` and the
    return shape, ``check`` semantics, and timeout behavior stay the same.
    One executor serves one run; the container name derives from the
    workspace directory name so a reaper can find strays by name alone.
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
        self.spec = spec
        self._workspace = Path(spec.workspace)
        self.container_name = container_name_for_workspace(self._workspace)

    def map_host_path(self, path: str | os.PathLike[str]) -> str:
        """Map a workspace-relative host path into the container; raise outside it."""
        return container_path(self._workspace, path)

    def _needs_id_remap(self) -> bool:
        """Whether the caller's uid/gid must be remapped for ``--user``.

        Only asked on Linux (macOS never passes ``--user`` at all). ``True``
        when either id exceeds Docker's int32 ``--user`` limit, which is
        exactly the case docker itself would otherwise refuse to start a
        container over.
        """
        return os.getuid() > _MAX_CONTAINER_ID or os.getgid() > _MAX_CONTAINER_ID

    def _remap_mounts(self) -> list[tuple[str, str]]:
        """Host path -> container path for every mount a chown pass must cover.

        The workspace, the generated kubeconfig, and every fixture mount (see ``discover_fixture_mounts``):
        fixtures live outside the workspace, in the operator's home, so a chown
        of the workspace alone would strand them exactly as un-owned as before.
        """
        spec = self.spec
        return [
            (str(spec.workspace), CONTAINER_WORKSPACE),
            (str(spec.kubeconfig), CONTAINER_KUBECONFIG),
            *spec.fixture_mounts.items(),
        ]

    def _chown_argv(self, uid: int, gid: int) -> list[str]:
        """``docker run`` argv for a throwaway root container that chowns every
        remap mount to ``uid:gid``.

        Runs from the same sandbox image as the agent container (no extra pull)
        and carries no ``--user``, so it runs as root, the only user that can
        chown arbitrary ids either direction (including up past int32, which
        plain ``chown`` has no restriction on, unlike docker's ``--user``).
        """
        argv = ["docker", "run", "--rm"]
        targets: list[str] = []
        for host_path, mount_path in self._remap_mounts():
            argv += ["-v", f"{host_path}:{mount_path}"]
            targets.append(mount_path)
        argv += [self.spec.image, "chown", "-R", f"{uid}:{gid}", *targets]
        return argv

    def _chown_before_remap(self) -> None:
        """Chown the workspace and fixtures to ``_REMAP_UID:_REMAP_GID`` before
        the agent container starts.

        Fatal on failure: running on without it means the remapped, unprivileged
        agent container cannot write a workspace it does not own, and the run
        would produce a misleading result instead of an honest refusal.
        """
        argv = self._chown_argv(_REMAP_UID, _REMAP_GID)
        try:
            run(argv, check=True)
        except SubprocessError as exc:
            raise SandboxError(
                f"could not chown the workspace/fixtures to {_REMAP_UID}:{_REMAP_GID} "
                "before starting the id-remapped agent container; running on would "
                "hand an unprivileged agent a workspace it cannot write, so refusing "
                "rather than produce a misleading result"
            ) from exc

    def _chown_after_remap(self) -> None:
        """Chown the workspace and fixtures back to the real host uid/gid after
        the agent container exits.

        Best-effort and never raises: a failure here must not mask a real agent
        result, but it does leave the artifacts owned by the remap id rather
        than the operator, who then cannot read or delete them without root. So
        it is logged as an error carrying the exact repair command to run by
        hand.
        """
        uid, gid = os.getuid(), os.getgid()
        argv = self._chown_argv(uid, gid)
        try:
            run(argv, check=True)
        except SubprocessError:
            _log.error(
                "could not chown the workspace/fixtures back to %s:%s after the "
                "id-remapped agent container exited; the artifacts are left owned "
                "by the remap id and the operator cannot read or delete them "
                "without root. Repair manually: %s",
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

        Flag by flag: ``--rm`` (clean exit leaves nothing) with a
        deterministic ``--name`` (unclean exit is reap-able);
        ``--cap-drop=ALL`` + ``no-new-privileges`` (nothing in the image
        needs a capability; contains the root-running macOS case);
        the network plan; ``host.docker.internal:host-gateway`` (loopback
        endpoints resolve on Linux); ``--user`` on Linux (workspace files
        stay operator-owned); workspace RW, kubeconfig RO, fixtures RW
        (tasks commit fixes back); overlay env as name-only ``-e`` (values
        ride the client env, never the world-readable argv); container-owned
        ``HOME``/``KUBECONFIG`` inline and last (non-secret constants,
        last ``-e`` wins); no ``-i`` (a headless run never reads stdin).
        """
        spec = self.spec
        argv: list[str] = ["docker", "run", "--rm", "--name", self.container_name]
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

        ``env`` is rejected (a full environment is the credential-inheritance
        channel the sandbox removes) and so is ``input`` (the container runs
        without stdin). Docker's own failures — a missing binary, or the
        daemon-reserved exit 125 (image or network absent) — raise
        :class:`SandboxError` instead of masquerading as an agent exit code.
        The container is reaped by name on every exit path, because ``--rm``
        does not fire when the docker client is killed by the host-side
        timeout.

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
        if input is not None:
            raise SandboxError(
                "the sandboxed agent runs without stdin (no -i, by design); input= is unsupported"
            )
        # Filter once: the same mapping supplies the name-only -e flags and
        # the values handed to the docker client process (its /proc environ
        # is same-uid-readable, unlike its cmdline).
        crossing = filter_boundary_env(extra_env, self.spec.env_allowlist)
        wrapped = self.wrap_argv(cmd, cwd=cwd, extra_env=crossing)
        remap = sys.platform.startswith("linux") and self._needs_id_remap()
        if remap:
            self._chown_before_remap()
        try:
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
            # 125 is the docker daemon's own failure code (missing image,
            # missing network); with check=False it would otherwise be
            # scored as the agent exiting 125.
            if completed.returncode == 125:
                raise SandboxError(
                    f"docker could not start the sandbox container: {completed.stderr}"
                )
            return completed
        finally:
            try:
                kill_container(self.container_name)
            finally:
                if remap:
                    self._chown_after_remap()


def container_name_for_workspace(workspace: Path) -> str:
    """Deterministic container name tied 1:1 to the run's workspace directory.

    An optional ``BENCH_AGENT_SANDBOX_OWNER`` segment scopes the name to one
    attempt so parallel harnesses can sweep only their own strays.
    """
    owner = os.environ.get("BENCH_AGENT_SANDBOX_OWNER", "")
    if owner and not re.fullmatch(r"[A-Za-z0-9_]{1,128}", owner):
        raise ValueError("BENCH_AGENT_SANDBOX_OWNER must be a unique alphanumeric attempt ID")
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
        result = run(["docker", "kill", name], check=False, timeout=_HOUSEKEEPING_TIMEOUT_SEC)
    except (OSError, SubprocessError):
        _log.warning("could not reap sandbox container %s", name, exc_info=True)
        return
    if result.returncode == 0:
        _log.info("reaped sandbox container %s", name)


def sweep_stray_containers() -> None:
    """Reap only the current attempt's leftovers; best-effort against docker failures.

    A shared name prefix is not proof another process has exited: without an
    owner id (``BENCH_AGENT_SANDBOX_OWNER``) a stray cannot be told apart from
    a parallel harness's live container, so unscoped containers are left for
    explicit operator recovery rather than swept here.
    """
    owner = os.environ.get("BENCH_AGENT_SANDBOX_OWNER", "")
    if not owner:
        return
    if not re.fullmatch(r"[A-Za-z0-9_]{1,128}", owner):
        raise ValueError("BENCH_AGENT_SANDBOX_OWNER must be a unique alphanumeric attempt ID")
    prefix = f"{_CONTAINER_NAME_PREFIX}{owner}-"
    try:
        listed = run(
            ["docker", "ps", "--format", "{{.Names}}"],
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
