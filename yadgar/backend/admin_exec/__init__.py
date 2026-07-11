"""Backend admin-op execution package (R3 Car 3a / R5 forward pattern).

The pure-CRUD MCP write tools (bookmarks, blocks, …) keep their ``@_tool``
shell + validation + secret-gate in core (``yadgar.core.server.tools.*``) and
forward the actual storage write to the backend over HTTP (POST /admin) via the
core ``_forward_admin`` helper. This package holds the backend EXECUTION bodies
— undecorated ``(payload: dict) -> dict`` impls that run the storage write — plus
the ``run_admin_op`` dispatch the ``/admin`` route calls.

Goal (R3): core is a thin router; core touches zero DB directly. Every DB write
goes: core validate → HTTP POST /admin → backend ``run_admin_op`` → storage.
"""

from __future__ import annotations

from collections.abc import Callable

from yadgar._shared.observability.observe import observe
from yadgar.backend.admin_exec import (
    audit,
    blocks,
    bookmarks,
    invariants,
    memory,
    project,
    restoration,
    seed,
    staleness,
    wiki,
)

# Dispatch table: op name (matches the core tool name) → backend impl.
# Keep this the single source of truth for the /admin surface; the /admin route
# validates ``op`` against these keys.
_ADMIN_OPS: dict[str, Callable[[dict], dict]] = {
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
    # memory / rules writes (R3 Car 3b / R5 group 2)
    "forget": memory.forget,
    "memory_update": memory.memory_update,
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
    "wiki_discard": wiki.wiki_discard,
    "wiki_approve": wiki.wiki_approve,
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
    "increment_prompt_usage": wiki.increment_prompt_usage,
    # anchor-audit + invariants + project writes (R3 Car 3d / R5 final group)
    "audit_apply_mutations": audit.audit_apply_mutations,
    "write_audit_sentinel": audit.write_audit_sentinel,
    "check_invariants": invariants.check_invariants,
    "update_active_work": project.update_active_work,
    "bootstrap_project_store": project.bootstrap_project_store,
    "wiki_cleanup_merged_branches": project.wiki_cleanup_merged_branches,
    "record_prelude_marker": project.record_prelude_marker,
    # restoration writes (T2 Car B — pre-compact drain is write-only, no compute)
    "pre_compact_drain": restoration.pre_compact_drain,
}


def admin_ops() -> frozenset[str]:
    """Return the set of registered admin op names (I32 capability discovery)."""
    return frozenset(_ADMIN_OPS)


@observe(tier="boundary", metric="backend.admin.run_admin_op")
def run_admin_op(op: str, payload: dict) -> dict:
    """Dispatch a single admin op to its backend execution body.

    Args:
        op: Op name — must be a key of ``_ADMIN_OPS`` (mirrors the core tool name).
        payload: The op's arguments (already validated + gated core-side).

    Returns:
        The impl's result dict.

    Raises:
        KeyError: if ``op`` is not a registered admin op (route maps to 400).
    """
    impl = _ADMIN_OPS.get(op)
    if impl is None:
        raise KeyError(f"unknown admin op: {op!r}")
    # T2 Car B: ops that anchor (agent_prompt_save) or drain read _st._replay,
    # which the shared root no longer builds. Compose the backend restoration
    # engines here (idempotent no-op once built; also covers the test bypass
    # path that skips the /admin route's _ensure_recall_engines).
    from yadgar.backend.restoration import ensure_restoration_engines  # noqa: PLC0415

    ensure_restoration_engines()
    return impl(payload)
