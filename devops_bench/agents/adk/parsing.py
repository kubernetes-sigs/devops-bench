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

"""Parser for the serialized ADK event stream.

An ADK run yields ``Event`` objects; the harness serializes each one and hands
the resulting list here. Working on plain mappings rather than SDK objects keeps
this module import-light (no ``google.adk`` dependency) and lets the tests
exercise the fold against recorded event dumps.

Each event carries a ``content.parts`` list. Three part shapes matter:

| Part shape          | Meaning                                              |
|---------------------|------------------------------------------------------|
| ``function_call``   | The model asked for a tool (``id``, ``name``, ``args``) |
| ``function_response`| The tool answered (``id``, ``name``, ``response``)   |
| ``text``            | Assistant prose                                      |

Calls and responses are correlated by the ``id`` ADK stamps on both sides, so a
call and its result fold into one :class:`~devops_bench.agents.result.ToolCall`
rather than two trajectory entries.

An event from a remote A2A agent also carries the raw task envelope under
``custom_metadata['a2a:response']``. That matters because the agent's actual
answer lives in the task's ``status.message``, while the ``content.parts`` ADK
builds alongside it hold a *mirror of the last artifact* — so the envelope, not
the content, is what the judge should grade.

That envelope is also the only place a remote's *sub-agents* are visible. They
run on the far side of the boundary and never reach the event stream as ADK
parts, but a remote that tags each artifact with its producer lets the parser
report which sub-agent ran, in what order, and what it contributed.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Mapping, Sequence
from typing import Any

from devops_bench.agents.result import ROOT_ACTOR, ToolCall, empty_tokens, scoped_actor

__all__: list[str] = ["parse_event_stream"]

# ``usage_metadata`` field -> the accumulator slot it feeds. ADK passes the
# google-genai usage block through verbatim, so these are the genai names.
_USAGE_FIELDS: dict[str, str] = {
    "prompt_token_count": "prompt",
    "cached_content_token_count": "cached",
    "candidates_token_count": "output",
    "thoughts_token_count": "reasoning",
    "total_token_count": "total",
}

# Where ``RemoteA2aAgent`` stashes the raw task envelope on the event it builds.
# ADK's converter does not export the key, so it is spelled out here.
_A2A_RESPONSE_KEY: str = "a2a:response"

# Terminal ``TaskState`` values meaning the remote agent never answered. The
# proto types serialize the enum as ``TASK_STATE_FAILED`` and the pydantic types
# as ``failed``, so states are normalized to the bare slug before the lookup.
_A2A_FAILURE_STATES: frozenset[str] = frozenset({"failed", "canceled", "rejected"})

# The one state whose status message is the agent's answer. ADK also emits
# streaming updates carrying a status message — ``working`` most often, and
# ``input_required`` / ``auth_required`` are no more final — and that text is
# progress commentary. Appending it would put narration ahead of the real
# answer in the output the judge grades. A failure state's message is a failure
# notice, which is not an answer either; it goes to ``errors`` instead.
_A2A_ANSWER_STATE: str = "completed"

# Artifact-metadata key naming the sub-agent that produced an artifact. A2A
# leaves ``Artifact.metadata`` free-form, so this is a convention a remote opts
# into rather than a guarantee of the protocol; an artifact without it falls
# back to the artifact's own ``name``, and one with neither is skipped.
_A2A_SUBAGENT_KEY: str = "sub_agent"


def _int_or_none(value: object) -> int | None:
    """Coerce to ``int``, rejecting ``bool`` (a JSON ``true`` is not a count)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _parts(event: Mapping[str, Any]) -> list[Any]:
    """Return an event's ``content.parts`` list, or ``[]`` when it has none."""
    content = event.get("content")
    if not isinstance(content, Mapping):
        return []
    parts = content.get("parts")
    return parts if isinstance(parts, list) else []


def _is_user_content(event: Mapping[str, Any]) -> bool:
    """Report whether the event's content is authored by the user.

    Tool results ride on ``role="user"`` content too, so this only gates *text*
    accumulation — never the function-call fold.
    """
    content = event.get("content")
    return isinstance(content, Mapping) and content.get("role") == "user"


def _a2a_task(event: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return the A2A task envelope ``RemoteA2aAgent`` attached, if any."""
    metadata = event.get("custom_metadata")
    if not isinstance(metadata, Mapping):
        return None
    response = metadata.get(_A2A_RESPONSE_KEY)
    return response if isinstance(response, Mapping) else None


def _a2a_status(task: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Pull the reported state and final message text out of a task envelope.

    Returns:
        A ``(state, text)`` tuple. ``state`` is normalized to the bare slug
        (``"completed"``, not ``"TASK_STATE_COMPLETED"``). ``text`` is ``None``
        when the task carried no status message.

    States other than :data:`_A2A_ANSWER_STATE` carry a message too — a
    streaming update's is progress commentary, a failure's is a failure notice —
    so the state must be checked before the text is treated as the answer.
    """
    status = task.get("status")
    if not isinstance(status, Mapping):
        return None, None

    raw_state = status.get("state")
    state = raw_state.lower().removeprefix("task_state_") if isinstance(raw_state, str) else None

    message = status.get("message")
    parts = message.get("parts") if isinstance(message, Mapping) else None
    texts = (
        [
            part["text"]
            for part in parts
            if isinstance(part, Mapping) and isinstance(part.get("text"), str)
        ]
        if isinstance(parts, list)
        else []
    )
    return state, ("".join(texts) or None)


def _artifact_actor(artifact: Mapping[str, Any]) -> str | None:
    """Name the sub-agent that produced an artifact, or ``None`` if unnamed.

    Prefers the ``metadata`` tag over the artifact's ``name``: the tag is what a
    remote sets deliberately to identify a producer, whereas ``name`` is a free
    label that often repeats the artifact's purpose rather than its author.
    """
    metadata = artifact.get("metadata")
    if isinstance(metadata, Mapping):
        tagged = metadata.get(_A2A_SUBAGENT_KEY)
        if isinstance(tagged, str) and tagged:
            return tagged
    name = artifact.get("name")
    return name if isinstance(name, str) and name else None


def _artifact_text(artifact: Mapping[str, Any]) -> str:
    """Concatenate an artifact's text parts."""
    parts = artifact.get("parts")
    if not isinstance(parts, list):
        return ""
    return "".join(
        part["text"]
        for part in parts
        if isinstance(part, Mapping) and isinstance(part.get("text"), str)
    )


def _artifact_key(task: Mapping[str, Any], artifact: Mapping[str, Any], position: int) -> tuple:
    """Return the identity of an artifact, stable across snapshots of one task.

    ADK re-emits a task envelope as it progresses, and each snapshot repeats the
    artifacts already produced, so identity is what stops one sub-agent being
    folded once per snapshot it survived into.

    ``artifactId`` is that identity when present. It is not guaranteed — A2A
    leaves the field optional — so an artifact without one falls back to its
    position, which is stable for the same reason the repetition happens: a
    snapshot appends to the artifact list rather than rewriting it.

    Args:
        task: The A2A task envelope the artifact came from.
        artifact: The artifact itself.
        position: Its index within the envelope's artifact list.

    Returns:
        A hashable key scoped to the task, so two concurrent remote tasks never
        collide on a shared artifact id.
    """
    artifact_id = artifact.get("artifactId")
    identity = artifact_id if isinstance(artifact_id, str) and artifact_id else position
    return (str(task.get("id", "")), identity)


def _fold_a2a_artifacts(
    task: Mapping[str, Any],
    author: object,
    trajectory: list[ToolCall],
    folded: dict[tuple, ToolCall],
) -> bool:
    """Append one attributed entry per sub-agent artifact on a task envelope.

    A remote agent runs its fleet on the far side of the boundary, so none of it
    reaches the event stream as ADK parts — the sub-agents would otherwise be
    invisible. What does cross is one artifact per producer, which is enough to
    report *that* a sub-agent ran, in what order, and what it contributed.

    The entry is a sub-agent *contribution*, not a tool invocation: ``name``
    repeats the producer and ``args`` is empty, because no tool call was
    observed. The trajectory carries it anyway because that is the only channel
    the judge reads. Tool-level detail inside a sub-agent — the calls its own
    loop made — is not recoverable this way and is deliberately not guessed at;
    it needs the remote to emit function parts, which no recorded run does.

    Artifacts are folded whatever the task's state. Unlike ``output``, where a
    failed task must contribute nothing (its artifacts would be graded as the
    answer), knowing which sub-agents ran before a failure is exactly the
    diagnostic a failed run is inspected for. That makes deduplication load-
    bearing rather than defensive: ADK emits a ``working`` snapshot and then a
    ``completed`` one for the same task, both carrying the artifacts produced so
    far, so folding every snapshot unguarded reports each sub-agent once per
    snapshot it appeared in — and "which sub-agents ran, in what order" is
    exactly what the trajectory is read for.

    A repeat is an *update*, not a no-op: A2A lets a producer keep writing to
    one ``artifactId``, so the ``working`` snapshot can carry a fragment of the
    text the ``completed`` one carries in full. The later text replaces the
    earlier in the entry already in ``trajectory``, which holds first-seen order
    — the order sub-agents *started*, which is what is being reported. It
    replaces rather than concatenates because each envelope is a snapshot of the
    whole task, not a delta: appending would repeat the prefix. A snapshot that
    carries no text for an artifact leaves what is there alone, so a later empty
    parts list cannot erase a result that already arrived.

    Args:
        task: The A2A task envelope.
        author: The event's ``author`` — the remote agent itself, which is the
            root that its sub-agents are distinguished from.
        trajectory: Accumulator appended to in artifact order.
        folded: Artifacts already folded from earlier snapshots, keyed by
            identity and mapped to the entry they produced, extended and
            updated in place. See :func:`_artifact_key`.

    Returns:
        Whether any artifact named a producer other than ``author``. That is the
        evidence the remote actually delegated; a single artifact the remote
        tagged with its own name is one agent reporting its own work. Reported
        for every artifact examined, including ones skipped as already folded —
        a repeat snapshot must not retract the run's attribution.
    """
    artifacts = task.get("artifacts")
    if not isinstance(artifacts, list):
        return False

    root = author if isinstance(author, str) else None
    delegated = False

    for position, artifact in enumerate(artifacts):
        if not isinstance(artifact, Mapping):
            continue
        label = _artifact_actor(artifact)
        if label is None:
            continue
        if label != root:
            delegated = True
        text = _artifact_text(artifact) or None
        key = _artifact_key(task, artifact, position)
        seen = folded.get(key)
        if seen is not None:
            # The producer wrote more to the same artifact. Only the text can
            # have changed; the label is its identity and the position in
            # ``trajectory`` is when it started, both of which stay put.
            if text is not None:
                seen.result = text
            continue
        entry = ToolCall(
            name=label,
            args={},
            result=text,
            status="completed",
            # An artifact the remote tagged with its own name is the remote
            # reporting its own work, so it is left unstamped and resolved
            # by the run-level pass exactly like the remote's own calls:
            # ``ROOT_ACTOR`` if the run delegated, cleared if it did not.
            # Stamping the author here would give one agent two labels on
            # the same run — its own name on the artifact, ``root`` on the
            # calls. Any other label is the remote's own naming and goes
            # through ``scoped_actor``: a sub-agent the remote happens to call
            # ``root`` must not land on the label reserved for the top-level
            # agent, which is the one thing this field has to keep straight.
            actor=None if label == root else scoped_actor(label),
        )
        folded[key] = entry
        trajectory.append(entry)

    return delegated


def _apply_a2a_attribution(trajectory: Sequence[ToolCall], delegated: bool) -> None:
    """Resolve the run's attribution once the whole stream has been folded.

    Attribution is all-or-nothing per run, and the decision cannot be made until
    the last event is in: a delegating artifact on the final event still has to
    attribute calls folded from the first. So ``actor`` is stamped provisionally
    while folding and reconciled here.

    With no delegation the field is *cleared* rather than defaulted to
    ``ROOT_ACTOR``, so the entry serializes exactly as it did before this
    existed. The judge re-reads the serialized trajectory, so stamping a key on
    every entry of every single-agent run would move scores that have no fleet
    to describe.
    """
    for entry in trajectory:
        if not delegated:
            entry.actor = None
        elif entry.actor is None:
            entry.actor = ROOT_ACTOR


def _response_text(response: Any) -> tuple[str, bool]:
    """Render a tool response as trajectory text and report whether it failed.

    Three response shapes reach here:

    * an MCP tool result — ``{"content": [{"type": "text", ...}], "isError": ...}``,
      rendered as the concatenated text blocks so the trajectory stays readable
      instead of carrying the envelope,
    * ADK's error convention — a mapping carrying a truthy ``error`` key,
    * any other tool return value, rendered as sorted JSON.

    Args:
        response: The ``function_response.response`` payload.

    Returns:
        A ``(text, is_error)`` tuple.
    """
    if not isinstance(response, Mapping):
        return ("" if response is None else str(response)), False

    is_error = bool(response.get("isError")) or bool(response.get("error"))

    blocks = response.get("content")
    if isinstance(blocks, list):
        texts = [
            block["text"]
            for block in blocks
            if isinstance(block, Mapping) and isinstance(block.get("text"), str)
        ]
        if texts:
            return "\n".join(texts), is_error

    return json.dumps(response, sort_keys=True, default=str), is_error


def _accumulate_usage(usage: Any, sums: dict[str, int], seen: set[str]) -> None:
    """Add one event's ``usage_metadata`` into the running per-bucket sums.

    ADK reports usage per LLM call, so a multi-turn tool loop emits one block
    per turn and the run total is their sum. A field absent from every block
    stays out of ``seen`` and is reported as ``None`` (unavailable) rather than
    a fabricated zero.
    """
    if not isinstance(usage, Mapping):
        return
    for field, slot in _USAGE_FIELDS.items():
        count = _int_or_none(usage.get(field))
        if count is None:
            continue
        sums[slot] = sums.get(slot, 0) + count
        seen.add(slot)


def _canonical_tokens(sums: Mapping[str, int], seen: set[str]) -> dict[str, int | None]:
    """Map the accumulated genai counts onto the canonical token buckets.

    genai reports ``prompt_token_count`` as the *full* prompt with the cached
    read as a subset of it, while the canonical ``input`` bucket excludes the
    cached portion — so ``input`` is the difference, clamped at ``0`` so an
    over-reported cache read can never produce a negative bucket. ``cache_write``
    has no genai counterpart and stays ``None``.
    """
    tokens = empty_tokens()
    if not seen:
        return tokens

    prompt = sums.get("prompt") if "prompt" in seen else None
    cached = sums.get("cached") if "cached" in seen else None
    inp = max(prompt - cached, 0) if prompt is not None and cached is not None else prompt

    tokens.update(
        input=inp,
        cached=cached,
        reasoning=sums.get("reasoning") if "reasoning" in seen else None,
        output=sums.get("output") if "output" in seen else None,
        total=sums.get("total") if "total" in seen else None,
    )
    return tokens


def parse_event_stream(
    events: Sequence[Any],
) -> tuple[str, list[dict], dict[str, int | None], list[str]]:
    """Fold a serialized ADK event stream into the canonical result shape.

    The parser is lenient by design — an unrecognized part shape is skipped
    rather than guessed at — but it never drops a signal silently: an event
    carrying ADK's ``error_code`` / ``error_message`` and a tool response
    matching no call both land on the returned ``errors`` list.

    Partial events (ADK's streaming chunks) and thought parts are excluded from
    the output text: the former would duplicate the text they are chunks of, and
    the latter is reasoning the judge should not grade as an answer.

    An event carrying an A2A task envelope is read from the envelope instead: its
    ``status.message`` is the remote agent's answer, and a terminal state other
    than completed lands on ``errors``. Artifacts the remote tagged with a
    producing sub-agent become attributed trajectory entries; if none name a
    producer other than the remote itself, the run is reported unattributed and
    serializes exactly as it did before attribution existed.

    Args:
        events: Serialized ``Event`` mappings in the order ADK yielded them.

    Returns:
        A ``(output, trajectory, tokens, errors)`` tuple. ``trajectory`` is a
        list of ``ToolCall.to_dict()`` mappings in call order; a call whose
        result never arrived stays ``status="called"`` with ``result=None``.
    """
    output_parts: list[str] = []
    errors: list[str] = []
    trajectory: list[ToolCall] = []
    # Calls still awaiting a result: keyed by ADK's correlation id, with a FIFO
    # queue for the id-less calls some models emit.
    pending_by_id: dict[str, ToolCall] = {}
    pending_unkeyed: deque[ToolCall] = deque()
    sums: dict[str, int] = {}
    seen: set[str] = set()
    # Whether any remote task reported an artifact from a sub-agent. Resolved
    # into ``actor`` once the stream is exhausted; see _apply_a2a_attribution.
    delegated = False
    # Artifacts already folded, so a task's later snapshots do not re-report the
    # sub-agents its earlier ones already did; see _artifact_key.
    folded_artifacts: dict[tuple, ToolCall] = {}

    for index, event in enumerate(events):
        if not isinstance(event, Mapping):
            errors.append(f"event {index}: unexpected type {type(event).__name__}")
            continue

        code = event.get("error_code")
        message = event.get("error_message")
        if code or message:
            errors.append(
                f"event {index} reported {code or 'an error'}: {message or '<no detail>'}"
            )

        _accumulate_usage(event.get("usage_metadata"), sums, seen)

        partial = bool(event.get("partial"))
        user_content = _is_user_content(event)

        # A remote agent's answer is the A2A task's status message. Take it in
        # place of the event's own text, which mirrors the trailing artifact.
        a2a_text: str | None = None
        a2a_failed = False
        task = _a2a_task(event)
        if task is not None:
            state, status_text = _a2a_status(task)
            if state in _A2A_FAILURE_STATES:
                # A failed task has no answer, and neither half of the event is
                # a stand-in for one: the status message is a failure notice,
                # and the content parts mirror the trailing artifact. The record
                # is still written as ``status: "success"`` and scored, so
                # whichever one reaches ``output`` gets graded as the response.
                # Report the failure and contribute nothing.
                a2a_failed = True
                errors.append(f"event {index}: remote A2A task {state}")
            elif state == _A2A_ANSWER_STATE:
                # Leaving ``a2a_text`` unset on a non-answer state also lets the
                # event's own text fall through as before, rather than being
                # suppressed in favour of the progress note that displaced it.
                # A completed task with no status message falls through the same
                # way: the artifact is then the only text there is.
                a2a_text = status_text
            if a2a_text and not partial:
                output_parts.append(a2a_text)
            # Fold the fleet the remote ran behind the boundary. Deliberately
            # not gated on the state, and deduplicated across the task's
            # snapshots: see _fold_a2a_artifacts.
            if _fold_a2a_artifacts(task, event.get("author"), trajectory, folded_artifacts):
                delegated = True

        for part in _parts(event):
            if not isinstance(part, Mapping):
                continue

            call = part.get("function_call")
            if isinstance(call, Mapping):
                args = call.get("args")
                entry = ToolCall(
                    name=str(call.get("name", "")),
                    args=dict(args) if isinstance(args, Mapping) else {},
                )
                trajectory.append(entry)
                call_id = call.get("id")
                if call_id is None:
                    pending_unkeyed.append(entry)
                else:
                    pending_by_id[str(call_id)] = entry
                continue

            response = part.get("function_response")
            if isinstance(response, Mapping):
                _fold_response(response, pending_by_id, pending_unkeyed, errors, index)
                continue

            # ``thought`` marks a reasoning part: it is not the answer.
            text = part.get("text")
            if (
                isinstance(text, str)
                and text
                and not partial
                and not user_content
                and not part.get("thought")
                and a2a_text is None
                and not a2a_failed
            ):
                output_parts.append(text)

    output = "".join(output_parts)
    tokens = _canonical_tokens(sums, seen)
    _apply_a2a_attribution(trajectory, delegated)
    return output, [entry.to_dict() for entry in trajectory], tokens, errors


def _fold_response(
    response: Mapping[str, Any],
    pending_by_id: dict[str, ToolCall],
    pending_unkeyed: deque[ToolCall],
    errors: list[str],
    index: int,
) -> None:
    """Attach one ``function_response`` to the call it answers.

    Matching is by ADK's correlation ``id``; a response with no id is paired
    with the oldest id-less call still awaiting a result, which is exact for a
    stream that answers calls in the order they were made. A response matching
    nothing is reported on ``errors`` rather than dropped.
    """
    call_id = response.get("id")
    entry: ToolCall | None = None
    if call_id is not None:
        entry = pending_by_id.pop(str(call_id), None)
    elif pending_unkeyed:
        entry = pending_unkeyed.popleft()

    if entry is None:
        name = response.get("name") or "<unnamed>"
        errors.append(
            f"event {index}: tool response for {name!r} (id={call_id!r}) matched no pending call"
        )
        return

    entry.result, is_error = _response_text(response.get("response"))
    entry.status = "error" if is_error else "completed"
