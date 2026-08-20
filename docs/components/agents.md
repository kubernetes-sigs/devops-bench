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

Four harnesses ship today. Each self-registers under a canonical key.

| Key | Wraps | How it runs | Capabilities |
| --- | --- | --- | --- |
| `gemini` | The Google **Gemini CLI** binary | Headless subprocess; trajectory parsed from `--output-format stream-json` on stdout | MCP, skills, rules, allowed-tools |
| `openclaw` | The **Openclaw Agent CLI** | `openclaw agent --local` with per-run isolated state/config; trajectory via `openclaw sessions export-trajectory` | MCP, skills, rules |
| `antigravity` | The **Antigravity CLI** (`agy` binary) | Headless subprocess that keeps the real `HOME` so cached OAuth/ADC credentials work (see the trust-boundary note below); trajectory parsed from the transcript JSONL it writes, token usage read from the conversation DB | MCP, skills, rules |
| `api` | **In-process** model call | Calls `get_model(provider, model)` and runs a model-agnostic MCP tool-use loop (`max_turns`, default 50) | MCP (spawns a stdio server), skills (served as tools), rules (system instruction) |

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
| `AGENT_TARGET` | unset | Path to the CLI binary (`gemini` / `oc`). Ignored by `api`. |
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
