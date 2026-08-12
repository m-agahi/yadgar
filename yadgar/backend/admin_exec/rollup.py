"""Car H (0047 spine train) — per-subsystem ADR rollup page regeneration.

D29: derived per-subsystem rollup pages. Each ``(project_id, subsystem)``
pair owns one rollup page (slug ``<project_id>_rollup_<subsystem>``,
``page_type="wiki_rollup"``) that lists every ADR for that subsystem. The
"what governs vacuum?" question (§8 line 628) becomes a one-page read
instead of a 194-entry scan.

§10 Q1 decision (ON-WRITE TRIGGER): the rollup is regenerated from
``adr_add``'s post-commit step. The Car K nightly sweep remains available
as a future backstop — when ``_should_regenerate_rollup`` returns True on
every call, the sweep's policy-dispatch input (if Car K consumes it) is
trivial. The trigger is fired from core (``tools/adr.py::
_trigger_subsystem_rollup_regen``) which imports this module lazily to
avoid a core→backend import cycle.

Mutability: ``wiki_rollup`` is registered with ``mutability="derived"``
(``yadgar/_shared/wiki/policy.py::MUTABILITY_BY_TYPE``); the storage
chokepoint rejects non-sanctioned writes. The regen writer passes
``_sanctioned=True`` so its lifecycle is the SOLE mutator.

Recall: ``recall_disposition="exclude"`` + ``opt_in_tag="rollup"`` —
the page never appears in normal fanout (D22-style disposition); a
targeted ``recall(tags=["rollup"])`` lookup surfaces it.
"""

from __future__ import annotations

import logging
from typing import Any

from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)


def _get_sql_storage() -> Any:
    """The composed ``MariaStorageEngine``, or None when engine #2 is absent.

    Matches the seam at ``admin_exec/ledger.py:63`` so tests patch one
    symbol across the admin_exec surface.
    """
    from yadgar._shared.runtime.lifecycle import _get_sql_storage  # noqa: PLC0415

    return _get_sql_storage()


# ── Rollup body render (pure — easy to unit test) ─────────────────────────────


@observe(exempt="trivial markdown render; no I/O, no external call, no error branch worth spanning")
def _render_rollup_body(rows: list[dict[str, Any]], *, subsystem: str) -> str:
    """Render the rollup body markdown for ``subsystem``.

    Pure function — no I/O. ``rows`` are the ledger-shaped ADR dicts that
    ``MariaStorageEngine.list_adr_rows`` returns (id, title, status,
    decided_on, tier, subsystem, …). When ``rows`` is empty, the body
    still names the subsystem so the page is informative ("no ADRs yet,
    this is the dedicated page for X").
    """
    lines: list[str] = []
    lines.append(f"# Rollup: {subsystem}")
    lines.append("")
    lines.append("## Overview")
    lines.append("")
    lines.append(
        f"Per-subsystem ADR rollup for ``{subsystem}``. "
        f"Regenerated on every ``adr_add`` post-commit (§10 Q1 on-write trigger). "
        f"Excluded from recall by default — targeted via "
        f"``recall(tags=['rollup'])``."
    )
    lines.append("")
    lines.append("## Decisions")
    lines.append("")
    if not rows:
        lines.append(f"_No ADRs for subsystem ``{subsystem}`` yet._")
        lines.append("")
        return "\n".join(lines)

    lines.append(f"_Total ADRs in this subsystem: {len(rows)}._")
    lines.append("")
    for row in rows:
        adr_id_int = int(row.get("id") or 0)
        adr_id = f"ADR-{adr_id_int:04d}"
        title = str(row.get("title") or "(no title)")
        status = str(row.get("status") or "open")
        decided_on = str(row.get("decided_on") or "")
        tier = str(row.get("tier") or "binding")
        body_slug = str(row.get("body_slug") or "")
        date_suffix = f" ({decided_on})" if decided_on else ""
        lines.append(f"- **{adr_id}** — {title}{date_suffix}")
        lines.append(f"  - status: ``{status}``")
        lines.append(f"  - tier: ``{tier}``")
        if body_slug:
            lines.append(f"  - body slug: `{body_slug}`")
    lines.append("")
    return "\n".join(lines)


# ── Rollup page write (sanctioned server-side) ────────────────────────────────


@observe(tier="boundary", metric="backend.admin.rollup._wiki_write_canonical_seam")
def _wiki_write_canonical(payload: dict[str, Any], wait: bool = False) -> dict[str, Any]:
    """Lazy seam to the core sanctioned writer.

    ``_shared/wiki/store.py::update_wiki_page(sanctioned=True)`` enforces the
    mutability gate; the page_type allowlist (``CANONICAL_PAGE_TYPES``,
    including the new ``wiki_rollup``) is a soft accident-guard lifted here.
    Together they reject agent-spoofed rollup writes. The seam avoids the
    core->backend import cycle by going through storage directly.
    """
    storage = _get_sql_storage()
    if storage is None:
        raise RuntimeError("rollup regen requires storage; runtime storage not initialised")

    slug_raw = payload.get("slug")
    title = payload.get("title")
    body = payload.get("content")
    page_type = payload.get("page_type")

    page = storage.get_wiki_page_by_slug(str(slug_raw)) if slug_raw else None
    fields: dict[str, Any] = {"page_type": page_type}
    if title is not None:
        fields["title"] = title
    if body is not None:
        fields["content"] = body

    page_id = int(page["id"]) if page else 0
    result: dict[str, Any] = storage.update_wiki_page(page_id, fields, _sanctioned=True)
    return result


def _rollup_slug(project_id: str, subsystem: str) -> str:
    """Stable slug for the ``(project_id, subsystem)`` rollup.

    Project_id slashes become underscores (D32 ③ scheme — matches the
    per-ADR body slug). The subsystem suffix is the literal lowercase
    value (post-§10 Q2 normalizer).
    """
    return f"{project_id.replace('/', '_')}_rollup_{subsystem}"


@observe(tier="boundary", metric="backend.admin.rollup._regenerate_subsystem_rollup")
async def _regenerate_subsystem_rollup(
    *,
    project_id: str,
    subsystem: str,
    storage: Any | None = None,
) -> dict[str, object]:
    """Regenerate the per-subsystem rollup page.

    SELECTs every ``adr`` row for ``(project_id, subsystem)``, renders the
    rollup body, writes via the canonical wiki path with
    ``page_type="wiki_rollup"`` (allowlist + D26-derived mutability). The
    storage arg is the test-seam injection point; production callers omit
    it and the function pulls from the live runtime lifecycle.

    The function is async because ``MariaStorageEngine.list_adr_rows`` is
    async (asyncmy driver). Test stubs inject a sync ``list_adr_rows``
    that returns a list directly — handled below via ``__await__`` check.

    Returns:
        ``{"ok": True, "slug": ..., "rows_rendered": N}`` on success or
        ``{"ok": False, "error": "..."}`` on failure (never raises —
        matches the admin-op contract).
    """
    if storage is None:
        from yadgar._shared.runtime.lifecycle import _get_sql_storage  # noqa: PLC0415

        storage = _get_sql_storage()
    if storage is None:
        return {
            "ok": False,
            "error": "engine #2 not composed (MariaStorageEngine is None) — "
            "rollup regen cannot read ADR rows",
        }

    try:
        rows_obj = storage.list_adr_rows(
            project_id=project_id,
            status=None,
            tier=None,
            subsystem=subsystem,
        )
        if hasattr(rows_obj, "__await__"):
            rows = await rows_obj
        else:
            rows = rows_obj
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "_regenerate_subsystem_rollup: list_adr_rows failed: project_id=%s subsystem=%s err=%s",
            project_id,
            subsystem,
            exc,
        )
        return {"ok": False, "error": str(exc)}

    if not isinstance(rows, list):
        rows = []

    body = _render_rollup_body(rows, subsystem=subsystem)
    slug = _rollup_slug(project_id, subsystem)

    # Tags carry the policy opt-in key (`rollup`) so a recall caller can
    # target the page via ``recall(tags=["rollup"])`` — the D22-style
    # disposition opt-in surface.
    tags = [
        "rollup",
        f"subsystem:{subsystem}",
        "page-type:wiki_rollup",
        f"project:{project_id}",
    ]

    page_payload: dict[str, Any] = {
        "slug": slug,
        "content": body,
        "page_type": "wiki_rollup",
        "tags": tags,
        "title": f"ADR rollup: {subsystem}",
        "directory_context": project_id,
    }

    try:
        write_result = _wiki_write_canonical(page_payload, wait=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "_regenerate_subsystem_rollup: wiki write failed: slug=%s err=%s",
            slug,
            exc,
        )
        return {"ok": False, "error": str(exc)}

    stored = bool(write_result.get("stored"))
    if not stored and not write_result.get("queued"):
        return {
            "ok": False,
            "error": f"wiki write refused: {write_result.get('reason', 'unknown')}",
            "slug": slug,
        }

    logger.info(
        "_regenerate_subsystem_rollup: regenerated slug=%s rows=%d subsystem=%s",
        slug,
        len(rows),
        subsystem,
    )
    return {"ok": True, "slug": slug, "rows_rendered": len(rows)}


@observe(exempt="trivial predicate; no I/O, no external call, no error branch worth spanning")
def _should_regenerate_rollup(subsystem: str, project_id: str) -> bool:
    """§10 Q1 on-write policy: ALWAYS regenerate when called.

    The on-write trigger fires from ``adr_add`` post-commit and is keyed
    on ``(project_id, subsystem)``. There is no "is stale" predicate — the
    trigger fires unconditionally when ``subsystem`` is set. A future
    migration that wants a smarter "is stale" check (e.g. compare row
    count to rollup metadata) can replace this body without changing
    callers.

    Car K's nightly sweep (when/if it ships) is the policy-dispatched
    backstop. Today the policy dispatch is trivial — on-write covers it.
    """
    return bool(subsystem) and bool(project_id)


# ── Admin-op entry point (the one _ADMIN_OPS registers) ──────────────────────


@observe(tier="boundary", metric="backend.admin.rollup.run_rollup_regen")
async def run_rollup_regen(payload: dict[str, Any]) -> dict[str, Any]:
    """Admin op: regenerate ALL per-subsystem rollups for a project.

    Iterates the distinct ``subsystem`` values from the project's
    ``adr`` ledger rows and regenerates each rollup page. Used as a
    one-shot catch-up tool after a bulk ``adr`` write (e.g. the Car H
    seed op backfilling ``tier``/``subsystem`` on existing rows) or by a
    future Car K nightly sweep.

    payload: ``{project_id: str}`` — required; today the op is
    per-project. A cross-project sweep would need a new
    ``list_adr_rows_all_projects`` storage helper (out of H scope).
    """
    from yadgar._shared.runtime.lifecycle import _get_sql_storage  # noqa: PLC0415

    storage = _get_sql_storage()
    if storage is None:
        return {
            "ok": False,
            "error": "engine #2 not composed (MariaStorageEngine is None)",
        }

    project_id = payload.get("project_id")
    if not project_id:
        return {
            "ok": False,
            "error": "project_id is required (cross-project rollup regen is "
            "out of Car H scope — see run_rollup_regen docstring)",
        }

    try:
        rows_obj = storage.list_adr_rows(
            project_id=str(project_id),
            status=None,
            tier=None,
            subsystem=None,
        )
        if hasattr(rows_obj, "__await__"):
            project_rows = await rows_obj
        else:
            project_rows = rows_obj
    except Exception as exc:  # noqa: BLE001
        logger.warning("run_rollup_regen: list_adr_rows failed: %s", exc)
        return {"ok": False, "error": str(exc)}

    if not isinstance(project_rows, list):
        project_rows = []

    # Distinct subsystems in this project (post §10 Q2 normalizer on read).
    subsystems: set[str] = set()
    for r in project_rows:
        if not isinstance(r, dict):
            continue
        sub = str(r.get("subsystem") or "").strip().lower()
        if sub:
            subsystems.add(sub)

    results: list[dict[str, Any]] = []
    for sub in sorted(subsystems):
        result = await _regenerate_subsystem_rollup(
            project_id=str(project_id),
            subsystem=sub,
            storage=storage,
        )
        results.append({"project_id": str(project_id), "subsystem": sub, **result})

    regenerated = sum(1 for r in results if r.get("ok"))
    return_obj: dict[str, Any] = {
        "ok": True,
        "project_id": str(project_id),
        "regenerated": regenerated,
        "skipped": len(results) - regenerated,
        "results": results,
    }
    return return_obj


__all__ = [
    "_regenerate_subsystem_rollup",
    "_render_rollup_body",
    "_rollup_slug",
    "_should_regenerate_rollup",
    "run_rollup_regen",
]
