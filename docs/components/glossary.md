# Glossary and Directory Structure

A plain-language reference to the vocabulary used throughout devops-bench, plus an annotated map of the repository so you can find where each idea lives in code.

## Part A — Glossary

Every term below names a real concept in the codebase. The "Where it lives" column points you at the package or module that owns it.

### Core components

| Term | What it is | Where it lives |
| --- | --- | --- |
| **Harness / EvalHarness** | The top-level engine that threads every component together and runs one task end to end (provision, run the agent, inject chaos, verify, score, tear down). The concrete implementation is `DefaultEvalHarness`. | `devops_bench/evalharness/` (`DefaultEvalHarness` in `default.py`) |
| **AgentHarness** | The wrapper around the *agent under test* — the thing being benchmarked. A base class plus an `AGENTS` registry of concrete agents (the Gemini CLI, Openclaw, and in-process API agents). | `devops_bench/agents/` |
| **Model / LLM provider** | The model-agnostic LLM client layer (`gemini`, `claude`, `ollama`), looked up through the `MODELS` registry and constructed by `get_model()`. A *model provider* serves the LLM; do not confuse it with a *cloud provider* (below). | `devops_bench/models/` |
| **Cloud provider** | Credentials plus Terraform variable resolution for a single cloud (`gcp`, `kind`), looked up through the `PROVIDERS` registry. | `devops_bench/providers/` |
| **Deployer** | Provisions and tears down the cluster the agent works against. `TFDeployer` drives OpenTofu; `NoOpDeployer` does nothing (for tasks that run against a pre-existing cluster). | `devops_bench/deployers/` |
| **Task** | The typed eval contract, authored as a `task.yaml`. The schema is code; the tasks themselves live on disk. | Schema in `devops_bench/tasks/schema.py`; tasks under `tasks/` |
| **Metric** | LLM-as-judge plus deterministic scoring, looked up through the `METRICS` registry. | `devops_bench/metrics/` |
| **Outcome validity** | The primary rubric: did the agent actually achieve the goal by *any* valid path? It grades the result, not whether a specific method was followed. | `devops_bench/metrics/outcome_validity.py` |
| **`expected_output` (rubric)** | The per-task grading reference — prose "critical requirements" — graded on the terminal outcome. | Task field; schema in `devops_bench/tasks/schema.py` |
| **Registry** | A generic name-to-object lookup with entry-point plugin discovery. It backs every extension axis: `AGENTS`, `MODELS`, `PROVIDERS`, `FAULTS`, `TRIGGERS`, `VERIFIERS`, `METRICS`. | `devops_bench/core/registry.py` |
| **Bastion** | An alternate execution environment — a VM that runs the harness in-VPC, so runs reach cluster-internal endpoints directly. Its tooling has not migrated yet. | `tf/modules/bastion` |

### Chaos

The planned-disruption subsystem (`devops_bench/chaos/`) that stresses the cluster while the agent works.

| Term | What it is | Where it lives |
| --- | --- | --- |
| **ChaosSpec** | The disruption definition on a task: pairs a trigger, an action/fault, and a `verify` reference. | `devops_bench/chaos/spec.py` |
| **Trigger** | Decides *when* a fault fires (e.g. a time delay). | `devops_bench/chaos/base.py`; concrete triggers in `chaos/triggers/` |
| **Fault** | The disruptive action itself (e.g. generate load against a service). | `devops_bench/chaos/base.py`; concrete faults in `chaos/faults/` |
| **ChaosAgent** | An LLM tool-loop that drives the fault's commands. | `devops_bench/chaos/agent.py` |

### Verification

Type-safe assertions about cluster state (`devops_bench/verification/`).

| Term | What it is | Where it lives |
| --- | --- | --- |
| **VerificationSpec** | The assertion tree on a task, referenced by a chaos spec's `verify`. | `devops_bench/verification/spec.py` |
| **Compound nodes** | Nodes that combine other verifiers: `sequence` and `parallel` for ordering, `all` / `any` / `none` for boolean combination. | `devops_bench/verification/spec.py`; executed by `verification/runner.py` |
| **Verifier** | A leaf check against live cluster state (`pod_healthy`, `scaling_complete`, `resource_property`), evaluated against one shared deadline. | `devops_bench/verification/verifiers/` |

### Run state

Per-run plumbing in `devops_bench/core/`.

| Term | What it is |
| --- | --- |
| **RunContext** | Per-task state threaded through a run. |
| **ClusterInfo** | Cluster connection details (name, location, kubeconfig). |
| **RunEnv** | Per-run isolation (kubeconfig, cloud CLI configuration, tofu data dir, unique cluster name) that lets concurrent runs share a host without colliding. |

## Part B — Directory structure

An annotated map of the repository. One line per entry; only the directories you will actually touch.

```text
devops-bench/
├── devops_bench/            # The benchmark engine (the canonical code path)
│   ├── core/                # Registry, RunContext, RunEnv, config, logging, errors
│   ├── evalharness/         # DefaultEvalHarness — the orchestration engine
│   ├── agents/              # AgentHarness base + AGENTS registry (gemini, openclaw, api)
│   ├── models/              # Model-agnostic LLM client layer + get_model()
│   ├── providers/           # Cloud providers + PROVIDERS registry (gcp, kind)
│   ├── deployers/           # TFDeployer (OpenTofu) and NoOpDeployer
│   ├── chaos/               # ChaosSpec, faults, triggers, ChaosAgent
│   ├── verification/        # VerificationSpec + leaf verifiers
│   ├── metrics/             # LLM-as-judge + deterministic scoring (METRICS registry)
│   ├── cheat_detection/     # Trajectory scan for access to the benchmark's own material
│   ├── tasks/               # Task schema + filesystem loader
│   ├── k8s/                 # kubectl wrappers (get, wait, poll, port-forward)
│   ├── results/             # Result rows, manifest, aggregation, normalization
│   ├── skills/              # Judge rubric markdown (used by the metrics layer)
│   ├── cli.py               # Argument parsing for the CLI entrypoint
│   ├── run.py               # run_benchmark — the library entrypoint
│   └── __main__.py          # `python -m devops_bench` dispatch
├── tasks/                   # Task definitions on disk (task.yaml files)
├── tf/                      # Terraform / OpenTofu modules and prebuilt stacks
├── .agents/                 # Skills and references for coding agents
│   ├── skills/              # One directory per skill (task-review, devops-bench-review, ...)
│   └── references/          # Shared reference material the skills link to
├── tests/                   # Test suite
├── docs/                    # Documentation (you are here)
├── hack/                    # Developer tooling and one-off utilities
└── results/                 # Generated run artifacts (results.json, rows.json, manifest.json)
```

> [!NOTE]
> The top-level `deployers/`, `skills/`, and `scripts/` directories currently hold only a placeholder `README.md` each. They are scaffolding for content that has not migrated yet — the live deployer code is `devops_bench/deployers/`, and the judge rubric markdown the metrics layer reads is `devops_bench/skills/`.
