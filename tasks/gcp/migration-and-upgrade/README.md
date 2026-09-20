# migration-and-upgrade

A GKE cluster starts at Kubernetes 1.34 with nothing deployed. The application manifests
live in a bare git repo at `~/migration-repo-<cluster>.git`: a web Deployment, its Service,
an Ingress on `networking.k8s.io/v1beta1` and a PodDisruptionBudget on `policy/v1beta1`.
The agent is asked to upgrade the cluster to the next minor and write
`production-readiness.md`; nothing names the deprecated APIs.

## Grading

task.yaml reads the repo with `git_repo_sync` (stable apiVersions in a new commit, the v1
Ingress backend schema and `pathType`, no v1beta1 left) and the cluster with kubectl (the
v1 Ingress and the PodDisruptionBudget live, web Ready, every node on a 1.35 kubelet,
kube-dns healthy, web still at two replicas). Validating before applying, upgrading in
place and cleaning up scaffolding are judged from the trajectory.

The node pool has two nodes and the PodDisruptionBudget is `minAvailable: 1` over two
replicas, so the drain has exactly enough capacity; scaling web down or deleting the budget
is the change the checks catch.

## Run

The stack provisions the cluster, a run-unique service account the sandboxed agent's
`gcloud` calls run as (`identity.tf`), and the repo. The provisioning identity needs
`roles/container.admin`, `roles/iam.serviceAccountAdmin`, `roles/iam.serviceAccountUser`,
`roles/resourcemanager.projectIamAdmin` and `roles/compute.admin`; allow about ten minutes
for a fresh grant to propagate.

```bash
export PROJECT_ID=<project> CLUSTER_NAME=migration-upgrade GCP_LOCATION=us-central1-a NAMESPACE=migration
python -m devops_bench tasks/gcp/migration-and-upgrade/task.yaml
```

`start_version` in task.yaml must be a GKE minor that is still supported and has a next
minor; check with `gcloud container get-server-config --zone "$GCP_LOCATION"`.
