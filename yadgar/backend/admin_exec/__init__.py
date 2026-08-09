"""Backend admin-op execution package (R3 Car 3a / R5 forward pattern).

The pure-CRUD MCP write tools (bookmarks, blocks, …) keep their ``@_tool``
shell + validation + secret-gate in core (``yadgar.core.server.tools.*``) and
forward the actual storage write to the backend over HTTP (POST /admin) via the
core ``_forward_admin`` helper. This package holds the backend EXECUTION bodies
— undecorated ``(payload: dict) -> dict`` impls that run the storage write — plus
the dispatch the ``/admin`` route calls.

Goal (R3): core is a thin router; core touches zero DB directly. Every DB write
goes: core validate → HTTP POST /admin → backend dispatch → storage.

TWO DISPATCH ENTRY POINTS (engine-#2 car B):

* ``run_admin_op_async`` — what the ``/admin`` route calls. Accepts BOTH op
  shapes: a sync body is delegated to ``run_admin_op`` inside
  ``asyncio.to_thread``; an ``async def`` body is awaited on the event loop.
* ``run_admin_op`` — the sync entry point, unchanged for every op that exists
  today. Still called directly by the in-process test bypasses.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import TypeIs

from yadgar._shared.observability.observe import observe
from yadgar.backend.admin_exec import (
    audit,
    backup_sql,
    blocks,
    bookmarks,
    drain,
    engine_status,
    invariants,
    ledger,
    memory,
    project,
    reslug,
    restoration,
    restore_sql,
    runtime_config,
    seed,
    staleness,
    wiki,
)

# An admin op body is EITHER a plain sync ``(payload) -> dict`` (every op today)
# OR a coroutine function ``async (payload) -> dict``. Engine-#2 car B widened the
# table to admit the second shape so an async-only driver (asyncmy) can be awaited
# on the event loop instead of from inside a worker thread, where awaiting would
# need a per-call private loop. NOTHING converts existing ops — see
# ``run_admin_op_async`` for how the two shapes are kept apart.
AdminOp = Callable[[dict], dict] | Callable[[dict], Awaitable[dict]]

# Dispatch table: op name (matches the core tool name) → backend impl.
# Keep this the single source of truth for the /admin surface; the /admin route
# validates ``op`` against these keys.
_ADMIN_OPS: dict[str, AdminOp] = {
    # bookmarks
    "bookmark_add": bookmarks.bookmark_add,
    "bookmark_remove": bookmarks.bookmark_remove,
    "bookmark_reorder": bookmarks.bookmark_reorder,
    # blocks
    "block_create": blocks.block_create,
    "block_update": blocks.block_update,
    "block_delete": blocks.block_delete,
    "block_replace": blocks.block_replace,
    "block_append": blocks.block_append,
    # runtime config store (ADR-0163, G1) — write ops only; reads stay core.
    "runtime_config_set": runtime_config.runtime_config_set,
    "runtime_config_delete": runtime_config.runtime_config_delete,
    # memory / rules writes (R3 Car 3b / R5 group 2)
    "forget": memory.forget,
    "memory_update": memory.memory_update,
    "anchor_renew": memory.anchor_renew,
    "reembed_all": memory.reembed_all,
    "add_rule": memory.add_rule,
    "archive_purge": memory.archive_purge,
    # staleness-flag + sentinel-vacuum writes (T2 Car E1)
    "update_memory_staleness": memory.update_memory_staleness,
    "vacuum_stale_sentinels": memory.vacuum_stale_sentinels,
    # staleness heat-decay compute (T2 Car E1 — census verdict #8)
    "staleness_file_changed": staleness.staleness_file_changed,
    "staleness_scan": staleness.staleness_scan,
    "staleness_flag_memory": staleness.staleness_flag_memory,
    # seed store phase (T2 Car E1 — census verdict #9)
    "seed_store": seed.seed_store,
    # wiki-edit + agent_prompt writes (R3 Car 3c / R5 group 3)
    "wiki_delete": wiki.wiki_delete,
    "wiki_autolink": wiki.wiki_autolink,
    "wiki_update": wiki.wiki_update,
    "wiki_restore": wiki.wiki_restore,
    "wiki_append_section": wiki.wiki_append_section,
    "wiki_set_metadata": wiki.wiki_set_metadata,
    "wiki_replace_text": wiki.wiki_replace_text,
    "wiki_delete_text": wiki.wiki_delete_text,
    "wiki_insert_after": wiki.wiki_insert_after,
    "wiki_insert_before": wiki.wiki_insert_before,
    "wiki_replace_at": wiki.wiki_replace_at,
    "wiki_delete_at": wiki.wiki_delete_at,
    "wiki_insert_at": wiki.wiki_insert_at,
    "wiki_replace_markdown_block": wiki.wiki_replace_markdown_block,
    "agent_prompt_save": wiki.agent_prompt_save,
    # Car I: ``increment_prompt_usage`` (memory-row path) is gone — uses is a
    # SQL integer on ``agent_pattern`` (D40). The new op is registered below
    # under ``ledger``.
    # anchor-audit + invariants + project writes (R3 Car 3d / R5 final group)
    "audit_apply_mutations": audit.audit_apply_mutations,
    "write_audit_sentinel": audit.write_audit_sentinel,
    "check_invariants": invariants.check_invariants,
    "update_active_work": project.update_active_work,
    "bootstrap_project_store": project.bootstrap_project_store,
    "record_prelude_marker": project.record_prelude_marker,
    # Car 0: trusted per-directory git-context durable store (upsert + read).
    # restoration writes (T2 Car B — pre-compact drain is write-only, no compute)
    "pre_compact_drain": restoration.pre_compact_drain,
    # cross-process drain nudge (task #29 — wiki_add/memorize wait cold-drain fix):
    # runs the LIVE backend drainer's drain_now() synchronously so the core
    # wait-path can flush promptly over HTTP instead of waiting a full interval.
    "drain_now": drain.drain_now,
    # engine #2 presence probe: the ONE question a host-side caller cannot answer
    # for itself, since a host-built MariaStorageEngine is connectionless and the
    # socket path in client.cnf is container-absolute. A pure slot read, never a
    # liveness probe — see admin_exec/engine_status.py for why that distinction
    # is what keeps an unreachable engine from being mistaken for an absent one.
    "sql_engine_status": engine_status.sql_engine_status,
    # engine #2 backup arm (car F): the logical dump runs HERE because only this
    # process's namespace makes client.cnf's container-absolute socket path true
    # and only this image carries mariadb-dump. See admin_exec/backup_sql.py.
    "mariadb_dump": backup_sql.mariadb_dump,
    # engine #2 restore arm (car G): THE restore path. There is no other way to
    # replay a dump into engine #2, and the enumeration gate runs inside this op
    # before a restore can be called good — see admin_exec/restore_sql.py.
    "mariadb_restore_verify": restore_sql.mariadb_restore_verify,
    # Car B: ledger READ ops (task / adr / agent_prompt) over MariaStorageEngine
    # methods. Async because asyncmy is async-only. Closes the in-process
    # _get_storage() read path core used to take for ledger tables.
    "list_task_rows": ledger.list_task_rows,
    "get_task_row": ledger.get_task_row,
    "list_task_rows_all_projects": ledger.list_task_rows_all_projects,
    # Car D: ledger WRITE ops (task) — create / update + task_blocked_by join-edge
    # reconcile (D39). Mirror the READ ops' async shape; the core ``task_write``
    # tool shells forward here over HTTP per §15 / ADR-0078.
    "create_task_row": ledger.create_task_row,
    "update_task_row": ledger.update_task_row,
    "list_adr_rows": ledger.list_adr_rows,
    "get_adr_row": ledger.get_adr_row,
    "list_agent_prompt_rows": ledger.list_agent_prompt_rows,
    # Car I additions: uses-DESC list, single-row lookup, composes reads,
    # ledger-row upserts for ``agent_prompt_save`` / ``discipline_save``,
    # and ``uses`` increment over the table (D40).
    "list_agent_pattern_rows_uses_desc": ledger.list_agent_pattern_rows_uses_desc,
    "get_agent_pattern_row": ledger.get_agent_pattern_row,
    "list_pattern_composes": ledger.list_pattern_composes,
    "save_agent_pattern_row": ledger.save_agent_pattern_row,
    "save_agent_discipline_row": ledger.save_agent_discipline_row,
    "increment_agent_pattern_uses": ledger.increment_agent_pattern_uses,
    "get_agent_prompt_toc_updated_at": ledger.get_agent_prompt_toc_updated_at,
    # Car B: runtime_config READ ops (SurrealDB sync path). Closes the in-process
    # _get_storage() read violation in core/server/tools/_runtime_config.py.
    "get_config_row": runtime_config.get_config_row,
    "list_config_rows": runtime_config.list_config_rows,
    # Car L (0047 §7 D32 ③): ADR wiki page re-slug — moves pages from
    # ``yadgar-adr-NNNN`` to ``{project_id}_adr-NNNN`` + updates crossrefs
    # + inline body links + adr.body_slug. Idempotent; dry-run by default.
    "reslug": reslug.reslug_adr_pages,
}


def admin_ops() -> frozenset[str]:
    """Return the set of registered admin op names (I32 capability discovery)."""
    return frozenset(_ADMIN_OPS)


@observe(
    exempt=(
        "trivial lazy-import shim shared by the sync and async dispatchers; the "
        "restoration compose it delegates to carries its own instrumentation"
    )
)
def _ensure_engines() -> None:
    """Compose the backend restoration engines (idempotent once built).

    T2 Car B: ops that anchor (agent_prompt_save) or drain read ``_st._replay``,
    which the shared root no longer builds. Also covers the test bypass path that
    skips the /admin route's ``_ensure_recall_engines``.

    BLOCKING — every caller must keep it off the event loop.
    """
    from yadgar.backend.restoration import ensure_restoration_engines  # noqa: PLC0415

    ensure_restoration_engines()


def _is_async_op(impl: AdminOp) -> TypeIs[Callable[[dict], Awaitable[dict]]]:
    """Return True when *impl* must be awaited rather than called in a thread.

    ``inspect.iscoroutinefunction`` sees through the decorator stack real ops
    carry: ``@observe`` branches on the ORIGINAL function and returns a genuine
    ``async def`` wrapper for a coroutine function (observe.py ``_build_wrapper``),
    and ``functools.wraps`` copies metadata without touching ``__code__``. So a
    decorated async op still answers True. Pinned by test_admin_async_dispatch.

    DELIBERATELY UNDECORATED: ``observe()`` is annotated ``-> Callable``, so any
    decorated function loses its signature — including this one's ``TypeIs``, and
    with it the narrowing both dispatchers rely on to stay type-clean. A one-line
    predicate over an in-memory object has nothing to observe anyway.
    """
    return inspect.iscoroutinefunction(impl)


@observe(tier="boundary", metric="backend.admin.run_admin_op")
def run_admin_op(op: str, payload: dict) -> dict:
    """Dispatch a single SYNC admin op to its backend execution body.

    Unchanged by engine-#2 car B for every op that exists today. An ASYNC op is a
    programming error here — it must go through ``run_admin_op_async``, which is
    what the /admin route calls.

    Args:
        op: Op name — must be a key of ``_ADMIN_OPS`` (mirrors the core tool name).
        payload: The op's arguments (already validated + gated core-side).

    Returns:
        The impl's result dict.

    Raises:
        KeyError: if ``op`` is not a registered admin op (route maps to 400).
        TypeError: if ``op`` is registered as a coroutine function.
    """
    impl = _ADMIN_OPS.get(op)
    if impl is None:
        raise KeyError(f"unknown admin op: {op!r}")
    if _is_async_op(impl):
        raise TypeError(f"admin op {op!r} is async — dispatch it via run_admin_op_async")
    _ensure_engines()
    return impl(payload)


@observe(tier="boundary", metric="backend.admin.run_admin_op_blocking")
def run_admin_op_blocking(op: str, payload: dict) -> dict:
    """Dispatch ANY op shape from a SYNC caller that has no running event loop.

    The in-process ``_forward_admin`` bypass the test harnesses install
    (``tests/conftest.py``, ``tests/_backend_harness.py``) is a plain sync
    function, because the core tools it stands in for are. Once engine-#2 car H
    made ``check_invariants`` a coroutine, that bypass would hit ``run_admin_op``'s
    deliberate ``TypeError`` — an error about a PRODUCTION misdispatch, raised at
    a test seam where the op is perfectly legal.

    ``run_admin_op`` itself is left alone on purpose: its TypeError is pinned by
    ``test_admin_async_dispatch`` and is the guard that keeps a coroutine body
    from being handed to a sync caller in the daemon, where a private event loop
    would bind a connection pool to a loop that is about to die.

    NOT for the daemon. The ``/admin`` route is already on a loop and must keep
    calling ``run_admin_op_async``; ``asyncio.run`` from inside a running loop
    raises, which is the correct outcome if this is ever misused there.
    """
    impl = _ADMIN_OPS.get(op)
    if impl is not None and _is_async_op(impl):
        return asyncio.run(run_admin_op_async(op, payload))
    return run_admin_op(op, payload)


@observe(
    exempt=(
        "instrumenting here would add a SECOND boundary metric sample to every "
        "existing sync op: observe's double-instrumentation guard suppresses a "
        "duplicate span, not a duplicate metric. The sync path is already measured "
        "by run_admin_op below; async op bodies carry their own @observe."
    )
)
async def run_admin_op_async(op: str, payload: dict) -> dict:
    """Await-capable dispatch entry point — the one the /admin route calls.

    Engine-#2 car B. Two shapes, one entry point:

    * SYNC op (every op today) → delegated verbatim to ``run_admin_op`` inside
      ``asyncio.to_thread``. Same function, same ``@observe`` boundary sample,
      same worker-thread property, same errors — including the ``KeyError`` for
      an unknown op, which is deliberately left to raise from inside the thread
      so its observability shape does not drift from the sync path.
    * ASYNC op → awaited directly on the event loop. This is why the car exists:
      ``asyncmy`` is async-only and cannot be awaited from a worker thread
      without a per-call private event loop.

    Carries ``@observe(exempt=...)`` — the categorized no-op, NOT a real span or
    metric source. observe's double-instrumentation guard suppresses a second
    SPAN, not a second METRIC, so real instrumentation here would add a spurious
    boundary sample to every existing sync op on top of the route's and
    ``run_admin_op``'s. Async op bodies carry their own ``@observe``, as sync
    bodies do.

    Raises:
        KeyError: if ``op`` is not a registered admin op (route maps to 400).
    """
    impl = _ADMIN_OPS.get(op)
    if impl is not None and _is_async_op(impl):
        # _ensure_engines is blocking — keep it off the loop, exactly as the sync
        # path does by virtue of running wholly inside to_thread.
        await asyncio.to_thread(_ensure_engines)
        return await impl(payload)
    return await asyncio.to_thread(run_admin_op, op, payload)
