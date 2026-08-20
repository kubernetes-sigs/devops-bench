---
name: cleanup-orphaned-resources
description: Discover and remove cloud or local resources leaked by aborted or failed eval runs — stale per-run state, leftover kind clusters and their node containers, stuck harness processes, and orphaned GKE clusters, node service accounts, secrets, VPCs and their dependencies, Cloud SQL instances, and Artifact Registry repos in the sandbox project, all scoped to the aborted run's own token. Invoke when a "fresh" run fails instantly, when someone reports leaked or orphaned resources, or asks to "clean up after a failed run", "sweep the sandbox project", or "why does re-running 409?".
---

# Clean up orphaned resources

A crashed or aborted run leaves debris: scratch state on the host the run
executed on, kind clusters and their node containers, stuck processes, and — worse —
cloud resources a failed teardown never removed. The cloud leftovers cause the
classic "a fresh run fails instantly" symptom, often a `409 already exists`. This skill finds that
debris and removes it **after explicit confirmation**.

- "Before any retry" local checklist (don't duplicate it — run it) →
  [`../../../docs/appendix/known_issues.md`](../../../docs/appendix/known_issues.md)

The cloud half of this skill is written against **GCP**, since that is where the
eval projects live. The shape generalizes: discover by run-token prefix, list,
confirm, delete in dependency order.

---

## Flow

### 1. Local wipe first

Most "instant fresh failure" cases are local stale state, not cloud leaks. Run the
**"Before any retry"** checklist in
[`known_issues.md`](../../../docs/appendix/known_issues.md) — it wipes
`/tmp/devops-bench-runs/*`, deletes leftover kind clusters and their orphaned node
containers (which `kind get clusters` does not track), and kills stale
`devops_bench` / agent processes from a prior launch. Do this on the host the run
actually ran on. Don't restate the commands here — follow the checklist.

### 2. Cloud discovery (sandbox project only, list mode)

Confirm the active project is the **sandbox / eval project** before touching
anything. Then *list* (never delete yet) the resources a failed teardown leaks.
Match on run-token prefixes so you never sweep shared infra.

```bash
# Fill these in, then paste the block.
PROJECT="my-sandbox-project"        # verify: gcloud config get-value project
CLUSTER=""                          # set in step 1 below

# Step 1 lists CANDIDATE clusters across all runs — it is not yet scoped to one.
# RunEnv names a cluster c<blake2s digest of the run id>. Pick the aborted run's
# cluster from the output; run-scoped filtering begins once CLUSTER is set below, and
# each later filter is anchored to that exact name so a resource that merely
# contains the token (shared-$CLUSTER-net) never matches.
gcloud container clusters list --project "$PROJECT" \
  --filter="name~'^c[0-9a-f]{8}-'" --format="table(name,location,status)"
CLUSTER="cbd827e1-bench-opa"        # <- copy the aborted run's cluster from that list

# The node SA account_id is derived, not the cluster name:
#   gke-nodes-<first 9 of the slugified cluster>-<first 6 of md5(cluster)>
# (tf/modules/cluster/gke/main.tf). Compute it rather than guessing.
SLUG=$(printf '%s' "$CLUSTER" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9][^a-z0-9]*/-/g' | cut -c1-9 | sed 's/-$//')
SA="gke-nodes-${SLUG}-$(printf '%s' "$CLUSTER" | md5sum | cut -c1-6)@${PROJECT}.iam.gserviceaccount.com"
gcloud iam service-accounts list --project "$PROJECT" --filter="email=$SA" --format="table(email)"

# Everything else, anchored to this run's cluster name.
gcloud artifacts repositories list --project "$PROJECT" \
  --filter="name~'/hello-app-${CLUSTER}$'" --format="table(name,location,createTime)"
gcloud secrets list        --project "$PROJECT" --filter="name~'${CLUSTER}$'"  --format="table(name)"
gcloud compute networks list --project "$PROJECT" --filter="name~'${CLUSTER}$'" --format="table(name)"
gcloud sql instances list  --project "$PROJECT" --filter="name~'${CLUSTER}$'"  --format="table(name,region,state)"
```

Every filter above is anchored to the exact `$CLUSTER` name, not a substring of
it. Do not relax them to a bare `gke-nodes-` or `hello-app-` prefix, and do not
drop the trailing `$`: a sibling run is very likely live in the same project and
its resources share those prefixes. Read the listing and confirm each name
belongs to the aborted run before deleting anything.

Two notes on Cloud SQL: a deleted instance's dependent resources can take
several days to disappear, and name reuse after deletion is not guaranteed to
be immediate. Neither matters if the instance name carries the run token, which
is what the task-review checklist requires.

### 3. LIST findings, then get explicit confirmation

Deletion here is **destructive and outward-facing** — it removes real cloud
resources. Present the discovered list to the operator and get an explicit
go-ahead before deleting anything. Default to list/dry-run; deletion is opt-in.

Only sweep resources whose names carry the run-token prefix of the aborted run(s).
**Never** touch shared or long-lived infra, and never operate outside the sandbox
project.

### 4. Delete (only after confirmation)

```bash
# Substitute the names the listing returned; run them one at a time and confirm
# each before the next.

# clusters — take <loc> from the cluster's own row
gcloud container clusters delete <name> --location <loc> --project "$PROJECT" --quiet
# node SA — use the $SA computed during discovery, not a hand-typed name:
# the account id is gke-nodes-<slug>-<md5(cluster)[:6]>, not gke-nodes-<cluster>
gcloud iam service-accounts delete "$SA" --project "$PROJECT" --quiet
# secrets
gcloud secrets delete <name> --project "$PROJECT" --quiet
# auto-mode VPCs — dependent resources first (firewall rules, subnets, routes,
# routers, peerings), then the network itself
gcloud compute firewall-rules delete <rule> --project "$PROJECT" --quiet
gcloud compute networks delete <network> --project "$PROJECT" --quiet
# Artifact Registry repos — <loc> may differ from the cluster's location
gcloud artifacts repositories delete <name> --location <loc> --project "$PROJECT" --quiet
```

Delete in dependency order: the cluster before its node SA, and a VPC's
dependents before the VPC. After deleting, re-run the discovery in step 2 and
confirm it returns nothing for this run's token.

### 5. Report

Report what was found, what was deleted (with names), what was deliberately left,
and confirm the discovery list is now empty so a re-run won't `409`.

---

## Guardrails

- **Always list and get explicit confirmation before deleting.** No silent sweeps.
- Sandbox / eval project only — verify the active project first; never a shared or
  production project.
- Match the aborted run's token prefix; never touch shared or long-lived infra.
- Default to list/dry-run mode; deletion is opt-in.
- A stranded `gke-nodes-*` SA no longer causes `409 already exists` on re-run — the
  name carries an md5 of the cluster name — but it is still the most commonly
  leaked resource, so sweep it.
