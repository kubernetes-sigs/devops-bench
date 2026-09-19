# API Deprecation Migration and Version Upgrade

This task evaluates the agent's ability to perform a safe Kubernetes minor-version upgrade:
audit version-controlled manifests for **deprecated/removed APIs**, migrate them to their
stable equivalents, validate the changes on the target version, apply them, upgrade the
cluster, and report.

It runs on **kind by default** (cheap, local) and can also run on **GKE** (to exercise a real
managed master + node-pool upgrade). The agent-facing flow is identical on both — only the
cluster substrate and the upgrade mechanism differ.

## How it works

- **Infrastructure** provisions a cluster at a **start** version and seeds a **local bare git
  repo** (`~/migration-repo-<cluster_name>.git`, run-unique so concurrent runs don't collide;
  the prompt uses `{{CLUSTER_NAME}}`) with application manifests that use deprecated APIs
  (`networking.k8s.io/v1beta1` Ingress, `policy/v1beta1` PodDisruptionBudget). The repo is the
  agent's source of truth — there is no mock audit script and nothing names the deprecations
  in-cluster; the agent must discover them itself.
- **The agent** clones the repo, audits the manifests with a real tool of its choice (e.g.
  `pluto`, `kubent`, or `kubectl` dry-run/convert), migrates the deprecated resources to stable
  APIs, commits/pushes, validates on the target version, applies, upgrades the cluster, and
  writes `production-readiness.md`.

## Shared setup (run on the GCE VM)

As with cp-recovery, the eval, the cluster, and the agent must be co-located, so run on the
runner VM. Prereqs (one-time):

- Docker (running), `kind`, `kubectl`, `tofu`, and the `oc` binary at `~/bin/oc`.
- Python ≥ 3.10 with a venv: `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`.
- Host tuning for multi-node/extra kind clusters (the agent creates a temporary validation cluster):
  ```bash
  echo -e "fs.inotify.max_user_watches=524288\nfs.inotify.max_user_instances=512" | sudo tee /etc/sysctl.d/99-kind.conf
  sudo sysctl --system
  ```
- ≥ 50 GB disk (the agent may run two kind clusters at once — prod + validation).

```bash
ssh <you>@<runner-vm>
cd ~/devops-bench && git checkout complextask2 && git pull
source .venv/bin/activate
```

## Run on kind (default)

```bash
export CLUSTER_NAME="migration-kind"   # used as the kind cluster name
export NAMESPACE="migration"
export PROJECT_ID="local-kind"         # Required by the harness validator; use any dummy string for local runs

export BENCH_AGENT_TYPE="cli"
export AGENT_TARGET="oc"
export AGENT_PROVIDER="google"
export AGENT_MODEL="gemini-3.1-pro-preview"
export AGENT_API_KEY="<your-gemini-key>"
export JUDGE_PROVIDER="google"
export JUDGE_MODEL="gemini-3.1-pro-preview"
export JUDGE_API_KEY="<your-gemini-key>"

python -m devops_bench tasks/common/migration-and-upgrade/task.yaml
```

On kind, "upgrade" is reframed: the agent validates the migrated manifests on a temporary kind
cluster it creates at the target version (kind has no in-place managed upgrade).

## Run on GKE (real managed upgrade)

Point the task at the GKE stack and provide real GCP credentials.

### One-time IAM prerequisite (must be done by a project admin)

On the runner VM, `tofu` authenticates as the VM service account
(`openclaw-vm-sa@<project>.iam.gserviceaccount.com`). Provisioning a self-contained GKE
environment (cluster + node SA + IAM bindings + firewall) requires broad rights that this SA
does **not** have by default. These grants **cannot** be automated in the stack — the stack
itself defines IAM bindings, so it can't run until the SA already has `setIamPolicy`
(chicken-and-egg). A project Owner/IAM-admin must grant them once, out-of-band (e.g. from Cloud
Shell), **not** from the VM:

```bash
PROJECT=<your-project-id>
SA=openclaw-vm-sa@${PROJECT}.iam.gserviceaccount.com
for role in \
  roles/container.admin \
  roles/iam.serviceAccountAdmin \
  roles/iam.serviceAccountUser \
  roles/resourcemanager.projectIamAdmin \
  roles/compute.admin ; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:$SA" --role="$role"
done
```

(The kind path needs none of this.)

> **Gotcha — `container.admin` gets stripped by teardown.** The shared GKE module grants
> `roles/container.admin` to the same VM SA that `tofu` runs as, and manages it as a
> `google_project_iam_member`. Because that's the *same* IAM binding your bootstrap grant
> creates, `tofu destroy` (teardown) **deletes it** — so the next run starts without it,
> re-creates it mid-`apply`, and then hits GKE's IAM propagation lag → a
> `container.clusters.create` 403. To make runs repeatable, grant the SA a create-capable role
> the stack does **not** manage, so teardown can't strip it:
> ```bash
> gcloud projects add-iam-policy-binding "$PROJECT" \
>   --member="serviceAccount:$SA" --role="roles/container.clusterAdmin"
> ```
> After any fresh IAM grant, **wait ~10 min for propagation** before running, or cluster
> creation may still 403.

```bash
# INFRA_PROVIDER is the whole switch: the stack is provider-parameterized, and the
# env var outranks the task's own `provider: "kind"`. Without it the GKE procedure
# below silently provisions a local kind cluster instead. No edit to task.yaml.
export INFRA_PROVIDER="gcp"

export PROJECT_ID="<your-project-id>"
export CLUSTER_NAME="migration-upgrade"
export GCP_LOCATION="us-central1-a"
export NAMESPACE="migration"
# ...same agent/judge vars as above...

python -m devops_bench tasks/common/migration-and-upgrade/task.yaml
```

Notes:
- The **start version** (`start_version` in `tf/prebuilt/migration-and-upgrade/variables.tf`)
  must be a **currently-supported GKE minor that still has a next minor available** — the agent
  upgrades to the next minor via `gcloud container clusters upgrade` (master + node pool).
  GKE's supported range **drifts over time**, so this default goes stale and will eventually be
  rejected (`No valid versions with the prefix ...`). Check the current set and update the
  default accordingly:
  ```bash
  gcloud container get-server-config --zone "$GCP_LOCATION" --format="yaml(channels)"
  ```
  (If a bare minor like `1.33` is rejected by the default version set, pin a full version such
  as `1.33.12-gke.1059000`.)
- Running on the VM is recommended even for GKE, because the agent still uses local `kind` for
  the cheap target-version validation. (Local Mac works for reachability since GKE is remote,
  but you'd have to replicate `oc`/Python 3.10/gcloud/docker/kind locally.)

## Results

`results/run_<timestamp>/`:
- `results.json` — per-check scores + the agent's full trajectory (the real audit + migration commands).
- `generated_files/production-readiness.md` — the report the agent wrote.

## Troubleshooting

| Symptom | Cause / Fix |
| --- | --- |
| `failed to join node with kubeadm … exit status 1` | inotify limits — apply the sysctl bump above. |
| `Error: … no space left on device` | Disk too small — the agent runs 2 kind clusters; grow to ≥ 50 GB. |
| `TypeError: unsupported operand type(s) for \|: …` on import | Python < 3.10 — use a 3.10+ venv. |
| `No such file or directory: 'tofu'` / `kind` / `docker` | Missing prerequisite — install it. |
| GKE: `403 ... permission denied` on cluster/SA/IAM/firewall create | The VM SA lacks provisioning rights — run the one-time IAM prerequisite grants (see "Run on GKE"). |
| GKE: start version not creatable | `start_version` is outside GKE's supported range — bump it. |
