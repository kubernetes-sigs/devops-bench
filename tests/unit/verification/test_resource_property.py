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

"""Unit tests for the resource_property verifier."""

from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from devops_bench.verification.spec import parse_node
from devops_bench.verification.verifiers.resource_property import (
    ResourcePropertyVerifier,
    _apply_op,
    to_number,
)

_GET = "devops_bench.verification.verifiers.resource_property.get_resource"


def _deployment(name: str = "web", ready: int = 2, ns: str = "shop") -> dict[str, Any]:
    return {
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": ns},
        "status": {"readyReplicas": ready},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {"name": "sidecar", "image": "proxy:1"},
                        {"name": "web", "image": "web:2"},
                    ]
                }
            }
        },
    }


def _items(*objs: dict[str, Any]) -> dict[str, Any]:
    return {"apiVersion": "v1", "kind": "List", "items": list(objs)}


def _multi_container_deployment(name: str = "web", ns: str = "shop") -> dict[str, Any]:
    """One deployment with three containers: satisfying, violating, and missing.

    ``web`` satisfies a ``cpu >= 100m`` check, ``sidecar`` violates it, and
    ``init`` declares no ``resources`` block at all, so the wildcard path
    resolves to no value for it rather than a failing one.
    """
    return {
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": ns},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {"name": "web", "resources": {"requests": {"cpu": "200m"}}},
                        {"name": "sidecar", "resources": {"requests": {"cpu": "50m"}}},
                        {"name": "init"},
                    ]
                }
            }
        },
    }


_MULTI_CONTAINER_CPU_PATH = "spec.template.spec.containers[*].resources.requests.cpu"


def _security_context_deployment(
    *run_as_non_root: bool, name: str = "web", ns: str = "shop"
) -> dict[str, Any]:
    """One deployment with one container per value of ``runAsNonRoot``."""
    return {
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": ns},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {"name": f"c{i}", "securityContext": {"runAsNonRoot": flag}}
                        for i, flag in enumerate(run_as_non_root)
                    ]
                }
            }
        },
    }


_SECURITY_CONTEXT_PATH = "spec.template.spec.containers[*].securityContext.runAsNonRoot"


def _deployment_with_image(image: str, name: str = "web", ns: str = "shop") -> dict[str, Any]:
    return {
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": ns},
        "spec": {"template": {"spec": {"containers": [{"name": "app", "image": image}]}}},
    }


# nginx:1.0 through nginx:1.26 are the vulnerable range; 1.27+ is patched.
_NGINX_VULNERABLE_PATTERN = r"nginx:1\.([0-9]|1[0-9]|2[0-6])(\.|$)"


def _verifier(**kwargs: Any) -> ResourcePropertyVerifier:
    base = {"type": "resource_property", "kind": "deployment"}
    base.update(kwargs)
    return ResourcePropertyVerifier(**base)


# -- quantity parsing ---------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2", 2.0),
        ("1.5", 1.5),
        ("150m", 0.15),
        ("192Mi", 192 * 1024 * 1024),
        ("1Gi", 1024**3),
        ("2k", 2000.0),
        ("3M", 3_000_000.0),
        (7, 7.0),
        (7.5, 7.5),
    ],
)
def test_to_number_parses_kubernetes_quantities(text: str | int | float, expected: float) -> None:
    assert to_number(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", ["", "abc", None, True, {"a": 1}])
def test_to_number_returns_none_for_non_quantities(text: object) -> None:
    assert to_number(text) is None


def test_quantities_compare_numerically_not_lexically() -> None:
    # "192Mi" > "1Gi" lexically but not numerically.
    assert to_number("192Mi") < to_number("1Gi")


# -- schema validation --------------------------------------------------


def test_resource_name_and_selector_are_mutually_exclusive() -> None:
    with pytest.raises(ValidationError, match="not both"):
        _verifier(op="exists", resource_name="web", selector="app=web")


def test_comparison_op_requires_a_path() -> None:
    with pytest.raises(ValidationError, match="requires 'path'"):
        _verifier(op="gte", value=2)


def test_comparison_op_requires_a_value() -> None:
    with pytest.raises(ValidationError, match="requires 'value'"):
        _verifier(op="gte", path="status.readyReplicas")


def test_absent_with_a_path_now_parses() -> None:
    # `absent` combined with `path` used to be rejected. It is now the way to
    # assert that a path resolves to nothing (see the behavioural tests below).
    _verifier(op="absent", path="status.readyReplicas")


def test_absent_does_not_take_across_matches() -> None:
    with pytest.raises(ValidationError, match="does not take 'across_matches'"):
        _verifier(op="absent", path="status.readyReplicas", across_matches="none")


def test_exists_without_a_path_does_not_take_across_matches() -> None:
    with pytest.raises(ValidationError, match="does not take 'across_matches'"):
        _verifier(op="exists", across_matches="none")


def test_a_value_op_with_across_matches_still_parses() -> None:
    _verifier(op="gte", value=1, path="status.readyReplicas", across_matches="every")


def test_exists_with_a_path_and_across_matches_still_parses() -> None:
    # `exists` with a path is a per-object predicate, so a reduction over it
    # is meaningful and must remain legal (e.g. opa-remediation, cve-remediation).
    _verifier(
        op="exists",
        path="results[?(@.result=='fail')]",
        across_matches="none",
    )


def test_it_is_registered_under_resource_property() -> None:
    node = parse_node({"type": "resource_property", "kind": "deployment", "op": "exists"})
    assert isinstance(node, ResourcePropertyVerifier)


def test_matches_op_with_a_malformed_pattern_is_rejected_at_parse_time() -> None:
    with pytest.raises(ValidationError, match=r"invalid pattern.*\[unclosed"):
        _verifier(op="matches", value="[unclosed", path="spec.image")


def test_matches_op_with_a_valid_pattern_still_parses() -> None:
    _verifier(op="matches", value=r"^web:\d+$", path="spec.image")


def test_a_malformed_jsonpath_is_rejected_at_parse_time() -> None:
    with pytest.raises(ValidationError, match=r"spec\.\[\[.*not a valid JSONPath"):
        _verifier(op="exists", path="spec.[[")


def test_a_valid_jsonpath_still_parses() -> None:
    _verifier(op="exists", path="spec.template.spec.containers[*].image")


# -- operators ----------------------------------------------------------


@pytest.mark.parametrize(
    ("op", "value", "expected"),
    [
        ("eq", 2, True),
        ("eq", 3, False),
        ("ne", 3, True),
        ("gt", 1, True),
        ("gt", 2, False),
        ("gte", 2, True),
        ("lt", 3, True),
        ("lte", 2, True),
        ("lte", 1, False),
    ],
)
def test_numeric_operators(op: str, value: int, expected: bool) -> None:
    with patch(_GET, return_value=_deployment()):
        result = _verifier(
            op=op, value=value, path="status.readyReplicas", resource_name="web"
        ).verify(0.0)
    assert result.success is expected


# -- eq/ne coercion (Change 3) -------------------------------------------
#
# eq/ne only coerce both sides to numbers on a concrete numeric signal (a
# non-bool int/float on either side, or a quantity-suffixed string on either
# side). Two plain strings compare raw, so a version-string tag never reads
# as a float.


@pytest.mark.parametrize(
    ("op", "actual", "expected_value", "outcome"),
    [
        ("eq", "1.20", "1.2", False),
        ("ne", "1.20", "1.2", True),
        ("eq", "100m", 0.1, True),
        ("eq", "100m", "0.1", True),
        ("eq", 2, "2", True),
        ("eq", "1Gi", "1024Mi", True),
        ("eq", True, True, True),
        ("eq", "abc", "abc", True),
        ("eq", "1e3", "1000", False),
        ("ne", "1e3", "1000", True),
        # Bools never coerce through to_number (explicit isinstance guard), so
        # a bool compares by raw Python equality, never numeric coercion. That
        # equality is still True against 1/0 because bool is an int subclass.
        ("eq", True, 1, True),
        ("eq", False, 0, True),
        ("ne", True, 1, False),
        ("eq", True, "true", False),
        ("eq", "true", True, False),
    ],
)
def test_eq_ne_coercion_behavior_table(
    op: str, actual: object, expected_value: object, outcome: bool
) -> None:
    result, _reason = _apply_op(op, actual, expected_value)
    assert result is outcome


def test_contains_operator_on_a_string() -> None:
    with patch(_GET, return_value=_deployment()):
        result = _verifier(
            op="contains",
            value="shop",
            path="metadata.namespace",
            resource_name="web",
        ).verify(0.0)
    assert result.success is True


def test_matches_operator_is_a_regex() -> None:
    with patch(_GET, return_value=_deployment()):
        result = _verifier(
            op="matches",
            value=r"^web:\d+$",
            path="spec.template.spec.containers[1].image",
            resource_name="web",
        ).verify(0.0)
    assert result.success is True


def test_matches_runtime_belt_fails_closed_on_a_malformed_pattern_instead_of_raising() -> None:
    # Load-time validation (_check_shape) makes this near-unreachable through
    # the public verifier API; call _apply_op directly to exercise the belt.
    result, reason = _apply_op("matches", "web:2", "[unclosed")
    assert result is False
    assert "invalid pattern" in reason


def test_exists_passes_when_the_path_resolves() -> None:
    with patch(_GET, return_value=_deployment()):
        result = _verifier(op="exists", path="status.readyReplicas", resource_name="web").verify(
            0.0
        )
    assert result.success is True


def test_exists_fails_when_the_path_does_not_resolve() -> None:
    with patch(_GET, return_value=_deployment()):
        result = _verifier(op="exists", path="status.noSuchField", resource_name="web").verify(0.0)
    assert result.success is False


# -- match resolution ---------------------------------------------------


def test_zero_matches_fails_a_scalar_op() -> None:
    with patch(_GET, return_value=_items()):
        result = _verifier(
            op="gte", value=1, path="status.readyReplicas", selector="app=web"
        ).verify(0.0)
    assert result.success is False
    assert "no deployment matched" in result.reason


def test_absent_passes_on_zero_matches() -> None:
    with patch(_GET, return_value=_items()):
        result = _verifier(op="absent", selector="app=web", namespace="default").verify(0.0)
    assert result.success is True


def test_absent_fails_when_something_matched() -> None:
    with patch(_GET, return_value=_items(_deployment())):
        result = _verifier(op="absent", selector="app=web", namespace="default").verify(0.0)
    assert result.success is False


def test_exists_with_no_path_passes_on_any_match() -> None:
    with patch(_GET, return_value=_items(_deployment(), _deployment("api"))):
        result = _verifier(op="exists", selector="app=web").verify(0.0)
    assert result.success is True


def _object_with_results(*results: str) -> dict[str, Any]:
    return {
        "kind": "Pod",
        "metadata": {"name": "web", "namespace": "shop"},
        "results": [{"result": result} for result in results],
    }


def test_exists_with_a_path_and_across_matches_none_fails_when_something_matches() -> None:
    # This is the fail-open regression check: `exists` with a path is a
    # per-object predicate, so `across_matches: none` must actually be
    # honoured rather than silently discarded. An object with a failing
    # result must flip the check to failure.
    with patch(_GET, return_value=_object_with_results("pass", "fail")):
        result = _verifier(
            op="exists",
            path="results[?(@.result=='fail')]",
            resource_name="web",
            across_matches="none",
        ).verify(0.0)
    assert result.success is False


def test_exists_with_a_path_and_across_matches_none_passes_when_nothing_matches() -> None:
    with patch(_GET, return_value=_object_with_results("pass", "pass")):
        result = _verifier(
            op="exists",
            path="results[?(@.result=='fail')]",
            resource_name="web",
            across_matches="none",
        ).verify(0.0)
    assert result.success is True


def test_absent_with_a_path_fails_when_something_resolves() -> None:
    with patch(_GET, return_value=_object_with_results("pass", "fail")):
        result = _verifier(
            op="absent",
            path="results[?(@.result=='fail')]",
            resource_name="web",
        ).verify(0.0)
    assert result.success is False


def test_absent_with_a_path_passes_when_nothing_resolves() -> None:
    with patch(_GET, return_value=_object_with_results("pass", "pass")):
        result = _verifier(
            op="absent",
            path="results[?(@.result=='fail')]",
            resource_name="web",
        ).verify(0.0)
    assert result.success is True


def test_plural_match_across_objects_without_across_matches_is_an_explicit_error() -> None:
    with patch(_GET, return_value=_items(_deployment("web"), _deployment("api"))):
        result = _verifier(
            op="gte", value=1, path="status.readyReplicas", selector="app=web"
        ).verify(0.0)
    assert result.success is False
    assert "across_matches" in result.reason
    assert "web" in result.reason and "api" in result.reason


def test_plural_match_within_one_object_without_across_matches_is_an_explicit_error() -> None:
    with patch(_GET, return_value=_multi_container_deployment()):
        result = _verifier(
            op="gte",
            value="100m",
            path=_MULTI_CONTAINER_CPU_PATH,
            resource_name="web",
        ).verify(0.0)
    assert result.success is False
    assert "across_matches" in result.reason


def test_a_single_flat_match_evaluates_normally_without_across_matches() -> None:
    with patch(_GET, return_value=_deployment(ready=2)):
        result = _verifier(
            op="gte", value=1, path="status.readyReplicas", resource_name="web"
        ).verify(0.0)
    assert result.success is True


@pytest.mark.parametrize("across_matches", ["every", "none"])
def test_zero_matched_objects_fails_closed_for_every_across_matches(across_matches: str) -> None:
    # The zero-object guard runs before flattening, so both reductions fail
    # closed on zero matches rather than deferring to their own vacuous-match
    # semantics (e.g. "every" of an empty set).
    with patch(_GET, return_value=_items()):
        result = _verifier(
            op="gte",
            value=1,
            path="status.readyReplicas",
            selector="app=web",
            across_matches=across_matches,
        ).verify(0.0)
    assert result.success is False
    assert result.reason == "no deployment matched"


def test_path_resolves_to_nothing_across_matches_none_passes() -> None:
    with patch(_GET, return_value=_deployment()):
        result = _verifier(
            op="gte",
            value=1,
            path="status.noSuchField",
            resource_name="web",
            across_matches="none",
        ).verify(0.0)
    assert result.success is True


def test_path_resolves_to_nothing_across_matches_every_fails_closed() -> None:
    with patch(_GET, return_value=_deployment()):
        result = _verifier(
            op="gte",
            value=1,
            path="status.noSuchField",
            resource_name="web",
            across_matches="every",
        ).verify(0.0)
    assert result.success is False


# -- across_matches -------------------------------------------------------


def test_across_matches_every_passes_across_multiple_objects() -> None:
    payload = _items(_deployment("web", ready=2), _deployment("api", ready=3))
    with patch(_GET, return_value=payload):
        result = _verifier(
            op="gte",
            value=1,
            path="status.readyReplicas",
            selector="tier=app",
            across_matches="every",
        ).verify(0.0)
    assert result.success is True


def test_across_matches_every_fails_when_one_object_does_not_satisfy_the_op() -> None:
    payload = _items(_deployment("web", ready=2), _deployment("api", ready=0))
    with patch(_GET, return_value=payload):
        result = _verifier(
            op="gte",
            value=1,
            path="status.readyReplicas",
            selector="tier=app",
            across_matches="every",
        ).verify(0.0)
    assert result.success is False
    assert "api" in result.reason


def test_across_matches_every_passes_across_multiple_values_in_one_object() -> None:
    with patch(_GET, return_value=_security_context_deployment(True, True, True)):
        result = _verifier(
            op="eq",
            value=True,
            path=_SECURITY_CONTEXT_PATH,
            resource_name="web",
            across_matches="every",
        ).verify(0.0)
    assert result.success is True


def test_across_matches_every_fails_when_one_value_in_one_object_fails() -> None:
    with patch(_GET, return_value=_security_context_deployment(True, False, True)):
        result = _verifier(
            op="eq",
            value=True,
            path=_SECURITY_CONTEXT_PATH,
            resource_name="web",
            across_matches="every",
        ).verify(0.0)
    assert result.success is False


def test_regression_across_matches_none_fails_when_one_value_violates_within_one_object() -> None:
    # This is the fail-open regression check. Under the old two-level model, a
    # wildcard path within one object first ANDed to a single per-object
    # verdict, and only then did the quantifier apply across objects. Three
    # containers with exactly one violator collapsed the object's verdict to
    # False, and "none" ("no object satisfied it") then read that collapsed
    # False as a pass, hiding a real violation. Flattening removes the inner
    # AND: "none" now ranges over every individual value, not a pre-collapsed
    # per-object boolean. Do not "simplify" the flattening back away, or this
    # regression returns.
    with patch(_GET, return_value=_multi_container_deployment()):
        result = _verifier(
            op="gte",
            value="100m",
            path=_MULTI_CONTAINER_CPU_PATH,
            resource_name="web",
            across_matches="none",
        ).verify(0.0)
    assert result.success is False


def test_across_matches_none_passes_across_multiple_objects_when_none_satisfy() -> None:
    payload = _items(
        _deployment_with_image("nginx:1.27.0", name="web"),
        _deployment_with_image("nginx:1.27.1", name="api"),
    )
    with patch(_GET, return_value=payload):
        result = _verifier(
            op="matches",
            value=_NGINX_VULNERABLE_PATTERN,
            path="spec.template.spec.containers[0].image",
            selector="tier=app",
            across_matches="none",
        ).verify(0.0)
    assert result.success is True


def test_across_matches_none_fails_when_one_object_satisfies() -> None:
    payload = _items(
        _deployment_with_image("nginx:1.27.0", name="web"),
        _deployment_with_image("nginx:1.19.0", name="api"),
    )
    with patch(_GET, return_value=payload):
        result = _verifier(
            op="matches",
            value=_NGINX_VULNERABLE_PATTERN,
            path="spec.template.spec.containers[0].image",
            selector="tier=app",
            across_matches="none",
        ).verify(0.0)
    assert result.success is False
    assert "api" in result.reason


# -- across_matches element-wise quantification (pradeepvrd regression) -----
#
# `across_matches` quantifies over the ELEMENTS a wildcard segment selects,
# not over the flattened values a path happens to resolve to. A container
# missing the target field is a failing element under `every`, not one that
# silently drops out of the match set. This is the exact bug that let a pod
# with one limited and one unlimited container pass an `exists` + `every`
# check: the old value-wise flatten only ever saw the container that HAD the
# field.


def _two_container_limits_deployment(
    cpu_a: str | None, cpu_b: str | None, name: str = "web", ns: str = "shop"
) -> dict[str, Any]:
    """Two containers; a ``None`` cpu limit omits ``resources.limits.cpu`` entirely."""

    def _container(cname: str, cpu: str | None) -> dict[str, Any]:
        container: dict[str, Any] = {"name": cname}
        if cpu is not None:
            container["resources"] = {"limits": {"cpu": cpu}}
        return container

    return {
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": ns},
        "spec": {
            "template": {"spec": {"containers": [_container("a", cpu_a), _container("b", cpu_b)]}}
        },
    }


_LIMITS_CPU_PATH = "spec.template.spec.containers[*].resources.limits.cpu"


def _privileged_deployment(
    *privileged: bool | None, name: str = "cache", ns: str = "team-alpha"
) -> dict[str, Any]:
    """One deployment with one container per value; ``None`` omits ``privileged``."""

    def _container(i: int, value: bool | None) -> dict[str, Any]:
        container: dict[str, Any] = {"name": f"c{i}"}
        if value is not None:
            container["securityContext"] = {"privileged": value}
        return container

    return {
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": ns},
        "spec": {
            "template": {
                "spec": {"containers": [_container(i, v) for i, v in enumerate(privileged)]}
            }
        },
    }


_PRIVILEGED_PATH = "spec.template.spec.containers[*].securityContext.privileged"


# 1. Two containers, both with cpu limits, exists+every -> pass.
def test_across_matches_every_element_wise_passes_when_every_container_has_the_field() -> None:
    with patch(_GET, return_value=_two_container_limits_deployment("100m", "200m")):
        result = _verifier(
            op="exists", path=_LIMITS_CPU_PATH, resource_name="web", across_matches="every"
        ).verify(0.0)
    assert result.success is True


# 2. Two containers, one missing limits entirely, exists+every -> FAIL naming
#    the element. This is the regression this change exists for.
def test_across_matches_every_element_wise_fails_when_one_container_has_no_limits_at_all() -> None:
    with patch(_GET, return_value=_two_container_limits_deployment("100m", None)):
        result = _verifier(
            op="exists", path=_LIMITS_CPU_PATH, resource_name="web", across_matches="every"
        ).verify(0.0)
    assert result.success is False
    assert "web" in result.reason
    assert "[1]" in result.reason
    # Element-wise reasons must be legible: no jsonpath-ng nested-paren noise
    # (e.g. "((((spec.template).spec).containers).[1])") should leak through.
    assert "((" not in result.reason
    assert "spec.template.spec.containers.[1] did not resolve resources.limits.cpu" in result.reason


# 3. Two containers, one missing the field, eq+every (value op) -> FAIL.
def test_across_matches_every_element_wise_fails_for_a_value_op_when_a_container_is_missing_the_field() -> (
    None
):
    with patch(_GET, return_value=_two_container_limits_deployment("100m", None)):
        result = _verifier(
            op="eq",
            value="100m",
            path=_LIMITS_CPU_PATH,
            resource_name="web",
            across_matches="every",
        ).verify(0.0)
    assert result.success is False


def test_across_matches_every_element_wise_fails_when_no_container_has_the_field() -> None:
    with patch(_GET, return_value=_two_container_limits_deployment(None, None)):
        result = _verifier(
            op="exists", path=_LIMITS_CPU_PATH, resource_name="web", across_matches="every"
        ).verify(0.0)
    assert result.success is False


def test_across_matches_every_element_wise_fails_closed_when_the_wildcard_selects_no_elements() -> (
    None
):
    deployment = {
        "kind": "Deployment",
        "metadata": {"name": "web", "namespace": "shop"},
        "spec": {"template": {"spec": {"containers": []}}},
    }
    with patch(_GET, return_value=deployment):
        result = _verifier(
            op="exists", path=_LIMITS_CPU_PATH, resource_name="web", across_matches="every"
        ).verify(0.0)
    assert result.success is False


# 4. none polarity (existing behaviors preserved).
def test_across_matches_none_element_wise_fails_when_one_container_is_privileged() -> None:
    with patch(_GET, return_value=_privileged_deployment(False, True)):
        result = _verifier(
            op="eq", value=True, path=_PRIVILEGED_PATH, resource_name="cache", across_matches="none"
        ).verify(0.0)
    assert result.success is False


def test_across_matches_none_element_wise_passes_when_privileged_is_absent_everywhere() -> None:
    with patch(_GET, return_value=_privileged_deployment(None, None)):
        result = _verifier(
            op="eq", value=True, path=_PRIVILEGED_PATH, resource_name="cache", across_matches="none"
        ).verify(0.0)
    assert result.success is True


def test_across_matches_none_element_wise_passes_when_privileged_is_false_everywhere() -> None:
    with patch(_GET, return_value=_privileged_deployment(False, False)):
        result = _verifier(
            op="eq", value=True, path=_PRIVILEGED_PATH, resource_name="cache", across_matches="none"
        ).verify(0.0)
    assert result.success is True


def test_across_matches_none_element_wise_passes_when_no_container_has_the_field() -> None:
    with patch(_GET, return_value=_two_container_limits_deployment(None, None)):
        result = _verifier(
            op="exists", path=_LIMITS_CPU_PATH, resource_name="web", across_matches="none"
        ).verify(0.0)
    assert result.success is True


def test_across_matches_none_element_wise_passes_when_the_wildcard_selects_no_elements() -> None:
    deployment = {
        "kind": "Deployment",
        "metadata": {"name": "web", "namespace": "shop"},
        "spec": {"template": {"spec": {"containers": []}}},
    }
    with patch(_GET, return_value=deployment):
        result = _verifier(
            op="exists", path=_LIMITS_CPU_PATH, resource_name="web", across_matches="none"
        ).verify(0.0)
    assert result.success is True


# 5. No-wildcard path with across_matches set -> unchanged current behavior.
def test_no_wildcard_path_with_across_matches_set_is_unaffected_by_element_wise_splitting() -> None:
    with patch(_GET, return_value=_deployment(ready=2)):
        result = _verifier(
            op="gte",
            value=1,
            path="status.readyReplicas",
            resource_name="web",
            across_matches="every",
        ).verify(0.0)
    assert result.success is True


# 6. Exactly-one semantics (no across_matches) unchanged, including the
#    multiple-matches failure naming matches. Already covered by
#    test_plural_match_across_objects_without_across_matches_is_an_explicit_error
#    and test_plural_match_within_one_object_without_across_matches_is_an_explicit_error
#    above: neither sets across_matches, so element-wise splitting never
#    engages and the pre-existing value-wise error path is exercised as-is.


# 7. Suffix-empty case: path ends AT the wildcard, degenerate but must not crash.
def test_across_matches_every_element_wise_suffix_empty_does_not_crash() -> None:
    with patch(_GET, return_value=_two_container_limits_deployment("100m", None)):
        result = _verifier(
            op="exists",
            path="spec.template.spec.containers[*]",
            resource_name="web",
            across_matches="every",
        ).verify(0.0)
    assert result.success is True


def test_across_matches_none_element_wise_suffix_empty_filter_terminated_path_still_works() -> None:
    # Mirrors the opa-remediation policy-reports-clear pattern: the wildcard
    # segment is a filter at the END of the path, so suffix is empty (`This`)
    # and each selected element IS the value being checked.
    with patch(_GET, return_value=_object_with_results("pass", "fail")):
        result = _verifier(
            op="exists",
            path="results[?(@.result=='fail')]",
            resource_name="web",
            across_matches="none",
        ).verify(0.0)
    assert result.success is False


def test_across_matches_none_element_wise_suffix_empty_filter_terminated_path_passes_when_clear() -> (
    None
):
    with patch(_GET, return_value=_object_with_results("pass", "pass")):
        result = _verifier(
            op="exists",
            path="results[?(@.result=='fail')]",
            resource_name="web",
            across_matches="none",
        ).verify(0.0)
    assert result.success is True


# -- jsonpath filters ---------------------------------------------------


def test_a_filter_predicate_selects_the_right_container() -> None:
    with patch(_GET, return_value=_deployment()):
        result = _verifier(
            op="eq",
            value="web:2",
            path='spec.template.spec.containers[?(@.name="web")].image',
            resource_name="web",
        ).verify(0.0)
    assert result.success is True


# -- failure handling ---------------------------------------------------


def test_kubectl_failure_is_a_check_failure_not_a_crash() -> None:
    with patch(_GET, side_effect=RuntimeError("connection refused")):
        result = _verifier(op="exists", resource_name="web").verify(0.0)
    assert result.success is False
    assert result.status == "error"
    assert "connection refused" in result.reason


def test_condition_observed_false_is_a_fail_not_an_error() -> None:
    with patch(_GET, return_value=_deployment()):
        result = _verifier(op="absent", resource_name="web").verify(0.0)
    assert result.success is False
    assert result.status == "fail"


def test_the_check_name_is_echoed_onto_the_result() -> None:
    with patch(_GET, return_value=_deployment()):
        result = _verifier(op="exists", resource_name="web", name="web-up").verify(0.0)
    assert result.name == "web-up"


@pytest.mark.parametrize(
    ("timeout_sec", "expected_timeout"),
    [(0.0, 30.0), (5.0, 5.0), (120.0, 120.0)],
)
def test_get_resource_is_called_with_a_floored_timeout(
    timeout_sec: float, expected_timeout: float
) -> None:
    with patch(_GET, return_value=_deployment()) as mock_get:
        _verifier(op="exists", resource_name="web").verify(timeout_sec)
    assert mock_get.call_args.kwargs["timeout"] == expected_timeout


def test_converge_mode_polls_until_the_property_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    # poll_until backs off for real between failed checks (initial_delay=1.0,
    # doubling), which would make this test really sleep ~3s across the two
    # retries below. Stub the sleep it uses so the retry-until-pass behavior
    # is proven without waiting on it.
    from devops_bench.k8s.conditions import poll_until as real_poll_until
    from devops_bench.verification import base as verification_base

    def _poll_until_no_sleep(predicate: Any, *, timeout_sec: float) -> bool:
        return real_poll_until(predicate, timeout_sec=timeout_sec, sleep=lambda _: None)

    monkeypatch.setattr(verification_base, "poll_until", _poll_until_no_sleep)

    payloads = [_deployment(ready=0), _deployment(ready=0), _deployment(ready=2)]
    with patch(_GET, side_effect=payloads):
        result = _verifier(
            op="gte", value=2, path="status.readyReplicas", resource_name="web"
        ).verify(5.0)
    assert result.success is True
