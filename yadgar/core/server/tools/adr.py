# ruff: noqa: PLR0913  — adr_add intentionally has 11 params (ADR schema
#   has 10 mandatory content fields; FastMCP derives JSON Schema from flat keyword args).
#   Collapsing into **kwargs loses schema enforcement. PERMANENT — see .complexity-allowlist.json.
"""ADR (Architecture Decision Record) MCP tool registrations.

Car F (0047 §7, ADR-tools re-pointed) re-points the three ADR MCP tools onto
the MariaDB ledger path:

  * ``adr_add``  — forward ``create_adr_row`` to the backend ledger (the new
    ID source of truth per ADR-0197: AUTO_INCREMENT id IS the ADR number),
    write the per-ADR wiki body page via the existing ``_wiki_write_canonical``
    seam (D4 — body stays in SurrealDB), then forward ``set_adr_body_slug`` to
    link the row to the body page slug. Supersede targets link via
    ``add_adr_supersedes`` + status flip on the target row.
  * ``adr_get``  — read the body page from SurrealDB (unchanged, D4), then
    forward ``get_adr_row`` to merge the ledger row's metadata into the
    response per D5 (additive-only contract; §4 step 3). Car F adds the
    ``baseline_hash`` / ``content_hash`` keys per ADR-0209 §14.3.
  * ``adr_list`` — forward ``list_adr_rows`` (project-scoped, status-filtered)
    over the core PTC → backend HTTP chain (§15), map the ledger row dict onto
    the 7-key consumer shape ``{adr_id, status, date, title, supersedes,
    superseded_by, slug}`` that the §7 live consumers cite. The
    ``total`` / ``truncated`` / ``next_offset`` envelope semantics are
    preserved exactly.

Decisions:
- @_tool(power=True) — write-tool convention (adr_add); reads are also power to
  match the module (adr_get/adr_list are cheap reads but sit next to the write).
- @observe decorators — every public method carries one (I33). The body-page
  write is still core-side (D4 — wiki body stays in SurrealDB); only the
  metadata/index row moves to MariaDB.
- IDs come from the ledger ``create_adr_row`` AUTO_INCREMENT (ADR-0197) —
  no more ``_next_adr_id`` / ``_committed_page_max_id`` / index-page scan.
- The ``_adr_log_lock`` / ``parse_index_rows`` / ``_build_index_content`` /
  ``_render_index_row`` / ``_assemble_index_rows`` helpers are now DORMANT in
  this module; Car G deletes them.

Module layout (car/adr-split, unchanged):
  adr_index.py  — slug helpers, ID assignment, index parse/render (DORMANT for adr_add)
  adr_render.py — body builder, tag helpers, supersede handling (DORMANT for adr_add)
  adr.py        — MCP tool handlers + lock + write-ok predicate (re-pointed)
"""

from __future__ import annotations

import re
from typing import Any

from yadgar._shared.errors import UnresolvedProjectError
from yadgar._shared.observability.observe import observe
from yadgar.core.forward import _forward_admin
from yadgar.core.server._app import _tool

# Car M (0047 §7, §16.6): cross-project ``project=`` override on the ADR MCP
# tools. C5 (0047 PR#40 §5) deleted the directory-derived and "global" tiers
# under it, so the chain is override → session → raise, and the unguarded
# ``from yadgar.core.identity import derive_project_id`` that forced C2 to be
# additive went with the three call sites it fed.
from yadgar.core.server.tools._project_param import (
    InvalidProjectOverrideError,
    resolve_effective_project,
)
from yadgar.core.server.tools.adr_index import adr_page_slug
from yadgar.core.server.tools.adr_render import (
    _REQUIRED_FIELDS,
    _VALID_STATUSES,
    _adr_tags,
    _assemble_index_rows,
    _build_adr_body,
    _canonical_adr_payload,
    _flip_superseded_target,
    _parse_supersedes,
)
from yadgar.core.server.tools.project import _resolve_project_root
from yadgar.core.server.tools.wiki import (
    _wiki_write_canonical,
    wiki_read,
)

# A wait=True canonical write that is still QUEUED after wait_timeout WILL commit
# on the next drain — it is NOT a failure. Only these terminal reasons are fatal.
_FATAL_WRITE_REASONS: frozenset[str] = frozenset(
    {"duplicate_detected", "rejected", "content_too_large", "invalid_unicode_surrogates"}
)


@observe(exempt="trivial dict-field predicate; no I/O, no error branch worth spanning")
def _write_ok(result: dict) -> bool:
    """True when a canonical write committed OR is safely queued (converges).

    ``_wiki_write_canonical(wait=True)`` returns ``stored:False, reason:wait_timeout,
    queued:True`` when the drainer did not commit within the wait budget — the write
    is still queued and WILL land. That is NOT a failure for the ADR body-page
    write (the row + body_slug link live in the ledger, not the wiki).
    """
    if result.get("stored") is not False:
        return True
    if result.get("queued"):
        return True  # wait_timeout — converges on next drain
    reason = str(result.get("reason", ""))
    if reason.startswith("blocked_by_policy"):
        return False
    return reason not in _FATAL_WRITE_REASONS if reason else False


# ── Ledger row → consumer-shape mapping (Car F refactor) ───────────────────────
# Car G's ``_build_adr_log`` re-point and Car I's analogous agent-prompt
# mapping reuse this helper. Stays in ``adr.py`` (NOT ``project.py`` — avoids
# the circular import noted at ``project.py:1799-1800``).
#
# Each ``row`` dict comes from ``list_adr_rows`` / ``get_adr_row`` (backend
# ``admin_exec/ledger.py``); schema per §3.5 of the master plan:
#   {id, project_id, title, status, decided_on, subsystem, tier, body_slug,
#    created_at, updated_at}
# Car G populates the ``adr_supersedes`` join table during the seed; F emits
# empty values for ``supersedes`` / ``superseded_by`` until that join is
# readable (no ``list_adr_supersedes`` read method exists yet — out of F scope).


@observe(tier="stage", metric="tools.adr._row_to_adr_list_entry")
def _row_to_adr_list_entry(row: dict) -> dict:
    """Map one ledger ``adr`` row dict onto the 7-key consumer shape.

    Returns ``{adr_id, status, date, title, supersedes, superseded_by, slug}``.
    The 7-key shape is the load-bearing contract for ``_build_adr_log``
    (project.py:1787) and ``adr_render._assemble_index_rows`` (Car G re-points
    it; F preserves the shape so the contract survives the re-point).
    """
    adr_id_int = int(row["id"])
    return {
        "adr_id": f"ADR-{adr_id_int:04d}",
        "status": row.get("status") or "open",
        "date": row.get("decided_on") or "",
        "title": row.get("title") or "",
        "supersedes": row.get("supersedes") or "none",
        "superseded_by": row.get("superseded_by") or "-",
        "slug": row.get("body_slug") or "",
    }


# ── Tools ──────────────────────────────────────────────────────────────────────


@observe(
    exempt="trivial dict-field validation; no I/O, no external call, no error branch worth spanning"
)
def _validate_adr_add_input(provided: dict[str, str]) -> dict | None:
    """Validate the 10 required schema fields + status. Returns an error dict on
    failure, None when all checks pass. Extracted from ``adr_add`` for fn_loc
    (I13) — the validation surface is large but flat (no logic branching)."""
    for field in _REQUIRED_FIELDS:
        val = provided.get(field)
        if not val or not str(val).strip():
            return {"ok": False, "error": f"missing required field: {field!r}"}
    status = provided.get("status")
    if status not in _VALID_STATUSES:
        return {
            "ok": False,
            "error": (f"invalid status {status!r}; must be one of {sorted(_VALID_STATUSES)}"),
        }
    return None


@observe(
    exempt="trivial project-root resolve; no I/O, no external call, no error branch worth spanning"
)
def _resolve_adr_add_context(directory: str, project: str | None) -> dict | tuple[str, str]:
    """Resolve ``directory`` → ``project_root`` + ``project_id``. Returns
    ``(resolved, project_id)`` on success, an error dict on failure.
    Extracted from ``adr_add`` for fn_loc (I13).

    C5 (0047 PR#40 §5): the ``derive_project_id(cwd=resolved)`` call is gone.
    ``project_id`` now comes from the caller's ``project=`` and nowhere else —
    a directory is a filesystem hint, and this process cannot see the tree it
    names. Absence is an ``UnresolvedProjectError`` returned as the tool's
    structured envelope so the agent reading it learns what to pass."""
    try:
        resolved = _resolve_project_root(directory)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"cannot resolve project root: {exc}"}
    try:
        project_id = resolve_effective_project(
            project=project,
            directory=resolved,
            session_project=None,
            tool="adr_add",
        )
    except UnresolvedProjectError as exc:
        return {"ok": False, **exc.payload}
    except InvalidProjectOverrideError as exc:
        return {"ok": False, "error": f"adr_add: {exc}"}
    return resolved, project_id


@observe(tier="hot", metric="tools.adr.adr_add")
@_tool(power=True)
def adr_add(
    directory: str,
    title: str,
    status: str,
    date: str,
    context: str,
    decision: str,
    rationale: str,
    alternatives: str,
    consequences: str,
    revisit_trigger: str,
    supersedes: str,
    *,
    project: str | None = None,
    tier: str | None = None,
    subsystem: str | None = None,
) -> dict:
    """Create a new Architecture Decision Record (ADR).

    Car F: writes one ``adr`` ledger row (MariaDB; ADR-0197: AUTO_INCREMENT id
    IS the ADR number) + one CANONICAL wiki body page (SurrealDB, D4) linked
    by ``body_slug``. Supersede targets are linked via ``adr_supersedes`` with
    the target row's status flipped to ``superseded`` (D23, status-flip only;
    the page-type retype is Car G).

    Car M (0047 §7, §16.6): the OPTIONAL ``project=`` override lets a caller
    write an ADR into another project's namespace without leaving the current
    working tree. Precedence: ``project`` (override) > ``session_project`` >
    ``directory``-derived (Car A0 ``derive_project_id``) > ``"global"``. The
    validated project_id is forwarded to the backend ledger write
    (``create_adr_row(project_id=...)``) so the row stamps the override
    namespace; the body page's slug follows the same project_id (D32 ③
    scheme — ``{project_id}_adr-NNNN``). When BOTH ``project`` and
    ``directory`` are supplied, ``project`` wins and ``directory`` is logged-
    and-ignored (§9 [VERIFY]). The deep registry check is backend-side
    (`_ensure_project_exists_sync`, §15 / ADR-0078); core enforces the
    type-level guard.

    Car H: accepts ``tier`` (D27 enum: ``binding|historical``) and
    ``subsystem`` (D28 explicit; §10 Q2 normalizer → lowercase + trim, empty
    → None). On success the per-subsystem rollup page is regenerated via
    ``_regenerate_subsystem_rollup`` (§10 Q1 on-write trigger, D29).

    Args:
        directory: Absolute path to the project root.
        title: Short human-readable title (e.g. "Use SurrealDB for storage").
        status: One of: open, accepted, superseded, rejected, deprecated.
        date: ISO date string (e.g. "2026-06-25").
        context: Background / problem statement.
        decision: The decision that was made.
        rationale: Why this decision was made.
        alternatives: Alternatives that were considered.
        consequences: Known / expected consequences.
        revisit_trigger: Condition that would trigger revisiting this decision.
        supersedes: "none" or a comma-separated list of superseded ADR IDs (e.g. "ADR-0002").
        tier: D27 tier — ``"binding"`` or ``"historical"``. OMIT IT and the
            value is derived from ``status`` (ledger task 213):
            ``superseded``/``rejected``/``deprecated`` → ``"historical"``,
            ``open``/``accepted`` → ``"binding"``. An explicit value wins.
            Never left NULL — a NULL-tier row is invisible to every
            ``adr_list`` filter value.
        subsystem: D28 explicit subsystem value. Normalized to lowercase + trim
            (§10 Q2); empty after normalize → None (no rollup regen fires).

    Returns:
        {"adr_id": "ADR-NNNN", "slug": "<project_id>_adr-NNNN"} on success.
        {"error": "...", "ok": False} on validation failure or storage error.
    """
    validation_error = _validate_adr_add_input(
        {
            "title": title,
            "status": status,
            "date": date,
            "context": context,
            "decision": decision,
            "rationale": rationale,
            "alternatives": alternatives,
            "consequences": consequences,
            "revisit_trigger": revisit_trigger,
            "supersedes": supersedes,
        }
    )
    if validation_error is not None:
        return validation_error

    # C5 (0047 PR#40 §5): one resolution, inside ``_resolve_adr_add_context``.
    # The old shape resolved a directory-derived project_id first and then let
    # ``project=`` overwrite it; with the derivation tier deleted there is only
    # the caller's value, so the second pass was dead code that re-ran the same
    # call. The override is the namespace stamp on the ledger row AND on the
    # body-page slug; ``directory`` stays as the wiki body's directory_context
    # (the directory is the file-system hint, the project_id is the namespace).
    ctx = _resolve_adr_add_context(directory, project)
    if isinstance(ctx, dict):
        return ctx
    resolved, project_id = ctx

    # ── Car H: §10 Q2 subsystem normalizer (lowercase + trim; empty → None).
    subsystem_normalized = _normalize_subsystem(subsystem)

    # ── Car G: the per-project ``_adr_log_lock`` is GONE. The ledger
    # ``create_adr_row`` AUTO_INCREMENT serialises ID allocation backend-side
    # (ADR-0197), so the lock no longer guards a sequence; the body-page write
    # (wait=True) + ledger row + set_adr_body_slug path is linearised by the
    # backend INSERT itself.
    step1 = _allocate_adr_ledger_row(
        project_id, title, status, date, tier=tier, subsystem=subsystem_normalized
    )
    if isinstance(step1, dict):
        return step1
    adr_id_int, adr_id = step1

    step2 = _write_adr_body_page(
        resolved=resolved,
        project_id=project_id,
        adr_id=adr_id,
        adr_id_int=adr_id_int,
        fields={
            "title": title,
            "status": status,
            "date": date,
            "context": context,
            "decision": decision,
            "rationale": rationale,
            "alternatives": alternatives,
            "consequences": consequences,
            "revisit_trigger": revisit_trigger,
            "supersedes": supersedes,
        },
    )
    if isinstance(step2, dict):
        return step2
    page_slug = step2

    step3 = _link_adr_body_slug(adr_id_int, adr_id, page_slug)
    if isinstance(step3, dict):
        return step3

    # Step 4: supersede-link rows + flip target status (D23 status flip).
    _link_adr_supersede_targets(adr_id_int, supersedes)

    # Step 5: §10 Q1 on-write rollup regen — only when subsystem is set
    # (a rollup keyed on ``None`` is meaningless; the page-by-subsystem
    # taxonomy collapses when subsystem is absent).
    if subsystem_normalized is not None:
        _trigger_subsystem_rollup_regen(project_id=project_id, subsystem=subsystem_normalized)

    return {"adr_id": adr_id, "slug": page_slug}


# ── Car H helpers (extracted for I13 fn_loc + testability) ──────────────────


# D27 statuses whose ADRs are ``historical`` rather than ``binding``.
# TWIN IMPLEMENTATION: ``yadgar.backend.admin_exec.seed_adr_tier_subsystem``
# carries the same frozenset + mapping for the one-shot backfill. It is
# duplicated rather than shared because ``core`` does not import ``backend``
# (the layering rule ``_trigger_subsystem_rollup_regen`` names); D27 in
# ``docs/plans/task-table-refactor-2026-07-29.md:295`` is the shared source of
# truth for both copies.
_HISTORICAL_STATUSES: frozenset[str] = frozenset({"superseded", "rejected", "deprecated"})


@observe(exempt="trivial status→tier mapping; no I/O, no error branch worth spanning")
def _tier_for_status(status: str | None) -> str:
    """Derive a D27 ``tier`` from an ADR ``status`` (ledger task 213).

    ``superseded`` | ``rejected`` | ``deprecated`` → ``"historical"``;
    ``open`` | ``accepted`` (and anything unrecognised) → ``"binding"``.

    This is the DEFAULT ``adr_add``'s docstring has promised since Car H and
    never applied: ``tier`` was forwarded verbatim, so a caller that omitted
    it wrote a row with ``tier=NULL``. ``adr_list`` defaults its filter to
    ``"binding"`` and forwards it verbatim, so a NULL-tier row matches NEITHER
    ``"binding"`` NOR ``"historical"`` — it is unreachable through every
    argument value the tool accepts.

    A blanket ``"binding"`` default would be WRONG for the three historical
    statuses: it puts superseded/rejected/deprecated ADRs into the default
    list, which is exactly what D27 excludes them from.
    """
    if status and status in _HISTORICAL_STATUSES:
        return "historical"
    return "binding"


@observe(exempt="trivial string normalisation; no I/O, no error branch worth spanning")
def _normalize_subsystem(value: str | None) -> str | None:
    """§10 Q2 subsystem normalizer: lowercase + trim; empty → None.

    The seed reads `subsystem` back from rows already-stamped by this same
    rule; round-trips are stable. A future migration to a controlled
    vocabulary would replace this function.
    """
    if value is None:
        return None
    normalized = str(value).strip().lower()
    return normalized or None


@observe(tier="stage", metric="tools.adr._trigger_subsystem_rollup_regen")
def _trigger_subsystem_rollup_regen(*, project_id: str, subsystem: str) -> None:
    """§10 Q1 on-write trigger — fire the per-subsystem rollup regen.

    Lazy import: ``_regenerate_subsystem_rollup`` lives in the backend
    ``admin_exec.rollup`` module (core ↔ backend layering rule forbids a
    direct import — the import is deferred inside the call so the module
    loads only when the trigger fires). The seam is the same canonical
    admin-op forward path the rest of ``adr_add`` uses (``_forward_admin``)
    so the call carries no extra latency on the hot path.
    """
    try:
        _forward_admin(
            "run_rollup_regen",
            {"project_id": project_id, "subsystem": subsystem},
        )
    except Exception as exc:  # noqa: BLE001
        import logging  # noqa: PLC0415

        logging.getLogger(__name__).warning(
            "adr_add rollup regen failed: project_id=%s subsystem=%s err=%s",
            project_id,
            subsystem,
            exc,
        )


@observe(tier="stage", metric="tools.adr._allocate_adr_ledger_row")
def _allocate_adr_ledger_row(
    project_id: str,
    title: str,
    status: str,
    date: str,
    *,
    tier: str | None = None,
    subsystem: str | None = None,
) -> dict | tuple[int, str]:
    """Step 1 of ``adr_add``: forward ``create_adr_row`` to the backend ledger
    (ADR-0197 — id IS the ADR number). Returns ``(adr_id_int, adr_id)`` on
    success or an error dict.

    Car H: threads ``tier`` (D27) and ``subsystem`` (D28, §10 Q2-normalized
    in the caller) onto the row at INSERT time — both columns are inert on
    rows created before Car H landed, so the seed op (``seed_adr_tier_subsystem``)
    backfills them in place afterward.

    Ledger task 213: an OMITTED ``tier`` is derived from ``status`` via
    ``_tier_for_status`` rather than forwarded as ``None``. A NULL tier is
    unreachable through every ``adr_list`` argument value. An explicitly
    supplied ``tier`` still wins — the derivation is a DEFAULT, not an
    override.
    """
    try:
        result = _forward_admin(
            "create_adr_row",
            {
                "project_id": project_id,
                "title": title,
                "status": status,
                "decided_on": date,
                "tier": tier or _tier_for_status(status),
                "subsystem": subsystem,
            },
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"create_adr_row forward failed: {exc}"}
    row = result.get("row") if isinstance(result, dict) else None
    if row is None:
        err = result.get("error") if isinstance(result, dict) else "unknown"
        return {"ok": False, "error": f"create_adr_row returned no row: {err}"}
    adr_id_int = int(row["id"])
    adr_id = f"ADR-{adr_id_int:04d}"
    return adr_id_int, adr_id


@observe(tier="stage", metric="tools.adr._write_adr_body_page")
def _write_adr_body_page(
    *,
    resolved: str,
    project_id: str,
    adr_id: str,
    adr_id_int: int,
    fields: dict[str, str],
) -> dict | str:
    """Step 2 of ``adr_add``: write the body page (D4 — wiki write stays
    core-side). Slug uses the D32 ③ scheme: body_slug = {project_id}_adr-NNNN
    where project_id's `/` becomes `_`. Returns the slug on success or an
    error dict. Extracted from ``adr_add`` (I13 fn_loc)."""
    page_slug = f"{project_id.replace('/', '_')}_adr-{adr_id_int:04d}"
    page_content = _build_adr_body(adr_id=adr_id, **fields)
    page_payload = _canonical_adr_payload(
        page_slug, page_content, "decision", _adr_tags(adr_id, fields["status"]), resolved
    )
    # C4 (0047 PR#40 §5): stamp the project_id the caller ALREADY resolved for
    # the ledger row, so the body page and its row agree across the two engines
    # and ``_wiki_write_canonical`` does not re-derive a value we hold.
    page_payload["project_id"] = project_id
    page_result = _wiki_write_canonical(page_payload, wait=True)
    if not _write_ok(page_result):
        return {
            "ok": False,
            "error": f"per-ADR page write failed: {page_result.get('reason', 'unknown')}",
            "adr_id": adr_id,
        }
    return page_slug


@observe(tier="stage", metric="tools.adr._link_adr_body_slug")
def _link_adr_body_slug(adr_id_int: int, adr_id: str, page_slug: str) -> dict | None:
    """Step 3 of ``adr_add``: forward ``set_adr_body_slug`` to link the ledger
    row to the body slug (D4 — only the slug pointer moves). Returns an error
    dict on failure, None on success. Extracted from ``adr_add`` (I13 fn_loc)."""
    try:
        slug_result = _forward_admin(
            "set_adr_body_slug",
            {"id": adr_id_int, "body_slug": page_slug},
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"set_adr_body_slug forward failed: {exc}",
            "adr_id": adr_id,
            "slug": page_slug,
        }
    if isinstance(slug_result, dict) and slug_result.get("ok") is False:
        return {
            "ok": False,
            "error": f"set_adr_body_slug failed: {slug_result.get('error', 'unknown')}",
            "adr_id": adr_id,
            "slug": page_slug,
        }
    return None


@observe(tier="stage", metric="tools.adr._link_adr_supersede_targets")
def _link_adr_supersede_targets(adr_id_int: int, supersedes: str) -> None:
    """Step 4 of ``adr_add``: forward ``add_adr_supersedes`` for each target.
    Best-effort — failure logs and continues so the row + body link survive.
    Extracted from ``adr_add`` (I13 fn_loc)."""
    import logging  # noqa: PLC0415

    target_ids = _parse_supersedes(supersedes)
    for tid in target_ids:
        target_int = int(tid.split("-")[1])
        try:
            _forward_admin(
                "add_adr_supersedes",
                {"adr_id": adr_id_int, "supersedes_id": target_int},
            )
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "adr_add supersede link failed: adr_id=%s target=%s err=%s",
                f"ADR-{adr_id_int:04d}",
                tid,
                exc,
            )


@observe(tier="hot", metric="tools.adr.adr_get")
@_tool(power=True)
def adr_get(directory: str, adr_id: str, *, project: str | None = None) -> dict:
    """Read a single ADR's body page + ledger row (merged per D5).

    Car F: the body page fetch is UNCHANGED (D4 — wiki body stays in SurrealDB).
    The ledger row's metadata is MERGED into the response (D5: row owns ALL
    metadata; page owns ONLY prose). The merge is ADDITIVE-ONLY — pre-migration
    keys (`content`, `slug`, `directory_context`, tags, …) remain a SUBSET of
    post-migration keys (§4 step 3 acceptance gate).

    Car F adds ``baseline_hash`` / ``content_hash`` per ADR-0209 §14.3
    (row-side — changes only on seed/adopt; row+page content_hash is the
    desync signal).

    Car M (0047 §7, §16.6): the OPTIONAL ``project=`` override lets a caller
    read another project's ADR. Precedence: ``project`` (override) >
    ``session_project`` > ``directory``-derived (Car A0) > ``"global"``.
    When supplied, the validated project_id is forwarded to the backend
    ledger read (``get_adr_row(project_id=...)``) so the row lookup is
    namespaced to the override. When BOTH ``project`` and ``directory`` are
    supplied, ``project`` wins and ``directory`` is logged-and-ignored
    (§9 [VERIFY]).

    Args:
        directory: Absolute path to the project root.
        adr_id: "ADR-NNNN" (case-insensitive; "adr-1" / "1" also accepted).

    Returns:
        The merged body+row dict, or {"error": "..."} if absent.
    """
    try:
        resolved = _resolve_project_root(directory)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"cannot resolve project root: {exc}"}

    m = re.search(r"(\d+)", adr_id or "")
    if not m:
        return {"error": f"invalid adr_id {adr_id!r}; expected 'ADR-NNNN'"}
    adr_id_int = int(m.group(1))

    # C5 (0047 PR#40 §5): ``derive_project_id(cwd=resolved)`` deleted. The
    # namespace stamp on the ledger row comes from ``project=`` and nothing
    # else. The deep registry check is backend-side
    # (`_ensure_project_exists_sync`, §15 / ADR-0078); core enforces the
    # type-level guard.
    try:
        project_id = resolve_effective_project(
            project=project,
            directory=resolved,
            session_project=None,
            tool="adr_get",
        )
    except UnresolvedProjectError as exc:
        return dict(exc.payload)
    except InvalidProjectOverrideError as exc:
        return {"error": f"adr_get: {exc}"}

    # Ledger task 214: the ROW is fetched FIRST so the body read can use the
    # row's authoritative ``body_slug`` instead of deriving one.
    row_result = _fetch_adr_ledger_row(adr_id, adr_id_int, project_id=project_id)
    body = _fetch_adr_body_page(project_id, adr_id_int, row_result)
    return _build_adr_get_response(body, row_result)


@observe(tier="stage", metric="tools.adr._fetch_adr_body_page")
def _fetch_adr_body_page(
    project_id: str,
    adr_id_int: int,
    row_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """D4 body fetch — keyed on the PROJECT, never on the working directory.

    Ledger task 214: this used to take the caller's ``resolved`` directory and
    (a) pass it as ``wiki_read(directory=...)`` and (b) derive the legacy
    fallback slug from ``os.path.basename(resolved)``. Both are directory-keyed
    where they must be project-keyed, so a cross-project read was structurally
    impossible:

      * ``wiki_read`` scopes its lookup on ``directory`` and NOT on
        ``project=`` (``wiki.py`` → ``read_by_directory(slug, _caller_dir)``;
        ``project=`` reaches only the validation + the cache key). Narrowing to
        the caller's tree therefore hides every page whose
        ``directory_context`` belongs to the project being read. Measured
        2026-08-19: the correct slug ``quinyx_flux_adr-0016`` resolves with
        ``project=`` alone and NOT found with ``directory=`` pointing at the
        yadgar tree.
      * the legacy slug came out ``yadgar-adr-0016`` while reading a
        ``quinyx/flux`` ADR — the caller's basename, which is the misleading
        error the symptom surfaced.

    ``directory`` is now dropped entirely (ADR-0233 retires it as a scoping
    key) and the candidate slugs come from the resolved ``project_id``. The
    D32 ③ slug is project-qualified and opaque/immutable (ADR-0211), so a
    slug-alone match cannot cross projects.

    Candidate order:
      1. the ROW's stored ``body_slug`` — authoritative — but ONLY when it is
         consistent with the resolved project_id. ``get_adr_row`` discards
         ``project_id`` (ledger task 188, out of scope here), so an
         inconsistent slug means a foreign row leaked; honouring it would
         serve another project's PROSE, not merely its metadata.
      2. the derived D32 ③ slug ``{project_id with / → _}_adr-NNNN``.
      3. the legacy ``{basename(project_id)}-adr-NNNN`` for the pages Car L
         has not re-slugged. Residual risk (pre-existing, unchanged by this
         car): this shape is basename-derived, so two projects sharing a repo
         name can collide on it. It is the LAST rung and fires only when both
         project-qualified candidates miss.
    """
    prefix = project_id.replace("/", "_")
    derived_slug = f"{prefix}_adr-{adr_id_int:04d}"
    legacy_slug = f"{project_id.rsplit('/', 1)[-1]}-adr-{adr_id_int:04d}"

    candidates: list[str] = []
    row = row_result.get("row") if isinstance(row_result, dict) else None
    row_slug = str(row.get("body_slug") or "") if isinstance(row, dict) else ""
    if row_slug and row_slug.startswith(prefix):
        candidates.append(row_slug)
    for slug in (derived_slug, legacy_slug):
        if slug not in candidates:
            candidates.append(slug)

    body: dict[str, Any] = {"error": f"Wiki page '{derived_slug}' not found"}
    for slug in candidates:
        body = wiki_read(slug, project=project_id)
        if "error" not in body:
            return body
    return body


@observe(tier="stage", metric="tools.adr._fetch_adr_ledger_row")
def _fetch_adr_ledger_row(
    adr_id: str,
    adr_id_int: int,
    *,
    project_id: str | None = None,
) -> dict[str, Any] | None:
    """Ledger row fetch — returns the row envelope dict (or None on forward
    failure). Extracted from ``adr_get`` for I13 cyclomatic.

    Car M (0047 §7, §16.6): when ``project_id`` is supplied (the caller
    provided ``project=`` and the override won), it is forwarded to the
    backend ``get_adr_row`` op so the row lookup is namespaced to that
    project_id. The deep registry check is backend-side
    (`_ensure_project_exists_sync`, §15 / ADR-0078); core enforces the
    type-level guard.
    """
    payload: dict[str, Any] = {"id": adr_id_int}
    if project_id is not None:
        payload["project_id"] = project_id
    try:
        return _forward_admin("get_adr_row", payload)
    except Exception as exc:  # noqa: BLE001 — merge is best-effort
        import logging  # noqa: PLC0415

        logging.getLogger(__name__).warning(
            "adr_get get_adr_row forward failed: adr_id=%s err=%s", adr_id, exc
        )
        return None


@observe(tier="stage", metric="tools.adr._build_adr_get_response")
def _build_adr_get_response(
    body: dict[str, Any],
    row_result: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge body + row metadata per D5 (additive-only). Reflects row-side
    status in merged tags. Extracted from ``adr_get`` for I13 cyclomatic."""
    row_metadata: dict[str, Any] = {}
    if isinstance(row_result, dict):
        row = row_result.get("row")
        if isinstance(row, dict):
            row_metadata = _row_to_response_metadata(row)
    merged: dict[str, Any] = dict(body)
    merged.update(row_metadata)
    if isinstance(merged.get("tags"), list) and isinstance(row_result, dict):
        merged = _reflect_row_status_in_tags(merged, row_result.get("row") or {})
    return merged


@observe(tier="stage", metric="tools.adr._row_to_response_metadata")
def _row_to_response_metadata(row: dict) -> dict[str, Any]:
    """Map ledger row fields onto the D5 additive metadata keys. ADR-0209
    §14.3: baseline_hash + content_hash keys. Extracted for I13."""
    return {
        "date": row.get("decided_on") or "",
        "rationale": "",  # prose lives on the body page (D4)
        "alternatives": "",  # ditto
        "revisit_trigger": "",  # ditto
        "supersedes": row.get("supersedes") or "none",
        "subsystem": row.get("subsystem") or "",
        "tier": row.get("tier") or "",
        "baseline_hash": row.get("baseline_hash") or "",
        # Car F: content_hash mirrors row+page (regenerated on every write);
        # Car G seeds the initial value from the existing page hash.
        # ADR-0209 §14.3.
        "content_hash": row.get("content_hash") or "",
    }


@observe(tier="stage", metric="tools.adr._reflect_row_status_in_tags")
def _reflect_row_status_in_tags(merged: dict[str, Any], row: dict) -> dict[str, Any]:
    """Replace any ``adr-status:*`` tag in ``merged['tags']`` with the
    row-side status (D5: row owns ALL metadata). Extracted from ``adr_get``
    for I13 cyclomatic."""
    row_status = row.get("status")
    if not row_status:
        return merged
    status_tag = f"adr-status:{row_status}"
    tags: list[str] = []
    seen = False
    for t in merged.get("tags", []):
        if isinstance(t, str) and t.startswith("adr-status:"):
            tags.append(status_tag)
            seen = True
        else:
            tags.append(t)
    if not seen:
        tags.append(status_tag)
    merged["tags"] = tags
    return merged


@observe(tier="hot", metric="tools.adr.adr_list")
@_tool(power=True)
def adr_list(
    directory: str,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    *,
    project: str | None = None,
    tier: str | None = "binding",
    subsystem: str | None = None,
) -> dict:
    """List ADRs from the ledger; optional status/tier filter + pagination.

    Car F: re-pointed from ``wiki_read(index_slug)`` + ``parse_index_rows`` to
    ``list_adr_rows`` over the core PTC → backend HTTP path. Return shape is
    UNCHANGED: ``{"adrs": [7-key rows], "count": N}`` plus optional
    ``total`` / ``truncated: True`` / ``next_offset`` when the page is
    truncated.

    Car M (0047 §7, §16.6): the ``project=`` override lets a caller list
    another project's ADRs. Precedence, as C5 left it: ``project`` (override) >
    ``session_project`` > a raise. The ``directory``-derived and ``"global"``
    tiers this docstring used to name are DELETED (ADR-0227). When
    supplied, the validated project_id is forwarded to the backend
    ``list_adr_rows`` op so the list is namespaced to the override. When
    BOTH ``project`` and ``directory`` are supplied, ``project`` wins and
    ``directory`` is logged-and-ignored (§9 [VERIFY]).

    Car H: defaults ``tier`` to ``"binding"`` (D27 — superseded/rejected/deprecated
    ADRs are tagged ``historical`` and excluded by default). Pass ``tier=None``
    to receive rows of any tier.

    Args:
        directory: Absolute path to the project root.
        status: Optional filter (open/accepted/superseded/rejected/deprecated).
        tier: Optional filter — ``"binding"`` (default, D27) or ``"historical"``.
            ``None`` returns rows of any tier.
        limit: Max ADRs returned per page (default 50). <= 0 means no limit.
        offset: 0-based index of the first ADR returned (default 0). Page forward
            with the `next_offset` value the response carries when truncated.

    Returns:
        ``{"adrs": [...7-key rows...], "count": N}`` (untruncated) or
        ``{"adrs": [...], "count": N, "total": M, "truncated": True,
        "next_offset": K}`` (truncated). Empty list when the ledger has no
        rows for the project.
    """
    try:
        resolved = _resolve_project_root(directory)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"cannot resolve project root: {exc}"}

    # C5 (0047 PR#40 §5): ``derive_project_id(cwd=resolved)`` deleted. The
    # namespace stamp on the ledger list comes from ``project=`` and nothing
    # else.
    try:
        project_id = resolve_effective_project(
            project=project,
            directory=resolved,
            session_project=None,
            tool="adr_list",
        )
    except UnresolvedProjectError as exc:
        return dict(exc.payload)
    except InvalidProjectOverrideError as exc:
        return {"error": f"adr_list: {exc}"}

    try:
        result = _forward_admin(
            "list_adr_rows",
            {
                "project_id": project_id,
                "status": status,
                "tier": tier,
                "subsystem": subsystem,
            },
        )
    except Exception as exc:  # noqa: BLE001
        import logging  # noqa: PLC0415

        logging.getLogger(__name__).warning(
            "adr_list forward failed: project_id=%s err=%s", project_id, exc
        )
        return {"adrs": [], "count": 0}

    rows = result.get("rows", []) if isinstance(result, dict) else []
    if not isinstance(rows, list):
        rows = []

    # Map ledger rows onto the 7-key consumer shape (the load-bearing contract).
    entries = [_row_to_adr_list_entry(r) for r in rows if isinstance(r, dict)]

    total = len(entries)
    start = max(0, offset)
    if limit <= 0:
        window = entries[start:]
    else:
        window = entries[start : start + limit]

    out: dict[str, Any] = {"adrs": window, "count": len(window)}
    if len(window) != total:
        out["total"] = total
        out["truncated"] = True
        if start + len(window) < total:
            out["next_offset"] = start + len(window)
    return out


# ── Public re-exports ───────────────────────────────────────────────────────
# Car G (0047 §7): the parser/serializer/index-render machinery is DELETED
# from ``adr_index.py``; the per-project lock + its globals are DELETED
# here. Only the slug-helper re-export survives (legacy body-fetch fallback
# in ``_fetch_adr_body_page`` — see ``adr_index.py`` module docstring).
__all__ = [
    # MCP tools
    "adr_add",
    "adr_get",
    "adr_list",
    # adr_index public surface — Car G keeps ONLY the slug helper
    "adr_page_slug",
    # adr_render public surface (helpers used by ``adr_add``)
    "_REQUIRED_FIELDS",
    "_VALID_STATUSES",
    "_adr_tags",
    "_assemble_index_rows",
    "_build_adr_body",
    "_canonical_adr_payload",
    "_flip_superseded_target",
    "_parse_supersedes",
    # adr.py-local
    "_FATAL_WRITE_REASONS",
    "_HISTORICAL_STATUSES",
    "_normalize_subsystem",
    "_row_to_adr_list_entry",
    "_tier_for_status",
    "_trigger_subsystem_rollup_regen",
    "_write_ok",
]
