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

"""Unit tests for devops_bench.k8s.agent_credentials.

The kubectl argv and the rendered YAML are the boundary this module owns, so
that is what the tests assert on — no cluster and no docker daemon needed.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from devops_bench.core import NetworkPlan
from devops_bench.core.errors import SandboxError, SubprocessError
from devops_bench.k8s import agent_credentials as creds
from devops_bench.k8s import kubectl

_CA = "ZmFrZS1jYQ=="
_TOKEN = "eyJhbGciOi.fake.token"

# Provisioning refuses an unpinned plan (it would write cluster-scoped objects
# onto the ambient current-context), so the provisioning tests carry a pin.
_PINNED = NetworkPlan(kubectl_context="kind-c1")


def _applies(argv: list[str], manifest: str) -> bool:
    """Report whether ``argv`` is a kubectl apply of ``manifest``.

    The manifest is matched anywhere in argv rather than at the tail: a pinned
    call appends ``--context <name>`` after the ``-f`` path.
    """
    return "apply" in argv and any(manifest in arg for arg in argv)


def _patch_kubectl(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ca: str = _CA,
    server: str = "https://127.0.0.1:6443",
    cert: str = "Y2VydA==",
    key: str = "a2V5",
    token: str = _TOKEN,
    mint_fails: bool = False,
    namespaces: dict | None = None,
    pods: dict | None = None,
    policy_api: bool = True,
    delete_fails: set[str] | None = None,
    calls: list[list[str]] | None = None,
) -> list[list[str]]:
    """Answer every kubectl call this module makes, recording the argv.

    Returns the list the argvs land in, so a test can assert on the exact
    command line the module would have run.
    """
    seen = calls if calls is not None else []
    namespaces = namespaces if namespaces is not None else {"items": []}
    pods = pods if pods is not None else {"items": []}
    answers = {
        "jsonpath={.clusters[0].cluster.certificate-authority-data}": ca,
        "jsonpath={.clusters[0].cluster.server}": server,
        "jsonpath={.users[0].user.client-certificate-data}": cert,
        "jsonpath={.users[0].user.client-key-data}": key,
        "jsonpath={.current-context}": "some-ambient-context",
    }

    def fake_run(argv, **kwargs):
        seen.append(argv)
        # Matched anywhere in argv, not at the tail: a pinned call appends
        # ``--context <name>`` after the jsonpath.
        asked = next((arg for arg in argv if arg in answers), None)
        if asked is not None:
            return SimpleNamespace(returncode=0, stdout=answers[asked], stderr="")
        if "token" in argv:
            if mint_fails:
                raise SubprocessError(argv, 1, stderr="forbidden: cannot create token")
            return SimpleNamespace(returncode=0, stdout=f"{token}\n", stderr="")
        if "apply" in argv:
            if mint_fails and _applies(argv, "bench-agent-rbac.yaml"):
                raise SubprocessError(argv, 1, stderr="forbidden: cannot create clusterroles")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "get" in argv:
            # Read off the verb rather than a fixed index: the context flags go
            # in ahead of the resource on a pinned call, and ``-A`` after it.
            resource = argv[argv.index("get") + 1]
            if resource == "namespaces":
                return SimpleNamespace(returncode=0, stdout=json.dumps(namespaces), stderr="")
            if resource == "pods":
                return SimpleNamespace(returncode=0, stdout=json.dumps(pods), stderr="")
            if resource == creds._POLICY_API_RESOURCE:
                if not policy_api:
                    raise SubprocessError(
                        argv,
                        1,
                        stderr=f'error: the server doesn\'t have a resource type "{resource}"',
                    )
                return SimpleNamespace(returncode=0, stdout=json.dumps({"items": []}), stderr="")
        if "label" in argv:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "delete" in argv:
            kind = argv[argv.index("delete") + 1]
            if delete_fails and kind in delete_fails:
                raise SubprocessError(argv, 1, stderr="conflict: operation cannot be fulfilled")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected kubectl argv: {argv}")

    monkeypatch.setattr(kubectl, "run", fake_run)
    return seen


# -- the agent identity ------------------------------------------------------


def test_ensure_agent_identity_applies_the_rendered_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _patch_kubectl(monkeypatch)

    creds.ensure_agent_identity(tmp_path)

    manifest = tmp_path / "bench-agent-rbac.yaml"
    assert calls == [["kubectl", "apply", "-f", str(manifest)]]
    assert manifest.exists()


def test_ensure_agent_identity_pins_the_apply_to_the_runs_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Under vcluster the host and virtual clusters share one kubeconfig, so an
    unpinned apply could seed the agent identity in the wrong one."""
    calls = _patch_kubectl(monkeypatch)

    creds.ensure_agent_identity(tmp_path, "vcluster-c1")

    assert calls[0][-2:] == ["--context", "vcluster-c1"]


def _rbac_docs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    _patch_kubectl(monkeypatch)
    creds.ensure_agent_identity(tmp_path)
    text = (tmp_path / "bench-agent-rbac.yaml").read_text()
    return [d for d in yaml.safe_load_all(text) if d]


def test_rbac_binds_edit_to_the_agent_service_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    docs = _rbac_docs(tmp_path, monkeypatch)
    by_kind = {(d["kind"], d["metadata"]["name"]): d for d in docs}

    assert ("Namespace", creds.AGENT_NAMESPACE) in by_kind
    assert ("ServiceAccount", creds.AGENT_SA_NAME) in by_kind
    binding = by_kind[("ClusterRoleBinding", f"{creds.AGENT_SA_NAME}-edit")]
    assert binding["roleRef"]["name"] == "edit"
    assert binding["subjects"] == [
        {
            "kind": "ServiceAccount",
            "name": creds.AGENT_SA_NAME,
            "namespace": creds.AGENT_NAMESPACE,
        }
    ]


def test_rbac_supplements_edit_with_the_cluster_scoped_reads_tasks_need(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``edit`` omits cluster-scoped resources, and an agent that cannot create
    a namespace or read nodes fails ordinary tasks — which is how the
    proposal's first observed incident started."""
    docs = _rbac_docs(tmp_path, monkeypatch)
    role = next(
        d
        for d in docs
        if d["kind"] == "ClusterRole"
        and d["metadata"]["name"] == f"{creds.AGENT_SA_NAME}-cluster-supplement"
    )
    granted = {
        (g, r) for rule in role["rules"] for g in rule["apiGroups"] for r in rule["resources"]
    }
    assert ("", "namespaces") in granted
    assert ("", "nodes") in granted
    assert ("storage.k8s.io", "storageclasses") in granted


@pytest.mark.parametrize(
    "forbidden_group",
    ["rbac.authorization.k8s.io", "admissionregistration.k8s.io"],
)
def test_rbac_never_grants_self_escalation_or_admission_control(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, forbidden_group: str
) -> None:
    """Without these two omissions every other limit is advisory: the agent
    could grant itself more, or delete the policy denying privileged pods."""
    docs = _rbac_docs(tmp_path, monkeypatch)
    for role in (d for d in docs if d["kind"] == "ClusterRole"):
        for rule in role["rules"]:
            assert forbidden_group not in rule["apiGroups"]


# -- token minting -----------------------------------------------------------


def test_mint_agent_token_requests_a_bounded_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_kubectl(monkeypatch)

    assert creds.mint_agent_token(1500, "kind-c1") == _TOKEN

    assert calls[0] == [
        "kubectl",
        "create",
        "token",
        creds.AGENT_SA_NAME,
        "--duration=1500s",
        "-n",
        creds.AGENT_NAMESPACE,
        "--context",
        "kind-c1",
    ]


@pytest.mark.parametrize(
    ("timeout_sec", "expected"),
    [
        (600.0, 1500),  # the default: timeout plus slack, under the cap
        (10.0, 910),  # the slack is what keeps a short task's token from expiring mid-run
        (30000.0, 7200),  # capped, so a long run's credential is not left lying around
        (None, 7200),  # unbounded agent: the cap is the whole point
    ],
)
def test_token_ttl_for_adds_slack_and_caps_the_lifetime(
    timeout_sec: float | None, expected: int
) -> None:
    assert creds.token_ttl_for(timeout_sec) == expected


# -- kubeconfig rendering ----------------------------------------------------


def test_render_agent_kubeconfig_emits_one_cluster_and_no_exec_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_kubectl(monkeypatch)
    plan = NetworkPlan(docker_network="kind", rewrite_server="https://c1-control-plane:6443")

    path = creds.render_agent_kubeconfig(plan, tmp_path, user_fields=f"token: {_TOKEN}")

    text = path.read_text()
    config = yaml.safe_load(text)
    assert len(config["clusters"]) == 1
    assert len(config["users"]) == 1
    assert len(config["contexts"]) == 1
    assert config["clusters"][0]["cluster"]["server"] == "https://c1-control-plane:6443"
    # No exec-plugin block and no ADC anywhere: the container can never be
    # asked to shell out to a cloud credential helper it does not have. This
    # is also what makes a GKE kubeconfig usable in-container at all.
    assert "exec" not in config["users"][0]["user"]
    assert "exec:" not in text
    assert "application_default" not in text


def test_render_agent_kubeconfig_is_owner_readable_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_kubectl(monkeypatch)
    path = creds.render_agent_kubeconfig(NetworkPlan(), tmp_path, user_fields=f"token: {_TOKEN}")
    assert (path.stat().st_mode & 0o777) == 0o600


def test_render_agent_kubeconfig_keeps_the_context_server_without_a_rewrite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_kubectl(monkeypatch, server="https://34.1.2.3")
    path = creds.render_agent_kubeconfig(NetworkPlan(), tmp_path, user_fields="token: t")
    cluster = yaml.safe_load(path.read_text())["clusters"][0]["cluster"]
    assert cluster["server"] == "https://34.1.2.3"


def test_render_agent_kubeconfig_renders_tls_server_name_when_the_plan_sets_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_kubectl(monkeypatch)
    plan = NetworkPlan(
        rewrite_server="https://host.docker.internal:8443", tls_server_name="localhost"
    )
    path = creds.render_agent_kubeconfig(plan, tmp_path, user_fields="token: t")
    cluster = yaml.safe_load(path.read_text())["clusters"][0]["cluster"]
    assert cluster["tls-server-name"] == "localhost"


def test_render_agent_kubeconfig_pins_reads_to_the_plans_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The rendered CA and server must belong to the run's own cluster even if
    the ambient current-context was switched after provisioning."""
    calls = _patch_kubectl(monkeypatch)
    creds.render_agent_kubeconfig(
        NetworkPlan(kubectl_context="kind-c1"), tmp_path, user_fields="token: t"
    )
    assert calls
    for argv in calls:
        assert argv[-2:] == ["--context", "kind-c1"]


def test_render_agent_kubeconfig_refuses_without_a_ca(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_kubectl(monkeypatch, ca="")
    with pytest.raises(SandboxError, match="CA"):
        creds.render_agent_kubeconfig(NetworkPlan(), tmp_path, user_fields="token: t")


def test_render_agent_kubeconfig_refuses_without_a_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_kubectl(monkeypatch, server="")
    with pytest.raises(SandboxError, match="server URL"):
        creds.render_agent_kubeconfig(NetworkPlan(), tmp_path, user_fields="token: t")


# -- the whole provisioning path ---------------------------------------------


def test_provision_gives_the_agent_a_service_account_token_not_a_certificate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The point of the module: the credential in the container is a scoped,
    short-lived SA token, so the RBAC boundary does real work."""
    _patch_kubectl(monkeypatch)

    path = creds.provision_agent_credentials(_PINNED, tmp_path, token_ttl_sec=1500)

    user = yaml.safe_load(path.read_text())["users"][0]["user"]
    assert user == {"token": _TOKEN}
    assert "client-certificate-data" not in user


def test_provision_refuses_before_writing_when_the_kubeconfig_cannot_render(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A context with no embedded CA fails the final render; by then the
    policies and RBAC are already on the cluster with nothing recording them.
    The preflight must surface that refusal before the first apply."""
    calls = _patch_kubectl(monkeypatch, ca="")

    with pytest.raises(SandboxError, match="certificate-authority-data"):
        creds.provision_agent_credentials(_PINNED, tmp_path, token_ttl_sec=1500)

    assert not any("apply" in argv for argv in calls)
    assert not (tmp_path / "kubeconfig").exists()


def test_provision_refuses_to_fall_back_to_the_admin_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A silent fallback would look identical in the results while having no
    RBAC boundary at all, so failing to mint must fail the run."""
    monkeypatch.delenv(creds.ALLOW_ADMIN_ENV, raising=False)
    _patch_kubectl(monkeypatch, mint_fails=True)

    with pytest.raises(SandboxError, match=creds.ALLOW_ADMIN_ENV):
        creds.provision_agent_credentials(_PINNED, tmp_path, token_ttl_sec=1500)

    assert not (tmp_path / "kubeconfig").exists()


def test_provision_falls_back_to_the_admin_cert_only_when_told_to(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(creds.ALLOW_ADMIN_ENV, "1")
    _patch_kubectl(monkeypatch, mint_fails=True)

    path = creds.provision_agent_credentials(_PINNED, tmp_path, token_ttl_sec=1500)

    user = yaml.safe_load(path.read_text())["users"][0]["user"]
    assert user["client-certificate-data"] == "Y2VydA=="
    assert "token" not in user


def test_provision_refuses_the_fallback_for_an_exec_plugin_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A GKE context has no static certificate to copy, and its exec plugin
    could not run inside the container anyway."""
    monkeypatch.setenv(creds.ALLOW_ADMIN_ENV, "1")
    _patch_kubectl(monkeypatch, mint_fails=True, cert="", key="")

    with pytest.raises(SandboxError, match="exec"):
        creds.provision_agent_credentials(_PINNED, tmp_path, token_ttl_sec=1500)


def test_provision_tears_down_when_the_fallback_render_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The exec-plugin refusal fires after the policies (and possibly the
    identity) are on the cluster, and the completed spec the run-end teardown
    keys off never comes to exist — so provisioning must clean up itself."""
    monkeypatch.setenv(creds.ALLOW_ADMIN_ENV, "1")
    calls = _patch_kubectl(monkeypatch, mint_fails=True, cert="", key="")

    with pytest.raises(SandboxError, match="exec"):
        creds.provision_agent_credentials(_PINNED, tmp_path, token_ttl_sec=1500)

    deleted = [argv for argv in calls if "delete" in argv]
    assert any(creds._POLICY_BINDING_KIND in argv for argv in deleted)
    assert any(creds.AGENT_NAMESPACE in argv for argv in deleted)


# -- pod security ------------------------------------------------------------


def _ns(name: str, **labels: str) -> dict:
    return {"metadata": {"name": name, "labels": labels}}


def _policy_docs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    _patch_kubectl(monkeypatch)
    creds.enforce_pod_security(tmp_path)
    text = (tmp_path / "bench-agent-pod-security.yaml").read_text()
    return [d for d in yaml.safe_load_all(text) if d]


def _doc(docs: list[dict], kind: str, name: str) -> dict:
    """Pick one document out of the multi-doc manifest by kind and name."""
    return next(d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name)


def test_pod_security_policy_denies_the_observed_escape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The proposal's first incident was a privileged pod with a hostPath mount
    reading the bench checkout off the node's disk. Every ingredient of it must
    have a validation that rejects it."""
    docs = _policy_docs(tmp_path, monkeypatch)
    policy = _doc(docs, "ValidatingAdmissionPolicy", "bench-agent-pod-security")
    expressions = " ".join(v["expression"] for v in policy["spec"]["validations"])

    assert "hostPath" in expressions
    assert "privileged" in expressions
    assert "hostNetwork" in expressions
    assert "hostPID" in expressions
    assert "hostIPC" in expressions


def test_pod_security_policy_denies_rather_than_warns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Detection is a tripwire; this is meant to be a boundary. A binding in
    Warn mode would let the escape through and merely mention it."""
    docs = _policy_docs(tmp_path, monkeypatch)
    binding = _doc(docs, "ValidatingAdmissionPolicyBinding", "bench-agent-pod-security")
    policy = _doc(docs, "ValidatingAdmissionPolicy", "bench-agent-pod-security")

    assert binding["spec"]["validationActions"] == ["Deny"]
    assert policy["spec"]["failurePolicy"] == "Fail"


def test_pod_security_policy_also_matches_the_ephemeral_container_subresource(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``pods/ephemeralcontainers`` is a distinct subresource, so a rule naming
    only ``pods`` never sees ``kubectl debug --profile=sysadmin`` — and the
    ephemeral-container validation below it would be dead code."""
    docs = _policy_docs(tmp_path, monkeypatch)
    policy = _doc(docs, "ValidatingAdmissionPolicy", "bench-agent-pod-security")
    resources = policy["spec"]["matchConstraints"]["resourceRules"][0]["resources"]

    assert "pods" in resources
    assert "pods/ephemeralcontainers" in resources


def test_pod_security_policy_exempts_the_clusters_own_components(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Control-plane and storage components legitimately run privileged with
    host mounts; enforcing on them would break the cluster, not the agent."""
    docs = _policy_docs(tmp_path, monkeypatch)
    binding = _doc(docs, "ValidatingAdmissionPolicyBinding", "bench-agent-pod-security")
    expr = binding["spec"]["matchResources"]["namespaceSelector"]["matchExpressions"][0]

    assert expr["key"] == "kubernetes.io/metadata.name"
    assert expr["operator"] == "NotIn"
    assert "kube-system" in expr["values"]


def test_pod_security_policy_does_not_exempt_the_harness_namespace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The agent holds ``edit`` cluster-wide, so it can create pods in
    ``bench-system``. Exempting that namespace would leave it a namespace it
    can reach and the policy cannot see — a privileged hostPath pod one
    ``-n bench-system`` away."""
    docs = _policy_docs(tmp_path, monkeypatch)
    binding = _doc(docs, "ValidatingAdmissionPolicyBinding", "bench-agent-pod-security")
    expr = binding["spec"]["matchResources"]["namespaceSelector"]["matchExpressions"][0]

    assert creds.AGENT_NAMESPACE not in expr["values"]


def test_namespace_guard_denies_claiming_an_exempt_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exemptions are by name and the agent can create namespaces, so without
    this it could ``create ns gmp-system`` (absent on kind) and deploy there."""
    docs = _policy_docs(tmp_path, monkeypatch)
    policy = _doc(docs, "ValidatingAdmissionPolicy", "bench-agent-namespace-guard")
    binding = _doc(docs, "ValidatingAdmissionPolicyBinding", "bench-agent-namespace-guard")
    rule = policy["spec"]["matchConstraints"]["resourceRules"][0]
    expression = policy["spec"]["validations"][0]["expression"]

    assert rule["resources"] == ["namespaces"]
    assert "CREATE" in rule["operations"]
    assert "'kube-system'" in expression
    assert "'gmp-system'" in expression
    assert binding["spec"]["validationActions"] == ["Deny"]
    # No namespaceSelector: the pod policy's ``NotIn`` would otherwise exempt
    # the very namespace creation being guarded.
    assert "namespaceSelector" not in binding["spec"].get("matchResources", {})


def test_pod_security_policy_exempts_namespaces_the_cluster_manages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A name list goes stale. A plain GKE run turned up four managed
    namespaces this one had never heard of, all inside the deny scope, one of
    them the home of the DRA driver's privileged DaemonSet on clusters that
    use it. The addon manager's own label covers the ones we cannot name."""
    docs = _policy_docs(tmp_path, monkeypatch)
    binding = _doc(docs, "ValidatingAdmissionPolicyBinding", "bench-agent-pod-security")
    exprs = binding["spec"]["matchResources"]["namespaceSelector"]["matchExpressions"]
    managed = next(e for e in exprs if e["key"] == "addonmanager.kubernetes.io/mode")

    assert managed["operator"] == "DoesNotExist"


def test_namespace_guard_denies_claiming_the_managed_label(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The label half of the exemption is mutable in a way the name half is
    not: the agent holds ``patch`` on namespaces, so without a guard on UPDATE
    it could label one it already owns and stop every pod in it being
    checked."""
    docs = _policy_docs(tmp_path, monkeypatch)
    policy = _doc(docs, "ValidatingAdmissionPolicy", "bench-agent-namespace-guard")
    rule = policy["spec"]["matchConstraints"]["resourceRules"][0]
    expressions = " ".join(v["expression"] for v in policy["spec"]["validations"])

    assert rule["operations"] == ["CREATE", "UPDATE"]
    assert "addonmanager.kubernetes.io/mode" in expressions


def test_namespace_guard_applies_only_to_the_agents_own_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A cluster add-on recreating its own namespace must not be denied by a
    policy that fails closed. Safe to scope by user here — unlike a pod, a
    namespace is always created by whoever asked, never by a controller acting
    on the agent's behalf."""
    docs = _policy_docs(tmp_path, monkeypatch)
    policy = _doc(docs, "ValidatingAdmissionPolicy", "bench-agent-namespace-guard")
    condition = policy["spec"]["matchConditions"][0]["expression"]

    assert f"system:serviceaccount:{creds.AGENT_NAMESPACE}:{creds.AGENT_SA_NAME}" in condition


def _exempt_guard_resources(docs: list[dict], operation: str) -> set[str]:
    """Every ``<group>/<resource>`` the exempt-namespace guard matches for one operation."""
    policy = _doc(docs, "ValidatingAdmissionPolicy", "bench-agent-exempt-namespace-guard")
    return {
        f"{rule['apiGroups'][0]}/{resource}"
        for rule in policy["spec"]["matchConstraints"]["resourceRules"]
        if operation in rule["operations"]
        for resource in rule["resources"]
    }


def test_exempt_namespaces_deny_the_agents_own_workloads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The hole a probe found: the pod policy skips these namespaces, but
    ``edit`` is bound cluster-wide, so ``kubectl run --privileged -n
    kube-system`` was admitted on a cluster carrying the full policy set. The
    exemption is only safe if the agent cannot write there at all."""
    docs = _policy_docs(tmp_path, monkeypatch)
    policy = _doc(docs, "ValidatingAdmissionPolicy", "bench-agent-exempt-namespace-guard")

    assert policy["spec"]["failurePolicy"] == "Fail"
    # Nothing to evaluate: being matched at all is the violation.
    assert [v["expression"] for v in policy["spec"]["validations"]] == ["false"]
    assert "/pods" in _exempt_guard_resources(docs, "CREATE")
    for suffix in ("by-name", "by-label"):
        binding = _doc(
            docs,
            "ValidatingAdmissionPolicyBinding",
            f"bench-agent-exempt-namespace-guard-{suffix}",
        )
        assert binding["spec"]["validationActions"] == ["Deny"]


def test_exempt_namespace_guard_covers_every_kind_that_makes_a_pod(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Denying ``pods`` alone would be one ``create deployment`` from useless:
    the ReplicaSet controller makes that pod under an identity of its own, so
    a username-scoped rule never sees it. The workload object is where the
    agent's own name is still on the request."""
    matched = _exempt_guard_resources(_policy_docs(tmp_path, monkeypatch), "CREATE")

    assert {
        "apps/deployments",
        "apps/daemonsets",
        "apps/statefulsets",
        "apps/replicasets",
        "batch/jobs",
        "batch/cronjobs",
        "/replicationcontrollers",
    } <= matched


def test_exempt_namespace_guard_covers_exec_into_the_clusters_own_pods(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``edit`` grants exec, and these are the namespaces whose pods are
    legitimately privileged. A shell in kube-proxy is the same escape by a
    longer route, and exec arrives as CONNECT, not CREATE."""
    matched = _exempt_guard_resources(_policy_docs(tmp_path, monkeypatch), "CONNECT")

    assert {"/pods/exec", "/pods/attach", "/pods/portforward"} <= matched


def test_exempt_namespace_guard_selects_exactly_what_the_pod_policy_skips(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The two must stay exact complements, or a namespace falls through both.
    A ``namespaceSelector`` ANDs its expressions, so the inverse of "not by
    name AND not by label" needs one binding per half."""
    docs = _policy_docs(tmp_path, monkeypatch)
    skipped = _doc(docs, "ValidatingAdmissionPolicyBinding", "bench-agent-pod-security")
    skipped_exprs = skipped["spec"]["matchResources"]["namespaceSelector"]["matchExpressions"]
    by_name, by_label = (
        _doc(docs, "ValidatingAdmissionPolicyBinding", f"bench-agent-exempt-namespace-guard-{s}")[
            "spec"
        ]["matchResources"]["namespaceSelector"]["matchExpressions"][0]
        for s in ("by-name", "by-label")
    )

    name_half = next(e for e in skipped_exprs if e["operator"] == "NotIn")
    label_half = next(e for e in skipped_exprs if e["operator"] == "DoesNotExist")
    assert (by_name["key"], by_name["operator"]) == (name_half["key"], "In")
    assert by_name["values"] == name_half["values"]
    assert (by_label["key"], by_label["operator"]) == (label_half["key"], "Exists")


def test_enforce_pod_security_labels_ordinary_namespaces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _patch_kubectl(monkeypatch, namespaces={"items": [_ns("default"), _ns("kube-system")]})

    creds.enforce_pod_security(tmp_path)

    labelled = [c for c in calls if "label" in c]
    assert len(labelled) == 1
    assert labelled[0][:4] == ["kubectl", "label", "namespace", "default"]
    assert "pod-security.kubernetes.io/enforce=baseline" in labelled[0]


def test_enforce_pod_security_leaves_a_declared_level_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One task's verifier asserts ``enforce=restricted`` on its own namespace.
    Overwriting it would fail the task this control exists to protect."""
    calls = _patch_kubectl(
        monkeypatch,
        namespaces={
            "items": [_ns("hello-app", **{"pod-security.kubernetes.io/enforce": "restricted"})]
        },
    )

    creds.enforce_pod_security(tmp_path)

    assert [c for c in calls if "label" in c] == []


def test_enforce_pod_security_leaves_the_clusters_own_namespaces_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Measured on GKE: the labeller stamped ``enforce=baseline`` on exactly
    the managed namespaces missing from the name list. The label is what the
    cluster itself uses to say which namespaces are its to run."""
    calls = _patch_kubectl(
        monkeypatch,
        namespaces={
            "items": [
                _ns("default"),
                _ns("gke-managed-cim", **{"addonmanager.kubernetes.io/mode": "Reconcile"}),
                _ns("gmp-public", **{"addonmanager.kubernetes.io/mode": "Reconcile"}),
            ]
        },
    )

    creds.enforce_pod_security(tmp_path)

    labelled = [c for c in calls if "label" in c]
    assert len(labelled) == 1
    assert labelled[0][:4] == ["kubectl", "label", "namespace", "default"]


def test_enforce_pod_security_pins_every_call_to_the_runs_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _patch_kubectl(monkeypatch, namespaces={"items": [_ns("default")]})

    creds.enforce_pod_security(tmp_path, "vcluster-c1")

    assert calls
    for argv in calls:
        assert argv[-2:] == ["--context", "vcluster-c1"]


# -- the cluster version floor -----------------------------------------------


def test_enforce_pod_security_refuses_a_cluster_too_old_for_the_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``admissionregistration.k8s.io/v1`` reached GA in 1.30; a 1.29 apiserver
    serves only ``v1beta1``. Left unchecked the apply dies with kubectl's ``no
    matches for kind``, which reads like a typo in our own manifest."""
    calls = _patch_kubectl(monkeypatch, policy_api=False)

    with pytest.raises(SandboxError, match=creds._MIN_CLUSTER_VERSION):
        creds.enforce_pod_security(tmp_path)

    # Named before anything is written, so the operator is not left wondering
    # which half of the provisioning got applied.
    assert not any("apply" in c for c in calls)


def test_the_version_refusal_is_not_the_admin_escape_hatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The hatch exists for an operator whose credential cannot write
    cluster-scoped objects. No credential makes a 1.29 apiserver serve a v1
    policy, so letting the run continue would just skip the backstop."""
    monkeypatch.setenv(creds.ALLOW_ADMIN_ENV, "1")
    _patch_kubectl(monkeypatch, policy_api=False)

    with pytest.raises(SandboxError, match=creds._MIN_CLUSTER_VERSION):
        creds.provision_agent_credentials(_PINNED, tmp_path, token_ttl_sec=1500)


# -- pods that predate the policy --------------------------------------------


def _pod(namespace: str, name: str, **spec: object) -> dict:
    return {"metadata": {"namespace": namespace, "name": name}, "spec": spec}


_PRIVILEGED = {"containers": [{"name": "c", "securityContext": {"privileged": True}}]}


def _shell_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    pods: dict,
    namespaces: dict | None = None,
) -> list[dict]:
    _patch_kubectl(monkeypatch, pods=pods, namespaces=namespaces)
    creds.enforce_pod_security(tmp_path)
    text = (tmp_path / "bench-agent-nonconformant-pods.yaml").read_text()
    return [d for d in yaml.safe_load_all(text) if d]


def _guard_expression(docs: list[dict]) -> str:
    policy = _doc(docs, "ValidatingAdmissionPolicy", creds._NONCONFORMANT_GUARD_NAME)
    return " ".join(v["expression"] for v in policy["spec"]["validations"])


def test_the_shell_guard_names_pods_the_policy_arrived_too_late_for(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The deployer runs before credentials are provisioned, and fixtures like
    ``opa-remediation`` deploy privileged pods on purpose — remediating them is
    the task. Admission never saw those creates and cannot retract them, so the
    agent holding cluster-wide ``pods/exec`` is node root by a route the
    pod-security policy is blind to."""
    docs = _shell_guard(
        tmp_path,
        monkeypatch,
        pods={
            "items": [
                _pod("team-alpha", "cache", **_PRIVILEGED),
                _pod("default", "web", containers=[{"name": "c"}]),
            ]
        },
    )

    expression = _guard_expression(docs)
    assert "'team-alpha/cache'" in expression
    assert "default/web" not in expression


def test_the_shell_guard_covers_every_way_into_a_running_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``exec`` is the obvious one; ``attach`` reaches the same process and
    ``port-forward`` reaches anything it is listening on."""
    docs = _shell_guard(
        tmp_path, monkeypatch, pods={"items": [_pod("team-alpha", "cache", **_PRIVILEGED)]}
    )
    policy = _doc(docs, "ValidatingAdmissionPolicy", creds._NONCONFORMANT_GUARD_NAME)
    rule = policy["spec"]["matchConstraints"]["resourceRules"][0]

    assert rule["operations"] == ["CONNECT"]
    assert set(rule["resources"]) == {"pods/exec", "pods/attach", "pods/portforward"}
    assert _doc(docs, "ValidatingAdmissionPolicyBinding", creds._NONCONFORMANT_GUARD_NAME)["spec"][
        "validationActions"
    ] == ["Deny"]


def test_the_shell_guard_applies_only_to_the_agents_own_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unlike the pod-security policy, this one is username-scoped: the pods it
    names are the fixture's own, and the operator and the task's controllers
    must keep being able to reach them."""
    docs = _shell_guard(
        tmp_path, monkeypatch, pods={"items": [_pod("team-alpha", "cache", **_PRIVILEGED)]}
    )
    policy = _doc(docs, "ValidatingAdmissionPolicy", creds._NONCONFORMANT_GUARD_NAME)
    conditions = policy["spec"]["matchConditions"]

    assert len(conditions) == 1
    assert creds._AGENT_USERNAME in conditions[0]["expression"]


@pytest.mark.parametrize(
    "spec",
    [
        {"hostNetwork": True, "containers": [{"name": "c"}]},
        {"hostPID": True, "containers": [{"name": "c"}]},
        {"hostIPC": True, "containers": [{"name": "c"}]},
        {"volumes": [{"name": "root", "hostPath": {"path": "/"}}], "containers": [{"name": "c"}]},
        {"initContainers": [{"name": "i", "securityContext": {"privileged": True}}]},
        {"ephemeralContainers": [{"name": "e", "securityContext": {"privileged": True}}]},
    ],
)
def test_the_shell_guard_reads_the_same_pod_spec_the_policy_would_have(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spec: dict
) -> None:
    """The scan is kept in lockstep with the policy's CEL, not with PSA
    ``baseline`` — a pod it skips must be one the policy would have admitted,
    or the guard's coverage claim is a lie."""
    docs = _shell_guard(
        tmp_path, monkeypatch, pods={"items": [_pod("team-alpha", "cache", **spec)]}
    )

    assert "'team-alpha/cache'" in _guard_expression(docs)


def test_the_shell_guard_ignores_pods_the_agent_already_cannot_reach(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every namespace the exempt-namespace guard covers — by name or by the
    addon manager's label — is already closed to the agent on all four verbs.
    Naming ``kube-system``'s privileged pods here would bury the ones that
    actually needed this."""
    docs = _shell_guard(
        tmp_path,
        monkeypatch,
        pods={
            "items": [
                _pod("kube-system", "kube-proxy", **_PRIVILEGED),
                _pod("gke-managed-cim", "collector", **_PRIVILEGED),
            ]
        },
        namespaces={
            "items": [
                _ns("kube-system"),
                _ns("gke-managed-cim", **{creds._ADDON_MANAGER_LABEL: "Reconcile"}),
            ]
        },
    )

    assert _guard_expression(docs) == "true"


def test_the_shell_guard_is_applied_even_with_nothing_to_deny(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Nothing here is torn down between runs, so a reused cluster would
    otherwise keep the previous run's list and refuse a shell into a pod that is
    long gone. An empty CEL list literal has no element type to infer either,
    and a policy that fails to compile under ``failurePolicy: Fail`` denies
    every exec the agent attempts — the opposite of inert."""
    calls = _patch_kubectl(monkeypatch)

    creds.enforce_pod_security(tmp_path)

    assert any(_applies(c, "bench-agent-nonconformant-pods.yaml") for c in calls)
    text = (tmp_path / "bench-agent-nonconformant-pods.yaml").read_text()
    docs = [d for d in yaml.safe_load_all(text) if d]
    assert _guard_expression(docs) == "true"


def test_the_nonconformant_scan_looks_at_every_namespace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without ``-A`` the listing comes from the kubeconfig's current namespace,
    which for a cluster-wide question is silently the wrong answer."""
    calls = _patch_kubectl(monkeypatch)

    creds.enforce_pod_security(tmp_path)

    pod_gets = [c for c in calls if "get" in c and c[c.index("get") + 1] == "pods"]
    assert len(pod_gets) == 1
    assert "-A" in pod_gets[0]


def test_provision_enforces_pod_security_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A task author who never heard of the key still gets the control."""
    calls = _patch_kubectl(monkeypatch)

    creds.provision_agent_credentials(_PINNED, tmp_path, token_ttl_sec=1500)

    assert any(_applies(c, "bench-agent-pod-security.yaml") for c in calls)


def test_provision_honours_the_privileged_opt_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A task whose subject matter *is* privileged workloads can opt out, and
    then nothing pod-security-related is applied at all."""
    calls = _patch_kubectl(monkeypatch)

    creds.provision_agent_credentials(
        _PINNED,
        tmp_path,
        token_ttl_sec=1500,
        pod_security=creds.POD_SECURITY_PRIVILEGED,
    )

    assert not any(_applies(c, "bench-agent-pod-security.yaml") for c in calls)
    assert [c for c in calls if "label" in c] == []


def test_provision_refuses_a_cluster_no_provider_vouched_for(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unpinned plan means the no-op deployer, so 'the cluster' is whatever
    the operator's kubeconfig last pointed at. Provisioning writes a cluster-wide
    Deny policy and ClusterRoleBindings; doing that unasked to someone's real
    cluster is not acceptable."""
    monkeypatch.delenv(creds.ALLOW_AMBIENT_ENV, raising=False)
    calls = _patch_kubectl(monkeypatch)

    with pytest.raises(SandboxError, match=creds.ALLOW_AMBIENT_ENV):
        creds.provision_agent_credentials(NetworkPlan(), tmp_path, token_ttl_sec=1500)

    assert not any("apply" in c for c in calls)


def test_provision_uses_the_ambient_cluster_only_when_told_to(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(creds.ALLOW_AMBIENT_ENV, "1")
    _patch_kubectl(monkeypatch)

    path = creds.provision_agent_credentials(NetworkPlan(), tmp_path, token_ttl_sec=1500)

    assert yaml.safe_load(path.read_text())["users"][0]["user"] == {"token": _TOKEN}


def test_provision_fails_loud_when_pod_security_cannot_be_applied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The policy apply is the first cluster-scoped write, so it is what fails
    for an operator who cannot create cluster-scoped objects. Running the agent
    anyway would leave the observed escape undenied."""
    monkeypatch.delenv(creds.ALLOW_ADMIN_ENV, raising=False)
    calls: list[list[str]] = []
    _patch_kubectl(monkeypatch, calls=calls)
    real = kubectl.run

    def fail_the_policy_apply(argv, **kwargs):
        if _applies(argv, "bench-agent-pod-security.yaml"):
            raise SubprocessError(argv, 1, stderr="forbidden: cannot create policies")
        return real(argv, **kwargs)

    monkeypatch.setattr(kubectl, "run", fail_the_policy_apply)

    with pytest.raises(SandboxError, match="pod security"):
        creds.provision_agent_credentials(_PINNED, tmp_path, token_ttl_sec=1500)


def test_the_admin_escape_hatch_also_covers_the_pod_security_apply(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One switch, because both failures have one cause: an operator who cannot
    create cluster roles cannot create an admission policy either. Before this,
    the policy apply raised first and the hatch was unreachable."""
    monkeypatch.setenv(creds.ALLOW_ADMIN_ENV, "1")
    _patch_kubectl(monkeypatch)
    real = kubectl.run

    def fail_the_policy_apply(argv, **kwargs):
        if _applies(argv, "bench-agent-pod-security.yaml"):
            raise SubprocessError(argv, 1, stderr="forbidden: cannot create policies")
        return real(argv, **kwargs)

    monkeypatch.setattr(kubectl, "run", fail_the_policy_apply)

    path = creds.provision_agent_credentials(_PINNED, tmp_path, token_ttl_sec=1500)

    # The scoped token still gets minted; only the pod-security half was lost.
    assert yaml.safe_load(path.read_text())["users"][0]["user"] == {"token": _TOKEN}


# -- teardown ----------------------------------------------------------------


def _delete_kinds(calls: list[list[str]]) -> list[str]:
    """The kind argument of every ``kubectl delete`` in ``calls``, in order."""
    return [argv[argv.index("delete") + 1] for argv in calls if "delete" in argv]


def test_teardown_inventory_matches_the_manifests() -> None:
    """The teardown name lists cannot drift from the manifests they mirror.

    A policy or binding added to the manifests without a row in the teardown
    inventory would survive every run on a reused cluster; this holds the two
    in lockstep so the omission fails the suite instead.
    """
    docs = [d for d in yaml.safe_load_all(creds._POD_SECURITY_POLICY_MANIFEST) if d]
    docs += [d for d in yaml.safe_load_all(creds._render_nonconformant_pod_guard([])) if d]
    by_kind: dict[str, set[str]] = {}
    for doc in docs:
        by_kind.setdefault(doc["kind"], set()).add(doc["metadata"]["name"])
    assert by_kind["ValidatingAdmissionPolicy"] == set(creds._POLICY_NAMES)
    assert by_kind["ValidatingAdmissionPolicyBinding"] == set(creds._POLICY_BINDING_NAMES)

    rbac = [d for d in yaml.safe_load_all(creds._RBAC_MANIFEST) if d]
    rbac_by_kind: dict[str, set[str]] = {}
    for doc in rbac:
        rbac_by_kind.setdefault(doc["kind"], set()).add(doc["metadata"]["name"])
    assert rbac_by_kind["ClusterRoleBinding"] == set(creds._CLUSTER_ROLE_BINDING_NAMES)
    assert rbac_by_kind["ClusterRole"] == set(creds._CLUSTER_ROLE_NAMES)
    assert rbac_by_kind["Namespace"] == {creds.AGENT_NAMESPACE}
    assert rbac_by_kind["ServiceAccount"] == {creds.AGENT_SA_NAME}


def test_teardown_deletes_everything_bindings_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bindings go first — a binding is what makes a policy enforce, so the
    cluster stops denying anyone the moment they are gone — and the namespace
    goes last, waited on, so a reused cluster's next apply cannot race it."""
    calls = _patch_kubectl(monkeypatch)

    assert creds.teardown_agent_credentials("kind-c1") is True

    assert _delete_kinds(calls) == [
        creds._POLICY_BINDING_KIND,
        creds._POLICY_KIND,
        "clusterrolebinding",
        "clusterrole",
        "namespace",
    ]
    deletes = [argv for argv in calls if "delete" in argv]
    for argv in deletes:
        assert "--ignore-not-found" in argv
        assert argv[-2:] == ["--context", "kind-c1"]
    for name in creds._POLICY_BINDING_NAMES:
        assert name in deletes[0]
    for name in creds._POLICY_NAMES:
        assert name in deletes[1]
    assert creds.AGENT_NAMESPACE in deletes[-1]
    assert "--wait=false" not in deletes[-1]


def test_teardown_unlabels_only_the_namespaces_it_marked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PSA label someone else set never carries the marker and is never
    removed — the labeller skips namespaces that already declare a level, so
    the marker is a faithful record of exactly what enforcement wrote."""
    namespaces = {
        "items": [
            _ns(
                "marked",
                **{creds._PSA_MANAGED_LABEL: "true", creds._PSA_ENFORCE_LABEL: "baseline"},
            ),
            _ns("operator-own", **{creds._PSA_ENFORCE_LABEL: "restricted"}),
            _ns("plain"),
        ]
    }
    calls = _patch_kubectl(monkeypatch, namespaces=namespaces)

    assert creds.teardown_agent_credentials("kind-c1") is True

    labelled = [argv for argv in calls if "label" in argv]
    assert len(labelled) == 1
    assert labelled[0][:4] == ["kubectl", "label", "namespace", "marked"]
    for key in creds._PSA_LABEL_KEYS:
        assert f"{key}-" in labelled[0]
    assert f"{creds._PSA_MANAGED_LABEL}-" in labelled[0]


def test_teardown_is_best_effort_and_reports_residue(monkeypatch: pytest.MonkeyPatch) -> None:
    """One failed delete must not stop the rest: every object that CAN come
    off the cluster does, and the return value says residue remains."""
    calls = _patch_kubectl(monkeypatch, delete_fails={creds._POLICY_BINDING_KIND})

    assert creds.teardown_agent_credentials("kind-c1") is False

    # The failed first step did not short-circuit the remaining four.
    assert _delete_kinds(calls)[-1] == "namespace"


def test_teardown_survives_an_unlistable_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    """Teardown never raises: both its callers sit on paths where a second
    failure must not eclipse the first."""
    _patch_kubectl(monkeypatch)

    def refuse_lists(argv, **kwargs):
        raise SubprocessError(argv, 1, stderr="connection refused")

    monkeypatch.setattr(kubectl, "run", refuse_lists)

    assert creds.teardown_agent_credentials("kind-c1") is False


def test_failed_provisioning_cleans_up_its_partial_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A mint failure raises AFTER pod security and the identity landed on the
    cluster, and the completed spec that would have carried their context to
    the run-end teardown never exists — so provisioning removes them itself,
    keeping the original error."""
    calls = _patch_kubectl(monkeypatch, mint_fails=True)

    with pytest.raises(SandboxError, match="scoped ServiceAccount"):
        creds.provision_agent_credentials(_PINNED, tmp_path, token_ttl_sec=1500)

    kinds = _delete_kinds(calls)
    assert creds._POLICY_BINDING_KIND in kinds
    assert "namespace" in kinds


def test_marker_label_rides_along_with_enforcement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Enforcement stamps the marker teardown keys off, on the same call that
    sets the PSA levels — one write, so they cannot come apart."""
    calls = _patch_kubectl(monkeypatch, namespaces={"items": [_ns("default")]})

    creds.enforce_pod_security(tmp_path, "kind-c1")

    labelled = [argv for argv in calls if "label" in argv]
    assert len(labelled) == 1
    assert f"{creds._PSA_MANAGED_LABEL}=true" in labelled[0]
