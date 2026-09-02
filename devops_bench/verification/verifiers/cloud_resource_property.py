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

"""Assert a JSONPath property of a cloud resource (GCP today).

The cloud-side sibling of ``resource_property``: same JSONPath dialect,
operator table, and quantifier semantics (imported from
``_property_semantics``, shared rather than reimplemented), applied to the
parsed JSON output of a read-only cloud CLI invocation that the task spec
itself supplies in ``args``.

The bench does not enumerate which cloud resources exist. ``args`` is the
CLI invocation minus the binary, minus output-format flags, and (usually)
minus the provider context flag. What the bench owns per provider is one
frozen :class:`ProviderDescriptor`: the binary, the read-only verb
allowlist, the JSON output flags, the not-found stderr markers, and how
ambient context is injected (``--project`` from ``GCP_PROJECT_ID``).
Grading never falls back to the CLI's own ambient config.

A cloud CLI's "resource absent" and "caller lacks permission to look" both
surface as a nonzero exit, and they are not the same claim: stderr is
checked against the permission markers first, since a permission failure can
be worded to look like a not-found one; only then does a not-found marker
match get treated as an empty fetched-object set (so ``op: absent`` can pass
on the right grounds). Any other nonzero exit is ``status="error"``, never
``"fail"``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from jsonpath_ng.exceptions import JSONPathError as _JsonPathError
from pydantic import model_validator

from devops_bench.core import get_env
from devops_bench.core.subprocess import run
from devops_bench.verification.base import (
    VERIFIERS,
    BaseVerifier,
    VerificationResult,
    VerificationStatus,
    single_call_timeout,
)
from devops_bench.verification.verifiers._property_semantics import (
    _SET_OPS,
    _VALUE_OPS,
    _compile,
    _compile_regex,
    evaluate_matched_objects,
)

__all__ = ["CloudResourcePropertyVerifier", "ProviderDescriptor"]


@dataclass(frozen=True)
class ProviderDescriptor:
    """Everything the bench knows about one cloud CLI, as constants.

    Attributes:
        binary: The CLI executable.
        read_verbs: ``args`` must contain at least one of these tokens; the
            only allowlist in the design, per provider rather than per
            resource.
        json_args: Output-format flags the verifier appends; a spec passing
            its own is rejected at load time.
        not_found_markers: Case-insensitive stderr substrings meaning "the
            resource genuinely does not exist", checked on nonzero exit.
        permission_markers: Case-insensitive stderr substrings meaning "the
            caller lacks permission to look", checked before
            ``not_found_markers`` since a permission failure can be worded to
            look like a not-found one.
        context_flag: Flag injected from ``context_env`` when the spec did
            not pass it explicitly.
        context_env: Environment variable supplying ``context_flag``'s value.
    """

    binary: str
    read_verbs: frozenset[str]
    json_args: tuple[str, ...]
    not_found_markers: tuple[str, ...]
    permission_markers: tuple[str, ...] = ()
    context_flag: str | None = None
    context_env: str | None = None


_GCP = ProviderDescriptor(
    binary="gcloud",
    read_verbs=frozenset({"list", "describe", "get-iam-policy"}),
    json_args=("--format=json",),
    not_found_markers=("was not found", "not_found", "no such", "does not exist"),
    permission_markers=(
        "permission_denied",
        "does not have permission",
        "you do not have permission",
        "insufficient authentication scopes",
        "forbidden",
    ),
    context_flag="--project",
    context_env="GCP_PROJECT_ID",
)

# Adding a provider is one descriptor here plus widening the `provider`
# Literal below; no protocol, no registry, no plugin machinery.
_DESCRIPTORS: dict[str, ProviderDescriptor] = {"gcp": _GCP}


def _object_label(obj: Any, index: int) -> str:
    """Best-effort display label for one fetched object.

    Cloud JSON shapes vary by resource type (there is no uniform
    ``metadata.name``); fall back to a positional label when a dict has no
    ``name`` field, or the object is not a dict at all.
    """
    if isinstance(obj, dict):
        name = obj.get("name")
        if isinstance(name, str):
            return name
    return f"item[{index}]"


_STDERR_LIMIT = 500
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _sanitize_stderr(text: str) -> str:
    """Collapse, truncate, and strip account emails from CLI stderr before it lands in a result reason."""
    text = " ".join((text or "").split())
    text = _EMAIL_RE.sub("<redacted-email>", text)
    if len(text) > _STDERR_LIMIT:
        text = text[:_STDERR_LIMIT] + "...(truncated)"
    return text


@VERIFIERS.register("cloud_resource_property")
class CloudResourcePropertyVerifier(BaseVerifier):
    """Compare a JSONPath property of objects fetched by a spec-supplied CLI call.

    See the module docstring for the absent-vs-permission-denied distinction
    and the shared JSONPath/operator semantics with ``resource_property``.
    """

    type: Literal["cloud_resource_property"] = "cloud_resource_property"
    provider: Literal["gcp"] = "gcp"
    args: list[str]
    path: str | None = None
    op: Literal["eq", "ne", "gt", "gte", "lt", "lte", "exists", "absent", "contains", "matches"]
    value: Any = None
    across_matches: Literal["every", "none"] | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> CloudResourcePropertyVerifier:
        """Reject specs that could mutate, or cannot mean anything, at load time."""
        desc = _DESCRIPTORS[self.provider]
        prefix = []
        for token in self.args:
            if token.startswith("-"):
                break
            prefix.append(token)
        if not set(prefix) & desc.read_verbs:
            msg = (
                f"args {self.args!r} contain no read-only verb for provider "
                f"{self.provider!r}; expected one of {sorted(desc.read_verbs)}"
            )
            raise ValueError(msg)
        owned = {flag.split("=", 1)[0] for flag in desc.json_args}
        clashing = [a for a in self.args if a.split("=", 1)[0] in owned]
        if clashing:
            msg = (
                f"args must not set the output format ({clashing!r}); the verifier "
                f"appends {list(desc.json_args)!r} itself"
            )
            raise ValueError(msg)
        if self.op not in _SET_OPS and not self.path:
            raise ValueError(f"op {self.op!r} requires 'path'")
        if self.path is not None:
            try:
                _compile(self.path)
            except _JsonPathError as exc:
                msg = f"path {self.path!r} is not a valid JSONPath: {exc}"
                raise ValueError(msg) from exc
        if self.op == "absent" and self.across_matches:
            msg = "op 'absent' already asserts emptiness and does not take 'across_matches'"
            raise ValueError(msg)
        if self.op == "exists" and not self.path and self.across_matches:
            msg = (
                "op 'exists' without 'path' applies to the fetched object set "
                "and does not take 'across_matches'"
            )
            raise ValueError(msg)
        if self.op in _VALUE_OPS and self.value is None:
            raise ValueError(f"op {self.op!r} requires 'value'")
        if self.op == "matches":
            try:
                _compile_regex(str(self.value))
            except re.error as exc:
                msg = f"op 'matches' has an invalid pattern {self.value!r}: {exc}"
                raise ValueError(msg) from exc
        return self

    def verify(self, timeout_sec: float) -> VerificationResult:
        """Poll the property until it holds or the budget runs out."""
        return self._poll_to_result(lambda: self._check(timeout_sec), timeout_sec)

    def _assemble_argv(self) -> list[str]:
        """The full command: binary, spec args, injected context, owned format flags."""
        desc = _DESCRIPTORS[self.provider]
        argv: list[str] = [desc.binary, *self.args]
        if desc.context_flag and desc.context_env:
            explicit = any(
                a == desc.context_flag or a.startswith(desc.context_flag + "=") for a in self.args
            )
            ambient = get_env(desc.context_env)
            if not explicit and ambient:
                argv += [desc.context_flag, ambient]
        argv += list(desc.json_args)
        return argv

    def _check(self, timeout_sec: float) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        """One evaluation pass: run the CLI, parse JSON, apply the operator."""
        desc = _DESCRIPTORS[self.provider]
        try:
            completed = run(
                self._assemble_argv(), check=False, timeout=single_call_timeout(timeout_sec)
            )
        except Exception as exc:  # noqa: BLE001 - a CLI failure is a check error
            return "error", f"{desc.binary} invocation failed: {exc}", None

        subject = " ".join(self.args)
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            lowered = stderr.lower()
            sanitized_stderr = _sanitize_stderr(stderr)
            if any(marker in lowered for marker in desc.permission_markers):
                return (
                    "error",
                    f"{desc.binary} exited {completed.returncode}: {sanitized_stderr or 'no stderr'}",
                    None,
                )
            if not any(marker in lowered for marker in desc.not_found_markers):
                return (
                    "error",
                    f"{desc.binary} exited {completed.returncode}: {sanitized_stderr or 'no stderr'}",
                    None,
                )
            objects: list[Any] = []
        else:
            try:
                payload = json.loads(completed.stdout or "null")
            except json.JSONDecodeError as exc:
                return "error", f"{desc.binary} produced non-JSON output: {exc}", None
            objects = [] if payload is None else payload if isinstance(payload, list) else [payload]

        names = [_object_label(obj, i) for i, obj in enumerate(objects)]
        return evaluate_matched_objects(
            self.op, self.value, self.across_matches, self.path, objects, names, subject
        )
