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
    adr_seed,
    audit,
    backup_sql,
    blocks,
    bookmarks,
    drain,
    engine_status,
    identity_stamp,
    invariants,
    ledger,
    memory,
    nightly_sweep,
    project,
    project_backfill,
    reslug,
    restoration,
    restore_sql,
    rollup,
    runtime_config,
    seed,
    seed_adr_tier_subsystem,
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
    # task 193: the Car J mutability escape hatch. Implemented, exported and
    # documented since Car J, but never registered here — so every call raised
    # KeyError -> HTTP 400 and the ADR corpus (page_type='adr' => 'locked') was
    # uncreatable, uneditable and undeletable through every unsanctioned path.
    "wiki_set_mutability": wiki.wiki_set_mutability,
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
    # C6: the ``project`` registry seed + read. The registry is load-bearing
    # (ADR-0202/0223) and ships with zero rows, so seeding it is the FIRST
    # operator step on a new deployment — every task/adr row FKs to it. These
    # two ops are deliberately NOT registry-guarded: they are the bootstrap.
    "create_project_row": ledger.create_project_row,
    "list_project_rows": ledger.list_project_rows,
    # Car C11-#88 (task #88): staleness surface for ``yadgar project list
    # --stale``. The threshold comes from Settings; the op echoes it so the
    # CLI can render "stale since N days" without a second admin call.
    "list_stale_projects": ledger.list_stale_projects,
    # C6: the operator-invoked project_id backfill (T2). Dry-run by default —
    # it returns a manifest and writes nothing until the operator re-runs with
    # dry_run=False AND acknowledges the unmapped bucket and the deletes.
    "project_id_backfill": project_backfill.project_id_backfill,
    # Car D: host-side migration's dry-run seed — returns DISTINCT
    # ``directory_context`` counts so the core CLI can build the map
    # without importing a storage handle (layer-boundary contract).
    "rekey_discover_directories": project_backfill.rekey_discover_directories,
    # Car 1 (ledger tasks 309 + 89): the graph-table half of the same job. C6
    # covered ``memory`` + ``wiki_page``; the six tables below it were never
    # named, leaving 10,997 rows with no identity that a later reader flip onto
    # ``project_id`` would make invisible. Dry-run by default, and the dry run
    # runs the write path's registry guard over every derived target.
    "stamp_project_id": identity_stamp.stamp_project_id,
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
    "list_agent_discipline_rows": ledger.list_agent_discipline_rows,
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
    # Car F: ADR WRITE ops over MariaStorageEngine — create_adr_row is the new
    # ID source of truth (ADR-0197: AUTO_INCREMENT id IS the ADR number),
    # set_adr_body_slug links the row to the wiki body page (D4 — body stays
    # in SurrealDB, only the slug pointer moves to MariaDB), and
    # add_adr_supersedes is the D23 supersede link + status flip. Async for
    # the same reason as the read ops above.
    "create_adr_row": ledger.create_adr_row,
    "set_adr_body_slug": ledger.set_adr_body_slug,
    "add_adr_supersedes": ledger.add_adr_supersedes,
    # Car G (0047 §7): the ``_get_adr_log_updated_at`` signal re-points off
    # the deleted ``<project>-adr-index`` wiki page onto the SQL ledger.
    "max_adr_updated_at": ledger.max_adr_updated_at,
    # Car B: runtime_config READ ops (SurrealDB sync path). Closes the in-process
    # _get_storage() read violation in core/server/tools/_runtime_config.py.
    "get_config_row": runtime_config.get_config_row,
    "list_config_rows": runtime_config.list_config_rows,
    # Car L (0047 §7 D32 ③): ADR wiki page re-slug — moves pages from
    # ``yadgar-adr-NNNN`` to ``{project_id}_adr-NNNN`` + updates crossrefs
    # + inline body links + adr.body_slug. Idempotent; dry-run by default.
    "reslug": reslug.reslug_adr_pages,
    # Car G (0047 §7 D23/D35a): ADR seed (pages→ledger) + retype mutator.
    # ``seed_adr_rows`` lifts the ~223 existing ADRs from per-ADR wiki PAGES
    # into the ``adr`` ledger table (D35a — one-shot, idempotent on
    # body_slug). ``retype_page_type`` flips ``wiki_page.page_type``
    # ``adr`` → ``adr_superseded`` atomic with the row-side status flip
    # (D23 — the sole sanctioned writer for the lifecycle transition).
    "seed_adr_rows": adr_seed.seed_adr_rows,
    "retype_page_type": adr_seed.retype_page_type,
    # Car K (0047 §7 row K): nightly archive sweep — cross-engine write that
    # flips MariaDB ledger rows to status='archived' and retypes SurrealDB
    # body pages to per-type archived variants. Per-page mutability_override
    # in ('locked','derived') is the operator opt-out. Idempotent;
    # circuit-breaker caps the candidate count.
    "run_nightly_archive_sweep": nightly_sweep.run_nightly_archive_sweep,
    # Car H (0047 §7 D29): per-subsystem ADR rollup pages (D29). The
    # ``_regenerate_subsystem_rollup`` is the internal write helper invoked
    # from core ``adr_add``'s post-commit step (§10 Q1 on-write trigger);
    # ``run_rollup_regen`` is the admin-op catch-up entry point that
    # iterates a project's distinct subsystems and regenerates each. The
    # one-shot ``seed_adr_tier_subsystem`` (D35a) backfills ``tier`` +
    # ``subsystem`` columns on existing rows.
    "run_rollup_regen": rollup.run_rollup_regen,
    "seed_adr_tier_subsystem": seed_adr_tier_subsystem.seed_adr_tier_subsystem,
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
    from yadgar.backend.restoration import ensure_restoration_engines

    ensure_restoration_engines()


def _resolve_storage():
    """Return the runtime storage engine for ops that take it as a parameter."""
    from yadgar._shared.runtime.lifecycle import _get_storage

    return _get_storage()


@observe(tier="hot", span=False)
def _kwargs_op(fn):
    """Adapt a KEYWORD-ONLY op body to the dispatch's ``impl(payload)`` contract.

    C10 (0047 §5(d)). Both dispatchers call ``impl(payload)`` — one positional
    dict. Four registered bodies are declared keyword-only (``def f(*, a, b)``),
    so that call raised ``TypeError: takes 0 positional arguments but 1 was
    given`` **every time**. They could never have executed through ``/admin``:

        reslug, retype_page_type, seed_adr_rows, seed_task_from_pages

    (measured by binding every entry in ``_ADMIN_OPS`` against ``impl({})``).
    ``retype_page_type`` is D23's "sole sanctioned writer" for the ADR supersede
    lifecycle transition, so that transition has never run through this route.

    The async branch is not cosmetic: ``_is_async_op`` uses
    ``inspect.iscoroutinefunction``, which inspects the wrapper's own code flags.
    A sync wrapper around a coroutine body would report False, and the sync
    dispatcher would return an un-awaited coroutine object as if it were the
    result dict. ``functools.wraps`` is deliberately NOT used — it would copy
    ``__wrapped__`` and make ``inspect.signature`` report the wrapped
    keyword-only signature, hiding the very mismatch this adapter exists to fix.
    """
    if inspect.iscoroutinefunction(fn):

        @observe(
            exempt=(
                "the WRAPPED op body already carries its own @observe boundary "
                "sample, and run_admin_op/run_admin_op_async instrument the "
                "dispatch around it. Instrumenting this adapter would add a "
                "THIRD sample per call for exactly four ops — observe's "
                "double-instrumentation guard suppresses a duplicate span, not "
                "a duplicate metric."
            )
        )
        async def _acall(payload: dict):
            return await fn(**payload)

        _acall.__name__ = f"{getattr(fn, '__name__', 'op')}__kwargs_adapter"
        return _acall

    def _call(payload: dict):
        return fn(**payload)

    _call.__name__ = f"{getattr(fn, '__name__', 'op')}__kwargs_adapter"
    return _call


@observe(tier="hot", span=False)
def _payload_storage_op(fn):
    """Adapt ``fn(payload, *, storage)`` to ``impl(payload)``, injecting storage.

    C10: ``reslug_adr_pages`` takes the payload positionally but declares
    ``storage`` keyword-only with **no default**, so ``impl(payload)`` raised
    ``TypeError: missing a required keyword-only argument: 'storage'``. The
    dispatchers call ``_ensure_engines()`` first, so the runtime storage is
    composed by the time this runs.
    """
    if inspect.iscoroutinefunction(fn):

        @observe(
            exempt=(
                "the WRAPPED op body already carries its own @observe boundary "
                "sample, and run_admin_op/run_admin_op_async instrument the "
                "dispatch around it. Instrumenting this adapter would add a "
                "THIRD sample per call for exactly four ops — observe's "
                "double-instrumentation guard suppresses a duplicate span, not "
                "a duplicate metric."
            )
        )
        async def _acall(payload: dict):
            return await fn(payload, storage=_resolve_storage())

        _acall.__name__ = f"{getattr(fn, '__name__', 'op')}__storage_adapter"
        return _acall

    def _call(payload: dict):
        return fn(payload, storage=_resolve_storage())

    _call.__name__ = f"{getattr(fn, '__name__', 'op')}__storage_adapter"
    return _call


# ── C10: re-register the four bodies whose signatures the dispatch cannot call ──
#
# Applied here rather than inline in ``_ADMIN_OPS`` because the adapters are
# defined below the table. Keeping the table a flat name → body map also keeps
# it readable as the registry it is; the adaptation is a dispatch concern.
#
# ``_ADMIN_OPS_KWARG_SHAPED`` is the closed list — ``test_admin_op_dispatch_shapes``
# re-derives it empirically from the live table, so a new keyword-only op added
# later fails that test instead of silently becoming unreachable.
_ADMIN_OPS_KWARG_SHAPED: tuple[str, ...] = (
    "retype_page_type",
    "seed_adr_rows",
)
_ADMIN_OPS_PAYLOAD_STORAGE_SHAPED: tuple[str, ...] = ("reslug",)

for _op_name in _ADMIN_OPS_KWARG_SHAPED:
    _ADMIN_OPS[_op_name] = _kwargs_op(_ADMIN_OPS[_op_name])
for _op_name in _ADMIN_OPS_PAYLOAD_STORAGE_SHAPED:
    _ADMIN_OPS[_op_name] = _payload_storage_op(_ADMIN_OPS[_op_name])
del _op_name


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
