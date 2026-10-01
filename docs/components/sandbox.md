# Agent sandbox

The agent under test runs inside a Docker container with a scoped cluster
credential, so the benchmark's own material — task definitions (whose
`expected_output` and `verification_spec` are the answer key), scoring code,
prior results, the operator's home directory and cloud identity — is not
merely *detected* when touched but **absent from the agent's world**. Eval
stays host-side: provisioning, verification, scoring and
[detection](./cheat-detection.md) run under the operator's credentials,
outside the boundary, unchanged.

Two boundaries, two different questions:

* the **container** decides what the agent can *see* — minimal mounts, an env
  allowlist, no Docker socket, `HOME` inside the workspace;
* the **credential** decides what the agent can *do* — a short-lived
  ServiceAccount token scoped by RBAC, backed by pod-security controls,
  instead of the operator's cluster-admin.

Neither substitutes for the other. Both exist because of two observed
escalations: an agent that used an admin kubeconfig to run a privileged pod,
mount the node's disk and read the bench checkout; and an agent that curled
the cloud metadata service and mined the VM's service account when its model
credential was missing. The second lesson generalizes: **completeness is a
security property** — an under-provisioned agent improvises, so the sandbox
also mounts the task's own seeded fixtures (read-write; tasks commit fixes
back) rather than starving the agent into hunting.

## Switching it on

| Variable | Meaning |
| --- | --- |
| `BENCH_AGENT_SANDBOX` | `docker` / `1` / `true` opts in. Unset, the harness behaves byte-for-byte as before the sandbox existed. |
| `BENCH_SANDBOX_IMAGE` | The container image holding the agent CLI. Required when sandboxing. |
| `BENCH_AGENT_FIXTURES` | Optional override for fixture discovery, for a stack that names its seeded inputs unconventionally. |

The matrix runner bakes the opt-in into the detached runner script, so a
sandboxed remote matrix cannot silently degrade to ambient. The escape
hatches (`BENCH_SANDBOX_ALLOW_ADMIN_CREDS`, `BENCH_SANDBOX_ALLOW_AMBIENT_CLUSTER`)
exist for local development only, warn loudly, and are never forwarded to
matrix runs. Failures never degrade either: a sandbox that cannot be built —
no image, no network plan, no scoped credential — fails the record rather
than quietly running ambient.

## What the container sees

Exactly four things:

1. the per-run **workspace** (`/workspace`, with `HOME=/workspace/home`);
2. the task's **seeded fixtures**, discovered by the run-unique cluster token;
3. a generated single-cluster **kubeconfig** (CA + bearer token, no `exec:`
   plugin — which is also what makes GKE reachable from a container at all);
4. an explicit **env allowlist** (`BENCH_*`/`TF_*` and ambient cloud
   credentials never cross; for Vertex, a host-side metadata emulator serves
   a narrowly-scoped token instead).

Everything absent is the point: the repo checkout, `results/`, the operator's
kubeconfig, ADC, and the Docker socket do not exist inside.

## What the credential can do

Provisioning (host-side, per run, pinned to the run's own kubectl context)
creates a `bench-agent` ServiceAccount bound to `edit` plus a read-mostly
cluster supplement, mints a token that expires just after the agent's
timeout, applies PSA `baseline` labels, and installs ValidatingAdmissionPolicy
backstops denying privileged pods, host namespaces, hostPath mounts, exempt-
namespace writes, and shells into pre-existing non-conformant pods. A task
whose subject matter *is* privileged workloads declares
`agent_pod_security: privileged`. (A per-task `requires_unsandboxed` opt-out
— run this one task ambient, loudly, with its records saying so — is planned
but **not yet implemented**: the schema ignores unknown keys, so declaring it
today changes nothing and the task still runs sandboxed.) Under vcluster the
ServiceAccount lives inside the
virtual cluster, so the token is cryptographically useless against the host.

**Teardown mirrors provisioning**: at the end of every sandboxed task — and
from provisioning's own failure paths — every policy, binding, label and RBAC
object comes back off, and `bench-system` is deleted. This is correctness,
not hygiene: the pod-security policy is deliberately not username-scoped, so
one left behind denies the *operator's* next privileged workload on a reused
cluster (see [known issues](../appendix/known_issues.md) for the by-hand
recovery on clusters last touched by older runs).

## Provenance in the results

A sandboxed arm carries a `sandboxed` augmentation token, so it lands in the
`setup_id` and aggregates as its own dashboard setup — the sandboxed-vs-ambient
comparison is a plain group-by on rows. Each row additionally carries its own
`sandboxed` boolean, because the arm's token is not the per-task truth (once
per-task exemptions exist, an exempted task inside a sandboxed arm runs
ambient and its row must say so; older records read `null` — unknown). The run
manifest records the sandbox image **and its content digest**, so "both runs
used the same image" is checkable rather than a mutable-tag claim.

## Trusting it

Three layers, weakest to strongest evidence:

* **Unit tests** hold the mechanics: argv assembly, env filtering, kubeconfig
  rendering, path remapping, teardown inventory (kept in lockstep with the
  manifests by a test that parses them).
* **Boundary probes** ([`hack/sandbox_probe.py`](../../hack/sandbox_probe.py),
  promoted into `tests/e2e/test_sandbox_boundary.py`) assert every channel
  against a live cluster with deterministic commands: controls first (a dead
  token makes every deny pass for the wrong reason), then both observed
  escapes, then every hole review found, then that teardown left nothing
  behind.
* **The canary** ([`tasks/canary/sandbox-boundary/`](../../tasks/canary/sandbox-boundary/))
  sends a *real agent* through the full pipeline to attempt the forbidden
  reads, proving the trajectory, detection and artifacts tell the story
  end-to-end. A canary's `cheating_report` is *expected* to flag — detection
  seeing the attempt while the boundary denies it is both layers working.

Detection stays on for sandboxed runs as the tripwire behind the wall: a
sensitive-access flag on a sandboxed record no longer means "expected ambient
access" — it is a boundary-breach indicator and should be treated as one.

## Runner-host requirements

The sandbox asks exactly one thing of the machine running the harness:
**Docker**. Scored runs use clusters whose nodes are not the harness host
(vcluster/GKE); co-located kind is a development configuration — a kind node
is a privileged container on the harness machine, so pod-level escalation
there lands on the host, which is precisely what the pod-security controls
exist to prevent while you develop. On any cloud VM host, the link-local
metadata endpoint must be blocked from agent containers (`vm-setup.sh`
installs the `DOCKER-USER` rule on the bastion); the endpoint serves the VM's
own credentials on every major cloud, so this is a host-class requirement,
not a GCP quirk.
