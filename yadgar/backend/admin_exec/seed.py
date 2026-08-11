"""Backend execution body for the seed_store admin op (T2 Car E1).

Census verdict #9 (layer-boundary train): seed_project's STORE phase runs
backend-side. Core keeps the host-FS half — ``scan_project`` +
``generate_memories`` + the ``_project_init`` draft — and forwards one
``seed_store`` op carrying the generated memory dicts. This impl owns:

- embedding + thermodynamic scoring (the backend has the ML engines),
- ``insert_memory`` / ``update_memory_scores``,
- old ``_seed`` row deletion (insert-first, §6 Q17 crash-safety order),
- the ``_project_init`` upsert.

Also owns the task-list page seed (0047 spine train Car E, plan §3.3):
``seed_task_from_pages`` reads the ``{project}-task-list`` wiki pages, parses
their ``## task:<id>`` sections, and inserts rows into the ``task`` ledger
table. Idempotent; ships with an exact-equality verification gate (D35c).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.lifecycle import _get_embeddings, _get_storage, _get_thermo

logger = logging.getLogger(__name__)


@observe(tier="stage", metric="backend.admin.seed_store_one")
def _store_one(storage, embeddings, thermo, mem: dict, project_id: str | None = None) -> int:
    """Embed + score + insert one seed memory. Returns the new memory id.

    C4 (0047 PR#40 §5): ``project_id`` arrives from the host-side caller
    (``core/seed/_generate.seed_project``) and is stamped verbatim. Nothing
    is derived here — this runs in the backend container, where a derivation
    can only manufacture ``local/<basename>`` (ADR-0227 §1.1).
    """
    content = mem["content"]
    context = mem["context"]
    tags = mem["tags"]
    base_heat = float(mem.get("base_heat", 0.6))

    embedding = embeddings.encode(content)

    surprise = thermo.compute_surprise(content, context)
    importance = thermo.compute_importance(content, tags)
    valence = thermo.compute_valence(content)
    # Use modest surprise boost so seeded memories don't all max out.
    initial_heat = min(base_heat + surprise * 0.1, 1.0)

    memory_id = storage.insert_memory(
        {
            "content": content,
            "embedding": embedding,
            "tags": tags,
            "directory_context": context,
            "project_id": project_id,
            "heat": initial_heat,
            "is_stale": False,
            "file_hash": None,
            "embedding_model": embeddings.get_model_name(),
        }
    )
    storage.update_memory_scores(
        memory_id,
        surprise_score=surprise,
        importance=importance,
        emotional_valence=valence,
    )
    return memory_id


@observe(tier="stage", metric="backend.admin.seed_delete_existing")
def _delete_existing_seed_memories(
    storage, directory: str, exclude_ids: list[int] | None = None
) -> int:
    """Delete existing _seed tagged memories for this directory before re-seeding.

    §6 Q17: exclude_ids lets callers preserve newly-inserted memories so the
    delete step only removes OLD seed memories, not the fresh ones.

    Returns count of deleted memories.
    """
    rows = storage._q(
        "SELECT id FROM memory WHERE directory_context = $dir AND '_seed' IN tags",
        {"dir": directory},
    )
    if not rows:
        return 0

    exclude_set: set[int] = set(exclude_ids or [])
    ids = [
        storage._extract_id(r.get("id"))
        for r in rows
        if storage._extract_id(r.get("id")) not in exclude_set
    ]
    for mid in ids:
        # Delete SR transitions referencing this memory
        storage._q(
            "DELETE memory_transition WHERE from_memory_id = $id OR to_memory_id = $id",
            {"id": mid},
        )
        # Delete the memory itself (embedding fields are on the record — no separate table)
        storage._q("DELETE type::record('memory', $id)", {"id": mid})

    return len(ids)


@observe(tier="boundary", metric="backend.admin.seed_store")
def seed_store(payload: dict) -> dict:
    """Store one generated seed batch. Storage-write half of seed_project.

    payload: {
        "root": str,                    # resolved project root (scan_data["root"])
        "memories": [{"content", "context", "tags", "base_heat"}, ...],
        "init_content": str,            # drafted _project_init markdown ("" = skip)
        "project_id": str | None,       # C4: host-resolved identity, stamped as-is
    }
    Returns {"created": int, "replaced": int}.
    """
    root = payload["root"]
    memories = payload.get("memories") or []
    init_content = payload.get("init_content") or ""
    project_id = payload.get("project_id") or None

    storage = _get_storage()
    embeddings = _get_embeddings()
    thermo = _get_thermo()

    # §6 Q17: build new memories FIRST; delete old ones only after successful
    # insert — a crash mid-insert must not leave the DB with no seed memories.
    new_memory_ids: list[int] = []
    for mem in memories:
        new_memory_ids.append(_store_one(storage, embeddings, thermo, mem, project_id))
        logger.info("Seed memory [created]: %s", mem["content"][:80])

    replaced = _delete_existing_seed_memories(storage, root, exclude_ids=new_memory_ids)
    if replaced:
        logger.info("Cleared %d old seed memories for %s", replaced, root)

    if init_content:
        # §23: starter _project_init from README + top-level docs (drafted core-side).
        # C5b: the fifth chokepoint bypass. This function already HELD the
        # caller's project_id (it threads it into every ``_store_one`` above)
        # and dropped it here, so the one row the seed wrote through the raw
        # upsert path was the one row that arrived unattributed. The existing
        # guard is unchanged and now also catches the chokepoint raise: an
        # ownerless init draft degrades to a warning rather than failing the
        # seed, which is what this except clause was written to promise.
        try:
            storage.upsert_project_init(root, init_content, project_id=project_id or "")
            logger.info("Drafted _project_init for %s", root)
        except Exception:  # noqa: BLE001 — init draft failure must not fail the seed
            logger.warning("Failed to draft _project_init for %s", root, exc_info=True)

    return {"created": len(new_memory_ids), "replaced": replaced}


# ---------------------------------------------------------------------------
# 0047 spine train Car E — task seed from existing wiki task-list pages
# ---------------------------------------------------------------------------

# D10: Crockford base32 (digits + a-z minus i,l,o,u). D11: optional origin/.
_TASK_SEED_RE = re.compile(r"^## task:(?:([\w-]+/)?([0-9a-hj-np-tv-z]+))", re.MULTILINE)
_TASK_FIELD_RE = re.compile(r"^- ([a-zA-Z_]+):\s*(.*)", re.MULTILINE)


@observe(
    exempt=(
        "pure formatter; runs once per parsed ## task:<id> section in the seed "
        "loop. Observability would add a per-row span sample with zero "
        "diagnostic value (no I/O, no storage side effects — just regex "
        "matching and dict assembly)."
    )
)
def _parse_task_section(body: str) -> dict[str, str]:
    """Parse the "- key: value" flat bullets of a single ## task:<id> section.

    Multi-line values (description, context) are flattened to single lines
    here — the section body in the wiki page is already a flat bullet list
    per the stop_checkpoint_prompt.md schema. Continuation-indented lines are
    joined with a single space so the seed doesn't lose content.

    Exempt from I33 observe-coverage: pure formatter, no I/O, called in a
    tight inner loop inside the per-section pass and observability would
    add per-row span overhead with zero diagnostic value.
    """
    fields: dict[str, str] = {}
    for line in body.splitlines():
        m = _TASK_FIELD_RE.match(line)
        if m:
            key = m.group(1).strip()
            value = m.group(2).strip()
            fields[key] = value
    return fields


def _list_task_list_pages(storage: Any, directory: str) -> list[dict[str, Any]]:
    """Return all ``page_type='task_list'`` wiki pages for the given directory.

    The schema is unindexed on ``page_type`` (only ``slug`` is indexed), so we
    scan the wiki_page table directly via storage._q. The query is bounded by
    ``directory_context`` so a global poll is avoided.
    """
    safe_dir = directory.replace("'", "''")
    rows = storage._q(
        "SELECT slug, content, directory_context FROM wiki_page "
        "WHERE page_type = 'task_list' "
        "AND (directory_context = $dir OR directory_context = 'global')",
        {"dir": safe_dir},
    )
    return storage._rows_to_dicts(rows) if hasattr(storage, "_rows_to_dicts") else list(rows)


@observe(tier="boundary", metric="backend.admin.seed_task_from_pages")
def seed_task_from_pages(*, directory: str, project_id: str, dry_run: bool = False) -> dict:
    """Seed the ``task`` table from existing ``{project}-task-list`` wiki pages.

    Car E (0047 spine train, plan §3.3). Reads page_type='task_list' pages,
    parses ``## task:<id>`` sections, and inserts rows into the task table.
    Idempotent (D35a). Verification gate: exact equality of per-page section
    count vs seeded rows per project (D35c).

    Args:
        directory: project root directory used to scope the wiki-page query.
        project_id: the project key that the ``task`` table clusters on.
        dry_run: when True, parse + count but do not write rows.

    Returns:
        dict with keys:
          - seeded: number of rows inserted (0 on dry_run)
          - skipped: number of rows skipped (idempotent re-run)
          - dry_run: True/False
          - candidates: number of parsed task sections per page
          - pages: per-page breakdown {slug: {candidates, seeded, skipped}}
    """
    storage = _get_storage()
    if storage is None:
        raise RuntimeError("storage not initialised; cannot seed tasks")

    _pages = _list_task_list_pages(storage, directory)
    result: dict[str, Any] = {
        "seeded": 0,
        "skipped": 0,
        "dry_run": dry_run,
        "candidates": 0,
        "pages": {},
    }

    for _page in _pages:
        _slug = _page.get("slug", "")
        _content = _page.get("content", "") or ""
        _sections = _TASK_SEED_RE.split(_content)
        # _TASK_SEED_RE.split yields: [pre, origin1, id1, body1, origin2, id2, body2, ...]
        _page_seed = {"candidates": 0, "seeded": 0, "skipped": 0}
        _i = 1
        while _i + 2 < len(_sections):
            _origin = _sections[_i].strip()
            _task_id = _sections[_i + 1].strip()
            _body = _sections[_i + 2]
            _fields = _parse_task_section(_body)
            _page_seed["candidates"] += 1
            if dry_run:
                _i += 3
                continue

            # Car D ships create_task_row. If it isn't present (without-Car-D),
            # the seed is a no-op write-side but the parse is verified.
            _create = getattr(storage, "create_task_row", None)
            if _create is None:
                logger.info(
                    "create_task_row not present on storage; skipping write for task %s on page %s",
                    _task_id,
                    _slug,
                )
                _page_seed["skipped"] += 1
                _i += 3
                continue

            try:
                _create(
                    project_id=project_id,
                    title=_fields.get("subject", "(no subject)"),
                    status=_fields.get("status", "pending"),
                    state=_fields.get("state", "open"),
                    active_form=_fields.get("active_form", ""),
                    plan_path=_fields.get("plan_path", ""),
                    body_slug=_fields.get("body_slug", ""),
                )
                _page_seed["seeded"] += 1
            except Exception as _we:  # noqa: BLE001 — idempotent: skip on duplicate
                logger.debug("seed task %s on page %s skipped: %s", _task_id, _slug, _we)
                _page_seed["skipped"] += 1
            _i += 3

        result["pages"][_slug] = _page_seed
        result["candidates"] += _page_seed["candidates"]
        result["seeded"] += _page_seed["seeded"]
        result["skipped"] += _page_seed["skipped"]

    # D35c: per-page section count must equal seeded rows per project. The
    # gate is recorded in the result so callers (the CLI, integration tests)
    # can fail on `candidates != seeded` per page.
    return result
