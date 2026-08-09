# ruff: noqa: PLR0913  — task_write intentionally has 10 params (task-table column
#   set: project_id, title, status, state, active_form, plan_path, body_slug, id,
#   blocked_by, blocks). FastMCP derives the JSON Schema from flat keyword args;
#   collapsing into **kwargs loses schema enforcement on the harness side. The
#   `.complexity-allowlist.json` entry covers the params=10 HARD cap for I13;
#   this noqa silences ruff's PLR0913. PERMANENT — see allowlist rationale.
"""Car D — task tools MCP (0047 spine train, §7 row D).

Three task tools that sit on top of the ``task`` ledger table (Car A) and the
backend ``yadgar.backend.admin_exec.ledger`` op bodies (Car B read surface +
Car D write surface).

The task tools are the SQL-backed replacement for the markdown
``{project}-task-list`` wiki page as the **source of truth** for task tracking
(ADR-0133). The wiki page becomes a derived mirror (ADR-0133: harness task
list = source of truth; yadgar ``{project}-task-list`` wiki = stop-hook-derived
mirror via ``wiki_write_task_list``). After this car, task reads/writes go
through the ledger, not through page parsing.

Tools (all in this file):

    task_write  — create or update a task row.
                  ``id`` is ``AUTO_INCREMENT`` and IS the semantic number
                  (ADR-0197, §14.1 — no ``number`` column, no allocation step).
                  Manages ``task_blocked_by`` join edges (D39).

    task_list   — list tasks for a project; defaults to **open-only**
                  ``status IN (pending, in_progress)`` (D37).

    task_get    — fetch one task by ``(project_id, id)``.

§15 / ADR-0078: core NEVER touches the database. These tools forward over HTTP
to the backend PTC. They do NOT call ``_get_storage()`` directly. The
PR #32 reference implementation (``task.py:107-159`` on
``feat/spine-knob-mariadb``) violated §15 by calling
``_get_storage().create_task_row()`` from core — this car fixes that.

Decisions:
  * ``project_id`` arrives from the caller (ADR-0202, §16.6) — never derived
    inside the tool. The cross-project case needs no separate kwarg: passing a
    different ``project_id`` IS the override (§16.6).
  * No ``origin`` parameter — §14.1 dropped ``origin`` as a column (hardcoded
    ``"yadgar"`` everywhere; discriminated nothing). The PR #32 reference passed
    ``origin="yadgar"``; do NOT carry it forward.
  * ``state`` cleared to NULL when ``status`` → ``completed``/``archived``
    (§16.10). Enforced tool-side, not backend-side (one place, visible).
  * Title ≤ 200 chars (D12, reject-on-write).
  * ``id`` keyed payload, NOT ``number`` (§13.2 blocker 2).
"""

from __future__ import annotations

from yadgar._shared.observability.observe import observe
from yadgar.core.forward import _forward_admin
from yadgar.core.server._app import _tool

# ── Constants ───────────────────────────────────────────────────────────────

# D37: default open-only ``status`` filter.
_OPEN_STATUSES: tuple[str, ...] = ("pending", "in_progress")

# §16.10: when ``status`` flips to one of these, ``state`` MUST be cleared to NULL.
_COMPLETED_LIKE_STATUSES: frozenset[str] = frozenset({"completed", "archived"})

# D12: hard cap on title length.
_TITLE_MAX_CHARS: int = 200

# Backwards-compat re-export for tests / callers that imported the old internal
# symbol. NOT part of the MCP surface.
_CREATE_OP = "create_task_row"
_UPDATE_OP = "update_task_row"
_LIST_OP = "list_task_rows"
_GET_OP = "get_task_row"


# ── Validators ──────────────────────────────────────────────────────────────


@observe(exempt="trivial type+length predicate; no I/O, no error branch worth spanning")
def _validate_title(title: object) -> str:
    """Strict type-check the title and enforce the D12 200-char cap.

    Mirrors the PR #32 reference (``task.py:43``). Raises ``ValueError`` on
    rejection — the caller catches and turns it into the tool's error envelope
    so the MCP boundary never raises.
    """
    if not isinstance(title, str):
        raise ValueError(f"title must be a string, got {type(title).__name__}")
    if not title:
        raise ValueError("title must be non-empty")
    if len(title) > _TITLE_MAX_CHARS:
        raise ValueError(f"title length {len(title)} exceeds {_TITLE_MAX_CHARS}-char cap (D12)")
    return title


@observe(exempt="trivial type+emptiness predicate; no I/O, no error branch worth spanning")
def _validate_project_id(project_id: object) -> str:
    """Strict type-check the project_id at the tool boundary.

    Per ADR-0202 the registry FAIL-LOUD check (unknown ``project_id`` →
    REJECTED with a structured error) is enforced at the backend write path
    (Car A0 + Car B), not here. The core tool only rejects non-string/empty
    before forwarding.
    """
    if not isinstance(project_id, str):
        raise ValueError(f"project_id must be a string, got {type(project_id).__name__}")
    if not project_id:
        raise ValueError("project_id must be non-empty")
    return project_id


# ── Public helpers (D11) ───────────────────────────────────────────────────


def _format_task_id(id: int) -> str:
    """D11 — format a task id as the harness-readable ``[id]`` prefix.

    The harness renders tasks as ``"[status] [id] subject"``. D11 says the
    ``[id]`` must be the prefix-reconciled task id, not a fresh session
    handle. Foreign projects get the ``[owner/repo/id]`` form (Car M adds the
    origin segment; here it is local-only). D10 (Crockford base32 display) is
    applied at render time, not stored — the id is stored as an integer.
    """
    return f"[{id}]"


# ── task_write helpers (extracted to keep cyclomatic / params within caps) ─


@observe(
    exempt="pure dict-builder; no I/O, no error branch — §16.10 status→state clearing is an inline conditional, not a spanable operation"
)
def _build_create_payload(
    project_id: str,
    title: str,
    status: str | None,
    state: str | None,
    active_form: str | None,
    plan_path: str | None,
    body_slug: str | None,
) -> dict:
    """Compose the INSERT-side payload for the ``create_task_row`` admin op."""
    # §16.10: if the caller asks for a completed/archived CREATE directly, the
    # state-clearing rule still applies (no stale `state` column on a closed row).
    if status in _COMPLETED_LIKE_STATUSES:
        state = None
    payload: dict = {
        "project_id": project_id,
        "title": title,
        "status": status if status is not None else "pending",
        "state": state if state is not None else "open",
    }
    if active_form is not None:
        payload["active_form"] = active_form
    if plan_path is not None:
        payload["plan_path"] = plan_path
    if body_slug is not None:
        payload["body_slug"] = body_slug
    return payload


@observe(
    exempt="pure dict-builder; no I/O, no error branch — §16.10 status→state clearing is an inline conditional, not a spanable operation"
)
def _build_update_payload(
    task_id: int,
    status: str | None,
    state: str | None,
    active_form: str | None,
    plan_path: str | None,
    body_slug: str | None,
    blocked_by: list[int] | None,
    blocks: list[int] | None,
) -> dict:
    """Compose the partial-UPDATE payload for the ``update_task_row`` admin op.

    §16.10: when ``status`` flips to ``completed``/``archived``, force
    ``state`` to NULL on the wire (even if the caller passed a value) so the
    DB's state column is cleared in the same UPDATE.
    """
    payload: dict = {"id": int(task_id)}
    if status is not None:
        payload["status"] = status
    if status in _COMPLETED_LIKE_STATUSES:
        # §16.10: completed/archived transition always clears state. Even if
        # the caller explicitly passed state="planned", the transition wins.
        payload["state"] = None
    elif state is not None:
        payload["state"] = state
    if active_form is not None:
        payload["active_form"] = active_form
    if plan_path is not None:
        payload["plan_path"] = plan_path
    if body_slug is not None:
        payload["body_slug"] = body_slug
    # D39: join-edge sync — both ``blocked_by`` and ``blocks`` ride the same
    # ``task_blocked_by`` join table; the backend's update path reconciles
    # them (delete-then-insert of the live set).
    if blocked_by is not None:
        payload["blocked_by"] = [int(x) for x in blocked_by]
    if blocks is not None:
        payload["blocks"] = [int(x) for x in blocks]
    return payload


@observe(
    exempt="trivial dispatch wrapper around _validate_*; no I/O, no error branch — callers handle ValueError upstream"
)
def _validate_write_inputs(
    project_id: str,
    title: object,
    *,
    is_update: bool,
) -> tuple[str, str | None]:
    """Run boundary validation and return ``(project_id, normalized_title)``.

    On UPDATE ``title`` may be ``None`` (the tool ignores it — the row keeps
    its original title). On CREATE it is required and length-checked (D12).
    """
    _validate_project_id(project_id)
    if is_update:
        # UPDATE: title is optional. If the caller passes a value, validate
        # it; if they pass None, signal "do not touch the column" via None.
        if title is None:
            return project_id, None
        return project_id, _validate_title(title)
    # CREATE: title is required.
    return project_id, _validate_title(title)


# ── task_write ──────────────────────────────────────────────────────────────


@_tool(power=True)
def task_write(
    project_id: str,
    title: str,
    *,
    id: int | None = None,
    status: str | None = None,
    state: str | None = None,
    active_form: str | None = None,
    plan_path: str | None = None,
    body_slug: str | None = None,
    blocked_by: list[int] | None = None,
    blocks: list[int] | None = None,
) -> dict:
    """Create or update a task row.

    Create (``id`` is None): INSERT; ``id`` is ``AUTO_INCREMENT`` and IS the
    number (ADR-0197, §14.1 — no ``number`` column, no allocation step).
    Returns the generated id. Update (``id`` given): UPDATE the row;
    ``status`` / ``state`` / ``active_form`` / ``plan_path`` / ``body_slug``
    fields are partial-update (None = leave unchanged).

    D12: ``title`` <= 200 chars, reject-on-write.
    D36: ``state`` is NULLABLE; cleared (set to NULL) when ``status``
    transitions to ``completed``/``archived`` (§16.10).
    D39: ``blocked_by`` / ``blocks`` manage ``task_blocked_by`` join rows
    (both FK → task.id CASCADE). One table serves both directions.
    D26: task mutability = free (no lock).
    §14.1: no ``origin`` parameter (column dropped).
    §15 / ADR-0078: forwards over HTTP to the backend PTC — does NOT call
    ``_get_storage()`` directly.

    Error model: ``{"ok": False, "error": "..."}`` — never raise.
    """
    is_update = id is not None
    try:
        project_id, normalized_title = _validate_write_inputs(
            project_id, title, is_update=is_update
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        if is_update:
            assert id is not None  # is_update narrows id: None → int
            payload = _build_update_payload(
                int(id),
                status,
                state,
                active_form,
                plan_path,
                body_slug,
                blocked_by,
                blocks,
            )
            result = _forward_admin(_UPDATE_OP, payload)
            return {"ok": True, "id": result.get("id", id)}
        # CREATE — ``normalized_title`` is the validated string; the param
        # ``title`` may have been None-on-update, but here is_update is False
        # so it was validated into a real string.
        assert normalized_title is not None  # _validate_write_inputs guarantees
        payload = _build_create_payload(
            project_id,
            normalized_title,
            status,
            state,
            active_form,
            plan_path,
            body_slug,
        )
        result = _forward_admin(_CREATE_OP, payload)
        return {"ok": True, "id": result.get("id")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"backend forward failed: {exc}"}


# ── task_list (D37) ─────────────────────────────────────────────────────────


@observe(
    exempt="trivial list-builder; no I/O — D37 filter resolution is a single conditional chain"
)
def _resolve_list_status(
    status: list[str] | None,
    include_closed: bool,
) -> list[str] | None:
    """D37: explicit ``status`` overrides; otherwise ``include_closed`` controls
    whether the open-only default fires."""
    if status is not None:
        return list(status)
    if include_closed:
        return None  # backend returns every row
    return list(_OPEN_STATUSES)


@_tool()
def task_list(
    project_id: str,
    *,
    include_closed: bool = False,
    status: list[str] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """List tasks for the given ``project_id``.

    D37: default to ``status IN (pending, in_progress)``. ``include_closed=True``
    returns all rows (completed/archived). An explicit ``status`` list
    overrides both. Closed/archived rows never appear unless requested — this
    is the mechanism that makes D7 (archive-never-delete) survivable (§11.2).

    ``project_id`` arrives from the caller (session-resolved). The cross-project
    case needs no separate kwarg: passing a different ``project_id`` IS the
    override (§16.6, ADR-0202).

    Returns the list of task row dicts keyed on ``id`` (NOT ``number`` —
    §13.2 blocker 2). Each row includes ``id``, ``project_id``, ``title``,
    ``status``, ``state``, ``active_form``, ``plan_path``, ``body_slug``,
    ``created_at``, ``updated_at``.
    """
    try:
        _validate_project_id(project_id)
    except ValueError:
        return []  # type errors are surfaced via empty list — never raise

    payload: dict = {
        "project_id": project_id,
        "status": _resolve_list_status(status, include_closed),
    }
    if limit is not None and int(limit) != 100:
        payload["limit"] = int(limit)
    if offset is not None and int(offset) > 0:
        payload["offset"] = int(offset)

    try:
        result = _forward_admin(_LIST_OP, payload)
    except Exception:  # noqa: BLE001
        return []  # backend down → empty list (mirrors wiki_query contract)
    return result.get("rows", [])


# ── task_get ────────────────────────────────────────────────────────────────


@_tool()
def task_get(
    project_id: str,
    id: int,
) -> dict | None:
    """Fetch one task by ``(project_id, id)``.

    Returns the row dict (id-keyed, §13.2 blocker 2) or ``None`` if absent.
    The forwarded payload keys on ``id``, NEVER ``number`` (§14.1).
    """
    try:
        _validate_project_id(project_id)
    except ValueError:
        return None

    if not isinstance(id, int) or id <= 0:
        return None

    try:
        result = _forward_admin(_GET_OP, {"id": int(id)})
    except Exception:  # noqa: BLE001
        return None
    return result.get("row")
