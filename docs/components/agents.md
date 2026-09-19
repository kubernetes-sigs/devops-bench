# Agents

An **agent harness** is the thing under test. It drives one AI agent against one
task prompt and hands back a typed result the rest of the benchmark can score.
Everything in this layer lives under `devops_bench/agents/`.

The base class is `AgentHarness` (`devops_bench/agents/base.py`). It owns two
concerns so subclasses never have to: the base `run()` stamps wall-clock
**latency** onto every result, and it wraps the agent in a **safety net** — any
crash inside the agent is caught and turned into an errored result, so one faulty
agent never aborts the whole benchmark. Subclasses implement a single method,
`_execute()`, which does the provider-specific work and returns an `AgentResult`.

```text
agent.run(prompt) -> AgentResult     # base: latency + safety net
   └─ agent._execute(prompt)         # subclass: build invocation, parse, return
```

## Supported harnesses

Five harnesses ship today. Each self-registers under a canonical key.

| Key | Wraps | How it runs | Capabilities |
| --- | --- | --- | --- |
| `gemini` | The Google **Gemini CLI** binary | Headless subprocess; trajectory parsed from `--output-format stream-json` on stdout | MCP, skills, rules, allowed-tools |
| `openclaw` | The **Openclaw Agent CLI** | `openclaw agent --local` with per-run isolated state/config; trajectory via `openclaw sessions export-trajectory` | MCP, skills, rules |
| `antigravity` | The **Antigravity CLI** (`agy` binary) | Headless subprocess that keeps the real `HOME` so cached OAuth/ADC credentials work (see the trust-boundary note below); trajectory parsed from the transcript JSONL it writes, token usage read from the conversation DB | MCP, skills, rules |
| `api` | **In-process** model call | Calls `get_model(provider, model)` and runs a model-agnostic MCP tool-use loop (`max_turns`, default 50) | MCP (spawns a stdio server), skills (served as tools), rules (system instruction) |
| `adk` | Any agent built with the **Agent Development Kit** | Imports the agent named by `AGENT_TARGET` and drives a deep copy of it in-process through ADK's `Runner`; trajectory folded from the `function_call` / `function_response` parts of the event stream | MCP (attached as an `McpToolset`), skills (appended to the instruction), rules (appended to the instruction) — all three applied to **every agent in the tree** |

> `oc` is just a shorthand alias for the `openclaw` CLI; this doc uses `openclaw` throughout.

> [!NOTE]
> The `gemini` key names the CLI **harness** — the program that drives the agent.
> It is not the gemini **model**. You can run the gemini *model* through the `api`
> harness, or run a non-gemini model through the `gemini` CLI, because the harness
> and the model are chosen independently (see [Harness vs model](#harness-vs-model)).
> The alias `gemini-cli` also resolves to `gemini`, and is the default agent type.

## Harness vs model

A harness does **not** hardcode a model. It reads `AGENT_PROVIDER` and
`AGENT_MODEL` from its config and maps them onto whatever it drives.

Every harness resolves `AGENT_PROVIDER` through one shared contract
(`devops_bench/core/model_providers.py`), so the same `AGENT_*` config behaves
identically across them. The `api` harness uses it to pick the adapter family and
backend for `get_model(provider, model)` and runs the tool-use loop in-process.
The CLI harnesses (`gemini`, `openclaw`) use it to route `AGENT_API_KEY` onto the
binary's provider-specific env var(s) and pass the model through: the Gemini CLI
gets `GEMINI_MODEL`, and openclaw gets a `--model provider/id` flag. Either way,
the model is a runtime input, never baked into the harness.

`adk` sits outside that contract on purpose: model routing belongs to ADK, which
resolves a model string (and its credentials) itself. So `AGENT_PROVIDER` and
`AGENT_API_KEY` are **not consumed** by this harness — authenticate the way ADK
expects (`GOOGLE_API_KEY`, ADC, or a `BaseLlm` the agent constructs itself).
`AGENT_MODEL` *is* honored: it overwrites `model` on the root agent **and every
sub-agent**, so the whole tree runs on the model the benchmark says it did. Leave
it unset to run the agent on whatever model its author configured.

`antigravity` is the exception: it does not go through the shared contract. It
writes `AGENT_API_KEY` straight onto `GEMINI_API_KEY` and `GOOGLE_API_KEY` and
maps the model onto `GEMINI_MODEL` (`agents/cli/antigravity/agent.py`), so it is
Gemini-only in practice — pointing `AGENT_PROVIDER` at another provider will not
route it.

> [!WARNING]
> **`antigravity` runs with the operator's real `HOME`.** That is deliberate, so
> cached OAuth/ADC credentials keep working without a re-login, but it means the
> agent under test inherits read access to everything in that home directory —
> `~/.config/gcloud`, `~/.ssh`, shell history, other tools' tokens. Every other
> harness gets an isolated per-run state directory. Run untrusted agents under a
> dedicated account or an isolated `HOME`, and treat any credential reachable
> from that home as exposed to the agent.

For everything about providers, model ids, and how `get_model` resolves them, see
[Model providers](./model_providers.md).

## Configuring a harness for an eval

Configuration is env-driven. The benchmark reads neutral `AGENT_*` variables and
each harness maps them onto its target.

**Selecting the harness**

| Variable | Default | Notes |
| --- | --- | --- |
| `BENCH_AGENT_TYPE` | `gemini-cli` (resolves to `gemini`) | The canonical key or an alias. The `--agent-type` flag overrides it. |

**Agent config**

| Variable | Default | Notes |
| --- | --- | --- |
| `AGENT_MODEL` | unset | Model id; flows to the harness's target. |
| `AGENT_PROVIDER` | unset | Provider key (e.g. `gemini`, `anthropic`, `google-vertex`). |
| `AGENT_API_KEY` | unset | Routed onto the provider's key env var(s) via the shared contract; omitted for keyless backends (Vertex/Bedrock ADC). |
| `AGENT_TARGET` | unset | Path to the CLI binary (`gemini` / `oc`). For `adk`, the **import target** of the agent to run (see below); required there. Ignored by `api`. |
| `AGENT_TIMEOUT_SEC` | `600` | Wall-clock budget for each external call. |
| `AGENT_MAX_TURNS` | harness default (50 for `api`) | Caps the `api` tool-use loop. |

**Capabilities**

| Variable | Default | Notes |
| --- | --- | --- |
| `BENCH_USE_MCP` | `true` | Master gate. `false` drops the MCP binding entirely. |
| `AGENT_MCP_SERVER` | unset | Shell-quoted argv for the MCP server (e.g. `"uv run k8s-mcp"`). |
| `AGENT_ALLOWED_TOOLS` | unset | CSV of pre-approved tool names. |
| `AGENT_SKILLS_PATHS` | unset | CSV of directories to discover `SKILL.md` files under. |
| `AGENT_RULES_TEXT` | unset | Operator-brief text handed to the agent. |

### Example: gemini CLI with MCP + skills

```bash
export BENCH_AGENT_TYPE=gemini
export AGENT_PROVIDER=gemini
export AGENT_MODEL=gemini-2.5-pro
export AGENT_API_KEY="$GEMINI_API_KEY"
export AGENT_TARGET=gemini

export BENCH_USE_MCP=true
export AGENT_MCP_SERVER="uv run k8s-mcp"
export AGENT_ALLOWED_TOOLS="list_clusters,get_pods"
export AGENT_SKILLS_PATHS="/opt/skills/devops,/opt/skills/k8s"
```

### Example: api harness on Claude with MCP off

```bash
export BENCH_AGENT_TYPE=api
export AGENT_PROVIDER=anthropic
export AGENT_MODEL=claude-sonnet-4-5
export AGENT_API_KEY="$ANTHROPIC_API_KEY"

export BENCH_USE_MCP=false      # no MCP server is spawned; tools are dropped
```

### Example: adk harness on an existing ADK agent

The SDK is an optional extra, so install it first: `uv sync --extra adk`.

```bash
export BENCH_AGENT_TYPE=adk
export AGENT_TARGET=~/agents/my_agent   # or my_pkg.agent:root_agent
export AGENT_MODEL=gemini-2.5-pro       # optional; unset keeps the agent's own model

export BENCH_USE_MCP=true
export AGENT_MCP_SERVER="uv run k8s-mcp"
export AGENT_ALLOWED_TOOLS="list_clusters,get_pods"
```

Because this harness does not consume `AGENT_PROVIDER` / `AGENT_API_KEY`, model
credentials are resolved by ADK. For a Gemini model that means `google-genai`,
which reads the `GOOGLE_*` variables — to run on Vertex rather than AI Studio:

```bash
unset GOOGLE_API_KEY GEMINI_API_KEY     # either one wins over the Vertex switch
export GOOGLE_GENAI_USE_VERTEXAI=true
export GOOGLE_CLOUD_PROJECT=my-project
export GOOGLE_CLOUD_LOCATION=global
```

For a non-Gemini model the agent supplies its own `BaseLlm` and credentials
follow that provider's convention instead. ADK's `LiteLlm` wrapper covers
Anthropic, OpenAI, and others, but it requires `google-adk[extensions]`, which
the `adk` extra does not install. Token accounting also assumes `google-genai`
usage field names, so a `LiteLlm`-backed run may report usage incompletely.

`AGENT_TARGET` accepts four spellings:

| Target | Resolves to |
| --- | --- |
| `my_pkg.agent:root_agent` | that attribute of that module |
| `my_pkg.agent` | `root_agent` in that module |
| `~/agents/my_agent` | an ADK agent directory (`<dir>/agent.py` exposing `root_agent`) |
| `~/agents/my_agent/agent.py` | that file's `root_agent` |

If the resolved attribute is a factory rather than an agent, it is called with no
arguments — an agent built lazily needs no wrapper. The imported agent is
deep-copied before every run, so the harness's edits (model override, appended
instruction text, MCP toolsets) never mutate the module a second run re-imports.

### Multi-agent trees

Everything the benchmark grants a run — the model override, the MCP toolset, the
rules brief, and discovered skills — is applied to **every agent in the tree**,
not just the root. ADK resolves tools from whichever agent is *active*
(`LlmAgent.canonical_tools`) with no inheritance from a parent, so a root-only
grant would leave any delegate unable to touch the cluster; and a delegate that
never sees the operator brief can violate a constraint the root was told about.
Workflow agents such as `SequentialAgent`, which hold no instruction or tools of
their own, are stepped over.

Two consequences worth knowing:

- A coordinator its author deliberately left toolless **will** receive the MCP
  toolset. That is the intended trade: an agent holding a tool it does not need
  is recoverable, an acting agent with no cluster access is not.
- One MCP binding is still one server process. The same toolset object is shared
  across the tree rather than rebuilt per agent.

The `AgentResult.metadata` mapping records what landed — `model_override_count`,
`instruction_agents`, `mcp_agents`, and `mcp_toolsets`. Note this is on the
in-process result, not the persisted one: the orchestrator's `results.json` does
not currently carry harness metadata, so reading these back needs the
`AgentResult` itself. If a sub-agent uses a *callable* instruction provider, only
that agent is skipped, and it is named in the result's errors — which *are*
persisted.

> [!NOTE]
> The trajectory records the tool calls a tree made but not **which** agent made
> each one. For a single `LlmAgent` that is invisible; for a delegating tree it
> means `transfer_to_agent` hops and the delegate's own calls are flattened
> together.

### Remote agents

A `RemoteA2aAgent` is a `BaseAgent`, so the harness drives one unmodified. What
differs is where its answer lives. ADK attaches the raw A2A task envelope to the
event under `custom_metadata['a2a:response']`, and the answer is that task's
`status.message` — the `content.parts` built alongside it mirror the trailing
artifact instead. The parser reads the envelope, and takes the status message as
the answer only on `completed` — a `working` / `input_required` /
`auth_required` message is progress commentary, and the event's own text is used
instead.

A failed task (`failed`, `canceled`, `rejected`) is recorded on the result's
errors and contributes **nothing** to the output: neither its status message,
which is a failure notice, nor its content parts, which mirror the artifact. The
record is still written as `status: "success"` and scored, so either one left in
the output would be graded as the agent's answer.

A remote agent's trajectory is normally empty and its token counts `None`: the
tool calls and LLM calls happen on the far side of the boundary, so they usually
never reach the event stream as ADK parts. This is a property of what the remote
reports, not a rule the parser enforces — an A2A event carrying
`function_call` / `function_response` parts is folded into the trajectory like
any other, and `usage_metadata` is accumulated wherever ADK supplies it.

The agent runs with the harness-owned workspace as the process working
directory, matching the `cwd` the CLI harnesses hand their subprocess. An agent
with filesystem tools that writes a relative path (`report.md`) therefore lands
it where the orchestrator diffs and collects artifacts. Note this is a
*process-wide* `chdir` for the duration of the run — safe because the harness
drives one agent at a time, but worth knowing if you embed the harness yourself.

## Capabilities

MCP tools, skills, and rules are the three augmentation axes, and they are
independent — an agent may run with any combination, or none. Each is expressed
as a structural Protocol (`SupportsMcp`, `SupportsSkills`, `SupportsRules` in
`devops_bench/agents/capabilities/`): a harness satisfies a Protocol simply by
assigning the matching binding attribute. **MCP** wires the agent to a tool
server, **skills** drop `SKILL.md` files the agent can discover, and **rules**
supply an operator brief. Setting `BENCH_USE_MCP=false` drops the MCP binding
entirely, so the agent sees no tools and the scorer agrees that none ran — skills
and rules are unaffected.

## Sandboxing

Opt-in, off by default. With `BENCH_AGENT_SANDBOX=docker` the agent runs inside
a container (`BENCH_SANDBOX_IMAGE`) that sees the run workspace, the task's
seeded fixtures, a generated kubeconfig, and an explicit env overlay — and not
the repo checkout, `results/`, your `$HOME`, gcloud config, Terraform state, or
the Docker socket. With the switch unset the harness behaves exactly as it did
before the sandbox existed. The design was shaped by two observed incidents:
an agent that used the admin kubeconfig to run a privileged pod and read the
bench checkout through the node's disk, and an agent that mined the harness
VM's cloud identity from the metadata endpoint when its model credential was
missing.

### The cluster credential

The agent does **not** get your kubeconfig. Before it starts, the harness
creates a `bench-agent` ServiceAccount in the `bench-system` namespace, binds it
to the built-in `edit` role cluster-wide plus a small cluster-scoped supplement
(namespaces CRUD; nodes, PVs, storage classes, CRDs read-only), mints a
short-lived token for it, and renders a single-cluster kubeconfig containing
that token and nothing else. The RBAC deliberately grants no write on
`rbac.authorization.k8s.io` or `admissionregistration.k8s.io`, so the agent
cannot grant itself permissions through RBAC objects or remove the admission
policy below. That closes the direct route, not every route: `edit` itself
carries permissions that can reach a stronger identity — see [Known gaps in
the RBAC scope](#known-gaps-in-the-rbac-scope).

Two consequences worth knowing:

- **GKE works in-container because of this.** A normal GKE kubeconfig
  authenticates through `gke-gcloud-auth-plugin`, which needs a `gcloud` binary
  and Application Default Credentials — neither of which the container has, by
  design. A bearer token needs no plugin.
- **Under vcluster the ServiceAccount lives in the virtual cluster**, because
  every call is pinned to the run's own kubectl context. Its token is
  cryptographically useless against the host cluster.

The token's lifetime is the agent's `timeout_sec` plus 15 minutes of slack,
capped at two hours. If a scoped credential cannot be minted — or if pod
security below cannot be applied — the run **fails**; it never falls back to
your admin credential silently. For local development against a cluster where
you cannot create cluster-scoped objects, set
`BENCH_SANDBOX_ALLOW_ADMIN_CREDS=1` to allow the old behaviour explicitly. Never
use it for a scored run.

**On GKE this is an IAM question, not a Kubernetes RBAC one.** GKE gates the
admission-policy resources behind the `container.thirdPartyObjects.*`
permissions, which `roles/container.developer` does not carry, so an operator
who can otherwise deploy freely still cannot apply the policy below — and the
run refuses. Grant `roles/container.admin`, or a custom role including those
permissions, to whatever identity runs the harness. A vcluster run is
unaffected: the policy is applied inside the virtual cluster, where the
generated kubeconfig is already admin.

Provisioning also refuses a cluster that no provider vouched for. With the no-op
deployer (`BENCH_NO_INFRA`) there is no context to pin to, so "the cluster"
would be whatever your kubeconfig last pointed at — and this step writes a
cluster-wide admission policy and `ClusterRoleBindings`. Set
`BENCH_SANDBOX_ALLOW_AMBIENT_CLUSTER=1` if that is genuinely what you want.

### Pod security

Overlapping controls, applied at the same point:

1. **PSA `baseline` labels** on every namespace that exists when the agent
   starts. Namespaces that already declare an `enforce` level are left alone, so
   a task asserting its own level keeps it.
2. **A `ValidatingAdmissionPolicy`** denying `hostNetwork` / `hostPID` /
   `hostIPC`, privileged containers (including init and ephemeral ones), and
   `hostPath` volumes — cluster-wide, in `Deny` mode, failing closed, with the
   cluster's own system namespaces exempted. It matches
   `pods/ephemeralcontainers` as well as `pods`, so `kubectl debug` cannot
   attach a privileged container to an already-admitted pod.
3. **A second policy guarding the exemption list**, denying the agent the
   creation of a namespace named after one of the exempt namespaces, or the
   labelling of any namespace as the cluster's own. The exemptions are by name
   and several do not exist on every provider (`gmp-system` on kind,
   `local-path-storage` on GKE), so without this the agent could simply claim
   an unused one and deploy there. It covers `UPDATE` as well as `CREATE`,
   because a name is immutable but a label is not.
4. **A third policy denying the agent workloads inside the exempt namespaces.**
   Guarding the names only protects the ones that do not exist yet. `edit` is
   bound cluster-wide, so the agent can write to `kube-system` on every
   provider, and policy 2 deliberately skips it — which made
   `kubectl run --privileged -n kube-system` an admitted request on a cluster
   carrying the full set. A boundary probe caught this; it is not hypothetical.
   The policy matches every kind that can produce a pod, not `pods` alone, since
   a `Deployment` reaches the same place with its pod created by the ReplicaSet
   controller. It matches `pods/exec`, `pods/attach` and `pods/portforward` too,
   because `edit` grants exec and these are precisely the namespaces whose pods
   are legitimately privileged. It does not touch config: a ConfigMap in
   `kube-system` is a blast-radius question rather than an escape.
5. **A fourth policy denying the agent a shell into pods that predate all of
   the above.** Admission only sees requests, so nothing here retracts a pod
   that already exists — and the deployer runs *before* credentials are
   provisioned. Some fixtures deploy privileged workloads on purpose:
   `opa-remediation` ships two, because remediating them is the task. Those pods
   stay, and `edit` grants `pods/exec` cluster-wide, so a shell into one is node
   root by a route policy 2 never sees. Before the agent starts, the harness
   scans every namespace for pods policy 2 would have rejected — skipping the
   ones policy 4 already covers — and renders their `namespace/name` into a
   policy denying the agent `exec`, `attach` and `port-forward` into exactly
   those. They are named individually rather than matched on a property because
   admission cannot see the target pod's spec on a `CONNECT`: the object on an
   exec request is a `PodExecOptions`, so a name list is the only thing there is
   to test. The list stays correct for the run — policy 2 denies these pods on
   `CREATE`, so a name that leaves it cannot come back. It is applied even when
   the list is empty, since nothing here is torn down and a reused cluster would
   otherwise keep the previous run's list.

Policies 3, 4 and 5 are scoped to the agent's own username, so the cluster's own
components keep running — the exemption exists for kube-proxy and the CNI, not
for whoever asks. That scoping is sound for these three and would not be for
policy 2: a namespace or a `Deployment` is always created by whoever asked,
whereas a pod is often created on the agent's behalf by a controller running
under an identity of its own.

The exemption has two halves: the names above, and any namespace carrying
`addonmanager.kubernetes.io/mode`. A name list alone goes stale — a plain GKE
run turned up four managed namespaces it had never heard of
(`gke-managed-cim`, `gke-managed-networking-dra-driver`,
`gke-managed-volumepopulator`, `gmp-public`), all created by the same addon
manager as the two that were listed. Nothing broke there, since three were
empty and the fourth runs an unprivileged metrics scraper, but a cluster using
DRA or TPUs runs a privileged `hostPath` DaemonSet in one of them and a
fail-closed policy would have denied it. The label is the cluster declaring
which namespaces are its own to run, so it covers managed namespaces that do
not exist yet. The names are still needed: nothing carries that label on kind
or vcluster, and on GKE `kube-system` itself does not.

The first policy exists because labels cannot cover a namespace the agent
creates *after* provisioning, and at least one task asks it to create one.
`bench-system` is skipped by the labeller — it holds only a ServiceAccount — but
is deliberately **not** exempt from the policy, since the agent can create pods
there. Together these deny the privileged-pod-plus-`hostPath` escape that was
used to read the benchmark's own answer key off a node's disk.

**The cluster must be Kubernetes 1.30 or newer.** Four of the five controls are
`ValidatingAdmissionPolicy` objects, and `admissionregistration.k8s.io/v1` only
reached GA in 1.30 — 1.29 serves `v1beta1`, behind a feature gate. The harness
checks for the `v1` resource before it applies anything and refuses by name if
it is missing, rather than letting the apply fail with kubectl's `no matches for
kind`, which reads like a typo in our own manifest. This is deliberately *not*
routed through `BENCH_SANDBOX_ALLOW_ADMIN_CREDS`: that hatch is for an operator
whose credential cannot write cluster-scoped objects, and no credential makes a
1.29 apiserver serve a v1 policy. For kind, the floor is why `node_image`
defaults to a digest-pinned `v1.30.0`; lowering it breaks every sandboxed run.

A task whose subject matter genuinely is privileged workloads opts out with
`agent_pod_security: privileged` in its `task.yaml` (see
[Add a task](../how-to/add-a-task.md)). The default is `baseline`, and any other
value is a load-time validation error rather than a silent fall-back.

**None of this is torn down.** `bench-system`, the ClusterRoleBindings, the
admission policies and the PSA labels outlive the run. On a disposable cluster that is
irrelevant; on a reused one it means a second run finds most namespaces already
labelled and skips them, which is correct but makes the labeller look inert.
Read the labels as the state of the cluster, not as the output of the run that
is in front of you.

### Known gaps in the RBAC scope

Three are open, all in how the agent's RBAC is scoped rather than in the
pod-security controls above. None is reachable without a working agent
credential, and all are fixed by replacing the built-in role with a derived one
and narrowing the supplement:

- **`edit` bound cluster-wide reaches the system namespaces.** The built-in role
  carries `impersonate` on `serviceaccounts` and `create` on
  `serviceaccounts/token`, so a cluster-wide binding lets the agent mint a token
  for, or impersonate, any ServiceAccount in `kube-system` — including ones bound
  to `cluster-admin`. That is a path to cluster-admin, and it defeats the
  deliberate omission of write on `rbac.authorization.k8s.io`. RBAC has no deny
  rule, so nothing in the supplement can subtract this; only replacing `edit`
  with a derived role closes it. The neighbouring exec route is closed —
  policy 4 above denies `pods/exec` in exactly those namespaces — but that is a
  patch over one exit, not a fix for the scope.
- **`edit` carries read and write on `secrets`.** The built-in role includes
  them, unlike `view`, which excludes them deliberately; bound cluster-wide that
  reaches every namespace. Nothing in the supplement adds this — it is inherited,
  and it is not narrowed anywhere. What the exposure is worth depends on the run:
  the cluster is disposable and its workloads are synthetic, so ordinarily this
  leaks fixture data. It matters for a task that seeds a real credential into a
  Secret, and for the controller and syncer Secrets a task did not author. Under
  vcluster it stops at the virtual cluster; the host cluster's Secrets are not
  reachable with this token.
- **The supplement grants `update`/`patch` on namespaces**, so the agent can
  strip the `pod-security.kubernetes.io/*` labels the harness just applied. The
  admission policies are unaffected — the agent has no write on
  `admissionregistration.k8s.io` — so this removes the PSA half of the
  enforcement, not the load-bearing half. It is a capability the agent holds
  rather than one it has been seen to use: on a GKE run the namespace labels
  were byte-identical before and after.

Until these are closed, treat the pod-security controls as the boundary and the
RBAC scope as best-effort. Narrowing the role is not a blind edit: the tasks were
authored against admin, so a scope that is too tight fails them in ways that read
as agent error. An A/B soak of sandboxed against ambient runs is what shows which
tasks need which verbs.

### The scope is already too tight for custom resources

`edit` covers Kubernetes' own API groups. It does not cover the CRDs an operator
installs, and the supplement grants only `get`/`list`/`watch` on
`customresourcedefinitions` — the definitions, not the objects. What the agent
gets on a given CRD is therefore whatever that operator chose to aggregate into
`edit`, which is usually nothing.

`opa-remediation` is the measured instance. Kyverno v1.12.7 ships
`kyverno:rbac:view:policies` labelled `aggregate-to-view` and
`kyverno:rbac:admin:policies` labelled `aggregate-to-admin`, with no
`aggregate-to-edit` on either. Aggregation flows view into edit and edit into
admin, so the agent can read `ClusterPolicy` objects and cannot write them. Two
of that task's objectives ask it to flip both policies from `Audit` to
`Enforce`, so **the task cannot be fully passed under the default scope** — one
of its three deterministic objective groups is unreachable. Sandboxed and
ambient scores are not comparable for it.

Nothing in a trajectory says so. Neither agent that ran the task attempted the
flip, so the run logs carry no `forbidden` — the objective simply goes
unattempted and reads as an agent miss. Anything that grades sandboxed runs
against ambient ones has to account for this class of gap explicitly rather
than infer it from failures.

### Model credentials

A key-based provider needs nothing special: the key is in the resolved overlay
and crosses the boundary by value like any other variable.

A **keyless** backend does not have that luxury. The sandbox strips
`CLOUDSDK_CONFIG` and `GOOGLE_APPLICATION_CREDENTIALS`, and on the bastion the
link-local metadata endpoint is blocked for containers (see
[infrastructure](infra.md)) — and Application Default Credentials is exactly
that chain. So each keyless backend needs its own *mint-and-inject recipe*: mint
a narrow, short-lived credential host-side, inject it explicitly. The recipes
live in `core/model_providers.py` (`sandbox_credential_env`), keyed off the
provider's backend, never in the sandbox module — cloud-specific code stays out
of the boundary code.

#### Vertex: a metadata-server emulator

Vertex's recipe is a small HTTP server the harness runs on the host, speaking
the subset of the GCE metadata protocol Google's auth libraries use to obtain a
token. The container is pointed at it with `GCE_METADATA_HOST`,
`GCE_METADATA_IP` and `METADATA_SERVER_DETECTION=assume-present`, reaching it at
`host.docker.internal` (the executor `--add-host`s that to the host gateway on
every run). The token behind it is minted by impersonating a service account
that holds `roles/aiplatform.user` and nothing else, refilled in place, and
never minted at all if the run does not call the model.

The route table is fixed and closed: the residency ping, `project/project-id`,
`universe/universe-domain`, and the service-account subtree (the account
listing, plus `email`, `scopes`, `aliases`, `token` and the recursive listing,
under both the `default` alias and the account's own email). Everything else is
a 404 — including `instance/attributes/`, which on a real GKE node carries
`kube-env`; the instance identity paths (`id`, `zone`, `hostname`, `disks`,
`network-interfaces`); any account other than the impersonated one; and the
`identity?audience=` OIDC-JWT issuer. So this is an emulator of an enumerated
set of paths, not a proxy onto the host's real metadata server: a container that
probes it learns nothing about the host it is running on. Requests must carry
`Metadata-Flavor: Google`, as the real server requires.

Two things have to be set up before a sandboxed Vertex run:

| Variable | Meaning |
| --- | --- |
| `BENCH_VERTEX_SANDBOX_SA` | The service account to impersonate. Give it `roles/aiplatform.user` on the project and nothing else. |
| `GOOGLE_CLOUD_PROJECT` (or `GCP_PROJECT`) | The project the run bills to; the emulator serves it to the SDK. |

plus `roles/iam.serviceAccountTokenCreator` for the *host's own* identity on
that service account. Anything missing fails loud — a sandboxed run never
degrades to an unsandboxed one to get a credential.

Bedrock has no recipe yet; a sandboxed `anthropic-bedrock` run is refused with
an error saying so. Ambient, unsandboxed runs of either are unaffected: they are
host processes with the operator's own ADC.

> [!IMPORTANT]
> **`AGENT_API_KEY` is the only variable the host reads the key from**
> (`agents/config.py`). The provider's own names — `GEMINI_API_KEY`,
> `GOOGLE_API_KEY` — are where the key is *written to* inside the container, not
> where it is read from outside. Unsandboxed the distinction never shows: the
> agent subprocess inherits the whole host environment and finds
> `GEMINI_API_KEY` by itself. Sandboxed, only the explicit overlay crosses, so a
> host-side `GEMINI_API_KEY` is simply absent and the CLI exits reporting that no
> auth method is set — naming the very variable you exported. Export
> `AGENT_API_KEY` and the sandboxed run routes it onward for you.

## Adding your own harness

Want to wrap a different agent? See
[Add an agent harness](../how-to/add-an-agent-harness.md).
