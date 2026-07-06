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

"""Unit tests for devops_bench.core.results."""

import json

from devops_bench.core.results import Result, Status


def test_status_is_str_comparable():
    assert Status.PASSED == "passed"
    assert str(Status.FAILED) == "failed"


def test_result_ok_only_when_passed():
    assert Result.passed().ok is True
    assert Result.failed().ok is False
    assert Result.errored().ok is False
    assert Result.skipped().ok is False


def test_factory_helpers_set_status_and_reason():
    result = Result.failed("pod never became ready")
    assert result.status is Status.FAILED
    assert result.reason == "pod never became ready"


def test_factory_helpers_accept_extra_fields():
    result = Result.passed("done", elapsed_sec=1.5, details={"count": 3})
    assert result.elapsed_sec == 1.5
    assert result.details == {"count": 3}


def test_string_status_is_normalized_to_enum():
    result = Result(status="error")
    assert result.status is Status.ERROR


def test_to_dict_is_json_serializable():
    result = Result.passed("ok", elapsed_sec=2.0, details={"nested": {"a": 1}})
    payload = result.to_dict()
    assert payload == {
        "status": "passed",
        "reason": "ok",
        "elapsed_sec": 2.0,
        "details": {"nested": {"a": 1}},
    }
    assert json.loads(json.dumps(payload))["status"] == "passed"


def test_details_default_is_independent_per_instance():
    a = Result.passed()
    b = Result.passed()
    a.details["x"] = 1
    assert b.details == {}
