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

"""JSONPath/operator/quantifier logic shared by every property-comparison verifier.

Extracted out of ``resource_property.py`` so ``resource_property``,
``git_repo_sync``, and ``cloud_resource_property`` import one implementation
instead of three: a fork drifts, and these semantics (in particular the
eq/ne numeric-coercion guard and the across_matches quantifier split) are
exactly the part that must stay byte-for-byte identical across verifiers, or
the same-shaped check grades differently depending which verifier wrote it.

Nothing here is provider-specific: these functions operate on already-fetched
JSON values and a JSONPath string, never on how the object was fetched. In
particular, :func:`evaluate_matched_objects` is the full post-fetch
evaluation (flat-match computation, ``absent``/``ne`` handling, the
ambiguous-multi-match guard, the ``across_matches`` branch): once a verifier
has its own object list and name labels in hand, applying
``op``/``path``/``across_matches`` to them is identical work regardless of
whether the objects came from a kubectl selector or a single cloud fetch.
Only obtaining that object list -- and what "absent" means upstream (a
kubectl NotFound vs. a classified cloud not-found) -- is each verifier's own
concern and stays out of this module.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Literal

from jsonpath_ng.ext import parse as _jsonpath_parse
from jsonpath_ng.ext.filter import Filter as _JsonPathFilter
from jsonpath_ng.jsonpath import Child as _JsonPathChild
from jsonpath_ng.jsonpath import JSONPath as _JsonPath
from jsonpath_ng.jsonpath import Slice as _JsonPathSlice
from jsonpath_ng.jsonpath import This as _JsonPathThis

__all__ = [
    "_ORDERING_OPS",
    "_SET_OPS",
    "_VALUE_OPS",
    "_apply_op",
    "_compile",
    "_compile_regex",
    "_render_path",
    "_split_at_last_wildcard",
    "_summarize_reason",
    "apply_check",
    "evaluate_across_elements",
    "evaluate_matched_objects",
    "to_number",
]

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

# How many violating entries a selector/across_matches reason enumerates
# before it switches to "and N more". A cluster-wide selector (or a cloud
# list response) can match dozens of objects; naming every one of them
# (conforming and violating alike) produces a reason string that is unusable
# in a terminal and worse in a grading pack a human reads.
_REASON_ENUMERATION_CAP = 5


def to_number(value: Any) -> float | None:
    """Coerce a scalar or suffixed quantity string to a float.

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
    """True when ``value`` is a string carrying a quantity suffix.

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
            # Belt, not the primary defense: each verifier's own _check_shape
            # already rejects a malformed pattern at load time, so this is
            # near-unreachable. Failing with a named reason rather than
            # raising keeps a task authoring bug from crashing verify().
            return False, f"op 'matches' has an invalid pattern {expected!r}: {exc}"
        outcome = pattern.search(actual) is not None
        return outcome, f"{actual!r} matches {expected!r} is {outcome}"

    return False, f"unhandled op {op!r}"


def apply_check(op: str, expected: Any, value: Any, path: str | None) -> tuple[bool, str]:
    """Apply ``op`` to one resolved value against ``expected``.

    ``exists`` is trivially satisfied by any resolved value — the resolution
    itself is the check, and ``path`` is only threaded through to name what
    was resolved in the reason string; every other op ignores ``path`` and
    delegates to :func:`_apply_op`.
    """
    if op == "exists":
        return True, f"path {path!r} resolved to {value!r}"
    return _apply_op(op, value, expected)


@lru_cache(maxsize=256)
def _compile(path: str) -> Any:
    """Compile a JSONPath expression once and reuse it across poll iterations."""
    return _jsonpath_parse(path)


@lru_cache(maxsize=256)
def _compile_regex(pattern: str) -> re.Pattern[str]:
    """Compile a 'matches' pattern once and reuse it across poll iterations.

    Also the entry point each verifier's own ``_check_shape`` calls at load
    time, so a malformed pattern surfaces as one ``re.error`` -> ``ValueError``
    translation shared by both the parse-time check and the runtime belt in
    :func:`_apply_op`.
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


def _summarize_reason(total: int, mode: str | None, violations: list[str]) -> str:
    """Summarize a multi-object/element evaluation into a bounded reason string.

    Only the violating entries are named; the conforming majority is
    collapsed into the leading count. Even the violations are capped at
    :data:`_REASON_ENUMERATION_CAP`, with the remainder folded into an
    accurate "and N more". The full, uncapped list still lands on
    ``raw["violations"]`` for anything that needs it.
    """
    label = f"across_matches={mode}: " if mode else ""
    if not violations:
        return f"{label}checked {total}; none violate"
    shown = violations[:_REASON_ENUMERATION_CAP]
    remainder = len(violations) - len(shown)
    more = f" (and {remainder} more)" if remainder else ""
    return f"{label}checked {total}; {len(violations)} violate: {'; '.join(shown)}{more}"


def evaluate_across_elements(
    op: str,
    expected: Any,
    mode: Literal["every", "none"],
    prefix: _JsonPath,
    suffix: _JsonPath,
    names: list[str],
    objects: list[Any],
    path: str | None,
) -> list[tuple[bool, str]]:
    """Quantify ``mode`` over ``prefix``'s elements, not ``suffix``'s values.

    For every element ``prefix`` selects in every object, resolve ``suffix``
    relative to that element. ``every``: an element that does not resolve
    ``suffix`` FAILS outright; every resolved value must also satisfy ``op``.
    ``none``: an element that does not resolve ``suffix`` trivially conforms;
    no resolved value may satisfy ``op``. Returns one ``(ok, reason)`` pair
    per element, naming it by owning object and jsonpath ``full_path`` so an
    element-wise failure is never invisible. ``path`` is only threaded
    through to :func:`apply_check` for its ``exists`` reason string.
    """
    suffix_str = _render_path(suffix)
    evaluations: list[tuple[bool, str]] = []
    for name, obj in zip(names, objects, strict=True):
        for element in prefix.find(obj):
            label = f"{name}: {_render_path(element.full_path)}"
            resolved = [match.value for match in suffix.find(element.value)]
            if not resolved:
                if mode == "every":
                    evaluations.append((False, f"{label} did not resolve {suffix_str}"))
                else:
                    evaluations.append(
                        (True, f"{label} did not resolve {suffix_str} (trivially conforms)")
                    )
                continue
            op_results = [apply_check(op, expected, value, path) for value in resolved]
            ok = (
                all(r for r, _ in op_results)
                if mode == "every"
                else not any(r for r, _ in op_results)
            )
            detail = "; ".join(why for _, why in op_results)
            evaluations.append((ok, f"{label}: {detail}"))
    return evaluations


def evaluate_matched_objects(
    op: str,
    expected: Any,
    across_matches: Literal["every", "none"] | None,
    path: str | None,
    objects: list[Any],
    names: list[str],
    subject: str,
) -> tuple[Literal["pass", "fail"], str, dict[str, Any]]:
    """Apply ``op``/``path``/``across_matches`` to an already-fetched object list.

    Shared by every property-comparison verifier once it has its own objects
    in hand (a ``resource_property`` object list matched by kubectl selector
    or name; a ``cloud_resource_property`` object list from one provider
    fetch): only how the objects were obtained, and what "absent" means
    upstream, differs per verifier. ``subject`` names what the objects are,
    for reason strings (a Kubernetes ``kind`` or a cloud ``resource_type``).

    Never returns ``"error"``: an upstream fetch failure is the caller's own
    concern, resolved before this function is ever called.
    """
    raw: dict[str, Any] = {"matched": len(objects), "names": names}

    if op == "absent" and path is None:
        if objects:
            return "fail", f"{len(objects)} matching {subject} found: {names}", raw
        return "pass", f"no matching {subject}", raw

    # Fail closed above the flattening for `every` (and the plain
    # exactly-one-match case): "zero objects existed" is an unobservable
    # predicate there, not a satisfied one, so it stays a fail. `none` is
    # the exception: it asserts that no matched object violates `op`, and
    # an empty match set vacuously satisfies that (e.g. a selector for a
    # job backlog that has been fully drained, or a cloud list that legitimately
    # returned nothing).
    if not objects:
        if across_matches == "none":
            return "pass", f"no {subject} matched the selector; nothing violates", raw
        return "fail", f"no {subject} matched", raw

    if op == "exists" and path is None:
        return "pass", f"{len(objects)} matching {subject} found: {names}", raw

    assert path is not None  # guaranteed by each verifier's own _check_shape
    compiled = _compile(path)

    # `absent` never carries across_matches (rejected in each verifier's
    # _check_shape), so this only ever fires for a genuine element-wise
    # reduction; `absent` and the plain exactly-one path below always take
    # the value-wise `flat` branch further down.
    wildcard_split = _split_at_last_wildcard(compiled) if across_matches is not None else None
    if wildcard_split is not None:
        prefix, suffix = wildcard_split
        evaluations = evaluate_across_elements(
            op, expected, across_matches, prefix, suffix, names, objects, path
        )
        raw["path_matches"] = len(evaluations)
        if not evaluations:
            if across_matches == "none":
                return (
                    "pass",
                    f"path {path!r} resolved to nothing in {len(objects)} "
                    "matched object(s), satisfying across_matches='none'",
                    raw,
                )
            # Deliberately fail closed rather than vacuously true: an
            # unobservable predicate must not read as a satisfied one.
            return (
                "fail",
                f"path {path!r} did not resolve in any of {len(objects)} matched object(s)",
                raw,
            )
        success = all(ok for ok, _ in evaluations)
        violations = [why for ok, why in evaluations if not ok]
        raw["violations"] = violations
        reason = _summarize_reason(len(evaluations), across_matches, violations)
        return ("pass" if success else "fail"), reason, raw

    flat: list[tuple[str, Any]] = [
        (name, match.value)
        for name, obj in zip(names, objects, strict=True)
        for match in compiled.find(obj)
    ]
    raw["path_matches"] = len(flat)

    if op == "absent":
        if flat:
            sample = [value for _, value in flat[:5]]
            return (
                "fail",
                f"path {path!r} resolved to {len(flat)} value(s), expected none (e.g. {sample})",
                raw,
            )
        return "pass", f"path {path!r} resolved to nothing in {len(objects)} object(s)", raw

    if not flat:
        if op == "ne":
            # An absent field is not equal to anything: `ne` is a negative
            # op, so absence trivially satisfies it (unlike a positive op
            # such as `eq`, which stays fail-closed below). e.g. a ConfigMap
            # without `immutable` IS mutable.
            return (
                "pass",
                f"path {path!r} did not resolve in any of {len(objects)} matched object(s), "
                "satisfying op 'ne' (absent is not equal to the expected value)",
                raw,
            )
        if across_matches == "none":
            return (
                "pass",
                f"path {path!r} resolved to nothing in {len(objects)} matched object(s), "
                "satisfying across_matches='none'",
                raw,
            )
        # Deliberately fail closed rather than vacuously true: an
        # unobservable predicate must not read as a satisfied one.
        return (
            "fail",
            f"path {path!r} did not resolve in any of {len(objects)} matched object(s)",
            raw,
        )

    if len(flat) > 1 and across_matches is None:
        flat_names = [name for name, _ in flat]
        flat_values = [value for _, value in flat]
        reason = (
            f"path {path!r} resolved to {len(flat)} value(s) across "
            f"{flat_names} ({flat_values}); set 'across_matches' to 'every' "
            "or 'none' to apply the check across all of them"
        )
        return "fail", reason, raw

    results = [apply_check(op, expected, value, path) for _, value in flat]
    if across_matches == "every":
        # Under `every`, a violator is an element the op did NOT hold for.
        success = all(ok for ok, _ in results)
        violating = {i for i, (ok, _) in enumerate(results) if not ok}
    elif across_matches == "none":
        # Under `none`, a violator is an element the op DID hold for.
        success = not any(ok for ok, _ in results)
        violating = {i for i, (ok, _) in enumerate(results) if ok}
    else:
        (success, why) = results[0]  # only reachable when len(flat) == 1
        name, _value = flat[0]
        return ("pass" if success else "fail"), f"{name}: {why}", raw

    violations = [
        f"{name}: {why}"
        for i, ((name, _value), (_ok, why)) in enumerate(zip(flat, results, strict=True))
        if i in violating
    ]
    raw["violations"] = violations
    reason = _summarize_reason(len(flat), across_matches, violations)
    return ("pass" if success else "fail"), reason, raw
