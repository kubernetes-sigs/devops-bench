# Gemma 3 12B (finetuned) — GKE serving manifests

vLLM serving the finetuned Gemma 3 12B checkpoint from
`gs://hypercomputer-d1-models-<PROJECT_ID>/gemma3-12b-finetuned`, mounted via the
GCS FUSE CSI driver, autoscaled on request-queue depth.

| File | Purpose |
| --- | --- |
| `00-namespace.yaml` | `ml-serving` namespace |
| `01-serviceaccount.yaml` | KSA for Workload Identity → GCS read access |
| `02-deployment.yaml` | vLLM server, 2× L4 per replica, GCS FUSE model mount |
| `03-service.yaml` | ClusterIP on port 80 → container 8000 |
| `04-podmonitoring.yaml` | GMP scrape of vLLM `/metrics` (required by the HPA) |
| `05-hpa.yaml` | HPA 1→8 on `vllm:num_requests_waiting` |
| `06-poddisruptionbudget.yaml` | `maxUnavailable: 1` |

## Before you apply — two things I could not resolve

**1. The cluster and project names are still placeholders.** Your request had
`{{GKE_CLUSTER_NAME}}` and `{{GCP_PROJECT_ID}}` unexpanded, and I could not look
them up: the local gcloud credentials are expired (`gcloud auth list` shows
`ngeugene@google.com`, but `gsutil ls` returns *"Your credentials are invalid"*
and `gcloud container clusters list` hangs). So I could not confirm the cluster
name, the project ID, the bucket's existence, the model's object prefix, or what
GPU node pools you actually have. Substitute before applying:

```sh
sed -i '' "s/PROJECT_ID/$(gcloud config get-value project)/g" 02-deployment.yaml 01-serviceaccount.yaml
```

Also check the `only-dir=gemma3-12b-finetuned` mount option in
`02-deployment.yaml` — I guessed that prefix. It must point at the directory
holding `config.json` and the safetensors shards. Verify with:

```sh
gcloud storage ls gs://hypercomputer-d1-models-<PROJECT_ID>/
```

**2. The cluster your kubeconfig points at is not GKE.** The only reachable
context is `dbench-skill-demo`, which is a single-node **kind** cluster on arm64
with no GPUs, no CSI drivers, and no metrics API. These manifests will not run
there — `nvidia.com/gpu` requests are unschedulable, the `gcsfuse.csi.storage.gke.io`
driver does not exist, and the `PodMonitoring` CRD is absent. They are written
for a real GKE cluster. I validated all six built-in resources against the live
API server with `kubectl apply --dry-run=client --validate=strict` (all pass);
the schema is sound, the runtime target is just elsewhere.

## Cluster prerequisites

```sh
gcloud container clusters update <CLUSTER> --region <REGION> \
  --update-addons=GcsFuseCsiDriver=ENABLED \
  --enable-managed-prometheus \
  --workload-pool=<PROJECT_ID>.svc.id.goog
```

Plus a **Custom Metrics Stackdriver Adapter** in the cluster — without it the
HPA's `External` metric never resolves and the deployment sits at `minReplicas`:

```sh
kubectl apply -f https://raw.githubusercontent.com/GoogleCloudPlatform/k8s-stackdriver/master/custom-metrics-stackdriver-adapter/deploy/production/adapter_new_resource_model.yaml
```

And a GPU node pool that can grow — node-level autoscaling is what makes the pod
autoscaler meaningful:

```sh
gcloud container node-pools create l4-pool --cluster <CLUSTER> --region <REGION> \
  --machine-type g2-standard-24 --accelerator type=nvidia-l4,count=2 \
  --ephemeral-storage-local-ssd count=2 \
  --enable-autoscaling --min-nodes 1 --max-nodes 8
```

Grant the KSA bucket access as documented in `01-serviceaccount.yaml`.

## Apply

```sh
kubectl apply -f .
kubectl -n ml-serving rollout status deploy/gemma3-12b-ft --timeout=20m
```

## How the autoscaling works

`vllm:num_requests_waiting` is the count of requests admitted but not yet
scheduled onto the GPU. It is the right signal here because a fully saturated
GPU registers as almost no CPU load, so a CPU-based HPA would never fire. The
HPA targets an average of 10 queued requests per replica.

The asymmetry in `behavior` is deliberate. Scale-up is immediate (no
stabilization window, up to 2 pods/min) because queueing is already
user-visible latency. Scale-down waits out a 15-minute stabilization window and
removes one pod per 5 minutes, because each replica is a GPU node plus ~24 GiB
of weights to page in — roughly 10 minutes to get back, so thrashing is far more
expensive than briefly over-provisioning.

Two knobs worth tuning once you have load-test data:

- `averageValue: "10"` in the HPA — lower it if time-to-first-token matters more
  than GPU utilization.
- `--max-num-seqs=128` and `--max-model-len=16384` in the deployment. I sized
  these for 2× L4 (48 GiB total VRAM, ~24 GiB of that is bf16 weights). Gemma 3
  supports a 128k context; you cannot have 128k *and* a useful batch size on L4.
  If you need long context, move to `a2-highgpu-1g` (1× A100 40GB) or H100 and
  drop `--tensor-parallel-size` to 1.

## Notes on the cluster's Kyverno policies

Both are `Audit` (non-blocking), but the manifests comply anyway:

- `require-resource-limits` — every container needs CPU + memory limits. The
  vLLM container sets them directly; the *injected* GCS FUSE sidecar is covered
  by the `gke-gcsfuse/{cpu,memory,ephemeral-storage}-limit` annotations, which
  is easy to miss since that container does not appear in this YAML.
- `disallow-privileged-containers` — the vLLM container sets
  `privileged: false`, `allowPrivilegeEscalation: false`, drops all capabilities.

## Not included

No external exposure — the Service is ClusterIP. Add a Gateway/`HTTPRoute` or an
Ingress when you know whether this is internal-only or public, and whether it
needs auth in front of it.
