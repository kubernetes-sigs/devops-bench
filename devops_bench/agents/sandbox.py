# Copyright 2026 Google LLC
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
"""Run a CLI agent inside a container with a scoped cluster identity.

WHY THIS EXISTS. The CLI agent harnesses invoke the agent binary as a plain
subprocess inheriting the parent environment, so ``run_shell_command`` under
``--approval-mode yolo`` has the operator's entire filesystem. Observed in real
runs: an agent read another task's ``seed.sh`` and ``verify.sh``, searched the
home directory for its own fixtures by namespace name, and copied an unrelated
archive out of ``~/Downloads`` before running ``rm -rf``. The agent's own
workspace sandbox does not help, because it only guards the native file tools
and not the shell.

Two boundaries, and they solve different problems:

* The CONTAINER removes the host filesystem and the operator's environment. That
  closes the on-disk answer-key channel and the safety hazard.
* The SCOPED TOKEN decides what the agent may do to the cluster. That is task
  design, not containment: it makes the agent's identity part of the topology.

Neither closes the third channel. An agent that can read a cluster can read
anything a task put IN that cluster, and a task that needs the agent to inspect a
workload cannot use RBAC to hide that workload's own definition. Answer material
must not be seeded into the cluster in the first place; see the factory's
``answer-leakage.md``.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from devops_bench.core import get_logger
from devops_bench.core.subprocess import run

__all__ = ["sandbox_enabled", "build_agent_kubeconfig", "wrap_argv"]

_log = get_logger("agents.sandbox")

# The ServiceAccount a task may seed to declare what its agent is allowed to do.
# Absent, the agent falls back to the operator's own context, which is what the
# harness did unconditionally before this module existed.
AGENT_SA_NAME = os.environ.get("BENCH_AGENT_SA", "bench-agent")
AGENT_SA_NAMESPACE = os.environ.get("BENCH_AGENT_SA_NAMESPACE", "bench-system")
TOKEN_DURATION = os.environ.get("BENCH_AGENT_TOKEN_DURATION", "2h")


def sandbox_enabled() -> bool:
    """True when the agent should run containerised.

    Opt-in rather than default so it can be A/B'd against the current behaviour
    while tasks are still being debugged.
    """
    return os.environ.get("BENCH_AGENT_SANDBOX", "").strip().lower() in {"docker", "1", "true"}


def current_cluster_name() -> str | None:
    """Derive the kind cluster name from the active kubectl context.

    The agent harness is handed a workspace and a prompt, never the cluster name,
    so rather than widen that interface we recover it from the context kind wrote:
    ``kind-<cluster>``. That is also exactly the prefix of the control-plane
    container name the container needs to reach, so the two stay consistent by
    construction. Returns None for a non-kind context, which is the signal that
    this sandbox's networking assumptions do not apply.
    """
    ctx = run(["kubectl", "config", "current-context"], check=False).stdout or ""
    ctx = ctx.strip()
    if not ctx.startswith("kind-"):
        _log.error("context %r is not a kind context; the docker-network sandbox assumes kind", ctx)
        return None
    return ctx[len("kind-") :]


def _kubectl_json(*args: str) -> dict:
    completed = run(["kubectl", *args, "-o", "json"], check=False)
    if completed.returncode != 0:
        return {}
    try:
        return json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def build_agent_kubeconfig(cluster_name: str, dest_dir: Path) -> Path | None:
    """Write a kubeconfig the container can use, and return its path.

    Two things have to change from the operator's own kubeconfig:

    1. The SERVER. ``kind`` writes ``https://127.0.0.1:<port>``, which means
       nothing inside a container. ``kind`` also creates a Docker network named
       ``kind``, so a container joined to it reaches the API server at
       ``https://<cluster>-control-plane:6443``. The API server certificate
       covers the control-plane node name, so TLS still verifies and no
       ``--insecure-skip-tls-verify`` is needed.
    2. The CREDENTIAL. The operator's context authenticates with an admin client
       certificate. If the task seeded an agent ServiceAccount we mint a short
       lived token for it instead, so the agent holds exactly the permissions the
       task chose to give it. If it did not, we fall back to the admin
       credential and say so loudly, because that is a strictly larger grant than
       most tasks intend.

    Returns None when no kubeconfig could be built, in which case the caller
    should refuse to run rather than silently fall back to the host.
    """
    ca = run(
        [
            "kubectl",
            "config",
            "view",
            "--raw",
            "--minify",
            "-o",
            "jsonpath={.clusters[0].cluster.certificate-authority-data}",
        ],
        check=False,
    ).stdout
    if not ca:
        _log.error("could not read cluster CA from the current context; refusing to sandbox")
        return None

    server = f"https://{cluster_name}-control-plane:6443"

    sa_exists = (
        run(
            ["kubectl", "-n", AGENT_SA_NAMESPACE, "get", "sa", AGENT_SA_NAME],
            check=False,
        ).returncode
        == 0
    )

    if sa_exists:
        token = run(
            [
                "kubectl",
                "-n",
                AGENT_SA_NAMESPACE,
                "create",
                "token",
                AGENT_SA_NAME,
                f"--duration={TOKEN_DURATION}",
            ],
            check=False,
        ).stdout
        if not token:
            _log.error("ServiceAccount %s/%s exists but token minting failed", AGENT_SA_NAMESPACE, AGENT_SA_NAME)
            return None
        user_block = f"user: {{token: {token.strip()}}}"
        _log.info(
            "agent identity: ServiceAccount %s/%s (scoped by the task)",
            AGENT_SA_NAMESPACE,
            AGENT_SA_NAME,
        )
    else:
        # No task-declared identity. Reuse the operator's client cert. This is
        # cluster-admin on kind, so the container boundary is doing all the work
        # and the RBAC boundary is doing none.
        cert = run(
            ["kubectl", "config", "view", "--raw", "--minify", "-o",
             "jsonpath={.users[0].user.client-certificate-data}"],
            check=False,
        ).stdout
        key = run(
            ["kubectl", "config", "view", "--raw", "--minify", "-o",
             "jsonpath={.users[0].user.client-key-data}"],
            check=False,
        ).stdout
        if not (cert and key):
            _log.error("no agent ServiceAccount and no client cert in the current context")
            return None
        user_block = f"user: {{client-certificate-data: {cert}, client-key-data: {key}}}"
        _log.warning(
            "no ServiceAccount %s/%s: agent runs with the operator's admin credential. "
            "Seed one in the task's stack to scope it.",
            AGENT_SA_NAMESPACE,
            AGENT_SA_NAME,
        )

    path = dest_dir / "kubeconfig"
    path.write_text(
        "apiVersion: v1\n"
        "kind: Config\n"
        f"clusters: [{{name: c, cluster: {{server: {server}, certificate-authority-data: {ca}}}}}]\n"
        f"users: [{{name: u, {user_block}}}]\n"
        "contexts: [{name: ctx, context: {cluster: c, user: u}}]\n"
        "current-context: ctx\n"
    )
    path.chmod(0o600)
    return path


def wrap_argv(
    argv: list[str],
    *,
    workspace: Path,
    kubeconfig: Path,
    image: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> list[str]:
    """Wrap an agent command line in ``docker run``.

    The mount set is deliberately short, and what is ABSENT matters more than
    what is present:

    * the repository is not mounted, so task definitions, seed scripts and
      scoring rubrics are unreachable
    * ``$HOME`` is not mounted, and HOME is repointed inside the container so a
      bare ``~`` cannot resolve to the operator's profile
    * the Docker socket is not mounted; with it the container boundary would be
      decorative, and it is tempting precisely because the cluster is Docker-hosted
    * Application Default Credentials are not mounted. Model access should use a
      credential scoped to model access; ADC is the operator's whole cloud identity
      and is a larger grant than the filesystem access this wrapper removes.
    """
    image = image or os.environ.get("BENCH_AGENT_IMAGE", "")
    if not image:
        raise ValueError("BENCH_AGENT_IMAGE must name an image containing the agent CLI")

    # Only the caller's resolved overlay crosses the boundary. Deliberately NOT
    # scraping os.environ for well-known credential names: that would reinstate
    # "inherit whatever the operator happened to export", which is the behaviour
    # this wrapper exists to remove. If a credential is not in the resolved
    # config, the agent does not get it.
    env_flags: list[str] = []
    for key, value in (extra_env or {}).items():
        env_flags += ["-e", f"{key}={value}"]

    return [
        # No -i. Keeping stdin open gives the agent an open, non-TTY stdin to block
        # on, and a headless `-p <prompt>` run never reads it. Combined with
        # stdin=DEVNULL in core.subprocess this closes the channel at both ends.
        "docker", "run", "--rm",
        "--network", "kind",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-v", f"{workspace}:/workspace",
        "-v", f"{kubeconfig}:/kubeconfig:ro",
        "-e", "KUBECONFIG=/kubeconfig",
        "-e", "HOME=/workspace",
        "-w", "/workspace",
        *env_flags,
        image,
        *argv,
    ]


def make_workspace() -> Path:
    """A world-writable scratch dir the container's non-root user can write to."""
    path = Path(tempfile.mkdtemp(prefix="devops-bench-sandbox-"))
    path.chmod(0o777)
    return path


def cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
