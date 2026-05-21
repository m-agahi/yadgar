"""Remaining admin MCP tools: forget, validate_memory, consolidate_now, reembed_all,
memory_stats, add_rule, get_rules, memory_get, wiki_get, memory_update, wiki_update."""

from __future__ import annotations

import logging

import yadgar.server._state as _st
from yadgar.config import get_settings
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
def consolidate_now() -> dict:
    """Trigger an immediate consolidation cycle."""
    if _st._consolidation is not None:
        stats = _st._consolidation.force_consolidate()
        if _st._sleep is not None:
            try:
                sleep_stats = _st._sleep.run_sleep_cycle()
                stats["sleep_cycle"] = sleep_stats
            except Exception:
                logger.exception("Sleep cycle failed during consolidate_now")
        return {"status": "completed", **stats}
    return {"status": "error", "message": "Consolidation engine not initialized"}


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


@_tool()
def memory_stats() -> dict:
    """Return system memory statistics."""
    storage = _get_storage()
    stats = storage.get_memory_stats()

    if _st._write_gate is not None:
        # Track rejections via memories with surprisal below threshold
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
        active_rules = _st._rules_engine.get_all_rules()
        stats["active_rules"] = len(active_rules)

    if _st._cls is not None:
        stats["episodic_count"] = storage.count_memories_by_store_type("episodic")
        stats["semantic_count"] = storage.count_memories_by_store_type("semantic")

    if _st._cognitive_map is not None:
        stats["sr_dimensions"] = (
            "active" if _st._cognitive_map.has_sufficient_data() else "insufficient_data"
        )

    if _st._causal is not None:
        causal_edges = storage.get_all_causal_edges()
        stats["causal_edges"] = len(causal_edges)

    if _st._metacognition is not None:
        # Average coverage across recent queries isn't tracked globally,
        # but we can report the chunk limit setting
        stats["cognitive_load_limit"] = _st._metacognition._chunk_limit

    # DB-size telemetry — always include so callers can monitor disk usage.
    try:
        db_size_info = storage.get_db_size()
        # Append per-table breakdown so callers can identify which table drives bloat.
        # Uses the module-level _PER_TABLE_FIELDS constant (shared with check_invariants).
        _ms_timeout = settings.CHECK_INVARIANTS_QUERY_TIMEOUT_SECONDS
        _ms_per_table: dict[str, dict] = {}
        for _ms_tbl, _ms_field in _st._PER_TABLE_FIELDS.items():
            try:
                if _ms_field:
                    _ms_rows = _q_with_timeout(
                        storage,
                        f"SELECT count() AS c, "
                        f"math::sum(string::len({_ms_field})) AS content_bytes "
                        f"FROM {_ms_tbl} GROUP ALL",
                        timeout_seconds=_ms_timeout,
                    )
                else:
                    _ms_rows = _q_with_timeout(
                        storage,
                        f"SELECT count() AS c FROM {_ms_tbl} GROUP ALL",
                        timeout_seconds=_ms_timeout,
                    )
                if _ms_rows:
                    _ms_r = _ms_rows[0]
                    _ms_entry: dict = {"rows": int(_ms_r.get("c", 0))}
                    if _ms_field:
                        _ms_entry["estimated_bytes"] = int(_ms_r.get("content_bytes") or 0)
                    _ms_per_table[_ms_tbl] = _ms_entry
                else:
                    _ms_per_table[_ms_tbl] = {"rows": 0}
            except Exception as _ms_exc:
                logger.warning("memory_stats: per_table query failed for %s: %s", _ms_tbl, _ms_exc)
                _ms_per_table[_ms_tbl] = {"rows": 0, "error": str(_ms_exc)}
        db_size_info["per_table"] = _ms_per_table
        stats["db_size"] = db_size_info
    except Exception:
        pass  # non-fatal: stats are best-effort

    # P11 — metrics summary block (I8: backpressure must be observable via memory_stats)
    try:
        from yadgar.metrics import (  # noqa: PLC0415
            yadgar_drainer_lag_ms,
            yadgar_recall_duration_ms,
        )

        def _p95(hist) -> float:
            """Extract approximate p95 from a Histogram sample."""
            try:
                samples = list(hist.collect()[0].samples)
                count = next((s.value for s in samples if s.name.endswith("_count")), 0.0)
                if count == 0:
                    return 0.0
                target = count * 0.95
                bucket_samples = [s for s in samples if s.name.endswith("_bucket")]
                bucket_samples.sort(key=lambda s: s.labels.get("le", "0"))
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

        # Queue depth from filesystem gauge (last scraped value)
        try:
            _qd_samples = list(yadgar_drainer_lag_ms.collect()[0].samples)
            _queue_depth_val = 0
            # Read from queue_depth gauge directly
            from yadgar.metrics import yadgar_queue_depth  # noqa: PLC0415

            _qd_gauge_samples = list(yadgar_queue_depth.collect()[0].samples)
            for _s in _qd_gauge_samples:
                if _s.labels.get("queue") == "queue":
                    _queue_depth_val = int(_s.value)
                    break
        except Exception:
            _queue_depth_val = 0

        # Circuit breaker states (read-only, non-fatal)
        _cb_states: dict[str, int] = {}
        try:
            from yadgar.ml_client import RemoteMLClient  # noqa: PLC0415

            _ml = getattr(_st, "_ml_client", None)
            if isinstance(_ml, RemoteMLClient):
                _state_map = {"closed": 0, "half_open": 1, "open": 2}
                for _ep in ("ce", "nli", "pair"):
                    _cb = getattr(_ml, f"_cb_{_ep}", None)
                    if _cb is not None:
                        _cb_states[_ep] = _state_map.get(_cb._state, 0)
        except Exception:
            pass

        stats["metrics"] = {
            "queue_depth": _queue_depth_val,
            "drainer_lag_p95_ms": _p95(yadgar_drainer_lag_ms),
            "recall_p95_ms": _p95(yadgar_recall_duration_ms),
            "circuit_breaker_states": _cb_states,
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
def wiki_update(page_id: int, fields: dict) -> dict:
    """Patch selected fields on a wiki page record.

    Allowed keys: content, tags, category, confidence.
    Rejected keys: slug, id, created_at (and any unknown key).

    Returns the updated wiki page dict.
    Raises ValueError on disallowed or unknown keys.
    """
    unknown = set(fields) - _WIKI_UPDATE_ALLOWED
    if unknown:
        raise ValueError(
            f"Disallowed field(s) for wiki_update: {sorted(unknown)}. "
            f"Allowed: {sorted(_WIKI_UPDATE_ALLOWED)}"
        )
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
