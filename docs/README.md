# devops-bench documentation

Everything you need to understand, run, and extend devops-bench. New here? Start with
[Getting started](./getting-started.md), then skim the [architecture](./components/architecture.md)
and [glossary](./components/glossary.md).

## Start here

- [Getting started](./getting-started.md) — set up your dev environment, understand how evals run, and meet the skills in this repo.

## How-to guides — get things done

- [Run evals](./how-to/run-evals.md) — single runs and parallel matrices.
- [Add a task](./how-to/add-a-task.md) — author a new benchmark task.
- [Add a model provider](./how-to/add-a-model-provider.md) — wire up a new LLM backend.
- [Add an agent harness](./how-to/add-an-agent-harness.md) — plug in a new agent under test.

## Components — how the pieces work

- [Architecture](./components/architecture.md) — the eval lifecycle and how the components fit together.
- [Glossary](./components/glossary.md) — the vocabulary, plus an annotated map of the codebase.
- [Model providers](./components/model_providers.md) — the LLMs that power agents and judges, and how to configure them.
- [Infrastructure](./components/infra.md) — deployers and cloud providers, and how to configure infra for a task.
- [Agents](./components/agents.md) — the agent harnesses under test and their capabilities.
- [Metrics](./components/metrics.md) — the scoring framework and how to read results.
- [Cheating detection](./components/cheat-detection.md) — how runs are scanned for access to the benchmark's own material.

## Reference

- [Known issues](./appendix/known_issues.md) — a recovery router for eval failures, plus the current known hacks.
- [Roadmap](../roadmap.md) — current initiatives and how to get involved.
- [Agent skills](../.agents/) — operational skills and shared references for coding agents working on this repo.
