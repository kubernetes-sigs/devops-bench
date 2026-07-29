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

"""Assert a JSONPath property of one or more Kubernetes objects.

This is the general-purpose leaf verifier. Where ``pod_healthy`` and
``scaling_complete`` each answer one fixed question, this one reads any field of
any resource and compares it, which is what most task objectives actually need.

JSONPath rather than a dotted path because filter predicates make checks
order-independent. ``containers[?(@.name="web")].image`` keeps meaning the same
thing when someone injects a sidecar ahead of the app container, where
``containers[0].image`` would silently start checking the wrong one.

``eq``/``ne`` only coerce both sides to numbers when there is a concrete
numeric signal: a non-bool int/float on either side, or a Kubernetes quantity
string (a numeric stem plus a known suffix, e.g. ``"100m"`` or ``"1Gi"``) on
either side. Two plain strings compare with raw ``==`` instead, because a
version string like an image tag is not a quantity: ``"1.20" == "1.2"`` must
stay false even though ``1.20 == 1.2`` as floats. Ordering ops (``gt`` /
``gte`` / ``lt`` / ``lte``) always coerce, since they are meaningless for
anything but numbers.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Literal

from jsonpath_ng.exceptions import JSONPathError as _JsonPathError
from jsonpath_ng.ext import parse as _jsonpath_parse
from jsonpath_ng.ext.filter import Filter as _JsonPathFilter
from jsonpath_ng.jsonpath import Child as _JsonPathChild
from jsonpath_ng.jsonpath import JSONPath as _JsonPath
from jsonpath_ng.jsonpath import Slice as _JsonPathSlice
from jsonpath_ng.jsonpath import This as _JsonPathThis
from pydantic import model_validator

from devops_bench.k8s import get_resource
from devops_bench.verification.base import (
    VERIFIERS,
    BaseVerifier,
    VerificationResult,
    VerificationStatus,
    single_call_timeout,
)

__all__ = ["ResourcePropertyVerifier", "to_number"]

_BINARY_SUFFIXES: dict[str, float] = {
    "Ki": 2**10,
    "Mi": 2**20,
    "Gi": 2**30,
    "Ti": 2**40,
    "Pi": 2**50,
    "Ei": 2**60,
}

_DECIMAL_SUFFIXES: dict[str, float] = {
    "n": 1e-9,
    "u": 1e-6,
    "m": 1e-3,
    "k": 1e3,
    "M": 1e6,
    "G": 1e9,
    "T": 1e12,
    "P": 1e15,
    "E": 1e18,
}

_ORDERING_OPS = ("gt", "gte", "lt", "lte")
_VALUE_OPS = ("eq", "ne", "gt", "gte", "lt", "lte", "contains", "matches")
_SET_OPS = ("exists", "absent")


def to_number(value: Any) -> float | None:
    """Coerce a scalar or Kubernetes quantity string to a float.

    Kubernetes writes resource values as suffixed strings, so ``"192Mi"`` and
    ``"1Gi"`` must compare by magnitude rather than lexically. Returns ``None``
    when the value is not a quantity, which callers treat as "not comparable".
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    for suffix, factor in _BINARY_SUFFIXES.items():
        if text.endswith(suffix):
            try:
                return float(text[: -len(suffix)]) * factor
            except ValueError:
                return None

    factor = _DECIMAL_SUFFIXES.get(text[-1])
    if factor is not None:
        try:
            return float(text[:-1]) * factor
        except ValueError:
            return None

    try:
        return float(text)
    except ValueError:
        return None


def _is_quantity_string(value: Any) -> bool:
    """True when ``value`` is a string carrying a Kubernetes quantity suffix.

    Reuses the same suffix tables :func:`to_number` parses against, but only
    the suffixed forms: a bare numeric string like ``"1.2"`` does not count,
    since that is exactly the version-string shape that must not coerce.
    """
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False

    for suffix in _BINARY_SUFFIXES:
        if text.endswith(suffix):
            try:
                float(text[: -len(suffix)])
                return True
            except ValueError:
                return False

    if text[-1] in _DECIMAL_SUFFIXES:
        try:
            float(text[:-1])
            return True
        except ValueError:
            return False

    return False


def _apply_op(op: str, actual: Any, expected: Any) -> tuple[bool, str]:
    """Apply one comparison operator, returning the verdict and a reason."""
    if op in _ORDERING_OPS:
        left = to_number(actual)
        right = to_number(expected)
        if left is None or right is None:
            return False, f"op {op!r} needs numbers, got {actual!r} and {expected!r}"
        outcome = {
            "gt": left > right,
            "gte": left >= right,
            "lt": left < right,
            "lte": left <= right,
        }[op]
        return outcome, f"{actual!r} {op} {expected!r} is {outcome}"

    if op in ("eq", "ne"):
        # Coerce only on a concrete numeric signal (see the module docstring):
        # a non-bool int/float on either side, or a quantity-suffixed string
        # on either side. Two plain strings (e.g. image tags) compare raw, so
        # a version string never reads as a float.
        numeric_side = (isinstance(actual, int | float) and not isinstance(actual, bool)) or (
            isinstance(expected, int | float) and not isinstance(expected, bool)
        )
        should_coerce = numeric_side or _is_quantity_string(actual) or _is_quantity_string(expected)
        if should_coerce:
            left = to_number(actual)
            right = to_number(expected)
            equal = left == right if left is not None and right is not None else actual == expected
        else:
            equal = actual == expected
        outcome = equal if op == "eq" else not equal
        return outcome, f"{actual!r} {op} {expected!r} is {outcome}"

    if op == "contains":
        try:
            outcome = expected in actual
        except TypeError:
            return False, f"op 'contains' does not apply to {type(actual).__name__}"
        return outcome, f"{actual!r} contains {expected!r} is {outcome}"

    if op == "matches":
        if not isinstance(actual, str):
            return False, f"op 'matches' needs a string, got {type(actual).__name__}"
        try:
            pattern = _compile_regex(str(expected))
        except re.error as exc:
            # Belt, not the primary defense: _check_shape already rejects a
            # malformed pattern at load time, so this is near-unreachable.
            # Failing with a named reason rather than raising keeps a task
            # authoring bug from crashing verify() outright.
            return False, f"op 'matches' has an invalid pattern {expected!r}: {exc}"
        outcome = pattern.search(actual) is not None
        return outcome, f"{actual!r} matches {expected!r} is {outcome}"

    return False, f"unhandled op {op!r}"


@lru_cache(maxsize=256)
def _compile(path: str) -> Any:
    """Compile a JSONPath expression once and reuse it across poll iterations."""
    return _jsonpath_parse(path)


@lru_cache(maxsize=256)
def _compile_regex(pattern: str) -> re.Pattern[str]:
    """Compile a 'matches' pattern once and reuse it across poll iterations.

    Also the entry point ``_check_shape`` calls at load time, so a malformed
    pattern surfaces as one ``re.error`` -> ``ValueError`` translation shared
    by both the parse-time check and the runtime belt in ``_apply_op``.
    """
    return re.compile(pattern)


def _chain_suffix(parts: list[_JsonPath]) -> _JsonPath:
    """Rebuild a suffix expression from AST nodes collected right-to-left."""
    if not parts:
        return _JsonPathThis()
    result = parts[0]
    for part in parts[1:]:
        result = _JsonPathChild(result, part)
    return result


def _split_at_last_wildcard(expr: _JsonPath) -> tuple[_JsonPath, _JsonPath] | None:
    """Split a compiled JSONPath at its last wildcard-like segment (``[*]``, a
    slice, or a filter ``[?(...)]``).

    ``across_matches`` quantifies over the ELEMENTS that segment selects, not
    over the flattened values the whole path resolves to: a container missing
    the target field is a real element that failed to resolve the suffix, not
    one that silently drops out of the match set. Returns ``(prefix, suffix)``
    where ``prefix`` selects the elements (e.g. ``containers[*]``) and
    ``suffix`` resolves relative to each element (e.g.
    ``resources.limits.cpu``; ``This()`` when the path ends AT the wildcard).
    Returns ``None`` when the path has no such segment, in which case the
    caller keeps the pre-existing value-wise behaviour.
    """
    tail: list[_JsonPath] = []
    node = expr
    while isinstance(node, _JsonPathChild):
        if isinstance(node.right, _JsonPathSlice | _JsonPathFilter):
            return node, _chain_suffix(list(reversed(tail)))
        tail.append(node.right)
        node = node.left
    return None


def _render_path(node: _JsonPath) -> str:
    """Render a jsonpath-ng AST node as a dotted path, without the nested
    parentheses ``Child.__str__`` wraps around every level.

    ``str(Child(Child(Fields('a'), Fields('b')), Fields('c')))`` reads
    ``((a.b).c)``, which is correct but unreadable in a failure reason. This
    walks the same ``Child`` structure and joins with plain dots instead, so
    ``spec.template.spec.containers[*].resources.limits.cpu`` reasons stay
    legible (e.g. ``spec.template.spec.containers.[1]``). Falls back to the
    node's own ``__str__`` for any non-``Child`` node (``Fields``, ``Index``,
    ``Slice``, ``Filter``, ``This``), which already render without parens.
    """
    if isinstance(node, _JsonPathChild):
        return f"{_render_path(node.left)}.{_render_path(node.right)}"
    return str(node)


def _object_name(obj: Any) -> str:
    """Best-effort display name for a matched object."""
    if isinstance(obj, dict):
        metadata = obj.get("metadata")
        if isinstance(metadata, dict):
            name = metadata.get("name")
            if isinstance(name, str):
                return name
    return "<unnamed>"


@VERIFIERS.register("resource_property")
class ResourcePropertyVerifier(BaseVerifier):
    """Compare a JSONPath property of matched Kubernetes objects.

    The field is ``resource_name`` rather than ``name`` because ``BaseVerifier``
    already uses ``name`` for the check's own label, which is what lands on
    :attr:`VerificationResult.name`.

    When ``path`` ends in a wildcard-like segment (``[*]``, a slice, or a
    filter ``[?(...)]``) and ``across_matches`` is set, quantification is over
    the ELEMENTS that segment selects, not over the flattened values the path
    happens to resolve to: a container missing the target field is a failing
    observation under ``every``, not an invisible one that silently drops out
    of the match set.
    """

    type: Literal["resource_property"]
    kind: str
    resource_name: str | None = None
    selector: str | None = None
    namespace: str | None = None
    path: str | None = None
    op: Literal["eq", "ne", "gt", "gte", "lt", "lte", "exists", "absent", "contains", "matches"]
    value: Any = None
    # Quantifies over the elements of path's LAST wildcard-like segment
    # (`[*]`, a slice, or a filter), not over the flattened values the path
    # resolves to. `every`: every element must resolve the suffix and every
    # resolved value must satisfy `op`; an element that does not resolve the
    # suffix FAILS. `none`: no element may resolve a value satisfying `op`;
    # an element missing the field trivially conforms. When path has no such
    # segment, or across_matches is unset, this reduces to the plain
    # exactly-one-match behaviour below.
    across_matches: Literal["every", "none"] | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> ResourcePropertyVerifier:
        """Reject combinations that cannot mean anything at evaluation time."""
        if self.resource_name and self.selector:
            msg = "resource_property takes 'resource_name' or 'selector', not both"
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
        # Scoped deliberately: `exists` WITH a path is an ordinary per-object
        # predicate and a reduction over it is meaningful. Only pathless
        # `exists` returns before any reduction runs (step 3 of _check), so
        # accepting `across_matches` there would silently discard it. Do not
        # widen this to cover `exists` WITH a path.
        if self.op == "exists" and not self.path and self.across_matches:
            msg = (
                "op 'exists' without 'path' applies to the matched object set "
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

    def _check(self, timeout_sec: float) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        """One evaluation pass: fetch, resolve matches, apply the operator."""
        try:
            payload = get_resource(
                self.kind,
                self.resource_name,
                selector=self.selector,
                namespace=self.namespace,
                kubeconfig=self.kubeconfig,
                timeout=single_call_timeout(timeout_sec),
            )
        except Exception as exc:  # noqa: BLE001 - a kubectl failure is a check error
            return "error", f"kubectl get {self.kind} failed: {exc}", None

        items = payload.get("items")
        objects = items if isinstance(items, list) else [payload]
        names = [_object_name(obj) for obj in objects]
        raw: dict[str, Any] = {"matched": len(objects), "names": names}

        if self.op == "absent" and self.path is None:
            if objects:
                return "fail", f"{len(objects)} matching {self.kind} found: {names}", raw
            return "pass", f"no matching {self.kind}", raw

        # Fail closed above the flattening. This is what keeps "zero objects
        # existed" distinct from "objects existed but the path matched
        # nothing": the former can never be observed, the latter is a real
        # answer.
        if not objects:
            return "fail", f"no {self.kind} matched", raw

        if self.op == "exists" and self.path is None:
            return "pass", f"{len(objects)} matching {self.kind} found: {names}", raw

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
            evaluations = self._evaluate_across_elements(prefix, suffix, names, objects)
            raw["path_matches"] = len(evaluations)
            if not evaluations:
                if self.across_matches == "none":
                    return (
                        "pass",
                        f"path {self.path!r} resolved to nothing in {len(objects)} "
                        "matched object(s), satisfying across_matches='none'",
                        raw,
                    )
                # Deliberately fail closed rather than vacuously true: an
                # unobservable predicate must not read as a satisfied one.
                return (
                    "fail",
                    f"path {self.path!r} did not resolve in any of {len(objects)} matched "
                    "object(s)",
                    raw,
                )
            success = all(ok for ok, _ in evaluations)
            detail = "; ".join(reason for _, reason in evaluations)
            reason = f"across_matches={self.across_matches}: {detail}"
            return ("pass" if success else "fail"), reason, raw

        flat: list[tuple[str, Any]] = [
            (name, match.value)
            for name, obj in zip(names, objects, strict=True)
            for match in compiled.find(obj)
        ]
        raw["path_matches"] = len(flat)

        if self.op == "absent":
            if flat:
                sample = [value for _, value in flat[:5]]
                return (
                    "fail",
                    f"path {self.path!r} resolved to {len(flat)} value(s), expected "
                    f"none (e.g. {sample})",
                    raw,
                )
            return (
                "pass",
                f"path {self.path!r} resolved to nothing in {len(objects)} object(s)",
                raw,
            )

        if not flat:
            if self.across_matches == "none":
                return (
                    "pass",
                    f"path {self.path!r} resolved to nothing in {len(objects)} "
                    "matched object(s), satisfying across_matches='none'",
                    raw,
                )
            # Deliberately fail closed rather than vacuously true: an
            # unobservable predicate must not read as a satisfied one.
            return (
                "fail",
                f"path {self.path!r} did not resolve in any of {len(objects)} matched object(s)",
                raw,
            )

        if len(flat) > 1 and self.across_matches is None:
            flat_names = [name for name, _ in flat]
            flat_values = [value for _, value in flat]
            reason = (
                f"path {self.path!r} resolved to {len(flat)} value(s) across "
                f"{flat_names} ({flat_values}); set 'across_matches' to 'every' "
                "or 'none' to apply the check across all of them"
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
            f"{name}: {why}" for (name, _value), (_ok, why) in zip(flat, results, strict=True)
        )
        reason = (
            f"across_matches={self.across_matches}: {detail}" if self.across_matches else detail
        )
        return ("pass" if success else "fail"), reason, raw

    def _apply_check(self, value: Any) -> tuple[bool, str]:
        """Apply the operator to one resolved value."""
        if self.op == "exists":
            return True, f"path {self.path!r} resolved to {value!r}"
        return _apply_op(self.op, value, self.value)

    def _evaluate_across_elements(
        self,
        prefix: _JsonPath,
        suffix: _JsonPath,
        names: list[str],
        objects: list[Any],
    ) -> list[tuple[bool, str]]:
        """Quantify ``across_matches`` over ``prefix``'s elements, not ``suffix``'s values.

        For every element ``prefix`` selects in every matched object, resolve
        ``suffix`` relative to that element. ``every``: an element that does
        not resolve ``suffix`` FAILS outright; every resolved value must also
        satisfy ``op``. ``none``: an element that does not resolve ``suffix``
        trivially conforms; no resolved value may satisfy ``op``. Returns one
        ``(ok, reason)`` pair per element, naming it by owning object and
        jsonpath ``full_path`` so an element-wise failure is never invisible.
        """
        suffix_str = _render_path(suffix)
        evaluations: list[tuple[bool, str]] = []
        for name, obj in zip(names, objects, strict=True):
            for element in prefix.find(obj):
                label = f"{name}: {_render_path(element.full_path)}"
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
