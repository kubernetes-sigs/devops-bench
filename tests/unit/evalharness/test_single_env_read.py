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

"""Single ``BENCH_USE_MCP`` read site (CONVENTIONS.md §7 / harness handoff §2.2).

The harness owns the only read of ``BENCH_USE_MCP``; the resolved boolean is
threaded into both the agent's :class:`AgentConfig` (gates the MCP binding)
and the metrics scoring call (``MetricContext.use_mcp``). This file pins the
invariant so a future change cannot silently re-introduce the agent-vs-judge
disagreement that motivated §7.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from devops_bench.evalharness.default import DefaultEvalHarness


@pytest.fixture
def isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every ``BENCH_*`` / ``AGENT_*`` knob the harness reads.

    Keeps the test from picking up the developer's shell env (e.g. an
    ``AGENT_MCP_SERVER`` left over from a manual run) which would muddle the
    capability assertions.
    """
    for var in (
        "BENCH_USE_MCP",
        "BENCH_AGENT_TYPE",
        "BENCH_NO_INFRA",
        "AGENT_MCP_SERVER",
        "AGENT_ALLOWED_TOOLS",
        "AGENT_SKILLS_PATHS",
        "AGENT_RULES_TEXT",
        "AGENT_TARGET",
        "AGENT_MODEL",
        "AGENT_PROVIDER",
    ):
        monkeypatch.delenv(var, raising=False)


def test_harness_reads_use_mcp_once_at_construction(
    monkeypatch: pytest.MonkeyPatch, isolate_env: None
) -> None:
    """``BENCH_USE_MCP`` is read once during ``__init__``, not on every method."""
    monkeypatch.setenv("BENCH_USE_MCP", "true")

    with patch(
        "devops_bench.evalharness.default.get_bool",
        wraps=__import__("devops_bench.core", fromlist=["get_bool"]).get_bool,
    ) as wrapped:
        harness = DefaultEvalHarness(project_id="p", cluster_name="c")

        bench_use_mcp_reads = [
            call for call in wrapped.call_args_list if call.args and call.args[0] == "BENCH_USE_MCP"
        ]
        # Exactly one harness-driven read of BENCH_USE_MCP at construction.
        assert len(bench_use_mcp_reads) == 1
        assert harness.use_mcp is True


def test_use_mcp_flows_into_agent_capabilities_and_metrics(
    monkeypatch: pytest.MonkeyPatch, isolate_env: None
) -> None:
    """The same resolved bool reaches the AgentConfig and the metrics call."""
    monkeypatch.setenv("BENCH_USE_MCP", "false")
    monkeypatch.setenv("AGENT_MCP_SERVER", "echo hi")
    monkeypatch.setenv("AGENT_ALLOWED_TOOLS", "run_command")

    harness = DefaultEvalHarness(project_id="p", cluster_name="c")

    # Agent side: a False ``use_mcp`` gates off the MCP binding even when the
    # env names a server — only the harness decides whether MCP runs.
    config = harness.build_agent_config()
    assert config.capabilities.mcp_servers == ()
    assert config.capabilities.tools_enabled is False
    assert harness.use_mcp is False

    # Metrics side: the score path threads the same boolean as
    # ``use_mcp=False`` into ``evaluate_metrics_batch``.
    captured: dict = {}

    def fake_batch(results: list[dict], judge: object, *, use_mcp: bool) -> None:
        captured["use_mcp"] = use_mcp
        captured["count"] = len(results)

    monkeypatch.setattr(
        "devops_bench.metrics.evaluate_metrics_batch",
        fake_batch,
        raising=False,
    )
    monkeypatch.setattr(
        "devops_bench.metrics.get_judge_model",
        lambda: object(),
        raising=False,
    )

    harness._score([{"status": "success", "name": "t"}])  # noqa: SLF001
    assert captured == {"use_mcp": False, "count": 1}


def test_use_mcp_true_grants_mcp_binding(
    monkeypatch: pytest.MonkeyPatch, isolate_env: None
) -> None:
    """A True ``use_mcp`` lets the env-declared MCP binding through."""
    monkeypatch.setenv("BENCH_USE_MCP", "true")
    monkeypatch.setenv("AGENT_MCP_SERVER", "/path/to/mcp")
    monkeypatch.setenv("AGENT_ALLOWED_TOOLS", "tool_a,tool_b")

    harness = DefaultEvalHarness(project_id="p", cluster_name="c")
    config = harness.build_agent_config()

    assert config.capabilities.tools_enabled is True
    assert config.capabilities.mcp is not None
    assert config.capabilities.allowed_tools == ("tool_a", "tool_b")


def test_only_one_executable_use_mcp_read_in_repo(
    monkeypatch: pytest.MonkeyPatch, isolate_env: None, tmp_path: Path
) -> None:
    """Only one *executable* ``BENCH_USE_MCP`` read site exists in the package.

    Pinned as a test rather than a comment so a refactor that re-introduces a
    second read site fails CI rather than passes review. Plain docstring
    mentions ("never reads ``BENCH_USE_MCP``") are intentionally allowed —
    the assertion is on actual calls into ``get_bool`` / ``os.environ``.
    """
    import re

    devops_bench_root = Path(__file__).resolve().parents[3] / "devops_bench"
    # Match every realistic way a Python module reads BENCH_USE_MCP — the
    # core helpers (``get_bool``, ``get_env``, ``first_env``), the stdlib
    # ``os.getenv`` / ``os.environ.get`` / ``os.environ[...]`` paths, and
    # bare ``environ["..."]`` (e.g. ``from os import environ``). A reviewer
    # eyeballing §7 compliance would search for any of these; codify the
    # search so a sneaked-in second read fails CI instead of passing review.
    read_pattern = re.compile(
        r"("
        r"get_bool|get_env|first_env|os\.getenv|"
        r"os\.environ\.get|os\.environ\[|"
        r"\bgetenv|\benviron\["
        r")\s*\(?\s*[\"']BENCH_USE_MCP[\"']"
    )
    reads: dict[Path, int] = {}
    for path in devops_bench_root.rglob("*.py"):
        text = path.read_text()
        matches = read_pattern.findall(text)
        if matches:
            reads[path.relative_to(devops_bench_root)] = len(matches)

    # Allowed read sites (CONVENTIONS.md §7):
    # * ``evalharness/default.py`` — the canonical single read site.
    # * ``metrics/pipeline.py`` — a documented *fallback* the harness never
    #   triggers (it always passes ``use_mcp=`` explicitly), retained for
    #   legacy CLI callers running ``evaluate_metrics_batch`` directly.
    assert set(reads.keys()) <= {
        Path("evalharness/default.py"),
        Path("metrics/pipeline.py"),
    }, f"unexpected BENCH_USE_MCP reads in: {sorted(reads)}"
    # Pin the harness file to exactly one read so an accidental second read
    # site (e.g. from a per-task refresh) is caught here.
    assert reads.get(Path("evalharness/default.py")) == 1
