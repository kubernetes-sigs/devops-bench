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

WHY THIS EXISTS. CLI agent harnesses invoke the agent binary as a plain
subprocess inheriting the operator's filesystem and environment, so the agent
can reach the benchmark's own material: ``tasks/**/task.yaml`` (the judge
rubric and ``verification_spec`` — the answer key), scoring and detection
code, prior results, the operator's cloud credentials and admin kubeconfig.
The detection layer is a tripwire, not a boundary; this module is the
boundary. Two observed incidents shaped it: an agent that used the admin
kubeconfig to run a privileged pod and read the bench checkout through the
node's host disk, and an agent that mined the harness VM's cloud identity
from the link-local metadata endpoint when its model credential was missing.

The container sees exactly four things, and what is ABSENT matters more than
what is present:

* the per-run **workspace** at ``/workspace``, with ``HOME`` repointed to
  ``/workspace/home`` so a bare ``~`` cannot resolve to the operator's profile
* the task's own seeded **fixtures**, mounted read-write under the container
  home (see :func:`discover_fixture_mounts`) — task input, not answer material
* a generated single-cluster **kubeconfig**, read-only at ``/creds/kubeconfig``:
  one cluster, one context, no ``exec:`` plugin blocks, never the operator's
  own kubeconfig or Application Default Credentials
* an explicit **env overlay**, passed by value as ``-e`` flags after a deny
  filter — never scraped from ``os.environ``

Never in the container: the repo checkout, ``results/``, operator ``$HOME``,
gcloud config, Terraform state, or the Docker socket (with the socket the
boundary would be decorative, and it is tempting precisely because a kind
cluster is Docker-hosted).

Completeness is a security property: an under-provisioned agent improvises
rather than giving up (observed: a missing fixture mount escalated to a
privileged pod reading the bench checkout through the node's host disk), so
refusal here is always loud — a sandbox that cannot be built raises
:class:`~devops_bench.core.errors.SandboxError` instead of quietly running
the agent on the host.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from devops_bench.core import ClusterInfo, NetworkPlan, get_env, get_logger
from devops_bench.core.errors import SandboxError
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
    "discover_fixture_mounts",
    "filter_boundary_env",
    "container_name_for_workspace",
    "kill_container",
    "sweep_stray_containers",
]

_log = get_logger("agents.sandbox")

# Opt-in switch and image selector. Opt-in rather than default so sandboxed
# runs can be A/B'd against the current ambient behaviour; with the switch
# unset the harness must behave byte-for-byte as before this module existed.
SANDBOX_ENV = "BENCH_AGENT_SANDBOX"
IMAGE_ENV = "BENCH_SANDBOX_IMAGE"
_SANDBOX_ENABLED_VALUES = frozenset({"docker", "1", "true"})
# Values that mean "off" on purpose. Anything outside both sets raises: an
# operator who typed ``yes`` believes the run is contained, and silently
# running ambient is exactly the quiet degrade this module promises never to
# do — the matrix runner also forwards the raw value to detached runners, so
# one typo would otherwise become a whole unsandboxed matrix.
_SANDBOX_DISABLED_VALUES = frozenset({"", "0", "false", "no", "off"})

# Env override naming this run's fixtures explicitly, as ``:``-separated host
# paths. Set it for a stack whose fixture name does not carry the cluster
# token that :func:`discover_fixture_mounts` keys off.
FIXTURES_ENV = "BENCH_AGENT_FIXTURES"

# Container-side geography. HOME lives *under* the workspace so everything the
# agent writes lands in the one host directory the harness already diffs and
# collects, while a prompt's ``~/<name>`` resolves somewhere the harness
# controls. The kubeconfig lives outside the workspace mount so the read-only
# bind is the only path to it.
CONTAINER_WORKSPACE = "/workspace"
CONTAINER_HOME = f"{CONTAINER_WORKSPACE}/home"
CONTAINER_KUBECONFIG = "/creds/kubeconfig"

# Every sandboxed container this harness starts carries this name prefix,
# followed by its run workspace's own directory name (see
# ``container_name_for_workspace``). The prefix is what lets
# ``sweep_stray_containers`` find and reap containers this harness itself
# created, and only those: a name match is the entire authorization to kill
# something, so it must never be able to match a container this harness did
# not start.
_CONTAINER_NAME_PREFIX = "devops-bench-agent-"

# Env vars that never cross the boundary, even when present in the caller's
# resolved overlay. Exact names cover the operator's cloud identity plumbing
# and the two variables the executor itself owns inside the container;
# prefixes cover the benchmark's own configuration (``BENCH_*`` includes this
# module's switches), Terraform state/credential plumbing, and every cloud's
# credential env family — the boundary is vendor-neutral, so the deny list
# must be too, not just the one cloud this benchmark happens to run on today.
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

# Variables the executor itself owns inside the container. Unlike the deny
# list above these are not even allowlistable: docker's last ``-e`` wins, so
# an overlay value crossing for one of these would repoint HOME / KUBECONFIG /
# PATH *inside* the boundary — redirecting the agent's home or credential
# resolution is never a legitimate per-task need.
_CONTAINER_OWNED_ENV = frozenset({"HOME", "KUBECONFIG", "PATH"})

# Apiserver hostnames that mean "this machine" and therefore mean the wrong
# machine once the agent is inside a container. ``0.0.0.0`` is a bind-any
# address rather than a destination, but kubeconfigs in the wild carry it and
# it is no more routable from the container than the others.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})


@dataclass(frozen=True)
class SandboxSpec:
    """Everything the executor needs to wrap one run's agent in ``docker run``.

    Built in two stages: :func:`spec_from_env` yields a skeletal spec (image
    only) that records the operator's opt-in on the
    :class:`~devops_bench.agents.config.AgentConfig`, and the eval harness
    completes it per task — workspace, kubeconfig, network plan, fixture
    mounts only exist after provisioning. :class:`SandboxExecutor` refuses an
    incomplete spec rather than guessing.

    Attributes:
        image: Container image holding the agent CLI (``BENCH_SANDBOX_IMAGE``).
        network: The run's :class:`NetworkPlan`.
        workspace: Host path of the per-run workspace, mounted read-write at
            ``/workspace``.
        kubeconfig: Host path of the generated agent kubeconfig, mounted
            read-only at ``/creds/kubeconfig``.
        fixture_mounts: Host path -> container path of the task's seeded
            fixtures, mounted READ-WRITE (see :func:`discover_fixture_mounts`).
        env_allowlist: Variable names explicitly permitted to cross even when
            they match a deny rule (e.g. a task that legitimately needs one
            ``TF_VAR``). The container-owned ``HOME``/``KUBECONFIG``/``PATH``
            are excepted — never crossable, allowlisted or not. Everything
            else in the caller's overlay crosses unless denied; nothing
            outside the overlay ever crosses.
    """

    image: str = ""
    network: NetworkPlan = field(default_factory=NetworkPlan)
    workspace: Path | None = None
    kubeconfig: Path | None = None
    fixture_mounts: Mapping[str, str] = field(default_factory=dict)
    env_allowlist: tuple[str, ...] = ()


def spec_from_env(env: Mapping[str, str] | None = None) -> SandboxSpec | None:
    """Read the sandbox opt-in from the environment.

    Args:
        env: Optional mapping to read from (defaults to ``os.environ``),
            matching :meth:`AgentConfig.from_env`'s injection seam.

    Returns:
        A skeletal :class:`SandboxSpec` when ``BENCH_AGENT_SANDBOX`` is one of
        ``docker``/``1``/``true``; ``None`` when it is unset or an explicit
        off value (``0``/``false``/``no``/``off``). The image may legitimately
        still be empty here; the executor is where that fails loud.

    Raises:
        SandboxError: On any other value. An unrecognized spelling (``yes``,
            ``podman``) must not silently run the agent ambient while the
            operator believes it is contained.
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

    Two stages, and the split is what keeps per-provider knowledge out of this
    module. First the provider answers
    :meth:`~devops_bench.providers.base.Provider.sandbox_network_plan` with
    whatever only it can know — a Docker network to join, an in-network
    hostname, the name of the context it wrote. Then this function applies the
    one rewrite that needs no provider knowledge at all: a server on host
    loopback is meaningless from inside a container (it resolves to the
    container), so it is remapped to ``host.docker.internal``, which
    :meth:`SandboxExecutor.wrap_argv` always maps to the host gateway.

    That generic step is why most providers need no override. A cloud endpoint
    routes already and is left alone; a locally-published one is rewritten
    whether it belongs to kind, a vcluster on a laptop, or something not
    written yet.

    Args:
        provider: The run's provider, or ``None`` when the deployer has none
            (the no-op deployer). ``None`` yields the default plan, built
            against the ambient current-context — acceptable only because such
            a run has no cluster identity of its own to pin to.
        cluster_info: The provisioned cluster the container must reach.

    Returns:
        The plan for this run, with any loopback server already rewritten.

    Raises:
        SandboxError: When the provider names a context kubectl does not know
            (a kubeconfig that never saw this cluster), or when no server URL
            can be read for a plan that does not supply its own. Refusing
            beats running against a server the container cannot reach — or
            handing it credentials for a different cluster entirely.
    """
    plan = provider.sandbox_network_plan(cluster_info) if provider is not None else NetworkPlan()
    if plan.kubectl_context:
        known = (
            run(["kubectl", "config", "get-contexts", "-o", "name"], check=False).stdout or ""
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

    ``127.0.0.1`` inside a container is the container, so a kubeconfig
    published on loopback silently points the agent at nothing. Docker exposes
    the host as ``host.docker.internal`` (natively on Docker Desktop, via the
    ``--add-host`` :meth:`SandboxExecutor.wrap_argv` always passes on Linux),
    so the port still reaches the same listener.

    The apiserver certificate will not carry that name, so ``tls-server-name``
    is set to ``localhost`` — the SAN a loopback-published cluster does have.
    That keeps TLS verified rather than disabled: the container still checks
    the certificate chain and the name, just against the name the certificate
    was actually issued for.

    A plan that already carries a ``rewrite_server`` is returned untouched: the
    provider knew better (kind's in-network control-plane name verifies with
    no override at all).
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
    _log.info(
        "cluster apiserver is published on loopback (%s); the container will reach it "
        "at host.docker.internal%s",
        server,
        port,
    )
    return replace(
        plan,
        rewrite_server=f"https://host.docker.internal{port}",
        tls_server_name=plan.tls_server_name or "localhost",
    )


def discover_fixture_mounts(cluster_name: str | None) -> dict[str, str]:
    """Find this run's seeded task fixtures and map them into the container.

    A task's stack seeds its inputs next to the operator's home — a GitOps
    repo (``~/opa-repo-<cluster>.git``), a delivered advisory, a rightsizing
    report — and the prompt then points the agent at ``~/<name>``. The
    container repoints ``HOME`` and mounts neither the real home nor the
    repository, so without this the agent is told to read a file that cannot
    exist for it. That is not containment, it is a broken task: the fixture is
    task INPUT, not answer material.

    It is also a containment problem in its own right (the first observed
    incident in the proposal doc): agents hunt the filesystem for the missing
    fixture, and the ones that hunt hardest escalate. Giving the agent the
    input it was promised removes the reason to go looking.

    Eligibility is deliberately narrow: only paths whose NAME carries the
    run-unique ``cluster_name`` token match, and only at the top level of the
    home directory. So this can surface artifacts this run's own stack created
    and nothing else — not the operator's unrelated files, and not a
    concurrent run's fixtures. ``BENCH_AGENT_FIXTURES`` overrides the search
    for a stack that names its fixtures some other way.

    Args:
        cluster_name: The run's cluster name, used as the discriminating token.

    Returns:
        Host path -> container path (under ``/workspace/home``, so a prompt's
        ``~/<name>`` resolves to exactly the mounted fixture). Empty when
        nothing matches, the normal case for tasks that seed no files.

    Raises:
        SandboxError: When two declared fixtures share a basename and would
            collide on one container mount point.
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
        # Top level only, and the name must carry the token. A recursive walk
        # would widen this well past "artifacts of this run".
        candidates = sorted(home.glob(f"*{cluster_name}*"))

    mounts: dict[str, str] = {}
    dest_owner: dict[str, str] = {}
    for path in candidates:
        if not path.exists():
            _log.warning("declared fixture %s does not exist; not mounting it", path)
            continue
        host_path = str(path.resolve())
        container_path = f"{CONTAINER_HOME}/{path.name}"
        # Two host paths sharing a basename (only reachable through the
        # explicit override; glob candidates share one directory) would emit
        # two ``-v`` flags with the same destination, which docker aborts on
        # with a cryptic "Duplicate mount point". Refuse with the actual paths.
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


def _env_denied(name: str) -> bool:
    return name in _DENIED_ENV_NAMES or name.startswith(_DENIED_ENV_PREFIXES)


def filter_boundary_env(
    overlay: Mapping[str, str] | None, allowlist: Sequence[str] = ()
) -> dict[str, str]:
    """Filter a resolved env overlay down to what may cross the boundary.

    Only the caller's overlay is ever considered — this function is the single
    place boundary env is decided, and it deliberately has no access to
    ``os.environ``: scraping the process environment for well-known credential
    names would reinstate "inherit whatever the operator happened to export",
    the exact behaviour the sandbox removes. Within the overlay, names
    matching the deny rules (operator cloud identity, ``BENCH_*``/``TF_*``)
    are dropped with a warning unless explicitly allowlisted — except the
    container-owned ``HOME``/``KUBECONFIG``/``PATH``, which never cross, not
    even allowlisted: docker's last ``-e`` wins, so a crossing value would
    override the executor's own inside the container.

    Args:
        overlay: The resolved per-run env overlay (provider-routed API key,
            model selection, agent toggles), or ``None``.
        allowlist: Names permitted to cross despite matching a deny rule
            (container-owned names excepted).

    Returns:
        The filtered mapping that becomes ``-e`` flags.
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
    harness swaps a direct subprocess call for
    ``AgentHarness.run_agent_cmd(...)`` and everything else — return shape,
    ``check`` semantics, timeout behaviour — stays the same.

    One executor serves one run: the container name is derived from the
    workspace directory name, so a reaper can find a stray container purely
    from its name (see :func:`sweep_stray_containers`).
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
        """Map a host path under the workspace to its container-side path.

        Anything outside the workspace raises: the alternative would be to
        grow the mount set to make the path exist, and the mount set is the
        boundary — it only ever widens through an explicit spec field, never
        as a side effect of a call site's ``cwd``.
        """
        resolved = Path(path).resolve()
        workspace = self._workspace.resolve()
        if resolved == workspace:
            return CONTAINER_WORKSPACE
        try:
            relative = resolved.relative_to(workspace)
        except ValueError as exc:
            raise SandboxError(
                f"host path {resolved} is outside the sandbox workspace {workspace} "
                "and has no container mapping; refusing to widen the mount set"
            ) from exc
        return f"{CONTAINER_WORKSPACE}/{relative.as_posix()}"

    def wrap_argv(
        self,
        cmd: Sequence[str | os.PathLike[str]],
        *,
        cwd: str | os.PathLike[str] | None = None,
        extra_env: Mapping[str, str] | None = None,
    ) -> list[str]:
        """Wrap an agent command line in ``docker run``.

        Boundary decisions encoded here, in order of appearance: ``--rm`` so a
        cleanly-exiting container leaves nothing behind; a deterministic
        ``--name`` so an unclean one can be reaped; ``--cap-drop=ALL`` with
        ``no-new-privileges`` because nothing in the image needs a capability
        (node, kubectl, helm, git are plain userland) and a setuid binary in
        the base image must not re-escalate — this is also what contains the
        macOS case, where no ``--user`` is emitted and the process runs as
        root; the network plan;
        ``host.docker.internal:host-gateway`` always, so loopback-published
        endpoints resolve on Linux the way Docker Desktop resolves them
        natively; ``--user`` on Linux only, so workspace files stay
        operator-owned (Docker Desktop already remaps ownership on macOS); the
        four-mount set (workspace RW, kubeconfig RO, fixtures RW — the write
        bit is deliberate, several tasks ask the agent to commit its fix back
        to the seeded repo); the filtered env overlay by value, then the
        container-owned ``HOME``/``KUBECONFIG`` last so they win any
        duplicate ``-e``; and **no ``-i``** — keeping stdin open gives the
        agent an open, non-TTY stdin to block on, and a headless prompt run
        never reads it.
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
            argv += ["--user", f"{os.getuid()}:{os.getgid()}"]
        argv += ["-v", f"{spec.workspace}:{CONTAINER_WORKSPACE}"]
        argv += ["-v", f"{spec.kubeconfig}:{CONTAINER_KUBECONFIG}:ro"]
        for host_path, container_path in spec.fixture_mounts.items():
            argv += ["-v", f"{host_path}:{container_path}"]
        for name, value in filter_boundary_env(extra_env, spec.env_allowlist).items():
            argv += ["-e", f"{name}={value}"]
        # Container-owned env comes AFTER the overlay: docker's last ``-e``
        # wins, so even a filter regression could not let an overlay value
        # repoint HOME or the credential path inside the boundary.
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

        Two parameters are rejected rather than reinterpreted, because both
        would silently widen the boundary or lie to the caller:

        * ``env`` replaces the whole child environment in the host ``run``;
          forwarding a full mapping (typically a copy of ``os.environ``) is
          exactly the credential-inheritance channel the sandbox removes.
        * ``input`` needs an open stdin, and the container deliberately runs
          without ``-i``; accepting it would drop the data on the floor.

        The container is reaped by name on every exit path. ``--rm`` only
        removes a container once *the container's own process* exits; a
        ``docker run`` client killed out from under it (exactly what happens
        when the host-side timeout fires) leaves the container running in the
        daemon, silently burning whatever quota the agent inside is still
        spending. The ``finally`` closes that, and killing an already-gone
        container is a harmless no-op.

        Raises:
            SandboxError: On ``env``/``input``, or an unmappable ``cwd``.
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
        wrapped = self.wrap_argv(cmd, cwd=cwd, extra_env=extra_env)
        try:
            return run(wrapped, check=check, capture=capture, text=text, timeout=timeout)
        finally:
            kill_container(self.container_name)


def container_name_for_workspace(workspace: Path) -> str:
    """Deterministic ``docker run --name`` for one run's sandboxed agent.

    Ties the container 1:1 to the run's own workspace directory name (already
    unique per run), so a reaper can find and kill a stray container purely
    from its name, without threading a separate run id through the agent
    harness.
    """
    return f"{_CONTAINER_NAME_PREFIX}{workspace.name}"


def kill_container(name: str) -> None:
    """Best-effort ``docker kill`` by name. Never raises.

    A container that is already gone (the common case, when the agent exited
    cleanly and ``--rm`` already reaped it) fails harmlessly.
    """
    result = run(["docker", "kill", name], check=False)
    if result.returncode == 0:
        _log.info("reaped sandbox container %s", name)


def sweep_stray_containers() -> None:
    """Best-effort reap of containers this harness left running from a prior run.

    Intended to run once at harness start (before any run's own container
    exists) so a container orphaned by a prior crash or a killed harness
    process gets cleaned up before it burns any more quota. Matches
    exclusively on this benchmark's own name prefix, so it can never reap a
    container some other tool created — but the prefix is shared across
    benchmark processes, so this assumes it is the only harness on the host:
    a *sibling* harness's live agent container matches too. Callers running
    parallel harnesses must not sweep (see the eval harness's
    ``BENCH_PARALLEL`` gate).
    """
    listed = run(
        ["docker", "ps", "-q", "--filter", f"name=^{_CONTAINER_NAME_PREFIX}"],
        check=False,
    )
    if listed.returncode != 0:
        return
    for container_id in (listed.stdout or "").split():
        result = run(["docker", "kill", container_id], check=False)
        if result.returncode == 0:
            _log.warning(
                "reaped stray sandbox container %s left running from a prior run", container_id
            )
