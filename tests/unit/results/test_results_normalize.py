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
from devops_bench.results.normalize import (
    OUTCOME_SCORE_KEY,
    TOOL_SCORE_KEY,
    count_tool_calls,
)


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


def test_normalize_tokens_legacy_api_shape() -> None:
    tokens = {"prompt_tokens": 10, "candidates_tokens": 5, "total_tokens": 15}
    assert normalize_tokens(tokens) == (10, 5, None, None, None, 15)


def test_normalize_tokens_canonical_shape() -> None:
    tokens = {
        "input": 7,
        "cached": 100,
        "cache_write": None,
        "reasoning": 4,
        "output": 3,
        "total": 114,
    }
    assert normalize_tokens(tokens) == (7, 3, 100, 4, None, 114)


def test_normalize_tokens_google_metadata_shape():
    tokens = {"prompt_token_count": 8, "candidates_token_count": 4}
    assert normalize_tokens(tokens) == (8, 4, None, None, None, None)


def test_normalize_tokens_missing_yields_none():
    assert normalize_tokens({}) == (None, None, None, None, None, None)
    assert normalize_tokens(None) == (None, None, None, None, None, None)


def test_normalize_tokens_none_valued_canonical_keys_fall_through() -> None:
    # A canonical dict with None buckets must not mask values under legacy keys.
    assert normalize_tokens({"input": None, "prompt_tokens": 9, "output": 2}) == (
        9,
        2,
        None,
        None,
        None,
        None,
    )


def test_normalize_tokens_float_coerced_to_int():
    assert normalize_tokens({"input": 12.0, "output": 3.9}) == (12, 3, None, None, None, None)


def test_normalize_tokens_openclaw_camel_case_cache_keys() -> None:
    """OpenClaw spells its cache buckets in camelCase; they must not be dropped.

    Verbatim from a live ``oc`` run. Before the aliases existed, ``cacheRead``
    matched nothing, so ``cached`` read as ``None`` and the buckets summed to
    26385 against a reported total of 50773 — 48% of the run's billed tokens
    invisible on every OpenClaw row.
    """
    tokens = {"input": 26362, "output": 23, "cacheRead": 24388, "total": 50773}
    normalized = normalize_tokens(tokens)
    assert normalized == (26362, 23, 24388, None, None, 50773)
    assert sum(v for v in normalized[:5] if v is not None) == normalized.total


def test_normalize_tokens_openclaw_cache_write_and_total_tokens() -> None:
    """``cacheWrite`` and ``totalTokens`` are the other two OpenClaw spellings.

    ``@openclaw/ai``'s ``parseChunkUsage`` builds every adapter's usage as
    ``{input, output, cacheRead, cacheWrite, totalTokens}``, with ``input``
    already net of both cache buckets — so all four buckets add up to the total.
    """
    tokens = {
        "input": 100,
        "output": 20,
        "cacheRead": 300,
        "cacheWrite": 40,
        "totalTokens": 460,
    }
    normalized = normalize_tokens(tokens)
    assert normalized == (100, 20, 300, None, 40, 460)
    assert sum(v for v in normalized[:5] if v is not None) == normalized.total


def test_normalize_tokens_snake_case_wins_over_camel_case() -> None:
    """The canonical key keeps priority when a record carries both spellings."""
    tokens = {"cached": 1, "cacheRead": 999, "cache_write": 2, "cacheWrite": 888}
    normalized = normalize_tokens(tokens)
    assert normalized.cached == 1
    assert normalized.cache_write == 2


def test_normalize_tokens_openclaw_cost_breakdown_is_not_read_as_tokens() -> None:
    """OpenClaw nests a float ``cost`` map beside the counts; it must be ignored.

    The nested map repeats the bucket names, so a flattening lookup would read
    dollars as tokens.
    """
    tokens = {
        "input": 100,
        "output": 20,
        "cacheRead": 300,
        "total": 420,
        "cost": {"input": 0.01, "output": 0.02, "cacheRead": 0.03, "total": 0.06},
    }
    assert normalize_tokens(tokens) == (100, 20, 300, None, None, 420)


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
        "terminal_reason": "completed",
        "trajectory": [
            {"name": "kubectl", "args": {}, "result": "ok", "status": "completed"},
            {"name": "kubectl", "args": {}, "result": None, "status": "error"},
        ],
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
        "correctnessScore": None,
        "recoverableSafetyScore": None,
        "catastrophic": False,
        "scoringVersion": "",
        "toolScore": 0.7,
        "toolCalls": 2,
        "toolErrors": 1,
        "latencySec": 42.5,
        "inputTokens": 100,
        "outputTokens": 20,
        "cachedTokens": None,
        "reasoningTokens": None,
        "cacheWriteTokens": None,
        "totalTokens": None,
        "status": "success",
        "modelTurns": None,
        "toolWaitSec": None,
        "servedModel": "",
        "terminalReason": "completed",
        "timeoutSec": None,
        "validated": False,
    }


def test_build_rows_defaults_terminal_reason_for_records_that_omit_it() -> None:
    """Records written before the field existed read as unreported, not clean.

    ``""`` must not collapse to ``"completed"`` — an old row cannot claim the
    agent finished on its own when nothing recorded that it did.
    """
    rows = build_rows([{"name": "n", "folder": "f", "status": "success"}], _manifest())
    assert rows[0].terminal_reason == ""


def test_build_rows_carries_a_cut_off_run_through_as_success() -> None:
    """A cut-off run still reads ``status: "success"`` — that is the point.

    The record status describes the harness, not the agent, so
    ``terminal_reason`` is the only thing separating "the harness killed it"
    from "it answered badly".
    """
    record = {"name": "n", "folder": "f", "status": "success", "terminal_reason": "timeout"}
    row = build_rows([record], _manifest())[0]
    assert (row.status, row.terminal_reason) == ("success", "timeout")


def test_build_rows_maps_all_v1_score_components() -> None:
    record = {
        "name": "Optimize Scale",
        "folder": "task_017",
        "status": "success",
        "scores": {
            "OutcomeScore": {"score": 0.82, "version": "v1", "reason": "..."},
            "ChecklistScore": {"score": 0.9, "success": True, "reason": "3/3"},
            "VerificationRecoverable": {"score": 0.5, "success": False, "reason": "1/2"},
            "VerificationCatastrophic": {"score": 1.0, "success": True, "reason": "0 fired"},
        },
    }

    d = build_rows([record], _manifest())[0].to_dict()

    assert d["outcomeScore"] == 0.82
    assert d["correctnessScore"] == 0.9
    # The raw pass fraction as emitted. This module maps and never scores, so
    # the [0.1, 1.0] rescale the outcome formula applies is not reapplied here.
    assert d["recoverableSafetyScore"] == 0.5
    assert d["catastrophic"] is False
    assert d["scoringVersion"] == "v1"


def test_build_rows_flags_catastrophic_and_zeroed_outcome() -> None:
    record = {
        "name": "Nuked prod",
        "folder": "task_x",
        "status": "success",
        "scores": {
            "OutcomeScore": {"score": 0.0, "version": "v1", "reason": "cat_v=0"},
            "ChecklistScore": {"score": 1.0, "success": True},
            "VerificationCatastrophic": {"score": 0.0, "success": False, "reason": "1 fired"},
        },
    }

    d = build_rows([record], _manifest())[0].to_dict()

    assert d["catastrophic"] is True
    assert d["outcomeScore"] == 0.0
    assert d["correctnessScore"] == 1.0


def test_build_rows_correctness_falls_back_to_outcome_validity() -> None:
    # A task with no checklist: correctness comes from OutcomeValidity instead.
    record = {
        "name": "No checklist",
        "folder": "task_y",
        "status": "success",
        "scores": {
            "OutcomeScore": {"score": 0.7, "version": "v1"},
            "OutcomeValidity": {"score": 0.7, "success": False},
        },
    }

    d = build_rows([record], _manifest())[0].to_dict()

    assert d["correctnessScore"] == 0.7


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
    assert row.cached_tokens is None
    assert row.reasoning_tokens is None
    assert row.cache_write_tokens is None
    assert row.total_tokens is None
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

    NOTE: the scoring-framework v1 fields (``correctnessScore`` /
    ``recoverableSafetyScore`` / ``catastrophic`` / ``scoringVersion``, and the
    ``outcomeScore`` re-semantics) are produced here first; the TS interface and
    the ingest validators are updated in the frontend-phase rollout.
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
        "correctnessScore",
        "recoverableSafetyScore",
        "catastrophic",
        "scoringVersion",
        "toolScore",
        "toolCalls",
        "toolErrors",
        "latencySec",
        "inputTokens",
        "outputTokens",
        "cachedTokens",
        "reasoningTokens",
        "cacheWriteTokens",
        "totalTokens",
        "modelTurns",
        "toolWaitSec",
        "servedModel",
        "terminalReason",
        "timeoutSec",
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
        "timeoutSec",
    }


def test_build_rows_carries_the_run_timeout_onto_every_row():
    """The wall-clock budget rides on the row, not just the manifest.

    Ingest uploads ``rows.json`` alone and never reads ``manifest.json``, so a
    run-level setting that stays on the manifest never reaches the dashboard.
    Without it, "timed out" cannot be told from "finished with room to spare".
    """
    manifest = _manifest().model_copy(update={"timeout_sec": 900.0})
    rows = build_rows(
        [
            {"name": "a", "folder": "a", "status": "success"},
            {"name": "b", "folder": "b", "status": "failed"},
        ],
        manifest,
    )
    assert [row.to_dict()["timeoutSec"] for row in rows] == [900.0, 900.0]


def test_build_rows_propagates_validated():
    manifest = _manifest()
    validated_row = build_rows(
        [{"name": "t", "folder": "f", "status": "success", "validated": True}], manifest
    )[0]
    assert validated_row.to_dict()["validated"] is True
    # Absent key defaults to False (unvetted tasks don't promote).
    default_row = build_rows([{"name": "t", "folder": "f", "status": "success"}], manifest)[0]
    assert default_row.to_dict()["validated"] is False


def test_build_rows_carries_cached_and_reasoning() -> None:
    record = {
        "name": "t",
        "folder": "f",
        "status": "success",
        "tokens": {
            "input": 19052,
            "cached": 12173,
            "cache_write": None,
            "reasoning": 229,
            "output": 35,
            "total": 31489,
        },
    }
    row = build_rows([record], _manifest())[0]
    assert row.input_tokens == 19052
    assert row.cached_tokens == 12173
    assert row.reasoning_tokens == 229
    assert row.output_tokens == 35
    assert row.total_tokens == 31489
    assert row.cache_write_tokens is None


def test_build_rows_carries_cache_write() -> None:
    record = {
        "name": "t",
        "folder": "f",
        "status": "success",
        "tokens": {
            "input": 5,
            "cached": 1000,
            "cache_write": 200,
            "reasoning": None,
            "output": 40,
            "total": 1245,
        },
    }
    row = build_rows([record], _manifest())[0]
    assert row.cache_write_tokens == 200
    assert row.total_tokens == 1245


# --- tool-call counts ---


def test_count_tool_calls_counts_entries_and_failures() -> None:
    trajectory = [
        {"name": "a", "status": "completed"},
        {"name": "b", "status": "error"},
        {"name": "c", "status": "interrupted"},
        {"name": "d", "status": "completed"},
    ]
    assert count_tool_calls(trajectory) == (4, 1)


def test_count_tool_calls_counts_only_error_as_a_failure() -> None:
    """``called`` and ``interrupted`` are the same condition under two names.

    Both mean the parser never saw the call resolve. Only the antigravity
    parser labels it ``interrupted``; four others leave the identical case as
    ``called``, so counting it would make ``toolErrors`` incomparable across
    the harness dimension the dashboard groups by.
    """
    assert count_tool_calls([{"name": "a", "status": "interrupted"}]) == (1, 0)
    assert count_tool_calls([{"name": "a", "status": "called"}]) == (1, 0)


def test_count_tool_calls_reports_none_when_no_trajectory_was_captured() -> None:
    """A trajectory export can fail, and 0 would read as "made no calls".

    Mirrors ``model_turns``, which uses ``None`` for the same situation. With no
    ``errors`` argument the caller cannot say which case an empty list is, so it
    stays unknown.
    """
    assert count_tool_calls(None) == (None, None)
    assert count_tool_calls([]) == (None, None)
    assert count_tool_calls("not a list") == (None, None)


def test_count_tool_calls_reports_a_clean_run_that_called_no_tool_as_zero() -> None:
    """An empty trajectory with no errors is a fact, not missing telemetry.

    A run can answer from the model alone. Reporting ``None`` there drops it out
    of the dashboard average instead of recording the zero it earned.
    """
    assert count_tool_calls([], []) == (0, 0)


def test_count_tool_calls_reports_none_when_an_empty_trajectory_came_with_errors() -> None:
    """Every path that loses a transcript says so in ``errors``.

    The openclaw exporter appends its failure, a timeout appends its own, and a
    failed record carries the exception — so an error next to an empty
    trajectory means "not captured", never "called no tools".
    """
    assert count_tool_calls([], ["oc export-trajectory exited 1"]) == (None, None)


def test_count_tool_calls_ignores_interleaved_text_turns() -> None:
    """``AgentResult`` lets an API agent interleave text turns with tool calls.

    A text turn is a mapping too, so counting entries rather than tool calls
    would inflate ``toolCalls`` on exactly the harness the column is meant to
    compare against the CLI ones.
    """
    trajectory = [
        {"name": "a", "status": "completed"},
        {"role": "assistant", "text": "thinking about it"},
        {"name": "b", "status": "error"},
    ]
    assert count_tool_calls(trajectory) == (2, 1)


def test_count_tool_calls_skips_malformed_entries() -> None:
    """A malformed entry should lose a count, not raise and kill the run."""
    assert count_tool_calls([{"name": "a", "status": "error"}, "junk", None]) == (1, 1)


def test_count_tool_calls_reports_none_when_every_entry_is_malformed() -> None:
    """All-junk is the "not captured" case, not a run that made zero calls.

    Skipping non-mappings can empty the list, and a confident 0 there would sink
    a dashboard average on exactly the corrupted records the skip exists to
    survive.
    """
    assert count_tool_calls(["junk", None]) == (None, None)


def test_build_rows_counts_tools_from_the_trajectory() -> None:
    """The trajectory is too large to aggregate over at dashboard time.

    Two models with the same score and the same wall clock can differ severalfold
    in how much work they did to get there; nothing on the row said so.
    """
    record = {
        "name": "n",
        "folder": "f",
        "status": "success",
        "trajectory": [
            {"name": "a", "status": "completed"},
            {"name": "b", "status": "error"},
            {"name": "c", "status": "completed"},
        ],
    }
    row = build_rows([record], _manifest())[0]
    assert (row.tool_calls, row.tool_errors) == (3, 1)


def test_build_rows_reports_none_tool_counts_for_a_record_with_no_trajectory() -> None:
    row = build_rows([{"name": "n", "folder": "f", "status": "failed"}], _manifest())[0]
    assert (row.tool_calls, row.tool_errors) == (None, None)


def test_build_rows_carries_model_turns_separately_from_tool_calls() -> None:
    """One model turn can issue several tool calls, so the counts diverge.

    Seen live in an openclaw run: a single ``assistant.message`` carried two
    ``toolCall`` entries. Input tokens grow with turns, not with tool calls, so
    a cost-per-turn read off ``toolCalls`` would be wrong by that factor.
    """
    record = {
        "name": "n",
        "folder": "f",
        "status": "success",
        "model_turns": 2,
        "trajectory": [{"name": "a", "status": "completed"}] * 3,
    }
    row = build_rows([record], _manifest())[0]
    assert (row.model_turns, row.tool_calls) == (2, 3)


def test_build_rows_joins_served_models_so_a_failover_stays_visible() -> None:
    """``model`` is the requested id; a failover means it is not what ran.

    Collapsing to the first entry would hide exactly the case the field exists
    for, so both are kept.
    """

    def served(value):
        record = {"name": "n", "folder": "f", "status": "success", "served_models": value}
        return build_rows([record], _manifest())[0].served_model

    assert served(["gemini-3-flash-preview"]) == "gemini-3-flash-preview"
    assert served(["a", "b"]) == "a,b"
    assert served([]) == ""
    assert served(None) == ""
    assert served("a") == ""


def test_build_rows_keeps_a_zero_tool_wait_but_drops_a_missing_one() -> None:
    """Unlike a count, zero seconds inside tools is a real reading.

    Tools that return within the transcript's millisecond resolution genuinely
    measured ~0, so that must stay on the row; a harness that reports no
    timings at all must not be averaged in as if it were instantaneous.
    """

    def wait(record_extra):
        record = {"name": "n", "folder": "f", "status": "success", **record_extra}
        return build_rows([record], _manifest())[0].tool_wait_sec

    assert wait({"tool_wait_sec": 0}) == 0.0
    assert wait({"tool_wait_sec": 1.75}) == 1.75
    assert wait({}) is None
    assert wait({"tool_wait_sec": -1.0}) is None
    assert wait({"tool_wait_sec": True}) is None


def test_build_rows_reports_unusable_model_turns_as_none() -> None:
    """Zero, a bool, or a non-int all mean unmeasured, not "took no turns".

    A record that produced output cannot have taken zero round-trips, and
    ``True`` is an ``int`` in Python, so both would otherwise land on the row
    as a real count and drag a dashboard average down.
    """

    def turns(value):
        record = {"name": "n", "folder": "f", "status": "success", "model_turns": value}
        return build_rows([record], _manifest())[0].model_turns

    assert turns(0) is None
    assert turns(True) is None
    assert turns("4") is None
    assert turns(None) is None
