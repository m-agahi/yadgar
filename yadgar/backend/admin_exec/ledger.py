"""Backend execution bodies for the ledger READ admin ops (Car B).

Engine #2's ``MariaStorageEngine`` exposes the task / adr / agent_prompt read
methods directly on the class (no ``_LedgerMixin`` — Car A's deliberate design
choice to avoid the PR-#32 MRO collision with SurrealDB's
``_RuntimeConfigMixin``). These op bodies are the backend dispatch wrappers
that forward them to the ``/admin`` route's caller.

Each body is an ``async def`` because ``asyncmy`` is async-only; the admin
dispatcher (``run_admin_op_async``) keeps SYNC bodies on
``asyncio.to_thread`` and awaits ASYNC bodies on the event loop directly.

PAYLOAD SHAPES (contract for Cars D / F / I):

    list_task_rows(payload) -> {"rows": list[dict]}
        payload: {"project_id": str, "status"?: list[str], "summary"?: bool}
        ``summary: True`` projects ``id, title, status`` only; absent/False
        keeps the full 11-column shape.

    get_task_row(payload) -> {"row": dict | None}
        payload: {"id": int}

    create_task_row(payload) -> {"id": int, ...row}
        payload: {"project_id": str, "title": str, "status"?: str,
                  "state"?: str, "active_form"?: str, "plan_path"?: str,
                  "body_slug"?: str}
        Returns the inserted PK (LAST_INSERT_ID) plus the inserted params.

    update_task_row(payload) -> {"id": int, ...patched}
        payload: {"id": int, <column>: <value>, ...}
        Only the named columns are UPDATEd; absent fields are left unchanged
        (the storage layer's update_task_row enforces an empty-fields no-op).
        ``state: None`` clears the column to NULL (§16.10 — completed/archived
        transitions). ``blocked_by`` / ``blocks`` lists are reconciled against
        the ``task_blocked_by`` join table (D39) — they are NOT columns on
        ``task``; the admin op handles the join-edge sync side-channel.

    list_task_rows_all_projects(payload) -> {"rows": list[dict]}
        payload: {"status"?: list[str], "summary"?: bool}

    list_adr_rows(payload) -> {"rows": list[dict]}
        payload: {"project_id": str, "status"?: str, "tier"?: str, "subsystem"?: str}

    get_adr_row(payload) -> {"row": dict | None}
        payload: {"id": int}

    list_agent_prompt_rows(payload) -> {"rows": list[dict]}
        payload: {}   # no parameters today

ERROR MODEL: never raise. A storage exception becomes ``{"ok": False,
"error": "..."}`` — matches the existing admin-op contract (see
``runtime_config.py``).
"""

from __future__ import annotations

import logging
from typing import Any

from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)


def _get_sql_storage() -> Any:
    """The composed ``MariaStorageEngine``, or None when engine #2 is absent.

    A FUNCTION, not a module-scope import: ``sqlalchemy`` lives in the
    ``sql`` extra and is not always available. Matches the seam at
    ``admin_exec/engine_status.py:58`` and ``invariants_cross_engine.py:136``
    so tests patch one symbol across the admin_exec ledger surface.
    """
    from yadgar._shared.runtime.lifecycle import _get_sql_storage  # noqa: PLC0415

    return _get_sql_storage()


@observe(tier="boundary", metric="backend.admin.ledger.list_task_rows")
async def list_task_rows(payload: dict) -> dict:
    """Project-scoped ``task`` read. payload: {project_id, status?, summary?}.

    ``summary`` (bool) selects the lean ``id, title, status`` projection. It
    defaults to ``False`` — the pre-projection shape — so a payload from an
    older core image (which sends no such key) keeps getting every column
    rather than silently losing the ones its consumers read. The lean shape is
    the ``task_list`` tool's default and that tool always sends the key.
    """
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        rows = await storage.list_task_rows(
            project_id=payload["project_id"],
            status=payload.get("status"),
            summary=bool(payload.get("summary", False)),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_task_rows error: %s", exc)
        return {"ok": False, "error": str(exc)}
    return {"rows": rows}


@observe(tier="boundary", metric="backend.admin.ledger.get_task_row")
async def get_task_row(payload: dict) -> dict:
    """Single ``task`` lookup by id. payload: {id}."""
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        row = await storage.get_task_row(int(payload["id"]))
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_task_row error id=%s: %s", payload.get("id"), exc)
        return {"ok": False, "error": str(exc)}
    return {"row": row}


@observe(tier="boundary", metric="backend.admin.ledger.list_task_rows_all_projects")
async def list_task_rows_all_projects(payload: dict) -> dict:
    """Cross-project ``task`` read. payload: {status?, summary?}.

    ``summary`` defaults to ``False`` — see ``list_task_rows``.
    """
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        rows = await storage.list_task_rows_all_projects(
            status=payload.get("status"),
            summary=bool(payload.get("summary", False)),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_task_rows_all_projects error: %s", exc)
        return {"ok": False, "error": str(exc)}
    return {"rows": rows}


# ── Car D: task write ops ─────────────────────────────────────────────────────
# The MCP tool shells in yadgar.core.server.tools.task forward here over HTTP.
# These wrappers translate the dict payload into the typed call into
# ``MariaStorageEngine`` (engine #2). The optional ``blocked_by`` / ``blocks``
# keys reconcile the ``task_blocked_by`` join table (D39) AFTER the row is
# created/updated; the reconciliation is idempotent (delete-then-insert).


@observe(tier="boundary", metric="backend.admin.ledger.create_task_row")
async def create_task_row(payload: dict) -> dict:
    """INSERT one ``task`` row. payload keys (see module docstring)."""
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        result = await storage.create_task_row(
            project_id=payload["project_id"],
            title=payload["title"],
            status=payload.get("status", "pending"),
            state=payload.get("state", "open"),
            active_form=payload.get("active_form"),
            plan_path=payload.get("plan_path"),
            body_slug=payload.get("body_slug"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("create_task_row error: %s", exc)
        return {"ok": False, "error": str(exc)}
    # D39: optionally reconcile ``task_blocked_by`` join edges on CREATE.
    inserted_id = int(result.get("id", 0))
    blocked_by = payload.get("blocked_by")
    if blocked_by is not None and inserted_id:
        try:
            for blocker_id in blocked_by:
                await storage.add_task_blocked_by(inserted_id, int(blocker_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("create_task_row blocked_by sync error: %s", exc)
            # Row created; edge-sync failure is non-fatal — surface the id.
    return result


@observe(tier="boundary", metric="backend.admin.ledger.update_task_row")
async def update_task_row(payload: dict) -> dict:
    """UPDATE one ``task`` row. payload: {id, <col>: <val>, ...}."""
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        task_id = int(payload["id"])
        # Strip non-column keys before the typed UPDATE; ``blocked_by`` and
        # ``blocks`` are join-edge reconcilers (D39), handled separately.
        column_payload = {
            k: v for k, v in payload.items() if k not in {"id", "blocked_by", "blocks"}
        }
        await storage.update_task_row(task_id, **column_payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("update_task_row error: %s", exc)
        return {"ok": False, "error": str(exc)}
    # D39: optionally reconcile ``task_blocked_by`` join edges on UPDATE.
    blocked_by = payload.get("blocked_by")
    if blocked_by is not None:
        try:
            # Read the existing set; delete the diff (removed); insert the diff (added).
            existing = set(await storage.list_task_blocked_by(task_id))
            desired = {int(x) for x in blocked_by}
            from sqlalchemy import text as _sa_text  # noqa: PLC0415

            async with storage._engine.begin() as conn:  # type: ignore[attr-defined]
                for gone in existing - desired:
                    await conn.execute(
                        _sa_text(
                            "DELETE FROM task_blocked_by "
                            "WHERE task_id = :task_id AND blocked_by_id = :blocked_by_id"
                        ),
                        {"task_id": task_id, "blocked_by_id": gone},
                    )
            for new in desired - existing:
                await storage.add_task_blocked_by(task_id, new)
        except Exception as exc:  # noqa: BLE001
            logger.warning("update_task_row blocked_by sync error: %s", exc)
    return {"id": task_id, **column_payload}


@observe(tier="boundary", metric="backend.admin.ledger.list_adr_rows")
async def list_adr_rows(payload: dict) -> dict:
    """Project-scoped ``adr`` read.

    payload: {project_id, status?, tier?, subsystem?}.

    Car H (0047 §7 D27/D28): forwards ``tier`` (D27 enum:
    ``"binding"`` | ``"historical"``; ``None`` = no filter) and
    ``subsystem`` (D28, author-supplied, on-write-normalized
    lowercase+trim) to ``MariaStorageEngine.list_adr_rows``. Both compose
    with the existing ``status`` filter and each other.
    """
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        rows = await storage.list_adr_rows(
            project_id=payload["project_id"],
            status=payload.get("status"),
            tier=payload.get("tier"),
            subsystem=payload.get("subsystem"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_adr_rows error: %s", exc)
        return {"ok": False, "error": str(exc)}
    return {"rows": rows}


@observe(tier="boundary", metric="backend.admin.ledger.get_adr_row")
async def get_adr_row(payload: dict) -> dict:
    """Single ``adr`` lookup by id. payload: {id}."""
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        row = await storage.get_adr_row(int(payload["id"]))
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_adr_row error id=%s: %s", payload.get("id"), exc)
        return {"ok": False, "error": str(exc)}
    return {"row": row}


@observe(tier="boundary", metric="backend.admin.ledger.list_agent_prompt_rows")
async def list_agent_prompt_rows(payload: dict) -> dict:
    """List every ``agent_pattern`` row. payload: {} (no params)."""
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        rows = await storage.list_agent_prompt_rows()
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_agent_prompt_rows error: %s", exc)
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
    except Exception as exc:  # noqa: BLE001
        logger.warning("increment_agent_pattern_uses error: %s", exc)
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "pattern": payload["pattern"]}


@observe(tier="boundary", metric="backend.admin.ledger.get_agent_prompt_toc_updated_at")
async def get_agent_prompt_toc_updated_at(payload: dict) -> dict:  # noqa: ARG001
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
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_agent_prompt_toc_updated_at error: %s", exc)
        return {"ok": False, "error": str(exc), "timestamp": None}
    if dt is None:
        return {"timestamp": None}
    return {"timestamp": dt.timestamp()}


# ── Car F: ADR write ops ───────────────────────────────────────────────────────
# Re-point of the ADR MCP tools (0047 §7 Car F): ``create_adr_row`` allocates
# the new row (returns the AUTO_INCREMENT id — ADR-0197: id IS the ADR
# number, no separate sequence table); ``set_adr_body_slug`` stamps the
# wiki body page slug onto the row once the page write commits; and
# ``add_adr_supersedes`` inserts the D23 supersede link between rows.
# All three are async because asyncmy is async-only.


@observe(tier="boundary", metric="backend.admin.ledger.create_adr_row")
async def create_adr_row(payload: dict) -> dict:
    """Insert one ``adr`` ledger row. payload: {project_id, title, status,
    decided_on?, subsystem?, tier?, body_slug?}.
    Returns ``{"id": <int>, ...fields}`` on success; ``{"ok": False, "error": ...}``
    on storage failure (carries ADR-0197's AUTO_INCREMENT id — the caller uses
    ``row["id"]`` as the ADR number).
    """
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        row = await storage.create_adr_row(
            project_id=payload["project_id"],
            title=payload["title"],
            status=payload.get("status", "open"),
            decided_on=payload.get("decided_on"),
            subsystem=payload.get("subsystem"),
            tier=payload.get("tier"),
            body_slug=payload.get("body_slug"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("create_adr_row error: %s", exc)
        return {"ok": False, "error": str(exc)}
    return {"row": row}


@observe(tier="boundary", metric="backend.admin.ledger.set_adr_body_slug")
async def set_adr_body_slug(payload: dict) -> dict:
    """Stamp the wiki ``body_slug`` on one ``adr`` row. payload: {id, body_slug}.

    Called by ``adr_add`` after the per-ADR body page commits (D4 — body stays
    in SurrealDB; only the slug pointer is stored on the SQL row).
    """
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        await storage.set_adr_body_slug(int(payload["id"]), payload["body_slug"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("set_adr_body_slug error id=%s: %s", payload.get("id"), exc)
        return {"ok": False, "error": str(exc)}
    return {"ok": True}


@observe(tier="boundary", metric="backend.admin.ledger.add_adr_supersedes")
async def add_adr_supersedes(payload: dict) -> dict:
    """Insert one ``adr_supersedes`` link. payload: {adr_id, supersedes_id}.

    Car F: ``adr_add(supersedes="ADR-0007")`` parses ``supersedes_id`` from the
    legacy ADR id string and forwards one row per target. The status flip on the
    target row (``status='superseded'``) is performed by ``flip_adr_superseded``
    below — both go in the same forward call sequence so a partial failure
    surfaces cleanly to the caller.
    """
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        await storage.add_adr_supersedes(int(payload["adr_id"]), int(payload["supersedes_id"]))
        # Car F: flip the target row's status to 'superseded' (D23). Car G
        # adds the canonical retype (``adr`` → ``adr_superseded``); F only flips
        # the status column. The SQL engine exposes ``set_task_status`` etc. as
        # a generic row update — Car F uses a direct UPDATE here to keep the
        # scope tight.
        await storage._flip_adr_status(int(payload["supersedes_id"]), "superseded")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "add_adr_supersedes error adr_id=%s supersedes_id=%s: %s",
            payload.get("adr_id"),
            payload.get("supersedes_id"),
            exc,
        )
        return {"ok": False, "error": str(exc)}
    return {"ok": True}


# ── Car G: max_adr_updated_at (replaces wiki-index timestamp signal) ─────────


@observe(tier="boundary", metric="backend.admin.ledger.max_adr_updated_at")
async def max_adr_updated_at(payload: dict) -> dict:
    """Return ``MAX(adr.updated_at)`` for *project_id* as a unix timestamp float.

    payload: ``{"project_id": str}``. Car G (0047 §7): the
    ``project_brief._get_adr_log_updated_at`` signal re-points off the
    deleted ``<project>-adr-index`` wiki page onto the SQL ledger. The
    raw datetime is coerced to a float for symmetry with the legacy wiki
    page-timestamp surface (so the ADR-due nudge downstream code is
    unchanged). Returns ``{"updated_at": None}`` when the table has no
    rows for the project — same nullability the old wiki-page signal had.
    """
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        dt = await storage.max_adr_updated_at(project_id=str(payload["project_id"]))
    except Exception as exc:  # noqa: BLE001
        logger.warning("max_adr_updated_at error: %s", exc)
        return {"ok": False, "error": str(exc), "updated_at": None}
    if dt is None:
        return {"updated_at": None}
    return {"updated_at": dt.timestamp()}


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

    A duplicate key comes back as ``{"ok": False, "error": ...}`` naming the
    key — NOT swallowed. The storage layer raises ``DuplicateProjectError``
    rather than issuing ``INSERT OR IGNORE`` (ADR-0202/0223: auto-creating on
    collision is how a typo mints a phantom namespace); this wrapper converts
    it to the admin-op error shape like every other failure here.
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
    except Exception as exc:  # noqa: BLE001
        logger.warning("create_project_row error key=%s: %s", payload.get("key"), exc)
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "row": row}


@observe(tier="boundary", metric="backend.admin.ledger.list_project_rows")
async def list_project_rows(payload: dict) -> dict:
    """Return every registered project. payload: ``{}`` (no parameters)."""
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        rows = await storage.list_project_rows()
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_project_rows error: %s", exc)
        return {"ok": False, "error": str(exc)}
    return {"rows": rows}
