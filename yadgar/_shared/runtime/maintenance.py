"""The maintenance-window envelope — one builder, three consumers.

Leaf module: reads ``yadgar._shared.runtime.state`` and nothing else, so both
``core/server/_app.py`` (the MCP write-gate) and ``core/server/http.py``
(``/health``) can import it without either importing the other.

WHY THIS EXISTS AS A MODULE.  The gate payload used to be a two-key literal built
inside a closure in a decorator factory, which meant (a) nothing could import it,
so its only test was a *source grep* for the exact strings, and (b) ``/health``
could not reuse it and so stayed blind to the gate entirely.  Both of those are
fixed by the payload having a name.

WHAT THE PAYLOAD IS FOR.  Measured on the 2026-08-20 21:00 vacuum: the gate
engaged at 21:00:01, the swap artifacts appeared at 21:06:07, the ``.old-`` dirs
cleared at 21:07:04 — and the backend needed a further ~30s to come up AFTER the
gate lifted.  So "retry shortly" understated the wait by two orders of magnitude,
and there is a SECOND window in which the gate is gone and the daemon still
cannot serve.  Worse, all four signals an instance naturally checks to confirm a
vacuum is live report that no vacuum is running (see ``_MISLEADING_SIGNALS``).
An instance that trusts them concludes the gate is wedged and files a false
production bug.  The prose below exists to pre-empt exactly that.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import yadgar._shared.runtime.state as _st
from yadgar._shared.observability.observe import observe

# Measured window: gate 21:00:01 -> `.old-` dirs cleared 21:07:04 (423s), plus
# ~30s of backend warm-up after the gate lifts. Rounded UP to 10 minutes so the
# typical case reads as "still normal" rather than "already overdue"; a genuinely
# stuck window is caught by ``looks_stuck`` below, not by this number. Also the
# fallback for an operation nobody has sized.
TYPICAL_DURATION_SECONDS = 600

# PER-OPERATION, because the windows differ by an order of magnitude and a single
# figure makes the envelope lie about the long one. Nightly holds the gate across
# steps 1-7 (backup + consolidation + vacuum + backup + prune) and
# ``_maintenance_entered_at`` is stamped by the OUTER enter, so elapsed spans the
# WHOLE cycle: at 600s a healthy nightly would read `looks_stuck: true` half an
# hour in, and `resume` would open by telling the caller to stop and report a
# broken system. That is precisely the class of false signal this envelope exists
# to delete, so it must not be reintroduced by the envelope itself.
#
# PROVENANCE, because these numbers are not equally solid: 600 for vacuum is
# MEASURED (the 2026-08-20 run above). 3600 for nightly is DERIVED, not measured
# — sized so ``looks_stuck`` fires at 3h, comfortably past a healthy night and
# comfortably before ``_NIGHTLY_MAINTENANCE_TTL_SEC``'s 6h self-heal backstop,
# which its own comment sizes as "long enough that a healthy-but-slow night never
# trips it". Replace it with a real measurement when one exists.
_TYPICAL_DURATION_BY_OPERATION = {
    "vacuum": TYPICAL_DURATION_SECONDS,
    "nightly": 3600,
}

# ``looks_stuck`` fires past this many typical durations. Deliberately BELOW the
# matching self-heal TTL in every case (vacuum: 30 min against
# ``MAINTENANCE_TTL_SEC``'s 2400s; nightly: 3h against 21600s): the instance
# should stop retrying and tell the user before the TTL fires, not after.
STUCK_MULTIPLIER = 3

# The gate lifting is NOT the end — the backend takes ~30s more to come up, and a
# retry landing in that warm-up hits a DIFFERENT failure that reads as a new
# problem. A flat 60s provably clears it. Deliberately NOT a curve: a curve is
# more code and cannot be tighter than the warm-up floor anyway.
RETRY_AFTER_SECONDS = 60

# Rendered when no caller labelled the window. The pre-car message hardcoded
# "(vacuum)", but nightly and the backup quiesce engage the same gate — an
# unlabelled window must stay neutral rather than name the wrong job.
DEFAULT_OPERATION = "maintenance"

# Every one of these reported "no vacuum is running" DURING a live vacuum on
# 2026-08-20. They are named in the message because an instance WILL check them.
_MISLEADING_SIGNALS = (
    "- `GET /health` returns `status: ok` throughout a maintenance window. Its new "
    "`maintenance` block is the only part of it that knows about this gate.\n"
    "- `yadgar-vacuum.service` reads `inactive (dead)` for a manually- or "
    "trigger-file-initiated vacuum, and `systemctl --user list-timers` points at a "
    "next run days away. Neither is a signal for this gate.\n"
    "- `state/yadgar/triggers/` is empty — the trigger file is consumed at start.\n"
    "- On-disk artifacts (`surreal_db.pre-vacuum-*`, `vacuum_export_*`) do not "
    "appear until roughly halfway through the run."
)


class MaintenanceGateError(RuntimeError):
    """Raised by the MCP-registered tool wrapper while the write-gate is engaged.

    RAISED rather than returned, and that asymmetry is the whole point.  The MCP
    SDK derives an output model from each tool's RETURN ANNOTATION and validates
    the returned value against it, so a returned envelope dict is a pydantic
    crash for every ``-> list[dict]`` tool (``type=list_type``, nine of them) and
    every ``-> str`` tool (``type=string_type``, one), and gets silently nested
    under ``{"result": ...}`` for every ``-> dict | None`` tool.  There is no
    single returned VALUE that satisfies all five annotation shapes, and the ones
    that could be synthesised are worse than the crash — a ``[envelope]`` for
    ``recall`` reports SUCCESS and injects an error dict into a result list.

    A raise is annotation-independent: it is uniform over all 95 registered tools
    and over every annotation a future tool might carry.  ``str(self)`` therefore
    has to carry everything, because a ``ToolError`` reaches the caller as text
    only — the prose first, then the full envelope as parseable JSON.

    The SYNC wrapper (the direct-call contract used by internal and test callers)
    still RETURNS the dict: nothing but FastMCP ever holds the async wrapper, so
    the split breaks no caller.
    """

    def __init__(self, envelope: dict) -> None:
        self.envelope = envelope
        # Prose first (it is what a reader acts on), then the machine-readable
        # fields. The two prose keys are dropped from the JSON tail rather than
        # repeated — a gated call already costs ~2k chars of context and every
        # tool call during the window pays it.
        fields = {k: v for k, v in envelope.items() if k not in ("message", "resume")}
        super().__init__(f"{envelope['message']}\n\n{envelope['resume']}\n\n{json.dumps(fields)}")


@observe(tier="stage")
def reset_maintenance_state() -> None:
    """Clear every gate variable. The one place that knows the full set."""
    _st._maintenance_mode = False
    _st._maintenance_deadline = None
    _st._maintenance_entered_at = None
    _st._maintenance_operation = None
    _st._maintenance_phase = None


def maintenance_expired(now: float | None = None) -> bool:
    """True when a window is open but its self-heal deadline has passed.

    Read-only: clearing the flag is the gate's job (it logs LOUDLY, because a
    fired TTL means a vacuum died without running its cleanup).  ``/health`` uses
    this so it does not report an already-open gate as an active window.
    """
    deadline = _st._maintenance_deadline
    return deadline is not None and (now if now is not None else time.monotonic()) >= deadline


def maintenance_active() -> bool:
    """True when the gate is engaged AND its deadline has not expired."""
    return bool(_st._maintenance_mode) and not maintenance_expired()


@observe(tier="stage")
def _elapsed_seconds() -> int | None:
    """Whole seconds the CURRENT window has been held, or None if unknown.

    None is a real answer: a caller that flips ``_maintenance_mode`` directly
    (legacy callers, tests) leaves no entered-at, and the envelope must degrade
    rather than raise inside the gate that is meant to be the safe path.
    """
    entered_at = _st._maintenance_entered_at
    if entered_at is None:
        return None
    return max(0, int(time.monotonic() - entered_at))


@observe(tier="stage")
def build_maintenance_envelope() -> dict:
    """The full envelope: what is happening, for how long, and what to do next."""
    operation = _st._maintenance_operation or DEFAULT_OPERATION
    phase = _st._maintenance_phase
    elapsed = _elapsed_seconds()
    typical = typical_duration_seconds(operation)

    started_at = expected_done_by = None
    if elapsed is not None:
        started = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=elapsed)
        started_at = started.isoformat()
        expected_done_by = (started + timedelta(seconds=typical)).isoformat()

    looks_stuck = elapsed is not None and elapsed > typical * STUCK_MULTIPLIER

    return {
        "error": "maintenance",
        "operation": operation,
        "phase": phase,
        "started_at": started_at,
        "elapsed_seconds": elapsed,
        "typical_duration_seconds": typical,
        "expected_done_by": expected_done_by,
        "looks_stuck": looks_stuck,
        "retry_after_seconds": RETRY_AFTER_SECONDS,
        "writes_were_rejected_not_queued": True,
        "message": _message(operation, phase, elapsed, typical),
        "resume": _resume(looks_stuck),
    }


@observe(tier="stage")
def typical_duration_seconds(operation: str) -> int:
    """How long ``operation``'s window usually runs. See the table's provenance."""
    return _TYPICAL_DURATION_BY_OPERATION.get(operation, TYPICAL_DURATION_SECONDS)


@observe(tier="stage")
def _human_elapsed(elapsed: int | None) -> str:
    if elapsed is None:
        return "an unknown time"
    return f"{elapsed // 60}m{elapsed % 60:02d}s"


def _message(operation: str, phase: str | None, elapsed: int | None, typical: int) -> str:
    phase_clause = f", phase {phase!r}" if phase else ""
    return (
        f"yadgar is in a maintenance window ({operation}){phase_clause}, "
        f"{_human_elapsed(elapsed)} elapsed of a typical "
        f"~{typical // 60}m. ALL DB-backed tools are gated, "
        f"including read-only ones (`db_inspect`, `recall`, `wiki_read`).\n\n"
        # Deliberately "this window", not "a vacuum": nightly and backup engage
        # the same gate, and naming the wrong job is the defect this replaces.
        # The signals below stay vacuum-shaped because they are the ones that
        # lie, and nightly's step 4 runs a vacuum anyway.
        f"DO NOT go looking for evidence that this window is real — you will not "
        f"find it, and you will conclude this gate is stuck when it is not:\n"
        f"{_MISLEADING_SIGNALS}\n\n"
        f"The ONLY reliable signal that the window is over is a DB-backed tool "
        f"call that succeeds. Sleep `retry_after_seconds` "
        f"({RETRY_AFTER_SECONDS}s) and retry the same call — that interval also "
        f"clears the ~30s the backend needs to come up AFTER the gate lifts, "
        f"which is a second window where calls fail for a different reason."
    )


def _resume(looks_stuck: bool) -> str:
    stuck_clause = (
        "This window has now run past 3x its typical duration (`looks_stuck: true`): "
        "STOP retrying and report it to the user. Do not start debugging the daemon.\n\n"
        if looks_stuck
        else ""
    )
    return (
        f"{stuck_clause}"
        f"Writes attempted during this window were REJECTED, not queued — nothing "
        f"you tried to write while gated has landed, and nothing will land later "
        f"on its own. (Confirmed empirically: a `task_write` inside the window "
        f"consumed no id.) Writes committed BEFORE the window are safe.\n\n"
        f"To get back to where you were:\n"
        f"1. Sleep `retry_after_seconds`, retry the failed call. Repeat until it "
        f"succeeds.\n"
        f"2. Re-issue every write you attempted during the window, in order. "
        f"Assume none landed.\n"
        f"3. Verify each one by RE-READING the row (`db_inspect` / `wiki_read`), "
        f"not by trusting the tool's success field.\n"
        f"4. If `elapsed_seconds` exceeds 3x `typical_duration_seconds` "
        f"(`looks_stuck: true`), stop retrying and report to the user."
    )


@observe(tier="stage")
def apply_maintenance_health(payload: dict) -> None:
    """Fold the maintenance window into the ``/health`` payload (Car 1, piece C).

    ADDITIVE ONLY, and that is load-bearing: ``health_check`` returns 503 on any
    non-ok ``status`` and P0 watches this endpoint, so degrading here would turn
    every nightly into a 503 and potentially a health-kill.  The window is
    reported, never scored.

    Same never-raises discipline as ``_apply_tool_pool_health`` — health must not
    crash on a state read.
    """
    try:
        if not maintenance_active():
            return
        payload["maintenance"] = {
            "active": True,
            "operation": _st._maintenance_operation or DEFAULT_OPERATION,
            "phase": _st._maintenance_phase,
            "elapsed_seconds": _elapsed_seconds(),
        }
    except Exception:  # noqa: BLE001 — /health must survive a bad state read
        pass
