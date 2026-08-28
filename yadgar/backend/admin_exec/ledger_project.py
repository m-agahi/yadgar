"""Backend execution bodies for the ``project`` registry admin ops.

Split out of ``admin_exec/ledger.py`` (ledger task 402) — that file sat at 998
lines against the I13 HARD ``file_loc`` cap of 1000. The seam is a real one
rather than a line-budget convenience: ``project`` is NOT one of the D20
chokepoint's ledger tables (see ``scripts/check_ledger_chokepoint.py``'s
``LEDGER_TABLES``) — it is the REGISTRY those tables FK into, and the two ops
below are deliberately exempt from the registry guard every ledger write must
pass. Keeping it beside ``task`` / ``adr`` blurred that distinction.

Behaviour is unchanged: the op bodies moved verbatim, ``@observe`` metric names
included (``backend.admin.ledger.*`` — metric names are the observable
contract, so the split does not rename them).

PAYLOAD SHAPES:

    create_project_row(payload) -> {"ok": True, "row": dict}
        payload: {"key": str, "kind": "git"|"local", "display_name"?: str,
                  "remote_url"?: str}
        A duplicate key PROPAGATES as an ``AdminRefusal`` (409), never an
        ``{"ok": False}`` envelope — see the function.

    list_project_rows(payload) -> {"rows": list[dict]}
        payload: {}   # no parameters

    list_stale_projects(payload) -> {"projects": [...], "threshold_days": int,
                                     "count": int}
        payload: {}   # threshold comes from Settings.PROJECT_STALENESS_DAYS

ERROR MODEL — identical to ``ledger.py``'s: a FAULT never raises, a REFUSAL
always does. Every handler carries ``except AdminRefusal: raise`` ABOVE its
``except Exception`` arm so the ``/admin`` route renders the refusal as a
structured 409 (ADR-0423). Driver failures in ``create_project_row`` route
through ``driver_errors.driver_error_detail`` rather than ``str(exc)`` so the
envelope carries ``db_errno``. The structural sweep that enforces the refusal
arm (``test_car_c_admin_exec_refusal.py``) walks THIS module too.
"""

from __future__ import annotations

import logging
from typing import Any

from yadgar._shared.observability.observe import observe
from yadgar._shared.refusal import AdminRefusal
from yadgar.backend.admin_exec.driver_errors import driver_error_detail

logger = logging.getLogger(__name__)


def _get_sql_storage() -> Any:
    """The composed ``MariaStorageEngine``, or None when engine #2 is absent.

    A FUNCTION, not a module-scope import: ``sqlalchemy`` lives in the
    ``sql`` extra and is not always available. Matches the seam at
    ``admin_exec/ledger.py``, ``admin_exec/ledger_agent.py``,
    ``admin_exec/engine_status.py:58`` and ``invariants_cross_engine.py:136``
    — one patchable symbol per module across the admin_exec ledger surface.
    """
    from yadgar._shared.runtime.lifecycle import _get_sql_storage

    return _get_sql_storage()


# ── C6: the ``project`` registry seed + read ────────────────────────────────
#
# The registry is the FIRST thing an operator writes on a new deployment —
# every ``task`` / ``adr`` row FKs to it, so with zero rows the ledger cannot
# accept a single write. These two ops are the whole operator surface:
# ``create_project_row`` seeds a project, ``list_project_rows`` shows what is
# registered (and is what the C6 backfill validates its host-supplied mapping
# against before applying anything).
#
# DELIBERATELY UNGUARDED by the registry check. They ARE the registry — a
# guard here would be a bootstrap deadlock: nothing could ever be registered
# because registering requires something to already be registered.


@observe(tier="boundary", metric="backend.admin.ledger.create_project_row")
async def create_project_row(payload: dict) -> dict:
    """Seed one ``project`` registry row.

    payload: ``{"key": str, "kind": "git"|"local", "display_name"?: str,
    "remote_url"?: str}``.

    The storage layer raises ``DuplicateProjectError`` on a collision rather
    than issuing ``INSERT OR IGNORE`` (ADR-0202/0223: auto-creating on
    collision is how a typo mints a phantom namespace). This wrapper lets
    that error PROPAGATE — the ``/admin`` route's ``except AdminRefusal`` arm
    renders it as a structured 409 with ``reason="duplicate_project"``. The
    prior swallow-to-``{"ok": False, "error": ...}`` shape masked the
    rejection as a generic op failure; the structured 409 lets the caller
    distinguish a refused registration from a genuine backend fault.
    """
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        row = await storage.create_project_row(
            key=str(payload["key"]),
            kind=str(payload["kind"]),
            display_name=payload.get("display_name"),
            remote_url=payload.get("remote_url"),
        )
    except AdminRefusal:
        raise
    except Exception as exc:  # noqa: BLE001
        reason, errno = driver_error_detail(exc)
        logger.warning("create_project_row error key=%s: %s", payload.get("key"), reason)
        envelope: dict[str, Any] = {"ok": False, "error": reason}
        if errno is not None:
            envelope["db_errno"] = errno
        return envelope
    return {"ok": True, "row": row}


@observe(tier="boundary", metric="backend.admin.ledger.list_project_rows")
async def list_project_rows(payload: dict) -> dict:
    """Return every registered project. payload: ``{}`` (no parameters)."""
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        rows = await storage.list_project_rows()
    except AdminRefusal:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_project_rows error: %s", exc)
        return {"ok": False, "error": str(exc)}
    return {"rows": rows}


@observe(tier="stage", metric="backend.admin.ledger.list_stale_projects")
async def list_stale_projects(payload: dict) -> dict:
    """Return project rows whose ``last_validated_at`` is older than threshold.

    Car C11-#88 (task #88). payload: ``{}`` (no parameters — the threshold
    comes from ``Settings.PROJECT_STALENESS_DAYS``, env
    ``YADGAR_PROJECT_STALENESS_DAYS``, default 90). Surfacing NULL
    ``last_validated_at`` is the failure mode: a row that pre-dates the
    column cannot be older than anything but IS stale in the operator's
    intent.

    Returns ``{"projects": [...], "threshold_days": int, "count": int}`` on
    success, or ``{"ok": False, "error": str}`` on a missing engine /
    raised exception. The CLI prints the threshold alongside the row
    count so the operator does not need to re-read settings.
    """
    from yadgar._shared.config import get_settings

    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    threshold_days = int(get_settings().PROJECT_STALENESS_DAYS)
    try:
        result = await storage.list_stale_projects(threshold_days)
    except AdminRefusal:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_stale_projects error: %s", exc)
        return {"ok": False, "error": str(exc)}
    return result
