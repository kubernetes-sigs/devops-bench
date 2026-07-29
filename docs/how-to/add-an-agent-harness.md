# Add an agent harness

This guide walks through wrapping a new agent so the benchmark can drive it. The
contract is small: subclass `AgentHarness`, implement `_execute`, register the
class with `@AGENTS.register`, and add your module to the builtin import list.
That's it — no `cli.py` or `run.py` edits.

For the concepts (harness vs model, capabilities, configuration), read
[Agents](../components/agents.md) first.

## The contract

| You do | Where |
| --- | --- |
| Subclass `AgentHarness` | `devops_bench/agents/base.py` |
| Implement `_execute(self, prompt) -> AgentResult` | your new module |
| Register with `@AGENTS.register("<key>")` | your new module |
| Add the module to `_BUILTIN_AGENT_MODULES` | `devops_bench/evalharness/default.py` |

## Steps

### 1. Create the module

Mirror an existing harness. For a CLI-backed agent, follow `gemini_cli` /
`openclaw`:

```text
devops_bench/agents/cli/<name>/agent.py
```

For an in-process agent, follow `api`:

```text
devops_bench/agents/<name>/agent.py
```

### 2. Subclass `AgentHarness` and assign capability bindings

Call the base `__init__` with your config, then assign `self.mcp_servers`,
`self.skills`, and `self.rules` from `self.config.capabilities`. Those three
assignments are what make your harness structurally satisfy the capability
Protocols (`SupportsMcp` / `SupportsSkills` / `SupportsRules`) — no mixin needed.

### 3. Implement only `_execute`

`_execute(self, prompt: str) -> AgentResult` is the single extension point.
Inside it:

- Build the invocation for your agent (argv, an API call, whatever it takes).
- Parse the agent's output into canonical `ToolCall` entries
  (`devops_bench/agents/result.py`) for the trajectory.
- On a *known* failure (subprocess error, parse miss, timeout), record a message
  on `AgentResult.errors` rather than dropping it silently. For a hard failure
  with no usable output, return `AgentResult.errored(msg)`.
- Return an `AgentResult`. Leave `latency` at zero — the base `run()` fills it in.

> [!NOTE]
> Only handle your *known* errors. The base class already catches unexpected
> exceptions and converts them to an errored result, so you don't need a
> catch-all.

### 4. Register the class

Decorate it with its canonical key:

```python
@AGENTS.register("<key>")
class MyAgent(AgentHarness):
    ...
```

### 5. Wire it for import side-effects

Registration only fires when the module is imported, so add its path to
`_BUILTIN_AGENT_MODULES` in `devops_bench/evalharness/default.py`:

```python
_BUILTIN_AGENT_MODULES: tuple[str, ...] = (
    "devops_bench.agents.cli.gemini_cli",
    "devops_bench.agents.cli.openclaw",
    "devops_bench.agents.api.agent",
    "devops_bench.agents.<name>.agent",   # <- your module
)
```

The import loop tolerates `ImportError` / `MissingDependencyError`, so a harness
that needs an optional SDK won't break the host that lacks it. If you want a
friendlier selector name, add an entry to `_AGENT_TYPE_ALIASES` in the same file —
for example, mapping `gemini-cli` to `gemini`.

### 6. Reuse the shared CLI helpers

For a CLI agent, don't re-implement capability plumbing. Reuse the helpers in
`devops_bench/agents/shared/cli_capabilities.py`:

- `build_mcp_servers(...)` — turns granted MCP bindings into a `{name: {command, args}}` launch map.
- `materialize_skills(...)` — copies discovered `SKILL.md` files into a skills directory and returns its path.

> [!IMPORTANT]
> These helpers stage the files, but they don't tell your agent where to find
> them. Your `_execute` is responsible for pointing the underlying tool at the
> staged locations — whether that's a CLI flag, a config file, or an environment
> variable (e.g. the Gemini CLI agent writes the MCP launch map into its settings
> and the openclaw agent exports its skills dir). Wire the path/env through in
> your harness, or the staged MCP servers and skills won't be picked up.

### 7. Select it

Pick your harness with `BENCH_AGENT_TYPE=<key>` (or `--agent-type <key>`). No
other code changes are required — the registry resolves it at run time.

## Skeleton

```python
from devops_bench.agents.base import AGENTS, AgentHarness
from devops_bench.agents.config import AgentConfig
from devops_bench.agents.result import AgentResult, ToolCall


@AGENTS.register("myagent")
class MyAgent(AgentHarness):
    """Harness driving <the agent you wrap>."""

    def __init__(self, config: AgentConfig | None = None) -> None:
        AgentHarness.__init__(self, config)
        caps = self.config.capabilities
        self.mcp_servers = caps.mcp_servers
        self.skills = caps.skills
        self.rules = caps.rules

    def _execute(self, prompt: str) -> AgentResult:
        # 1. Build and run the invocation for `prompt`.
        # 2. Parse output into canonical ToolCall entries.
        trajectory: list[dict] = [
            ToolCall(name="example_tool", args={}).to_dict(),
        ]
        # 3. On a known failure, return AgentResult.errored("...").
        # 4. Return the result (leave latency at zero; the base stamps it).
        return AgentResult(output="...", trajectory=trajectory)
```

## Test it

Run a no-infra task with your harness selected. The `noop` deployer skips cluster
provisioning so you can confirm the harness drives the agent and returns a
trajectory end-to-end without standing up infrastructure:

```bash
export BENCH_AGENT_TYPE=myagent
export BENCH_NO_INFRA=true
export AGENT_PROVIDER=...
export AGENT_MODEL=...
# run a single generation-only task and inspect results.json
```

Check the run's `results.json`: a clean run shows your parsed `trajectory`, a
populated `output`, and an empty `errors` list.
