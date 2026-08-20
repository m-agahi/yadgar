"""Cross-engine backup quiesce — ADR-0204 as amended by ADR-0210 and ADR-0211.

    assert the gate -> VERIFIED-EMPTY drain -> snapshot MariaDB -> snapshot
    Surreal -> release.

WHY THE ORDER IS THE MECHANISM
------------------------------
References run ONE WAY: a MariaDB row points at a Surreal body page via
``body_slug``; no Surreal row points back (ADR-0204 context). Restoring a
Surreal snapshot from 03:00 beside a SQL snapshot from 03:05 yields rows naming
memories that do not exist. Holding the write-gate across both makes the pair
describe one instant, and draining first is what removes the writes that are
in flight and therefore belong to NEITHER snapshot.

WHY THIS IS A NIGHTLY STEP AND NOT ITS OWN SCHEDULE
---------------------------------------------------
Maintenance windows NEST by design (``core/server/routes/control.py``), so a
second scheduled holder could open a window that overlaps the nightly cycle and
snapshot mid-consolidation-write. Folding the backup into nightly makes that
structurally impossible — one holder, one window — and reuses the outage that
already exists rather than adding a second (ADR-0210 §2). The window IS a full
MCP outage: the gate short-circuits every tool, reads included; ADR-0204's
"reads stay available" claim is withdrawn by ADR-0210.

AN ABSENT ENGINE #2 IS A SKIP, NOT A FAILURE
--------------------------------------------
Before anything else — and specifically BEFORE the gate — this driver asks the
backend whether engine #2 exists at all. If it does not, there is nothing to
quiesce, so there is nothing to fail about: step 5b logs a skip, the nightly
continues, and NO maintenance window is ever opened. The ordering is the point.
A window is a full MCP outage (ADR-0210), so opening one only to discover there
was nothing to back up would impose the whole cost of the arm on every host that
does not have the arm.

That is not hypothetical. Engine #2 ships in the BACKEND IMAGE (ADR-0212 bakes
mariadb-server into ``Dockerfile.backend``) while this driver ships in CORE,
which updates independently. Between the two rollouts every host — production
included — runs a new core against an old backend. Hard-failing there fails the
nightly nightly, for a backup that was never possible.

WHY "ABSENT" AND "PRESENT BUT BROKEN" MUST NOT COLLAPSE
-------------------------------------------------------
Absence disables the backup. So absence is only ever concluded from a POSITIVE
answer given by a REACHABLE backend, of which there are exactly two:

* ``{"present": false}`` — the backend looked at its own composition slot and
  found no engine (``_get_sql_storage`` returns None on core always, and on the
  backend whenever MariaDB did not come up).
* HTTP 400 ``unknown admin op`` — the backend answered, and does not know the
  op. The image that registers ``sql_engine_status`` is the image that bakes
  mariadb-server, so a backend without the op cannot be running engine #2. An
  UNREACHABLE backend cannot return 400; this is evidence, not silence.

Everything else — a connect error, a timeout, a 5xx, any other 400, a body
without ``present`` — is "cannot tell", and cannot-tell HARD-FAILS. A detector
that reported absent on a connect error would silently switch the backup off on
every host whose backend was momentarily down, which is the exact vacuous-pass
shape the three hard failures below exist to prevent.

An engine that is PRESENT BUT BROKEN stays on the hard-fail path by
construction: the probe is a slot read, not a liveness check, so a wedged
MariaDB still answers ``present: true`` and then ``mariadb_dump`` fails loudly
on the real fault.

THREE HARD FAILURES, ALL DELIBERATE
-----------------------------------
Nightly's own gate entry is BEST-EFFORT — it proceeds ungated when core is
unreachable (``nightly_cycle._step_stop_core``) because a degraded maintenance
pass beats losing the whole cycle. A backup must NOT inherit that (ADR-0210 §3):
proceeding ungated yields a snapshot that restores into an inconsistent state,
silently. So:

* gate unobtainable        -> raise, snapshot nothing.
* ``deadline_seconds`` null -> raise, snapshot nothing. Car E returns that field
  precisely so a holder can VERIFY it has a self-heal belt (ADR-0211); null
  means the effective window has no expiry and a SIGKILLed driver would wedge
  every write until a human noticed.
* drain not VERIFIED empty  -> raise, snapshot nothing. ``drain_now`` answers
  ``{"drained": False, "items_processed": 0}`` BOTH when there was nothing to do
  and when no live drainer is wired at all (``backend/admin_exec/drain.py``), so
  ``items_processed == 0`` alone passes vacuously against a backend that cannot
  drain. ``drained is True`` is the half that carries evidence.

THE GATE PRIMITIVE IS NOT TOUCHED (ADR-0211)
--------------------------------------------
A nested enter never shortens the outer window and ``previous`` is the
CALLER-side contract — this driver consumes it exactly as the vacuum does
(``core/vacuum/__init__.py``: ``entered = not _maintenance_enter(...)``), exiting
only a window it opened itself. Nothing in ``control.py`` changes.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from yadgar._shared.observability.observe import observe
from yadgar.core.backup.backup import create_snapshot
from yadgar.core.forward import _forward_admin
from yadgar.core.install.auth_token import resolve_auth_token

_log = logging.getLogger("yadgar.backup.quiesce")

_MAINTENANCE_ENTER_PATH = "/api/control/maintenance/enter"
_MAINTENANCE_EXIT_PATH = "/api/control/maintenance/exit"

# Self-heal deadline for THIS driver's nested window. Sized for the SLOWER of
# the two snapshots (ADR-0204: the window is the slower one, not their overlap)
# with headroom, and deliberately far below the nightly's own 6h so that a
# driver that dies alone still leaves a bounded window if it happened to be the
# outer holder. A nested enter cannot shorten nightly's window either way.
QUIESCE_TTL_SEC = 1800.0

# Drain passes before giving up. A queue that keeps refilling under a held gate
# means a free-running writer, which is precisely the condition under which the
# two snapshots would NOT describe one instant.
MAX_DRAIN_PASSES = 5

# The label for the Surreal half of the pair. Deliberately NOT ``nightly-*``:
# ``_step_prune``'s existing glob is ``surreal_db.nightly-*``, so a quiesced
# artifact sharing that prefix would compete with the pre/post snapshots for one
# retention pool, and car G could not tell a quiesced artifact from an
# unquiesced one when looking for the matched pair.
SURREAL_LABEL = "quiesce"

# The table car D's Alembic chain creates (ADR-0203: schema-only, zero rows).
# Asserting it appears in the dump is what distinguishes "the arm worked" from
# "the arm produced a header" — with zero rows, size alone cannot.
EXPECTED_TABLE = "config"

# The backend admin op that answers "was engine #2 composed in your process?".
# Registered in ``backend/admin_exec/__init__.py``; a backend that does not know
# it predates engine #2 entirely.
ENGINE_STATUS_OP = "sql_engine_status"

# Tri-state, and the third value is a first-class outcome rather than a synonym
# for either of the others — the same stance car H's ``check_invariants`` arm
# takes, and for the same reason: a check that could not run has proven nothing.
ENGINE_PRESENT = "present"
ENGINE_ABSENT = "absent"
ENGINE_UNKNOWN = "unknown"

# Reasons mirror ``backend/admin_exec/invariants_cross_engine.py``'s vocabulary
# BY VALUE and are duplicated here deliberately: core must not import backend
# (pyproject import-linter contract "core and backend must not import each
# other's internals"). Keep the strings in step if that file's ever change.
REASON_ENGINE_TWO_ABSENT = "engine_two_absent"
REASON_BACKEND_PREDATES_ENGINE_TWO = "backend_predates_engine_two"

# Marker in the /admin route's 400 body for an op it does not have. The route
# maps ``KeyError(f"unknown admin op: {op!r}")`` straight into ``detail``, so
# this substring is the contract between the two sides.
_UNKNOWN_OP_MARKER = "unknown admin op"


@observe(tier="stage")
def _core_url() -> str:
    """Base URL of the running core process (the gate lives in core, not the backend)."""
    return os.environ.get("YADGAR_CORE_URL", "http://127.0.0.1:8765").rstrip("/")


@observe(tier="stage")
def _maintenance_post(path: str, body: dict | None = None) -> dict:
    """POST a core maintenance route and return the decoded body. Raises on failure.

    Mirrors ``core/vacuum``'s helper of the same name, including its bearer
    handling. Unlike the vacuum's callers, ours convert a raise into a HARD
    FAILURE rather than warn-and-proceed.
    """
    import httpx  # noqa: PLC0415 — lazy, matching the rest of the host-ops path

    # Car 9: route through the ONE sanctioned bearer-token resolver (env var,
    # else secrets.env) — this is a host-ops path where the env var may not
    # be exported even though secrets.env holds it.
    token = resolve_auth_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = httpx.post(f"{_core_url()}{path}", json=body or {}, headers=headers, timeout=15.0)
    if resp.status_code not in (200, 202):
        raise RuntimeError(f"{path} returned HTTP {resp.status_code}: {resp.text[:200]}")
    payload = resp.json()
    return payload if isinstance(payload, dict) else {}


@observe(tier="stage")
def _maintenance_enter(ttl_seconds: float) -> dict:
    """Engage the core write-gate; return the enter response.

    ``previous`` is the state BEFORE this call — True means an outer holder
    (nightly steps 1-7) owns the window and we must not exit it.
    ``deadline_seconds`` is the EFFECTIVE deadline after nesting resolves, or
    None when the window has no expiry (car E / ADR-0211).
    """
    # Car 1 (2026-08-20 train): ``operation`` labels the window for the gate
    # envelope, and is honoured only when THIS call opens it — nested under
    # nightly (steps 1-7) the outer label stands, which is correct: the window
    # an instance is waiting on is nightly's, not this driver's.
    return _maintenance_post(
        _MAINTENANCE_ENTER_PATH, {"ttl_seconds": ttl_seconds, "operation": "backup"}
    )


@observe(tier="stage")
def _maintenance_exit() -> None:
    """Release the core write-gate."""
    _maintenance_post(_MAINTENANCE_EXIT_PATH)


@observe(tier="stage")
def _release(entered: bool) -> None:
    """Un-gate, but ONLY if this driver opened the window. NEVER raises.

    ``entered`` is False when an outer window was already open — exiting then
    would un-gate the nightly cycle mid-flight. A raise out of a ``finally``
    would also mask the original failure, so this swallows and shouts instead.
    """
    if not entered:
        return
    try:
        _maintenance_exit()
    except Exception as exc:  # noqa: BLE001 — belt must not mask the real failure
        _log.critical(
            "could not release the core write-gate after the cross-engine backup: %s. "
            "Every MCP tool fast-fails until it clears; it self-heals at the TTL, or "
            "POST %s to un-wedge now.",
            exc,
            _MAINTENANCE_EXIT_PATH,
            extra={"component": "backup.quiesce", "action": "release", "outcome": "error"},
        )


@observe(tier="stage")
def _engine_two_state() -> tuple[str, str]:
    """Ask the BACKEND whether engine #2 exists. Returns ``(state, reason)``.

    Asking rather than inferring is forced by the deployment shape, not chosen
    for tidiness: ``client.cnf``'s socket path is container-absolute, a
    host-built ``MariaStorageEngine`` is connectionless and so fails silently,
    and mariadbd runs ``--skip-networking``. Only the backend process knows
    whether ``_init_sql_storage`` composed anything, so only it can answer.

    Needs NO maintenance window — it is one small POST to /admin, which is why
    it can run before the gate.

    ABSENT is returned ONLY on a positive answer from a reachable backend:
    ``present: false``, or a 400 naming the op as unknown (an old image, whose
    absence of the op implies absence of the engine that ships with it). Every
    other outcome is UNKNOWN, which the caller converts into a hard failure.
    Never raises — the tri-state IS the error channel, so a transport fault
    cannot arrive looking like a verdict.
    """
    import httpx  # noqa: PLC0415 — lazy, matching the rest of the host-ops path

    try:
        result = _forward_admin(ENGINE_STATUS_OP, {}, timeout_s=15.0)
    except httpx.HTTPStatusError as exc:
        response = exc.response
        if response.status_code == 400 and _UNKNOWN_OP_MARKER in response.text:
            return ENGINE_ABSENT, REASON_BACKEND_PREDATES_ENGINE_TWO
        # Any other HTTP failure is the backend faulting while answering, which
        # says nothing about engine #2. Widening this branch to every
        # HTTPStatusError would turn a backend fault into a skipped backup.
        return ENGINE_UNKNOWN, f"backend returned HTTP {response.status_code}"
    except Exception as exc:  # noqa: BLE001 — a transport fault is not a verdict
        return ENGINE_UNKNOWN, f"{type(exc).__name__}: {exc}"

    present = result.get("present")
    if present is True:
        return ENGINE_PRESENT, ""
    if present is False:
        return ENGINE_ABSENT, REASON_ENGINE_TWO_ABSENT
    # Tested EXPLICITLY against True/False rather than by truthiness: a body
    # with no ``present`` key would otherwise read as absent, which is deciding
    # to stop backing up on the strength of a key that is not there.
    return ENGINE_UNKNOWN, f"status op returned no usable 'present' field: {result!r}"


@observe(tier="stage")
def _skip_result(reason: str) -> dict[str, Any]:
    """The shape returned when there is no engine #2 to back up.

    Carries NO ``sql_dump``/``surreal_snapshot`` keys on purpose. A skip that
    reported the artifact keys as empty would be indistinguishable from a backup
    that ran and produced nothing — which is the vacuous pass, spelled in the
    return value instead of in the logs.
    """
    _log.info(
        "cross-engine backup SKIPPED — engine #2 is not present (%s); no maintenance "
        "window was opened and nothing was snapshotted",
        reason,
        extra={
            "component": "backup.quiesce",
            "action": "cross_engine_backup",
            "outcome": "skipped",
            "reason": reason,
        },
    )
    return {"ok": True, "skipped": True, "reason": reason}


@observe(tier="stage")
def _drain_verified(max_passes: int) -> int:
    """Drain the file queue until a pass reports it processed nothing. Raises otherwise.

    Returns the number of passes taken. See the module docstring for why
    ``drained is True`` is checked separately from ``items_processed``.
    """
    for attempt in range(1, max_passes + 1):
        result = _forward_admin("drain_now", {})
        if result.get("drained") is not True:
            raise RuntimeError(
                "cross-engine backup: drain_now reported drained=False "
                f"({result!r}) — the backend has no live drainer or the drain errored, "
                "so an empty item count is not evidence the queue is empty"
            )
        if int(result.get("items_processed") or 0) == 0:
            return attempt
    raise RuntimeError(
        f"cross-engine backup: queue never settled after {max_passes} drain passes — "
        "a writer is still running under the write-gate, so the two snapshots "
        "would not describe one instant"
    )


@observe(tier="stage")
def _verify_dump(data_root: Path, filename: str, expected_table: str) -> Path:
    """Confirm the container's dump is visible and non-trivial from the HOST side.

    The op runs in the backend container and reports a BASENAME; resolving it
    under the host's own data root is what proves the artifact landed on the
    shared bind-mount rather than in the container's writable layer. Trusting
    the op's absolute path would re-open exactly the failure this guards.

    STREAMED, not read whole (car G). The first cut of this function called
    ``read_text()`` on the artifact — harmless against a zero-row table and
    wrong the moment engine #2 holds real data, because a logical dump has no
    size bound and this runs on the nightly host beside everything else. Scanning
    line by line and stopping at the marker also means the happy path usually
    touches only the artifact's head.
    """
    path = data_root / "backups" / "mariadb" / filename
    if not path.is_file():
        raise RuntimeError(
            f"cross-engine backup: the dump op reported {filename!r} but it is "
            f"not visible at {path} — the artifact did not land on the shared data root"
        )
    saw_content = False
    saw_table = False
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not saw_content and line.strip():
                saw_content = True
            if f"`{expected_table}`" in line or f" {expected_table} " in line:
                saw_table = True
                break
    if not saw_content:
        raise RuntimeError(f"cross-engine backup: dump at {path} is empty")
    if not saw_table:
        raise RuntimeError(
            f"cross-engine backup: dump at {path} does not carry the {expected_table!r} "
            "table. With a zero-row schema, a successful exit and an empty artifact "
            "look identical on size alone — the schema is the evidence"
        )
    return path


@observe(tier="boundary")
def run_cross_engine_backup(
    db_path: Path,
    snapshot_dir: Path,
    backend_url: str,
    data_root: Path,
    *,
    ttl_seconds: float = QUIESCE_TTL_SEC,
    max_drain_passes: int = MAX_DRAIN_PASSES,
    expected_table: str = EXPECTED_TABLE,
) -> dict[str, Any]:
    """Take a matched, quiesced snapshot of BOTH engines. Raises on any hard failure.

    Args:
        db_path: the surrealkv store (its basename names the surql artifact).
        snapshot_dir: where the Surreal ``.surql`` export goes.
        backend_url: SurrealDB HTTP URL for ``GET /export``.
        data_root: the HOST's shared data root — the bind-mount the backend
            container sees as ``/data``. Used to verify the dump by basename.
        ttl_seconds: self-heal deadline requested for this driver's window.
        max_drain_passes: passes before the queue is declared unsettled.
        expected_table: table whose presence proves the dump carries schema.

    Returns:
        ``{"ok": True, "sql_dump": <basename>, "sql_bytes": int,
        "surreal_snapshot": str, "drain_passes": int, "nested": bool,
        "deadline_seconds": float, "restore_verified": {<table>: {source, restored}}}``

        or, when engine #2 is absent, ``{"ok": True, "skipped": True,
        "reason": str}`` — with no window opened and nothing snapshotted.

    Raises:
        RuntimeError: engine-#2 presence could not be DETERMINED, gate
            unobtainable, no self-heal belt, drain unverified, a dump that is not
            visible/complete on the host side, or a dump that does not RESTORE
            (car G's enumeration). In every case NEITHER engine is snapshotted,
            and an unrestorable dump is deleted rather than kept.
    """
    # DETECT FIRST, GATE SECOND. An absent engine must not cost a maintenance
    # window — the window is a full MCP outage (ADR-0210), and there would be
    # nothing to put in it. Nothing below this block runs on a host without
    # engine #2, including the gate.
    state, reason = _engine_two_state()
    if state == ENGINE_ABSENT:
        return _skip_result(reason)
    if state != ENGINE_PRESENT:
        raise RuntimeError(
            f"cross-engine backup: could not determine whether engine #2 is present "
            f"({reason}). Refusing to guess: treating this as ABSENT would silently "
            "disable the backup on a host that HAS an engine and whose backend is "
            "merely unreachable, and treating it as PRESENT would open a maintenance "
            "window to back up something that may not exist."
        )

    try:
        enter = _maintenance_enter(ttl_seconds)
    except Exception as exc:
        raise RuntimeError(
            f"cross-engine backup: could not assert the core write-gate ({exc}). "
            "Unlike the nightly cycle's own best-effort entry, the backup HARD-FAILS "
            "here (ADR-0210): an ungated snapshot is silently inconsistent."
        ) from exc

    entered = not bool(enter.get("previous"))
    try:
        deadline = enter.get("deadline_seconds")
        if deadline is None:
            raise RuntimeError(
                "cross-engine backup: the maintenance window reports "
                "deadline_seconds=null, so there is NO self-heal belt — a driver "
                "that dies mid-window would wedge every write indefinitely (ADR-0211)"
            )

        passes = _drain_verified(max_drain_passes)

        dump = _forward_admin("mariadb_dump", {"label": "nightly-quiesce"}, timeout_s=120.0)
        filename = str(dump.get("filename") or "")
        if not filename:
            raise RuntimeError(f"cross-engine backup: mariadb_dump returned no filename: {dump!r}")
        # From here on the dump EXISTS on disk, and the two halves live in
        # SEPARATE retention pools that ``_step_prune`` ages independently. A
        # dump left without its Surreal partner does not merely waste a slot —
        # it skews the pools apart and leaves car G's restore-verification an
        # artifact it cannot pair. So any failure past this point removes it:
        # the pools stay 1:1 BY CONSTRUCTION rather than by arithmetic that
        # only holds while nothing ever fails. Triage is unharmed — the raised
        # message names the path and the reason.
        try:
            dump_path = _verify_dump(data_root, filename, expected_table)
            sql_bytes = dump_path.stat().st_size

            # Car G: the artifact is now PROVEN RESTORABLE before it is kept, by
            # replaying it into a scratch schema in the backend container and
            # enumerating that schema row-for-row against the live one. This runs
            # INSIDE the held window on purpose — the source must not move while
            # it is the thing being compared against, and the gate is what stops
            # it. A backup nobody has ever restored is the false safety net the
            # 2026-06-16 incident was made of, so a failure here deletes the
            # artifact via the handler below rather than keeping one that reads
            # as a backup and is not.
            verification = _forward_admin(
                "mariadb_restore_verify", {"filename": filename}, timeout_s=900.0
            )
            if verification.get("status") != "ok":
                raise RuntimeError(
                    f"cross-engine backup: {filename!r} did NOT verify by enumeration "
                    f"({verification.get('status')}): "
                    f"{verification.get('violations') or verification.get('unavailable')}"
                )

            # MariaDB first, Surreal second — the referencing side before the
            # referenced one, so even a gate that failed open leaves at worst an
            # unreferenced body page rather than a dangling row (ADR-0204).
            surreal_path = create_snapshot(
                db_path,
                snapshot_dir=snapshot_dir,
                label=SURREAL_LABEL,
                backend_url=backend_url,
            )
        except Exception:
            (data_root / "backups" / "mariadb" / filename).unlink(missing_ok=True)
            raise
    finally:
        _release(entered)

    _log.info(
        "cross-engine backup complete",
        extra={
            "component": "backup.quiesce",
            "action": "cross_engine_backup",
            "outcome": "ok",
            "drain_passes": passes,
            "nested": not entered,
        },
    )
    return {
        "ok": True,
        "sql_dump": filename,
        "sql_bytes": sql_bytes,
        "surreal_snapshot": str(surreal_path),
        "drain_passes": passes,
        "nested": not entered,
        "deadline_seconds": float(deadline),
        # Positive evidence, not a boolean nobody set: the row counts the
        # enumeration actually compared. An "ok" with no numbers behind it is the
        # vacuous pass this whole arm exists to make impossible.
        "restore_verified": verification.get("checks", {})
        .get("row_identity", {})
        .get("detail", {})
        .get("counts", {}),
    }
