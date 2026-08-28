"""Backend execution bodies for the ``agent_pattern`` / ``agent_discipline`` ops.

Split out of ``admin_exec/ledger.py`` (ledger task 402) — that file sat at 998
lines against the I13 HARD ``file_loc`` cap of 1000, so the next paragraph
anyone added would have failed the gate on someone else's behalf. The split is
by LEDGER TABLE FAMILY, not by line budget: this module owns the two
agent-library tables (``agent_pattern``, ``agent_discipline``, and the
``agent_pattern_composes`` join), ``ledger_project.py`` owns the ``project``
registry, and ``ledger.py`` keeps ``task`` + ``adr``. Behaviour is unchanged —
the op bodies moved verbatim, ``@observe`` metric names included (they are the
observable contract; ``backend.admin.ledger.*`` still names the family).

PAYLOAD SHAPES:

    list_agent_prompt_rows(payload) -> {"rows": list[dict]}
        payload: {}   # no parameters today

    list_agent_discipline_rows(payload) -> {"rows": list[dict]}
        payload: {}   # no parameters today

    The Car I additions (``list_agent_pattern_rows_uses_desc``,
    ``get_agent_pattern_row``, ``list_pattern_composes``,
    ``save_agent_pattern_row``, ``save_agent_discipline_row``,
    ``increment_agent_pattern_uses``, ``get_agent_prompt_toc_updated_at``)
    document their own payloads on each function.

ERROR MODEL — identical to ``ledger.py``'s, and deliberately so: a FAULT never
raises, a REFUSAL always does. Every handler carries ``except AdminRefusal:
raise`` ABOVE its ``except Exception`` arm so the ``/admin`` route renders the
refusal as a structured 409 (ADR-0423) instead of flattening it into an
``{"ok": False}`` body at HTTP 200. The structural sweep that enforces this
(``test_car_c_admin_exec_refusal.py``) walks THIS module too — it was widened
from one ``__file__`` to the ledger module list in the same commit as the
split, because a sweep that follows the code is the only kind worth having.
"""

from __future__ import annotations

import logging
from typing import Any

from yadgar._shared.observability.observe import observe
from yadgar._shared.refusal import AdminRefusal

logger = logging.getLogger(__name__)


def _get_sql_storage() -> Any:
    """The composed ``MariaStorageEngine``, or None when engine #2 is absent.

    A FUNCTION, not a module-scope import: ``sqlalchemy`` lives in the
    ``sql`` extra and is not always available. Matches the seam at
    ``admin_exec/ledger.py``, ``admin_exec/ledger_project.py``,
    ``admin_exec/engine_status.py:58`` and ``invariants_cross_engine.py:136``
    — one patchable symbol per module across the admin_exec ledger surface.
    """
    from yadgar._shared.runtime.lifecycle import _get_sql_storage

    return _get_sql_storage()


@observe(tier="boundary", metric="backend.admin.ledger.list_agent_prompt_rows")
async def list_agent_prompt_rows(payload: dict) -> dict:
    """List every ``agent_pattern`` row. payload: {} (no params)."""
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        rows = await storage.list_agent_prompt_rows()
    except AdminRefusal:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_agent_prompt_rows error: %s", exc)
        return {"ok": False, "error": str(exc)}
    return {"rows": rows}


@observe(tier="boundary", metric="backend.admin.ledger.list_agent_discipline_rows")
async def list_agent_discipline_rows(payload: dict) -> dict:
    """List every ``agent_discipline`` row, ordered by position then name. payload: {}.

    Sister op to ``list_agent_prompt_rows``. Same engine-composed-or-not
    contract; same error envelope on a storage exception. The admin op
    surface for the discipline table was the half that never shipped —
    ``save_agent_discipline_row`` landed in Car I but the read counterpart
    was not added to the dispatch table, so any caller asking for a list
    hit ``KeyError`` on the op name. C5 closes the gap.
    """
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        rows = await storage.list_agent_discipline_rows()
    except AdminRefusal:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_agent_discipline_rows error: %s", exc)
        return {"ok": False, "error": str(exc)}
    return {"rows": rows}


# ── Car I additions: uses-DESC list, single-row lookup, composes read ──────


@observe(tier="boundary", metric="backend.admin.ledger.list_agent_pattern_rows_uses_desc")
async def list_agent_pattern_rows_uses_desc(payload: dict) -> dict:
    """``agent_pattern`` rows ordered by ``uses`` DESC, then ``name`` ASC.

    payload: ``{"limit": int = 20}`` — default 20 caps the restore token
    budget (mirrors the old wiki-TOC page's 20-row cap). D40.
    """
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        rows = await storage.list_agent_pattern_rows_uses_desc(
            limit=int(payload.get("limit", 20)),
        )
    except AdminRefusal:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_agent_pattern_rows_uses_desc error: %s", exc)
        return {"ok": False, "error": str(exc)}
    return {"rows": rows}


@observe(tier="boundary", metric="backend.admin.ledger.get_agent_pattern_row")
async def get_agent_pattern_row(payload: dict) -> dict:
    """Single ``agent_pattern`` lookup by ``name``.

    payload: ``{"name": str}``. Returns ``{"row": dict | None}`` —
    ``None`` for an unknown name so the caller can distinguish "absent"
    from "engine unavailable".
    """
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        row = await storage.get_agent_prompt_row(str(payload["name"]))
    except AdminRefusal:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_agent_pattern_row error name=%s: %s", payload.get("name"), exc)
        return {"ok": False, "error": str(exc)}
    return {"row": row}


@observe(tier="boundary", metric="backend.admin.ledger.list_pattern_composes")
async def list_pattern_composes(payload: dict) -> dict:
    """Ordered list of composed discipline slugs for one ``agent_pattern``.

    payload: ``{"pattern_name": str}``. Returns
    ``{"rows": [{"pattern_name", "discipline_name", "position"}, ...]}``,
    ordered by ``position`` ASC. Empty list for an absent row.
    """
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        rows = await storage.list_pattern_composes(
            pattern_name=str(payload["pattern_name"]),
        )
    except AdminRefusal:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "list_pattern_composes error pattern=%s: %s", payload.get("pattern_name"), exc
        )
        return {"ok": False, "error": str(exc)}
    return {"rows": rows}


@observe(tier="boundary", metric="backend.admin.ledger.save_agent_pattern_row")
async def save_agent_pattern_row(payload: dict) -> dict:
    """Upsert one ``agent_pattern`` row by ``name``.

    payload: ``{name, body_slug, content_hash, purpose?, status?, baseline_hash?}``.
    Used by ``agent_prompt_save`` to mirror the wiki body page as a
    ledger row (the cross-engine invariant arm compares the two via
    ``content_hash``).
    """
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        return await storage.save_agent_prompt(
            name=str(payload["name"]),
            body_slug=str(payload["body_slug"]),
            content_hash=str(payload["content_hash"]),
            purpose=payload.get("purpose"),
            status=str(payload.get("status", "active")),
            baseline_hash=payload.get("baseline_hash"),
        )
    except AdminRefusal:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("save_agent_pattern_row error name=%s: %s", payload.get("name"), exc)
        return {"ok": False, "error": str(exc)}


@observe(tier="boundary", metric="backend.admin.ledger.save_agent_discipline_row")
async def save_agent_discipline_row(payload: dict) -> dict:
    """Upsert one ``agent_discipline`` row by ``name``.

    payload: ``{name, body_slug, content_hash, baseline_hash?, meta?}``.
    ``meta`` carries ``{purpose?, always_applied?, position?, status?}``
    (per the engine method's signature).
    """
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        return await storage.save_agent_discipline(
            name=str(payload["name"]),
            body_slug=str(payload["body_slug"]),
            content_hash=str(payload["content_hash"]),
            baseline_hash=payload.get("baseline_hash"),
            meta=payload.get("meta"),
        )
    except AdminRefusal:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("save_agent_discipline_row error name=%s: %s", payload.get("name"), exc)
        return {"ok": False, "error": str(exc)}


@observe(tier="boundary", metric="backend.admin.ledger.increment_agent_pattern_uses")
async def increment_agent_pattern_uses(payload: dict) -> dict:
    """``UPDATE agent_pattern SET uses = uses + 1 WHERE name = :name``.

    payload: ``{"pattern": str}``. Replaces the old
    ``increment_prompt_usage`` op (the memory-row read-modify-write
    path is gone; ``uses`` is a SQL integer, D40).
    """
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        await storage.increment_agent_prompt_uses(str(payload["pattern"]))
    except AdminRefusal:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("increment_agent_pattern_uses error: %s", exc)
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "pattern": payload["pattern"]}


@observe(tier="boundary", metric="backend.admin.ledger.get_agent_prompt_toc_updated_at")
async def get_agent_prompt_toc_updated_at(payload: dict) -> dict:
    """Return ``MAX(agent_pattern.updated_at)`` as a unix timestamp float.

    payload: ``{}``. The S6 restore-surface signal that used to read the
    wiki-TOC page's ``updated_at`` now reads the table directly. Returns
    ``{"timestamp": float | None}`` — ``None`` when the table is empty.
    """
    storage = _get_sql_storage()
    if storage is None:
        return {
            "ok": False,
            "error": "engine #2 not composed (MariaStorageEngine is None)",
            "timestamp": None,
        }
    try:
        dt = await storage.max_agent_pattern_updated_at()
    except AdminRefusal:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_agent_prompt_toc_updated_at error: %s", exc)
        return {"ok": False, "error": str(exc), "timestamp": None}
    if dt is None:
        return {"timestamp": None}
    return {"timestamp": dt.timestamp()}
