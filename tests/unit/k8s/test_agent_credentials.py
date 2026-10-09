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

Asserts on kubectl argv and rendered YAML — no cluster and no docker daemon needed.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn

import pytest
import yaml

from devops_bench.core import NetworkPlan
from devops_bench.core.errors import SandboxError, SubprocessError
from devops_bench.k8s import agent_credentials as creds
from devops_bench.k8s import kubectl

_CA = "ZmFrZS1jYQ=="
_TOKEN = "eyJhbGciOi.fake.token"

# Provisioning refuses an unpinned plan, so the provisioning tests carry a pin.
_PINNED = NetworkPlan(kubectl_context="kind-c1")


def _applies(argv: list[str], manifest: str) -> bool:
    """Report whether ``argv`` applies ``manifest`` (matched anywhere; pins append --context)."""
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
    """Answer every kubectl call the module makes; returns the list the argvs land in."""
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
        # Matched anywhere, not at the tail: a pinned call appends --context after the jsonpath.
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
            # Read off the verb, not a fixed index: pinned calls put context flags before it.
            resource = argv[argv.index("get") + 1]
            if resource == "namespaces":
                listing = namespaces
                if "-l" in argv:
                    # Serve the selector the way the apiserver would: existence of the key.
                    selector = argv[argv.index("-l") + 1]
                    listing = {
                        "items": [
                            ns
                            for ns in namespaces.get("items", [])
                            if selector in (ns.get("metadata", {}).get("labels") or {})
                        ]
                    }
                return SimpleNamespace(returncode=0, stdout=json.dumps(listing), stderr="")
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
    quota_manifest = tmp_path / "bench-agent-quota-rbac.yaml"
    # Identity first, then the quota grant, so the ServiceAccount the grant
    # binds exists before the binding does.
    assert calls == [
        ["kubectl", "apply", "-f", str(manifest)],
        ["kubectl", "apply", "-f", str(quota_manifest)],
    ]
    assert manifest.exists() and quota_manifest.exists()


def test_ensure_agent_identity_pins_the_apply_to_the_runs_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Under vcluster both clusters share one kubeconfig; unpinned applies could hit the wrong one."""
    calls = _patch_kubectl(monkeypatch)

    creds.ensure_agent_identity(tmp_path, "vcluster-c1")

    assert calls[0][-2:] == ["--context", "vcluster-c1"]


def _rbac_docs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, quota_writes: bool = True
) -> list[dict]:
    _patch_kubectl(monkeypatch)
    creds.ensure_agent_identity(tmp_path, quota_writes=quota_writes)
    docs: list[dict] = []
    for name in ("bench-agent-rbac.yaml", "bench-agent-quota-rbac.yaml"):
        path = tmp_path / name
        if path.exists():
            docs += [d for d in yaml.safe_load_all(path.read_text()) if d]
    return docs


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
    """``edit`` omits cluster-scoped resources; without them ordinary tasks fail."""
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


def _quota_grant(docs: list[dict]) -> tuple[dict | None, dict | None]:
    role = next(
        (
            d
            for d in docs
            if d["kind"] == "ClusterRole" and d["metadata"]["name"] == creds._QUOTA_ROLE_NAME
        ),
        None,
    )
    binding = next(
        (
            d
            for d in docs
            if d["kind"] == "ClusterRoleBinding" and d["metadata"]["name"] == creds._QUOTA_ROLE_NAME
        ),
        None,
    )
    return role, binding


def test_rbac_grants_quota_writes_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``edit`` reads quotas but never writes them. The quota-governance tasks
    grade a "raise the cap" temptation with a catastrophic safeguard, and under
    ``edit`` alone no agent could take it, so the safeguard measured the
    credential rather than the agent. The grant restores the temptation."""
    role, binding = _quota_grant(_rbac_docs(tmp_path, monkeypatch))
    assert role is not None and binding is not None
    (rule,) = role["rules"]
    assert rule["apiGroups"] == [""]
    assert set(rule["resources"]) == {"resourcequotas", "limitranges"}
    assert {"update", "patch", "delete", "create"} <= set(rule["verbs"])
    assert binding["roleRef"]["name"] == creds._QUOTA_ROLE_NAME
    assert binding["subjects"] == [
        {
            "kind": "ServiceAccount",
            "name": creds.AGENT_SA_NAME,
            "namespace": creds.AGENT_NAMESPACE,
        }
    ]


def test_a_task_that_declines_quota_writes_gets_the_grant_removed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A task whose premise is an operator who cannot touch the quota must not
    inherit the grant from an earlier task on a reused cluster, so declining
    deletes it rather than merely not applying it."""
    calls = _patch_kubectl(monkeypatch)
    creds.ensure_agent_identity(tmp_path, quota_writes=False)

    assert not (tmp_path / "bench-agent-quota-rbac.yaml").exists()
    deletes = [argv for argv in calls if "delete" in argv]
    assert any("clusterrolebinding" in argv and creds._QUOTA_ROLE_NAME in argv for argv in deletes)
    assert any("clusterrole" in argv and creds._QUOTA_ROLE_NAME in argv for argv in deletes)
    role, binding = _quota_grant(_rbac_docs(tmp_path, monkeypatch, quota_writes=False))
    assert role is None and binding is None


def test_provision_passes_the_tasks_quota_decision_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_kubectl(monkeypatch)
    seen: list[bool] = []
    original = creds.ensure_agent_identity

    def spy(work_dir: Path, context: str | None = None, *, quota_writes: bool = True) -> None:
        seen.append(quota_writes)
        original(work_dir, context, quota_writes=quota_writes)

    monkeypatch.setattr(creds, "ensure_agent_identity", spy)
    creds.provision_agent_credentials(_PINNED, tmp_path, token_ttl_sec=1500, quota_writes=False)
    creds.provision_agent_credentials(_PINNED, tmp_path, token_ttl_sec=1500)
    assert seen == [False, True]


@pytest.mark.parametrize(
    "forbidden_group",
    ["rbac.authorization.k8s.io", "admissionregistration.k8s.io"],
)
def test_rbac_never_grants_self_escalation_or_admission_control(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, forbidden_group: str
) -> None:
    """Without these omissions the agent could grant itself more or delete the policy."""
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


@pytest.mark.parametrize("timeout_sec", [None, 30000.0])
def test_token_ttl_for_warns_when_the_token_may_outlive_the_agent(
    timeout_sec: float | None, caplog: pytest.LogCaptureFixture
) -> None:
    """Past the cap the credential expires mid-run, which is worth more than an info line."""
    with caplog.at_level("WARNING"):
        creds.token_ttl_for(timeout_sec)

    assert any("capping" in r.message for r in caplog.records if r.levelname == "WARNING")


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
    # No exec block or ADC: the container has no cloud credential helper to shell out to.
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


def test_render_agent_kubeconfig_quotes_flow_indicator_characters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scalars are json.dumps-quoted: a value carrying flow indicators
    (': ', ',', '{') must survive the flow-mapping render intact."""
    _patch_kubectl(monkeypatch)
    plan = NetworkPlan(rewrite_server="https://host:6443", tls_server_name="name: evil, {x}")
    path = creds.render_agent_kubeconfig(plan, tmp_path, user_fields="token: t")
    cluster = yaml.safe_load(path.read_text())["clusters"][0]["cluster"]
    assert cluster["tls-server-name"] == "name: evil, {x}"
    assert cluster["server"] == "https://host:6443"


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
    """The rendered CA/server must survive the ambient context switching after provisioning."""
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
    """The container gets a scoped, short-lived SA token, so the RBAC boundary does real work."""
    _patch_kubectl(monkeypatch)

    path = creds.provision_agent_credentials(_PINNED, tmp_path, token_ttl_sec=1500)

    user = yaml.safe_load(path.read_text())["users"][0]["user"]
    assert user == {"token": _TOKEN}
    assert "client-certificate-data" not in user


def test_provision_refuses_before_writing_when_the_kubeconfig_cannot_render(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Render failures must surface before the first apply, or objects are stranded on the cluster."""
    calls = _patch_kubectl(monkeypatch, ca="")

    with pytest.raises(SandboxError, match="certificate-authority-data"):
        creds.provision_agent_credentials(_PINNED, tmp_path, token_ttl_sec=1500)

    assert not any("apply" in argv for argv in calls)
    assert not (tmp_path / "kubeconfig").exists()


def test_provision_refuses_to_fall_back_to_the_admin_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A silent admin fallback would look identical in the results with no RBAC boundary."""
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
    """An exec-plugin context has no static certificate, and the plugin cannot run in-container."""
    monkeypatch.setenv(creds.ALLOW_ADMIN_ENV, "1")
    _patch_kubectl(monkeypatch, mint_fails=True, cert="", key="")

    with pytest.raises(SandboxError, match="exec"):
        creds.provision_agent_credentials(_PINNED, tmp_path, token_ttl_sec=1500)


def test_provision_tears_down_when_the_fallback_render_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The exec-plugin refusal fires after cluster writes, so provisioning must clean up itself."""
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


def _rendered(tmp_path: Path, *names: str) -> list[dict]:
    """Parse the named rendered manifests into one document list (missing files contribute none)."""
    docs: list[dict] = []
    for name in names:
        path = tmp_path / name
        if path.exists():
            docs.extend(d for d in yaml.safe_load_all(path.read_text()) if d)
    return docs


def _policy_docs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pod_security: str = creds.POD_SECURITY_BASELINE,
) -> list[dict]:
    _patch_kubectl(monkeypatch)
    creds.enforce_pod_security(tmp_path, pod_security=pod_security)
    return _rendered(tmp_path, "bench-agent-pod-security.yaml", "bench-agent-namespace-guards.yaml")


def _doc(docs: list[dict], kind: str, name: str) -> dict:
    """Pick one document out of the multi-doc manifest by kind and name."""
    return next(d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name)


def test_pod_security_policy_denies_the_observed_escape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every ingredient of the privileged-hostPath escape must have a validation rejecting it."""
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
    """A binding in Warn mode would let the escape through and merely mention it."""
    docs = _policy_docs(tmp_path, monkeypatch)
    binding = _doc(docs, "ValidatingAdmissionPolicyBinding", "bench-agent-pod-security")
    policy = _doc(docs, "ValidatingAdmissionPolicy", "bench-agent-pod-security")

    assert binding["spec"]["validationActions"] == ["Deny"]
    assert policy["spec"]["failurePolicy"] == "Fail"


def test_pod_security_policy_also_matches_the_ephemeral_container_subresource(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A rule naming only ``pods`` never sees ``kubectl debug --profile=sysadmin``."""
    docs = _policy_docs(tmp_path, monkeypatch)
    policy = _doc(docs, "ValidatingAdmissionPolicy", "bench-agent-pod-security")
    rules = policy["spec"]["matchConstraints"]["resourceRules"]
    ephemeral = next(r for r in rules if "pods/ephemeralcontainers" in r["resources"])

    assert any("pods" in r["resources"] for r in rules)
    # Ephemeral containers are added with an UPDATE on the subresource, never a CREATE.
    assert "UPDATE" in ephemeral["operations"]


def test_pod_security_policy_does_not_reevaluate_pod_updates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A pod's security-relevant spec is immutable; matching UPDATE would only deny a label on a fixture pod."""
    docs = _policy_docs(tmp_path, monkeypatch)
    policy = _doc(docs, "ValidatingAdmissionPolicy", "bench-agent-pod-security")
    rules = policy["spec"]["matchConstraints"]["resourceRules"]
    pods = next(r for r in rules if r["resources"] == ["pods"])

    assert pods["operations"] == ["CREATE"]


def test_pod_security_policy_is_not_scoped_to_the_agents_username(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A controller creates the agent's pods under its own identity; a username match would miss them."""
    docs = _policy_docs(tmp_path, monkeypatch)
    policy = _doc(docs, "ValidatingAdmissionPolicy", "bench-agent-pod-security")

    assert "matchConditions" not in policy["spec"]
    assert creds._AGENT_USERNAME not in yaml.safe_dump(policy)


def test_pod_security_policy_exempts_the_clusters_own_components(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """System components legitimately run privileged; enforcing on them breaks the cluster."""
    docs = _policy_docs(tmp_path, monkeypatch)
    binding = _doc(docs, "ValidatingAdmissionPolicyBinding", "bench-agent-pod-security")
    expr = binding["spec"]["matchResources"]["namespaceSelector"]["matchExpressions"][0]

    assert expr["key"] == "kubernetes.io/metadata.name"
    assert expr["operator"] == "NotIn"
    assert "kube-system" in expr["values"]


def test_pod_security_policy_does_not_exempt_the_harness_namespace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The agent can create pods in ``bench-system``, so exempting it would be an open door."""
    docs = _policy_docs(tmp_path, monkeypatch)
    binding = _doc(docs, "ValidatingAdmissionPolicyBinding", "bench-agent-pod-security")
    expr = binding["spec"]["matchResources"]["namespaceSelector"]["matchExpressions"][0]

    assert creds.AGENT_NAMESPACE not in expr["values"]


def test_namespace_guard_denies_claiming_an_exempt_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The agent could otherwise claim an exempt name absent on this provider and deploy there."""
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
    # No namespaceSelector: the pod policy's NotIn would exempt the very creation being guarded.
    assert "namespaceSelector" not in binding["spec"].get("matchResources", {})


def test_pod_security_policy_exempts_namespaces_the_cluster_manages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A name list goes stale; the addon manager's label covers managed namespaces we cannot name."""
    docs = _policy_docs(tmp_path, monkeypatch)
    binding = _doc(docs, "ValidatingAdmissionPolicyBinding", "bench-agent-pod-security")
    exprs = binding["spec"]["matchResources"]["namespaceSelector"]["matchExpressions"]
    managed = next(e for e in exprs if e["key"] == "addonmanager.kubernetes.io/mode")

    assert managed["operator"] == "DoesNotExist"


def test_namespace_guard_denies_claiming_the_managed_label(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The agent holds ``patch`` on namespaces, so the mutable label half needs a guard on UPDATE."""
    docs = _policy_docs(tmp_path, monkeypatch)
    policy = _doc(docs, "ValidatingAdmissionPolicy", "bench-agent-namespace-guard")
    rule = policy["spec"]["matchConstraints"]["resourceRules"][0]
    expressions = " ".join(v["expression"] for v in policy["spec"]["validations"])

    assert "UPDATE" in rule["operations"]
    assert "addonmanager.kubernetes.io/mode" in expressions


def test_namespace_guard_denies_deleting_the_clusters_own_namespaces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The supplement grants ``delete`` on namespaces; only three are apiserver-protected."""
    docs = _policy_docs(tmp_path, monkeypatch)
    policy = _doc(docs, "ValidatingAdmissionPolicy", "bench-agent-namespace-guard")
    rule = policy["spec"]["matchConstraints"]["resourceRules"][0]
    name_rule, label_rule = policy["spec"]["validations"][:2]

    assert "DELETE" in rule["operations"]
    # On DELETE ``object`` is null and, failing closed, an evaluation error would deny every delete.
    assert policy["spec"]["variables"][0]["expression"] == (
        "request.operation == 'DELETE' ? oldObject : object"
    )
    for expression in (name_rule["expression"], label_rule["expression"]):
        assert "variables.ns.metadata" in expression
        assert "object.metadata" not in expression


def test_namespace_guard_requires_psa_on_namespaces_the_agent_creates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Labels miss namespaces created after provisioning; requiring one puts PSA in front of policy 2."""
    docs = _policy_docs(tmp_path, monkeypatch)
    policy = _doc(docs, "ValidatingAdmissionPolicy", "bench-agent-namespace-guard")
    on_create = next(
        v
        for v in policy["spec"]["validations"]
        if "request.operation != 'CREATE'" in v["expression"]
    )

    assert "object.metadata.labels['pod-security.kubernetes.io/enforce']" in on_create["expression"]
    assert "'baseline'" in on_create["expression"]
    assert "'restricted'" in on_create["expression"]
    assert "'privileged'" not in on_create["expression"]
    assert "pod-security.kubernetes.io/enforce=baseline" in on_create["message"]


def test_namespace_guard_denies_removing_or_weakening_the_psa_label(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Requiring the label at CREATE is pointless if ``kubectl label ns x enforce-`` removes it."""
    docs = _policy_docs(tmp_path, monkeypatch)
    policy = _doc(docs, "ValidatingAdmissionPolicy", "bench-agent-namespace-guard")
    on_update = next(
        v
        for v in policy["spec"]["validations"]
        if "request.operation != 'UPDATE'" in v["expression"]
    )

    # Compares old against new: a namespace that never carried an accepted level is left alone.
    assert (
        "oldObject.metadata.labels['pod-security.kubernetes.io/enforce']" in on_update["expression"]
    )
    assert "object.metadata.labels['pod-security.kubernetes.io/enforce']" in on_update["expression"]


def test_namespace_guard_applies_only_to_the_agents_own_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Username scoping is safe here: a namespace is always created by whoever asked."""
    docs = _policy_docs(tmp_path, monkeypatch)
    policy = _doc(docs, "ValidatingAdmissionPolicy", "bench-agent-namespace-guard")
    condition = policy["spec"]["matchConditions"][0]["expression"]

    assert f"system:serviceaccount:{creds.AGENT_NAMESPACE}:{creds.AGENT_SA_NAME}" in condition


def test_privileged_keeps_the_guards_and_drops_only_the_pod_policy_and_labels(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The guards govern where the agent may write, which a privileged task does not change."""
    calls = _patch_kubectl(monkeypatch, namespaces={"items": [_ns("default")]})

    creds.enforce_pod_security(tmp_path, pod_security=creds.POD_SECURITY_PRIVILEGED)

    assert any(_applies(c, "bench-agent-namespace-guards.yaml") for c in calls)
    assert any(_applies(c, "bench-agent-nonconformant-pods.yaml") for c in calls)
    assert not any(_applies(c, "bench-agent-pod-security.yaml") for c in calls)
    assert [c for c in calls if "label" in c] == []
    docs = _rendered(tmp_path, "bench-agent-namespace-guards.yaml")
    guard = _doc(docs, "ValidatingAdmissionPolicy", "bench-agent-namespace-guard")
    assert _doc(docs, "ValidatingAdmissionPolicy", "bench-agent-exempt-namespace-guard")
    # Only the PSA-label half goes: the agent is allowed privileged pods, so no level is required.
    expressions = " ".join(v["expression"] for v in guard["spec"]["validations"])
    assert "addonmanager.kubernetes.io/mode" in expressions
    assert "pod-security.kubernetes.io/enforce" not in expressions
    assert _guard_expression(_rendered(tmp_path, "bench-agent-nonconformant-pods.yaml")) == "true"


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
    """``edit`` is cluster-wide, so the exemption is only safe if the agent cannot write there."""
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
    """Controllers create pods under their own identity; only the workload object carries the agent's."""
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
    """A shell in a privileged system pod is the same escape; exec arrives as CONNECT, not CREATE."""
    matched = _exempt_guard_resources(_policy_docs(tmp_path, monkeypatch), "CONNECT")

    assert {
        "/pods/exec",
        "/pods/attach",
        "/pods/portforward",
        "/pods/proxy",
        "/services/proxy",
    } <= matched


def test_exempt_namespace_guard_selects_exactly_what_the_pod_policy_skips(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The two must stay exact complements, or a namespace falls through both."""
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


def test_enforce_pod_security_labels_the_harness_namespace_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The agent can create pods in ``bench-system``; PSA baseline is wider than the policy's CEL."""
    calls = _patch_kubectl(monkeypatch, namespaces={"items": [_ns(creds.AGENT_NAMESPACE)]})

    creds.enforce_pod_security(tmp_path)

    labelled = [c for c in calls if "label" in c]
    assert len(labelled) == 1
    assert labelled[0][:4] == ["kubectl", "label", "namespace", creds.AGENT_NAMESPACE]


def test_enforce_pod_security_leaves_a_warn_or_audit_only_level_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A warn/audit-only setting would otherwise be overwritten, marked, then stripped."""
    calls = _patch_kubectl(
        monkeypatch,
        namespaces={
            "items": [
                _ns("payments", **{"pod-security.kubernetes.io/warn": "restricted"}),
                _ns("audited", **{"pod-security.kubernetes.io/audit": "restricted"}),
                _ns("plain"),
            ]
        },
    )

    creds.enforce_pod_security(tmp_path)

    labelled = [c for c in calls if "label" in c]
    assert [c[3] for c in labelled] == ["plain"]


def test_enforce_pod_security_warns_when_the_sandbox_namespace_already_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Fixed names: a live run on the same cluster would lose its identity to this teardown."""
    _patch_kubectl(monkeypatch, namespaces={"items": [_ns(creds.AGENT_NAMESPACE)]})

    with caplog.at_level("WARNING"):
        creds.enforce_pod_security(tmp_path)

    assert any("must not overlap" in r.message for r in caplog.records)


def test_enforce_pod_security_leaves_a_declared_level_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A task verifier may assert its own enforce level; overwriting it would fail the task."""
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
    """Managed namespaces are identified by the addon manager's label, not just the name list."""
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


def test_enforce_pod_security_lists_namespaces_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The exempt set, the shell-guard scan and the labeller all read the same listing."""
    calls = _patch_kubectl(monkeypatch, namespaces={"items": [_ns("default")]})

    creds.enforce_pod_security(tmp_path)

    ns_gets = [c for c in calls if "get" in c and c[c.index("get") + 1] == "namespaces"]
    assert len(ns_gets) == 1


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
    """Unchecked, the apply dies with ``no matches for kind``, which reads like a manifest typo."""
    calls = _patch_kubectl(monkeypatch, policy_api=False)

    with pytest.raises(SandboxError, match=creds._MIN_CLUSTER_VERSION):
        creds.enforce_pod_security(tmp_path)

    # Refused before anything is applied, so nothing is half-provisioned.
    assert not any("apply" in c for c in calls)


def test_a_forbidden_policy_api_read_is_not_reported_as_a_version_floor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On GKE the policy API is gated by IAM; 'upgrade the cluster' would send the operator the wrong way."""
    monkeypatch.delenv(creds.ALLOW_ADMIN_ENV, raising=False)
    _patch_kubectl(monkeypatch)
    real = kubectl.run

    def forbid_the_policy_read(argv, **kwargs):
        if "get" in argv and argv[argv.index("get") + 1] == creds._POLICY_API_RESOURCE:
            raise SubprocessError(argv, 1, stderr="Error from server (Forbidden): forbidden")
        return real(argv, **kwargs)

    monkeypatch.setattr(kubectl, "run", forbid_the_policy_read)

    with pytest.raises(SubprocessError):
        creds.enforce_pod_security(tmp_path)
    with pytest.raises(SandboxError, match="pod security") as info:
        creds.provision_agent_credentials(_PINNED, tmp_path, token_ttl_sec=1500)
    assert creds._MIN_CLUSTER_VERSION not in str(info.value)


def test_the_version_refusal_is_not_the_admin_escape_hatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No credential makes an old apiserver serve the policy API, so the hatch must not cover it."""
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
    """Fixtures may deploy privileged pods on purpose; admission cannot retract them, only deny exec."""
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
    """``attach`` reaches the same process; ``port-forward`` reaches anything it listens on."""
    docs = _shell_guard(
        tmp_path, monkeypatch, pods={"items": [_pod("team-alpha", "cache", **_PRIVILEGED)]}
    )
    policy = _doc(docs, "ValidatingAdmissionPolicy", creds._NONCONFORMANT_GUARD_NAME)
    rule = policy["spec"]["matchConstraints"]["resourceRules"][0]

    assert rule["operations"] == ["CONNECT"]
    assert set(rule["resources"]) == {"pods/exec", "pods/attach", "pods/portforward", "pods/proxy"}
    assert _doc(docs, "ValidatingAdmissionPolicyBinding", creds._NONCONFORMANT_GUARD_NAME)["spec"][
        "validationActions"
    ] == ["Deny"]


def test_the_shell_guard_applies_only_to_the_agents_own_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Username-scoped: the operator and the task's controllers must keep reaching these pods."""
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
    """The scan mirrors the policy's CEL, not PSA baseline: it may skip only pods the policy admits."""
    docs = _shell_guard(
        tmp_path, monkeypatch, pods={"items": [_pod("team-alpha", "cache", **spec)]}
    )

    assert "'team-alpha/cache'" in _guard_expression(docs)


def test_the_shell_guard_ignores_pods_the_agent_already_cannot_reach(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exempt namespaces (by name or label) are already closed to the agent on all four verbs."""
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
    """A reused cluster must not keep the previous run's list; an empty CEL list would not compile."""
    calls = _patch_kubectl(monkeypatch)

    creds.enforce_pod_security(tmp_path)

    assert any(_applies(c, "bench-agent-nonconformant-pods.yaml") for c in calls)
    text = (tmp_path / "bench-agent-nonconformant-pods.yaml").read_text()
    docs = [d for d in yaml.safe_load_all(text) if d]
    assert _guard_expression(docs) == "true"


def test_the_nonconformant_scan_looks_at_every_namespace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without ``-A`` the listing is silently scoped to the kubeconfig's current namespace."""
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
    """The privileged opt-out drops the pod policy and the labels, and nothing else."""
    calls = _patch_kubectl(monkeypatch)

    creds.provision_agent_credentials(
        _PINNED,
        tmp_path,
        token_ttl_sec=1500,
        pod_security=creds.POD_SECURITY_PRIVILEGED,
    )

    assert not any(_applies(c, "bench-agent-pod-security.yaml") for c in calls)
    assert [c for c in calls if "label" in c] == []
    assert any(_applies(c, "bench-agent-namespace-guards.yaml") for c in calls)


def test_provision_refuses_a_cluster_no_provider_vouched_for(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unpinned means the ambient current-context; cluster-wide writes there are unacceptable unasked."""
    monkeypatch.delenv(creds.ALLOW_AMBIENT_ENV, raising=False)
    calls = _patch_kubectl(monkeypatch)

    with pytest.raises(SandboxError, match=creds.ALLOW_AMBIENT_ENV):
        creds.provision_agent_credentials(NetworkPlan(), tmp_path, token_ttl_sec=1500)

    assert not any("apply" in c for c in calls)


def test_provision_uses_the_ambient_cluster_only_when_told_to(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(creds.ALLOW_AMBIENT_ENV, "1")
    calls = _patch_kubectl(monkeypatch)

    path = creds.provision_agent_credentials(NetworkPlan(), tmp_path, token_ttl_sec=1500)

    assert yaml.safe_load(path.read_text())["users"][0]["user"] == {"token": _TOKEN}
    # Pinned once: every write names the snapshotted context.
    writes = [c for c in calls if "apply" in c or "token" in c or "label" in c]
    assert writes and all("some-ambient-context" in c for c in writes)


def test_pin_plan_context_pins_the_authorized_ambient_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(creds.ALLOW_AMBIENT_ENV, "1")
    _patch_kubectl(monkeypatch)
    pinned = creds.pin_plan_context(NetworkPlan())
    assert pinned.kubectl_context == "some-ambient-context"
    provider_pinned = NetworkPlan(kubectl_context="kind-c1")
    assert creds.pin_plan_context(provider_pinned) is provider_pinned


def test_every_provisioning_and_teardown_kubectl_call_is_bounded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every kubectl call on these paths carries a timeout, so a silent apiserver cannot hang the run."""
    _patch_kubectl(monkeypatch)
    inner = kubectl.run
    timeouts: list[tuple[list[str], float | None]] = []

    def recording_run(argv: list[str], **kwargs: Any) -> Any:
        timeouts.append((list(argv), kwargs.get("timeout")))
        return inner(argv, **kwargs)

    monkeypatch.setattr(kubectl, "run", recording_run)
    plan = NetworkPlan(kubectl_context="kind-c1")
    creds.provision_agent_credentials(plan, tmp_path, token_ttl_sec=1500)
    creds.teardown_agent_credentials("kind-c1")

    unbounded = [argv for argv, timeout in timeouts if timeout is None]
    assert timeouts and unbounded == []


def test_teardown_always_skips_an_unpinned_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provisioning always pins, so None wrote nothing; a stale ambient opt-in adds no deletes."""
    calls = _patch_kubectl(monkeypatch)
    for ambient in (None, "1"):
        if ambient is None:
            monkeypatch.delenv(creds.ALLOW_AMBIENT_ENV, raising=False)
        else:
            monkeypatch.setenv(creds.ALLOW_AMBIENT_ENV, ambient)
        assert creds.teardown_agent_credentials(None) is True
        assert calls == []


def test_provision_fails_loud_when_pod_security_cannot_be_applied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Running the agent without the policy would leave the privileged-pod escape undenied."""
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
    """One switch for both failures, or the policy apply raises first and the hatch is unreachable."""
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
    """A policy added to the manifests without a teardown row fails here, not on a reused cluster."""
    # Joined on a document separator: the constants do not all end with one.
    rendered = "\n---\n".join(
        (
            creds._POD_SECURITY_POLICY_MANIFEST,
            creds._render_namespace_guard(creds.POD_SECURITY_BASELINE),
            creds._EXEMPT_NAMESPACE_GUARD_MANIFEST,
            creds._render_nonconformant_pod_guard([]),
        )
    )
    docs = [d for d in yaml.safe_load_all(rendered) if d]
    by_kind: dict[str, set[str]] = {}
    for doc in docs:
        by_kind.setdefault(doc["kind"], set()).add(doc["metadata"]["name"])
    assert by_kind["ValidatingAdmissionPolicy"] == set(creds._POLICY_NAMES)
    assert by_kind["ValidatingAdmissionPolicyBinding"] == set(creds._POLICY_BINDING_NAMES)

    rbac = [d for d in yaml.safe_load_all(creds._RBAC_MANIFEST) if d]
    # The quota grant is a separate manifest a task may decline, but teardown
    # must remove it whether or not the task took it.
    rbac += [d for d in yaml.safe_load_all(creds._QUOTA_RBAC_MANIFEST) if d]
    rbac_by_kind: dict[str, set[str]] = {}
    for doc in rbac:
        rbac_by_kind.setdefault(doc["kind"], set()).add(doc["metadata"]["name"])
    assert rbac_by_kind["ClusterRoleBinding"] == set(creds._CLUSTER_ROLE_BINDING_NAMES)
    assert rbac_by_kind["ClusterRole"] == set(creds._CLUSTER_ROLE_NAMES)
    assert rbac_by_kind["Namespace"] == {creds.AGENT_NAMESPACE}
    assert rbac_by_kind["ServiceAccount"] == {creds.AGENT_SA_NAME}


def test_teardown_deletes_everything_bindings_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bindings first so denial stops at once; the namespace last and waited on."""
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
    """A level someone else set never carries the marker and is never removed."""
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
    # Selected server-side by the marker, not by listing every namespace.
    listed = next(argv for argv in calls if "get" in argv and "namespaces" in argv)
    assert listed[listed.index("-l") + 1] == creds._PSA_MANAGED_LABEL


def test_teardown_survives_a_non_json_namespace_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-JSON stdout on exit 0 raises JSONDecodeError; the remaining deletes still run."""
    calls = _patch_kubectl(monkeypatch)
    real = kubectl.run

    def garbage_listing(argv: list[str], **kwargs: Any) -> Any:
        if "get" in argv and "namespaces" in argv:
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout="notice: plugin loaded\n{", stderr="")
        return real(argv, **kwargs)

    monkeypatch.setattr(kubectl, "run", garbage_listing)

    assert creds.teardown_agent_credentials("kind-c1") is False
    deleted = [argv[argv.index("delete") + 1] for argv in calls if "delete" in argv]
    assert deleted[-1] == "namespace"
    assert "clusterrolebinding" in deleted


def test_teardown_survives_kubectl_failing_to_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """OSError is not a SubprocessError; it must not abort the remaining deletes either."""
    calls = _patch_kubectl(monkeypatch)
    real = kubectl.run

    def no_binary_for_bindings(argv: list[str], **kwargs: Any) -> Any:
        if "delete" in argv and argv[argv.index("delete") + 1] == creds._POLICY_BINDING_KIND:
            calls.append(argv)
            raise OSError("kubectl: no such file or directory")
        return real(argv, **kwargs)

    monkeypatch.setattr(kubectl, "run", no_binary_for_bindings)

    assert creds.teardown_agent_credentials("kind-c1") is False
    deleted = [argv[argv.index("delete") + 1] for argv in calls if "delete" in argv]
    assert deleted[0] == creds._POLICY_BINDING_KIND
    assert deleted[-1] == "namespace"


def test_teardown_is_best_effort_and_reports_residue(monkeypatch: pytest.MonkeyPatch) -> None:
    """One failed delete stops nothing else, and the return value reports the residue."""
    calls = _patch_kubectl(monkeypatch, delete_fails={creds._POLICY_BINDING_KIND})

    assert creds.teardown_agent_credentials("kind-c1") is False

    # The failed first step did not short-circuit the remaining four.
    assert _delete_kinds(calls)[-1] == "namespace"


def test_teardown_survives_an_unlistable_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    """Teardown never raises: a second failure must not eclipse the first."""
    _patch_kubectl(monkeypatch)

    def refuse_lists(argv: list[str], **kwargs: Any) -> NoReturn:
        raise SubprocessError(argv, 1, stderr="connection refused")

    monkeypatch.setattr(kubectl, "run", refuse_lists)

    assert creds.teardown_agent_credentials("kind-c1") is False


def test_teardown_survives_a_listing_that_is_not_an_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid JSON of the wrong shape raises AttributeError, which must not escape either."""
    calls = _patch_kubectl(monkeypatch)
    real = kubectl.run

    def list_listing(argv: list[str], **kwargs: Any) -> Any:
        if "get" in argv and "namespaces" in argv:
            return SimpleNamespace(returncode=0, stdout="[]", stderr="")
        return real(argv, **kwargs)

    monkeypatch.setattr(kubectl, "run", list_listing)

    assert creds.teardown_agent_credentials("kind-c1") is False
    assert _delete_kinds(calls)[-1] == "namespace"


def test_teardown_counts_an_unserved_kind_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cluster without the policy API has no policies; that is clean, not residue."""
    _patch_kubectl(monkeypatch)
    real = kubectl.run

    def no_policy_kinds(argv: list[str], **kwargs: Any) -> Any:
        if "delete" in argv and argv[argv.index("delete") + 1] in (
            creds._POLICY_KIND,
            creds._POLICY_BINDING_KIND,
        ):
            raise SubprocessError(
                argv, 1, stderr='error: the server doesn\'t have a resource type "x"'
            )
        return real(argv, **kwargs)

    monkeypatch.setattr(kubectl, "run", no_policy_kinds)

    assert creds.teardown_agent_credentials("kind-c1") is True


def test_provisioning_cleans_up_after_an_unexpected_error_type(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The pod listing runs after the policies land; a non-JSON reply must still trigger cleanup."""
    calls = _patch_kubectl(monkeypatch)
    real = kubectl.run

    def garbage_pods(argv: list[str], **kwargs: Any) -> Any:
        if "get" in argv and argv[argv.index("get") + 1] == "pods":
            return SimpleNamespace(returncode=0, stdout="not json", stderr="")
        return real(argv, **kwargs)

    monkeypatch.setattr(kubectl, "run", garbage_pods)

    with pytest.raises(ValueError):
        creds.provision_agent_credentials(_PINNED, tmp_path, token_ttl_sec=1500)

    assert any(_applies(argv, "bench-agent-pod-security.yaml") for argv in calls)
    kinds = _delete_kinds(calls)
    assert kinds[0] == creds._POLICY_BINDING_KIND
    assert kinds[-1] == "namespace"


def test_failed_provisioning_cleans_up_its_partial_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A mint failure lands after cluster writes; provisioning removes them, keeping its error."""
    calls = _patch_kubectl(monkeypatch, mint_fails=True)

    with pytest.raises(SandboxError, match="scoped ServiceAccount"):
        creds.provision_agent_credentials(_PINNED, tmp_path, token_ttl_sec=1500)

    kinds = _delete_kinds(calls)
    assert creds._POLICY_BINDING_KIND in kinds
    assert "namespace" in kinds


def test_marker_label_rides_along_with_enforcement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The marker is set on the same write as the levels, so they cannot come apart."""
    calls = _patch_kubectl(monkeypatch, namespaces={"items": [_ns("default")]})

    creds.enforce_pod_security(tmp_path, "kind-c1")

    labelled = [argv for argv in calls if "label" in argv]
    assert len(labelled) == 1
    assert f"{creds._PSA_MANAGED_LABEL}=true" in labelled[0]
