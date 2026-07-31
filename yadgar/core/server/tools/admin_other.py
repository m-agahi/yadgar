"""Remaining admin MCP tools: forget, validate_memory, consolidate_now, reembed_all,
recent_memories, memory_stats, add_rule, get_rules, memory_get, wiki_get, memory_update,
wiki_update."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

import yadgar._shared.runtime.state as _st
from yadgar._shared.config import get_settings
from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.lifecycle import (
    _get_storage,
)
from yadgar._shared.security.secrets import gate_or_reject
from yadgar.core.forward import _forward_admin
from yadgar.core.server._app import _tool
from yadgar.core.server._helpers import _q_with_timeout

logger = logging.getLogger(__name__)

settings = get_settings()


# ── Allowed update fields ──────────────────────────────────────────────

# Allowed fields for MCP-level memory_update (subset of _MEMORY_UPDATABLE_FIELDS).
# v5.158 (Car #85): importance + tier widened in so de_anchor can retire an anchor
# into the normal decay path (clearing is_protected alone is insufficient — the
# decay RATE is keyed on importance: thermodynamics.py compute_decay uses the slow
# IMPORTANCE_DECAY_FACTOR when importance>0.7, so an anchor left at importance=1.0
# barely decays even after is_protected is cleared).
_MEMORY_UPDATE_ALLOWED: frozenset[str] = frozenset(
    {"content", "tags", "is_protected", "is_stale", "importance", "tier"}
)

# Allowed fields for MCP-level wiki_update
_WIKI_UPDATE_ALLOWED: frozenset[str] = frozenset({"content", "tags", "category", "confidence"})


@_tool()
def forget(memory_id: int) -> dict:
    """Mark a memory for deletion by setting heat to 0, then delete it."""
    # R3 Car 3b: the delete + structural-epoch bump forward to the backend /admin
    # op. The epoch bump is file-backed on the shared queue volume (Car 2), so a
    # backend-side bump still busts the core process's cached project_brief.
    return _forward_admin("forget", {"memory_id": int(memory_id)})


@_tool(power=True)
def validate_memory(memory_id: int) -> dict:
    """Check memory validity against current file state."""
    from yadgar._shared.server_helpers import _file_hash

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

    # T2 Car E1 (ADR-0078): the host-FS hash comparison stays core; the
    # staleness-flag WRITE forwards to the backend /admin op.
    current_hash = _file_hash(memory["directory_context"])
    if current_hash is None:
        _forward_admin("update_memory_staleness", {"memory_id": memory_id, "is_stale": True})
        return {"memory_id": memory_id, "is_valid": False, "reason": "file no longer exists"}

    if current_hash != memory["file_hash"]:
        _forward_admin("update_memory_staleness", {"memory_id": memory_id, "is_stale": True})
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
        auto-narrate) + anchor audit pass (if ANCHOR_AUDIT_CONSOLIDATION_ENABLED)
        + graph-layout precompute (unconditional on the full path).
        Takes 5–15 minutes. Use for deliberate maintenance before a multi-day
        break or after a large memory import. Also sets the 6-hour sleep cycle
        gate timestamp so the nightly cron does not double-fire.
    """
    if mode not in ("light", "full"):
        return {"status": "error", "message": f"Invalid mode {mode!r}. Use 'light' or 'full'."}

    # R3 Car 1 D3: the consolidation COMPUTE (cycle + sleep) lives in the backend.
    # Forward to /consolidate; the core orchestrator runs the graph-layout
    # precompute (full) + invariant-check + auto-vacuum tail around the result.
    # Forward-only: backend unreachable → RuntimeError/HTTP error surfaces.
    from yadgar.core.consolidation import run_consolidate_now  # noqa: PLC0415

    stats = run_consolidate_now(mode)

    # v5.9.0: anchor audit pass as final step — mode='full' only (gated on config flag)
    if mode == "full":
        cfg = get_settings()
        if cfg.ANCHOR_AUDIT_CONSOLIDATION_ENABLED:
            try:
                from yadgar.core.server.tools.audit import _run_anchor_audit_pass  # noqa: PLC0415

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
    # R3 Car 3b: heavy DB write (embeds every missing row). Forwards to backend
    # /admin with a generous timeout so a large backlog does not trip the default
    # 30s HTTP timeout.
    return _forward_admin("reembed_all", {}, timeout_s=1800.0)


# ── Duration parser for recent_memories ───────────────────────────────────

_DURATION_RE = re.compile(r"^(\d+)(m|h|d)$", re.IGNORECASE)

_UNIT_SECONDS: dict[str, int] = {"m": 60, "h": 3600, "d": 86400}


@observe(tier="hot", metric="tools.admin_other._parse_since_duration")
def _parse_since_duration(since: str) -> str:
    """Convert a duration string ('24h', '7d', '30m') or ISO datetime to cutoff ISO string.

    Duration strings: <N>(m|h|d) where m=minutes, h=hours, d=days.
    ISO strings are returned as-is after validation.
    Returns an ISO-8601 UTC string.
    """
    m = _DURATION_RE.match(since.strip())
    if m:
        amount = int(m.group(1))
        unit = m.group(2).lower()
        delta = timedelta(seconds=amount * _UNIT_SECONDS[unit])
        return (datetime.now(UTC) - delta).isoformat()
    # Try parsing as ISO datetime
    try:
        dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        return dt.isoformat()
    except ValueError:
        # Fall back to 24h if unparseable
        logger.warning("recent_memories: could not parse since=%r, defaulting to 24h", since)
        return (datetime.now(UTC) - timedelta(hours=24)).isoformat()


@_tool()
def recent_memories(
    limit: int = 10,
    since: str = "24h",
    directory: str = "",
) -> dict:
    """Return recently stored memories, newest first, without classifier dependency.

    Use for quick temporal context ("what did I memorize in the last 24h?") or to
    recover what the session wrote before a context compaction. For semantic search
    use recall(); for fetching a single memory by ID use memory_get().

    Args:
        limit: Max memories to return (default 10, capped at 100).
        since: How far back to look. Duration string ('24h', '7d', '30m') or
               ISO-8601 UTC datetime. Default '24h'.
        directory: Restrict to this project directory. Pass 'global' or omit
                   to search across all directories.

    Returns:
        {
            "memories": [
                {id, created_at, content (≤300 chars), tags, store_type,
                 heat, is_protected, directory_context}
            ],
            "count": <int>,
            "since": <ISO cutoff>,
            "directory": <str>,
        }
    """
    storage = _get_storage()
    effective_limit = min(max(1, limit), 100)
    effective_dir = directory.strip() if directory else ""
    since_iso = _parse_since_duration(since)

    rows = storage.get_recent_memories_since(
        since=since_iso,
        limit=effective_limit,
        directory=effective_dir if effective_dir else None,
    )

    memories = []
    for row in rows:
        content = row.get("content") or ""
        if len(content) > 300:
            content = content[:297] + "..."
        memories.append(
            {
                "id": row.get("id"),
                "created_at": row.get("created_at"),
                "content": content,
                "tags": row.get("tags") or [],
                "store_type": row.get("store_type"),
                "heat": row.get("heat"),
                "is_protected": row.get("is_protected", False),
                "directory_context": row.get("directory_context"),
            }
        )

    return {
        "memories": memories,
        "count": len(memories),
        "since": since_iso,
        "directory": effective_dir or "global",
    }


@observe(tier="stage", metric="tools.admin_other._ms_per_table_stats")
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


@observe(tier="stage", metric="tools.admin_other._ms_histogram_p95")
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


@observe(tier="stage", metric="tools.admin_other._ms_queue_depth")
def _ms_queue_depth() -> int:
    """Return current queue depth from Prometheus gauge (memory_stats helper)."""
    try:
        from yadgar._shared.observability.metrics import yadgar_queue_depth  # noqa: PLC0415

        gauge_samples = list(yadgar_queue_depth.collect()[0].samples)
        for s in gauge_samples:
            if s.labels.get("queue") == "queue":
                return int(s.value)
    except Exception:
        pass
    return 0


@observe(tier="stage", metric="tools.admin_other._ms_circuit_breaker_states")
def _ms_circuit_breaker_states() -> dict[str, int]:
    """Return ML client circuit-breaker states (memory_stats helper)."""
    cb_states: dict[str, int] = {}
    try:
        # Duck-typed: read per-endpoint breaker state without importing the
        # concrete RemoteMLClient (which lives in yadgar.backend and would be a
        # forbidden core→backend edge, folder-split #17). Only RemoteMLClient
        # carries the ``_cb_<ep>`` attributes; any other client leaves cb_states
        # empty — behaviourally identical to the prior isinstance gate.
        _ml = getattr(_st, "_ml_client", None)
        if _ml is not None:
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

    # R3: episodic/semantic counts and causal-edge count are pure storage reads.
    # They were previously gated on _st._cls / _st._causal being non-None, but
    # those engines are backend-only now (None on the core process) while the
    # features still run backend-side and their data still lands in storage —
    # report the DB truth unconditionally.
    stats["episodic_count"] = storage.count_memories_by_store_type("episodic")
    stats["semantic_count"] = storage.count_memories_by_store_type("semantic")
    stats["causal_edges"] = len(storage.get_all_causal_edges())

    if _st._cognitive_map is not None:
        stats["sr_dimensions"] = (
            "active" if _st._cognitive_map.has_sufficient_data() else "insufficient_data"
        )

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
        from yadgar._shared.observability.metrics import (  # noqa: PLC0415
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
    # R3 Car 3b: the rules-engine DB write (insert_rule) forwards to backend /admin.
    # rule_type/scope/action/condition validation runs backend-side inside
    # RulesEngine.add_rule and surfaces as {status: "error", message}. The backend
    # clears ITS rules cache, which is the one the drainer's write-policy
    # enforcement (phase_validate) reads — so the enforcement path stays coherent.
    return _forward_admin(
        "add_rule",
        {
            "rule_type": rule_type,
            "scope": scope,
            "condition": condition,
            "action": action,
            "priority": int(priority),
            "scope_value": scope_value or "",
        },
    )


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
    """Fetch a memory by numeric ID (direct lookup).

    Use when you already have a memory ID (from a prior recall result, error message,
    or recent_memories output). For discovery/search use recall(query, directory);
    for recent temporal context use recent_memories().

    Args:
        memory_id: Integer ID of the memory to fetch.

    Returns:
        Memory dict with all fields (embedding bytes stripped), or None if not found.
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
    """Fetch a wiki page by numeric ID (direct lookup).

    Use when you already have a page_id (from wiki_list, wiki_add, or an error
    message). For slug-based access use wiki_read(slug); for search use
    recall(type="wiki", query=..., directory=...).

    Args:
        page_id: Integer ID of the wiki page to fetch.

    Returns:
        Wiki page dict with all fields (embedding bytes stripped), or None if not found.
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
    # R3 Car 3b: allowed-key validation stays core (raises before any state touch);
    # the DB write (update_memory_fields) forwards to backend /admin. The empty-
    # fields read short-circuit is handled by the backend impl too.
    return _forward_admin("memory_update", {"memory_id": int(memory_id), "fields": fields})


# Server-side de-anchor defaults so callers never hardcode the magic numbers.
_DE_ANCHOR_IMPORTANCE: float = 0.5
_DE_ANCHOR_TIER: str = "ephemeral"


@_tool(power=True)
def de_anchor(memory_id: int) -> dict:
    """Retire an anchor so it re-enters the normal heat-decay path (Car #85).

    De-anchoring undoes what ``anchor()`` / ``memorize(is_protected=True)`` did,
    so a fact that is no longer worth surfacing forever ages out naturally:

      is_protected → False   re-enables decay — the decay query excludes protected
                             rows, so a protected anchor never decays at all.
      importance   → 0.5     re-enables the FAST decay factor — ``compute_decay``
                             uses the slow ``IMPORTANCE_DECAY_FACTOR`` only when
                             importance>0.7, so an anchor left at importance=1.0
                             barely decays even after is_protected is cleared.
      tags         → strip ``_anchor`` and any ``anchor:*`` tag.
      tier         → demoted to ``ephemeral`` (cosmetic — tier does NOT affect
                     decay; ``ephemeral`` is the least-permanent valid tier and
                     the ``memory.tier`` field is ``option<string>`` so it cannot
                     be set to JSON null).

    This is the RETIRE path (gentle: the memory keeps living and decays over
    months). To hard-delete a memory outright, use ``forget(memory_id)`` instead.

    Args:
        memory_id: Integer ID of the memory to de-anchor.

    Returns:
        The updated memory dict on success, or ``{"ok": False, "error": ...}``
        when the memory does not exist.
    """
    mid = int(memory_id)
    mem = _st._storage.get_memory(mid)
    if mem is None:
        return {"ok": False, "error": f"Memory {mid} not found"}

    existing_tags = mem.get("tags") or []
    stripped_tags = [
        t for t in existing_tags if t != "_anchor" and not str(t).startswith("anchor:")
    ]

    fields: dict = {
        "is_protected": False,
        "importance": _DE_ANCHOR_IMPORTANCE,
        "tags": stripped_tags,
        # Demote the anchor tier to the least-permanent valid tier (cosmetic —
        # decay is driven by is_protected + importance, not tier). The schema
        # field is option<string>, so a JSON null is rejected; "ephemeral" is
        # the deterministic demote target.
        "tier": _DE_ANCHOR_TIER,
    }
    # Route through the same forward path as memory_update so the DB write lands
    # backend-side and the fields are filtered by _MEMORY_UPDATABLE_FIELDS.
    return _forward_admin("memory_update", {"memory_id": mid, "fields": fields})


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
    from yadgar._shared.storage.ops import vacuum_checkpoints as _vc  # noqa: PLC0415

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
    # v5.10.2: secret gate — scan content field before any state mutation (STAYS core, I26)
    _content_val = fields.get("content", "") if fields else ""
    _gate = gate_or_reject(_content_val)
    if _gate is not None:
        return _gate

    # R3 Car 3c: allowed-key validation + secret-gate stay core (raise before any
    # state touch); the DB write (update_wiki_page → epoch bump) forwards to the
    # backend /admin op. The empty-fields read short-circuit is handled backend-side.
    return _forward_admin("wiki_update", {"page_id": int(page_id), "fields": fields})
