# devops-bench

A standardized benchmarking suite to evaluate how well different agents or models perform specific DevOps tasks. Its goal is to provide an open-source, reproducible way to transparently assess agent performance across various infrastructure platforms and operational environments.

Most benchmarks stop at "did the model produce reasonable text?" This one runs the agent against live infrastructure and checks the result. It also lets you quantify the payoff of giving agents more to work with — context, operational rules, and tools like MCP servers and skills — so you can see what those additions are actually worth.

See the [project roadmap](roadmap.md) for current initiatives and how to get involved.

## How it works

For each task, the harness provisions real infrastructure if the task needs it, runs your agent against it, optionally injects chaos and verifies the resulting cluster state, then scores the run with LLM-as-judge metrics — and tears everything down when it's done.

A single run, end to end:

1. **Provision** — OpenTofu stands up a cloud cluster or a local kind cluster (or nothing, for no-infra tasks).
2. **Run the agent** — your chosen agent harness drives the task.
3. **Chaos + verify** — optionally break things, then check the live cluster state.
4. **Score** — LLM-as-judge metrics grade the outcome and the agent's tool use.
5. **Teardown** — everything provisioned is cleaned up.

## What's supported

**Agent harnesses** — choose with `BENCH_AGENT_TYPE` or `--agent-type` (default `gemini-cli`, an alias for `gemini`):

| Key | What it runs |
| :-- | :-- |
| `gemini` | The Google Gemini CLI. |
| `openclaw` | The Openclaw Agent CLI. |
| `antigravity` | The Antigravity CLI. |
| `api` | In-process: drives a provider SDK directly through a model-agnostic MCP tool loop. |

**Model providers** — choose with `AGENT_PROVIDER` and `AGENT_MODEL`:

| Key | Backends |
| :-- | :-- |
| `gemini` | Google AI Studio API key, or Vertex AI. |
| `claude` | Anthropic API, Vertex AI, or Bedrock. |
| `ollama` | Local models. |

**Infrastructure targets** — the OpenTofu deployer supports these targets (set `INFRA_PROVIDER`, or the task's `provider:` key):

| Key | Target |
| :-- | :-- |
| `gcp` | GKE. |
| `kind` | Local kind clusters. |

`--no-infra` skips provisioning entirely and runs against a pre-existing cluster or none at all.

## Install

You need Python 3.12 or newer. The project uses [`uv`](https://docs.astral.sh/uv/):

```bash
uv sync
```

The default `dev` group includes the test/lint toolchain and every optional provider SDK (`anthropic`, `openai`).

## Run your first eval

The `opa-remediation` task provisions its own local kind cluster, so a real end-to-end eval needs no cloud account. With `tofu`, Docker, `kind`, `kubectl`, and the `gemini` CLI on `PATH`:

```bash
export AGENT_API_KEY=...   # used by the agent's model provider and the default judge
uv run devops-bench tasks/common/opa-remediation \
  --project local-kind --cluster devops-bench-kind
```

The results path is printed at the end of the run. The full walkthrough — prerequisites, judge configuration, exit codes — is in [Getting started](docs/getting-started.md); for cloud runs and parallel matrices, see the [run-evals how-to](docs/how-to/run-evals.md).

**Working through a coding agent?** Point it at the repo's skills instead of assembling commands yourself — see [the skills overview](docs/getting-started.md#skills-in-this-repo).

## Adding a benchmark task

New tasks live under `tasks/<provider>/<name>/task.yaml`, each pairing a `chaos_spec` (what breaks) with a `verification_spec`/`expected_output` (how it's graded). The full schema, placeholders, worked examples, and authoring best practices are in [docs/how-to/add-a-task.md](docs/how-to/add-a-task.md) — read that before you start. Before submitting, run the `task-review` skill over your task — see [the skills overview](docs/getting-started.md#skills-in-this-repo).

## Documentation

We welcome contributions around adding new tasks, models, or agent harnesses. Start with [Getting started](docs/getting-started.md), then browse the how-to guides, component docs, known issues, and agent skills from the [documentation index](docs/README.md).

## Community, discussion, contribution, and support

Learn how to engage with the Kubernetes community on the [community page](http://kubernetes.io/community/).

You can reach the maintainers of this project at:

- [Slack channel](https://kubernetes.slack.com/messages/sig-apps)
- [Mailing List](https://groups.google.com/a/kubernetes.io/g/sig-apps)

### Code of conduct

Participation in the Kubernetes community is governed by the [Kubernetes Code of Conduct](code-of-conduct.md).

## License

Apache 2.0 — see [`LICENSE`](LICENSE).
