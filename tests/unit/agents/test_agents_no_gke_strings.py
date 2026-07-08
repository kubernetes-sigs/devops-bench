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

"""Acceptance: ``devops_bench/agents/`` runs no GKE-specific behavior.

Capabilities are generic by design (handoff §5 / PR3 acceptance): the GKE MCP
server name, the GKE tool names, and the GKE skill paths all arrive as values
on :class:`~devops_bench.agents.capabilities.AllCapabilities`, never as
literals inside agent code. The orchestrator's catalog is the *one* place that
knows the word "GKE".

This is a regression bar — it walks the agents/ source AST and fails fast if
any *runtime* code (string literal, identifier, attribute) references GKE.
Comments and docstrings are intentionally allowed: the new capability classes
document why GKE *no longer* appears in their bodies, which is the right place
to teach the reader.
"""

from __future__ import annotations

import ast
import pathlib

import devops_bench.agents as _agents_pkg

# Tokens that must not appear in *runtime* values inside agents/. Matched
# case-insensitively against AST identifiers and string-literal values.
_FORBIDDEN_TOKENS = ("gke",)


def _docstring_node_ids(tree: ast.AST) -> set[int]:
    """Return ``id(node)`` of every docstring constant in ``tree``.

    A docstring is the first statement of a Module / FunctionDef /
    AsyncFunctionDef / ClassDef when that statement is an ``Expr`` wrapping a
    string ``Constant``. The walker skips those identifiers when checking
    runtime literals.
    """
    docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            body = getattr(node, "body", None)
            if not body:
                continue
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstring_ids.add(id(first.value))
    return docstring_ids


def test_agents_package_has_no_gke_specific_runtime_strings():
    root = pathlib.Path(_agents_pkg.__file__).parent
    hits: list[tuple[pathlib.Path, int, str]] = []
    for src in root.rglob("*.py"):
        text = src.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(src))
        docstring_ids = _docstring_node_ids(tree)

        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstring_ids
            ):
                lowered = node.value.lower()
                if any(tok in lowered for tok in _FORBIDDEN_TOKENS):
                    hits.append((src.relative_to(root.parent), node.lineno, node.value))
            elif isinstance(node, ast.Name):
                lowered = node.id.lower()
                if any(tok in lowered for tok in _FORBIDDEN_TOKENS):
                    hits.append((src.relative_to(root.parent), node.lineno, node.id))
            elif isinstance(node, ast.Attribute):
                lowered = node.attr.lower()
                if any(tok in lowered for tok in _FORBIDDEN_TOKENS):
                    hits.append((src.relative_to(root.parent), node.lineno, node.attr))

    assert not hits, (
        "agents/ must carry no GKE-specific runtime literals (catalog lives "
        "in the harness, per PR3 §5). Offenders:\n"
        + "\n".join(f"  {p}:{ln}: {snippet}" for p, ln, snippet in hits)
    )
