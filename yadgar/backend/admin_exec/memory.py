"""Backend execution bodies for the memory/rules-write admin ops (R3 Car 3b / R5).

These are the storage-write halves of the core memory/rules MCP tools whose
``@_tool`` shells (validation + secret-gate) stay in
``yadgar.core.server.tools.*`` and forward the DB write here over HTTP
(POST /admin) via ``_forward_admin``.

Group 2 (R5) ops: ``forget``, ``memory_update``, ``reembed_all``, ``add_rule``,
``archive_purge``. Reads (recent_memories, memory_stats, memory_get, get_rules,
dlq_inspect) stay core with direct storage access — "zero DB" is a write-side goal.

Each op is an undecorated ``(payload: dict) -> dict`` function; the ``@observe``
decorators satisfy the I33 tri-signal ratchet. Storage / embeddings / rules are
fetched via the shared lifecycle getters — the /admin route builds the slim
engine set (which includes storage, embeddings and the rules engine) first.
"""

from __future__ import annotations

import logging

import yadgar._shared.runtime.state as _st
from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.lifecycle import _get_embeddings, _get_storage

logger = logging.getLogger(__name__)


@observe(tier="boundary", metric="backend.admin.forget")
def forget(payload: dict) -> dict:
    """Delete a memory (heat→0 semantics) + bump structural epoch. Storage-write half.

    payload: {"memory_id": int}
    Returns {memory_id, status: "deleted"|"not_found"}.

    The structural-epoch bump is file-backed on the shared queue volume (Car 2),
    so a backend-side bump busts the core process's cached project_brief for that
    directory. It must never break the delete.
    """
    memory_id = int(payload["memory_id"])
    storage = _get_storage()
    memory = storage.get_memory(memory_id)
    if memory is None:
        return {"memory_id": memory_id, "status": "not_found"}
    directory = memory.get("directory_context")
    storage.delete_memory(memory_id)
    # v5.111.0 (Car 1): a delete is a STRUCTURAL write — bump the directory's
    # epoch (normalized to git-root, same key project_brief reads) so any cached
    # project_brief for that dir busts. Cross-process via the shared queue volume
    # (Car 2). Guarded: must never break the delete.
    try:
        from yadgar._shared.server_helpers import _bump_epoch_for_context  # noqa: PLC0415

        _bump_epoch_for_context(directory)
    except Exception:  # noqa: BLE001 - instrumentation must never break the delete
        pass
    return {"memory_id": memory_id, "status": "deleted"}


@observe(tier="boundary", metric="backend.admin.memory_update")
def memory_update(payload: dict) -> dict:
    """Patch selected fields on a memory record. Storage-write half.

    payload: {"memory_id": int, "fields": dict}
    ``fields`` is already validated core-side (allowed keys enforced there).
    Returns the updated memory dict (embedding bytes stripped).
    Raises ValueError if the memory is not found.
    """
    memory_id = int(payload["memory_id"])
    fields = payload.get("fields") or {}
    if not fields:
        result = _st._storage.get_memory(memory_id)
        if result is None:
            raise ValueError(f"Memory {memory_id} not found")
        result.pop("embedding", None)
        result.pop("centroid_embedding", None)
        result.pop("implicit_embedding", None)
        return result

    # Car 2 (Part B): content-change re-embed guard. update_memory_fields never
    # re-encodes, so a content patch would keep a STALE vector — the memory stays
    # unfindable by its new text (the latent bug this fixes). Re-embed ONLY when
    # `content` is patched AND actually differs from the stored content; a
    # metadata-only patch (tags/is_protected/is_stale) or a same-value content
    # patch stays cheap (no embed round-trip).
    _reembed_content: str | None = None
    if "content" in fields:
        _existing = _st._storage.get_memory(memory_id)
        if _existing is None:
            raise ValueError(f"Memory {memory_id} not found")
        _new_content = fields["content"]
        if _new_content and _new_content != _existing.get("content"):
            _reembed_content = _new_content

    _st._storage.update_memory_fields(memory_id, **fields)

    if _reembed_content is not None:
        try:
            embeddings = _get_embeddings()
            encoded = embeddings.encode_batch([_reembed_content])
            emb = encoded[0] if encoded else None
            if emb is not None:
                _st._storage.update_memory_embedding(memory_id, emb, embeddings.model_name)
        except Exception:  # noqa: BLE001 — re-embed is best-effort; the field write already committed
            logger.warning(
                "memory_update re-embed failed for memory %s (field write committed)",
                memory_id,
                exc_info=True,
            )

    updated = _st._storage.get_memory(memory_id)
    if updated is None:
        raise ValueError(f"Memory {memory_id} not found after update")
    updated.pop("embedding", None)
    updated.pop("centroid_embedding", None)
    updated.pop("implicit_embedding", None)
    return updated


@observe(tier="boundary", metric="backend.admin.reembed_all")
def reembed_all(payload: dict) -> dict:
    """Generate embeddings for all memories missing them. Storage-write half.

    payload: {} (no args)
    Returns {status, reembedded, total_missing, model} or an all-present stub.

    Heavy op — the core forwarder passes a generous HTTP timeout so a large
    backlog does not trip the default 30s.
    """
    storage = _get_storage()
    embeddings = _get_embeddings()

    rows = storage.get_memories_without_embeddings()

    if not rows:
        return {"status": "ok", "message": "All memories already have embeddings", "reembedded": 0}

    batch_size = 64
    total = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        # Filter out rows with None/empty content — passing None to encode_batch
        # causes the entire batch to fail on the backend (HTTP 500), returning
        # all-None embeddings and leaving reembedded=0 even when other rows are valid.
        valid = [(r["id"], r["content"]) for r in batch if r.get("content")]
        if not valid:
            continue
        ids, texts = zip(*valid, strict=False)
        encoded = embeddings.encode_batch(list(texts))
        for mid, emb in zip(ids, encoded, strict=False):
            if emb is not None:
                storage.update_memory_embedding(mid, emb, embeddings.model_name)
                total += 1

    return {
        "status": "ok",
        "reembedded": total,
        "total_missing": len(rows),
        "model": embeddings.model_name,
    }


@observe(tier="boundary", metric="backend.admin.add_rule")
def add_rule(payload: dict) -> dict:
    """Add a neuro-symbolic rule (rules-engine DB write). Storage-write half.

    payload: {"rule_type", "scope", "condition", "action", "priority", "scope_value"}
    Returns {status: "created", rule_id} or {status: "error", message}.

    Writes via ``_st._rules_engine.add_rule`` → ``storage.insert_rule`` and clears
    the BACKEND rules-engine's applicable-rules cache. Write-policy enforcement
    (phase_validate → check_write_policy) runs in the backend drainer with THIS
    same engine instance, so the enforcement path stays coherent. The core
    ``wiki.py`` write-policy pre-check uses its own (separate) core RulesEngine
    cache — a pre-existing cross-process advisory drift, not introduced here.
    """
    if _st._rules_engine is None:
        return {"status": "error", "message": "RulesEngine not initialized"}
    try:
        rule_id = _st._rules_engine.add_rule(
            rule_type=payload["rule_type"],
            scope=payload["scope"],
            condition=payload["condition"],
            action=payload["action"],
            priority=int(payload.get("priority", 0)),
            scope_value=payload.get("scope_value") or None,
        )
        return {"status": "created", "rule_id": rule_id}
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}


@observe(tier="boundary", metric="backend.admin.update_memory_staleness")
def update_memory_staleness(payload: dict) -> dict:
    """Set the is_stale flag on a memory. Storage-write half (T2 Car E1).

    payload: {"memory_id": int, "is_stale": bool}
    Core caller: the validate_memory fallback path in admin_other.py (the
    file-hash comparison runs core-side — host FS — and only the flag write
    forwards here).
    """
    memory_id = int(payload["memory_id"])
    is_stale = bool(payload.get("is_stale", True))
    storage = _get_storage()
    storage.update_memory_staleness(memory_id, is_stale)
    return {"memory_id": memory_id, "is_stale": is_stale}


@observe(tier="boundary", metric="backend.admin.vacuum_stale_sentinels")
def vacuum_stale_sentinels(payload: dict) -> dict:
    """Delete _session_end_sentinel memory rows older than retention_days.

    Storage read+delete half (T2 Car E1) — relocated whole from
    ``core/server/http.py`` (stateless-over-DB compute; the only trigger is
    ops/debug usage). Never raises.

    payload: {"retention_days": int | None} — None resolves the configured
    SESSION_END_RETENTION_DAYS backend-side.
    Returns {"deleted": int, "retention_days": int}.
    """
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    retention_days = payload.get("retention_days")
    if retention_days is None:
        from yadgar._shared.config import get_settings  # noqa: PLC0415

        retention_days = get_settings().SESSION_END_RETENTION_DAYS
    retention_days = int(retention_days)

    cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
    deleted = 0
    try:
        storage = _get_storage()
        rows = storage._q(
            "SELECT id FROM memory "
            "WHERE '_session_end_sentinel' INSIDE tags "
            "AND created_at < $cutoff",
            {"cutoff": cutoff},
        )
        deleted = _vacuum_delete_rows(storage, rows)
    except Exception as e:  # noqa: BLE001 — ops path: log, never raise
        logger.warning("sentinel vacuum error: %s", e)
    return {"deleted": deleted, "retention_days": retention_days}


@observe(tier="stage", metric="backend.admin.vacuum_delete_rows")
def _vacuum_delete_rows(storage, rows: list) -> int:
    """Delete memory rows by id. Returns count deleted."""
    deleted = 0
    for row in rows:
        mid = storage._extract_id(row.get("id"))
        if mid is None:
            continue
        try:
            storage.delete_memory(mid)
            deleted += 1
        except Exception as e:  # noqa: BLE001 — per-row: log and continue
            logger.warning("sentinel vacuum: delete_memory(%s) failed: %s", mid, e)
    return deleted


@observe(tier="boundary", metric="backend.admin.archive_purge")
def archive_purge(payload: dict) -> dict:
    """Purge memory_archive rows older than retention threshold. Storage-write half.

    payload: {"dry_run": bool, "retention_days": int | None}
    Returns the purge summary dict (candidates/purged/skipped_*/circuit_breaker_hit/
    sample/dry_run/retention_days). dry_run=True performs no deletion.
    """
    dry_run = bool(payload.get("dry_run", True))
    retention_days = payload.get("retention_days")

    storage = _get_storage()

    from yadgar._shared.storage.ops import purge_expired_archives as _purge  # noqa: PLC0415

    raw = _purge(storage, dry_run=dry_run, retention_days_override=retention_days)

    # Resolve effective retention_days for caller visibility.
    if retention_days is not None:
        effective_days = int(retention_days)
    else:
        from yadgar._shared.config import get_settings  # noqa: PLC0415

        effective_days = get_settings().MEMORY_ARCHIVE_RETENTION_DAYS

    result = {
        "candidates": raw["candidates"],
        "purged": raw["purged"],
        "skipped_protected": raw["skipped_protected"],
        "skipped_anchor": raw["skipped_anchor"],
        "skipped_recent": raw["skipped_recent"],
        "circuit_breaker_hit": raw["circuit_breaker_hit"],
        "sample": raw.get("candidate_ids", [])[:10],
        "dry_run": dry_run,
        "retention_days": effective_days,
    }

    logger.info(
        "archive_purge: dry_run=%s retention_days=%d candidates=%d purged=%d",
        dry_run,
        effective_days,
        result["candidates"],
        result["purged"],
    )

    return result
