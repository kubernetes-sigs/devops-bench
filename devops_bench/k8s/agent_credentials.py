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

"""Mint the scoped cluster credential a sandboxed agent is given.

The sandbox's first iteration mounted the operator's own kubeconfig
credential, which on every provider this benchmark uses is cluster-admin. The
container boundary was therefore doing all the work and the RBAC boundary
none: an agent that got a shell out of the container — or simply used the
credential as intended — held the whole cluster. This module replaces that
with a ServiceAccount token minted for the run: scoped by RBAC, short-lived,
and expired by the time anyone could reuse it.

It is also what makes GKE reachable from inside a container at all. A GKE
kubeconfig authenticates through ``gke-gcloud-auth-plugin``, an ``exec:``
credential plugin needing a ``gcloud`` binary and Application Default
Credentials — both of which the container deliberately lacks, and neither of
which it can be given without handing back the cloud identity the sandbox
exists to withhold. A bearer token needs no plugin, so the rendered kubeconfig
is self-contained.

Everything here runs HOST-SIDE, under the operator's credentials, before the
agent starts. Every call is pinned to the run's own kubectl context, which is
what puts the identity in the right cluster: under vcluster the host and
virtual clusters both appear in the operator's kubeconfig, and creating the
ServiceAccount in the virtual one is what makes its token cryptographically
useless against the host.

Review rule: no cloud CLI — ``gcloud``, ``aws``, ``az`` — is invoked
anywhere in this module. Everything
it does is plain Kubernetes API surface reached through ``kubectl``, so it
behaves identically on every provider and adds no cloud dependency to the
credential path.
"""

from __future__ import annotations

from pathlib import Path

from devops_bench.core import NetworkPlan, SandboxError, SubprocessError, get_bool, get_logger
from devops_bench.k8s import kubectl

__all__ = [
    "AGENT_NAMESPACE",
    "AGENT_SA_NAME",
    "ALLOW_ADMIN_ENV",
    "ALLOW_AMBIENT_ENV",
    "POD_SECURITY_BASELINE",
    "POD_SECURITY_PRIVILEGED",
    "ensure_agent_identity",
    "enforce_pod_security",
    "mint_agent_token",
    "provision_agent_credentials",
    "render_agent_kubeconfig",
    "token_ttl_for",
]

_log = get_logger("k8s.agent_credentials")

# The agent's identity. It gets its own namespace so the ServiceAccount is not
# mistaken for part of a task's workload, and so a task that deletes its own
# namespace cannot delete the credential out from under the running agent.
AGENT_NAMESPACE = "bench-system"
AGENT_SA_NAME = "bench-agent"

# Escape hatch back to the previous behaviour: reuse the operator's admin
# certificate when a scoped credential cannot be minted. Opt-in and loudly
# warned, because it gives up the RBAC boundary entirely. Being ``BENCH_``
# prefixed it is also on the sandbox's env deny list, so it cannot itself
# reach the container.
ALLOW_ADMIN_ENV = "BENCH_SANDBOX_ALLOW_ADMIN_CREDS"

# Escape hatch for provisioning against an UNPINNED cluster — the ambient
# current-context, because the run's deployer has no provider to ask (the no-op
# deployer, i.e. ``BENCH_NO_INFRA``). Everything this module creates is
# cluster-scoped and cluster-wide, so doing that unasked would write a Deny
# admission policy and a ClusterRoleBinding onto whatever cluster the
# operator's kubeconfig last pointed at. Opt-in, and ``BENCH_`` prefixed so it
# cannot itself cross into the container.
ALLOW_AMBIENT_ENV = "BENCH_SANDBOX_ALLOW_AMBIENT_CLUSTER"

# Slack added to the agent's own timeout so its token outlasts the work it is
# for, covering provisioning, teardown, and clock skew against the apiserver.
TOKEN_TTL_SLACK_SEC = 900

# Pod-security levels a task may declare via ``agent_pod_security:``.
POD_SECURITY_BASELINE = "baseline"
POD_SECURITY_PRIVILEGED = "privileged"

# The backstop's API, spelled so ``kubectl get`` resolves that exact version and
# not whatever else the cluster happens to serve. ValidatingAdmissionPolicy was
# GA'd in Kubernetes 1.30; 1.29 serves only ``v1beta1``, and behind a feature
# gate at that. Checked before the apply so an old cluster is named as an old
# cluster, rather than surfacing as ``no matches for kind``.
_POLICY_API_RESOURCE = "validatingadmissionpolicies.v1.admissionregistration.k8s.io"
_MIN_CLUSTER_VERSION = "1.30"

# Namespaces the ADMISSION POLICY leaves alone. They hold the cluster's own
# control-plane, storage and managed add-on components, which legitimately run
# privileged and with host mounts; denying them would break the cluster rather
# than the agent.
#
# ``bench-system`` is deliberately NOT in this set. It is skipped by the
# labeller below — it carries no workload to constrain — but the agent holds
# ``edit`` cluster-wide and can therefore create pods in it, so exempting it
# from the policy too would leave a namespace the agent can reach and the
# control cannot see.
#
# Every name here must be one the agent cannot write to. That is not a property
# of the names, it is a thing that has to be enforced, and enforcing it takes
# two more policies: ``bench-agent-namespace-guard`` stops the agent claiming an
# exempt name that does not exist yet, and
# ``bench-agent-exempt-namespace-guard`` stops it putting a workload into one
# that does. Without the second, a probe found the whole control was one
# ``-n kube-system`` away from irrelevant.
_POLICY_EXEMPT_NAMESPACES = frozenset(
    {
        "kube-system",
        "kube-public",
        "kube-node-lease",
        "local-path-storage",
        "gke-managed-system",
        "gmp-system",
        "vcluster",
    }
)

# The label a managed cluster puts on the namespaces it owns, and the second
# half of the exemption. A list of names cannot be kept current: a plain GKE
# run found four managed namespaces this set had never heard of
# (``gke-managed-cim``, ``gke-managed-networking-dra-driver``,
# ``gke-managed-volumepopulator``, ``gmp-public``), all created by the same
# addon manager as the two that ARE listed, all inside the deny scope. Nothing
# broke, because three were empty and the fourth runs an unprivileged metrics
# scraper — but a cluster using DRA or TPUs puts a privileged, hostPath
# DaemonSet in one of them, and a fail-closed policy would deny it.
#
# Prefix matching is what that wants and is not available: a label selector
# has no prefix operator, so the binding below cannot express
# ``gke-managed-*``. Keying off the addon manager's own label is better than
# the prefixes anyway — it is the cluster stating which namespaces are its to
# run, so it covers managed namespaces that do not exist yet and names that
# follow no convention we know about.
#
# Names stay as well: they are not redundant. On kind and on vcluster nothing
# carries this label, and on GKE ``kube-system`` itself does not.
_ADDON_MANAGER_LABEL = "addonmanager.kubernetes.io/mode"

# Namespaces the PSA labeller skips: the policy-exempt set, plus the harness's
# own — which is not the agent's to deploy into and holds only a
# ServiceAccount.
_LABEL_EXEMPT_NAMESPACES = _POLICY_EXEMPT_NAMESPACES | {AGENT_NAMESPACE}

_PSA_ENFORCE_LABEL = "pod-security.kubernetes.io/enforce"

# The agent's own apiserver username, as RBAC and admission see it.
_AGENT_USERNAME = f"system:serviceaccount:{AGENT_NAMESPACE}:{AGENT_SA_NAME}"

# The exempt names as a CEL list literal, for the guard policy's expression.
_EXEMPT_CEL_LIST = ", ".join(f"'{name}'" for name in sorted(_POLICY_EXEMPT_NAMESPACES))

# The admission-policy backstop, denying the escape the proposal observed:
# a privileged pod with a hostPath mount, used to read the bench checkout off
# the node's disk.
#
# PSA labels alone cannot cover this. A label is per-namespace, applied to the
# namespaces that exist when the agent starts — but the agent can create a
# namespace afterwards (``deploy-hello-app`` literally asks it to) and that
# one carries no label. A ValidatingAdmissionPolicy is cluster-wide and
# therefore proof against namespaces that do not exist yet. It also sidesteps
# a collision: ``deploy-hello-app``'s verifier asserts ``enforce=restricted``
# on its namespace, which the labeller must not clobber.
#
# ``failurePolicy: Fail`` because a control that fails open is not a control.
# The binding exempts the system namespaces by name, so a CEL evaluation error
# can cost the agent a pod but cannot wedge the cluster's own components.
#
# ``pods/ephemeralcontainers`` is matched alongside ``pods`` because it is a
# distinct subresource: a rule naming only ``pods`` matches the empty
# subresource, so without this ``kubectl debug --profile=sysadmin`` would
# attach a privileged container to an existing pod and never reach the
# ``ephemeralContainers`` validation below. ``object`` is the whole Pod on that
# subresource, so the same expressions apply unchanged.
#
# The second policy closes the other way past a name-based exemption: the agent
# can create namespaces, and several exempt names do not exist on every
# provider (``gmp-system`` on kind, ``local-path-storage`` on GKE), so it could
# simply claim one and deploy there. It is a separate policy with its own
# unselected binding because the pod policy's ``namespaceSelector`` would
# otherwise exempt the very creation being guarded. Scoping it to the agent's
# own username via ``matchConditions`` is safe here in a way it would NOT be
# for pods: a namespace is always created by whoever asked, whereas a pod may
# be created on the agent's behalf by a controller running as another identity.
#
# It covers UPDATE as well as CREATE, which the name half alone would not need
# — a name is immutable, so a namespace cannot be renamed into an exemption.
# A label can be added to one at any time, and the agent holds ``update`` and
# ``patch`` on namespaces, so exempting by ``_ADDON_MANAGER_LABEL`` without
# this would hand it a one-command escape: label a namespace it already owns
# and every pod in it stops being checked.
#
# The third policy is the one that makes the exemption safe at all, and it is
# here because a probe showed the first two were not enough. Guarding the
# *names* leaves the namespaces that already exist wide open: ``edit`` is bound
# cluster-wide, so the agent can write to ``kube-system`` on any cluster, and
# the pod policy deliberately skips it. ``kubectl run --privileged
# -n kube-system`` was therefore admitted on a cluster carrying the full policy
# set — the exact escape this module exists to deny, reachable by appending a
# flag. So the agent is denied workload writes into the exempt namespaces
# outright, which is what the "cannot write to" claim above always assumed.
#
# Scoped to the agent's username, because the cluster's own components must
# keep running: the exemption exists for kube-proxy and the CNI, not for
# whoever asks. Username scoping is sound here for the same reason it is for
# namespaces and is NOT for the pod policy — a controller never creates a pod
# in these namespaces on the agent's behalf, since the workload object it would
# have to be told to create is itself denied, at the agent's own username, one
# admission earlier.
#
# It covers every kind that can produce a pod rather than pods alone, because
# a Deployment reaches the same place with the pod created by the ReplicaSet
# controller under an identity of its own; and ``pods/exec`` alongside them,
# because ``edit`` grants exec and half the pods in ``kube-system`` are
# privileged, so a shell in one is the same escape by a longer route. It does
# not cover config: a ConfigMap in ``kube-system`` is a blast-radius question,
# not an escape, and denying those would break tasks for no boundary gain.
#
# Two bindings, because it has to select exactly what the pod policy skips and
# a ``namespaceSelector`` ANDs its expressions. "Not by name AND not by label"
# inverts to "by name OR by label", and an OR takes one binding each. Scoping
# by selector rather than testing ``namespaceObject`` in the expression also
# keeps ``failurePolicy: Fail`` cheap: a CEL error here can only block agent
# writes to namespaces where they are denied anyway, instead of every pod on
# the cluster.
_POD_SECURITY_POLICY_MANIFEST = f"""\
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicy
metadata:
  name: bench-agent-pod-security
spec:
  failurePolicy: Fail
  matchConstraints:
    resourceRules:
      - apiGroups: [""]
        apiVersions: ["v1"]
        operations: ["CREATE", "UPDATE"]
        resources: ["pods", "pods/ephemeralcontainers"]
  validations:
    - expression: "!has(object.spec.hostNetwork) || !object.spec.hostNetwork"
      message: "hostNetwork is not allowed for benchmark workloads"
    - expression: "!has(object.spec.hostPID) || !object.spec.hostPID"
      message: "hostPID is not allowed for benchmark workloads"
    - expression: "!has(object.spec.hostIPC) || !object.spec.hostIPC"
      message: "hostIPC is not allowed for benchmark workloads"
    - expression: >-
        !has(object.spec.volumes) ||
        object.spec.volumes.all(v, !has(v.hostPath))
      message: "hostPath volumes are not allowed for benchmark workloads"
    - expression: >-
        object.spec.containers.all(c,
          !has(c.securityContext) ||
          !has(c.securityContext.privileged) ||
          !c.securityContext.privileged)
      message: "privileged containers are not allowed for benchmark workloads"
    - expression: >-
        !has(object.spec.initContainers) ||
        object.spec.initContainers.all(c,
          !has(c.securityContext) ||
          !has(c.securityContext.privileged) ||
          !c.securityContext.privileged)
      message: "privileged init containers are not allowed for benchmark workloads"
    - expression: >-
        !has(object.spec.ephemeralContainers) ||
        object.spec.ephemeralContainers.all(c,
          !has(c.securityContext) ||
          !has(c.securityContext.privileged) ||
          !c.securityContext.privileged)
      message: "privileged ephemeral containers are not allowed for benchmark workloads"
---
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicyBinding
metadata:
  name: bench-agent-pod-security
spec:
  policyName: bench-agent-pod-security
  validationActions: ["Deny"]
  matchResources:
    namespaceSelector:
      matchExpressions:
        - key: kubernetes.io/metadata.name
          operator: NotIn
          values: [{", ".join(sorted(_POLICY_EXEMPT_NAMESPACES))}]
        - key: {_ADDON_MANAGER_LABEL}
          operator: DoesNotExist
---
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicy
metadata:
  name: bench-agent-namespace-guard
spec:
  failurePolicy: Fail
  matchConstraints:
    resourceRules:
      - apiGroups: [""]
        apiVersions: ["v1"]
        operations: ["CREATE", "UPDATE"]
        resources: ["namespaces"]
  matchConditions:
    - name: only-the-sandboxed-agent
      expression: "request.userInfo.username == '{_AGENT_USERNAME}'"
  validations:
    - expression: "!(object.metadata.name in [{_EXEMPT_CEL_LIST}])"
      message: >-
        that namespace name is reserved for the cluster's own components and is
        exempt from the benchmark's pod-security policy
    - expression: >-
        !has(object.metadata.labels) ||
        !('{_ADDON_MANAGER_LABEL}' in object.metadata.labels)
      message: >-
        that label marks a namespace as the cluster's own to manage and is
        exempt from the benchmark's pod-security policy
---
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicyBinding
metadata:
  name: bench-agent-namespace-guard
spec:
  policyName: bench-agent-namespace-guard
  validationActions: ["Deny"]
---
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicy
metadata:
  name: bench-agent-exempt-namespace-guard
spec:
  failurePolicy: Fail
  matchConstraints:
    resourceRules:
      - apiGroups: [""]
        apiVersions: ["v1"]
        operations: ["CREATE", "UPDATE"]
        resources:
          - pods
          - pods/ephemeralcontainers
          - replicationcontrollers
          - podtemplates
      - apiGroups: ["apps"]
        apiVersions: ["*"]
        operations: ["CREATE", "UPDATE"]
        resources: ["deployments", "daemonsets", "statefulsets", "replicasets"]
      - apiGroups: ["batch"]
        apiVersions: ["*"]
        operations: ["CREATE", "UPDATE"]
        resources: ["jobs", "cronjobs"]
      - apiGroups: [""]
        apiVersions: ["v1"]
        operations: ["CONNECT"]
        resources: ["pods/exec", "pods/attach", "pods/portforward"]
  matchConditions:
    - name: only-the-sandboxed-agent
      expression: "request.userInfo.username == '{_AGENT_USERNAME}'"
  validations:
    - expression: "false"
      message: >-
        this namespace holds the cluster's own components and is exempt from the
        benchmark's pod-security policy, so the sandboxed agent may not run a
        workload in it or exec into one
---
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicyBinding
metadata:
  name: bench-agent-exempt-namespace-guard-by-name
spec:
  policyName: bench-agent-exempt-namespace-guard
  validationActions: ["Deny"]
  matchResources:
    namespaceSelector:
      matchExpressions:
        - key: kubernetes.io/metadata.name
          operator: In
          values: [{", ".join(sorted(_POLICY_EXEMPT_NAMESPACES))}]
---
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicyBinding
metadata:
  name: bench-agent-exempt-namespace-guard-by-label
spec:
  policyName: bench-agent-exempt-namespace-guard
  validationActions: ["Deny"]
  matchResources:
    namespaceSelector:
      matchExpressions:
        - key: {_ADDON_MANAGER_LABEL}
          operator: Exists
"""

_NONCONFORMANT_GUARD_NAME = "bench-agent-nonconformant-pod-guard"


def _render_nonconformant_pod_guard(pods: list[str]) -> str:
    """Render the policy denying the agent a shell into named pods.

    Args:
        pods: ``namespace/name`` of every pod to deny, possibly empty.

    Returns:
        A multi-document manifest: the policy and its binding.
    """
    # An empty CEL list literal has no element type to infer, and a policy that
    # fails to compile under `failurePolicy: Fail` denies every exec the agent
    # attempts -- the opposite of inert. So the empty case gets a constant.
    if pods:
        listed = ", ".join(f"'{pod}'" for pod in pods)
        expression = f"!((request.namespace + '/' + request.name) in [{listed}])"
    else:
        expression = "true"

    return f"""\
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicy
metadata:
  name: {_NONCONFORMANT_GUARD_NAME}
spec:
  failurePolicy: Fail
  matchConstraints:
    resourceRules:
      - apiGroups: [""]
        apiVersions: ["v1"]
        operations: ["CONNECT"]
        resources: ["pods/exec", "pods/attach", "pods/portforward"]
  matchConditions:
    - name: only-the-sandboxed-agent
      expression: "request.userInfo.username == '{_AGENT_USERNAME}'"
  validations:
    - expression: "{expression}"
      message: >-
        this pod was created before the benchmark's pod-security policy was
        applied and would not be admitted under it, so the sandboxed agent may
        not open a shell, attach, or port-forward into it
---
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicyBinding
metadata:
  name: {_NONCONFORMANT_GUARD_NAME}
spec:
  policyName: {_NONCONFORMANT_GUARD_NAME}
  validationActions: ["Deny"]
"""


# Ceiling on the lifetime, so a long or unbounded run cannot mint a credential
# that outlives it by hours. There is no matching floor: the slack above is
# already the minimum any run gets. The apiserver may shorten the result
# further, which is fine — a shorter token is never a security problem.
_MAX_TOKEN_TTL_SEC = 7200

# The agent's permissions, as one applyable document.
#
# Built-in ``edit`` bound cluster-wide is the baseline: it is Kubernetes' own
# role for "change workloads, but not permissions", which is what the tasks
# ask for. What it omits is cluster-scoped resources, hence the supplement —
# an agent that cannot create a namespace or read nodes fails ordinary tasks,
# and an agent that fails an ordinary task goes looking for another way, which
# is how the proposal's first observed incident started.
#
# Two omissions are deliberate and load-bearing:
#
# * no write on ``rbac.authorization.k8s.io``, so the agent cannot grant
#   itself anything beyond this. Without that, every other limit is advisory.
# * no write on ``admissionregistration.k8s.io``, so it cannot remove the
#   admission policy that denies privileged pods.
_RBAC_MANIFEST = f"""\
apiVersion: v1
kind: Namespace
metadata:
  name: {AGENT_NAMESPACE}
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {AGENT_SA_NAME}
  namespace: {AGENT_NAMESPACE}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: {AGENT_SA_NAME}-edit
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: edit
subjects:
  - kind: ServiceAccount
    name: {AGENT_SA_NAME}
    namespace: {AGENT_NAMESPACE}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: {AGENT_SA_NAME}-cluster-supplement
rules:
  - apiGroups: [""]
    resources: ["namespaces"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: [""]
    resources: ["nodes", "persistentvolumes"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["storage.k8s.io"]
    resources: ["storageclasses", "csidrivers", "csinodes", "volumeattachments"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["apiextensions.k8s.io"]
    resources: ["customresourcedefinitions"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["apiregistration.k8s.io"]
    resources: ["apiservices"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["metrics.k8s.io"]
    resources: ["nodes", "pods"]
    verbs: ["get", "list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: {AGENT_SA_NAME}-cluster-supplement
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: {AGENT_SA_NAME}-cluster-supplement
subjects:
  - kind: ServiceAccount
    name: {AGENT_SA_NAME}
    namespace: {AGENT_NAMESPACE}
"""


def token_ttl_for(agent_timeout_sec: float | None) -> int:
    """Choose a token lifetime for an agent running under this timeout.

    Args:
        agent_timeout_sec: The agent's wall-clock budget, or ``None`` when it
            runs unbounded — which is exactly when the ceiling matters.

    Returns:
        The lifetime to request, in seconds: the timeout plus
        :data:`TOKEN_TTL_SLACK_SEC`, capped at two hours.
    """
    if agent_timeout_sec is None:
        _log.info(
            "agent runs without a timeout; capping its cluster token at %ds", _MAX_TOKEN_TTL_SEC
        )
        return _MAX_TOKEN_TTL_SEC
    requested = int(agent_timeout_sec) + TOKEN_TTL_SLACK_SEC
    if requested > _MAX_TOKEN_TTL_SEC:
        _log.info(
            "capping the agent cluster token lifetime at %ds (%ds requested)",
            _MAX_TOKEN_TTL_SEC,
            requested,
        )
        return _MAX_TOKEN_TTL_SEC
    return requested


def ensure_agent_identity(work_dir: Path, context: str | None = None) -> None:
    """Create or update the agent's ServiceAccount and the RBAC that scopes it.

    Idempotent by ``kubectl apply``, so this is safe to call once per task
    without tracking whether an earlier task on the same cluster already did
    it, and a manifest change takes effect on the next run.

    Args:
        work_dir: Directory to render the manifest into before applying. Must
            not itself be mounted into the container; the harness's
            credentials directory qualifies, since only the kubeconfig file
            within it is bind-mounted.
        context: kubectl context to pin the apply to. ``None`` uses the
            ambient current-context.

    Raises:
        SubprocessError: If the apply fails — most often because the operator
            cannot create cluster roles, or the apiserver is unreachable.
    """
    manifest = work_dir / "bench-agent-rbac.yaml"
    manifest.write_text(_RBAC_MANIFEST)
    kubectl.apply(str(manifest), context=context)
    _log.info(
        "ensured the sandboxed agent identity %s/%s (edit, plus a cluster-scoped supplement)",
        AGENT_NAMESPACE,
        AGENT_SA_NAME,
    )


def enforce_pod_security(work_dir: Path, context: str | None = None) -> None:
    """Deny privileged pods, host namespaces, and hostPath mounts cluster-wide.

    Two halves, because neither alone is enough. PSA ``baseline`` labels go on
    every namespace that exists now, which is the mechanism Kubernetes ships
    and the one an operator can read off a namespace; a
    ValidatingAdmissionPolicy backs them up cluster-wide, covering namespaces
    the agent creates *after* this runs — a label cannot, and one of the tasks
    asks the agent to create a namespace. Two further policies close the ways
    round the first one's exemptions: the agent may not claim an exempt
    namespace that does not exist yet, and may not run a workload in — or exec
    into one in — an exempt namespace that does.

    Namespaces that already carry an ``enforce`` label are left alone: a task
    may assert a specific level as part of its own verification (one asserts
    ``restricted``), and overwriting it would fail the task this control is
    supposed to protect.

    A third half, and the reason for the scan at the end: admission control
    only sees requests, so nothing here retroactively covers pods that already
    exist. The deployer runs first and some fixtures deploy privileged
    workloads on purpose — ``opa-remediation`` ships two, because remediating
    them is the task. Those pods stay, and the agent holds ``pods/exec``
    cluster-wide, so a shell into one is node root by a route this policy never
    sees. :func:`_deny_shell_into_nonconformant_pods` closes it.

    Args:
        work_dir: Directory to render the policy manifest into before
            applying. Must not itself be mounted into the container.
        context: kubectl context to pin every call to.

    Raises:
        SandboxError: If the cluster does not serve the policy API at all.
            Deliberately not routed through the admin escape hatch: that hatch
            is for an operator whose credential cannot write cluster-scoped
            objects, and no credential makes a 1.29 apiserver serve a v1
            policy.
        SubprocessError: If the policy cannot be applied, or the pods it cannot
            retroactively cover cannot be listed. Namespace labelling failures
            are warned and skipped — the policies are the load-bearing half,
            and one unlabellable namespace must not fail the run.
    """
    _require_policy_api(context)

    manifest = work_dir / "bench-agent-pod-security.yaml"
    manifest.write_text(_POD_SECURITY_POLICY_MANIFEST)
    kubectl.apply(str(manifest), context=context)

    _deny_shell_into_nonconformant_pods(work_dir, context)

    for name in _labellable_namespaces(context):
        try:
            kubectl.label(
                "namespace",
                name,
                {
                    _PSA_ENFORCE_LABEL: POD_SECURITY_BASELINE,
                    "pod-security.kubernetes.io/warn": POD_SECURITY_BASELINE,
                    "pod-security.kubernetes.io/audit": POD_SECURITY_BASELINE,
                },
                overwrite=True,
                context=context,
            )
        except SubprocessError as exc:
            _log.warning("could not label namespace %s for pod security: %s", name, exc)
    _log.info("pod security enforced: baseline labels plus the cluster-wide admission policy")


def _require_policy_api(context: str | None) -> None:
    """Refuse a cluster too old to serve the pod-security backstop.

    Without this the apply fails with kubectl's ``no matches for kind``, which
    reads like a typo in our manifest rather than what it is: an apiserver
    predating the API's GA. Every sandboxed run on such a cluster then refuses,
    correctly but unhelpfully.

    Args:
        context: kubectl context to pin the check to.

    Raises:
        SandboxError: If the cluster does not serve the policy API at ``v1``.
    """
    try:
        kubectl.get_resource(_POLICY_API_RESOURCE, context=context, timeout=60)
    except SubprocessError as exc:
        raise SandboxError(
            f"this cluster does not serve {_POLICY_API_RESOURCE} ({exc}); the sandbox's "
            "pod-security backstop is a ValidatingAdmissionPolicy, which reached GA in "
            f"Kubernetes {_MIN_CLUSTER_VERSION} — upgrade the cluster (for kind, the "
            "node_image variable) rather than running the agent without the backstop"
        ) from exc


def _policy_exempt_namespaces(context: str | None) -> set[str]:
    """Name the namespaces the exempt-namespace guard already covers.

    Mirrors that guard's two bindings — the name list and the addon manager's
    label — so a caller asking "can the agent reach into this namespace at
    all?" gets the same answer admission would give.

    Args:
        context: kubectl context to pin the listing to.

    Returns:
        Exempt namespace names present on the cluster.

    Raises:
        SubprocessError: If the namespaces cannot be listed.
    """
    listing = kubectl.get_resource("namespaces", context=context, timeout=60)
    exempt = set()
    for item in listing.get("items", []):
        meta = item.get("metadata", {})
        name = meta.get("name", "")
        if name and (
            name in _POLICY_EXEMPT_NAMESPACES or _ADDON_MANAGER_LABEL in meta.get("labels", {})
        ):
            exempt.add(name)
    return exempt


def _violates_pod_security(spec: dict) -> bool:
    """Report whether a pod spec is one :data:`_POD_SECURITY_POLICY_MANIFEST` denies.

    Kept deliberately in lockstep with that policy's CEL rather than with PSA
    ``baseline``, which is broader: a pod this returns False for is one the
    policy would admit, and claiming more than that would be a lie about what
    the guard below covers.

    Args:
        spec: The pod's ``.spec``.

    Returns:
        True when the policy would reject it.
    """
    if spec.get("hostNetwork") or spec.get("hostPID") or spec.get("hostIPC"):
        return True
    if any("hostPath" in volume for volume in spec.get("volumes") or []):
        return True
    for key in ("containers", "initContainers", "ephemeralContainers"):
        for container in spec.get(key) or []:
            if (container.get("securityContext") or {}).get("privileged"):
                return True
    return False


def _nonconformant_pods(context: str | None) -> list[str]:
    """List ``namespace/name`` for running pods the policy would have rejected.

    Only pods outside the exempt namespaces: inside them non-conformance is
    expected and the exempt-namespace guard already denies the agent every way
    in, so listing them here would bury the interesting ones.

    Args:
        context: kubectl context to pin the listing to.

    Returns:
        Sorted ``namespace/name`` strings.

    Raises:
        SubprocessError: If the pods or namespaces cannot be listed.
    """
    exempt = _policy_exempt_namespaces(context)
    listing = kubectl.get_resource("pods", all_namespaces=True, context=context, timeout=60)
    found = []
    for item in listing.get("items", []):
        meta = item.get("metadata", {})
        namespace, name = meta.get("namespace", ""), meta.get("name", "")
        if not namespace or not name or namespace in exempt:
            continue
        if _violates_pod_security(item.get("spec", {})):
            found.append(f"{namespace}/{name}")
    return sorted(found)


def _deny_shell_into_nonconformant_pods(work_dir: Path, context: str | None = None) -> None:
    """Deny the agent a shell into pods the policy could not stop being created.

    The pods are named individually rather than matched by a property, because
    admission cannot see the target pod's spec on a ``CONNECT``: the object on
    an exec request is a ``PodExecOptions``, so there is nothing to test. A
    list is the only thing a policy can check, and it stays correct for the run
    — the pod-security policy denies these pods on CREATE, so a name that
    leaves the list cannot come back.

    Applied even when nothing is non-conformant, with an expression that admits
    everything. Nothing here is torn down between runs, so a reused cluster
    would otherwise keep the previous run's list and refuse a shell into a pod
    that no longer exists.

    Args:
        work_dir: Directory to render the manifest into before applying.
        context: kubectl context to pin every call to.

    Raises:
        SubprocessError: If the pods cannot be listed or the policy applied.
    """
    pods = _nonconformant_pods(context)
    if pods:
        _log.warning(
            "%d pod(s) predate the pod-security policy and violate it (%s); they were "
            "created before this ran and admission cannot retract them, so the agent is "
            "denied exec, attach and port-forward into them instead",
            len(pods),
            ", ".join(pods),
        )
    else:
        _log.info("no pre-existing non-conformant pods; the shell guard is inert this run")

    manifest = work_dir / "bench-agent-nonconformant-pods.yaml"
    manifest.write_text(_render_nonconformant_pod_guard(pods))
    kubectl.apply(str(manifest), context=context)


def _labellable_namespaces(context: str | None) -> list[str]:
    """List the namespaces this run should label, skipping the ones it must not.

    Skips the system namespaces outright, the ones a managed cluster has
    labelled as its own to run, and any namespace that already declares an
    ``enforce`` level — that value belongs to whoever set it.
    """
    try:
        listing = kubectl.get_resource("namespaces", context=context, timeout=60)
    except SubprocessError as exc:
        _log.warning("could not list namespaces for pod-security labelling: %s", exc)
        return []
    names = []
    for item in listing.get("items", []):
        meta = item.get("metadata", {})
        name = meta.get("name", "")
        if not name or name in _LABEL_EXEMPT_NAMESPACES:
            continue
        if _ADDON_MANAGER_LABEL in meta.get("labels", {}):
            _log.debug("namespace %s is the cluster's own to manage; leaving it", name)
            continue
        if meta.get("labels", {}).get(_PSA_ENFORCE_LABEL):
            _log.debug("namespace %s already declares a pod-security level; leaving it", name)
            continue
        names.append(name)
    return names


def mint_agent_token(ttl_sec: int, context: str | None = None) -> str:
    """Mint a short-lived bearer token for the agent's ServiceAccount.

    Args:
        ttl_sec: Requested lifetime, already chosen by :func:`token_ttl_for`.
        context: kubectl context to pin the request to.

    Returns:
        The bearer token.

    Raises:
        SubprocessError: If the mint fails.
    """
    return kubectl.create_token(
        AGENT_SA_NAME,
        namespace=AGENT_NAMESPACE,
        duration_sec=ttl_sec,
        context=context,
    )


def render_agent_kubeconfig(plan: NetworkPlan, dest_dir: Path, *, user_fields: str) -> Path:
    """Write the single-cluster kubeconfig the container gets, and return its path.

    Exactly one cluster, one user, one context: the agent cannot switch to
    another cluster the operator's kubeconfig happens to know about. And no
    ``exec:`` block, so nothing in the file can invoke a credential plugin
    that would need the cloud identity the container deliberately lacks.

    Every read is pinned to ``plan.kubectl_context`` when the plan carries
    one, so the rendered CA and server belong to the run's own cluster even if
    the ambient current-context was switched after provisioning — by an
    operator mid-run, or by a parallel harness's ``up()``.

    Args:
        plan: The run's network plan. ``rewrite_server`` replaces the
            context's server URL and ``tls_server_name`` is rendered when set.
        dest_dir: Directory to write into. Callers must keep it OUTSIDE the
            workspace, otherwise the credential would also surface read-write
            under ``/workspace``.
        user_fields: Rendered inline-YAML body of the ``user:`` block, e.g.
            ``"token: <jwt>"``.

    Returns:
        Path of the written kubeconfig (mode 0600).

    Raises:
        SandboxError: When the context carries no CA or no server URL —
            refusing beats handing the container a kubeconfig that cannot
            authenticate.
    """
    ctx = plan.kubectl_context
    ca = kubectl.config_value("{.clusters[0].cluster.certificate-authority-data}", context=ctx)
    if not ca:
        raise SandboxError("could not read the cluster CA from the run's kubectl context")

    server = plan.rewrite_server or kubectl.config_value(
        "{.clusters[0].cluster.server}", context=ctx
    )
    if not server:
        raise SandboxError("could not read the cluster server URL from the run's kubectl context")

    cluster_fields = f"server: {server}, certificate-authority-data: {ca}"
    if plan.tls_server_name:
        cluster_fields += f", tls-server-name: {plan.tls_server_name}"
    path = dest_dir / "kubeconfig"
    # 0600 from the moment the file exists. Write-then-chmod would leave a
    # umask-wide window with a live bearer token inside, on a host that may
    # be shared; touch's mode only applies on creation, so the chmod stays
    # for the (re-render) case where the file is already there.
    path.touch(mode=0o600)
    path.chmod(0o600)
    path.write_text(
        "apiVersion: v1\n"
        "kind: Config\n"
        f"clusters: [{{name: c, cluster: {{{cluster_fields}}}}}]\n"
        f"users: [{{name: u, user: {{{user_fields}}}}}]\n"
        "contexts: [{name: ctx, context: {cluster: c, user: u}}]\n"
        "current-context: ctx\n"
    )
    return path


def _preflight_render_inputs(plan: NetworkPlan) -> None:
    """Refuse before the first cluster write when the kubeconfig cannot render.

    :func:`render_agent_kubeconfig` needs the context's embedded CA bundle and
    a server URL, and it runs *last* — after the admission policies and RBAC
    are already on the cluster. A context that cannot render (a
    ``certificate-authority`` file path instead of embedded ``-data``, no
    readable server) would strand those objects with nothing recording that
    they exist. The same reads up front cost one kubectl call each and keep
    every render failure ahead of the first write.

    Raises:
        SandboxError: When the context carries no embedded CA, or no server
            URL can be determined for a plan without its own rewrite.
    """
    ctx = plan.kubectl_context
    if not kubectl.config_value("{.clusters[0].cluster.certificate-authority-data}", context=ctx):
        raise SandboxError(
            "the run's kubectl context embeds no certificate-authority-data (a "
            "certificate-authority file path cannot cross into the container); "
            "refusing before anything is written to the cluster"
        )
    if not (
        plan.rewrite_server or kubectl.config_value("{.clusters[0].cluster.server}", context=ctx)
    ):
        raise SandboxError(
            "could not read the cluster server URL from the run's kubectl context; "
            "refusing before anything is written to the cluster"
        )


def provision_agent_credentials(
    plan: NetworkPlan,
    dest_dir: Path,
    *,
    token_ttl_sec: int,
    pod_security: str = POD_SECURITY_BASELINE,
) -> Path:
    """Seed the agent's identity and pod security, and render its kubeconfig.

    The single entry point the eval harness calls. On any failure to produce a
    scoped credential this raises rather than falling back: a run that quietly
    reverted to the operator's admin certificate would look identical in the
    results while having no RBAC boundary at all. The fallback exists only
    behind :data:`ALLOW_ADMIN_ENV`, for developing against a cluster where the
    operator cannot create cluster roles.

    Args:
        plan: The run's network plan, supplying the context pin and any server
            rewrite.
        dest_dir: Directory (outside the workspace) for the kubeconfig and the
            rendered RBAC manifest.
        token_ttl_sec: Requested token lifetime; see :func:`token_ttl_for`.
        pod_security: The task's declared ``agent_pod_security`` level.
            ``"privileged"`` skips :func:`enforce_pod_security` entirely, for
            a task whose own subject matter is privileged workloads.

    Returns:
        Path of the written kubeconfig (mode 0600).

    Raises:
        SandboxError: When the plan carries no context pin and
            :data:`ALLOW_AMBIENT_ENV` is unset; when the context cannot render
            a kubeconfig (checked up front, before anything is written to the
            cluster); when pod security cannot be enforced; or when no scoped
            credential can be minted — unless the admin fallback is explicitly
            enabled.
    """
    _refuse_unpinned_cluster(plan)
    _preflight_render_inputs(plan)
    # One switch covers both failures below, because they have one cause: an
    # operator whose credential cannot create cluster roles cannot create an
    # admission policy either.
    allow_admin = get_bool(ALLOW_ADMIN_ENV, False)

    if pod_security == POD_SECURITY_PRIVILEGED:
        _log.warning(
            "task declares agent_pod_security: %s, so privileged pods, host namespaces "
            "and hostPath mounts are NOT denied for this run",
            POD_SECURITY_PRIVILEGED,
        )
    else:
        try:
            enforce_pod_security(dest_dir, plan.kubectl_context)
        except SubprocessError as exc:
            # Inside the same guard as the credential below, and not merely
            # before it: this is the first cluster-scoped write the module
            # makes, so letting it escape uncaught would make the escape hatch
            # unreachable for the very operator it exists for.
            if not allow_admin:
                raise SandboxError(
                    f"could not enforce pod security for the sandboxed agent ({exc}); "
                    "refusing to run against a cluster where the privileged-pod and "
                    f"hostPath escape is not denied — set {ALLOW_ADMIN_ENV}=1 to "
                    "accept that explicitly"
                ) from exc
            _log.warning(
                "%s is set: continuing without pod-security enforcement (%s)",
                ALLOW_ADMIN_ENV,
                exc,
            )

    try:
        ensure_agent_identity(dest_dir, plan.kubectl_context)
        token = mint_agent_token(token_ttl_sec, plan.kubectl_context)
    except SubprocessError as exc:
        if not allow_admin:
            raise SandboxError(
                "could not mint a scoped ServiceAccount credential for the sandboxed "
                f"agent ({exc}); refusing to fall back to the operator's admin "
                f"credential — set {ALLOW_ADMIN_ENV}=1 to allow that explicitly"
            ) from exc
        return _render_admin_fallback_kubeconfig(plan, dest_dir)
    _log.info(
        "sandboxed agent will authenticate as %s/%s with a %ds token",
        AGENT_NAMESPACE,
        AGENT_SA_NAME,
        token_ttl_sec,
    )
    return render_agent_kubeconfig(plan, dest_dir, user_fields=f"token: {token}")


def _refuse_unpinned_cluster(plan: NetworkPlan) -> None:
    """Refuse to write cluster-scoped objects onto an unidentified cluster.

    A plan with no context pin means no provider answered for this run — the
    no-op deployer, i.e. ``BENCH_NO_INFRA``. Every provider that provisions a
    cluster pins to it, so the unpinned case is not "some cluster we made" but
    "whatever the operator's kubeconfig happens to point at", which may well be
    something that matters. This module would then install a cluster-wide Deny
    admission policy, relabel its namespaces, and bind ``edit`` on it.

    Raises:
        SandboxError: When the plan is unpinned and :data:`ALLOW_AMBIENT_ENV`
            is not set.
    """
    if plan.kubectl_context:
        return
    current = kubectl.config_value("{.current-context}") or "<unset>"
    if get_bool(ALLOW_AMBIENT_ENV, False):
        _log.warning(
            "%s is set: provisioning the sandboxed agent's identity and pod-security "
            "policy on the ambient current-context (%s), which no provider vouched for",
            ALLOW_AMBIENT_ENV,
            current,
        )
        return
    raise SandboxError(
        "this run's network plan carries no kubectl context pin, so its deployer has "
        "no provider to identify the cluster (BENCH_NO_INFRA / the no-op deployer). "
        "Provisioning would create a cluster-wide admission policy and ClusterRoleBindings "
        f"on the ambient current-context ({current}) — whatever cluster the operator's "
        f"kubeconfig last pointed at. Refusing; set {ALLOW_AMBIENT_ENV}=1 to allow it."
    )


def _render_admin_fallback_kubeconfig(plan: NetworkPlan, dest_dir: Path) -> Path:
    """Give the agent the operator's own client certificate instead of a token.

    The pre-scoping behaviour, kept only for local development against a
    cluster where the operator cannot create cluster roles. It gives up the
    RBAC boundary completely, so it warns every time — and it still refuses
    when the context has no static certificate to copy.

    Raises:
        SandboxError: When the context authenticates through an ``exec:``
            plugin, which has no static credential to copy and could not run
            inside the container anyway.
    """
    ctx = plan.kubectl_context
    cert = kubectl.config_value("{.users[0].user.client-certificate-data}", context=ctx)
    key = kubectl.config_value("{.users[0].user.client-key-data}", context=ctx)
    if not (cert and key):
        raise SandboxError(
            f"{ALLOW_ADMIN_ENV} is set but the run's kubectl context carries no static "
            "client certificate to fall back to; it authenticates through an exec "
            "credential plugin, which cannot run inside the container"
        )
    _log.warning(
        "%s is set: the sandboxed agent is being given the operator's admin client "
        "certificate. The container boundary is doing all the work and the RBAC "
        "boundary none. Never use this for a scored run.",
        ALLOW_ADMIN_ENV,
    )
    return render_agent_kubeconfig(
        plan,
        dest_dir,
        user_fields=f"client-certificate-data: {cert}, client-key-data: {key}",
    )
