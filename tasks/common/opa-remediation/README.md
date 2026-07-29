# Autonomous Kyverno Remediation

This task evaluates an agent's ability to **detect and remediate compliance violations** across
multiple namespaces using policy-as-code. The cluster runs **Kyverno** with two audit-mode
policies; several team workloads violate them (privileged containers, and workloads missing CPU/
memory limits). The agent must scan, remediate the manifests in a GitOps repo, apply the fixes,
verify compliance, and report.

Runs on **kind** (local, on the runner VM): no cloud dependency.

## How it works

- **Infrastructure** (`tf/prebuilt/opa-remediation`) provisions a kind cluster and runs
  `scripts/setup.sh`, which installs Kyverno, applies two **audit** `ClusterPolicy`s
  (`disallow-privileged-containers`, `require-resource-limits`), deploys team workloads (across
  `team-alpha`/`team-beta`/`team-gamma`, with `owner`/`env` labels), some violating, one
  compliant as a control, and seeds a per-run local bare git repo `~/opa-repo-<cluster_name>.git`
  (run-unique so concurrent runs don't collide; the prompt uses `{{CLUSTER_NAME}}`) with the workload
  manifests (the GitOps source of truth).
- Audit mode means the violating workloads exist **live** and are flagged in PolicyReports,
  rather than being blocked at admission. Nothing in the cluster names the fix. The agent
  discovers the violations by scanning (`kubectl get policyreport,clusterpolicyreport -A`).
- **The agent** scans, removes `privileged: true` and adds resource limits in the repo manifests,
  commits/pushes, applies them, verifies the reports go clean, ideally flips the policies to
  enforce to prevent regressions, and writes `report.md`.

## Setup (run on the GCE VM)

As with the other complex tasks, run on the runner VM so kind and the agent are co-located.
Prereqs (one-time):

- Docker (running), `kind`, `kubectl`, `tofu`, and the `oc` binary at `~/bin/oc`.
- Python ≥ 3.10 venv: `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`.
- `fs.inotify` bump (kind) + ≥ 20 GB free disk (kind node image + Kyverno + workloads):
  ```bash
  echo -e "fs.inotify.max_user_watches=524288\nfs.inotify.max_user_instances=512" | sudo tee /etc/sysctl.d/99-kind.conf
  sudo sysctl --system
  ```

```bash
ssh <you>@<runner-vm>
cd ~/devops-bench && git checkout complextask3 && git pull
source .venv/bin/activate
```

## Run

```bash
export CLUSTER_NAME="opa-kind"         # used as the kind cluster name
export NAMESPACE="default"             # unused by this task; just needs to be set
export PROJECT_ID="local-kind"         # Required by the harness validator; use any dummy string for local runs
export GCP_PROJECT_ID="<your-project-id>" # Required by the google-vertex provider, for both agent and judge

export BENCH_AGENT_TYPE="gemini"
export AGENT_PROVIDER="google-vertex"  # uses GCP_PROJECT_ID above
export AGENT_MODEL="gemini-3.1-pro-preview"
export JUDGE_PROVIDER="google"
export JUDGE_MODEL="gemini-3.1-pro-preview"
# No API key needed here: gcloud ADC authenticates both the agent and the judge.
# For a plain API-key setup instead, set AGENT_PROVIDER="google" and export
# AGENT_API_KEY; the judge shares it.

python -m devops_bench tasks/common/opa-remediation/task.yaml
```

## Verify the environment manually (optional Phase-1 smoke test)

```bash
cd tf/prebuilt/opa-remediation
tofu init && tofu apply -auto-approve -var=infra_provider=kind -var=cluster_name=opa-kind -var=kubeconfig_path=~/.kube/config
export KUBECONFIG=~/.kube/config && kubectl config use-context kind-opa-kind
# kind creates this context by default. A duplicate gke_<project_id>_<location>_opa-kind context is
# also created, for tooling that expects GKE-style context names.

kubectl get pods -n kyverno                              # Kyverno controllers Running
kubectl get cpol                                         # both ClusterPolicies (Audit)
kubectl get deploy -A | grep -E 'team-'                  # team workloads
kubectl get policyreport,clusterpolicyreport -A          # FAIL results for the violations
git clone ~/opa-repo-<cluster_name>.git /tmp/opa && ls /tmp/opa/workloads && rm -rf /tmp/opa

tofu destroy -auto-approve -var=infra_provider=kind -var=cluster_name=opa-kind -var=kubeconfig_path=~/.kube/config
```
You should see failing policy results for the privileged workloads (`cache`, `payments`) and the
limitless workloads (`web`, `worker`), but **not** for the compliant `api`.

## Results

`results/run_<timestamp>/`:
- `results.json`: per-check scores + the agent's full trajectory (scan + remediation + commit).
- `generated_files/report.md`: the report the agent wrote.

## Troubleshooting

| Symptom | Cause / Fix |
| --- | --- |
| `failed to join node with kubeadm … exit status 1` | inotify limits: apply the sysctl bump above. |
| `Error: … no space left on device` | Disk too small: grow to ≥ 20 GB. |
| `TypeError: unsupported operand type(s) for \|: …` on import | Python < 3.10: use a 3.10+ venv. |
| Kyverno pods not Ready / policy CRD missing | Re-run; `setup.sh` waits for the CRD + controllers. Check the pinned `KYVERNO_VERSION` is compatible with the kind node's k8s version. |
| No PolicyReports appear | Kyverno background scan can take ~30–60s after install; re-check `kubectl get polr -A`. |
