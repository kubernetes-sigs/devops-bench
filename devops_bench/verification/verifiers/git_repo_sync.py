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

"""Assert a property of a file, or the commit state, in a host-side bare git repo.

This is ``resource_property``'s counterpart for the other half of a GitOps
loop: where that verifier reads live Kubernetes API state, this one reads the
repository that state is supposed to converge from. The two share one
comparison vocabulary (``op``/``value``/``across_matches``) on purpose, so a
task author only learns "eq/ne/gt/gte/lt/lte/contains/matches" and the
fail-closed reduction rules once.

Reads the bare repo directly with ``git -C <repo_path> show <ref>:<file>``
rather than cloning it: the agent already owns a working clone (or pushes
straight to the bare repo), so a second clone here would only add I/O and
worktree management for a check that reads at most one file at one ref.
``file`` content is parsed as one or more YAML documents (ruamel.yaml, the
same library the task loader uses) and ``path`` (JSONPath, extended) is
evaluated against the list of documents, so a filter predicate selects the
right document out of a multi-document manifest.

``require_new_commit`` asserts HEAD is not among the repo's root (seed)
commits (``git rev-list --max-parents=0``). The repo is seeded with one
commit before the task starts, so "HEAD is still a root commit" means the
agent never committed anything, and a no-op agent fails this check even if
the file it would otherwise assert on happens to already satisfy the rest of
the check.

``require_new_commit`` does not combine with a content assertion (``file``)
in the same check: ``_check`` resolves the seed commit before it reads any
file, so a transient ``git rev-list`` failure would short-circuit to
``"error"`` and mask what the content assertion would otherwise have
observed. Assert the new commit and the file's content as two separate
``git_repo_sync`` entries instead, e.g. under one ``all`` node, as
opa-remediation's ``repo-remediated`` objective does.
"""

from __future__ import annotations

import os
from typing import Any, Literal

from jsonpath_ng.exceptions import JSONPathError
from pydantic import model_validator
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from devops_bench.core import SubprocessError, get_logger
from devops_bench.core.subprocess import run
from devops_bench.verification.base import (
    VERIFIERS,
    BaseVerifier,
    VerificationResult,
    VerificationStatus,
)

# The two verifiers share one comparison vocabulary on purpose (see the
# module docstring); reuse resource_property's operator table, and its
# element-wise across_matches machinery, instead of maintaining a second
# copy here.
from devops_bench.verification.verifiers.resource_property import (
    _VALUE_OPS,
    _apply_op,
    _compile,
    _render_path,
    _split_at_last_wildcard,
)

__all__ = ["GitRepoSyncVerifier"]

_log = get_logger("verification.git_repo_sync")

# A fixed per-call cap, not a floor derived from the entry's own deadline:
# every `_git()` call reads one already-committed ref out of a local bare
# repo, so 30s is generous for that regardless of timeout_sec (including
# timeout_sec=0.0 for an assert-mode entry, where "evaluate once" must not
# mean "give up immediately"). This is what actually stops an unresponsive
# filesystem or git process from hanging the whole benchmark run.
_GIT_TIMEOUT_SEC = 30.0

# YAML 1.2 semantics, matching devops_bench.tasks.loader: only `true`/`false`
# parse as booleans, so `on`/`off`/`yes`/`no` in a manifest stay plain strings
# rather than being coerced.
_yaml = YAML(typ="safe")


@VERIFIERS.register("git_repo_sync")
class GitRepoSyncVerifier(BaseVerifier):
    """Assert a property of a file, or the commit state, at a git ref in a bare repo.

    Attributes:
        type: Discriminator literal, always ``"git_repo_sync"``.
        repo_path: Path to the bare repo (``~`` is expanded).
        ref: Git ref to read; default ``"HEAD"``.
        file: Path of a file within the repo tree; content is read at ``ref``.
        path: Optional JSONPath into the file's YAML documents (a list).
        op: Comparison operator; the same vocabulary as ``resource_property``.
        value: Expected value for comparison ops.
        across_matches: When ``path`` ends in a wildcard-like segment
            (``[*]``, a slice, or a filter ``[?(...)]``), quantifies over the
            ELEMENTS that segment selects, not over the flattened values the
            path happens to resolve to: a document missing the target field
            is a failing observation under ``every``, not an invisible one
            that silently drops out of the match set. ``none`` treats a
            document missing the field as trivially conforming. Unset means
            exactly one match is expected. See ``resource_property``'s
            docstring for the fail-closed rationale this mirrors.
        require_new_commit: Also require HEAD to be past the root (seed) commit.
    """

    type: Literal["git_repo_sync"] = "git_repo_sync"
    repo_path: str
    ref: str = "HEAD"
    file: str | None = None
    path: str | None = None
    op: Literal["eq", "ne", "gt", "gte", "lt", "lte", "exists", "absent", "contains", "matches"]
    value: Any = None
    across_matches: Literal["every", "none"] | None = None
    require_new_commit: bool = False

    @model_validator(mode="after")
    def _check_shape(self) -> GitRepoSyncVerifier:
        """Expand ``~`` in ``repo_path``; reject combinations meaningless at evaluation time."""
        self.repo_path = os.path.expanduser(self.repo_path)
        if self.op in _VALUE_OPS and self.file is None:
            raise ValueError(f"op {self.op!r} requires 'file'")
        if self.path is not None and self.file is None:
            raise ValueError("'path' requires a 'file' to read from the repo")
        if self.op == "absent" and self.across_matches:
            msg = "op 'absent' already asserts emptiness and does not take 'across_matches'"
            raise ValueError(msg)
        # Scoped deliberately, mirroring resource_property: pathless `exists`
        # checks the file's (or the ref's) mere presence and returns before
        # any reduction runs, so accepting `across_matches` there would
        # silently discard it. `exists` WITH a path is an ordinary per-match
        # predicate and a reduction over it is meaningful; do not widen this.
        if self.op == "exists" and not self.path and self.across_matches:
            msg = (
                "op 'exists' without 'path' applies to the file/ref itself "
                "and does not take 'across_matches'"
            )
            raise ValueError(msg)
        if self.op in _VALUE_OPS and self.value is None:
            raise ValueError(f"op {self.op!r} requires 'value'")
        if self.path is not None:
            try:
                _compile(self.path)
            except JSONPathError as exc:
                raise ValueError(f"invalid JSONPath {self.path!r}: {exc}") from exc
        # Footgun guard: `_check` resolves the seed commit before it reads
        # any file, so a transient `git rev-list` failure would short-circuit
        # to `"error"` before a content assertion in the same check ever ran,
        # masking what would otherwise have been an observable pass or fail.
        # Reject the combination rather than let it silently mask a result;
        # the author should split it into two `git_repo_sync` entries.
        if self.require_new_commit and self.file is not None:
            msg = (
                "'require_new_commit' does not combine with a content assertion "
                "('file') in the same check: a transient git failure while "
                "resolving the seed commit would mask the content assertion's "
                "own pass/fail; split them into two git_repo_sync entries"
            )
            raise ValueError(msg)
        return self

    def verify(self, timeout_sec: float) -> VerificationResult:
        """Poll the git assertion to a result."""
        return self._poll_to_result(self._check, timeout_sec)

    def _git(self, *args: str) -> str:
        return run(["git", "-C", self.repo_path, *args], timeout=_GIT_TIMEOUT_SEC).stdout

    def _check(self) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        """One evaluation pass: resolve ``ref``, optionally check for a new commit, compare."""
        try:
            head = self._git("rev-parse", self.ref).strip()
        except SubprocessError as exc:
            # Could not even resolve where to look: the repo itself was never
            # observed, so this is an error, not an observed violation.
            _log.debug("git rev-parse failed for %s:%s: %s", self.repo_path, self.ref, exc)
            return (
                "error",
                f"git ref not resolvable at {self.repo_path}:{self.ref}: "
                f"{(exc.stderr or '').strip()}",
                None,
            )
        except Exception as exc:  # noqa: BLE001 - a git failure is a check error, never raise
            _log.debug("unexpected git error resolving %s:%s: %s", self.repo_path, self.ref, exc)
            return "error", f"unexpected git error: {exc}", None

        if self.require_new_commit:
            try:
                roots = self._git("rev-list", "--max-parents=0", self.ref).split()
            except SubprocessError as exc:
                _log.debug("git rev-list failed for %s:%s: %s", self.repo_path, self.ref, exc)
                return (
                    "error",
                    f"could not determine the seed root for {self.repo_path}:{self.ref}: "
                    f"{(exc.stderr or '').strip()}",
                    {"sha": head},
                )
            except Exception as exc:  # noqa: BLE001 - a git failure is a check error, never raise
                _log.debug(
                    "unexpected git error resolving the seed root for %s:%s: %s",
                    self.repo_path,
                    self.ref,
                    exc,
                )
                return (
                    "error",
                    f"unexpected git error resolving the seed root: {exc}",
                    {"sha": head},
                )
            if head in roots:
                return "fail", f"no new commit since the seed root ({head[:8]})", {"sha": head}

        if self.file is None:
            if self.op == "absent":
                return (
                    "fail",
                    f"repo ref present at {self.ref} (absent not satisfied)",
                    {"sha": head},
                )
            return "pass", f"repo at {self.ref} ({head[:8]})", {"sha": head}

        try:
            content = self._git("show", f"{self.ref}:{self.file}")
        except SubprocessError as exc:
            # The ref itself resolved above, so a missing file at that ref is
            # an observed fact about the repo's contents, not a check error.
            if self.op == "absent" and self.path is None:
                return "pass", f"{self.file} absent at {self.ref}", {"sha": head}
            return (
                "fail",
                f"{self.file} not found at {self.ref}: {(exc.stderr or '').strip()}",
                {"sha": head},
            )
        except Exception as exc:  # noqa: BLE001 - a git failure is a check error, never raise
            _log.debug("unexpected git error reading %s:%s: %s", self.repo_path, self.ref, exc)
            return "error", f"unexpected git error reading {self.file}: {exc}", {"sha": head}

        if self.path is None:
            if self.op == "exists":
                return "pass", f"{self.file} exists at {self.ref}", {"sha": head}
            if self.op == "absent":
                return "fail", f"{self.file} present at {self.ref}", {"sha": head}
            ok, why = _apply_op(self.op, content, self.value)
            status: VerificationStatus = "pass" if ok else "fail"
            return status, f"{self.file}: {why}", {"sha": head}

        try:
            docs = [doc for doc in _yaml.load_all(content) if doc is not None]
        except YAMLError as exc:
            return "fail", f"failed to parse YAML in {self.file}: {exc}", {"sha": head}

        assert self.path is not None  # guaranteed by _check_shape at this point
        compiled = _compile(self.path)

        # `absent` never carries across_matches (rejected in _check_shape), so
        # this only ever fires for a genuine element-wise reduction; `absent`
        # and the plain exactly-one path below always take the value-wise
        # `flat` branch further down.
        wildcard_split = (
            _split_at_last_wildcard(compiled) if self.across_matches is not None else None
        )
        if wildcard_split is not None:
            prefix, suffix = wildcard_split
            evaluations = self._evaluate_across_elements(prefix, suffix, docs)
            raw = {"sha": head, "path_matches": len(evaluations)}
            if not evaluations:
                if self.across_matches == "none":
                    return (
                        "pass",
                        f"path {self.path!r} resolved to nothing in {self.file} at {self.ref}, "
                        "satisfying across_matches='none'",
                        raw,
                    )
                # Deliberately fail closed rather than vacuously true: an
                # unobservable predicate must not read as a satisfied one.
                return (
                    "fail",
                    f"path {self.path!r} did not resolve in {self.file} at {self.ref}",
                    raw,
                )
            success = all(ok for ok, _ in evaluations)
            detail = "; ".join(reason for _, reason in evaluations)
            reason = f"across_matches={self.across_matches}: {detail}"
            return ("pass" if success else "fail"), reason, raw

        found = compiled.find(docs)
        flat: list[tuple[str, Any]] = [
            (_render_path(match.full_path), match.value) for match in found
        ]
        raw: dict[str, Any] = {"sha": head, "path_matches": len(flat)}

        if self.op == "absent":
            if flat:
                sample = [value for _, value in flat[:5]]
                return (
                    "fail",
                    f"path {self.path!r} resolved to {len(flat)} value(s) in {self.file}, "
                    f"expected none (e.g. {sample})",
                    raw,
                )
            return (
                "pass",
                f"path {self.path!r} resolved to nothing in {self.file} at {self.ref}",
                raw,
            )

        if not flat:
            if self.across_matches == "none":
                return (
                    "pass",
                    f"path {self.path!r} resolved to nothing in {self.file} at {self.ref}, "
                    "satisfying across_matches='none'",
                    raw,
                )
            # Deliberately fail closed rather than vacuously true: an
            # unobservable predicate must not read as a satisfied one.
            return (
                "fail",
                f"path {self.path!r} did not resolve in {self.file} at {self.ref}",
                raw,
            )

        if len(flat) > 1 and self.across_matches is None:
            flat_paths = [path for path, _ in flat]
            flat_values = [value for _, value in flat]
            reason = (
                f"path {self.path!r} resolved to {len(flat)} value(s) in {self.file} at "
                f"{flat_paths} ({flat_values}); set 'across_matches' to 'every' or 'none' to "
                "apply the check across all of them"
            )
            return "fail", reason, raw

        results = [self._apply_check(value) for _, value in flat]
        if self.across_matches == "every":
            success = all(ok for ok, _ in results)
        elif self.across_matches == "none":
            success = not any(ok for ok, _ in results)
        else:
            (success, _) = results[0]  # only reachable when len(flat) == 1

        detail = "; ".join(
            f"{path}: {why}" for (path, _value), (_ok, why) in zip(flat, results, strict=True)
        )
        reason = (
            f"across_matches={self.across_matches}: {detail}" if self.across_matches else detail
        )
        status = "pass" if success else "fail"
        return status, reason, raw

    def _apply_check(self, value: Any) -> tuple[bool, str]:
        """Apply the operator to one resolved value."""
        if self.op == "exists":
            return True, f"path {self.path!r} resolved to {value!r}"
        return _apply_op(self.op, value, self.value)

    def _evaluate_across_elements(
        self,
        prefix: Any,
        suffix: Any,
        docs: list[Any],
    ) -> list[tuple[bool, str]]:
        """Quantify ``across_matches`` over ``prefix``'s elements, not ``suffix``'s values.

        Mirrors ``resource_property._evaluate_across_elements``: for every
        element ``prefix`` selects out of ``docs``, resolve ``suffix``
        relative to that element. ``every``: an element that does not
        resolve ``suffix`` FAILS outright; every resolved value must also
        satisfy ``op``. ``none``: an element that does not resolve
        ``suffix`` trivially conforms; no resolved value may satisfy ``op``.
        Returns one ``(ok, reason)`` pair per element, named by its jsonpath
        ``full_path`` in ``file`` at ``ref`` so an element-wise failure is
        never invisible.
        """
        suffix_str = _render_path(suffix)
        evaluations: list[tuple[bool, str]] = []
        for element in prefix.find(docs):
            label = f"{_render_path(element.full_path)} in {self.file} at {self.ref}"
            resolved = [match.value for match in suffix.find(element.value)]
            if not resolved:
                if self.across_matches == "every":
                    evaluations.append((False, f"{label} did not resolve {suffix_str}"))
                else:
                    evaluations.append(
                        (True, f"{label} did not resolve {suffix_str} (trivially conforms)")
                    )
                continue
            op_results = [self._apply_check(value) for value in resolved]
            if self.across_matches == "every":
                ok = all(result for result, _ in op_results)
            else:
                ok = not any(result for result, _ in op_results)
            detail = "; ".join(why for _, why in op_results)
            evaluations.append((ok, f"{label}: {detail}"))
        return evaluations
