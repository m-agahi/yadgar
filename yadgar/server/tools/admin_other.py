"""Remaining admin MCP tools: forget, validate_memory, consolidate_now, reembed_all,
memory_stats, add_rule, get_rules, memory_get, wiki_get, memory_update, wiki_update."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import yadgar.server._state as _st
from yadgar.config import get_settings
from yadgar.secrets import gate_or_reject
from yadgar.server._app import _tool
from yadgar.server._helpers import _q_with_timeout
from yadgar.server.lifecycle import (
    _get_embeddings,
    _get_storage,
)

logger = logging.getLogger(__name__)

settings = get_settings()


# ── Allowed update fields ──────────────────────────────────────────────

# Allowed fields for MCP-level memory_update (subset of _MEMORY_UPDATABLE_FIELDS)
_MEMORY_UPDATE_ALLOWED: frozenset[str] = frozenset({"content", "tags", "is_protected", "is_stale"})

# Allowed fields for MCP-level wiki_update
_WIKI_UPDATE_ALLOWED: frozenset[str] = frozenset({"content", "tags", "category", "confidence"})


@_tool()
def forget(memory_id: int) -> dict:
    """Mark a memory for deletion by setting heat to 0, then delete it."""
    storage = _get_storage()
    memory = storage.get_memory(memory_id)
    if memory is None:
        return {"memory_id": memory_id, "status": "not_found"}
    storage.delete_memory(memory_id)
    return {"memory_id": memory_id, "status": "deleted"}


@_tool(power=True)
def validate_memory(memory_id: int) -> dict:
    """Check memory validity against current file state."""
    from yadgar.server._helpers import _file_hash

    if _st._staleness is not None:
        result = _st._staleness.validate_memory(memory_id)
        # Normalize response format for the MCP tool
        return {
            "memory_id": memory_id,
            "is_valid": result["valid"],
            "reason": result["reason"],
        }

    # Fallback if staleness detector not initialized
    storage = _get_storage()
    memory = storage.get_memory(memory_id)
    if memory is None:
        return {"memory_id": memory_id, "is_valid": False, "reason": "memory not found"}

    if not memory.get("file_hash"):
        return {"memory_id": memory_id, "is_valid": True, "reason": "no file hash to validate"}

    current_hash = _file_hash(memory["directory_context"])
    if current_hash is None:
        storage.update_memory_staleness(memory_id, True)
        return {"memory_id": memory_id, "is_valid": False, "reason": "file no longer exists"}

    if current_hash != memory["file_hash"]:
        storage.update_memory_staleness(memory_id, True)
        return {"memory_id": memory_id, "is_valid": False, "reason": "file has changed"}

    return {"memory_id": memory_id, "is_valid": True, "reason": "file hash matches"}


@_tool(power=True)
def consolidate_now(mode: str = "light") -> dict:
    """Trigger an immediate consolidation cycle.

    mode="light" (default): consolidation cycle only (decay, episodes, merge,
        CLS, causal). Fast — typically < 30 seconds. Use for pre-shutdown
        flushes, debug runs, and queue-fill scenarios.

    mode="full": consolidation cycle + full sleep cycle (dream replay,
        community detection, cluster summaries, re-embedding, compression,
        auto-narrate) + anchor audit pass (if ANCHOR_AUDIT_CONSOLIDATION_ENABLED).
        Takes 5–15 minutes. Use for deliberate maintenance before a multi-day
        break or after a large memory import. Also sets the 6-hour sleep cycle
        gate timestamp so the nightly cron does not double-fire.
    """
    if _st._consolidation is None:
        return {"status": "error", "message": "Consolidation engine not initialized"}

    if mode not in ("light", "full"):
        return {"status": "error", "message": f"Invalid mode {mode!r}. Use 'light' or 'full'."}

    stats = _st._consolidation.force_consolidate()

    if mode == "full" and _st._sleep is not None:
        try:
            sleep_stats = _st._sleep.run_sleep_cycle()
            stats["sleep_cycle"] = sleep_stats
            # Update the 6-hour gate timestamp so nightly cron sees the cycle ran
            _st._consolidation._last_sleep_cycle = datetime.now(UTC)
        except Exception:
            logger.exception("Sleep cycle failed during consolidate_now(mode='full')")

    # v5.9.0: anchor audit pass as final step — mode='full' only (gated on config flag)
    if mode == "full":
        cfg = get_settings()
        if cfg.ANCHOR_AUDIT_CONSOLIDATION_ENABLED:
            try:
                from yadgar.server.tools.audit import _run_anchor_audit_pass  # noqa: PLC0415

                anchor_pass_stats = _run_anchor_audit_pass(_get_storage())
                stats["anchor_audit_pass"] = anchor_pass_stats
            except Exception:
                logger.exception("Anchor audit pass failed during consolidate_now (non-fatal)")

    return {"status": "completed", "mode": mode, **stats}


@_tool(power=True)
def reembed_all() -> dict:
    """Generate embeddings for all memories that are missing them.

    Bulk-imported memories often lack embeddings. This tool generates them
    using the current embedding model, enabling similarity search and
    semantic relationship discovery during consolidation.
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
        texts = [r["content"] for r in batch]
        ids = [r["id"] for r in batch]
        encoded = embeddings.encode_batch(texts)
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


def _ms_per_table_stats(storage) -> dict:
    """Return per-table row/byte counts for DB-size telemetry (memory_stats helper)."""
    _ms_timeout = settings.CHECK_INVARIANTS_QUERY_TIMEOUT_SECONDS
    per_table: dict[str, dict] = {}
    for tbl, field in _st._PER_TABLE_FIELDS.items():
        try:
            if field:
                rows = _q_with_timeout(
                    storage,
                    f"SELECT count() AS c, math::sum(string::len({field})) AS content_bytes "
                    f"FROM {tbl} GROUP ALL",
                    timeout_seconds=_ms_timeout,
                )
            else:
                rows = _q_with_timeout(
                    storage,
                    f"SELECT count() AS c FROM {tbl} GROUP ALL",
                    timeout_seconds=_ms_timeout,
                )
            if rows:
                r = rows[0]
                entry: dict = {"rows": int(r.get("c", 0))}
                if field:
                    entry["estimated_bytes"] = int(r.get("content_bytes") or 0)
                per_table[tbl] = entry
            else:
                per_table[tbl] = {"rows": 0}
        except Exception as exc:
            logger.warning("memory_stats: per_table query failed for %s: %s", tbl, exc)
            per_table[tbl] = {"rows": 0, "error": str(exc)}
    return per_table


def _ms_histogram_p95(hist) -> float:
    """Extract approximate p95 from a Prometheus Histogram sample."""
    try:
        samples = list(hist.collect()[0].samples)
        count = next((s.value for s in samples if s.name.endswith("_count")), 0.0)
        if count == 0:
            return 0.0
        target = count * 0.95
        bucket_samples = sorted(
            (s for s in samples if s.name.endswith("_bucket")),
            key=lambda s: s.labels.get("le", "0"),
        )
        cumulative = 0.0
        for s in bucket_samples:
            le_val = s.labels.get("le", "+Inf")
            if le_val == "+Inf":
                break
            cumulative = s.value
            if cumulative >= target:
                return float(le_val)
        return 0.0
    except Exception:
        return 0.0


def _ms_queue_depth() -> int:
    """Return current queue depth from Prometheus gauge (memory_stats helper)."""
    try:
        from yadgar.metrics import yadgar_queue_depth  # noqa: PLC0415

        gauge_samples = list(yadgar_queue_depth.collect()[0].samples)
        for s in gauge_samples:
            if s.labels.get("queue") == "queue":
                return int(s.value)
    except Exception:
        pass
    return 0


def _ms_circuit_breaker_states() -> dict[str, int]:
    """Return ML client circuit-breaker states (memory_stats helper)."""
    cb_states: dict[str, int] = {}
    try:
        from yadgar.ml_client import RemoteMLClient  # noqa: PLC0415

        _ml = getattr(_st, "_ml_client", None)
        if isinstance(_ml, RemoteMLClient):
            state_map = {"closed": 0, "half_open": 1, "open": 2}
            for ep in ("ce", "nli", "pair"):
                cb = getattr(_ml, f"_cb_{ep}", None)
                if cb is not None:
                    cb_states[ep] = state_map.get(cb._state, 0)
    except Exception:
        pass
    return cb_states


@_tool()
def memory_stats() -> dict:
    """Return system memory statistics."""
    storage = _get_storage()
    stats = storage.get_memory_stats()

    if _st._write_gate is not None:
        stats["write_gate_rejections"] = getattr(_st._write_gate, "_rejection_count", 0)

    if _st._engram is not None:
        try:
            slot_stats = _st._engram.get_slot_statistics()
            total = slot_stats.get("total_slots", 1)
            occupied = slot_stats.get("occupied_slots", 0)
            stats["engram_slot_utilization"] = round(occupied / max(total, 1), 4)
        except Exception:
            stats["engram_slot_utilization"] = 0.0

    if _st._rules_engine is not None:
        stats["active_rules"] = len(_st._rules_engine.get_all_rules())

    if _st._cls is not None:
        stats["episodic_count"] = storage.count_memories_by_store_type("episodic")
        stats["semantic_count"] = storage.count_memories_by_store_type("semantic")

    if _st._cognitive_map is not None:
        stats["sr_dimensions"] = (
            "active" if _st._cognitive_map.has_sufficient_data() else "insufficient_data"
        )

    if _st._causal is not None:
        stats["causal_edges"] = len(storage.get_all_causal_edges())

    if _st._metacognition is not None:
        stats["cognitive_load_limit"] = _st._metacognition._chunk_limit

    # DB-size telemetry — always include so callers can monitor disk usage.
    try:
        db_size_info = storage.get_db_size()
        db_size_info["per_table"] = _ms_per_table_stats(storage)
        stats["db_size"] = db_size_info
    except Exception:
        pass  # non-fatal: stats are best-effort

    # P11 — metrics summary block (I8: backpressure must be observable via memory_stats)
    try:
        from yadgar.metrics import (  # noqa: PLC0415
            yadgar_drainer_lag_ms,
            yadgar_recall_duration_ms,
        )

        stats["metrics"] = {
            "queue_depth": _ms_queue_depth(),
            "drainer_lag_p95_ms": _ms_histogram_p95(yadgar_drainer_lag_ms),
            "recall_p95_ms": _ms_histogram_p95(yadgar_recall_duration_ms),
            "circuit_breaker_states": _ms_circuit_breaker_states(),
        }
    except Exception:
        # If prometheus_client missing or any error: still return a stub so
        # callers can check the key without crashing (I3 backward compat)
        stats["metrics"] = {
            "queue_depth": 0,
            "drainer_lag_p95_ms": 0.0,
            "recall_p95_ms": 0.0,
            "circuit_breaker_states": {},
        }

    return stats


@_tool(power=True)
def add_rule(
    rule_type: str,
    scope: str,
    condition: str,
    action: str,
    priority: int = 0,
    scope_value: str = "",
) -> dict:
    """Add a neuro-symbolic rule for filtering/re-ranking memories.

    rule_type: "hard" (must satisfy) or "soft" (preference).
    scope: "global", "directory", or "file".
    condition: e.g. "importance > 0.7", "tag contains architecture".
    action: "filter" for hard rules, "boost:0.3" or "penalty:0.2" for soft rules.
    priority: Higher = applied first (default 0).
    scope_value: Directory path or file pattern for scoped rules.
    """
    if _st._rules_engine is None:
        return {"status": "error", "message": "RulesEngine not initialized"}
    try:
        rule_id = _st._rules_engine.add_rule(
            rule_type=rule_type,
            scope=scope,
            condition=condition,
            action=action,
            priority=priority,
            scope_value=scope_value or None,
        )
        return {"status": "created", "rule_id": rule_id}
    except ValueError as e:
        return {"status": "error", "message": str(e)}


@_tool(power=True)
def get_rules(directory: str = "") -> list[dict]:
    """Get active rules. If directory is provided, returns only applicable rules."""
    if _st._rules_engine is None:
        return []
    if directory:
        return _st._rules_engine.get_applicable_rules(directory)
    return _st._rules_engine.get_all_rules()


@_tool()
def memory_get(memory_id: int) -> dict | None:
    """Fetch a memory record by integer ID.

    Returns the memory dict, or None if not found.
    Embedding bytes are stripped from the response.
    """
    result = _st._storage.get_memory(int(memory_id))
    if result is None:
        return None
    # Strip raw embedding bytes — not useful over MCP and wastes bandwidth
    result.pop("embedding", None)
    result.pop("centroid_embedding", None)
    result.pop("implicit_embedding", None)
    return result


@_tool()
def wiki_get(page_id: int) -> dict | None:
    """Fetch a wiki page by integer ID.

    Returns the wiki page dict, or None if not found.
    Embedding bytes are stripped from the response.
    """
    result = _st._storage.get_wiki_page(int(page_id))
    if result is None:
        return None
    # Strip raw embedding bytes
    result.pop("embedding", None)
    return result


@_tool(power=True)
def memory_update(memory_id: int, fields: dict) -> dict:
    """Patch selected fields on a memory record.

    Allowed keys: content, tags, is_protected, is_stale.
    Rejected keys: heat, embedding, id, created_at (and any unknown key).

    Returns the updated memory dict.
    Raises ValueError on disallowed or unknown keys.
    """
    unknown = set(fields) - _MEMORY_UPDATE_ALLOWED
    if unknown:
        raise ValueError(
            f"Disallowed field(s) for memory_update: {sorted(unknown)}. "
            f"Allowed: {sorted(_MEMORY_UPDATE_ALLOWED)}"
        )
    if not fields:
        result = _st._storage.get_memory(int(memory_id))
        if result is None:
            raise ValueError(f"Memory {memory_id} not found")
        result.pop("embedding", None)
        result.pop("centroid_embedding", None)
        result.pop("implicit_embedding", None)
        return result
    _st._storage.update_memory_fields(int(memory_id), **fields)
    updated = _st._storage.get_memory(int(memory_id))
    if updated is None:
        raise ValueError(f"Memory {memory_id} not found after update")
    updated.pop("embedding", None)
    updated.pop("centroid_embedding", None)
    updated.pop("implicit_embedding", None)
    return updated


@_tool(power=True)
def vacuum_checkpoints(dry_run: bool = True) -> dict:
    """Collapse stale checkpoints: keep latest per directory_context, delete rest.

    Idempotent. Operators should run vacuum_checkpoints(dry_run=False) once after
    upgrading to v5.6.5 to collapse rows accumulated under the old global-deactivate
    scheme.

    Args:
        dry_run: if True (default), report stale count without deleting. Set to
            False to perform the actual deletion.

    Returns:
        {
            "stale_count": int,   # rows that would be / were deleted
            "deleted": int,       # 0 if dry_run=True
            "survivors": int,     # rows remaining after vacuum
            "dry_run": bool,
        }
    """
    from yadgar.storage.ops import vacuum_checkpoints as _vc  # noqa: PLC0415

    storage = _get_storage()
    return _vc(storage, dry_run=dry_run)


@_tool(power=True)
def wiki_update(page_id: int, fields: dict, wait: bool = False) -> dict:
    """Patch selected fields on a wiki page record.

    Allowed keys: content, tags, category, confidence.
    Rejected keys: slug, id, created_at (and any unknown key).

    Returns the updated wiki page dict.
    Raises ValueError on disallowed or unknown keys.

    wait=True: accepted for API symmetry with wiki_add. This tool is
    synchronous (no async queue) — wait=True is a no-op and always returns
    immediately with committed=True in the response.
    """
    unknown = set(fields) - _WIKI_UPDATE_ALLOWED
    if unknown:
        raise ValueError(
            f"Disallowed field(s) for wiki_update: {sorted(unknown)}. "
            f"Allowed: {sorted(_WIKI_UPDATE_ALLOWED)}"
        )
    # v5.10.2: secret gate — scan content field before any state mutation
    _content_val = fields.get("content", "") if fields else ""
    _gate = gate_or_reject(_content_val)
    if _gate is not None:
        return _gate

    if not fields:
        result = _st._storage.get_wiki_page(int(page_id))
        if result is None:
            raise ValueError(f"Wiki page {page_id} not found")
        result.pop("embedding", None)
        return result
    _st._storage.update_wiki_page(int(page_id), fields)
    updated = _st._storage.get_wiki_page(int(page_id))
    if updated is None:
        raise ValueError(f"Wiki page {page_id} not found after update")
    updated.pop("embedding", None)
    return updated
