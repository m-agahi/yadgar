"""Backend execution bodies for the project-scoped write admin ops (R3 Car 3d / R5).

The core ``@_tool`` shells keep the host-side halves — secret-gate + the
``~/.local/state`` marker for ``update_active_work`` — and forward the DB write
here.

Ops:
  - ``update_active_work`` — atomic delete-then-insert of the ``_active_work``
    memory + a project_brief structural-epoch bump for the resolved directory.
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
