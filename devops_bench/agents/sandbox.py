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

import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from devops_bench.core import get_env, get_logger
from devops_bench.core.errors import SandboxError, SubprocessError
from devops_bench.core.subprocess import CompletedProcess, run

__all__ = [
    "NetworkPlan",
    "SandboxSpec",
    "SandboxExecutor",
    "spec_from_env",
    "current_cluster_name",
    "build_network_plan",
    "build_agent_kubeconfig",
    "discover_fixture_mounts",
    "filter_boundary_env",
    "container_name_for_workspace",
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

# Bound on the module's own docker/kubectl housekeeping calls, so a wedged
# daemon cannot hang a reap (and with it the whole batch).
_HOUSEKEEPING_TIMEOUT_SEC = 30

# The running benchmark's own tree (…/devops_bench/agents/sandbox.py -> repo
# root). Mounting it would hand the agent the answer material no token rule
# can reliably exclude (a cluster named "bench" makes ~/devops-bench a
# legitimate token match), so fixture discovery refuses it by path, not name.
_BENCH_REPO_ROOT = Path(__file__).resolve().parents[2]


def _overlaps_bench_checkout(path: Path) -> bool:
    resolved = path.resolve()
    return resolved.is_relative_to(_BENCH_REPO_ROOT) or _BENCH_REPO_ROOT.is_relative_to(resolved)


@dataclass(frozen=True)
class NetworkPlan:
    """How the container reaches this run's cluster apiserver.

    Attributes:
        docker_network: Docker network to join; ``None`` = default bridge.
        extra_hosts: Additional ``--add-host`` entries (``host:ip``).
        rewrite_server: Replacement apiserver URL for the generated
            kubeconfig; ``None`` keeps the context's own server.
        tls_server_name: ``tls-server-name`` for a rewritten endpoint whose
            certificate carries a different SAN.
        kubectl_context: Context every credential read is pinned to, so an
            ambient current-context switch cannot hand the container another
            cluster's credential. ``None`` = ambient current-context.
    """

    docker_network: str | None = None
    extra_hosts: tuple[str, ...] = ()
    rewrite_server: str | None = None
    tls_server_name: str | None = None
    kubectl_context: str | None = None


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


def current_cluster_name() -> str | None:
    """Cluster name from the active ``kind-<cluster>`` context, else ``None``."""
    ctx = (
        run(
            ["kubectl", "config", "current-context"],
            check=False,
            timeout=_HOUSEKEEPING_TIMEOUT_SEC,
        ).stdout
        or ""
    )
    ctx = ctx.strip()
    if not ctx.startswith("kind-"):
        return None
    return ctx[len("kind-") :]


def build_network_plan(cluster_name: str | None = None) -> NetworkPlan:
    """Build the plan for this run's cluster. kind only, for now.

    kind writes ``https://127.0.0.1:<port>`` as the server, meaningless
    in-container; joining the ``kind`` docker network reaches the apiserver
    at ``https://<cluster>-control-plane:6443`` (TLS verifies via the
    node-name SAN). The plan is pinned to ``kind-<cluster_name>``, never the
    ambient current-context. Raises :class:`SandboxError` when no name
    resolves or kubectl knows no such context.
    """
    cluster = cluster_name or current_cluster_name()
    if cluster is None:
        raise SandboxError(
            "the active kubectl context is not a kind context; the sandbox currently "
            "only knows how to reach kind clusters (the per-provider network plan "
            "hook arrives with the credential-scoping follow-up)"
        )
    context = f"kind-{cluster}"
    known = (
        run(
            ["kubectl", "config", "get-contexts", "-o", "name"],
            check=False,
            timeout=_HOUSEKEEPING_TIMEOUT_SEC,
        ).stdout
        or ""
    ).split()
    if context not in known:
        raise SandboxError(
            f"kubectl has no {context!r} context for this run's cluster {cluster!r}; "
            "either the cluster is not a kind cluster (the per-provider plan hook "
            "arrives with the credential-scoping follow-up) or this kubeconfig "
            "never saw it — refusing to build a plan from the ambient context"
        )
    return NetworkPlan(
        docker_network="kind",
        rewrite_server=f"https://{cluster}-control-plane:6443",
        kubectl_context=context,
    )


def _kubectl_config_value(jsonpath: str, context: str | None = None) -> str:
    """One kubectl config value, pinned to ``context`` when given; empty if absent."""
    argv = ["kubectl", "config", "view", "--raw", "--minify"]
    if context:
        argv += ["--context", context]
    argv += ["-o", f"jsonpath={jsonpath}"]
    completed = run(argv, check=False, timeout=_HOUSEKEEPING_TIMEOUT_SEC)
    return (completed.stdout or "").strip()


def build_agent_kubeconfig(plan: NetworkPlan, dest_dir: Path) -> Path:
    """Write the single-cluster kubeconfig the container gets; return its path.

    One cluster, one user, one context, no ``exec:`` plugin blocks. The
    credential is the operator's client certificate — cluster-admin, a
    loudly-logged interim until the credential-scoping follow-up ships
    ServiceAccount tokens. ``dest_dir`` must stay outside the workspace so
    the read-only bind is the only path to the file. Raises
    :class:`SandboxError` when the context carries no CA or no static client
    certificate.
    """
    ctx = plan.kubectl_context
    ca = _kubectl_config_value("{.clusters[0].cluster.certificate-authority-data}", context=ctx)
    if not ca:
        raise SandboxError("could not read the cluster CA from the run's kubectl context")

    server = plan.rewrite_server or _kubectl_config_value(
        "{.clusters[0].cluster.server}", context=ctx
    )
    if not server:
        raise SandboxError("could not read the cluster server URL from the run's kubectl context")

    cert = _kubectl_config_value("{.users[0].user.client-certificate-data}", context=ctx)
    key = _kubectl_config_value("{.users[0].user.client-key-data}", context=ctx)
    if not (cert and key):
        raise SandboxError(
            "the run's kubectl context carries no static client certificate; "
            "exec-credential-plugin contexts are handled by the credential-scoping "
            "follow-up, not by reusing the operator's plugin inside the container"
        )
    _log.warning(
        "sandbox kubeconfig reuses the operator's admin client certificate: the "
        "container boundary is doing all the work and the RBAC boundary none. "
        "Scoped ServiceAccount credentials arrive with the credential-scoping "
        "follow-up."
    )

    cluster_fields = f"server: {server}, certificate-authority-data: {ca}"
    if plan.tls_server_name:
        cluster_fields += f", tls-server-name: {plan.tls_server_name}"
    path = dest_dir / "kubeconfig"
    path.write_text(
        "apiVersion: v1\n"
        "kind: Config\n"
        f"clusters: [{{name: c, cluster: {{{cluster_fields}}}}}]\n"
        f"users: [{{name: u, user: {{client-certificate-data: {cert}, client-key-data: {key}}}}}]\n"
        "contexts: [{name: ctx, context: {cluster: c, user: u}}]\n"
        "current-context: ctx\n"
    )
    path.chmod(0o600)
    return path


def discover_fixture_mounts(cluster_name: str | None) -> dict[str, str]:
    """Find this run's seeded task fixtures and map them into the container.

    Task stacks seed inputs in the operator's home (``~/opa-repo-<cluster>.git``)
    and the prompt points the agent at ``~/<name>``; the container mounts
    neither the real home nor the repo, so without this the task is broken —
    and an under-provisioned agent hunts the filesystem instead of giving up.
    Only top-level entries whose name carries ``cluster_name`` as a
    ``-``/``_``/``.``-delimited token match (dot-entries excluded), so a short
    or reused name cannot sweep in the operator's unrelated files.
    ``BENCH_AGENT_FIXTURES`` overrides the search. The benchmark's own
    checkout is refused by path regardless of name. Raises
    :class:`SandboxError` on a container-path collision or an explicit
    fixture overlapping the checkout.
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
        """Map a workspace-relative host path into the container; raise outside it.

        The mount set is the boundary; it only widens through an explicit
        spec field, never as a side effect of a call site's ``cwd``.
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
            argv += ["--user", f"{os.getuid()}:{os.getgid()}"]
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
            except SubprocessError as exc:
                # check=True raises before the returncode test below runs.
                if exc.returncode == 125:
                    raise SandboxError(
                        f"docker could not start the sandbox container: {exc.stderr}"
                    ) from exc
                raise
            # 125 is the docker daemon's own failure code (missing image,
            # missing network); with check=False it would otherwise be
            # scored as the agent exiting 125.
            if completed.returncode == 125:
                raise SandboxError(
                    f"docker could not start the sandbox container: {completed.stderr}"
                )
            return completed
        finally:
            kill_container(self.container_name)


def container_name_for_workspace(workspace: Path) -> str:
    """Deterministic container name tied 1:1 to the run's workspace directory."""
    return f"{_CONTAINER_NAME_PREFIX}{workspace.name}"


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
    """Best-effort reap of containers a prior crashed run left behind. Never raises.

    Matches only this benchmark's name prefix — but that prefix is shared
    across harness processes, so parallel harnesses must not sweep (see the
    eval harness's ``BENCH_PARALLEL`` gate).
    """
    try:
        listed = run(
            ["docker", "ps", "-q", "--filter", f"name=^{_CONTAINER_NAME_PREFIX}"],
            check=False,
            timeout=_HOUSEKEEPING_TIMEOUT_SEC,
        )
    except (OSError, SubprocessError):
        _log.warning("could not list stray sandbox containers", exc_info=True)
        return
    if listed.returncode != 0:
        return
    for container_id in (listed.stdout or "").split():
        kill_container(container_id)
