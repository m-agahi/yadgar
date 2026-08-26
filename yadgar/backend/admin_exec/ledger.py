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
        payload: {"project_id": str, "status"?: list[str], "summary"?: bool,
                  "with_edges"?: bool, "limit"?: int, "offset"?: int}
        ``summary: True`` projects ``id, title, status`` only; absent/False
        keeps the full 11-column shape.
        ``with_edges: True`` adds ``blocked_by`` / ``blocks`` to every row
        (Car E). OPT-IN, because it is a second query the projection win
        (Car A, 324 -> 110 chars/row) would otherwise be spent on: the
        harness seeder needs the edges, a status sweep does not.

    get_task_row(payload) -> {"row": dict | None}
        payload: {"id": int}
        The single-row read ALWAYS carries ``blocked_by`` / ``blocks``
        (Car E) — there is no width argument for one row, and a caller
        asking about one task is the caller most likely to want them.

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
        A join-edge sync FAILURE is reported as ``{"ok": False, ...}``
        (Car E) — see ``_reconcile_edges``.

    list_task_rows_all_projects(payload) -> {"rows": list[dict]}
        payload: {"status"?: list[str], "summary"?: bool, "limit"?: int,
                  "offset"?: int}

    list_adr_rows(payload) -> {"rows": list[dict]}
        payload: {"project_id": str, "status"?: str, "tier"?: str, "subsystem"?: str}
        Every row ALWAYS carries ``supersedes`` / ``superseded_by`` — lists of
        ``adr.id``, read from the ``adr_supersedes`` join table (ledger task
        195). They are NOT columns on ``adr``. Always-on rather than
        ``with_edges``-gated like ``list_task_rows``, because the 7-key ADR
        consumer shape always emits both keys.

    get_adr_row(payload) -> {"row": dict | None}
        payload: {"id": int, "project_id": str}
        ``project_id`` is REQUIRED (ledger task 188) — ``adr.id`` is one
        global AUTO_INCREMENT shared across projects, so an unscoped by-id
        lookup returns foreign rows. Absent → ``{"ok": False, "error": ...}``.
        The row carries ``supersedes`` / ``superseded_by`` as above; a
        ``None`` row stays ``None`` (the attach never resurrects a row the
        scoped lookup refused).

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
from yadgar._shared.refusal import AdminRefusal

logger = logging.getLogger(__name__)


class TaskEdgePartialStateError(AdminRefusal, RuntimeError):
    """The row was created/updated, but one of its edge directions did not write.

    Car C10 (task #319): pre-C10 the ledger ops caught ``Exception`` around
    the entire body and returned ``{"ok": False, "error": "..."}`` at HTTP
    200 — operationally identical to a fully-failed create/update. The
    /admin route catches ``AdminRefusal`` → 409, so the row+missing-edge
    partial state had no way to reach that seam.

    Subclasses BOTH ``AdminRefusal`` and ``RuntimeError`` so:
      - the /admin route's ``except AdminRefusal`` arm renders it as 409 +
        a structured envelope (``refused``, ``reason``, ``task_id``,
        ``edge_kind``, ``edge_error``);
      - any pre-existing ``except RuntimeError`` catchers keep working
        (e.g. forwarder-side handlers that key off RuntimeError for retry).

    Carries ``task_id`` and ``edge_kind`` so the caller can decide whether
    to roll back the row, retry the edge sync, or accept the partial state
    — D39 partial state is a deliberate outcome, not a fault.
    """

    reason = "task_edge_partial_state"

    def __init__(self, *, task_id: int, kind: str, reason: str) -> None:
        super().__init__(reason)
        self.task_id = int(task_id)
        self.edge_kind = str(kind)
        self.edge_error = str(reason)

    def refusal_report(self) -> dict:
        return {
            "task_id": self.task_id,
            "edge_kind": self.edge_kind,
            "edge_error": self.edge_error,
        }


def _get_sql_storage() -> Any:
    """The composed ``MariaStorageEngine``, or None when engine #2 is absent.

    A FUNCTION, not a module-scope import: ``sqlalchemy`` lives in the
    ``sql`` extra and is not always available. Matches the seam at
    ``admin_exec/engine_status.py:58`` and ``invariants_cross_engine.py:136``
    so tests patch one symbol across the admin_exec ledger surface.
    """
    from yadgar._shared.runtime.lifecycle import _get_sql_storage

    return _get_sql_storage()


# ── Car E: task_blocked_by join edges, read + reconcile ──────────────────────


@observe(tier="stage", metric="backend.admin.ledger.attach_task_edges")
async def _attach_task_edges(storage: Any, rows: list[dict]) -> list[dict]:
    """Return ``rows`` with ``blocked_by`` / ``blocks`` on every row.

    One bulk query for the whole page (``list_task_edges``), never a pair of
    reads per row. Rows with no ``id`` are passed through untouched — they
    cannot be keyed, and dropping them would turn a projection surprise into
    a silently shorter list.
    """
    ids = [int(r["id"]) for r in rows if isinstance(r, dict) and r.get("id") is not None]
    edges = await storage.list_task_edges(ids)
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("id") is None:
            out.append(row)
            continue
        entry = edges.get(int(row["id"])) or {}
        out.append(
            {
                **row,
                "blocked_by": list(entry.get("blocked_by", [])),
                "blocks": list(entry.get("blocks", [])),
            }
        )
    return out


@observe(tier="stage", metric="backend.admin.ledger.reconcile_task_edges")
async def _reconcile_edges(
    storage: Any,
    task_id: int,
    desired_ids: Any,
    *,
    inverse: bool,
) -> None:
    """Make the live edge set for ``task_id`` equal ``desired_ids``.

    ``inverse=False`` is the ``blocked_by`` direction — rows
    ``(task_id, other)``. ``inverse=True`` is the ``blocks`` direction — rows
    ``(other, task_id)``. One join table, read and written from both ends.

    The inverse direction had NO implementation before Car E: ``blocks`` was
    accepted by ``task_write``, stripped out of the column payload here, and
    then dropped — a write that returned ``ok: true`` and stored nothing.
    Shipping the ``blocks`` READ without this would have made every
    write-then-read look like a broken reader.

    Raises whatever storage raises; both callers turn that into an
    ``{"ok": False, ...}`` envelope rather than logging it and reporting
    success.
    """
    read = storage.list_task_blocks if inverse else storage.list_task_blocked_by
    existing = {int(x) for x in await read(task_id)}
    desired = {int(x) for x in desired_ids}
    for gone in sorted(existing - desired):
        pair = (gone, task_id) if inverse else (task_id, gone)
        await storage.remove_task_blocked_by(*pair)
    for new in sorted(desired - existing):
        pair = (new, task_id) if inverse else (task_id, new)
        await storage.add_task_blocked_by(*pair)


@observe(tier="stage", metric="backend.admin.ledger.sync_task_edges")
async def _sync_task_edges(storage: Any, task_id: int, payload: dict) -> None:
    """Reconcile both edge directions named in ``payload``.

    Car C10 (task #319): pre-C10 this returned ``f"its {key} edges were
    not written: {exc}"`` on partial state — a string the caller prefixed
    with the row that WAS created. That surface lived inside an
    ``{"ok": False}`` envelope at HTTP 200, indistinguishable from "row
    did not create at all". The route catches ``AdminRefusal`` → 409, so
    raising ``TaskEdgePartialStateError`` is the only way the partial
    state reaches the 409 seam.

    Absent keys are left alone — ``blocked_by`` missing means "the caller
    did not mention dependencies", never "clear them".

    Raises:
        TaskEdgePartialStateError: a direction's reconcile raised. The row
            (created by ``create_task_row`` or mutated by
            ``update_task_row``) is left in place — the caller decides
            whether to roll back, retry the edge sync, or accept the
            partial state. The error carries ``task_id`` and ``edge_kind``.
    """
    for key, inverse in (("blocked_by", False), ("blocks", True)):
        desired = payload.get(key)
        if desired is None:
            continue
        try:
            await _reconcile_edges(storage, task_id, desired, inverse=inverse)
        except TaskEdgePartialStateError:
            # Already-typed refusal from the inner reconcile — propagate as-is.
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("task %s %s edge sync error: %s", task_id, key, exc)
            raise TaskEdgePartialStateError(
                task_id=task_id,
                kind=key,
                reason=f"its {key} edges were not written: {exc}",
            ) from exc


@observe(tier="boundary", metric="backend.admin.ledger.list_task_rows")
async def list_task_rows(payload: dict) -> dict:
    """Project-scoped ``task`` read. payload: {project_id, status?, summary?,
    with_edges?}.

    ``summary`` (bool) selects the lean ``id, title, status`` projection. It
    defaults to ``False`` — the pre-projection shape — so a payload from an
    older core image (which sends no such key) keeps getting every column
    rather than silently losing the ones its consumers read. The lean shape is
    the ``task_list`` tool's default and that tool always sends the key.

    ``with_edges`` (bool, Car E) adds ``blocked_by`` / ``blocks``. It defaults
    to ``False`` in the OTHER direction to ``summary``, and for the same
    reason read the other way round: absent means an older core that never
    asked, and a list read must not pay for a join nobody wanted.

    ``limit`` / ``offset`` (Car D) are forwarded as ``None`` when absent, which
    the storage layer reads as "emit no clause". They used to be read by
    nothing at all: the tool forwarded ``limit`` when non-default and the
    op body dropped it on the floor.
    """
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        rows = await storage.list_task_rows(
            project_id=payload["project_id"],
            status=payload.get("status"),
            summary=bool(payload.get("summary", False)),
            limit=payload.get("limit"),
            offset=payload.get("offset"),
        )
        if payload.get("with_edges"):
            rows = await _attach_task_edges(storage, rows)
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_task_rows error: %s", exc)
        return {"ok": False, "error": str(exc)}
    return {"rows": rows}


@observe(tier="boundary", metric="backend.admin.ledger.get_task_row")
async def get_task_row(payload: dict) -> dict:
    """Single ``task`` lookup by id. payload: {id}.

    Car E: the returned row ALWAYS carries ``blocked_by`` and ``blocks``.
    They are join rows, not ``task`` columns, so they cost two extra reads —
    accepted unconditionally here because this op reads ONE row and the
    ``task_get`` caller has no width knob to turn.

    An edge read that FAILS fails the whole op. The alternative (empty lists
    on error) is indistinguishable from "this task has no dependencies", which
    is the exact reading that lost six real edges during the 2026-08-15
    backfill.
    """
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        row = await storage.get_task_row(int(payload["id"]))
        if row is not None and row.get("id") is not None:
            task_id = int(row["id"])
            row = {
                **row,
                "blocked_by": list(await storage.list_task_blocked_by(task_id)),
                "blocks": list(await storage.list_task_blocks(task_id)),
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_task_row error id=%s: %s", payload.get("id"), exc)
        return {"ok": False, "error": str(exc)}
    return {"row": row}


@observe(tier="boundary", metric="backend.admin.ledger.list_task_rows_all_projects")
async def list_task_rows_all_projects(payload: dict) -> dict:
    """Cross-project ``task`` read. payload: {status?, summary?, with_edges?}.

    ``summary`` defaults to ``False`` and ``with_edges`` to ``False`` — see
    ``list_task_rows`` for both.
    """
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        rows = await storage.list_task_rows_all_projects(
            status=payload.get("status"),
            summary=bool(payload.get("summary", False)),
            limit=payload.get("limit"),
            offset=payload.get("offset"),
        )
        if payload.get("with_edges"):
            rows = await _attach_task_edges(storage, rows)
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
    except AdminRefusal:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("create_task_row error: %s", exc)
        return {"ok": False, "error": str(exc)}
    # D39: optionally reconcile ``task_blocked_by`` join edges on CREATE.
    inserted_id = int(result.get("id", 0))
    if inserted_id:
        # Car C10 (task #319): partial-state edge sync raises
        # ``TaskEdgePartialStateError`` (an AdminRefusal) so the /admin route
        # renders it as 409 + a structured envelope, instead of swallowing it
        # into ``{"ok": False, ...}`` at HTTP 200. The row IS still created
        # and its id is carried on the envelope so the caller can decide
        # whether to roll back, retry, or accept the partial state.
        await _sync_task_edges(storage, inserted_id, payload)
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
    # Car C10 (task #319): see create_task_row — partial-state edge sync now
    # raises TaskEdgePartialStateError so the route renders it as 409.
    await _sync_task_edges(storage, task_id, payload)
    return {"id": task_id, **column_payload}


@observe(tier="boundary", metric="backend.admin.ledger.update_adr_tier_subsystem")
async def update_adr_tier_subsystem(payload: dict) -> dict:
    """UPDATE one ``adr`` row's ``tier`` + ``subsystem`` columns.

    payload: ``{id, tier, subsystem}`` — ``id`` is the ADR primary key,
    ``tier`` is the D27 enum (``"binding"`` | ``"historical"``),
    ``subsystem`` is the D28 free-form VARCHAR(128) (may be ``None`` when
    the row carries no subsystem yet — operator reviews).

    Car B (task #202): the seed op that backfills these columns on the
    legacy corpus used to write via a direct ``storage._engine.begin()``
    UPDATE — a D20 chokepoint violation. This admin op is the sanctioned
    chokepoint surface: every ``adr`` row write goes through
    ``MariaStorageEngine.update_adr_tier_subsystem``, no exceptions.
    """
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        adr_id = int(payload["id"])
        tier = str(payload["tier"])
        subsystem = payload.get("subsystem")
        if subsystem is not None:
            subsystem = str(subsystem)
        await storage.update_adr_tier_subsystem(adr_id, tier, subsystem)
    except Exception as exc:  # noqa: BLE001
        logger.warning("update_adr_tier_subsystem error: %s", exc)
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "id": adr_id, "tier": tier, "subsystem": subsystem}


@observe(tier="stage", metric="backend.admin.ledger.attach_adr_supersedes")
async def _attach_supersedes(storage: Any, project_id: str, rows: list[dict]) -> list[dict]:
    """Add ``supersedes`` / ``superseded_by`` id lists to each ``adr`` row.

    Ledger task 195: ``adr`` carries no such COLUMN (migration 002) — the
    relation lives only in ``adr_supersedes``, which ``add_adr_supersedes`` has
    written since Car F and which nothing has ever read. Both consumers of the
    7-key ADR shape therefore rendered ``"none"`` / ``"-"`` unconditionally, in
    22/22 supersede-bearing ADRs across two corpora.

    THE ATTACH IS HERE, NOT IN CORE'S ``adr_list``. Three call sites forward
    these ops (``adr_list``, ``adr_get``, and the dormant
    ``adr_render._assemble_index_rows``); enriching in core would fix two and
    leave the third quietly wrong. Same placement as ``_attach_edges`` for
    ``task_blocked_by``, and one bulk read for the whole page rather than a
    lookup per row.

    ALWAYS-ON, unlike ``list_task_rows``' ``with_edges``: that flag exists
    because the lean task projection OMITS those keys, whereas the ADR shape
    always emits these two, so an opt-in would leave every existing caller
    wrong. One extra query against a ~237-row corpus.

    BEST-EFFORT: an enrichment that can take out ``adr_list`` is a worse defect
    than the one it fixes, so a failed edge read logs and degrades to the empty
    lists — i.e. to exactly the pre-fix rendering.

    Rows with no ``id`` pass through untouched (they cannot be keyed), and rows
    the reader's SPARSE map does not mention get empty lists — "has none" must
    not be indistinguishable from "never looked up".
    """
    try:
        edges = await storage.list_adr_supersedes(project_id=project_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "adr supersede attach failed project_id=%s: %s — rows keep empty edge lists",
            project_id,
            exc,
        )
        edges = {}
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("id") is None:
            out.append(row)
            continue
        entry = edges.get(int(row["id"])) or {}
        out.append(
            {
                **row,
                "supersedes": list(entry.get("supersedes", [])),
                "superseded_by": list(entry.get("superseded_by", [])),
            }
        )
    return out


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
    return {"rows": await _attach_supersedes(storage, str(payload["project_id"]), rows)}


@observe(tier="boundary", metric="backend.admin.ledger.get_adr_row")
async def get_adr_row(payload: dict) -> dict:
    """Single ``adr`` lookup by id, SCOPED to a project. payload: {id, project_id}.

    Ledger task 188: ``project_id`` is REQUIRED. ``adr.id`` is one global
    ``AUTO_INCREMENT`` shared across every project, so an unscoped by-id lookup
    returns foreign rows routinely and ``adr_get`` merges their metadata onto
    this project's body page. Refusing beats defaulting to ``None``: a silent
    degrade to a corpus-wide lookup is the defect itself, and core's
    ``adr_get`` — the only caller — always holds a project_id post-ADR-0227.
    """
    project_id = payload.get("project_id")
    if not project_id:
        return {
            "ok": False,
            "error": (
                "get_adr_row requires project_id — adr.id is a global "
                "AUTO_INCREMENT shared across projects, so an unscoped lookup "
                "can return another project's row (ledger task 188)"
            ),
        }
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        row = await storage.get_adr_row(int(payload["id"]), project_id=str(project_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_adr_row error id=%s: %s", payload.get("id"), exc)
        return {"ok": False, "error": str(exc)}
    if row is None:
        return {"row": None}
    attached = await _attach_supersedes(storage, str(project_id), [row])
    return {"row": attached[0]}


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
    except AdminRefusal:
        raise
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
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_stale_projects error: %s", exc)
        return {"ok": False, "error": str(exc)}
    return result
