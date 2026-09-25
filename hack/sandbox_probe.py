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

A passing task run proves the agent can WORK inside the sandbox. It says
nothing about whether the boundary HOLDS -- the pod-security policy could be
absent entirely and the run would look identical. This script probes the
boundary directly: it provisions the agent's real credential through the real
code path, then runs each escape from inside a real sandbox container and
checks it is refused.

Control probes come first and are not optional. If the token is broken, every
escape probe "passes" for the wrong reason, and a green run would mean nothing.

Usage:
    uv run python hack/sandbox_probe.py --provider kind --cluster-name kind \\
        --image <sandbox-image>

The network plan is built through the shipped
``agents.sandbox.build_network_plan`` rather than assembled here, so the
container reaches the apiserver exactly the way a real run does. Hand-building
the plan is what made an earlier version of this script unable to connect at
all: kind writes ``https://127.0.0.1:<port>`` as its server, which from inside
a container is the container.

This began as scratch validation tooling (18/18 probes green on GKE before
promotion) and is now the boundary's regression suite:
``tests/e2e/test_sandbox_boundary.py`` drives it and asserts the exit code, so
a boundary regression is a red build. It stays runnable standalone because an
operator debugging a refusal wants this transcript, not a pytest traceback.
Unless ``--keep`` is given, the run ends by tearing the provisioned boundary
back down and asserting nothing survived — teardown is part of the contract,
not cleanup.
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
from devops_bench.k8s import agent_credentials as creds
from devops_bench.providers.base import Provider
from devops_bench.providers.gcp import GcpProvider
from devops_bench.providers.kind import KindProvider
from devops_bench.providers.vcluster import VClusterProvider

# Written into the workspace rather than piped: the container runs without
# ``-i`` by design, so ``kubectl apply -f -`` would read an empty stdin and
# the probe would "pass" without ever reaching admission.
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

_METADATA_URL = "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token"

# A namespace carrying the addon manager's label and absent from the exempt
# NAME list, created host-side so the by-label binding has something only it
# can match. The agent cannot build this itself: ``bench-agent-namespace-guard``
# denies it the label, which the ``review:claim-managed-label`` probe asserts.
_MANAGED_NAMESPACE = "bench-probe-managed"

# An ordinary, non-exempt namespace holding two pods built host-side *before*
# provisioning, standing in for what ``tf/prebuilt/opa-remediation`` leaves
# behind: one privileged pod the policy would have refused had it existed yet,
# and one conformant pod beside it. The pair is the point -- they differ only
# in conformance, so a deny on the first and a success on the second can only
# be the shell guard discriminating between them.
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
        expect_stderr: Substring the refusal must mention, so a probe that
            fails for an unrelated reason (typo, missing binary) is not
            mistaken for the boundary doing its job.
        setup: Host-side state this probe needs, re-asserted immediately
            before it runs and reported as a failure when it cannot be. A
            probe whose precondition is missing has not passed and has not
            been skipped -- it never reached the boundary at all.
    """

    name: str
    why: str
    argv: list[str]
    expect_denied: bool = True
    expect_stderr: str = ""
    setup: Callable[[], bool] | None = None


def _running_pod_in_kube_system(context: str) -> str | None:
    """Name a running kube-system pod for the exec probe to aim at.

    Resolved host-side because the probe needs a target that exists: with a
    made-up name ``kubectl exec`` fails on the preliminary GET, before the
    apiserver ever reaches admission, and the probe would report a refusal the
    boundary had nothing to do with.

    Args:
        context: kubectl context to query.

    Returns:
        A pod name, or ``None`` when the namespace has no running pod — a
        vcluster's virtual ``kube-system`` may have none, and there is nothing
        to prove there.
    """
    completed = subprocess.run(
        [
            "kubectl",
            "--context",
            context,
            "get",
            "pods",
            "-n",
            "kube-system",
            "--field-selector=status.phase=Running",
            "-o",
            "jsonpath={.items[0].metadata.name}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() or None


def _ensure_managed_namespace(context: str) -> bool:
    """Make sure the labelled namespace the by-label binding needs is Active.

    Two details here are not stylistic, they are what the first live run cost.
    GKE's addon manager treats a namespace carrying its label as one of its own
    and prunes it: on that run it deleted this namespace 2.5 seconds before the
    probe's request reached the apiserver, and the probe reported ``namespaces
    "bench-probe-managed" not found`` rather than a refusal.

    So the namespace is built with ``create`` + ``label`` rather than ``apply``
    -- the pruner skips objects with no ``last-applied-configuration``
    annotation, and ``apply`` is what writes one -- and this function is called
    again immediately before the probe, which closes whatever window is left.

    Args:
        context: kubectl context to create it in.

    Returns:
        True once the namespace is Active. False means the probe must not run:
        a missing namespace produces a refusal of its own that has nothing to
        do with the boundary.
    """

    def kubectl(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["kubectl", "--context", context, *args], capture_output=True, text=True, check=False
        )

    for _ in range(4):
        phase = kubectl("get", "namespace", _MANAGED_NAMESPACE, "-o", "jsonpath={.status.phase}")
        if phase.returncode == 0 and phase.stdout.strip() == "Active":
            break
        if phase.stdout.strip() == "Terminating":
            # A prune already in flight. Recreating now fails; wait it out.
            time.sleep(5)
            continue
        created = kubectl("create", "namespace", _MANAGED_NAMESPACE)
        if created.returncode != 0 and "already exists" not in created.stderr:
            print(f"    {created.stderr.strip()}")
            time.sleep(2)
    else:
        return False

    labelled = kubectl(
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
    return subprocess.run(
        ["kubectl", "--context", context, *args], capture_output=True, text=True, check=False
    )


def _create_legacy_pods(context: str) -> bool:
    """Build the pods that must predate provisioning, and wait for them.

    Called before ``provision_agent_credentials`` on purpose: the whole point
    of the guard under test is that admission never saw these creates. Running
    this afterwards would have the pod-security policy refuse the privileged
    one, and the probe would be testing the wrong control.

    They must reach Running, not merely exist. ``kubectl exec`` on a Pending
    pod fails in the kubelet with a message the boundary had nothing to do
    with, which would read as a deny.

    The pod-security policy is dropped first, and that is not a shortcut. On a
    fresh cluster the deployer runs before anything here exists, which is the
    situation being reproduced; on a cluster a previous run left policies on,
    the operator's own create is refused -- the policy is deliberately not
    username-scoped, because a pod is often created on the agent's behalf by a
    controller. Provisioning re-applies it seconds later, and the probe below
    reads the policy's own status to confirm that.

    Args:
        context: kubectl context to create them in.

    Returns:
        True once both pods are Running.
    """
    for kind in ("validatingadmissionpolicybinding", "validatingadmissionpolicy"):
        _kubectl(context, "delete", kind, "bench-agent-pod-security", "--ignore-not-found")
    _kubectl(context, "create", "namespace", _LEGACY_NAMESPACE)
    # The namespace's default ServiceAccount is minted asynchronously by the
    # controller manager, and a pod create that races it is refused with
    # "serviceaccount \"default\" not found" -- a failure the boundary had
    # nothing to do with, surfaced live on a freshly created kind cluster.
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
            "--timeout=120s",
        )
        if waited.returncode != 0:
            print(f"    {waited.stderr.strip()}")
            return False
    return True


def _legacy_pod_still_running(context: str, name: str) -> bool:
    """Re-assert that a legacy pod is still Running, without rebuilding it.

    Deliberately not a create: by the time this runs the pod-security policy is
    installed, so recreating the privileged one would be denied and the probe
    would report a refusal from the wrong control. If the pod is gone, the
    precondition is gone with it and the probe has to say so.

    Args:
        context: kubectl context to query.
        name: Pod name in :data:`_LEGACY_NAMESPACE`.

    Returns:
        True when the pod is Running.
    """
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
    """Build the probe list.

    Args:
        workspace_pod_path: Container path of the hostPath manifest.
        exec_target: A running kube-system pod to attempt exec into, or
            ``None`` to skip that probe.
        managed_setup: Re-asserts the labelled namespace, or ``None`` to drop
            the by-label probe because it could not be built at all.
        legacy_setup: Re-asserts that a named pre-provisioning pod is still
            Running, or ``None`` to drop the shell-guard probes because the
            pods could not be built at all.

    Returns:
        Controls first, then the escapes, then the informational checks.
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
            argv=["kubectl", "create", "namespace", "gmp-system"],
            expect_stderr="reserved",
        ),
        # -- the exemption's other edge: namespaces that already exist --
        #
        # This trio found a real hole and is the reason the exempt-namespace
        # guard exists. On the first live run the pod probe came back CREATED:
        # the pod policy skips kube-system by namespaceSelector, the labeller
        # skips it too, and edit is bound cluster-wide, so the escape the whole
        # stack exists to deny was one -n kube-system away. Keep all three --
        # they now assert the guard rather than document the hole, and each
        # covers a different way in.
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
    """Run one probe and judge it.

    Args:
        executor: Executor wrapping the same sandbox the agent would get.
        probe: The probe to run.

    Returns:
        ``(passed, detail)``; ``detail`` is the output worth printing.
    """
    if probe.setup and not probe.setup():
        return False, "[the probe's precondition could not be met; the boundary was never reached]"

    completed = executor.run(probe.argv, check=False, timeout=120)
    out = ((completed.stdout or "") + (completed.stderr or "")).strip()
    denied = completed.returncode != 0

    if probe.expect_denied != denied:
        return False, out
    if probe.expect_denied and probe.expect_stderr and probe.expect_stderr not in out:
        # Refused, but not by the control we are testing -- a typo, a missing
        # binary, or an RBAC denial standing in for an admission denial.
        return False, f"[refused, but not by the expected control]\n{out}"
    return True, out


_POLICY_NAMES = (
    "bench-agent-pod-security",
    "bench-agent-namespace-guard",
    "bench-agent-exempt-namespace-guard",
    "bench-agent-nonconformant-pod-guard",
)


def _check_policies(context: str) -> list[str]:
    """Host-side: confirm every policy exists and its CEL compiled.

    A ValidatingAdmissionPolicy whose expression does not type-check is
    accepted by the apiserver and then, under ``failurePolicy: Fail``, denies
    everything it matches. On a shared cluster that is a bad afternoon, so it
    is worth reading the status rather than assuming the apply succeeded.

    Args:
        context: kubectl context to query.

    Returns:
        Human-readable problem lines; empty when every policy is healthy.
    """
    problems: list[str] = []
    for name in _POLICY_NAMES:
        completed = subprocess.run(
            [
                "kubectl",
                "--context",
                context,
                "get",
                "validatingadmissionpolicy",
                name,
                "-o",
                "json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            problems.append(f"{name}: not present ({completed.stderr.strip()})")
            continue
        status = json.loads(completed.stdout).get("status", {})
        for condition in status.get("typeChecking", {}).get("expressionWarnings", []):
            problems.append(f"{name}: CEL warning on {condition}")
        for condition in status.get("conditions", []):
            if condition.get("type") == "TypeChecking" and condition.get("status") != "True":
                problems.append(f"{name}: type checking failed: {condition.get('message')}")
    return problems


def _check_token_is_useless_against_host(kubeconfig: Path, host_apiserver: str) -> str:
    """Replay the agent's token against the HOST apiserver; expect a 401.

    The whole point of pinning to the virtual cluster's context is that the
    minted ServiceAccount lives inside the vcluster and its token is signed by
    a key the host apiserver does not trust.

    Args:
        kubeconfig: The generated agent kubeconfig.
        host_apiserver: Base URL of the host cluster's apiserver.

    Returns:
        A problem line, or ``""`` when the token was correctly rejected.
    """
    import yaml  # local: only this check needs it

    token = yaml.safe_load(kubeconfig.read_text())["users"][0]["user"].get("token")
    if not token:
        return "the agent kubeconfig carries no token -- it fell back to a certificate"
    completed = subprocess.run(
        [
            "curl",
            "-sk",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            "-H",
            f"Authorization: Bearer {token}",
            f"{host_apiserver.rstrip('/')}/api/v1/nodes",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    code = completed.stdout.strip()
    if code in {"401", "403"}:
        return ""
    return f"the vcluster token got HTTP {code} from the HOST apiserver; expected 401/403"


def _provider_and_cluster(args: argparse.Namespace) -> tuple[Provider | None, ClusterInfo]:
    """Build the run's provider and cluster description from the CLI args.

    The point of going through a real provider is that the plan it returns is
    the plan a real run gets. A hand-built plan can be wrong in exactly the way
    the code under test is supposed to prevent.

    Args:
        args: Parsed command line.

    Returns:
        The provider (``None`` for ``--provider none``) and its cluster info.

    Raises:
        SystemExit: When the chosen provider is missing a required argument.
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
    """Provision, probe, report.

    Returns:
        0 when every probe passed, 1 otherwise.
    """
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

        # Before provisioning, so it is also in front of the PSA labeller,
        # which must skip a namespace the cluster has claimed as its own.
        print(f"==> creating the labelled namespace {_MANAGED_NAMESPACE}")
        managed_setup: Callable[[], bool] | None = functools.partial(
            _ensure_managed_namespace, context
        )
        if not managed_setup():
            print("    FAIL setup:managed-namespace (the by-label binding stays untested)")
            managed_setup = None

        # Also before provisioning, and that ordering is the whole test: these
        # pods exist because admission was not there yet to refuse them.
        print(f"==> creating the pre-provisioning pods in {_LEGACY_NAMESPACE}")
        legacy_setup: Callable[[str], bool] | None = functools.partial(
            _legacy_pod_still_running, context
        )
        if not _create_legacy_pods(context):
            print("    FAIL setup:legacy-pods (the shell guard stays untested)")
            legacy_setup = None

        print(f"==> provisioning the agent credential against {context}")
        kubeconfig = creds.provision_agent_credentials(plan, creds_dir, token_ttl_sec=3600)
        print(f"    kubeconfig: {kubeconfig}")

        print("==> checking the admission policies compiled")
        problems = _check_policies(context)
        for line in problems:
            print(f"    FAIL {line}")

        executor = SandboxExecutor(
            SandboxSpec(image=args.image, network=plan, workspace=workspace, kubeconfig=kubeconfig)
        )

        exec_target = _running_pod_in_kube_system(context)
        if not exec_target:
            print("    NOTE: no running kube-system pod; skipping the exec probe")

        failures = list(problems)
        if managed_setup is None:
            failures.append("setup:managed-namespace")
        if legacy_setup is None:
            failures.append("setup:legacy-pods")
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

        # Always, even under --keep. If the exempt-namespace probes FAILED then
        # the escape worked, and what they left behind is a privileged
        # container in kube-system. That is not something to leave for
        # inspection.
        for kind, name in (
            ("pod", "bench-probe-exempt"),
            ("deployment", "bench-probe-exempt-deploy"),
        ):
            subprocess.run(
                [
                    "kubectl",
                    "--context",
                    context,
                    "delete",
                    kind,
                    name,
                    "-n",
                    "kube-system",
                    "--ignore-not-found",
                    "--wait=false",
                ],
                capture_output=True,
                check=False,
            )

        # Also unconditional: this namespace holds a privileged pod that the
        # probe itself put there, and leaving it for inspection would leave the
        # escape route open on a cluster the next run reuses.
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
                subprocess.run(
                    [
                        "kubectl",
                        "--context",
                        context,
                        "delete",
                        "namespace",
                        namespace,
                        "--ignore-not-found",
                        "--wait=false",
                    ],
                    capture_output=True,
                    check=False,
                )

        # The boundary objects follow --keep the same way the namespaces do:
        # kept for inspection on request, removed otherwise — and their
        # removal is itself a probe. Teardown is a correctness requirement on
        # reused clusters (the pod-security policy is not username-scoped, so
        # a survivor denies the OPERATOR too), so a teardown that strands an
        # object, or a policy that outlives it, fails the suite like any
        # escape would.
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
