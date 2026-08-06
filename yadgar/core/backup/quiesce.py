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

    token = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")
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
    return _maintenance_post(_MAINTENANCE_ENTER_PATH, {"ttl_seconds": ttl_seconds})


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
    """
    path = data_root / "backups" / "mariadb" / filename
    if not path.is_file():
        raise RuntimeError(
            f"cross-engine backup: the dump op reported {filename!r} but it is "
            f"not visible at {path} — the artifact did not land on the shared data root"
        )
    body = path.read_text(encoding="utf-8", errors="replace")
    if not body.strip():
        raise RuntimeError(f"cross-engine backup: dump at {path} is empty")
    if f"`{expected_table}`" not in body and f" {expected_table} " not in body:
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
        "deadline_seconds": float}``

    Raises:
        RuntimeError: gate unobtainable, no self-heal belt, drain unverified, or
            a dump that is not visible/complete on the host side. In every case
            NEITHER engine is snapshotted.
    """
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
    }
