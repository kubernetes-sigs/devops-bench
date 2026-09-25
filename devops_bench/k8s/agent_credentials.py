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

"""Mint the scoped, short-lived ServiceAccount credential a sandboxed agent is given.

Everything here runs HOST-SIDE, under the operator's credentials, before the agent
starts, and every call is pinned to the run's own kubectl context.

Review rule: no cloud CLI (``gcloud``, ``aws``, ``az``) is ever invoked in this
module — everything is plain Kubernetes API surface reached through ``kubectl``.
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

# Own namespace: keeps the SA out of task workloads, and a task deleting its
# namespace cannot delete the credential out from under the running agent.
AGENT_NAMESPACE = "bench-system"
AGENT_SA_NAME = "bench-agent"

# Opt-in escape hatch: reuse the operator's admin certificate when a scoped
# credential cannot be minted. BENCH_-prefixed, so the env deny list keeps it out of the container.
ALLOW_ADMIN_ENV = "BENCH_SANDBOX_ALLOW_ADMIN_CREDS"

# Opt-in escape hatch: provision on the ambient current-context when the plan carries no
# pin (the no-op deployer), which writes cluster-wide objects onto an unidentified cluster.
ALLOW_AMBIENT_ENV = "BENCH_SANDBOX_ALLOW_AMBIENT_CLUSTER"

# Slack over the agent's timeout so the token covers provisioning, teardown, and clock skew.
TOKEN_TTL_SLACK_SEC = 900

# Pod-security levels a task may declare via ``agent_pod_security:``.
POD_SECURITY_BASELINE = "baseline"
POD_SECURITY_PRIVILEGED = "privileged"

# v1 spelled explicitly: ValidatingAdmissionPolicy is GA only in 1.30+; checked before
# the apply so an old cluster fails with a real message, not ``no matches for kind``.
_POLICY_API_RESOURCE = "validatingadmissionpolicies.v1.admissionregistration.k8s.io"
_MIN_CLUSTER_VERSION = "1.30"

# Namespaces the ADMISSION POLICY exempts: the cluster's own components legitimately run
# privileged. ``bench-system`` is deliberately NOT here — the agent can create pods in it.
# The two guard policies below enforce that the agent cannot write to any exempt name.
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

# Second half of the exemption: the label a managed cluster puts on namespaces it owns.
# A name list goes stale, and label selectors have no prefix operator; the names stay
# because on kind/vcluster nothing carries this label, and on GKE kube-system does not.
_ADDON_MANAGER_LABEL = "addonmanager.kubernetes.io/mode"

# Namespaces the PSA labeller skips: the policy-exempt set plus the harness's own.
_LABEL_EXEMPT_NAMESPACES = _POLICY_EXEMPT_NAMESPACES | {AGENT_NAMESPACE}

_PSA_ENFORCE_LABEL = "pod-security.kubernetes.io/enforce"

# The agent's own apiserver username, as RBAC and admission see it.
_AGENT_USERNAME = f"system:serviceaccount:{AGENT_NAMESPACE}:{AGENT_SA_NAME}"

# The exempt names as a CEL list literal, for the guard policy's expression.
_EXEMPT_CEL_LIST = ", ".join(f"'{name}'" for name in sorted(_POLICY_EXEMPT_NAMESPACES))

# Cluster-wide backstop behind the PSA labels (labels miss namespaces created later);
# ``failurePolicy: Fail`` throughout, because a control that fails open is not a control.
# ``pods/ephemeralcontainers`` is a distinct subresource: without it ``kubectl debug`` attaches
# privileged containers unchecked. The namespace guard covers UPDATE too — names are immutable
# but the agent holds ``patch``, so the addon-manager label would be a one-command escape.
# The exempt-namespace guard denies agent workload writes and exec into exempt namespaces
# outright; it matches every pod-producing kind because controllers create pods under their
# own identity, which is also why username scoping is sound for it and NOT for the pod policy.
# Two exempt-guard bindings: a namespaceSelector ANDs, so "by name OR by label" takes one each.
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
    """Render the policy denying exec into the named pods (CONNECT cannot see the pod's spec)."""
    # An empty CEL list literal won't compile, and under failurePolicy: Fail that denies every exec.
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


# Lifetime ceiling; no floor needed (the slack is the minimum), and the apiserver may shorten it.
_MAX_TOKEN_TTL_SEC = 7200

# Built-in ``edit`` bound cluster-wide, plus the cluster-scoped access it omits and tasks need.
# Deliberately no write on rbac.* (no self-escalation) or admissionregistration.* (policy stays).
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
    """Return a token lifetime: timeout plus slack, capped at two hours (cap alone if unbounded)."""
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
    """Create or update the agent's ServiceAccount and RBAC (idempotent via ``kubectl apply``).

    Args:
        work_dir: Where the manifest is rendered; must not be mounted into the container.
        context: kubectl context to pin the apply to; ``None`` uses the ambient current-context.
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

    PSA ``baseline`` labels plus the admission-policy backstop; namespaces already
    declaring an ``enforce`` level are left alone. Pre-existing non-conformant pods
    are handled by :func:`_deny_shell_into_nonconformant_pods`.

    Raises:
        SandboxError: Cluster does not serve the policy API; deliberately not gated by the hatch.
        SubprocessError: Policy apply or pod listing failed; label failures are warned and skipped.
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
    """Refuse a cluster too old to serve the pod-security backstop at ``v1``."""
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
    """Name the exempt namespaces on the cluster, mirroring the guard's two bindings."""
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
    """Report whether the policy would deny this spec (lockstep with its CEL, not PSA baseline)."""
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
    """List ``namespace/name`` of pods the policy would deny, outside the exempt namespaces."""
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
    """Deny the agent exec/attach/port-forward into pods that predate the policy."""
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

    # Applied even when empty: a reused cluster must not keep the previous run's list.
    manifest = work_dir / "bench-agent-nonconformant-pods.yaml"
    manifest.write_text(_render_nonconformant_pod_guard(pods))
    kubectl.apply(str(manifest), context=context)


def _labellable_namespaces(context: str | None) -> list[str]:
    """List namespaces to label, skipping system, cluster-managed, and already-enforcing ones."""
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
    """Mint a short-lived bearer token for the agent's ServiceAccount."""
    return kubectl.create_token(
        AGENT_SA_NAME,
        namespace=AGENT_NAMESPACE,
        duration_sec=ttl_sec,
        context=context,
    )


def render_agent_kubeconfig(plan: NetworkPlan, dest_dir: Path, *, user_fields: str) -> Path:
    """Write the self-contained single-cluster kubeconfig the container gets (no ``exec:`` block).

    Args:
        plan: Supplies the context pin, optional server rewrite, and tls-server-name.
        dest_dir: Must be OUTSIDE the workspace, or the credential surfaces under ``/workspace``.
        user_fields: Rendered inline-YAML body of the ``user:`` block, e.g. ``"token: <jwt>"``.

    Returns:
        Path of the written kubeconfig (mode 0600).

    Raises:
        SandboxError: When the context carries no CA or no server URL.
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
    # 0600 from creation (no umask window with a live token); chmod covers the re-render case.
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
    """Refuse before the first cluster write when the final kubeconfig render would fail."""
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
    """Seed the agent's identity and pod security, and render its kubeconfig (the entry point).

    Raises rather than silently falling back to the operator's admin credential.

    Args:
        plan: The run's network plan, supplying the context pin and any server rewrite.
        dest_dir: Directory (outside the workspace) for the kubeconfig and rendered manifests.
        token_ttl_sec: Requested token lifetime; see :func:`token_ttl_for`.
        pod_security: ``"privileged"`` skips :func:`enforce_pod_security` entirely.

    Returns:
        Path of the written kubeconfig (mode 0600).

    Raises:
        SandboxError: Unpinned plan without :data:`ALLOW_AMBIENT_ENV`; unrenderable context
            (checked before any cluster write); or enforcement/mint failure without
            :data:`ALLOW_ADMIN_ENV`.
    """
    _refuse_unpinned_cluster(plan)
    _preflight_render_inputs(plan)
    # One switch for both: an operator who cannot create cluster roles cannot create policies.
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
            # Caught here too, else the first cluster-scoped write makes the hatch unreachable.
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
    """Refuse cluster-wide writes onto a cluster no provider vouched for (unpinned plan)."""
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
    """Fall back to the operator's client certificate, giving up the RBAC boundary entirely."""
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
