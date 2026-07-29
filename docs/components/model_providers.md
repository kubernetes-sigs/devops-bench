# Model providers

devops-bench can drive an agent, a judge, and a chaos agent with different LLMs,
and it does so through one small interface. Every provider implements the same
`LLMClient` contract (`devops_bench/models/base.py`), and a single factory,
`get_model()`, constructs the right adapter at runtime. Add a provider by
dropping a file in `devops_bench/models/`; nothing else in the tree needs to
change, unless it also needs an alias, a distinct backend, a non-default
API-key variable, or keyless auth — those belong to the provider contract in
`devops_bench/core/model_providers.py`.

> [!IMPORTANT]
> This page is about the **LLM** that powers the agent under test and the judge
> — not the **cloud** your benchmark infrastructure runs on. Those are separate
> concerns. A run can use Claude on Anthropic's API while provisioning Google
> Cloud resources, for example. For the cloud/infra side, see
> [infra.md](./infra.md).

## Supported providers and models

Every harness — the `api` runner and the `gemini`/`openclaw` CLIs — resolves
`AGENT_PROVIDER` through one shared contract
(`devops_bench/core/model_providers.py`). A provider resolves to an *adapter
family* (which `LLMClient` the `api` harness builds), a *backend* hint
(genai/vertex/bedrock), the *openclaw wire-provider*, and the *API-key env
var(s)* a CLI harness sets — so the same `AGENT_*` config behaves identically
across harnesses.

| Provider key | Aliases | Adapter family | Backend | Key env var(s) | Keyless |
| --- | --- | --- | --- | --- | --- |
| `google` | `gemini` | `gemini` | genai (API key) | `GEMINI_API_KEY`, `GOOGLE_API_KEY` | no |
| `google-vertex` | `google_vertex` | `gemini` | Vertex AI | `GOOGLE_CLOUD_API_KEY` | yes (ADC) |
| `anthropic` | `claude` | `claude` | inferred (api/vertex/bedrock) | `ANTHROPIC_API_KEY` | no |
| `anthropic-vertex` | `anthropic_vertex` | `claude` | Vertex AI | — | yes (ADC) |
| `anthropic-bedrock` | `anthropic_bedrock` | `claude` | Amazon Bedrock | — | yes (AWS creds) |
| `openai` | — | `openai` | — | `OPENAI_API_KEY` | no |
| `ollama` | — | `ollama` | local OpenAI-compatible server | optional `AGENT_API_KEY` | yes |

Default models: `gemini` → `gemini-3.1-pro-preview`; `claude` → backend-specific
(`api` → `claude-sonnet-4-5`; Bedrock requires `AGENT_MODEL`); `ollama` →
`gemma4:2b`.

A few things worth calling out:

- **Vertex and Bedrock are keyless.** `google-vertex`, `anthropic-vertex`, and
  `anthropic-bedrock` authenticate via ADC / AWS credentials; the contract never
  forces an API key onto them (their key-env list is empty). The bare
  `anthropic` provider still infers its backend from the environment.
- **Ollama accepts an optional key.** It defaults to a dummy the local server
  ignores, but uses `AGENT_API_KEY` when set (for remote/hosted endpoints).
- **Install extras are named by PyPI package, not by provider key.** The extras
  are `anthropic` and `openai` — a different axis from the provider keys above.
  The most surprising consequence: the `openai` extra is what backs the
  `ollama` provider, because the Ollama adapter talks to its server through the
  `openai` client. `google-genai` needs no extra; it is a base dependency, so
  `gemini` works out of the box. Install the SDK you need:

  ```bash
  pip install "devops-bench[anthropic]"   # claude
  pip install "devops-bench[openai]"      # ollama
  ```

## How a model is selected

There are three roles, and each reads its own environment variables.

| Role | Provider var | Model var | Notes |
| --- | --- | --- | --- |
| Agent under test | `AGENT_PROVIDER` | `AGENT_MODEL` | Also `AGENT_API_KEY`, `AGENT_MAX_TOKENS`. |
| Judge | `JUDGE_PROVIDER` | `JUDGE_MODEL` | Also settable via the `--judge-provider` / `--judge-model` CLI flags. |
| Chaos agent | `CHAOS_PROVIDER` | `CHAOS_MODEL` | Falls back to `AGENT_PROVIDER` / `AGENT_MODEL` when unset. |

When no provider is given anywhere, the contract defaults to `google`.

### Backend selection

The cleanest way to pick a backend is the **provider key** itself —
`google-vertex`, `anthropic-vertex`, and `anthropic-bedrock` select Vertex AI /
Bedrock deterministically, independent of which keys happen to be in the
environment. These also flow consistently into the CLI harnesses.

These variables still influence backend/transport details:

| Variable | Effect |
| --- | --- |
| `GCP_PROJECT_ID` + `GCP_VERTEX_LOCATION` | Vertex project/region (`GCP_VERTEX_LOCATION` defaults to `global`). |
| `ANTHROPIC_BACKEND` | Forces the Claude backend (`api`/`vertex`/`bedrock`) for the bare `anthropic` provider. |
| `AWS_REGION` / `AWS_DEFAULT_REGION` | Region for the Claude Bedrock backend. |
| `OLLAMA_BASE_URL` | Endpoint for the Ollama server (defaults to `http://localhost:11434/v1`). |

For the bare `anthropic` provider (no explicit `-vertex`/`-bedrock` key and no
`ANTHROPIC_BACKEND`), the adapter still infers a backend: an API key
(`AGENT_API_KEY` / `ANTHROPIC_API_KEY`) selects `api`, then `GCP_PROJECT_ID`
selects `vertex`, then an AWS region selects `bedrock`, with `vertex` as the
final fallback.

> [!NOTE]
> **All harnesses share one provider contract.** The `api` runner and the
> `gemini`/`openclaw` CLIs all resolve `AGENT_PROVIDER` through
> `devops_bench/core/model_providers.py`. The `api` harness uses it to pick the
> adapter family and backend for `get_model()`; the CLI harnesses use it to route
> `AGENT_API_KEY` onto the binary's provider-specific env var(s) (e.g. `google` →
> `GEMINI_API_KEY` + `GOOGLE_API_KEY`, `google-vertex` → `GOOGLE_CLOUD_API_KEY`)
> and, for openclaw, to pin the per-run model-catalog transport. A keyless
> provider routes no key.

## Configuration examples

These are copy-pasteable. Set the variables in your shell (or in whatever you use
to launch a run) before invoking the benchmark.

**Gemini via AI Studio (API key):**

```bash
export AGENT_PROVIDER=gemini
export AGENT_MODEL=gemini-3.1-pro-preview
export AGENT_API_KEY="$YOUR_AI_STUDIO_KEY"
```

**Gemini on Vertex AI (no key — uses ADC + project):**

```bash
export AGENT_PROVIDER=gemini
export AGENT_MODEL=gemini-3.1-pro-preview
export GCP_PROJECT_ID=my-gcp-project
export GCP_VERTEX_LOCATION=global
# No AGENT_API_KEY: the adapter uses Application Default Credentials.
```

**Claude via the first-party Anthropic API:**

```bash
export AGENT_PROVIDER=claude
export AGENT_MODEL=claude-sonnet-4-5
export AGENT_API_KEY="$YOUR_ANTHROPIC_KEY"
```

**Claude on Vertex AI (no key — uses ADC + project):**

```bash
export AGENT_PROVIDER=anthropic-vertex
export AGENT_MODEL=claude-sonnet-4-5@20250929
export GCP_PROJECT_ID=my-gcp-project
export GCP_VERTEX_LOCATION=global
# The provider key selects Vertex; no AGENT_API_KEY is needed or used.
```

**Ollama (local server):**

```bash
export AGENT_PROVIDER=ollama
export AGENT_MODEL=gemma4:2b
export OLLAMA_BASE_URL=http://localhost:11434/v1
```

## Adding a provider

To add your own provider — including the adapter file-naming convention and how
aliases resolve — see [Add a model provider](../how-to/add-a-model-provider.md).
