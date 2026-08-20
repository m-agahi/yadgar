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
from yadgar._shared.wiki.prompt_guard import removed_prompt_lines
from yadgar._shared.wiki.wiki_meta import PAGE_TYPE_AGENT_DISCIPLINE
from yadgar.core.forward import _forward_admin
from yadgar.core.server._app import _tool
from yadgar.core.server._helpers import _q_with_timeout
from yadgar.core.server.tools._project_param import accept_project_param, project_id_value_error

logger = logging.getLogger(__name__)

settings = get_settings()


# ── Allowed update fields ──────────────────────────────────────────────

# Allowed fields for MCP-level memory_update (subset of _MEMORY_UPDATABLE_FIELDS).
# v5.158 (Car #85): importance + tier widened in so de_anchor can retire an anchor
# into the normal decay path (clearing is_protected alone is insufficient — the
# decay RATE is keyed on importance: thermodynamics.py compute_decay uses the slow
# IMPORTANCE_DECAY_FACTOR when importance>0.7, so an anchor left at importance=1.0
# barely decays even after is_protected is cleared).
# Task 262: "project_id" ADDED — the SOLE memory scoping key, and this list was the ONLY
# gate on it (storage accepts it per Car L; the backend op forwards **fields unvalidated),
# so a mis-stamped row had no restamp path at all. Memory half of ledger task 246.
_MEMORY_UPDATE_ALLOWED: frozenset[str] = frozenset(
    {"content", "tags", "is_protected", "is_stale", "importance", "tier", "project_id"}
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
    *,
    project: str | None = None,
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
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; C7 re-keys
    # this tool's scope from ``directory`` onto the resolved project_id.
    accept_project_param(project, directory)
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
    scope: "global", "project", or "path".
    condition: e.g. "importance > 0.7", "tag contains architecture".
    action: "filter" for hard rules, "boost:0.3" or "penalty:0.2" for soft rules.
    priority: Higher = applied first (default 0).
    scope_value: project_id for scope="project" (matched by EXACT equality); a
        glob over a real filesystem path for scope="path"; unused for "global".

    C10 (0047 §5(a)): the retired kinds "directory" and "file" are REJECTED
    backend-side with a message naming their replacement. A "directory" rule
    carried a filesystem path in scope_value and could never match a project_id,
    so accepting it would mint a rule that is dead on arrival.
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
def get_rules(directory: str = "", *, project: str | None = None) -> list[dict]:
    """Get active rules. If a project is named, returns only applicable rules.

    C10 (0047 §5(a)): ``get_applicable_rules`` is now keyed on the **project_id**
    (exact equality), not a directory prefix. The resolved ``project`` override
    is preferred when supplied; ``directory`` remains accepted as the legacy
    positional so existing callers keep working until C11 re-keys this tool's
    signature along with the rest of the scoped tool surface.
    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary.
    resolved = accept_project_param(project, directory)
    if _st._rules_engine is None:
        return []
    scope_key = resolved or directory
    if scope_key:
        return _st._rules_engine.get_applicable_rules(scope_key)
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

    Allowed keys: ``content``, ``tags``, ``is_protected``, ``is_stale``,
    ``importance``, ``tier``, ``project_id``. ``_MEMORY_UPDATE_ALLOWED`` is
    authoritative and the rejection message renders it, so a caller need not
    trust this list — it was stale from v5.158 until ledger task 262.
    Rejected: ``heat``, ``embedding``, ``id``, ``created_at``, unknown keys.

    ``project_id`` (task 262) is the SOLE memory scoping key and this is the
    only MCP path that restamps it. Its VALUE is shape-validated — and NOT
    registry-checked — by ``project_id_value_error``, which states why.
    Raises ValueError on a disallowed key or a project_id naming no project.
    """
    unknown = set(fields) - _MEMORY_UPDATE_ALLOWED
    if unknown:
        raise ValueError(
            f"Disallowed field(s) for memory_update: {sorted(unknown)}. "
            f"Allowed: {sorted(_MEMORY_UPDATE_ALLOWED)}"
        )
    if "project_id" in fields and (err := project_id_value_error(fields["project_id"])):
        raise ValueError(err)
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


# Fallback tier when a renewal names neither ttl_days nor tier AND the row itself
# carries no usable tier. NEVER "semantic_immortal": immortality must be asked for
# explicitly, never granted by omission (see anchor_renew's docstring).
_RENEW_DEFAULT_TIER: str = "conditional"


@observe(tier="stage", metric="tools.admin_other._validate_anchor_renew_args")
def _validate_anchor_renew_args(
    ttl_days: int | None,
    tier: str | None,
    reason: str,
) -> dict | None:
    """Validate ``anchor_renew``'s arguments in isolation (no DB access).

    Returns the error dict to hand straight back to the caller, or ``None`` when
    the arguments are acceptable.  Split from the row-resolution half purely to
    keep both under the I30 complexity cap.
    """
    from yadgar.core.server.tools.misc import _VALID_ANCHOR_TIERS

    if not reason or not reason.strip():
        return {
            "ok": False,
            "error": (
                "anchor_renew requires a non-empty 'reason' argument explaining why "
                "this anchor still deserves a compaction-proof slot"
            ),
        }

    _gate = gate_or_reject(reason, tags=["_anchor"])
    if _gate is not None:
        return _gate

    if tier is not None and tier not in _VALID_ANCHOR_TIERS:
        return {
            "ok": False,
            "error": f"invalid tier: {tier!r}. Must be one of {sorted(_VALID_ANCHOR_TIERS)}",
        }

    if ttl_days is not None and tier == "semantic_immortal":
        return {
            "ok": False,
            "error": (
                "conflict: ttl_days sets a finite expiry but tier='semantic_immortal' "
                "means never expires — choose one"
            ),
        }

    if ttl_days is not None and int(ttl_days) <= 0:
        return {"ok": False, "error": f"ttl_days must be positive, got {ttl_days!r}"}

    return None


@observe(tier="stage", metric="tools.admin_other._resolve_anchor_renew_target")
def _resolve_anchor_renew_target(
    memory_id: int,
    ttl_days: int | None,
    tier: str | None,
) -> tuple[dict | None, str, str | None, dict | None]:
    """Fetch the target row, confirm it is an anchor, and resolve the new expiry.

    Mirrors ``misc._validate_anchor_inputs``' shape: returns
    ``(memory_row, effective_tier, new_valid_until, error_dict_or_None)``.
    """
    from yadgar.core.server.tools.misc import _VALID_ANCHOR_TIERS

    mid = int(memory_id)
    mem = _get_storage().get_memory(mid)
    if mem is None:
        return None, "", None, {"ok": False, "error": f"Memory {mid} not found"}

    # Anchor-ness is keyed on the TAG, not is_protected: the corpus holds many
    # is_protected rows without the tag (_active_work and friends), and both
    # surfacing queries require the tag.
    if "_anchor" not in list(mem.get("tags") or []):
        return (
            None,
            "",
            None,
            {
                "ok": False,
                "error": (
                    f"Memory {mid} is not an anchor (no '_anchor' tag), so it has no "
                    "time-box to renew. Use anchor() to create one — anchor_renew never "
                    "promotes."
                ),
            },
        )

    # Effective tier: explicit → the row's own tier → conditional. Never immortal
    # by omission.
    _row_tier = mem.get("tier")
    if tier is not None:
        effective_tier = tier
    elif _row_tier in _VALID_ANCHOR_TIERS:
        effective_tier = str(_row_tier)
    else:
        effective_tier = _RENEW_DEFAULT_TIER

    # ttl_days vs. an EFFECTIVE semantic_immortal tier is the same conflict
    # _validate_anchor_renew_args already rejects for the explicit `tier` argument
    # (admin_other.py:626-633) — but that check only ever sees the raw argument.
    # When `tier` is omitted, effective_tier falls back to the ROW's own stored
    # tier (just above); if that stored tier is semantic_immortal, ttl_days must
    # be rejected here too. Otherwise _compute_valid_until's ttl_days-wins
    # resolution order (server_helpers.py:137-140 — correct and depended-on by
    # memorize()/anchor(), NOT to be reordered) writes a finite valid_until
    # alongside tier="semantic_immortal": the exact zombie shape this tool exists
    # to repair, freshly manufactured. Demoting the tier silently instead of
    # rejecting would be the same class of unasked-for persistence change this
    # function already refuses in the non-anchor-promotion check above.
    if ttl_days is not None and effective_tier == "semantic_immortal":
        return (
            None,
            "",
            None,
            {
                "ok": False,
                "error": (
                    "conflict: ttl_days sets a finite expiry but the effective tier "
                    "is 'semantic_immortal' (resolved from the row's own stored tier) "
                    "— pass tier='conditional' or tier='ephemeral' to demote and renew "
                    "with ttl_days, or pass tier='semantic_immortal' explicitly to keep "
                    "it immortal (this still clears migration_grace)"
                ),
            },
        )

    from yadgar._shared.server_helpers import _compute_valid_until

    try:
        # valid_until=None: this tool takes ttl_days/tier only, so the explicit-
        # timestamp branch never applies. ttl_days wins over the tier default,
        # matching _compute_valid_until's documented resolution order.
        new_valid_until = _compute_valid_until(effective_tier, None, ttl_days, settings)
    except ValueError as exc:
        return None, "", None, {"ok": False, "error": str(exc)}

    return mem, effective_tier, new_valid_until, None


@_tool(power=True)
def anchor_renew(
    memory_id: int,
    ttl_days: int | None = None,
    tier: str | None = None,
    reason: str = "",
) -> dict:
    """Renew an anchor's time-box so it keeps surfacing past its ``valid_until``.

    The gap this closes: every anchor surfacing query filters
    ``valid_until IS NONE OR valid_until > now``, so at its expiry instant an anchor
    silently stops surfacing.  Nothing deletes it and — for ``migration_grace`` rows —
    no signal fires either (``project.py`` excludes grace rows by design, ADR-0083).
    The row becomes an invisible, undeleted zombie.  Until this tool there was no
    sanctioned way back: ``memory_update``'s allowlist rejects ``valid_until`` and
    ``migration_grace``, and ``db_inspect`` is read-only.

    This is deliberately a DEDICATED tool rather than a widening of
    ``_MEMORY_UPDATE_ALLOWED``.  That allowlist is a safety boundary (it rejects
    ``heat``, ``embedding``, ``id``, ``created_at``); adding expiry fields to it would
    weaken the guarantee for every ``memory_update`` caller to serve one workflow.

    Semantics:
      * ``ttl_days``  → new expiry at ``now + ttl_days``.
      * ``tier``      → new expiry from that tier's default TTL
                        (``conditional`` 90d, ``ephemeral`` 14d), or NO expiry for
                        ``semantic_immortal``.
      * Neither given → falls back to the ROW's existing tier, then to
        ``conditional``.  A NORMAL row can only become immortal by naming
        ``tier="semantic_immortal"`` explicitly — never by omission, which would
        otherwise turn a bare ``anchor_renew(id, reason=...)`` on a normal row into
        the opposite of this tool's job.  A row that is ALREADY stored
        ``semantic_immortal`` correctly stays immortal on a bare renew — that is
        inheriting the row's own tier, the same fallback ``conditional``/
        ``ephemeral`` rows get, not "granting immortality by omission".
      * ``ttl_days`` conflicts with an effective ``semantic_immortal`` tier
        whether that tier came from the explicit ``tier`` argument OR was resolved
        from the row's own stored tier — REJECTED either way with an error naming
        the conflict.  A finite TTL and "never expires" written to the same row in
        the same call is the one outcome this tool must never produce silently;
        demote first with ``tier="conditional"``/``"ephemeral"`` to renew with
        ``ttl_days``, or keep immortality with an explicit
        ``tier="semantic_immortal"`` (which still clears ``migration_grace``).
      * ``migration_grace`` is ALWAYS cleared.  That flag is what makes an expired row
        invisible-but-undeleted; renewing without clearing it just moves the cliff.
      * ``reason`` is REQUIRED and is recorded as an ``anchor:<reason>`` tag, the same
        way ``anchor()`` does.  Renewing re-asserts that something deserves a
        compaction-proof slot, and an unreasoned anchor set is how the corpus filled
        with junk.

    Anchor-ness is keyed on the ``_anchor`` TAG, not ``is_protected`` — the corpus
    holds many ``is_protected`` rows without the tag (``_active_work`` and friends),
    and both surfacing queries require the tag.

    Args:
        memory_id: Integer ID of the anchor to renew.
        ttl_days: Renew for this many days from now. Mutually exclusive with an
            effective ``semantic_immortal`` tier — rejected whether that tier is
            passed explicitly via ``tier`` or resolved from the row's own stored
            tier.
        tier: One of ``semantic_immortal`` | ``conditional`` | ``ephemeral``.
        reason: REQUIRED, non-empty. Why this anchor still deserves its slot.

    Returns:
        ``{ok, memory_id, tier, valid_until, reason, migration_grace_cleared, memory}``
        — ``valid_until`` is the RESOLVED new expiry (``None`` = never expires), so the
        caller can see the new cliff. Returns ``{"ok": False, "error": ...}`` on a
        missing memory, a non-anchor, a missing reason, or an invalid/conflicting tier.
    """
    _arg_err = _validate_anchor_renew_args(ttl_days, tier, reason)
    if _arg_err is not None:
        return _arg_err

    mem, effective_tier, new_valid_until, _err = _resolve_anchor_renew_target(
        memory_id, ttl_days, tier
    )
    if _err is not None:
        return _err
    if mem is None:  # unreachable: _err is None ⇒ the row resolved. Guard, not assert.
        return {"ok": False, "error": f"Memory {int(memory_id)} could not be resolved"}

    mid = int(memory_id)
    new_tags = list(mem.get("tags") or [])
    if f"anchor:{reason}" not in new_tags:
        new_tags.append(f"anchor:{reason}")

    fields: dict = {
        # The zombie-maker. Always cleared — a renewed row must never sit in grace.
        "migration_grace": False,
        "tags": new_tags,
        "tier": effective_tier,
    }
    if new_valid_until is not None:
        fields["valid_until"] = new_valid_until

    updated = _forward_admin(
        "anchor_renew",
        {
            "memory_id": mid,
            "fields": fields,
            # option<string> cannot be cleared by a JSON null; the backend half
            # issues the bare NONE literal instead.
            "clear_valid_until": new_valid_until is None,
        },
    )

    # Car 6 (bug-train 2026-08-13): the result was previously discarded here —
    # this function unconditionally returned ok:True regardless of what the
    # backend reported. A success envelope carries no "ok" key at all (KEY
    # INVARIANT — see backend/admin_exec/ledger.py), so this only ever fires
    # on an explicit rejection. NOTE: as of this fix the concrete backend body
    # (backend/admin_exec/memory.py's anchor_renew) raises ValueError rather
    # than returning ok:False on a not-found row, and the /admin route does
    # not convert that raise into an ok:False envelope (only KeyError is
    # caught there) — so that raise propagates as an httpx error instead of
    # reaching this check. This guard is therefore contract hygiene against
    # the general _forward_admin contract (any admin op MAY return
    # ok:False), not a fix for a presently-reachable path.
    if isinstance(updated, dict) and updated.get("ok") is False:
        return updated

    return {
        "ok": True,
        "memory_id": mid,
        "tier": effective_tier,
        "valid_until": new_valid_until,
        "reason": reason,
        "migration_grace_cleared": True,
        "memory": updated,
    }


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


@observe(tier="stage", metric="tools.wiki_update._reject_discipline_content_removal")
def _reject_discipline_content_removal(page_id: int, fields: dict) -> dict | None:
    """ADR-0208 guard for wiki_update's content patch (task 23).

    Mirrors ``WikiStore._reject_if_discipline_weakening`` using the same shared
    ``removed_prompt_lines`` primitive; it lives here rather than in the store
    because wiki_update's backend op bypasses ``WikiStore`` entirely.

    Returns an error dict when the patch would remove rule lines from an
    ``agent_discipline`` page, else None. A patch that does not touch
    ``content`` cannot remove a rule and is never gated.
    """
    if "content" not in fields:
        return None
    try:
        page = _get_storage().get_wiki_page(page_id)
    except Exception as e:  # noqa: BLE001 — a read failure must not block the write path
        logger.debug("wiki_update discipline guard: page read failed: %s", e)
        return None
    if page is None or page.get("page_type") != PAGE_TYPE_AGENT_DISCIPLINE:
        return None
    removed = removed_prompt_lines(page.get("content", ""), fields["content"])
    if not removed:
        return None
    return {
        "ok": False,
        "error": "discipline_removal_requires_confirmation",
        "page_id": page_id,
        "slug": page.get("slug"),
        "removed_lines": removed,
        "message": (
            f"{len(removed)} rule line(s) would be removed from discipline page "
            f"{page.get('slug')!r}. Generic wiki edits may only ADD to a discipline "
            "(ADR-0208). Use discipline_save(name, content, confirm_removal=True) "
            "to ratify a removal:\n" + "\n".join(f"  - {ln}" for ln in removed)
        ),
    }


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

    # Task 23 / ADR-0208: wiki_update is the ONE edit path that never enters
    # WikiStore — the backend op calls storage.update_wiki_page directly, so the
    # store-level discipline guard cannot see it, and `content` is an allowed key
    # here. Pre-check core-side instead (reads are allowed core-side). This shell
    # is a disjoint entry point from discipline_save (which goes
    # _save_discipline_page -> _forward_admin("agent_prompt_save") -> wiki.add),
    # so the sanctioned write path is not double-gated.
    _rejected = _reject_discipline_content_removal(int(page_id), fields)
    if _rejected is not None:
        return _rejected

    # R3 Car 3c: allowed-key validation + secret-gate stay core (raise before any
    # state touch); the DB write (update_wiki_page → epoch bump) forwards to the
    # backend /admin op. The empty-fields read short-circuit is handled backend-side.
    return _forward_admin("wiki_update", {"page_id": int(page_id), "fields": fields})
