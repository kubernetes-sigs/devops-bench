#!/usr/bin/env python3
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

"""Boundary probes for the sandboxed agent, run against a live cluster.

A passing task run proves the agent can work inside the sandbox, not that the
boundary holds. This script provisions the real agent credential, then runs
each escape from inside a real sandbox container and checks it is refused.
Controls come first: a dead token would make every deny pass for the wrong reason.

Usage:
    uv run python hack/sandbox_probe.py --provider kind --cluster-name kind \\
        --image <sandbox-image>

The network plan comes from ``agents.sandbox.build_network_plan`` so the
container reaches the apiserver the way a real run does.
``tests/e2e/test_sandbox_boundary.py`` drives this and asserts the exit code;
it stays runnable standalone for the per-probe transcript. Unless ``--keep``
is given, the run tears the boundary down and asserts nothing survived.
"""

from __future__ import annotations

import argparse
import functools
import json
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from devops_bench.agents.sandbox import SandboxExecutor, SandboxSpec, build_network_plan
from devops_bench.core.context import ClusterInfo, NetworkPlan
from devops_bench.core.errors import SandboxError, SubprocessError
from devops_bench.k8s import agent_credentials as creds
from devops_bench.providers.base import Provider
from devops_bench.providers.gcp import GcpProvider
from devops_bench.providers.kind import KindProvider
from devops_bench.providers.vcluster import VClusterProvider

# A file, not stdin: the container runs without -i, so ``apply -f -`` would never reach admission.
_HOSTPATH_POD = """\
apiVersion: v1
kind: Pod
metadata:
  name: bench-probe-hostpath
spec:
  containers:
    - name: c
      image: busybox
      command: ["sleep", "1d"]
      volumeMounts:
        - name: host
          mountPath: /host
  volumes:
    - name: host
      hostPath:
        path: /
"""

_PRIVILEGED_OVERRIDE = json.dumps(
    {
        "spec": {
            "containers": [
                {
                    "name": "c",
                    "image": "busybox",
                    "command": ["sleep", "1d"],
                    "securityContext": {"privileged": True},
                }
            ]
        }
    }
)

_ORDINARY_POD = json.dumps(
    {"spec": {"containers": [{"name": "c", "image": "busybox", "command": ["sleep", "1d"]}]}}
)

# An exempt name the agent tries to claim; it exists on some providers, so cleanup checks first.
_CLAIMED_EXEMPT_NAMESPACE = "gmp-system"

_METADATA_URL = "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token"

# Bounds every host-side kubectl/curl call so a stalled apiserver cannot outlast teardown.
_HOST_CMD_TIMEOUT_SEC = 120

# Labelled and absent from the exempt NAME list, so only the by-label binding can match it.
_MANAGED_NAMESPACE = "bench-probe-managed"

# Two pods built before provisioning that differ only in conformance, so a deny isolates the guard.
_LEGACY_NAMESPACE = "bench-probe-legacy"
_LEGACY_PRIVILEGED_POD = "bench-probe-legacy-priv"
_LEGACY_ORDINARY_POD = "bench-probe-legacy-ok"


@dataclass
class Probe:
    """One boundary check run inside the sandbox container.

    Attributes:
        name: Short label printed in the report.
        why: What a failure of this probe would mean.
        argv: Command line run inside the container.
        expect_denied: True when a non-zero exit is the passing outcome.
        expect_stderr: Substring the refusal must mention, so an unrelated failure is not a pass.
        setup: Host-side precondition re-asserted just before the run; failing it fails the probe.
    """

    name: str
    why: str
    argv: list[str]
    expect_denied: bool = True
    expect_stderr: str = ""
    setup: Callable[[], bool] | None = None


def _running_pod_in_kube_system(context: str) -> str | None:
    """Name a running kube-system pod for the exec probe to aim at.

    Resolved host-side: with a made-up name ``kubectl exec`` fails before admission.
    Returns ``None`` when there is no running pod (skip) and ``""`` when the query failed.
    """
    completed = _kubectl(
        context,
        "get",
        "pods",
        "-n",
        "kube-system",
        "--field-selector=status.phase=Running",
        "-o",
        "jsonpath={.items[*].metadata.name}",
    )
    if completed.returncode != 0:
        print(f"    FAIL setup:exec-target ({completed.stderr.strip()})")
        return ""
    # ``[*]`` prints nothing on an empty list; ``[0]`` would exit non-zero and read as a failed query.
    names = completed.stdout.split()
    return names[0] if names else None


def _ensure_managed_namespace(context: str) -> bool:
    """Make sure the labelled namespace the by-label binding needs is Active.

    Built with ``create`` + ``label`` rather than ``apply``: the addon manager
    prunes labelled namespaces that carry a last-applied annotation, which cost
    the first live run. Called again right before the probe to close the
    remaining window. False means the probe must not run.
    """
    for attempt in range(5):
        phase = _kubectl(
            context, "get", "namespace", _MANAGED_NAMESPACE, "-o", "jsonpath={.status.phase}"
        )
        if phase.returncode == 0 and phase.stdout.strip() == "Active":
            break
        if attempt == 4:
            # The last pass only re-reads the phase, so a create on the pass before still counts.
            return False
        if phase.stdout.strip() == "Terminating":
            # A prune already in flight. Recreating now fails; wait it out.
            time.sleep(5)
            continue
        created = _kubectl(context, "create", "namespace", _MANAGED_NAMESPACE)
        if created.returncode != 0 and "already exists" not in created.stderr:
            print(f"    {created.stderr.strip()}")
            time.sleep(2)

    labelled = _kubectl(
        context,
        "label",
        "namespace",
        _MANAGED_NAMESPACE,
        f"{creds._ADDON_MANAGER_LABEL}=Reconcile",
        "--overwrite",
    )
    if labelled.returncode != 0:
        print(f"    {labelled.stderr.strip()}")
        return False
    return True


def _kubectl(context: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a host-side kubectl pinned to ``context``, never raising."""
    argv = ["kubectl", "--context", context, *args]
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, check=False, timeout=_HOST_CMD_TIMEOUT_SEC
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        # A raise here would skip the teardown; a failed CompletedProcess reads as a FAIL instead.
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr=str(exc))


def _namespace_exists(context: str, name: str) -> bool | None:
    """Whether ``name`` exists; ``None`` when the query itself failed."""
    completed = _kubectl(context, "get", "namespace", name, "--ignore-not-found", "-o", "name")
    return bool(completed.stdout.strip()) if completed.returncode == 0 else None


def _create_legacy_pods(context: str) -> bool:
    """Build the pods that must predate provisioning, and wait until they are Running.

    Before provisioning on purpose: the guard under test exists because admission
    never saw these creates. The pod-security policy is dropped first so a reused
    cluster does not refuse the privileged one; provisioning re-applies it.
    Running, not Pending: exec on a Pending pod fails in the kubelet, which reads as a deny.
    """
    for kind in ("validatingadmissionpolicybinding", "validatingadmissionpolicy"):
        _kubectl(context, "delete", kind, "bench-agent-pod-security", "--ignore-not-found")
    _kubectl(context, "create", "namespace", _LEGACY_NAMESPACE)
    # The default ServiceAccount is minted asynchronously; a pod create that races it is refused.
    for _ in range(60):
        if (
            _kubectl(
                context, "get", "serviceaccount", "default", "-n", _LEGACY_NAMESPACE
            ).returncode
            == 0
        ):
            break
        time.sleep(1)
    else:
        print(f"    no default ServiceAccount appeared in {_LEGACY_NAMESPACE} after 60s")
        return False
    for name, overrides in (
        (_LEGACY_PRIVILEGED_POD, _PRIVILEGED_OVERRIDE),
        (_LEGACY_ORDINARY_POD, _ORDINARY_POD),
    ):
        created = _kubectl(
            context,
            "run",
            name,
            "-n",
            _LEGACY_NAMESPACE,
            "--image=busybox",
            "--restart=Never",
            f"--overrides={overrides}",
        )
        if created.returncode != 0 and "already exists" not in created.stderr:
            print(f"    {created.stderr.strip()}")
            return False

    for name in (_LEGACY_PRIVILEGED_POD, _LEGACY_ORDINARY_POD):
        waited = _kubectl(
            context,
            "wait",
            "--for=condition=Ready",
            f"pod/{name}",
            "-n",
            _LEGACY_NAMESPACE,
            # Under the host bound, so a slow pull is a FAIL here rather than a timeout escaping.
            f"--timeout={_HOST_CMD_TIMEOUT_SEC - 20}s",
        )
        if waited.returncode != 0:
            print(f"    {waited.stderr.strip()}")
            return False
    return True


def _legacy_pod_still_running(context: str, name: str) -> bool:
    """Re-assert a legacy pod is Running without recreating it (the policy is installed by now)."""
    phase = _kubectl(
        context, "get", "pod", name, "-n", _LEGACY_NAMESPACE, "-o", "jsonpath={.status.phase}"
    )
    return phase.returncode == 0 and phase.stdout.strip() == "Running"


def _probes(
    workspace_pod_path: str,
    exec_target: str | None,
    managed_setup: Callable[[], bool] | None,
    legacy_setup: Callable[[str], bool] | None,
) -> list[Probe]:
    """Build the probe list: controls first, then the escapes, then the informational checks.

    ``exec_target``, ``managed_setup`` and ``legacy_setup`` are ``None`` when their
    precondition could not be built, which drops the probes that need it.
    """
    return [
        # -- controls: prove the credential works before trusting any refusal --
        Probe(
            name="control:credential-works",
            why="a dead token makes every deny probe pass for the wrong reason",
            argv=["kubectl", "get", "namespaces", "-o", "name"],
            expect_denied=False,
        ),
        Probe(
            name="control:curl-present",
            why="an image without curl makes the metadata probe below pass "
            "without ever reaching the network",
            argv=["curl", "--version"],
            expect_denied=False,
        ),
        Probe(
            name="control:ordinary-namespace",
            why="the namespace guard must deny reserved names, not all names",
            argv=["kubectl", "create", "namespace", "bench-probe-ok"],
            expect_denied=False,
        ),
        Probe(
            name="control:ordinary-pod",
            why="baseline pods must still run, or the policy is too broad to ship",
            argv=[
                "kubectl",
                "run",
                "bench-probe-ok-pod",
                "-n",
                "bench-probe-ok",
                "--image=busybox",
                "--restart=Never",
                f"--overrides={_ORDINARY_POD}",
            ],
            expect_denied=False,
        ),
        # -- incident 1: privileged pod + hostPath, reading the host disk --
        Probe(
            name="escape:privileged-pod",
            why="observed incident 1; the escape this PR exists to deny",
            argv=[
                "kubectl",
                "run",
                "bench-probe-priv",
                "-n",
                "bench-probe-ok",
                "--image=busybox",
                "--restart=Never",
                f"--overrides={_PRIVILEGED_OVERRIDE}",
            ],
            expect_stderr="privileged",
        ),
        Probe(
            name="escape:hostpath-volume",
            why="the other half of incident 1: mounting the node's filesystem",
            argv=["kubectl", "apply", "-n", "bench-probe-ok", "-f", workspace_pod_path],
            expect_stderr="hostPath",
        ),
        # -- incident 2: the VM service account via the metadata server --
        Probe(
            name="escape:metadata-server",
            why="observed incident 2; mined the bastion VM's service account",
            argv=["curl", "-sS", "-m", "5", "-H", "Metadata-Flavor: Google", _METADATA_URL],
        ),
        # -- the holes the code review found, none of them ever run live --
        Probe(
            name="review:ephemeral-container",
            why="pods/ephemeralcontainers is a distinct subresource; a rule "
            "naming only 'pods' never sees kubectl debug --profile=sysadmin",
            argv=[
                "kubectl",
                "debug",
                "-n",
                "bench-probe-ok",
                "pod/bench-probe-ok-pod",
                "--image=busybox",
                "--profile=sysadmin",
                "--attach=false",
                "-q",
            ],
        ),
        Probe(
            name="review:bench-system-pod",
            why="bench-system was policy-exempt and the agent holds edit "
            "cluster-wide, so the escape was one -n bench-system away",
            argv=[
                "kubectl",
                "run",
                "bench-probe-sys",
                "-n",
                creds.AGENT_NAMESPACE,
                "--image=busybox",
                "--restart=Never",
                f"--overrides={_PRIVILEGED_OVERRIDE}",
            ],
            expect_stderr="privileged",
        ),
        Probe(
            name="review:claim-exempt-namespace",
            why="exemptions are by name and several do not exist on every "
            "provider, so the agent could claim one and deploy there freely",
            argv=["kubectl", "create", "namespace", _CLAIMED_EXEMPT_NAMESPACE],
            expect_stderr="reserved",
        ),
        # -- exempt namespaces that already exist: the trio that found the hole the guard closes --
        Probe(
            name="review:exempt-namespace-pod",
            why="the module asserts every exempt name is one the agent cannot "
            "write to; edit bound cluster-wide made that false until the "
            "exempt-namespace guard, and this is the request that proved it",
            argv=[
                "kubectl",
                "run",
                "bench-probe-exempt",
                "-n",
                "kube-system",
                "--image=busybox",
                "--restart=Never",
                f"--overrides={_PRIVILEGED_OVERRIDE}",
            ],
            expect_stderr="may not run a workload",
        ),
        Probe(
            name="review:exempt-namespace-deployment",
            why="the guard is username-scoped, and a Deployment's pod is made "
            "by the ReplicaSet controller under an identity of its own -- so "
            "the workload object is the only place the agent's name still "
            "appears. Deliberately unprivileged: only the exempt-namespace "
            "guard can deny this, so it cannot pass on the pod policy's back",
            argv=[
                "kubectl",
                "create",
                "deployment",
                "bench-probe-exempt-deploy",
                "-n",
                "kube-system",
                "--image=busybox",
            ],
            expect_stderr="may not run a workload",
        ),
        *(
            [
                Probe(
                    name="review:exempt-namespace-exec",
                    why="denying creates is only half of it: edit grants "
                    "pods/exec, and these are the namespaces whose pods are "
                    "legitimately privileged, so a shell in one of them is "
                    "the same escape by a longer route. exec arrives as "
                    "CONNECT on a subresource, which a CREATE rule never sees",
                    argv=["kubectl", "exec", "-n", "kube-system", exec_target, "--", "true"],
                    expect_stderr="exec into one",
                )
            ]
            if exec_target
            else []
        ),
        Probe(
            name="review:claim-managed-label",
            why="the exemption is by label as well as by name, and a label -- "
            "unlike a name -- can be added to a namespace the agent already "
            "owns, which would exempt everything in it",
            argv=[
                "kubectl",
                "label",
                "namespace",
                "bench-probe-ok",
                "addonmanager.kubernetes.io/mode=Reconcile",
            ],
            expect_stderr="exempt",
        ),
        *(
            [
                Probe(
                    name="review:managed-label-namespace",
                    why="the exemption has two bindings and only one of them "
                    "has ever fired. On GKE every managed namespace is also on "
                    "the exempt NAME list, so by-name matches first and "
                    "by-label -- the half that covers managed namespaces this "
                    "code has never heard of -- is untested. This namespace "
                    "carries the label and is absent from the list, so a deny "
                    "here can only have come from by-label, which is what the "
                    "expected substring asserts. Deliberately unprivileged, "
                    "for the same reason as the Deployment above",
                    argv=[
                        "kubectl",
                        "create",
                        "deployment",
                        "bench-probe-managed-deploy",
                        "-n",
                        _MANAGED_NAMESPACE,
                        "--image=busybox",
                    ],
                    expect_stderr="by-label",
                    setup=managed_setup,
                )
            ]
            if managed_setup
            else []
        ),
        *(
            [
                Probe(
                    name="control:exec-conformant-legacy-pod",
                    why="the deny below must be the shell guard picking one pod "
                    "out, not exec being broken wholesale. This pod predates "
                    "provisioning exactly like the privileged one, sits in the "
                    "same non-exempt namespace and differs only in conformance, "
                    "so the pair isolates the guard and nothing else",
                    argv=[
                        "kubectl",
                        "exec",
                        "-n",
                        _LEGACY_NAMESPACE,
                        _LEGACY_ORDINARY_POD,
                        "--",
                        "true",
                    ],
                    expect_denied=False,
                    setup=functools.partial(legacy_setup, _LEGACY_ORDINARY_POD),
                ),
                Probe(
                    name="review:exec-nonconformant-pod",
                    why="the deployer runs before credentials are provisioned, "
                    "so fixtures like opa-remediation leave privileged pods "
                    "admission never saw and cannot retract. edit grants "
                    "pods/exec cluster-wide, and this namespace is not exempt, "
                    "so a shell into one of those is node root by a route the "
                    "pod-security policy is blind to. Admission cannot read the "
                    "pod's spec on a CONNECT -- the object is a PodExecOptions "
                    "-- so the guard names the pods instead, and this asserts "
                    "the name list was built from a real cluster scan",
                    argv=[
                        "kubectl",
                        "exec",
                        "-n",
                        _LEGACY_NAMESPACE,
                        _LEGACY_PRIVILEGED_POD,
                        "--",
                        "true",
                    ],
                    expect_stderr="may not open a shell",
                    setup=functools.partial(legacy_setup, _LEGACY_PRIVILEGED_POD),
                ),
            ]
            if legacy_setup
            else []
        ),
        # -- informational: read the output, there is no pass/fail here --
        Probe(
            name="info:visible-nodes",
            why="on vcluster this must list only virtual nodes",
            argv=["kubectl", "get", "nodes", "-o", "name"],
            expect_denied=False,
        ),
    ]


def _run_probe(executor: SandboxExecutor, probe: Probe) -> tuple[bool, str]:
    """Run one probe and judge it; returns ``(passed, detail)``."""
    if probe.setup and not probe.setup():
        return False, "[the probe's precondition could not be met; the boundary was never reached]"

    try:
        completed = executor.run(probe.argv, check=False, timeout=120)
    except (SandboxError, SubprocessError) as exc:
        return False, f"[the probe could not be run to a verdict: {exc}]"
    out = ((completed.stdout or "") + (completed.stderr or "")).strip()
    denied = completed.returncode != 0

    if probe.expect_denied != denied:
        return False, out
    if probe.expect_denied and probe.expect_stderr and probe.expect_stderr not in out:
        # Refused, but not by the control under test (typo, missing binary, RBAC not admission).
        return False, f"[refused, but not by the expected control]\n{out}"
    return True, out


def _check_policies(context: str) -> list[str]:
    """Host-side: confirm every policy exists and its CEL compiled; returns problem lines.

    A policy whose expression fails type-checking is accepted and then, under
    ``failurePolicy: Fail``, denies everything it matches.
    """
    problems: list[str] = []
    for name in creds._POLICY_NAMES:
        completed = _kubectl(context, "get", "validatingadmissionpolicy", name, "-o", "json")
        if completed.returncode != 0:
            problems.append(f"{name}: not present ({completed.stderr.strip()})")
            continue
        try:
            status = json.loads(completed.stdout).get("status", {})
        except (json.JSONDecodeError, AttributeError):
            problems.append(f"{name}: unreadable status ({completed.stdout[:80]!r})")
            continue
        for condition in status.get("typeChecking", {}).get("expressionWarnings", []):
            problems.append(f"{name}: CEL warning on {condition}")
        for condition in status.get("conditions", []):
            if condition.get("type") == "TypeChecking" and condition.get("status") != "True":
                problems.append(f"{name}: type checking failed: {condition.get('message')}")
    return problems


def _check_token_is_useless_against_host(kubeconfig: Path, host_apiserver: str) -> str:
    """Replay the agent's token against the HOST apiserver; returns a problem line or ``""``.

    A vcluster-minted token is signed by a key the host does not trust, so it must get 401/403.
    """
    from ruamel.yaml import YAML  # local: only this check needs it
    from ruamel.yaml.error import YAMLError

    try:
        loaded = YAML(typ="safe").load(kubeconfig.read_text())
        token = loaded["users"][0]["user"].get("token")
    except (OSError, YAMLError, KeyError, IndexError, TypeError, AttributeError) as exc:
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
        return f"the agent kubeconfig could not be read ({type(exc).__name__}: {first})"
    if not token:
        return "the agent kubeconfig carries no token -- it fell back to a certificate"
    argv = [
        "curl",
        "-sk",
        "-o",
        "/dev/null",
        "-w",
        "%{http_code}",
        "-H",
        "@-",
        f"{host_apiserver.rstrip('/')}/api/v1/nodes",
    ]
    try:
        # The header arrives on stdin so the token never appears in the process list.
        completed = subprocess.run(
            argv,
            input=f"Authorization: Bearer {token}\n",
            capture_output=True,
            text=True,
            check=False,
            timeout=_HOST_CMD_TIMEOUT_SEC,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"the host apiserver replay could not run ({exc})"
    code = completed.stdout.strip()
    if code in {"401", "403"}:
        return ""
    return f"the vcluster token got HTTP {code} from the HOST apiserver; expected 401/403"


def _provider_and_cluster(args: argparse.Namespace) -> tuple[Provider | None, ClusterInfo]:
    """Build the provider (``None`` for ``--provider none``) and cluster info from the CLI args.

    A real provider yields the plan a real run gets; raises SystemExit on a missing argument.
    """
    cluster = ClusterInfo(
        name=args.cluster_name or "",
        location=args.location,
        project=args.project,
        **({"kubeconfig_path": args.cluster_kubeconfig} if args.cluster_kubeconfig else {}),
    )
    if args.provider == "none":
        return None, cluster
    if args.provider == "kind":
        if not args.cluster_name:
            raise SystemExit("--provider kind needs --cluster-name (the kind cluster name)")
        return KindProvider(), cluster
    if args.provider == "gcp":
        if not (args.cluster_name and args.location and args.project):
            raise SystemExit("--provider gcp needs --cluster-name, --location and --project")
        return GcpProvider(), cluster
    if not args.cluster_kubeconfig:
        raise SystemExit(
            "--provider vcluster needs --cluster-kubeconfig (the virtual cluster's own "
            "kubeconfig, usually $TMPDIR/vcluster-<name>-kubeconfig.yaml); the provider "
            "reads its context from that file"
        )
    return VClusterProvider(), cluster


def main() -> int:
    """Provision, probe, report; 0 when every probe passed, 1 otherwise."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="sandbox image (BENCH_SANDBOX_IMAGE)")
    parser.add_argument(
        "--provider",
        choices=("none", "kind", "gcp", "vcluster"),
        default="none",
        help="build the network plan through this provider's sandbox_network_plan hook",
    )
    parser.add_argument("--cluster-name", default=None, help="cluster name (kind, gcp)")
    parser.add_argument("--location", default=None, help="cloud region or zone (gcp)")
    parser.add_argument("--project", default=None, help="cloud project (gcp)")
    parser.add_argument(
        "--cluster-kubeconfig",
        default=None,
        help="the cluster's own kubeconfig; required for vcluster, whose context is read from it",
    )
    parser.add_argument(
        "--context",
        default=None,
        help="kubectl context; required only for --provider none, else a cross-check",
    )
    parser.add_argument(
        "--docker-network", default=None, help="docker network override (--provider none only)"
    )
    parser.add_argument(
        "--host-apiserver",
        default=None,
        help="host cluster apiserver URL; enables the vcluster token-replay check",
    )
    parser.add_argument(
        "--keep", action="store_true", help="leave the probe namespace behind for inspection"
    )
    args = parser.parse_args()

    provider, cluster = _provider_and_cluster(args)
    if provider is None:
        if not args.context:
            raise SystemExit("--provider none needs --context")
        plan = NetworkPlan(kubectl_context=args.context, docker_network=args.docker_network)
    else:
        plan = build_network_plan(provider, cluster)

    context = plan.kubectl_context or args.context
    if not context:
        raise SystemExit("the plan named no context and none was given; refusing to run unpinned")
    if args.context and args.context != context:
        print(f"    NOTE: --context {args.context} overridden by the provider's {context}")
    print(f"==> plan: {plan}")

    with tempfile.TemporaryDirectory(prefix="bench-probe-") as tmp:
        workspace = Path(tmp) / "workspace"
        (workspace / "home").mkdir(parents=True)
        (workspace / "probe-hostpath.yaml").write_text(_HOSTPATH_POD)
        creds_dir = Path(tmp) / "creds"
        creds_dir.mkdir()

        failures: list[str] = []
        claimed_preexisted: bool | None = None
        # One finally from the first cluster write on: a raising probe must still reach cleanup.
        try:
            # Before provisioning: the PSA labeller must skip a namespace the cluster claims.
            print(f"==> creating the labelled namespace {_MANAGED_NAMESPACE}")
            managed_setup: Callable[[], bool] | None = functools.partial(
                _ensure_managed_namespace, context
            )
            if not managed_setup():
                print("    FAIL setup:managed-namespace (the by-label binding stays untested)")
                failures.append("setup:managed-namespace")
                managed_setup = None

            # Also before provisioning: these pods exist because admission could not refuse them.
            print(f"==> creating the pre-provisioning pods in {_LEGACY_NAMESPACE}")
            legacy_setup: Callable[[str], bool] | None = functools.partial(
                _legacy_pod_still_running, context
            )
            if not _create_legacy_pods(context):
                print("    FAIL setup:legacy-pods (the shell guard stays untested)")
                failures.append("setup:legacy-pods")
                legacy_setup = None

            print(f"==> provisioning the agent credential against {context}")
            kubeconfig = creds.provision_agent_credentials(plan, creds_dir, token_ttl_sec=3600)
            print(f"    kubeconfig: {kubeconfig}")

            print("==> checking the admission policies compiled")
            problems = _check_policies(context)
            for line in problems:
                print(f"    FAIL {line}")
            failures.extend(problems)

            executor = SandboxExecutor(
                SandboxSpec(
                    image=args.image, network=plan, workspace=workspace, kubeconfig=kubeconfig
                )
            )

            # Read before any probe runs, so cleanup never deletes a namespace the cluster owns.
            claimed_preexisted = _namespace_exists(context, _CLAIMED_EXEMPT_NAMESPACE)

            exec_target = _running_pod_in_kube_system(context)
            if exec_target == "":
                # A failed query is not an empty namespace: an untested escape must not read green.
                failures.append("setup:exec-target")
                exec_target = None
            elif exec_target is None:
                print("    NOTE: no running kube-system pod; skipping the exec probe")

            for probe in _probes(
                "/workspace/probe-hostpath.yaml", exec_target, managed_setup, legacy_setup
            ):
                passed, detail = _run_probe(executor, probe)
                verdict = "PASS" if passed else "FAIL"
                print(f"\n==> [{verdict}] {probe.name}")
                print(f"    why: {probe.why}")
                for line in detail.splitlines()[:12]:
                    print(f"    | {line}")
                if not passed:
                    failures.append(probe.name)

            if args.host_apiserver:
                print("\n==> replaying the agent token against the host apiserver")
                problem = _check_token_is_useless_against_host(kubeconfig, args.host_apiserver)
                print(f"    {problem or 'PASS: rejected, as it must be'}")
                if problem:
                    failures.append("vcluster:token-replay")
        finally:
            # Always, even under --keep: a failed probe here left a privileged pod in kube-system.
            for kind, name in (
                ("pod", "bench-probe-exempt"),
                ("deployment", "bench-probe-exempt-deploy"),
            ):
                _kubectl(
                    context,
                    "delete",
                    kind,
                    name,
                    "-n",
                    "kube-system",
                    "--ignore-not-found",
                    "--wait=false",
                )

            # Only if absent before and present now: the agent's claim worked and must not persist.
            if claimed_preexisted is False and _namespace_exists(
                context, _CLAIMED_EXEMPT_NAMESPACE
            ):
                _kubectl(
                    context,
                    "delete",
                    "namespace",
                    _CLAIMED_EXEMPT_NAMESPACE,
                    "--ignore-not-found",
                    "--wait=false",
                )

            # Also unconditional: a privileged pod the probe put there must not outlive the run.
            _kubectl(
                context,
                "delete",
                "namespace",
                _LEGACY_NAMESPACE,
                "--ignore-not-found",
                "--wait=false",
            )

            if not args.keep:
                for namespace in ("bench-probe-ok", _MANAGED_NAMESPACE):
                    _kubectl(
                        context,
                        "delete",
                        "namespace",
                        namespace,
                        "--ignore-not-found",
                        "--wait=false",
                    )

            # Teardown follows --keep and is itself a probe: a leftover policy denies the operator.
            if args.keep:
                print(
                    "\n==> --keep: the agent credential, PSA labels and admission policies "
                    "REMAIN on this cluster. The surviving pod-security policy denies the "
                    "operator's own privileged workloads — tear down before the next run "
                    "(see teardown_agent_credentials, or the known-issues recovery)."
                )
            else:
                print("\n==> tearing down the agent credential and admission policies")
                if not creds.teardown_agent_credentials(context):
                    print("    FAIL teardown:residue (teardown reported leftovers)")
                    failures.append("teardown:residue")
                leftover = _kubectl(
                    context,
                    "get",
                    "validatingadmissionpolicies.admissionregistration.k8s.io",
                    "-o",
                    "name",
                )
                stranded = [
                    line for line in (leftover.stdout or "").splitlines() if "bench-agent" in line
                ]
                if stranded:
                    print(f"    FAIL teardown:policies-survived ({', '.join(stranded)})")
                    failures.append("teardown:policies-survived")
                else:
                    print("    PASS: no bench-agent policies remain")

    print("\n" + "=" * 60)
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("all probes passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
