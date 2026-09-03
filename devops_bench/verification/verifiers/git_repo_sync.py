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

"""Assert a JSONPath property of a YAML file inside a git repository.

Several tasks are GitOps-shaped: the repository is the source of truth and the
prompt asks the agent to keep it in sync with the cluster. Every other verifier
reads the cluster, so the repository half of those tasks is invisible to
verification — an agent that applies manifests directly and never commits scores
exactly like one that did the whole job. This closes that gap.

The comparison semantics are deliberately identical to ``resource_property``:
same operators, same ``across_matches`` element-wise quantification, same
quantity coercion. Only the source of the document differs — ``git show
<ref>:<file>`` instead of ``kubectl get``. Reusing that module's helpers is
intentional; a second, subtly different implementation of "does this JSONPath
satisfy this operator" is how two checks that read identically start disagreeing.

The document root is a **list**, because Kubernetes manifests are multi-document
YAML. A path therefore starts at the document array, e.g.

    $[?(@.kind=='Ingress')].apiVersion

Repositories are read at a ref (``HEAD`` by default) rather than from a working
tree, so the check works against the bare repo the fixtures seed and never
depends on the agent having left a clone behind.

Failure modes are closed, not open. A missing repository, a missing file at the
ref, and a path that resolves to nothing all FAIL rather than vacuously passing,
because each of them is indistinguishable from the agent having deleted the
thing the check exists to inspect. The one exception is documented on
``across_matches: none``, which matches ``resource_property``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import model_validator

from devops_bench.verification.base import (
    VERIFIERS,
    BaseVerifier,
    VerificationResult,
    VerificationStatus,
    single_call_timeout,
)

# Shared with resource_property on purpose — see the module docstring. These are
# private there because they are not part of that verifier's public surface, not
# because they are unsafe to share; the intended end state is to lift them into
# a `verification/pathcheck.py` that both import, which is a mechanical refactor
# left out of this change to keep it reviewable.
from devops_bench.verification.verifiers.resource_property import (
    _apply_op,
    _compile,
    _render_path,
    _split_at_last_wildcard,
)

__all__ = ["GitRepoSyncVerifier"]

_VALUE_OPS = ("eq", "ne", "gt", "gte", "lt", "lte", "contains", "matches")
_SET_OPS = ("exists", "absent")


class _GitError(RuntimeError):
    """A git invocation failed for a reason other than 'the thing is not there'."""


def _git(repo: Path, args: list[str], timeout: float) -> str:
    """Run one git command against ``repo`` and return stdout.

    ``safe.bareRepository=all`` mirrors what the fixture seed scripts use: the
    repositories these checks read are bare, and git refuses to operate on a
    bare repo by path under its default ``safe.bareRepository`` handling in
    some configurations. Setting it per-invocation keeps the verifier from
    depending on the host's global git config.
    """
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-c", "safe.bareRepository=all", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise _GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


@VERIFIERS.register("git_repo_sync")
class GitRepoSyncVerifier(BaseVerifier):
    """Compare a JSONPath property of a YAML file committed in a git repository.

    Attributes:
        repo_path: Path to the repository, bare or not. A leading ``~`` is
            expanded against the harness user's home, which is where the
            fixtures create it (``main.tf`` passes ``pathexpand(...)`` to the
            seed script, so both sides resolve the same way).
        ref: Git ref to read. Defaults to ``HEAD``.
        file: Path of the file within the repository. Omit only when the check
            is a bare ``exists``/``absent`` on the repository itself.
        path: JSONPath into the parsed document list. Required for value ops.
        op: Comparison operator, same set as ``resource_property``.
        value: Right-hand side for value ops.
        across_matches: Element-wise quantifier, same semantics as
            ``resource_property`` — see that module for the details.
        require_new_commit: When true, additionally require that ``ref`` is not
            the repository's root commit. The fixtures seed exactly one commit,
            so this is the check for "the agent actually committed its work"
            rather than leaving the repository untouched. It is ANDed with the
            path check: both must hold.
    """

    type: Literal["git_repo_sync"]
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
        """Reject combinations that cannot mean anything at evaluation time."""
        if self.op not in _SET_OPS and not self.path:
            raise ValueError(f"op {self.op!r} requires 'path'")
        if self.op == "absent" and not self.path:
            # Every pathless route through _check returns fail: a missing repo
            # or a missing file already fails closed, and a present one is
            # reported as present. Unlike resource_property, where an empty
            # matched-object set is itself an observation, a pathless `absent`
            # has nothing here to observe. Reject it at load time rather than
            # handing the author a check that can only ever fail.
            msg = "op 'absent' requires 'path'; a missing repository or file already fails closed"
            raise ValueError(msg)
        if self.path and not self.file:
            raise ValueError("'path' requires 'file' to read it from")
        if self.path is not None:
            try:
                _compile(self.path)
            except Exception as exc:  # noqa: BLE001 - any parse failure is authoring error
                msg = f"path {self.path!r} is not a valid JSONPath: {exc}"
                raise ValueError(msg) from exc
        if self.op == "absent" and self.across_matches:
            msg = "op 'absent' already asserts emptiness and does not take 'across_matches'"
            raise ValueError(msg)
        if self.op == "exists" and not self.path and self.across_matches:
            msg = "op 'exists' without 'path' applies to the file itself, not to elements"
            raise ValueError(msg)
        if self.op in _VALUE_OPS and self.value is None:
            raise ValueError(f"op {self.op!r} requires 'value'")
        return self

    def verify(self, timeout_sec: float) -> VerificationResult:
        """Poll the repository until the property holds or the budget runs out."""
        return self._poll_to_result(lambda: self._check(timeout_sec), timeout_sec)

    def _check(self, timeout_sec: float) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        """One evaluation pass: locate the repo, read the file, apply the operator."""
        call_timeout = single_call_timeout(timeout_sec)
        repo = Path(os.path.expanduser(self.repo_path))
        raw: dict[str, Any] = {"repo_path": str(repo), "ref": self.ref, "file": self.file}

        if not repo.exists():
            # Observed absence, not an infrastructure error: the fixture created
            # this repo before the agent started, so it being gone is a result.
            return "fail", f"repository {repo} does not exist", raw

        try:
            head = _git(repo, ["rev-parse", self.ref], call_timeout).strip()
            raw["head"] = head
        except _GitError as exc:
            return "error", str(exc), raw
        except (OSError, subprocess.SubprocessError) as exc:
            return "error", f"could not run git in {repo}: {exc}", raw

        new_commit_reason = ""
        if self.require_new_commit:
            try:
                roots = _git(repo, ["rev-list", "--max-parents=0", self.ref], call_timeout).split()
            except (_GitError, OSError, subprocess.SubprocessError) as exc:
                return "error", f"could not read root commit of {repo}: {exc}", raw
            raw["root_commits"] = roots
            if head in roots:
                return (
                    "fail",
                    f"{self.ref} is still the repository's root commit ({head[:8]}) — "
                    "nothing was committed",
                    raw,
                )
            new_commit_reason = f"{self.ref} is past the root commit ({head[:8]}); "

        if self.file is None:
            # Bare repository-level assertion; existence was settled above.
            if self.op == "absent":
                return "fail", f"repository {repo} exists", raw
            return "pass", f"{new_commit_reason}repository {repo} exists", raw

        try:
            content = _git(repo, ["show", f"{self.ref}:{self.file}"], call_timeout)
        except _GitError as exc:
            # A file missing at the ref fails closed even for `absent`. "The
            # agent deleted the manifest" must not read the same as "the agent
            # removed the offending field from it" — a recoverable safeguard on
            # these tasks exists precisely to forbid the former.
            raw["git_error"] = str(exc)
            return "fail", f"{self.file!r} is not present at {self.ref} in {repo}", raw
        except (OSError, subprocess.SubprocessError) as exc:
            return "error", f"could not read {self.file!r} from {repo}: {exc}", raw

        try:
            docs = [d for d in yaml.safe_load_all(content) if d is not None]
        except yaml.YAMLError as exc:
            # The agent left the file unparseable. That is an observation about
            # the repository's state, not a harness failure.
            return "fail", f"{self.file!r} at {self.ref} is not valid YAML: {exc}", raw
        raw["documents"] = len(docs)

        if self.path is None:
            if self.op == "absent":
                return "fail", f"{self.file!r} exists at {self.ref}", raw
            return "pass", f"{new_commit_reason}{self.file!r} exists at {self.ref}", raw

        return self._evaluate_path(docs, raw, new_commit_reason)

    def _evaluate_path(
        self,
        docs: list[Any],
        raw: dict[str, Any],
        prefix_reason: str,
    ) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        """Apply ``path``/``op``/``across_matches`` to the parsed document list.

        Mirrors ``ResourcePropertyVerifier._check`` from the point where it has
        its objects, with the document list standing in for the matched
        resources: ``across_matches`` quantifies over the ELEMENTS the path's
        last wildcard-like segment selects, so a document that fails to resolve
        the suffix is a failing observation under ``every`` rather than one that
        silently drops out.
        """
        assert self.path is not None  # guaranteed by _check_shape
        compiled = _compile(self.path)

        wildcard_split = (
            _split_at_last_wildcard(compiled) if self.across_matches is not None else None
        )
        if wildcard_split is not None:
            prefix, suffix = wildcard_split
            suffix_str = _render_path(suffix)
            evaluations: list[tuple[bool, str]] = []
            for element in prefix.find(docs):
                label = _render_path(element.full_path)
                resolved = [m.value for m in suffix.find(element.value)]
                if not resolved:
                    if self.across_matches == "every":
                        evaluations.append((False, f"{label} did not resolve {suffix_str}"))
                    else:
                        evaluations.append(
                            (True, f"{label} did not resolve {suffix_str} (trivially conforms)")
                        )
                    continue
                results = [self._apply_check(v) for v in resolved]
                ok = (
                    all(r for r, _ in results)
                    if self.across_matches == "every"
                    else not any(r for r, _ in results)
                )
                evaluations.append((ok, f"{label}: {'; '.join(w for _, w in results)}"))

            raw["path_matches"] = len(evaluations)
            if not evaluations:
                if self.across_matches == "none":
                    return (
                        "pass",
                        f"{prefix_reason}path {self.path!r} resolved to nothing in "
                        f"{self.file!r}, satisfying across_matches='none'",
                        raw,
                    )
                return (
                    "fail",
                    f"path {self.path!r} did not resolve in {self.file!r}",
                    raw,
                )
            success = all(ok for ok, _ in evaluations)
            detail = "; ".join(reason for _, reason in evaluations)
            return (
                ("pass" if success else "fail"),
                f"{prefix_reason}across_matches={self.across_matches}: {detail}",
                raw,
            )

        flat = [m.value for m in compiled.find(docs)]
        raw["path_matches"] = len(flat)

        if self.op == "absent":
            if flat:
                return (
                    "fail",
                    f"path {self.path!r} resolved to {len(flat)} value(s) in "
                    f"{self.file!r}, expected none (e.g. {flat[:5]})",
                    raw,
                )
            return (
                "pass",
                f"{prefix_reason}path {self.path!r} resolved to nothing in {self.file!r}",
                raw,
            )

        if not flat:
            if self.across_matches == "none":
                return (
                    "pass",
                    f"{prefix_reason}path {self.path!r} resolved to nothing in "
                    f"{self.file!r}, satisfying across_matches='none'",
                    raw,
                )
            return "fail", f"path {self.path!r} did not resolve in {self.file!r}", raw

        if len(flat) > 1 and self.across_matches is None:
            return (
                "fail",
                f"path {self.path!r} resolved to {len(flat)} value(s) ({flat}); set "
                "'across_matches' to 'every' or 'none' to apply the check across all of them",
                raw,
            )

        results = [self._apply_check(v) for v in flat]
        if self.across_matches == "every":
            success = all(ok for ok, _ in results)
        elif self.across_matches == "none":
            success = not any(ok for ok, _ in results)
        else:
            (success, _) = results[0]  # only reachable when len(flat) == 1

        detail = "; ".join(why for _, why in results)
        reason = (
            f"across_matches={self.across_matches}: {detail}" if self.across_matches else detail
        )
        return ("pass" if success else "fail"), f"{prefix_reason}{reason}", raw

    def _apply_check(self, value: Any) -> tuple[bool, str]:
        """Apply the operator to one resolved value."""
        if self.op == "exists":
            return True, f"path {self.path!r} resolved to {value!r}"
        return _apply_op(self.op, value, self.value)
