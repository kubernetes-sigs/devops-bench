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

"""``mode: hold`` sampling: two drivers sharing one fold and one verdict.

``mode: hold`` means a condition must hold continuously over some window,
rather than being checked once at a single point in time. What that window
is, and when it must be observed, differs by role, so this module provides
two drivers instead of one:

* :class:`SafeguardMonitor` drives a **safeguard** hold: the window is the
  agent's turn, and the property must never break. It runs on a daemon
  thread started before the agent's turn and drained after it, sampling
  concurrently with the agent so a violation the agent commits and then
  undoes before the run ends is still observed. Verification that only runs
  AFTER the agent exits (the ``assert`` mode's single evaluation) cannot see
  that: an agent that scales a deployment down and back up between two
  ``kubectl`` calls has its violation read as healthy if the only
  observation happens once, at the end, after the replica count has already
  recovered. That is the motivating failure this driver closes.
* :func:`run_hold_window` drives an **objective** hold: the window is an
  explicit post-run soak, sampled synchronously after the agent's turn ends.
  An objective starts false and must become true and then stay true;
  sampling it live, during the agent's turn, would latch a violation before
  the agent has done anything. Running an objective through the live monitor
  is a bug, not a feature: it is not a different way to check the same
  thing, it is checking the wrong window.

Both drivers fold every sample through the same :func:`_fold_sample` into a
:class:`HoldObservation`, and both outcomes are scored through the same
:func:`hold_verdict`, so a hold entry's pass/fail/error rule is defined
exactly once regardless of which driver produced its samples.

:class:`SafeguardMonitor` is modeled on
:class:`~devops_bench.evalharness.scenario.ScenarioManager`: it runs on a
daemon thread, writes into a lock-guarded observation table, and never lets
an internal failure propagate out to the run.

FIDELITY LIMIT. This is sampling, not a watch: a violation that starts and
ends entirely between two samples is never observed. The poll interval is the
tunable that trades that blind spot against load on the API server /
``kubectl`` subprocess overhead; it is not, and must not be sold as, a
continuous guarantee. A ``kubectl get --watch`` (or native Kubernetes watch
API) based implementation would close the gap by observing every change
event rather than sampling at fixed points in time, but that is a different
and larger piece of work and is not built here.
"""

from __future__ import annotations

import copy
import math
import os
import threading
import time
from dataclasses import dataclass

from devops_bench.core import get_logger
from devops_bench.verification import VerificationEntry, VerificationResult, VerifierAgent

__all__ = [
    "HOLD_POLL_INTERVAL_SEC",
    "HoldObservation",
    "SafeguardMonitor",
    "hold_verdict",
    "run_hold_window",
]

_log = get_logger("evalharness.hold")


def _default_poll_interval() -> float:
    """Parse ``BENCH_HOLD_INTERVAL_SEC`` as a finite float greater than zero.

    Falls back to ``5.0`` when the variable is unset. Also falls back to
    ``5.0``, logging a warning naming the variable and its offending value,
    when the variable is set but is not a finite positive number, so a bad
    override degrades to the safe default instead of raising a bare
    ``ValueError`` deep inside module import or letting a zero/negative value
    make the scheduler spin without sleeping.
    """
    raw = os.environ.get("BENCH_HOLD_INTERVAL_SEC")
    if raw is None:
        return 5.0
    try:
        value = float(raw)
    except ValueError:
        _log.warning("BENCH_HOLD_INTERVAL_SEC=%r is not a valid number; falling back to 5.0", raw)
        return 5.0
    if not math.isfinite(value) or value <= 0:
        _log.warning(
            "BENCH_HOLD_INTERVAL_SEC=%r must be a finite number greater than zero; "
            "falling back to 5.0",
            raw,
        )
        return 5.0
    return value


# Default seconds between samples for a hold entry that does not set its own
# ``hold_poll_interval_sec``. Overridable via BENCH_HOLD_INTERVAL_SEC, a
# module-level tunable in the same style as VERIFICATION_TIMEOUT_SEC /
# VERIFICATION_TOTAL_BUDGET_SEC in devops_bench.evalharness.scenario (those
# two are plain constants, not env-overridable).
HOLD_POLL_INTERVAL_SEC = _default_poll_interval()

# Consecutive errored samples required at the end of an observation window
# before hold_verdict() reports "error" instead of "pass". One errored
# sample at the end of a window is treated as observation noise: a single
# transient kubectl blip is common, and the entry may well still have been
# holding. A run of this many consecutive errors ending the window means
# observation was actually lost and never regained, which is not a pass.
# Set above 1 because a downstream objective reporting "error" nulls its
# whole task's correctness (not just that entry's contribution), so a
# single sample is too sensitive a trigger for that amplified cost.
HOLD_TRAILING_ERROR_SAMPLES = 2

# Upper bound on how long the monitor's own scheduling loop sleeps between
# checking which entries are due for a sample. Bounds how long stop() can
# take to be noticed: the loop wakes at least this often even when every
# entry's next sample is further away, so a stop() call is never blocked
# behind a long per-entry interval.
_SCHEDULER_TICK_SEC = 1.0

# Default bound for stop()'s join. A single sample's kubectl call can run up
# to the leaf verifiers' own I/O floor (30s, see
# devops_bench.verification.base.single_call_timeout) before returning, so the
# join budget is set comfortably above that rather than at the poll interval.
_DEFAULT_JOIN_TIMEOUT_SEC = 40.0


@dataclass
class HoldObservation:
    """What the monitor observed for one hold entry.

    Attributes:
        violated: True once any sample was observed to fail. Once set, stays
            set: a later sample recovering does not clear it, since a hold
            safeguard is about continuous compliance, not the value at the
            end.
        first_violation_reason: The failing sample's ``reason``, captured the
            first time ``violated`` is set. ``None`` until then.
        first_violation_at_sec: Seconds after the monitor started that the
            first violation was observed, via ``time.monotonic()``. ``None``
            until a violation is observed.
        sample_count: Total number of samples taken (pass, fail, or error).
        error_count: Of ``sample_count``, how many could not be evaluated
            (the check itself failed to run, as distinct from running and
            observing the condition false). Never counted as a violation.
        last_sample_status: The most recent sample's ``status`` (e.g.
            ``"pass"``, ``"fail"``, ``"error"``). ``None`` until a sample is
            taken. Kept for report/debug value; :func:`hold_verdict` keys off
            ``trailing_error_count`` instead, since a single trailing error
            is noise and only a sustained run at the end of the window means
            the entry was never actually observed.
        trailing_error_count: How many consecutive samples ending at the
            most recent one have errored. Incremented on an errored sample,
            reset to zero on any non-error sample. Used by
            :func:`hold_verdict` to tell a single error that recovered
            before the window ended (noise) from a sustained run that never
            cleared (the entry was never actually observed).
        last_error_reason: The most recent errored sample's reason (the
            leaf verifier's ``result.reason``, or the stringified exception
            when the sample raised instead of returning a result). ``None``
            until an errored sample is folded in. Lets a sustained-trailing-
            error verdict report what actually went wrong instead of only a
            generic message.
    """

    violated: bool = False
    first_violation_reason: str | None = None
    first_violation_at_sec: float | None = None
    sample_count: int = 0
    error_count: int = 0
    last_sample_status: str | None = None
    trailing_error_count: int = 0
    last_error_reason: str | None = None


def _fold_sample(obs: HoldObservation, result: VerificationResult, elapsed_sec: float) -> None:
    """Fold one sample's result into ``obs``, shared by every hold driver.

    A check that ERRORS (the check could not run: a transient kubectl
    failure, an API server blip, a timeout) is recorded separately from a
    check that ran and reported failure. Only the latter is a violation.
    Getting this backwards would turn a flaky cluster into a failed hold,
    which is worse than the bug hold mode exists to fix.

    Args:
        obs: The observation to update in place.
        result: The single sample's :class:`VerificationResult`.
        elapsed_sec: Seconds into the observation window this sample was
            taken, recorded on the first violation only.
    """
    if result.status == "error":
        _fold_error_sample(obs, result.reason)
        return
    obs.sample_count += 1
    obs.last_sample_status = result.status
    obs.trailing_error_count = 0
    if not result.success and not obs.violated:
        obs.violated = True
        obs.first_violation_reason = result.reason
        obs.first_violation_at_sec = elapsed_sec


def _fold_error_sample(obs: HoldObservation, reason: str | None = None) -> None:
    """Record one sample that could not be evaluated at all.

    Exists so an exception raised while sampling (the check never even ran)
    is folded into ``obs`` identically to an in-band ``status == "error"``
    result from :func:`_fold_sample`. Both drivers must call this rather than
    updating the fields directly, so the two cases can never drift out of
    sync.

    Args:
        obs: The observation to update in place.
        reason: What went wrong on this sample, so a sustained-trailing-error
            verdict can report it instead of only a generic message.
    """
    obs.sample_count += 1
    obs.error_count += 1
    obs.last_sample_status = "error"
    obs.trailing_error_count += 1
    obs.last_error_reason = reason


def hold_verdict(obs: HoldObservation) -> tuple[bool, str, str]:
    """Compute the pass/fail/error verdict for one hold entry's observation.

    Shared by every hold driver so the outcome rule is defined exactly once.
    Checked in this order:

    1. Zero samples: the entry was never observed at all.
    2. Every sample errored: the check never once managed to run, so there
       is nothing to score a pass or fail against.
    3. Violated: a hold that dipped at any point did not hold, regardless
       of whether it later recovered. A confirmed violation is a positive
       observation, and a trailing error sample does not un-observe it: an
       objective reporting "error" nulls a task's correctness entirely
       downstream, while "fail" scores as a fail, so checking violated
       before the trailing-error case keeps a genuine violation from being
       masked by a single error sample at the end of the window. That is
       exactly the masking behavior hold mode exists to prevent.
    4. The window ended on a sustained run of errors (at least
       :data:`HOLD_TRAILING_ERROR_SAMPLES` consecutive): a single error that
       recovers within the window is treated as observation noise (a
       transient kubectl blip), but a run of errors that never clears means
       the entry was never actually observed at the point the window
       closed, and that is not a pass.
    5. Otherwise, pass, noting any absorbed (recovered) errors, including a
       single trailing error too short to trigger rule 4.

    Args:
        obs: The observation to score.

    Returns:
        A ``(success, status, reason)`` triple, matching the vocabulary of
        :class:`~devops_bench.verification.base.VerificationResult`.
    """
    if obs.sample_count == 0:
        return (
            False,
            "error",
            "hold entry was never sampled during its observation window; a hold "
            "nobody watched must not read as one that held",
        )
    if obs.error_count == obs.sample_count:
        return (
            False,
            "error",
            f"every sample ({obs.sample_count}) errored; the entry could never be evaluated",
        )
    if obs.violated:
        reason = (
            f"hold violated {obs.first_violation_at_sec:.1f}s into the observation "
            f"window: {obs.first_violation_reason}"
        )
        return False, "fail", reason
    if obs.trailing_error_count >= HOLD_TRAILING_ERROR_SAMPLES:
        return (
            False,
            "error",
            f"the observation window ended on {obs.trailing_error_count} consecutive "
            "unevaluable samples (it never recovered), so the entry was never "
            "actually observed; last error reason: "
            f"{obs.last_error_reason or 'no error reason provided'}",
        )
    reason = f"held for {obs.sample_count} sample(s) across the observation window"
    if obs.error_count > 0:
        reason += f" ({obs.error_count} sample(s) could not be evaluated)"
    return True, "pass", reason


class SafeguardMonitor:
    """Sample hold-mode safeguards on a daemon thread while the agent runs.

    Constructed with the subset of a task's :class:`VerificationEntry` objects
    whose ``resolved_mode == "hold"``, already pinned to the run's cluster
    (see ``_pin_verification_targets`` in ``devops_bench.evalharness.default``).
    :meth:`start` spawns the sampling thread; :meth:`stop` signals it to exit
    and joins with a bounded timeout; :meth:`get_observations` returns a
    locked snapshot, safe to call before or after :meth:`stop`.

    Each entry is sampled independently on its own interval (its own
    ``hold_poll_interval_sec``, or :data:`HOLD_POLL_INTERVAL_SEC` when unset),
    all from a single scheduling thread rather than one thread per entry.

    Args:
        entries: The task's hold-mode entries. An empty list is accepted;
            :meth:`start` is then a no-op and every method behaves as if no
            monitoring ever happened.
    """

    def __init__(self, entries: list[VerificationEntry]) -> None:
        self._entries: list[VerificationEntry] = list(entries)
        self._agent = VerifierAgent()
        self._observations: dict[str, HoldObservation] = {
            entry.name: HoldObservation() for entry in self._entries
        }
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_time: float | None = None

    def start(self) -> None:
        """Start the background sampling thread.

        A no-op when there are no hold entries to watch, so callers do not
        need to special-case an empty list.
        """
        if not self._entries:
            return
        self._start_time = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True, name="safeguard-monitor")
        self._thread.start()

    def stop(self, join_timeout_sec: float = _DEFAULT_JOIN_TIMEOUT_SEC) -> None:
        """Signal the sampling thread to exit and join it with a bounded timeout.

        Safe to call more than once, and safe to call even when :meth:`start`
        was never called (or was a no-op). Never raises, so it can run from a
        ``finally`` block during task teardown.

        Args:
            join_timeout_sec: Maximum seconds to wait for the thread to exit.
                A join that times out is logged, not raised; the thread is a
                daemon, so it cannot leak the process.
        """
        self._stop_event.set()
        if self._thread is None:
            return
        self._thread.join(timeout=join_timeout_sec)
        if self._thread.is_alive():
            _log.warning(
                "safeguard monitor thread still alive after %ss join budget; "
                "abandoning it (it is a daemon thread and cannot leak the process)",
                join_timeout_sec,
            )

    def get_observations(self) -> dict[str, HoldObservation]:
        """Return a locked snapshot of every entry's observation so far.

        Safe to call while the thread is still running, or after :meth:`stop`.

        Returns:
            A name-keyed copy of the current observations; mutating the
            returned dict or its values does not affect the monitor's own
            state.
        """
        with self._lock:
            return {name: copy.copy(obs) for name, obs in self._observations.items()}

    def _run(self) -> None:
        """Scheduling loop: sample every entry that is due, then sleep to the next one.

        Any exception escaping a single entry's sample is caught inside
        :meth:`_sample_one`; this loop additionally wraps the whole pass so a
        bug in the scheduling logic itself (not just in one entry's sample)
        cannot kill the thread either. A monitor bug must never take down the
        task run.
        """
        next_due: dict[str, float] = dict.fromkeys((e.name for e in self._entries), 0.0)
        while not self._stop_event.is_set():
            try:
                now = time.monotonic()
                soonest = None
                for entry in self._entries:
                    if now >= next_due[entry.name]:
                        self._sample_one(entry)
                        interval = self._interval_for(entry)
                        next_due[entry.name] = time.monotonic() + interval
                    due_at = next_due[entry.name]
                    if soonest is None or due_at < soonest:
                        soonest = due_at
                sleep_for = _SCHEDULER_TICK_SEC
                if soonest is not None:
                    sleep_for = min(sleep_for, max(0.0, soonest - time.monotonic()))
                self._stop_event.wait(sleep_for)
            except Exception:  # noqa: BLE001 - a monitor bug must not kill the run
                _log.exception("safeguard monitor scheduling loop hit an unexpected error")
                self._stop_event.wait(_SCHEDULER_TICK_SEC)

    @staticmethod
    def _interval_for(entry: VerificationEntry) -> float:
        """Resolve one entry's poll interval: its own, else the module default."""
        if entry.hold_poll_interval_sec is not None:
            return entry.hold_poll_interval_sec
        return HOLD_POLL_INTERVAL_SEC

    def _sample_one(self, entry: VerificationEntry) -> None:
        """Evaluate one entry once and fold the outcome into its observation.

        Any exception raised while evaluating (a bug in a leaf verifier, an
        unexpected error in the runner) is caught here and folded in as an
        error sample, not a violation, and never propagates.
        """
        elapsed = time.monotonic() - self._start_time if self._start_time is not None else 0.0
        try:
            result = self._agent.run_entry(entry, timeout_sec=0.0)
        except Exception as exc:  # noqa: BLE001 - see docstring: never propagate
            _log.warning("safeguard monitor: sampling %r raised: %s", entry.name, exc)
            with self._lock:
                obs = self._observations[entry.name]
                _fold_error_sample(obs, str(exc))
            return

        with self._lock:
            obs = self._observations[entry.name]
            _fold_sample(obs, result, elapsed)


def run_hold_window(
    entry: VerificationEntry,
    window_sec: float,
    *,
    interval_sec: float,
    deadline: float,
) -> HoldObservation:
    """Synchronously soak-sample ``entry`` for an objective-role hold window.

    An objective starts false and must become true and stay true; that
    cannot be observed live during the agent's turn (the first sample would
    fail before the agent has done anything and latch a permanent
    violation). This runs after the agent's turn ends instead, sampling
    ``entry`` on the caller's own thread in a blocking loop for up to
    ``window_sec``, folding every sample through the same :func:`_fold_sample`
    :class:`SafeguardMonitor` uses.

    ``deadline`` is an absolute ``time.monotonic()`` value bounding the
    caller's whole post-run verification pass (see
    ``VERIFICATION_TOTAL_BUDGET_SEC`` in
    ``devops_bench.evalharness.scenario``). The window stops at whichever of
    ``window_sec`` or ``deadline`` is sooner, so one entry's soak can never
    overrun the shared budget the rest of the task's verification draws
    from.

    On a violation, sampling does NOT stop early. It keeps sampling to the
    end of the window so the report can show whether the entry recovered.
    The verdict is still a fail either way: a hold that dipped at any point
    did not hold, so continuing cannot turn a fail into a pass, it only adds
    detail (and matches how :class:`SafeguardMonitor` already behaves across
    the agent's turn).

    Args:
        entry: The objective-role, hold-mode entry to sample.
        window_sec: How long to sample for, in seconds.
        interval_sec: Seconds to sleep between samples.
        deadline: Absolute ``time.monotonic()`` deadline for the caller's
            whole post-run verification pass; the window stops early if this
            is reached before ``window_sec`` has elapsed.

    Returns:
        The resulting :class:`HoldObservation`, ready for :func:`hold_verdict`.
    """
    obs = HoldObservation()
    agent = VerifierAgent()
    start = time.monotonic()
    window_deadline = min(start + window_sec, deadline)

    while time.monotonic() < window_deadline:
        elapsed = time.monotonic() - start
        try:
            result = agent.run_entry(entry, timeout_sec=0.0)
        except Exception as exc:  # noqa: BLE001 - a hold driver bug must not sink the run
            _log.warning("hold window: sampling %r raised: %s", entry.name, exc)
            _fold_error_sample(obs, str(exc))
        else:
            _fold_sample(obs, result, elapsed)

        remaining = window_deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval_sec, remaining))

    return obs
