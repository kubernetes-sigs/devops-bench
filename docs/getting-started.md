# Getting started

Welcome! This is the doc you start with if you want to contribute to **devops-bench**. It walks you through setting up a dev environment, explains how evals actually run, and points you at the skills that help you work on the repo.

## Intro

devops-bench is a benchmark suite for evaluating how well DevOps agents and models perform real operational tasks against live Kubernetes clusters. As a contributor you'll mostly do three things: set up the tooling, run evals (locally on a kind cluster for the fast path, against real cloud infrastructure for the full thing), and use the repo's skills to review and maintain them. This page covers all three so you can get productive quickly.

## Set up your dev environment

You need **Python ≥ 3.12** (the repo's `.python-version` pins 3.14) and **[uv](https://docs.astral.sh/uv/)** as the dependency manager. `uv.lock` pins the full resolution; `pyproject.toml` carries minimum-version floors. The build backend is hatchling.

| Command | What you get |
| --- | --- |
| `uv sync` | Runtime deps plus the `dev` group (it's included by default). |
| `uv sync --frozen` | Lockfile-pinned, no re-resolution — the reproducible install (exactly the locked versions). |

The `dev` dependency group ships by default and pulls in `pytest`, `pytest-asyncio`, `pytest-mock`, `ruff`, `pre-commit`, and `devops-bench[anthropic,openai]`, so a plain `uv sync` already gives you the test and lint toolchain plus every optional provider SDK.

### Provider extras

The Gemini SDK (`google-genai`) is a core dependency — it's always installed. The other provider SDKs are optional extras, named by package:

| Extra | SDK | Powers |
| --- | --- | --- |
| `anthropic` | `anthropic` | The Claude model adapter. |
| `openai` | `openai` | Installs the `openai` SDK. No OpenAI adapter ships yet; the Ollama adapter talks to its server through this client. |

Extras matter for runtime-only installs (e.g. `uv sync --no-dev --extra anthropic`); the `dev` group already includes both, so a plain `uv sync` covers them.

### Console script

Installing the package exposes the `devops-bench` console script, which maps to `devops_bench.cli:main` (`python -m devops_bench` is equivalent). Run it through uv:

```bash
uv run devops-bench --help
```

### Pre-commit hooks

Install both the commit-time and push-time hooks:

```bash
uv run pre-commit install --hook-type pre-commit --hook-type pre-push
```

| Hook | Stage | What it does |
| --- | --- | --- |
| `ruff-format` | pre-commit | Formats Python. |
| `ruff` | pre-commit | Lints Python. |
| `uv-lock-check` | pre-push | Fails if `uv.lock` is out of date. |
| `license-header-check` | pre-push | Fails if a source file is missing the Apache 2.0 header. |

Ruff is configured for target `py312`, line length 100, with rule sets `E`, `F`, `I`, `UP`, `B`, and `SIM`. Every new source file needs the Apache 2.0 license header (Markdown and YAML are exempt); apply it with `uv run python hack/boilerplate.py`.

### Tests

```bash
uv run pytest
```

The `dev` group includes the provider extras, so the full adapter test suite runs after a plain `uv sync` — no extra install step.

## How evals run

The CLI takes a tasks directory or a single `task.yaml` and runs each task through the pipeline: optionally provision infrastructure, run the agent under test against the task prompt, verify and score the result, then tear the infrastructure down. Two tasks ship on main today:

- `tasks/common/opa-remediation` — policy remediation on a local **kind** cluster; no cloud account involved.
- `tasks/gcp/deploy-hello-app` — a cloud-backed deployment task targeting the GCP provider. It isn't runnable yet — the task doesn't declare a `provider:` key; [known issues](./appendix/known_issues.md) tracks this.

**Infrastructure** is provisioned by OpenTofu stacks under `tf/` (default stack: `prebuilt/kind`). `--no-infra` (or `BENCH_NO_INFRA=true`) skips infrastructure provisioning and runs against the NoOpDeployer — nothing is provisioned or torn down, whether you're running a plumbing check or targeting pre-existing infrastructure; model and judge credentials may still be required. `--infra` forces provisioning back on, and `--no-teardown` / `--teardown` control cleanup the same way. See [infrastructure](./components/infra.md).

**The agent under test** is selected with `--agent-type` (or `BENCH_AGENT_TYPE`); the default is `gemini-cli`, an alias for the `gemini` harness. Four harnesses ship: `gemini`, `openclaw`, `antigravity`, and `api` — see [agents](./components/agents.md). The CLI harnesses drive a binary resolved from `AGENT_TARGET` (falling back to the harness's binary on `PATH`, e.g. `gemini`).

**Agent configuration** flows through `AGENT_*` environment variables (`devops_bench/agents/config.py`):

| Variable | Purpose |
| --- | --- |
| `AGENT_MODEL` | Model id for the agent under test. |
| `AGENT_PROVIDER` | Model provider key (`gemini` / `anthropic` / `ollama` / ...). |
| `AGENT_API_KEY` | API key, routed onto the provider-specific env vars each harness expects. |
| `AGENT_TARGET` | Binary path for CLI harnesses. |
| `AGENT_TIMEOUT_SEC` | Wall-clock cap per external call (default 600). |
| `AGENT_MAX_TURNS` | Tool-use loop cap for the `api` harness. |
| `AGENT_MCP_SERVER` | Shell-quoted MCP server command granted to the agent. |
| `AGENT_ALLOWED_TOOLS` | CSV of pre-approved tool names. |
| `AGENT_SKILLS_PATHS` | CSV of skill directories granted to the agent. |
| `AGENT_RULES_TEXT` | Rules text injected into the agent's context. |

**The judge** that scores results is configured with `--judge-provider` / `--judge-model` (or `JUDGE_PROVIDER` / `JUDGE_MODEL`). Leaving both unset is fine: the harness builds a default judge from the models layer, which follows `AGENT_PROVIDER` and defaults to the Gemini adapter — authenticated via `AGENT_API_KEY`, or keylessly through the Vertex AI backend when `GCP_PROJECT_ID` and ambient cloud credentials are available. For a fully local judge, point it at Ollama (`JUDGE_PROVIDER=ollama`, endpoint via `OLLAMA_BASE_URL`).

The run-level knobs also have env forms (`devops_bench/run.py`): `PROJECT_ID`, `CLUSTER_NAME`, `EVAL_LIMIT`, `RESULTS_ROOT`, `BENCH_NO_INFRA`, `BENCH_NO_TEARDOWN`, `BENCH_PARALLEL`, and `RUN_ID`. `--parallel` isolates a run (its own kubeconfig, cloud CLI config, and tofu data dir, plus a run-unique cluster name) so several runs can share one host.

### Your first run — no cloud required

The `opa-remediation` task provisions its own local kind cluster, so a real end-to-end eval needs no cloud account. You need on `PATH`: `tofu`, Docker, the `kind` binary, `kubectl`, and the default agent's `gemini` CLI. On Linux, raise the `fs.inotify` limits for kind:

```bash
echo -e "fs.inotify.max_user_watches=524288\nfs.inotify.max_user_instances=512" | sudo tee /etc/sysctl.d/99-kind.conf
sudo sysctl --system
```

Then:

```bash
export AGENT_API_KEY=...   # used by the agent's model provider and the default judge
uv run devops-bench tasks/common/opa-remediation \
  --project local-kind --cluster devops-bench-kind
```

A project id and cluster name are required whenever infra is on — the kind provider ignores the project, so any placeholder works. Exit code 0 means no task failed, 1 means at least one did, 2 is a configuration error, and the results path is printed at the end.

For a plumbing check without provisioning anything:

```bash
uv run devops-bench tasks --no-infra --limit 1
```

This exercises the pipeline against the NoOpDeployer. No cluster is provisioned, so cluster-backed verifications fail — use it to validate your setup, not to produce real scores.

**Real cloud evals** additionally need the target provider's CLI tooling and credentials co-located with the harness (e.g. `gcloud` with application-default credentials for the GCP provider). When you're ready, follow [how-to/run-evals.md](./how-to/run-evals.md).

## Skills in this repo

The repo ships **skills for coding agents** in `.agents/skills/` — you invoke them to review and maintain the benchmark. They are agent-agnostic (the capability-to-tool mapping per harness lives in `.agents/references/harness-capabilities.md`).

| Skill | Purpose | When to use |
| --- | --- | --- |
| `devops-bench-review` | Review a code change across correctness, testability, maintainability, API hygiene, domain modeling, conventions, and security. | Reviewing a code diff or PR. |
| `task-review` | Review a benchmark task — schema, rubric quality, parallel-safety, infra config, leaks. | Vetting a new or changed task. |
| `cleanup-orphaned-resources` | Find and remove cloud or local resources leaked by aborted runs. | Cleaning up after failures. |
| `run-eval` | Run one Task × Model × AgentConfig eval end to end, local or on the bastion. | Kicking off a single eval run. |
| `run-parallel-evals` | Run a Task × Model × AgentConfig matrix in parallel, with monitoring and retries. | Comparing models or configs, or running many evals at once. |
| `validate-eval` | Run a newly authored eval in a fix-and-retry loop until it's green. | Vetting a new task before setting `validated: true`. |
| `diagnose-eval-failure` | Explain why a model scored low — the judge's reasons and the agent's trajectory, lined up against the rubric. | Understanding a low score on a completed run. |
| `docs-sync` | Map a code change to the docs that describe it and update them in place. | After changing code the docs describe. |

## Where to go next

- [Run evals](./how-to/run-evals.md) — actually kick off a run.
- [Add a task](./how-to/add-a-task.md) — contribute a new benchmark task.
- [Architecture](./components/architecture.md) — how the pipeline fits together.
- [Known issues](./appendix/known_issues.md) — current rough edges.
- [Contributing](../CONTRIBUTING.md) — CLA and the PR process.
- [Project README](../README.md) — the high-level overview and community channels.
