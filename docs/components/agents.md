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

## Adding your own harness

Want to wrap a different agent? See
[Add an agent harness](../how-to/add-an-agent-harness.md).
