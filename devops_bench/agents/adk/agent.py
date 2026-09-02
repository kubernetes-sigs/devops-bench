# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""In-process harness driving an agent built with the Agent Development Kit."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import importlib.util
import json
import os
import pathlib
import sys
from collections.abc import Iterator
from types import ModuleType
from typing import TYPE_CHECKING, Any

from devops_bench import core
from devops_bench.agents import base
from devops_bench.agents import config as agents_config
from devops_bench.agents import result as agents_result
from devops_bench.agents.adk import parsing
from devops_bench.agents.shared import cli_capabilities
from devops_bench.agents.shared import skills as shared_skills

if TYPE_CHECKING:
    from devops_bench.agents import capabilities

__all__ = ["AdkAgent"]

_log = core.get_logger("agents.adk")

#: Optional dependency group carrying the ADK SDK.
_EXTRA = "adk"

#: Attribute an ADK agent module is expected to expose, matching the convention
#: ADK's own loader follows.
_ROOT_AGENT_ATTR = "root_agent"

#: Submodule searched when a target package does not expose the root agent
#: itself — again ADK's own ``<agent_dir>/agent.py`` convention.
_AGENT_SUBMODULE = "agent"

#: Identifiers for the throwaway session each run drives the agent through.
_APP_NAME = "devops-bench"
_USER_ID = "devops-bench"

#: Budget for an MCP server to start and list its tools. Deliberately generous:
#: a tool server that shells out to a cloud CLI can be slow on a cold start, and
#: the SDK default is tuned for local in-memory servers.
_MCP_STARTUP_TIMEOUT_SEC = 60.0


def _looks_like_path(spec: str) -> bool:
    """Report whether a target names a filesystem location, not a module.

    A dotted module path (``my_pkg.agent``) and a filesystem path are both
    plausible targets, so the two are told apart syntactically rather than by
    probing the filesystem — a stray directory sharing a module's name must not
    silently change which agent loads.
    """
    return spec.endswith(".py") or spec.startswith(("~", ".")) or os.sep in spec


def _ensure_on_sys_path(directory: pathlib.Path) -> None:
    """Prepend ``directory`` to ``sys.path`` when it is not already there."""
    entry = str(directory)
    if entry not in sys.path:
        sys.path.insert(0, entry)


def _import_file(path: pathlib.Path, module_name: str) -> ModuleType:
    """Import a single ``.py`` file under ``module_name``, parent importable.

    Args:
        path: The ``.py`` file to load.
        module_name: Name to register the module under. Callers name it after
            the agent *directory* rather than the file, because ADK's layout
            puts every agent in an ``agent.py`` and registering that name would
            have each target clobber the last (and shadow any unrelated
            top-level ``agent`` module).

    Returns:
        The executed module.

    Raises:
        ConfigError: When the file is missing or has no usable import spec.
    """
    if not path.is_file():
        raise core.ConfigError(f"no ADK agent module at {path}")
    _ensure_on_sys_path(path.parent)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise core.ConfigError(f"cannot import an ADK agent module from {path}")
    module = importlib.util.module_from_spec(spec)
    # Register before executing so a module that imports itself by name (or is
    # re-imported by a dataclass/pydantic hook) resolves to this same object,
    # and drop the half-built entry again if execution fails.
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def _import_agent_module(spec: str) -> ModuleType:
    """Import the module holding the ADK agent named by ``spec``.

    Args:
        spec: A dotted module path, a path to a ``.py`` file, or a path to an
            agent directory laid out the way ADK expects
            (``<dir>/agent.py`` exposing ``root_agent``).

    Returns:
        The imported module.

    Raises:
        ConfigError: When the target names nothing importable.
    """
    if not _looks_like_path(spec):
        return importlib.import_module(spec)

    path = pathlib.Path(os.path.expanduser(spec))
    if not path.is_dir():
        return _import_file(path, path.stem)

    _ensure_on_sys_path(path.parent)
    if (path / "__init__.py").is_file():
        # A real package: import it by name so relative imports inside the
        # agent resolve against the package rather than breaking.
        return importlib.import_module(path.name)
    return _import_file(path / f"{_AGENT_SUBMODULE}.py", path.name)


def _resolve_root_agent(target: str) -> Any:
    """Load the ADK agent named by ``AGENT_TARGET``.

    Four spellings are accepted::

        my_pkg.agent:root_agent   # explicit module + attribute
        my_pkg.agent              # module, attribute defaults to root_agent
        ~/agents/my_agent         # ADK agent directory (<dir>/agent.py)
        ~/agents/my_agent/agent.py

    When the resolved attribute is a factory rather than an agent it is called
    with no arguments, so a team that builds its agent lazily needs no wrapper.

    Args:
        target: The configured target string.

    Returns:
        The ADK agent object.

    Raises:
        ConfigError: When the target is empty, names nothing importable,
            or resolves to something that is not an ADK agent.
    """
    from google.adk.agents import BaseAgent

    spec, _, attr = target.partition(":")
    attr = attr or _ROOT_AGENT_ATTR

    module = _import_agent_module(spec)
    obj = getattr(module, attr, None)
    if obj is None and hasattr(module, "__path__"):
        # A package whose ``__init__`` does not re-export the agent: fall back
        # to ADK's ``<package>/agent.py`` convention.
        module = importlib.import_module(f"{module.__name__}.{_AGENT_SUBMODULE}")
        obj = getattr(module, attr, None)
    if obj is None:
        raise core.ConfigError(f"module {module.__name__!r} exposes no {attr!r}")

    if not isinstance(obj, BaseAgent) and callable(obj):
        obj = obj()
    if not isinstance(obj, BaseAgent):
        raise core.ConfigError(
            f"{module.__name__}:{attr} is a {type(obj).__name__}, not an ADK agent"
        )
    return obj


def _iter_agents(agent: Any) -> list[Any]:
    """Return ``agent`` and every descendant, depth-first."""
    found = [agent]
    for sub in getattr(agent, "sub_agents", None) or ():
        found.extend(_iter_agents(sub))
    return found


def _apply_model(agent: Any, model: str) -> int:
    """Point every agent in the tree at ``model`` and report how many changed.

    A benchmark compares agents at a fixed model, so ``AGENT_MODEL`` overrides
    whatever the team baked into its agent — including sub-agents, since a tree
    left half-overridden would report a model it only partly ran.
    """
    changed = 0
    for node in _iter_agents(agent):
        if not hasattr(node, "model"):
            continue
        node.model = model
        changed += 1
    return changed


def _append_instruction(agent: Any, text: str) -> bool:
    """Append ``text`` to the agent's instruction, reporting whether it landed.

    ADK also accepts a *callable* instruction provider. There is no sound way to
    append to one, so the text is refused rather than silently dropped — the
    caller surfaces that as an error on the result.
    """
    if not text:
        return True
    current = getattr(agent, "instruction", None)
    if current is not None and not isinstance(current, str):
        return False
    agent.instruction = f"{current}\n\n{text}" if current else text
    return True


def _apply_instruction(agent: Any, text: str) -> tuple[int, list[str]]:
    """Append ``text`` to every instruction-bearing agent in the tree.

    Rules and skills are granted to the *run*, not to one node of it. The
    benchmark grades a tree as a single agent, and a delegate that never sees
    the operator brief can violate a constraint the root was told about.

    Workflow agents (``SequentialAgent`` and friends) carry no instruction and
    are skipped. A node using a callable instruction provider is named in the
    returned list, so the caller can report it rather than drop it silently.
    """
    delivered = 0
    refused: list[str] = []
    for node in _iter_agents(agent):
        if not hasattr(node, "instruction"):
            continue
        if _append_instruction(node, text):
            delivered += 1
        else:
            refused.append(str(getattr(node, "name", None) or "<unnamed>"))
    return delivered, refused


def _attach_toolsets(agent: Any, toolsets: list[Any]) -> int:
    """Attach ``toolsets`` to every tool-bearing agent in the tree.

    ADK resolves tools from the *active* agent alone (``BaseLlmFlow`` asks
    ``LlmAgent.canonical_tools``, which returns that agent's own ``tools``);
    nothing is inherited from a parent. Attaching to the root only would leave
    every delegate unable to touch the cluster the benchmark provisioned, and
    the run would still report success — it would just score zero.

    The toolset objects are shared across nodes rather than rebuilt per node,
    so a binding still means one MCP server process. ``McpToolset.close`` is
    documented as safe to call more than once, which is what ADK's cleanup does
    when the same toolset is reachable from several agents.
    """
    attached = 0
    for node in _iter_agents(agent):
        if not hasattr(node, "tools"):
            continue
        node.tools = list(getattr(node, "tools", None) or []) + toolsets
        attached += 1
    return attached


def _render_skills(paths: tuple[str, ...]) -> tuple[str, list[str]]:
    """Render discovered skills as an instruction section.

    ADK has no skills primitive, so skills are delivered the way rules are: as
    text the agent reads. Discovery goes through the shared walk, so which files
    count as skills cannot drift from the other harnesses.

    Args:
        paths: Skill source directories granted for the run.

    Returns:
        A ``(section, names)`` tuple; both are empty when nothing was found.
    """
    blocks: list[str] = []
    names: list[str] = []
    for skill in shared_skills.iter_skills(paths):
        names.append(skill.name)
        blocks.append(skill.content)
    if not blocks:
        return "", []
    return "# Available skills\n\n" + "\n\n".join(blocks), names


def _load_toolset_types() -> tuple[Any, Any, Any]:
    """Return the ``(McpToolset, StdioConnectionParams, StdioServerParameters)`` types.

    ``McpToolset`` was spelled ``MCPToolset`` in older ADK releases; both are
    accepted so the harness is not pinned to one SDK minor.
    """
    from google.adk.tools.mcp_tool import mcp_session_manager, mcp_toolset
    from mcp import StdioServerParameters

    toolset = getattr(mcp_toolset, "McpToolset", None) or mcp_toolset.MCPToolset
    return toolset, mcp_session_manager.StdioConnectionParams, StdioServerParameters


def _build_toolsets(
    mcp_servers: tuple[capabilities.McpBinding, ...],
) -> tuple[list[Any], list[str]]:
    """Build one ADK MCP toolset per binding that carries a launch command.

    A binding with no command denotes a server the agent hosts itself, so it is
    skipped — matching how the CLI harnesses read the same binding.

    Args:
        mcp_servers: Bindings granted for the run.

    Returns:
        A ``(toolsets, errors)`` tuple. ``errors`` is non-empty when the SDK's
        MCP support could not be loaded at all, in which case no toolset is
        returned and the caller reports that the agent ran without its tools.
    """
    # Reuse the shared builder so path-like commands resolve identically across
    # harnesses. One binding in, at most one entry out, so pairing each binding
    # with its own result keeps the tool filter attached to the right server
    # even when an earlier binding carried no command.
    pairs = [
        (binding, entry)
        for binding in mcp_servers
        for entry in cli_capabilities.build_mcp_servers((binding,)).values()
    ]
    if not pairs:
        return [], []

    try:
        toolset_cls, connection_cls, server_params_cls = _load_toolset_types()
    except ImportError as exc:
        return [], [f"ADK MCP support unavailable, agent ran without MCP tools: {exc}"]

    toolsets: list[Any] = []
    for binding, entry in pairs:
        params = connection_cls(
            server_params=server_params_cls(
                command=entry["command"], args=list(entry.get("args", []))
            ),
            timeout=_MCP_STARTUP_TIMEOUT_SEC,
        )
        toolsets.append(
            toolset_cls(
                connection_params=params,
                tool_filter=list(binding.tools) or None,
            )
        )
    return toolsets, []


def _dump_event(event: Any) -> dict:
    """Serialize one ADK event into a JSON-safe mapping.

    ``mode="json"`` is tried first because it renders enums and timestamps
    faithfully, but it raises on a tool that returned an object pydantic cannot
    encode. The fallback stringifies those leaves so one exotic tool result
    costs a field's fidelity rather than the whole trajectory.
    """
    try:
        return event.model_dump(mode="json", exclude_none=True)
    except Exception as exc:  # noqa: BLE001 - any serializer failure must degrade, not abort
        _log.debug("event did not serialize in json mode (%s); coercing", exc)
        return json.loads(json.dumps(event.model_dump(mode="python"), default=str))


@contextlib.contextmanager
def _in_workspace(workspace_path: pathlib.Path | None) -> Iterator[None]:
    """Run the agent with the harness workspace as the process working directory.

    The CLI harnesses launch their binary with ``cwd`` set to the harness-owned
    workspace, so a task that says "write ``report.md``" produces a file the
    orchestrator can diff and collect. An ADK agent runs in-process and has no
    subprocess to point anywhere, so without this its filesystem tools resolve
    relative paths against *the harness's* working directory: the deliverable
    misses the artifact diff entirely, and it lands somewhere shared where the
    next run can still see it.

    ``chdir`` is process-global, which is safe here only because the harness
    drives one agent at a time and the ADK run is confined to a single
    ``asyncio.run``. The previous directory is always restored.

    Args:
        workspace_path: The harness-owned workspace, or ``None`` when the
            orchestrator did not supply one (the agent then keeps the current
            directory, as before).
    """
    if workspace_path is None:
        yield
        return
    previous = os.getcwd()
    os.chdir(workspace_path)
    try:
        yield
    finally:
        os.chdir(previous)


def _drive(root_agent: Any, prompt: str, timeout_sec: float | None) -> tuple[list[dict], list[str]]:
    """Run the agent to completion and collect its serialized event stream.

    ``events`` is populated as the stream arrives rather than at the end: ADK
    surfaces a failing tool by yielding an error event and *then* raising out of
    the iterator, so a run that dies mid-way still has a partial trajectory
    worth scoring. Both the timeout and the failure are recorded as errors and
    returned alongside whatever was collected.

    Args:
        root_agent: The prepared ADK agent.
        prompt: Task prompt to send as the user message.
        timeout_sec: Wall-clock budget, or ``None`` for no limit.

    Returns:
        A ``(events, errors)`` tuple.
    """
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    events: list[dict] = []
    errors: list[str] = []

    async def _consume() -> None:
        runner = InMemoryRunner(root_agent, app_name=_APP_NAME)
        try:
            session = await runner.session_service.create_session(
                app_name=_APP_NAME, user_id=_USER_ID
            )
            message = types.Content(role="user", parts=[types.Part(text=prompt)])
            async for event in runner.run_async(
                user_id=_USER_ID, session_id=session.id, new_message=message
            ):
                events.append(_dump_event(event))
        finally:
            await runner.close()

    async def _main() -> None:
        try:
            if timeout_sec is None:
                await _consume()
            else:
                await asyncio.wait_for(_consume(), timeout=timeout_sec)
        except TimeoutError:
            errors.append(f"ADK run exceeded the {timeout_sec}s budget")
        except Exception as exc:  # noqa: BLE001 - keep the partial trajectory
            errors.append(f"ADK run failed: {type(exc).__name__}: {exc}")

    asyncio.run(_main())
    return events, errors


@base.AGENTS.register("adk")
class AdkAgent(base.AgentHarness):
    """Harness driving an Agent Development Kit agent in-process.

    ``AGENT_TARGET`` names the agent to load — a dotted module path, an
    ``agent_dir``, or either of those with an explicit ``:attribute``. The agent
    is deep-copied before the run so the harness's edits (model override,
    instruction text, MCP toolsets) never leak into the imported module and a
    second run in the same process starts from the same baseline.

    The trajectory comes from ADK's own event stream, so tool calls are recorded
    as the agent makes them rather than reconstructed from its prose.

    The run happens with the harness-owned workspace as the process working
    directory, matching the ``cwd`` the CLI harnesses give their subprocess, so
    an agent with filesystem tools writes its deliverables where the
    orchestrator collects them.
    """

    def __init__(self, config: agents_config.AgentConfig | None = None) -> None:
        super().__init__(config)
        caps = self.config.capabilities
        self.mcp_servers = caps.mcp_servers
        self.skills = caps.skills
        self.rules = caps.rules

    def _prepare(self, root_agent: Any) -> tuple[Any, dict, list[str]]:
        """Apply the run's model, capabilities, and tools to a private copy.

        Args:
            root_agent: The agent as imported (left untouched).

        Returns:
            A ``(prepared_agent, metadata, errors)`` tuple.
        """
        prepared = root_agent.model_copy(deep=True)
        metadata: dict = {"agent_name": getattr(prepared, "name", None)}
        errors: list[str] = []

        if self.config.model:
            metadata["model_override_count"] = _apply_model(prepared, self.config.model)

        sections: list[str] = []
        if self.rules.text:
            sections.append(self.rules.text)
        skills_section, skill_names = _render_skills(self.skills.paths)
        if skills_section:
            sections.append(skills_section)
            metadata["skills"] = skill_names
        if sections:
            delivered, refused = _apply_instruction(prepared, "\n\n".join(sections))
            metadata["instruction_agents"] = delivered
            if refused:
                errors.append(
                    "rules and skills were not delivered to agents using a callable "
                    f"instruction provider: {', '.join(refused)}"
                )

        toolsets, toolset_errors = _build_toolsets(self.mcp_servers)
        errors.extend(toolset_errors)
        if toolsets:
            metadata["mcp_toolsets"] = len(toolsets)
            metadata["mcp_agents"] = _attach_toolsets(prepared, toolsets)

        return prepared, metadata, errors

    def _execute(
        self, prompt: str, workspace_path: pathlib.Path | None = None
    ) -> agents_result.AgentResult:
        try:
            import google.adk  # noqa: F401
        except ImportError as exc:
            raise core.MissingDependencyError("the ADK agent harness", _EXTRA) from exc

        target = (self.config.target or "").strip()
        if not target:
            return agents_result.AgentResult.errored(
                "AGENT_TARGET must name the ADK agent to run "
                "(e.g. 'my_pkg.agent:root_agent' or a path to an agent directory)"
            )

        try:
            root_agent = _resolve_root_agent(target)
        except (core.ConfigError, ImportError, AttributeError, TypeError) as exc:
            return agents_result.AgentResult.errored(
                f"could not load the ADK agent at {target!r}: {type(exc).__name__}: {exc}"
            )

        # Prepared outside the workspace: ``_build_toolsets`` resolves path-like
        # MCP commands against the current directory, and those are the
        # operator's paths, not the agent's.
        prepared, metadata, errors = self._prepare(root_agent)
        if workspace_path is not None:
            metadata["workspace"] = str(workspace_path)
        with _in_workspace(workspace_path):
            events, run_errors = _drive(prepared, prompt, self.config.timeout_sec)
        errors.extend(run_errors)

        output, trajectory, tokens, parse_errors = parsing.parse_event_stream(events)
        errors.extend(parse_errors)
        metadata["event_count"] = len(events)

        if not output and errors:
            output = f"Error: {errors[0]}"

        return agents_result.AgentResult(
            output=output,
            trajectory=trajectory,
            tokens=tokens,
            errors=errors,
            metadata=metadata,
        )
