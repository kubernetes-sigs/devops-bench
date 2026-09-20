# Autoscaling A Workload Under A Live Load Spike

This task evaluates an agent's ability to **make an under-provisioned workload survive a traffic
surge**, using only what the live cluster tells it. A CPU-burn HTTP service is running with **no
resource requests or limits** and **no HorizontalPodAutoscaler**. Five seconds after the agent
starts, a load generator begins driving traffic at the Service and keeps going for five minutes —
so the surge is something the agent has to handle while it works, not a test run afterwards.

The prompt does not name the fix. It says the workload "has to absorb a traffic surge without
falling over" and asks the agent to inspect cluster state and metrics; deciding that this means
requests, limits and an HPA is part of the task.

Runs on **GKE**. See [Why GKE](#why-gke) below.

## How it works

- **Infrastructure** (`tf/prebuilt/optimize-scale`) provisions a cluster and seeds a Deployment
  and Service both named `scale-target` in `default`. The container is a single-replica Python
  HTTP server that burns ~3M integer multiply-adds per request, listening on port 8080. It has no
  `resources` block, so the scheduler has no signal and a resource-metric HPA has no denominator.
- The Deployment carries `lifecycle { ignore_changes = [spec[0].replicas] }`, so a re-apply does
  not fight whatever the agent's autoscaler does.
- **Chaos** injects one `generate_load` fault at `delay_seconds: 5`. `task.yaml` spells the target
  as `http://scale-target.default.svc.cluster.local`, but that value is not what the generator
  dials: the harness rewrites `service_url` to the Service's external LoadBalancer URL, falling
  back to a local `kubectl port-forward` when the LoadBalancer has no address. The load therefore
  arrives from outside the cluster, through the Service — which is why the selector safeguard
  below is load-bearing and not decorative.
- **The agent** inspects the workload and the metrics pipeline, sets requests and limits, creates
  an HPA, watches the surge land, and writes `scaling-report.md`.

## What is graded

`expected_output` covers only `scaling-report.md` and is explicitly non-gating. Everything about
the cluster is machine-checked in `verification_spec`.

**Objectives** (total weight 7.0):

| Weight | Entry | Passes when |
| --- | --- | --- |
| 2.0 | Planned Load Spike Verification | ≥ 2 replicas are Ready and the target's pods are healthy |
| 2.0 | HPA Configured For The Target Deployment | an HPA in the namespace targets `scale-target`, has `minReplicas ≥ 2`, and has a `cpu` resource metric |
| 2.0 | Autoscaler Is Reading Live Metrics | that HPA reports `ScalingActive=True` |
| 1.0 | Resource Requests And Limits Set | every container in the target has both a `requests` and a `limits` block |

**Safeguards:**

| Severity | Entry | Fails when |
| --- | --- | --- |
| catastrophic | Target Deployment Still Present | nothing named `scale-target` is left in the namespace |
| recoverable | Target Workload Still Available | the Deployment's `Available` condition is not `True` — e.g. a memory limit that OOMKills the container into `CrashLoopBackOff`, or a rollout that leaves too few Pods Ready |
| recoverable | Target Service Still Routes To The Workload | no Service in the namespace still selects `app=scale-target` |

The four objectives split two and two, and the split is deliberate. `HPA Configured For The
Target Deployment` and `Resource Requests And Limits Set` read `spec`: they ask whether the agent
declared the right thing. `Autoscaler Is Reading Live Metrics` and `Planned Load Spike
Verification` read `status`: they ask whether the thing the agent declared actually did anything.
An HPA can be shaped correctly and still be inert, and a `resources` block can be present and
still be wrong, so neither pair substitutes for the other.

The fixture as seeded has **no readiness probe**, so a running container is Ready. Throttling the
app with a low CPU limit therefore slows it down but does not by itself make `Available` false.
What does: an OOMKill or crash loop, a readiness probe the agent adds that then fails, or a
rollout that leaves too few Pods Ready.

### Known gap: the replica floor

There is no way, with the verifiers currently registered, to distinguish an agent that built a
working autoscaler from one that set `minReplicas` high and left it there. `minReplicas ≥ 2`
alone is satisfiable by a floor, and `across_matches` cannot compare one field of an object
against another, so "observed scale exceeds this HPA's own floor" is not expressible.

`ScalingActive=True` is the closest available proxy and it does real work — it is the API
server's own statement that the autoscaler read utilisation and computed a recommendation, which
an inert HPA with no metrics pipeline and no requests cannot produce. But an agent that sets
requests, sets a CPU metric, and sets `minReplicas: 5` will score 1.0 without the autoscaler ever
having reacted to anything.

Closing this needs a verifier that resolves two paths on the same object and compares them
(`status.desiredReplicas` against `spec.minReplicas`). That is a harness change, not a task
change, and is left as a follow-up.

### Known gap: the HPA objectives are not bound to one HPA

`HPA Configured For The Target Deployment` is an `all` over three `resource_property` checks, and
none of them can set `resource_name` — the agent chooses the HPA's name, so the task cannot know
it in advance. Each check therefore matches *every* HorizontalPodAutoscaler in the namespace
independently, and the `all` is satisfied if some HPA supplies `spec.scaleTargetRef.name`, some
HPA supplies `minReplicas ≥ 2`, and some HPA supplies the CPU metric — not necessarily the same
one. An agent that leaves two partial HPAs behind clears the entry.

`across_matches: every` does not help: it quantifies over the path elements resolved within one
object, not over the matched objects themselves.

Closing this needs the same class of harness change as the floor above — an object-scoped `all`,
where one matched object must satisfy every child check — and is likewise left as a follow-up.

## Why the chaos parameters are what they are

`qps: 300` with `concurrency: 2` and `duration: "300s"` looks conservative and is deliberate.

- **Concurrency.** The handler is GIL-bound at roughly 4 rps. Every request past about ten in
  flight exceeds the load generator's 3s client timeout; at `concurrency: 32` the generator died
  after 11s having delivered nothing measurable. Two connections is enough to peg the pod's CPU,
  which is all the HPA needs to see. Raising concurrency does not raise difficulty, it destroys
  the measurement.
- **Duration.** Long enough that the spike is still running when the agent finishes and
  verification starts, so the objectives observe a cluster that is genuinely under load rather
  than one that was, briefly, ten minutes ago. Safe as long as
  `delay + duration + verification` fits inside the run's agent budget.
- **No QPS floor.** Gating the fault on an achieved-QPS threshold voids the spike — and with it
  the objective that references it — for a cluster that had already scaled correctly. The
  autoscaling outcome is the measurement; the generator's own throughput is not.

**Caveat.** The load generator is driven by the agent runtime, so it can decline or fail to
deliver, and this harness does not currently mark a stimulus that never landed. If the spike dies
at 15s, the objectives above are still evaluated against a cluster that was never stressed, and
`scaling_complete` will simply reflect whatever `minReplicas` the agent chose. Check the chaos
entry's status in `results.json` before reading a passing score as evidence the workload absorbed
anything.

## Why GKE

The metrics objective needs a working metrics pipeline. GKE ships metrics-server; a stock kind
cluster does not, so `ScalingActive` would read `False` there for reasons that have nothing to do
with the agent.

`tf/prebuilt/optimize-scale` still supports `infra_provider=kind` — it swaps the Service to
`ClusterIP` and relies on the harness port-forward — and running with `INFRA_PROVIDER=kind` is
much cheaper if you only want to exercise the fixture. Expect **Autoscaler Is Reading Live
Metrics** to fail unless you install metrics-server yourself.

## Run

```bash
export CLUSTER_NAME="optimize-scale-1"
export NAMESPACE="default"
export TARGET_DEPLOYMENT_NAME="scale-target"
export PROJECT_ID="<your-project-id>"
export GCP_PROJECT_ID="<your-project-id>"

export AGENT_PROVIDER="google-vertex"
export AGENT_MODEL="gemini-3.1-pro-preview"
export JUDGE_PROVIDER="google-vertex"
export JUDGE_MODEL="gemini-3.1-pro-preview"

python -m devops_bench --infra --project "$PROJECT_ID" --cluster "$CLUSTER_NAME" \
  tasks/common/optimize-scale/task.yaml
```

Budget roughly 25 minutes per run; most of that is cluster create and destroy.

## Verify the environment manually (optional smoke test)

```bash
cd tf/prebuilt/optimize-scale
tofu init && tofu apply -auto-approve -var=infra_provider=kind -var=cluster_name=os-kind \
  -var=kubeconfig_path=~/.kube/config

kubectl get deploy scale-target -o jsonpath='{.spec.template.spec.containers[*].resources}'
# -> {} : no requests, no limits. This is the fixture, not a provisioning failure.
kubectl get hpa                     # -> No resources found. Also the fixture.
kubectl get svc scale-target        # port 8080

tofu destroy -auto-approve -var=infra_provider=kind -var=cluster_name=os-kind \
  -var=kubeconfig_path=~/.kube/config
```

## Troubleshooting

| Symptom | Cause / Fix |
| --- | --- |
| `Autoscaler Is Reading Live Metrics` fails on a run where the agent did everything right | metrics-server not ready yet, or the agent set limits but no requests. Check `kubectl describe hpa` for `FailedGetResourceMetric`. |
| Chaos entry shows `status: failed` a few seconds in | The generator could not reach the Service from outside the cluster. Check that the Service still has a LoadBalancer address (`kubectl get svc scale-target`), still exposes 8080, and still selects `app=scale-target`; the recoverable safeguard covers the selector case. With no address the harness falls back to a port-forward, which a not-yet-Ready pod can race. |
| Every objective passes but the replica count never moved | See [Known gap: the replica floor](#known-gap-the-replica-floor) and the chaos caveat above. |
| Deployment never becomes Available after the agent's edit | Usually a memory limit that OOMKills the burn loop, or a readiness probe the agent added that the throttled app cannot answer in time. A tight CPU limit alone does not do it — with no probe, a throttled container is still Ready. Check `kubectl get pod -o wide` for `OOMKilled` in the last state, and `kubectl describe deploy scale-target` for an added probe. The recoverable safeguard is meant to catch exactly this. |
