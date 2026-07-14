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

"""Tests for the harness-to-dashboard result normalizer."""

from devops_bench.results import (
    SCHEMA_VERSION,
    Manifest,
    build_rows,
    derive_augmentation,
    extract_score,
    normalize_tokens,
    setup_id,
)
from devops_bench.results.normalize import OUTCOME_SCORE_KEY, TOOL_SCORE_KEY


def _manifest(**overrides):
    base = dict(
        schema_version=SCHEMA_VERSION,
        run_id="run_20260601_000000",
        t="2026-06-01T00:00:00Z",
        setup_id="m-h",
        model="m",
        harness="h",
        augmentation=[],
    )
    base.update(overrides)
    return Manifest(**base)


# -- derive_augmentation -----------------------------------------------------


def test_derive_augmentation_baseline_is_empty():
    assert derive_augmentation({"use_mcp": False, "skills": []}) == []
    assert derive_augmentation(None) == []


def test_derive_augmentation_mcp_only():
    assert derive_augmentation({"use_mcp": True, "skills": []}) == ["mcp"]


def test_derive_augmentation_skills_maps_to_skills():
    assert derive_augmentation({"use_mcp": False, "skills": ["s.md"]}) == ["skills"]


def test_derive_augmentation_combined_is_sorted():
    assert derive_augmentation({"use_mcp": True, "skills": ["s.md"]}) == ["mcp", "skills"]


# -- setup_id ----------------------------------------------------------------


def test_setup_id_baseline_has_no_trailing_dash():
    assert setup_id("alpha-pro", "gemini", []) == "alpha-pro-gemini"


def test_setup_id_is_order_independent():
    assert setup_id("m", "h", ["skills", "mcp"]) == setup_id("m", "h", ["mcp", "skills"])
    assert setup_id("m", "h", ["skills", "mcp"]) == "m-h-mcp-skills"


def test_setup_id_strips_unsafe_chars():
    # Runs of unsafe chars collapse to a single dash and the id is lower-cased,
    # matching catalog.mjs (NOT dropped, which would give "gemini25pro-api").
    assert setup_id("gemini 2.5/pro", "api", []) == "gemini-2-5-pro-api"


def test_setup_id_matches_catalog_slug_for_dotted_model():
    # The setup id's model component must equal the model catalog doc key that
    # catalog.mjs slugify produces, so the rows<->catalog join holds.
    assert setup_id("gemini-3.1-pro", "gemini-cli", []) == "gemini-3-1-pro-gemini-cli"


# -- normalize_tokens --------------------------------------------------------


def test_normalize_tokens_api_shape():
    tokens = {"prompt_tokens": 10, "candidates_tokens": 5, "total_tokens": 15}
    assert normalize_tokens(tokens) == (10, 5)


def test_normalize_tokens_cli_shape():
    assert normalize_tokens({"input": 7, "output": 3}) == (7, 3)


def test_normalize_tokens_google_metadata_shape():
    tokens = {"prompt_token_count": 8, "candidates_token_count": 4}
    assert normalize_tokens(tokens) == (8, 4)


def test_normalize_tokens_missing_yields_none():
    assert normalize_tokens({}) == (None, None)
    assert normalize_tokens(None) == (None, None)


def test_normalize_tokens_float_coerced_to_int():
    assert normalize_tokens({"input": 12.0, "output": 3.9}) == (12, 3)


# -- extract_score -----------------------------------------------------------


def test_extract_score_dict_shape():
    scores = {OUTCOME_SCORE_KEY: {"score": 0.83, "success": True, "reason": "ok"}}
    assert extract_score(scores, OUTCOME_SCORE_KEY) == 0.83


def test_extract_score_bare_float_shape():
    assert extract_score({TOOL_SCORE_KEY: 0.5}, TOOL_SCORE_KEY) == 0.5


def test_extract_score_missing_metric_is_none():
    assert extract_score({}, OUTCOME_SCORE_KEY) is None
    assert extract_score(None, OUTCOME_SCORE_KEY) is None


def test_extract_score_none_score_in_dict_is_none():
    assert extract_score({OUTCOME_SCORE_KEY: {"score": None}}, OUTCOME_SCORE_KEY) is None


# -- build_rows --------------------------------------------------------------


def test_build_rows_success_record():
    manifest = _manifest(
        setup_id="alpha-gemini-mcp-skills",
        model="alpha",
        harness="gemini",
        augmentation=["mcp", "skills"],
    )
    record = {
        "name": "Rotate Secret",
        "folder": "task_001",
        "status": "success",
        "latency": 42.5,
        "tokens": {"prompt_tokens": 100, "candidates_tokens": 20},
        "scores": {
            OUTCOME_SCORE_KEY: {"score": 0.9, "success": True, "reason": "ok"},
            TOOL_SCORE_KEY: {"score": 0.7, "success": True, "reason": "ok"},
        },
    }

    rows = build_rows([record], manifest)

    assert len(rows) == 1
    d = rows[0].to_dict()
    assert d == {
        "setupId": "alpha-gemini-mcp-skills",
        "model": "alpha",
        "harness": "gemini",
        "augmentation": ["mcp", "skills"],
        "runId": "run_20260601_000000",
        "t": "2026-06-01T00:00:00Z",
        "taskFolder": "task_001",
        "taskName": "Rotate Secret",
        "iteration": 0,
        "outcomeScore": 0.9,
        "toolScore": 0.7,
        "latencySec": 42.5,
        "inputTokens": 100,
        "outputTokens": 20,
        "status": "success",
        "validated": False,
    }


def test_build_rows_failed_record_has_null_scores_and_tokens():
    record = {
        "name": "Broken Task",
        "folder": "task_002",
        "status": "failed",
        "latency": 0.0,
        "tokens": {},
        "scores": {},
    }

    row = build_rows([record], _manifest())[0]

    assert row.status == "failed"
    assert row.outcome_score is None
    assert row.tool_score is None
    assert row.input_tokens is None
    assert row.output_tokens is None
    assert row.iteration == 0


def test_build_rows_preserves_order_and_run_identity():
    manifest = _manifest(run_id="run_20260101_120000")
    records = [
        {"name": "a", "folder": "f-a", "status": "success", "scores": {}, "tokens": {}},
        {"name": "b", "folder": "f-b", "status": "success", "scores": {}, "tokens": {}},
    ]

    rows = build_rows(records, manifest)

    assert [r.task_name for r in rows] == ["a", "b"]
    assert all(r.run_id == "run_20260101_120000" for r in rows)


def test_result_row_keys_match_typescript_interface():
    """``ResultRow.to_dict()`` keys mirror the dashboard ``ResultRow`` interface.

    Pinned so the producer and the ``site/src/lib/schema.d.ts`` consumer
    cannot drift apart silently. The contract version lives on the manifest, not
    on each row, so ``schemaVersion`` is intentionally absent here.
    """
    ts_result_row_fields = {
        "setupId",
        "model",
        "harness",
        "augmentation",
        "runId",
        "t",
        "taskFolder",
        "taskName",
        "iteration",
        "status",
        "outcomeScore",
        "toolScore",
        "latencySec",
        "inputTokens",
        "outputTokens",
        "validated",
    }
    row = build_rows(
        [{"name": "n", "folder": "f", "status": "success", "scores": {}, "tokens": {}}],
        _manifest(),
    )[0]
    assert set(row.to_dict()) == ts_result_row_fields


def test_manifest_to_dict_keys():
    assert set(_manifest().to_dict()) == {
        "schemaVersion",
        "runId",
        "t",
        "setupId",
        "model",
        "harness",
        "augmentation",
    }


def test_build_rows_propagates_validated():
    manifest = _manifest()
    validated_row = build_rows(
        [{"name": "t", "folder": "f", "status": "success", "validated": True}], manifest
    )[0]
    assert validated_row.to_dict()["validated"] is True
    # Absent key defaults to False (unvetted tasks don't promote).
    default_row = build_rows([{"name": "t", "folder": "f", "status": "success"}], manifest)[0]
    assert default_row.to_dict()["validated"] is False
