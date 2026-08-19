---
name: cleanup-orphaned-resources
description: Discover and remove cloud or local resources leaked by aborted or failed eval runs — stale per-run state, leftover kind clusters and their node containers, stuck harness processes, and orphaned GKE clusters, node service accounts, secrets, VPCs, and Artifact Registry repos in the sandbox project. Invoke when a "fresh" run fails instantly, when someone reports leaked or orphaned resources, or asks to "clean up after a failed run", "sweep the sandbox project", or "why does re-running 409?".
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
PROJECT=<sandbox-project>   # verify: gcloud config get-value project

# Run-scoped GKE clusters from a crashed run. RunEnv names a cluster
# c<blake2s digest of the run id>, so match that shape rather than a fixed suffix.
gcloud container clusters list --project "$PROJECT" \
  --filter="name~'^c[0-9a-f]{8}-'" --format="table(name,location,status)"

# gke-nodes-* node SAs — deterministic names cause 409 already exists on re-run
gcloud iam service-accounts list --project "$PROJECT" \
  --filter="email~'^gke-nodes-'" --format="table(email)"

# Artifact Registry repos a task created outside its own stack
gcloud artifacts repositories list --project "$PROJECT" \
  --format="table(name,location,createTime)"
```

Widen the sweep to match the tasks that actually ran. Beyond the three classes
above, teardown failures have also stranded:

- **Secrets** — `gcloud secrets list --project "$PROJECT"`, filtered to the
  run's token prefix.
- **Auto-mode VPCs and their firewall rules** — a task that creates its own
  network leaves both; the firewall rules must go first (step 4).
- **Cloud SQL instances** — note the ~1-week name tombstone: a deleted
  instance's name stays reserved, so a re-run with the same name can still
  collide even after a clean delete.

### 3. LIST findings, then get explicit confirmation

Deletion here is **destructive and outward-facing** — it removes real cloud
resources. Present the discovered list to the operator and get an explicit
go-ahead before deleting anything. Default to list/dry-run; deletion is opt-in.

Only sweep resources whose names carry the run-token prefix of the aborted run(s).
**Never** touch shared or long-lived infra, and never operate outside the sandbox
project.

### 4. Delete (only after confirmation)

```bash
# clusters
gcloud container clusters delete <name> --location <loc> --project "$PROJECT" --quiet
# node SAs (the 409 culprit)
gcloud iam service-accounts delete "gke-nodes-<cluster>@${PROJECT}.iam.gserviceaccount.com" --project "$PROJECT" --quiet
# secrets
gcloud secrets delete <name> --project "$PROJECT" --quiet
# auto-mode VPCs — delete dependent firewall rules first, then the network
gcloud compute firewall-rules delete <rule> --project "$PROJECT" --quiet
gcloud compute networks delete <name> --project "$PROJECT" --quiet
# Artifact Registry repos
gcloud artifacts repositories delete <name> --location <loc> --project "$PROJECT" --quiet
```

Delete in dependency order: firewall rules before their VPC, and clusters before
their node SAs (deleting the SA first can wedge the cluster's teardown). After
deleting, re-run the discovery in step 2 to confirm the leak is gone.

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
- The deterministic `gke-nodes-*` SA is the usual `409 already exists` cause — when
  in doubt, that's the one to sweep.
