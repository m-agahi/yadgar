"""Backend execution bodies for the project-scoped write admin ops (R3 Car 3d / R5).

The core ``@_tool`` shells keep the host-side halves — secret-gate + branch
resolution + the ``~/.local/state`` marker for ``update_active_work``; the
``git branch -a`` enumeration for ``wiki_cleanup_merged_branches`` (the backend
container cannot reach the host ``.git``) — and forward the DB write here.

Ops:
  - ``update_active_work`` — atomic delete-then-insert of the ``_active_work``
    memory + a project_brief structural-epoch bump for the resolved directory.
  - ``wiki_cleanup_merged_branches`` — given the core-enumerated live-branch set,
    query wiki_page rows on stale branches and delete them (wiki-epoch bump runs
    inside ``delete_wiki_page``; the slug-fallback path bumps explicitly).
"""

from __future__ import annotations

import logging

from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.lifecycle import _get_storage

logger = logging.getLogger(__name__)


@observe(tier="boundary", metric="backend.admin.bootstrap_project_store")
def bootstrap_project_store(payload: dict) -> dict:
    """Store phase of bootstrap_project (T2 Car F sweep — 6th raw-write path).

    payload: {"resolved": str, "content": str}
    ``resolved`` is the git-root-resolved directory (host-side); ``content``
    is already secret-gated + cap-validated core-side. Upserts the
    _project_init memory and seeds the default memory blocks (current_task +
    gotchas) idempotently. Returns the new _project_init memory dict.
    """
    import yadgar._shared.runtime.state as _st  # noqa: PLC0415

    resolved = payload["resolved"]
    content = payload["content"]
    storage = _st._storage
    result = storage.upsert_project_init(resolved, content)

    # v5.33.0: seed default memory blocks (idempotent — skip existing).
    for name, block_content in (("current_task", ""), ("gotchas", "")):
        try:
            existing = storage.get_block(name, scope="project", directory=resolved)
            if existing is None:
                storage.create_block(
                    name=name,
                    content=block_content,
                    scope="project",
                    directory=resolved,
                    char_limit=2000,
                )
        except Exception:  # noqa: BLE001 — block seeding must never break the upsert
            logger.debug("bootstrap_project_store: block seed failed %r for %r", name, resolved)

    return result


@observe(tier="boundary", metric="backend.admin.update_active_work")
def update_active_work(payload: dict) -> dict:
    """Replace a directory's _active_work memory (atomic). Storage-write half.

    payload: {"resolved": str, "content": str}
    ``resolved`` is the git-root-resolved directory (core resolved it host-side;
    the backend container cannot run git). Returns {previous_content, new_memory}.

    A new _active_work row is a STRUCTURAL write for that directory — bump its
    project_brief epoch so any cached brief busts. Cross-process via the shared
    queue volume (Car 2). Guarded: never breaks the write.
    """
    resolved = payload["resolved"]
    content = payload["content"]
    storage = _get_storage()
    result = storage.upsert_active_work(resolved, content)
    try:
        from yadgar._shared.server_helpers import _bump_epoch_for_context  # noqa: PLC0415

        _bump_epoch_for_context(resolved)
    except Exception:  # noqa: BLE001 - instrumentation must never break the write
        pass
    return result


@observe(tier="boundary", metric="backend.admin.record_prelude_marker")
def record_prelude_marker(payload: dict) -> dict:
    """Upsert the _dispatch_prelude marker for a directory. Storage-write half.

    payload: {"directory": str}
    Best-effort nudge write (agent_dispatch_prelude read-side signal, #69). The
    core shell already guards the forward with a swallow, so a failure here is
    non-fatal. Returns {"recorded": bool}.
    """
    directory = payload.get("directory")
    if not directory:
        return {"recorded": False}
    storage = _get_storage()
    try:
        storage.upsert_dispatch_prelude_marker(directory)
        return {"recorded": True}
    except Exception:
        logger.debug("record_prelude_marker: upsert failed for %s", directory, exc_info=True)
        return {"recorded": False}


@observe(tier="stage", metric="backend.admin._wiki_cleanup_candidates")
def _wiki_cleanup_candidates(storage, live_branches: set[str]) -> list[dict]:
    """Build the stale-branch wiki_page candidate list. Read-only."""
    try:
        rows = storage._q(
            "SELECT id, slug, branch FROM wiki_page "
            "WHERE branch IS NOT NONE "
            "AND branch != 'master' AND branch != 'main'"
        )
    except Exception as _e:
        logger.warning("wiki_cleanup_merged_branches: DB query failed: %s", _e)
        rows = []

    candidates: list[dict] = []
    for row in rows:
        row_branch = row.get("branch") or ""
        if row_branch in live_branches:
            continue
        try:
            int_id = storage._extract_id(row.get("id"))
        except (ValueError, TypeError):  # fmt: skip
            int_id = None
        candidates.append(
            {
                "id": int_id,
                "_raw_id": row.get("id"),
                "slug": row.get("slug", ""),
                "branch": row_branch,
            }
        )
    return candidates


@observe(tier="stage", metric="backend.admin._wiki_cleanup_delete_one")
def _wiki_cleanup_delete_one(storage, candidate: dict) -> bool:
    """Delete a single candidate wiki page. Returns True on delete.

    delete_wiki_page bumps the wiki epoch internally; the slug-fallback raw DELETE
    does not, so it bumps explicitly to keep cache invalidation coherent.
    """
    slug = candidate.get("slug", "")
    try:
        if candidate["id"] is not None:
            storage.delete_wiki_page(candidate["id"])
            return True
        if slug:
            storage._q("DELETE wiki_page WHERE slug = $slug", {"slug": slug})
            try:
                storage._bump_wiki_epoch()
            except Exception:  # noqa: BLE001
                pass
            return True
    except Exception as _e:
        logger.warning("wiki_cleanup_merged_branches: delete of page failed: %s", _e)
    return False


@observe(tier="boundary", metric="backend.admin.wiki_cleanup_merged_branches")
def wiki_cleanup_merged_branches(payload: dict) -> dict:
    """Delete wiki_page rows on branches no longer live. Storage-write half.

    payload: {"live_branches": [str, ...], "dry_run": bool}
    The core shell ran ``git branch -a`` host-side and passed the live-branch set.
    Here we query wiki_page rows with a non-canonical branch, keep those whose
    branch is not live as candidates, and (unless dry_run) delete them.

    Returns {"candidates": [{id, slug, branch}], "deleted_count": int, "dry_run": bool}.
    """
    live_branches = set(payload.get("live_branches") or [])
    dry_run = bool(payload.get("dry_run", True))
    storage = _get_storage()

    candidates = _wiki_cleanup_candidates(storage, live_branches)

    deleted_count = 0
    if not dry_run:
        deleted_count = sum(_wiki_cleanup_delete_one(storage, c) for c in candidates)

    return_candidates = [{k: v for k, v in c.items() if not k.startswith("_")} for c in candidates]
    return {
        "candidates": return_candidates,
        "deleted_count": deleted_count,
        "dry_run": dry_run,
    }
