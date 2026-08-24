# How to add a task

A benchmark task is a typed contract on disk. You author one directory under `tasks/<provider>/<name>/` containing a single `task.yaml`, and the harness does the rest: it validates the file against the `Task` schema, substitutes infrastructure placeholders, optionally provisions a cluster, runs the agent, and grades the result.

This guide walks you through the schema, the placeholders the harness fills in, the step-by-step authoring flow, two complete worked examples, and the considerations that keep your task safe to run alongside dozens of others. The schema is enforced by `Task` in `devops_bench/tasks/schema.py`, and tasks are discovered and loaded by `FileSystemTaskLoader`. Those are the source of truth; this guide describes them and can fall behind, so where the two disagree the schema is authoritative.

For how deployers and stacks fit together, see [infrastructure](../components/infra.md).

## The task schema

Every field below maps to an attribute on `Task`. Fields marked `Required` must produce a usable value — the harness validates types strictly (no string-to-bool coercion) and ignores unknown keys, so a typo'd field name is silently dropped rather than flagged. Author carefully.

| Field | Required | Meaning |
| --- | --- | --- |
| `task_id` (alias `id`) | Yes | Unique identifier for the task. `task_id` is accepted as an alias for `id` and is coerced to a string. |
| `name` | No (defaults to the directory name) | Human-readable task name. Set it explicitly rather than relying on the default. |
| `prompt` (aliases `goal`, `input`) | Yes | The instruction handed to the agent. Use `{{...}}` placeholders for any infra value — never hardcode a project, cluster, namespace, or deployment name. |
| `expected_output` | Yes | The grading rubric, written as prose "critical requirements". Graded on **outcome**, so accept any valid path to the goal, not one prescribed method. |
| `infrastructure` | No (defaults to a local kind cluster) | `{deployer, stack, teardown, variables, provider}`. Omitting it entirely gives `deployer: tofu` with the `prebuilt/kind` stack, whose provider is deduced as `kind`. Use `deployer: noop` for generation-only tasks (no cluster), or `deployer: tofu` with a `stack` under `tf/prebuilt/<dir>` for real infrastructure — any stack whose final path segment is not `kind` **must** set `provider` explicitly, since no cloud is ever assumed. |
| `validated` | No (defaults `false`) | Set `true` only after a human has vetted the task. Required for leaderboard eligibility — an unvetted task never counts. |
| `verification_spec` | No | A list of `{name, spec: <typed node>}` entries. Deterministic cluster assertions; `name` is the cross-reference key a `chaos_spec` resolves against. |
| `chaos_spec` | No | A list of `{name, trigger, action, verify}` entries, where `verify` matches a `verification_spec` entry's `name`. |
| `documentation` | No | `[{doc_name, url, constraints: [{text, critical}]}]` — reference docs and the requirements drawn from them, used for grounding scoring. |
| `retrieval_context` | No | A list of supporting passages for retrieval-based (RAG) scoring. |

> [!NOTE]
> An empty YAML block (`key:` with no value) is treated as the field's empty default rather than an error, so you can stub out an optional field without breaking validation.

## Placeholders

The harness substitutes a fixed set of `{{...}}` placeholders into your `prompt` and `expected_output` (and into chaos/verification spec string leaves) just before the agent runs, using the live cluster and project for that run. The table below is the complete set — a `{{...}}` name that is not listed is left in the prompt verbatim rather than reported as an error, so check it against `_substitute_placeholders` in `devops_bench/evalharness/default.py` if in doubt.

| Placeholder | Resolves to |
| --- | --- |
| `{{PROJECT_ID}}` | The cloud project id for this run. |
| `{{CLUSTER_NAME}}` | The target Kubernetes cluster name for this run. |
| `{{APP_LOCATION}}` | The configured application location. |
| `{{TARGET_DEPLOYMENT_NAME}}` | The deployment the agent operates on (and the chaos port-forward target). |
| `{{NAMESPACE}}` | The namespace the target workload lives in. |

Write everything infra-specific as a placeholder. That is what lets the *same* task run against a different project and a freshly named cluster on every invocation, and in parallel with other runs, without edits. A hardcoded cluster name or project ID will break the moment the task runs anywhere but your machine.

## Step by step

1. **Create the file.** Make `tasks/<provider>/<name>/task.yaml`. Set `task_id` (unique) and `name`.
2. **Choose a deployer.** Use `noop` for a manifest-generation task (the agent's YAML is judged, never applied — no cluster is brought up). Use `tofu` with a `stack` under `tf/prebuilt/<dir>` when the agent needs a live cluster; create a new stack directory or reuse an existing one.
3. **Write the `prompt`** using placeholders only for any infra value.
4. **Write `expected_output`** as outcome-based "critical requirements" — describe *what* a correct result must achieve, not the exact commands to get there.
5. **(Optional) Add a `verification_spec` and `chaos_spec`.** Express deterministic cluster assertions as compound `sequence` / `parallel` nodes wrapping leaf verifiers like `pod_healthy` and `scaling_complete`. If you inject chaos, set the chaos entry's `verify:` to match a `verification_spec` entry's `name`.
6. **(Optional) Add `documentation`** entries to ground scoring against authoritative docs.
7. **Smoke-test with no infra.** This forces the `noop` deployer and skips provisioning, so it's fast and free. The `--no-infra` CLI flag does the same thing as the environment variable, and `--infra` forces provisioning back on:
   ```bash
   BENCH_NO_INFRA=true python -m devops_bench "tasks/PROVIDER/NAME/task.yaml"
   ```
8. **Run for real, review the scores, then promote.** Once you've run the task against real infra and are satisfied with the grading, set `validated: true`.

## A complete example

Here is a realistic, annotated `noop` generation task. The agent is asked to produce manifests; nothing is provisioned, and the YAML it generates is what gets graded. Note that every infra value is a placeholder, and `expected_output` lists outcome requirements rather than prescribing exact field values.

```yaml
task_id: 2
name: "create-deployment"
# deployer: noop skips provisioning — the agent's generated YAML is judged,
# never applied. generation_only is derived automatically from deployer == noop.
infrastructure:
  deployer: "noop"
prompt: "Generate a Kubernetes manifest for deploying our finetuned model to cluster {{CLUSTER_NAME}}. The models are stored in the object storage bucket models-{{PROJECT_ID}}. Ensure it can scale up and down with traffic."
expected_output: |
  critical requirements:

  - Deploy a vLLM server for the model.
  - Configure the deployment to use 1 NVIDIA L4 GPU and the 'nvidia.com/gpu' toleration.
  - Mount the model bucket 'models-{{PROJECT_ID}}' using the gcsfuse CSI driver.
  - Create a 'ClusterIP' Service exposing the server on a feasible port.
  - Create an HPA with feasible scaling parameters (max replicas, CPU utilization).
  - Use a dedicated service account for the deployment.
# Optional: ground scoring against authoritative docs.
documentation:
  - doc_name: "GPU Setup Docs"
    url: "https://cloud.google.com/kubernetes-engine/docs/how-to/gpus"
    constraints:
      - text: "nvidia.com/gpu resource limit of 1"
        critical: true
      - text: "nvidia.com/gpu toleration"
        critical: true
```

For a task that needs a live cluster, swap the `infrastructure` block for a `tofu` deployer with a stack, and pair a `chaos_spec` with a `verification_spec`. The snippet below provisions a workload, fires a planned load spike five seconds in, and then verifies — in parallel — that pods stay healthy and the deployment scales out. The chaos entry's `verify:` value is the cross-reference key; it **must** match the `name` of a `verification_spec` entry.

```yaml
infrastructure:
  deployer: "tofu"
  provider: "gcp"          # required: the stack is not named `kind`
  stack: "prebuilt/minimum"
  teardown: true
  variables:
    namespace: "scale-test"
    target_deployment_name: "scale-target"
chaos_spec:
  - name: "Planned Load Spike"
    trigger:
      type: time
      delay_seconds: 5
    action:
      type: generate_load
      target:
        service_url: "http://{{TARGET_DEPLOYMENT_NAME}}.{{NAMESPACE}}.svc.cluster.local"
        qps: 300
    # Resolved against verification_spec[*].name below.
    verify: "Planned Load Spike Verification"
verification_spec:
  - name: "Planned Load Spike Verification"
    role: objective
    weight: 1.0
    check:
      type: parallel
      checks:
        - type: pod_healthy
          name: pod_spec
          selector: "app={{TARGET_DEPLOYMENT_NAME}}"
          namespace: "{{NAMESPACE}}"
        - type: scaling_complete
          name: scaling_spec
          deployment: "{{TARGET_DEPLOYMENT_NAME}}"
          min_replicas: 2
          namespace: "{{NAMESPACE}}"
  # A safeguard asserts the agent did not do something harmful. Severity picks
  # how it scores: `recoverable` drags the outcome down, `catastrophic` zeroes it.
  # Note the namespace: the workload lives in `scale-test`, so finding it in
  # `default` means the agent deployed somewhere it should not have. A safeguard
  # pointed at the task's own namespace would fire on a correct run.
  - name: "nothing-in-default"
    role: safeguard
    severity: catastrophic
    check:
      type: resource_property
      kind: deployment
      selector: "app={{TARGET_DEPLOYMENT_NAME}}"
      namespace: default
      op: absent
```

Each entry carries the scoring vocabulary, not just a check tree:

| Key | Required | Meaning |
| --- | --- | --- |
| `name` | Yes | Cross-reference key; a `chaos_spec` entry's `verify:` matches this. |
| `role` | Yes | `objective` feeds correctness; `safeguard` asserts the agent avoided harm. |
| `severity` | Safeguards only | `recoverable` or `catastrophic`. A failed catastrophic safeguard zeroes the outcome. |
| `weight` | No (default `1.0`) | Relative contribution within its role. Must be greater than zero. |
| `mode` | No (derived from `role`) | `converge` polls until true, `assert` evaluates once. Objectives converge, safeguards assert. |
| `check` | Yes | The verifier tree: a leaf, or `sequence` / `parallel` / `all` / `any` / `none` wrapping others. |

> [!IMPORTANT]
> The entry schema is strict (`extra="forbid"`), so a stray or misspelled key is
> rejected outright rather than ignored. In particular the check tree lives under
> `check:`, not `spec:`.

> [!NOTE]
> A typo'd `verify:` (no matching `verification_spec` name) does not crash the run. The chaos entry records a failed verification on `chaos_report["verification"]` with `success: false`, plus `unresolved_reference` and `known_references` so you can see what it looked for and what existed. Note this is **not** a `verification_parse_errors` entry — that field is for specs that fail to parse, so grepping for it will not surface a bad cross-reference.

## Key considerations

> [!IMPORTANT]
> **Parallel safety is non-negotiable.** Tasks run concurrently across a matrix of models and configs. Any project-global resource your task creates must be run-scoped — derive its name from `var.cluster_name` or a random suffix — and it must be destroyed at teardown. A fixed, shared name means two concurrent runs collide and both fail. Keep `variables.namespace` and `variables.target_deployment_name` in your stack consistent with the `{{NAMESPACE}}` and `{{TARGET_DEPLOYMENT_NAME}}` placeholders in your prompt, or the agent and the chaos injector will address different workloads.

A few more habits that keep tasks healthy:

- **Grade on outcome, not method.** Write `expected_output` so any correct path scores well. If your rubric only credits one specific command sequence, you're testing recall, not capability.
- **Leave `validated: false` until you've actually run it.** The flag gates leaderboard inclusion; promoting an unvetted task pollutes the results.
- **Prefer `noop` when a cluster adds nothing.** Generation-only tasks are faster, cheaper, and inherently collision-free.
- **Prefer Terraform-native resources over ad-hoc shell scripts, unless absolutely necessary.** Model your stack's setup as managed OpenTofu resources rather than a `local-exec` shell-out wherever the provider can express it. A resource a script creates falls outside OpenTofu's state, so `tofu destroy` can't remove it — you end up hand-rolling a destroy-time sweep instead, and a forgotten one leaks. This alone removes most of the cleanup burden described above. Reach for a script only when the OpenTofu provider genuinely can't express what you need, and keep it idempotent and scoped to resources the stack itself tears down.
- **Model one realistic failure per `chaos_spec` entry.** Unless you're deliberately building an advanced multi-stage cascading scenario, each entry should model exactly one realistic failure or stress mechanism (a traffic spike, a pod kill, injected latency), with parameters (`qps`, `duration`, disruption targets) that reflect production conditions without overwhelming the host running the eval.
- **Use lightweight, fast-pulling manifests.** Use small base images (`alpine`, `busybox`, `nginx:alpine`) in your task manifests, and avoid depending on the open internet or third-party APIs during validation — stub or seed what you need inside the cluster instead.
- **Keep tasks organized and discoverable.** File each task under the directory that matches its infrastructure provider (`gcp`, `kind`), under `noop` for generation-only tasks, or under `common` for tasks that need no cloud account (like the kind-based `opa-remediation`). Give it a globally unique `task_id`, and use a descriptive `name` — there's no formal difficulty/category field today, so naming and placement are how reviewers and other contributors scope a task at a glance. The `tests/` directory is reserved for the Python codebase's own unit tests — it is not where benchmark task definitions go.

## Reviewing and validating your task

Before you submit, run the `task-review` skill over your task, once it lands — it is not in this repo yet, so until then work the checklist above by hand. It checks the schema and rubric
quality and, most importantly, hunts the parallel-safety problems above — shared state that would
make your task fail when the full matrix runs at once. It's review-only: static analysis plus maybe
unit tests and linters, never provisioning infra or running an eval. (For changes to the harness
*code* rather than a task, use `devops-bench-review` instead, which is migrating alongside it.)

To actually prove the task runs and grades correctly, use the `validate-eval` skill, which runs it in
a self-healing loop and recommends setting `validated: true` once it's green.
