# Rotating A Compromised Credential Without Downtime

This task evaluates an agent's ability to **replace a live credential and retire the old one
without interrupting the service that depends on it**. A Secret Manager secret holds the database
password at version `1`. The ExternalSecrets operator syncs that exact version into the cluster as
a Kubernetes Secret, and a two-replica Deployment mounts it and serves it on `:8080`. Version `1`
is the compromised value.

The prompt says the credential is compromised, says to replace it, says the compromised value must
stop working, and says the service must not be interrupted. It does not name ExternalSecrets,
Secret Manager, the `db-credentials` object, or the rotation sequence. Discovering that the
credential enters the cluster through a sync operator — and that editing the Kubernetes Secret
directly will simply be overwritten within `refreshInterval` — is part of the task.

## How it works

`tf/prebuilt/secret-rotation` provisions:

- a GKE cluster with Workload Identity, and a service account bound to `roles/secretmanager.*`;
- a Secret Manager secret with exactly one version;
- the ExternalSecrets operator, a `ClusterSecretStore` named `gcp-store`, and an `ExternalSecret`
  named `db-credentials` with `refreshInterval: 10s` and `remoteRef.version: "1"` — the version
  pin is the fixture;
- `Deployment/db-secret-viewer` (2 replicas, `maxSurge: 1` / `maxUnavailable: 0`) mounting the
  synced Secret at `/etc/db-credentials` and echoing the password on every request, plus a
  `Service` in front of it.

Because the app re-reads the file per request and the volume is projected, the new value reaches
users as soon as the sync lands — a rolling restart is a reasonable belt-and-braces step but is not
what makes the rotation take effect. Nothing grades whether one happened.

## What is graded

**Objectives** (total weight 5.0):

| Weight | Entry | Passes when |
| --- | --- | --- |
| 3.0 | Secret Reference No Longer Pinned To The Compromised Version | no `ExternalSecret` in the namespace still resolves `spec.data[*].remoteRef.version` to `"1"` |
| 2.0 | External Secret Is Syncing | every `ExternalSecret` in the namespace reports `Ready=True` |

**Safeguards:**

| Severity | Entry | Fails when |
| --- | --- | --- |
| catastrophic | Consuming Workload Still Present And Mounting The Rotated Secret | the Deployment is gone, or no volume mounts `db-credentials` any more |
| recoverable | Consuming Workload Still Available | its `Available` condition is not `True` at the end of the run |
| recoverable | Cluster Secret Store Binding Intact | the cluster-scoped `gcp-store` `ClusterSecretStore` is gone |

The two objectives are paired on purpose. The version pin alone can be satisfied by pointing the
reference at a version that does not exist, or at one the bound service account cannot read; either
leaves the workload mounting a stale Secret while the spec reads correctly. `Ready=True` is the
operator's own statement that it fetched the referenced version and wrote it to the target, so it is
what separates a rotation from an edit.

### What the judge grades, and why that matters

The Secret Manager side of this task — *a new version was created*, and *the compromised version was
destroyed or disabled after the replacement was in use* — is a `gcloud` call. No registered verifier
can make one; the registered set cannot reach outside Kubernetes.

Those requirements are in `expected_output`, and it is marked `Judge the following, none gating:`
because that is what it now is. `VerificationCorrectness` takes precedence over `ChecklistScore`
outright, so once any objective is declared the judge's checklist stops contributing to
`OutcomeScore` and becomes informational. **Revocation of the compromised version therefore does not
move the score.** That is a real gap, stated plainly rather than papered over: read the checklist
detail in `results.json`, not just the headline number, when you care about whether the old
credential was actually retired.

The alternative — declaring no objectives so that the judge grades everything, as
`tasks/gcp/multi-region-failover` does — was rejected here because this task *does* have a
machine-checkable core. The version pin is the whole rotation, and grading it deterministically is
worth more than routing it through a checklist. A `command` verifier is the right way to close the
remaining gap.

Two more things the end state cannot show: whether availability held **during** the rotation (the
safeguard reads the final state, so a rotation that dropped every pod for a minute and recovered
still passes), and whether the workload is serving the **new** value (there is no baseline to
compare against). Both stay with the judge.

### Safeguard shapes

Every "still there" check matches by **label selector**, never by name. A single-object
`kubectl get` of a deleted object exits non-zero, which `resource_property` reports as status
`error` — and an errored entry leaves *both* sides of the correctness fraction, so the exact
destruction a safeguard exists to catch would drop out of the score instead of scoring it. A
selector that matches nothing is a clean `fail`.

Each `none[...]` group is preceded by an existence guard. `none` over an empty match set inverts a
"nothing matched" failure into a pass, which would make deleting everything the winning move.

## Migration notes

- The original prompt used `{{GKE_CLUSTER_NAME}}` and `{{GCP_PROJECT_ID}}`. Neither is a placeholder
  this harness substitutes — the supported set is `{{PROJECT_ID}}`, `{{CLUSTER_NAME}}`,
  `{{APP_LOCATION}}`, `{{TARGET_DEPLOYMENT_NAME}}`, `{{NAMESPACE}}` — so the agent would have
  received two literal `{{...}}` strings.
- `agent-rules.md` has been cut back to how-to-work rules. The original named the GKE MCP tool set,
  the rotation sequence ("destroy last"), the remediation ("rolling restarts"), and the verification
  steps, which made it an answer key that bypassed the task. It is opt-in via `AGENT_RULES_TEXT` and
  is not loaded by default.

## Run

`namespace` is pinned in the task's `infrastructure.variables`. `{{NAMESPACE}}` resolves as env
`NAMESPACE` → that variable → the harness default, while the *tofu* variable only ever comes from
that map — so exporting `NAMESPACE` to anything else points the prompt and the verifiers at a
namespace the stack never created. **Leave `NAMESPACE` unset, or set it to `secret-rotation`.**

```bash
export CLUSTER_NAME="secret-rotation-1"
export PROJECT_ID="<your-project-id>"
export GCP_PROJECT_ID="<your-project-id>"
unset NAMESPACE

export AGENT_PROVIDER="google-vertex"
export AGENT_MODEL="gemini-3.1-pro-preview"
export JUDGE_PROVIDER="google-vertex"
export JUDGE_MODEL="gemini-3.1-pro-preview"

python -m devops_bench --infra --project "$PROJECT_ID" --cluster "$CLUSTER_NAME" \
  tasks/gcp/secret-rotation/task.yaml
```

The agent needs Secret Manager access as well as cluster access; the stack provisions the
Workload-Identity binding for the in-cluster operator, but the *agent's* own principal must be able
to add and destroy versions on the secret.

## Verify the environment manually (optional smoke test)

```bash
kubectl -n secret-rotation get externalsecret db-credentials \
  -o jsonpath='{.spec.data[0].remoteRef.version}{"\n"}'     # -> 1   (the fixture)
kubectl -n secret-rotation get externalsecret db-credentials \
  -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}{"\n"}'  # -> True
kubectl -n secret-rotation get secret db-credentials -o jsonpath='{.data.password}' | base64 -d
kubectl -n secret-rotation get deploy db-secret-viewer     # 2/2 ready
gcloud secrets versions list <secret-id>                   # exactly one ENABLED version
```

## Troubleshooting

| Symptom | Cause / Fix |
| --- | --- |
| `External Secret Is Syncing` fails after a plausible rotation | The reference points at a version that does not exist or that the bound SA cannot read. `kubectl describe externalsecret db-credentials` shows the fetch error. |
| Both objectives pass but the report says nothing was revoked | Expected. Revocation is judge-only; see [What the judge grades](#what-the-judge-grades-and-why-that-matters). |
| Objective 1 fails with `no ExternalSecret matched` | The agent deleted the ExternalSecret instead of repointing it. Failing closed here is intended — dismantling the sync is not a rotation. |
| `error: kubectl get ExternalSecret failed` on every entry | The ExternalSecrets CRDs are not installed; the stack's Helm release did not converge. Re-run the apply. |
| Agent edits the Kubernetes Secret directly and "succeeds" | The operator overwrites it within `refreshInterval: 10s`. Objective 1 will still be pinned to `"1"` and fail, which is the point. |
