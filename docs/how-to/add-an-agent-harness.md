# Add an agent harness

This guide walks through wrapping a new agent so the benchmark can drive it. The
contract is small: subclass `AgentHarness`, implement `_execute`, register the
class with `@AGENTS.register`, and add your module to the built-in import list.
That's it — no `cli.py` or `run.py` edits.

For the concepts (harness vs model, capabilities, configuration), read
[Agents](../components/agents.md) first.

## The contract

| You do | Where |
| --- | --- |
| Subclass `AgentHarness` | `devops_bench/agents/base.py` |
| Implement `_execute(self, prompt, workspace_path=None) -> AgentResult` | your new module |
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

`_execute(self, prompt: str, workspace_path: Path | None = None) -> AgentResult`
is the single extension point. `run()` calls it positionally, so the second
parameter is required even if your harness ignores it.
Inside it:

- Build the invocation for your agent (argv, an API call, whatever it takes).
- Parse the agent's output into canonical `ToolCall` entries
  (`devops_bench/agents/result.py`) for the trajectory.
- If your agent **delegates to subagents**, attribute their calls — see
  [Multi-agent trajectories](#multi-agent-trajectories) below.
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
from pathlib import Path

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

    def _execute(self, prompt: str, workspace_path: Path | None = None) -> AgentResult:
        # 1. Build and run the invocation for `prompt`.
        # 2. Parse output into canonical ToolCall entries.
        trajectory: list[dict] = [
            ToolCall(name="example_tool", args={}).to_dict(),
        ]
        # 3. On a known failure, return AgentResult.errored("...").
        # 4. Return the result (leave latency at zero; the base stamps it).
        return AgentResult(output="...", trajectory=trajectory)
```

## Multi-agent trajectories

Some agents don't do the work themselves — they route it to specialized
subagents. If yours does, the trajectory has to say **which** agent made each
call. A flat list can't distinguish a router that only read the cluster from one
whose worker mutated it, so any metric grading tool-use fidelity or a
"must not touch" safeguard is reading a trace it cannot trust.

`ToolCall` carries three optional attribution fields:

| Field | Meaning |
| --- | --- |
| `actor` | Who made the call — `ROOT_ACTOR` (`"root"`) for the top-level agent, otherwise the delegate's role name (`"cluster"`, `"operator"`, …). Fall back to `SUBAGENT_ACTOR` only when the delegation is visible but the role is not. |
| `call_id` | Your agent's own id for this call, when it exposes one. |
| `parent_id` | The `call_id` of the delegating call this one was made *inside*; unset for a top-level call. |

```python
ToolCall(name="Task", args={"subagent": "cluster"}, actor=ROOT_ACTOR, call_id="spawn-1")
ToolCall(name="kubectl_get", args={"resource": "pods"}, actor="cluster", parent_id="spawn-1")
```

Three rules:

- **Switch attribution on only once a delegated *call* reaches the trajectory.**
  It is the *feature* that is all-or-nothing, not the three fields. A run with no
  delegated call leaves all three unset on every entry, so they are omitted from
  the serialized entry and the trajectory is byte-identical to one produced
  before these fields existed. That is deliberate: the trajectory is
  re-serialized into the judge's prompt, so a key present on every entry would
  move the scores of runs that have no fleet to attribute.

  The trigger is a delegated call, not the delegation itself. A delegate that
  answers in text and calls no tool contributes no entry, so there is nothing to
  misattribute and attribution stays off — every call in that trajectory really
  was the root's. Switching it on there would stamp `actor` on every entry to
  convey nothing, and move the run's score for it.

  Once attribution is on, the three fields are **not** uniform — set only what
  you actually know:

  | Field | When attribution is on |
  | --- | --- |
  | `actor` | Required on **every** entry, including the top-level agent's (`ROOT_ACTOR`). |
  | `call_id` | Only when your agent exposes an id for the call. |
  | `parent_id` | Only on a call made *inside* a delegation; a top-level call has none. |
- **Never fold an unattributable call into `root`.** If you can see that a call
  came from a delegate but can't name which, fall back to `SUBAGENT_ACTOR` —
  and make that fallback **distinct per delegation** (`subagent-1`,
  `subagent-2`, …). Attributing the call to the top-level agent asserts
  something it didn't do; collapsing two anonymous workers onto one label makes
  one agent look like it placed every call, which is the same error one level
  down.
- **Prefer a name your runtime stamped over one the model supplied.** If the
  delegate's role reaches you both as framework metadata and as an argument the
  agent passed to its own spawn tool, trust the framework. The argument is the
  one an agent under test could misreport, which matters precisely when you are
  using attribution to check whether it stayed in its lane.

`devops_bench/agents/cli/claude_code/parsing.py` is the worked example. It
recovers attribution from the `parent_tool_use_id` the CLI tags delegated turns
with, and resolves the role name from three sources in descending authority: the
`subagent_type` stamped on the delegated turn itself, the `task_*` lifecycle
event announcing the spawn, and last the spawning call's arguments.

## Test it

Run a no-infra task with your harness selected. The `noop` deployer skips cluster
provisioning so you can confirm the harness drives the agent and returns a
trajectory end-to-end without standing up infrastructure:

```bash
export BENCH_AGENT_TYPE=myagent
export BENCH_NO_INFRA=true
export AGENT_PROVIDER=myprovider
export AGENT_MODEL=mymodel
# run a single generation-only task and inspect results.json
```

Check the run's `results.json`: a clean run shows your parsed `trajectory`, a
populated `output`, and an empty `errors` list.
