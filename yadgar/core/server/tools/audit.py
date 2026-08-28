"""Anchor audit MCP tool (v5.9.0).

audit_anchors() performs a hygiene audit of anchors for a directory:
  - forget_expired: valid_until < now AND migration_grace=False AND tier != semantic_immortal
  - merge: cosine-similar pairs; keep higher-rank survivor; forget lower
  - promote: oversized anchors with wiki-worthy tags; returns draft only, never calls wiki_add

dry_run=True (default): returns recommendations only; no DB mutations.
dry_run=False: applies safe mutations (forget_expired, merge); logs to action_log.

NEVER mutates:
  - tier=semantic_immortal rows (regardless of dry_run)
  - is_protected=True rows (preserved for v5.11 repurpose)
  - promote candidates (draft only; wiki page creation is user-gated)
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from yadgar._shared.config import get_settings
from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.lifecycle import _get_storage
from yadgar.core.forward import _forward_admin
from yadgar.core.server._app import _tool
from yadgar.core.server.tools._audit_coverage import _build_coverage
from yadgar.core.server.tools._project_param import accept_project_param
from yadgar.core.server.tools.project import (
    _ANCHOR_PROMOTE_TAGS,
    _cosine_similarity,
    _count_markdown_headers,
    _resolve_project_root,
)

logger = logging.getLogger(__name__)

# ── Slug derivation helpers ───────────────────────────────────────────────

_NONALPHA_RE = re.compile(r"[^a-z0-9]+")


@observe(tier="hot", metric="tools.audit._derive_slug")
def _derive_slug(content: str) -> str:
    """Derive a wiki slug from anchor content.

    Tries the first H1 header; falls back to first 60 chars of content.
    Lowercases, replaces non-alnum runs with '-', strips leading/trailing dashes.
    """
    # Look for first markdown H1
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip()
            if title:
                slug = _NONALPHA_RE.sub("-", title.lower()).strip("-")
                return slug[:80] or "anchor"
    # Fallback: first 60 chars
    slug = _NONALPHA_RE.sub("-", content[:60].lower()).strip("-")
    return slug or "anchor"


@observe(tier="hot", metric="tools.audit._derive_title")
def _derive_title(content: str) -> str:
    """Derive a human-readable title from anchor content."""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    # Fallback: first sentence or 80 chars
    first_line = content.splitlines()[0].strip() if content.strip() else content
    return first_line[:80]


@observe(tier="hot", metric="tools.audit._derive_category")
def _derive_category(tags: list[str]) -> str:
    """Infer wiki category from tag intersection with known category indicators."""
    tag_set = set(tags)
    if "rule" in tag_set or "convention" in tag_set:
        return "convention"
    if "playbook" in tag_set or "workflow" in tag_set or "recipe" in tag_set:
        return "playbook"
    if "pattern" in tag_set:
        return "pattern"
    return "reference"


@observe(tier="hot", metric="tools.audit._build_promote_draft")
def _build_promote_draft(anchor_id: int, content: str, tags: list[str], rationale: str) -> dict:
    """Build promote-to-wiki draft dict for an anchor.

    NEVER calls wiki_add — caller is responsible for creating the wiki page.
    """
    slug = _derive_slug(content)
    title = _derive_title(content)
    category = _derive_category(tags)
    suggested_tags = list({t for t in tags if not t.startswith("_")} | {"_anchor"})
    return {
        "action": "promote",
        "id": anchor_id,
        "draft": {
            "suggested_slug": slug,
            "suggested_title": title,
            "suggested_category": category,
            "suggested_tags": suggested_tags,
            "body": content,
            "rationale": rationale,
        },
        "next_step": (
            f"Call wiki_add(title={title!r}, content=..., tags={suggested_tags!r}, "
            f"category={category!r}) with these values, then forget({anchor_id})."
        ),
    }


# ── Rank helper for merge survivor selection ─────────────────────────────


def _anchor_rank(row: dict) -> float:
    """Compute rank for merge survivor selection.

    Higher rank = kept. Rank = access_count * 1000 + last_accessed_epoch_seconds.
    Deterministic: ties broken by anchor id (higher id = newer = keep).
    """
    access_count = row.get("access_count") or 0
    last_accessed = row.get("last_accessed") or ""
    epoch = 0.0
    if last_accessed:
        try:
            ts = last_accessed.rstrip("Z").replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            epoch = dt.timestamp()
        except (AttributeError, TypeError, ValueError):  # fmt: skip
            pass
    return access_count * 1_000_000.0 + epoch


# ── Core audit logic ─────────────────────────────────────────────────────


@observe(tier="stage", metric="tools.audit._fetch_expired_rows")
def _fetch_expired_rows(storage, directory: str, _now: str) -> list[dict]:
    """Fetch expired anchors: valid_until < now, migration_grace falsy."""
    try:
        rows = storage._q(
            "SELECT id, content, tags, tier, is_protected, valid_until FROM memory "
            "WHERE '_anchor' INSIDE tags "
            "AND directory_context = $dir "
            "AND valid_until IS NOT NONE "
            "AND valid_until < $now "
            "AND (migration_grace IS NONE OR migration_grace = false)",
            {"dir": directory, "now": _now},
        )
        return rows
    except Exception:
        logger.debug("_fetch_expired_rows failed", exc_info=True)
        return []


@observe(tier="stage", metric="tools.audit._fetch_redundant_pairs")
def _fetch_redundant_pairs(
    storage, directory: str, _now: str, threshold: float
) -> list[tuple[dict, dict, float]]:
    """Fetch redundant (similar) anchor pairs above cosine threshold.

    Returns list of (row_a, row_b, similarity) sorted DESC by similarity.
    """
    try:
        emb_rows = storage._q(
            "SELECT id, content, tags, tier, is_protected, "
            "access_count, last_accessed, embedding FROM memory "
            "WHERE '_anchor' INSIDE tags "
            "AND directory_context = $dir "
            "AND (valid_until IS NONE OR valid_until > $now) "
            "AND embedding IS NOT NONE",
            {"dir": directory, "now": _now},
        )
        id_vec: list[tuple[dict, list[float]]] = []
        for row in emb_rows:
            raw_emb = row.get("embedding")
            if raw_emb is None:
                continue
            if isinstance(raw_emb, (bytes, bytearray)):
                floats = storage._bytes_to_floats(raw_emb)
            elif isinstance(raw_emb, list):
                floats = [float(x) for x in raw_emb]
            else:
                continue
            id_vec.append((row, floats))

        pairs: list[tuple[dict, dict, float]] = []
        for i in range(len(id_vec)):
            for j in range(i + 1, len(id_vec)):
                row_a, vec_a = id_vec[i]
                row_b, vec_b = id_vec[j]
                sim = _cosine_similarity(vec_a, vec_b)
                if sim >= threshold:
                    pairs.append((row_a, row_b, round(sim, 4)))
        pairs.sort(key=lambda p: p[2], reverse=True)
        return pairs
    except Exception:
        logger.debug("_fetch_redundant_pairs failed", exc_info=True)
        return []


@observe(tier="stage", metric="tools.audit._fetch_grace_expired_rows")
def _fetch_grace_expired_rows(storage, directory: str, _now: str) -> list[dict]:
    """Fetch migration_grace=True anchors past their valid_until (PD-23).

    These rows are invisible to normal restore/hot queries but persist in DB
    indefinitely, counting toward anchor_count_project. Surface as
    verify_grace_expired_anchor candidates for user review.
    """
    try:
        rows = storage._q(
            "SELECT id, content, tags, tier, valid_until FROM memory "
            "WHERE '_anchor' INSIDE tags "
            "AND directory_context = $dir "
            "AND migration_grace = true "
            "AND valid_until IS NOT NONE "
            "AND valid_until < $now",
            {"dir": directory, "now": _now},
        )
        return rows
    except Exception:
        logger.debug("_fetch_grace_expired_rows failed", exc_info=True)
        return []


@observe(tier="stage", metric="tools.audit._fetch_cross_project_anchor_pool")
def _fetch_cross_project_anchor_pool(storage, _now: str) -> list[tuple[dict, list[float]]]:
    """Fetch all non-expired, non-global anchors with embeddings across all directories.

    Returns list of (row_dict, floats) pairs. Excludes directory_context='global'
    (those are already globally scoped; no dedup benefit).
    """
    try:
        rows = storage._q(
            "SELECT id, content, tags, tier, is_protected, "
            "access_count, last_accessed, created_at, heat, directory_context, embedding "
            "FROM memory "
            "WHERE '_anchor' INSIDE tags "
            "AND directory_context != 'global' "
            "AND directory_context IS NOT NONE "
            "AND (valid_until IS NONE OR valid_until > $now) "
            "AND embedding IS NOT NONE",
            {"now": _now},
        )
        result: list[tuple[dict, list[float]]] = []
        for row in rows:
            raw_emb = row.get("embedding")
            if raw_emb is None:
                continue
            if isinstance(raw_emb, (bytes, bytearray)):
                floats = storage._bytes_to_floats(raw_emb)
            elif isinstance(raw_emb, list):
                floats = [float(x) for x in raw_emb]
            else:
                continue
            result.append((row, floats))
        return result
    except Exception:
        logger.debug("_fetch_cross_project_anchor_pool failed", exc_info=True)
        return []


def _cross_project_content_length_ratio(content_a: str, content_b: str) -> float:
    """Compute content_length_ratio = min(len_a, len_b) / max(len_a, len_b).

    Returns 0.0 when either content is empty to avoid false positives.
    """
    len_a = len(content_a)
    len_b = len(content_b)
    if len_a == 0 or len_b == 0:
        return 0.0
    return min(len_a, len_b) / max(len_a, len_b)


def _cross_project_primary_rank(row: dict) -> float:
    """Rank for cross-project primary selection.

    Higher rank = becomes primary (canonical). Formula: access_count * heat.
    Tie-broken by oldest created_at (preserves original intent).
    """
    access_count = row.get("access_count") or 0
    heat = row.get("heat") or 0.0
    return float(access_count) * float(heat)


@observe(tier="stage", metric="tools.audit._fetch_cross_project_candidates")
def _fetch_cross_project_candidates(
    storage, _now: str, cosine_threshold: float, content_length_ratio_min: float = 0.85
) -> list[dict]:
    """Detect anchor pairs across different directory_context values.

    Returns list of candidate dicts:
        {
            "primary_id": int,
            "duplicate_ids": [int, ...],
            "similarity": float,
            "directory_contexts": [str, ...],
            "recommended_action": "promote_to_global" | "merge_to_primary",
        }

    Detection criteria:
      - cosine >= cosine_threshold (default 0.95)
      - content_length_ratio > content_length_ratio_min (default 0.85)
      - rows from different directory_context values

    NEVER auto-mutates — returns candidates only.
    Grouped: high-similarity cluster → single candidate, primary = highest rank.
    """
    pool = _fetch_cross_project_anchor_pool(storage, _now)
    if len(pool) < 2:
        return []

    # Pairwise: only pairs where directory_context differs
    raw_pairs: list[tuple[dict, dict, float]] = []
    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            row_a, vec_a = pool[i]
            row_b, vec_b = pool[j]
            dir_a = row_a.get("directory_context", "")
            dir_b = row_b.get("directory_context", "")
            if dir_a == dir_b:
                continue
            sim = _cosine_similarity(vec_a, vec_b)
            if sim < cosine_threshold:
                continue
            content_a = row_a.get("content") or ""
            content_b = row_b.get("content") or ""
            ratio = _cross_project_content_length_ratio(content_a, content_b)
            if ratio <= content_length_ratio_min:
                continue
            raw_pairs.append((row_a, row_b, round(sim, 4)))

    if not raw_pairs:
        return []

    # Group overlapping pairs into clusters via union-find
    candidates = _group_cross_project_pairs(storage, raw_pairs)
    return candidates


@observe(tier="stage", metric="tools.audit._group_cross_project_pairs")
def _group_cross_project_pairs(storage, raw_pairs: list[tuple[dict, dict, float]]) -> list[dict]:
    """Group raw pairs into candidate dicts using greedy primary selection.

    Uses seen-set to avoid assigning the same ID to multiple groups.
    Within each group: primary = highest _cross_project_primary_rank;
    tie-broken by oldest created_at (earliest ISO string wins).
    """
    # Build adjacency: id -> {neighbour_id, sim}
    id_to_row: dict[int, dict] = {}
    adj: dict[int, list[tuple[int, float]]] = {}

    for row_a, row_b, sim in raw_pairs:
        mid_a = storage._extract_id(row_a.get("id"))
        mid_b = storage._extract_id(row_b.get("id"))
        id_to_row[mid_a] = row_a
        id_to_row[mid_b] = row_b
        adj.setdefault(mid_a, []).append((mid_b, sim))
        adj.setdefault(mid_b, []).append((mid_a, sim))

    seen: set[int] = set()
    candidates: list[dict] = []

    for mid_seed in sorted(adj.keys()):
        if mid_seed in seen:
            continue
        # BFS to find cluster
        cluster_ids: list[int] = []
        queue = [mid_seed]
        while queue:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            cluster_ids.append(current)
            for neighbour_id, _ in adj.get(current, []):
                if neighbour_id not in seen:
                    queue.append(neighbour_id)

        if len(cluster_ids) < 2:
            continue

        # Select primary: highest rank; tie-break by oldest created_at
        def _sort_key(mid: int) -> tuple:
            row = id_to_row[mid]
            rank = _cross_project_primary_rank(row)
            created_at = str(row.get("created_at") or "")
            return (-rank, created_at)  # negative rank: higher rank sorts first

        cluster_ids_sorted = sorted(cluster_ids, key=_sort_key)
        primary_id = cluster_ids_sorted[0]
        dup_ids = cluster_ids_sorted[1:]

        # Collect max similarity across pairs in cluster
        max_sim = max(
            sim
            for row_a, row_b, sim in raw_pairs
            if storage._extract_id(row_a.get("id")) in set(cluster_ids)
            and storage._extract_id(row_b.get("id")) in set(cluster_ids)
        )

        # Collect directory contexts
        dir_contexts = sorted(
            {str(id_to_row[mid].get("directory_context") or "") for mid in cluster_ids}
        )

        candidates.append(
            {
                "primary_id": primary_id,
                "duplicate_ids": dup_ids,
                "similarity": max_sim,
                "directory_contexts": dir_contexts,
                "recommended_action": "promote_to_global",
            }
        )

    # Sort by similarity desc
    candidates.sort(key=lambda c: c["similarity"], reverse=True)
    return candidates


@observe(tier="stage", metric="tools.audit._fetch_anchored_by_prose_only_archives")
def _fetch_anchored_by_prose_only_archives(storage) -> list[int]:
    """Fetch memory_archive IDs that are at risk from retention purge.

    Criteria (ALL must hold):
      - _anchor tag ABSENT  (no explicit anchor signal in tags)
      - is_protected = false (or absent — schemaless rows default to absent)
      - heat = 0            (or absent — cold, not recently accessed)

    These archives lack any structural anchor signal; their anchor intent is
    encoded only in prose content. The upcoming retention purge (v5.49 Strand A)
    will delete them unless the user intervenes before enabling
    MEMORY_ARCHIVE_RETENTION_DAYS.
    """
    try:
        rows = storage._q(
            "SELECT id FROM memory_archive "
            "WHERE (tags IS NONE OR '_anchor' NOTINSIDE tags) "
            "AND (is_protected IS NONE OR is_protected = false) "
            "AND (heat IS NONE OR heat = 0)"
        )
        return [storage._extract_id(r.get("id")) for r in rows if r.get("id") is not None]
    except Exception:
        logger.debug("_fetch_anchored_by_prose_only_archives failed", exc_info=True)
        return []


@observe(tier="stage", metric="tools.audit._build_anchored_by_prose_only_result")
def _build_anchored_by_prose_only_result(ids: list[int]) -> dict:
    """Build the anchored_by_prose_only audit sub-dict.

    Returns {"count": 0, "sample": []} when no matches.
    Adds "recommended_action" only when count > 0.
    """
    count = len(ids)
    sample = ids[:10]
    result: dict = {"count": count, "sample": sample}
    if count > 0:
        result["recommended_action"] = (
            "review before enabling MEMORY_ARCHIVE_RETENTION_DAYS — these will be purged"
        )
    return result


@observe(tier="stage", metric="tools.audit._fetch_promote_rows")
def _fetch_promote_rows(storage, directory: str, _now: str, cfg) -> list[dict]:
    """Fetch promote candidates: oversized anchors with wiki-worthy tags."""
    try:
        rows = storage._q(
            "SELECT id, content, tags FROM memory "
            "WHERE '_anchor' INSIDE tags "
            "AND directory_context = $dir "
            "AND (valid_until IS NONE OR valid_until > $now)",
            {"dir": directory, "now": _now},
        )
        word_threshold = int(cfg.ANCHOR_PROMOTE_WORDS)
        header_threshold = int(cfg.ANCHOR_PROMOTE_HEADERS)
        result = []
        for row in rows:
            content = row.get("content") or ""
            tags = row.get("tags") or []
            if len(content.split()) <= word_threshold:
                continue
            if _count_markdown_headers(content) < header_threshold:
                continue
            if not (_ANCHOR_PROMOTE_TAGS & set(tags)):
                continue
            result.append(row)
        return result
    except Exception:
        logger.debug("_fetch_promote_rows failed", exc_info=True)
        return []


@observe(tier="hot", metric="tools.audit._is_safe_to_mutate")
def _is_safe_to_mutate(row: dict) -> bool:
    """Return True if this anchor row may be auto-mutated.

    Guards:
      - tier=semantic_immortal → never auto-mutate
      - is_protected=True AND tier IS None/empty → legacy anchor; never auto-mutate
        (pre-v5.8 anchors had no tier; preserved for v5.11 repurpose)
      - anchors with an explicit tier (conditional/ephemeral) may be auto-mutated

    Rationale: all anchors have is_protected=True by design. The guard only
    applies to pre-v5.8 legacy anchors that lack a tier assignment. v5.8+
    anchors with tier=conditional/ephemeral are safe to auto-mutate.
    """
    tier = row.get("tier") or ""
    if tier == "semantic_immortal":
        return False
    # Legacy guard: is_protected=True + no tier → pre-v5.8 anchor
    if row.get("is_protected") and not tier:
        return False
    return True


# ── Per-directory action builders ────────────────────────────────────────


@observe(tier="stage", metric="tools.audit._build_verify_grace_actions")
def _build_verify_grace_actions(storage, audit_dir: str, _now: str) -> list[dict]:
    """Build verify_grace_expired_anchor entries for PD-23 handler.

    Surfaces migration_grace=True rows past valid_until as user-gated review items.
    Always skipped=True — NEVER auto-applied. User must manually verify and act.

    PD-23 deadline: 2026-08-26 (first backfilled pre-v5.8 anchors expire).
    """
    actions: list[dict] = []
    for row in _fetch_grace_expired_rows(storage, audit_dir, _now):
        mid = storage._extract_id(row.get("id"))
        expired_at = row.get("valid_until", "")
        tier = row.get("tier") or ""
        entry: dict = {
            "action": "verify_grace_expired_anchor",
            "id": mid,
            "expired_at": str(expired_at),
            "rationale": (
                f"migration_grace=True anchor past valid_until; tier={tier}. "
                "Verify whether this anchor should be kept (update tier) or forgotten."
            ),
            "skipped": True,
            "skip_reason": "user_verification_required",
        }
        actions.append(entry)
    return actions


@observe(tier="stage", metric="tools.audit._build_expire_actions")
def _build_expire_actions(storage, audit_dir: str, _now: str) -> list[dict]:
    """Build forget_expired action entries for a single directory."""
    actions: list[dict] = []
    for row in _fetch_expired_rows(storage, audit_dir, _now):
        mid = storage._extract_id(row.get("id"))
        tier = row.get("tier") or ""
        expired_at = row.get("valid_until", "")
        safe = _is_safe_to_mutate(row)
        entry: dict = {
            "action": "forget_expired",
            "id": mid,
            "expired_at": str(expired_at),
            "rationale": f"valid_until expired; tier={tier}",
        }
        if not safe:
            entry["skipped"] = True
            entry["skip_reason"] = "semantic_immortal or is_protected"
        actions.append(entry)
    return actions


@observe(tier="stage", metric="tools.audit._build_promote_actions")
def _build_promote_actions(storage, audit_dir: str, _now: str, cfg) -> list[dict]:
    """Build promote draft entries for a single directory."""
    actions: list[dict] = []
    for row in _fetch_promote_rows(storage, audit_dir, _now, cfg):
        mid = storage._extract_id(row.get("id"))
        content = row.get("content") or ""
        tags = row.get("tags") or []
        wc = len(content.split())
        hc = _count_markdown_headers(content)
        rationale = f"{wc} words + {hc} headers + tag intersect {_ANCHOR_PROMOTE_TAGS & set(tags)}"
        actions.append(_build_promote_draft(mid, content, tags, rationale))
    return actions


@observe(tier="stage", metric="tools.audit._build_merge_actions")
def _build_merge_actions(storage, audit_dir: str, _now: str, threshold: float) -> list[dict]:
    """Build merge action entries for a single directory."""
    actions: list[dict] = []
    seen: set[int] = set()
    for row_a, row_b, sim in _fetch_redundant_pairs(storage, audit_dir, _now, threshold):
        mid_a = storage._extract_id(row_a.get("id"))
        mid_b = storage._extract_id(row_b.get("id"))
        if mid_a in seen or mid_b in seen:
            continue
        if _anchor_rank(row_a) >= _anchor_rank(row_b):
            keep_id, forget_id, forget_row = mid_a, mid_b, row_b
        else:
            keep_id, forget_id, forget_row = mid_b, mid_a, row_a
        entry: dict = {
            "action": "merge",
            "ids": [mid_a, mid_b],
            "keep_id": keep_id,
            "forget_id": forget_id,
            "similarity": sim,
            "rationale": f"cosine={sim} >= threshold={threshold}",
        }
        if not _is_safe_to_mutate(forget_row):
            entry["skipped"] = True
            entry["skip_reason"] = "forget_id is semantic_immortal or is_protected"
        actions.append(entry)
        seen.add(mid_a)
        seen.add(mid_b)
    return actions


# ── Mutation application ──────────────────────────────────────────────────
# R3 Car 3d: the apply/write half (forget_expired + merge DELETEs, action_log,
# epoch bump) moved to yadgar.backend.admin_exec.audit. The core dry-run BUILD
# path (all _fetch_* / _build_* readers) stays here — reads are allowed in core,
# and project_brief's signals path shares _fetch_cross_project_candidates. The
# core audit_anchors shell forwards the built actions via _forward_admin below.


@observe(tier="stage", metric="tools.audit._apply_mutations")
def _apply_mutations(resolved: str, actions: list[dict]) -> list[dict]:
    """Forward the (core-built) audit mutations to the backend /admin op.

    The dry-run BUILD ran core-side (reads); the DELETEs + action_log + epoch
    bump run in the backend. Returns the applied list the backend produced.
    """
    result = _forward_admin("audit_apply_mutations", {"resolved": resolved, "actions": actions})
    return result.get("applied", [])


# ── Public MCP tool ───────────────────────────────────────────────────────


@_tool(power=True)
def audit_anchors(
    directory: str,
    dry_run: bool = True,
    cosine_threshold: float | None = None,
    include_global: bool = False,
    *,
    project: str | None = None,
) -> dict:
    """Audit anchors for redundancy, oversize, expiry, and completion.

    Returns:
        {
            "scanned": int,
            "coverage": {...},  # task 391 — see below; always present
            "actions": [...],  # forget_expired / merge / promote / verify_grace_expired_anchor
            "dry_run": bool,
            "applied": [...],  # populated when dry_run=False
            "cross_project_redundancy_candidates": [...],  # always present (may be empty)
            "_truncated": bool,  # True when MAX_ACTIONS_PER_RUN cap hit
        }

    ``scanned`` counts only rows matching the scan's own selector
    (``'_anchor' INSIDE tags AND directory_context = $dir``), which is
    narrower than "this project's protected rows". ``coverage`` states the
    shortfall instead of leaving it silent::

        {
            "scanned": 95,              # the number above, unchanged
            "scanned_protected": 95,    # of those, how many are is_protected
            "protected_total": 102,     # protected rows owned by either key
            "unscanned": 7,
            "unscanned_reasons": {"no_anchor_tag": 6,
                                  "directory_context_mismatch": 1},
            "unscanned_sample": {...},  # up to 10 ids per reason
            "scope_keys": {"directory_context": [...], "project_id": "..."},
        }

    No tier is excluded from the scan — ``semantic_immortal`` rows ARE
    scanned; the tier guard governs mutation (``_is_safe_to_mutate``), not
    visibility. ``coverage`` is a REPORT: it never enlarges the population
    the action builders run over.

    NEVER auto-applies:
      - promote_to_wiki (draft only)
      - tier=semantic_immortal rows
      - is_protected=True legacy rows
      - verify_grace_expired_anchor rows (user-gated, always skipped=True)
      - cross_project_redundancy_candidates (surfaced only, never mutated)

    Idempotent: second call on unchanged state returns empty applied list.
    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; C7 re-keys
    # this tool's scope from ``directory`` onto the resolved project_id.
    accepted_project = accept_project_param(project, directory)
    cfg = get_settings()
    storage = _get_storage()
    resolved = _resolve_project_root(directory)
    threshold = (
        cosine_threshold if cosine_threshold is not None else float(cfg.ANCHOR_REDUNDANCY_COSINE)
    )
    max_actions = int(cfg.ANCHOR_AUDIT_MAX_ACTIONS_PER_RUN)

    actions: list[dict] = []
    scanned = 0
    audit_dirs = [resolved] + (["global"] if include_global else [])

    for audit_dir in audit_dirs:
        _now = storage._now_iso()
        try:
            cnt_rows = storage._q(
                "SELECT count() AS cnt FROM memory "
                "WHERE '_anchor' INSIDE tags AND directory_context = $dir GROUP ALL",
                {"dir": audit_dir},
            )
            scanned += int(cnt_rows[0]["cnt"]) if cnt_rows else 0
        except Exception:
            pass
        actions.extend(_build_expire_actions(storage, audit_dir, _now))
        actions.extend(_build_verify_grace_actions(storage, audit_dir, _now))
        actions.extend(_build_promote_actions(storage, audit_dir, _now, cfg))
        actions.extend(_build_merge_actions(storage, audit_dir, _now, threshold))

    truncated = len(actions) > max_actions
    if truncated:
        actions = actions[:max_actions]

    applied = _apply_mutations(resolved, actions) if not dry_run else []

    # Cross-project candidates — computed globally (not per-directory).
    # Always present in result; empty list when no candidates.
    # NEVER auto-mutated regardless of dry_run.
    cross_threshold = float(cfg.ANCHOR_CROSS_PROJECT_COSINE)
    _now_global = storage._now_iso()
    cross_candidates = _fetch_cross_project_candidates(storage, _now_global, cross_threshold)

    # Phase 0 (v5.49) — prose-only archives at risk from upcoming retention purge.
    # Computed globally: memory_archive has no directory_context column.
    prose_only_ids = _fetch_anchored_by_prose_only_archives(storage)
    anchored_by_prose_only = _build_anchored_by_prose_only_result(prose_only_ids)

    result: dict = {
        "scanned": scanned,
        "coverage": _build_coverage(storage, audit_dirs, scanned, accepted_project),
        "actions": actions,
        "dry_run": dry_run,
        "applied": applied,
        "cross_project_redundancy_candidates": cross_candidates,
        "anchored_by_prose_only": anchored_by_prose_only,
    }
    if truncated:
        result["_truncated"] = True

    return result


# ── Consolidation anchor audit pass ──────────────────────────────────────


@observe(tier="stage", metric="tools.audit._run_anchor_audit_pass")
def _run_anchor_audit_pass(storage) -> dict:
    """Run dry-run anchor audit per directory; write _audit_anchors sentinel.

    Called from consolidate_now() when ANCHOR_AUDIT_CONSOLIDATION_ENABLED=true.
    Latest-wins: deletes existing sentinel before inserting new one.
    Skips directories with anchor_count < ANCHOR_AUDIT_THRESHOLD.

    Returns {directories_audited: N, sentinels_written: N}.
    """
    cfg = get_settings()
    threshold = int(cfg.ANCHOR_AUDIT_THRESHOLD)
    dirs_audited = 0
    sentinels_written = 0

    try:
        dir_rows = storage._q(
            "SELECT directory_context FROM memory "
            "WHERE '_anchor' INSIDE tags "
            "AND directory_context IS NOT NONE "
            "AND directory_context != '' "
            "GROUP BY directory_context"
        )
        directories = list(
            {r.get("directory_context") for r in dir_rows if r.get("directory_context")}
        )
    except Exception:
        logger.debug("_run_anchor_audit_pass: could not list directories", exc_info=True)
        return {"directories_audited": 0, "sentinels_written": 0}

    for directory in directories:
        anchor_count = _count_anchors_for_dir(storage, directory)
        if anchor_count < threshold:
            continue
        audit_result = _audit_dir_safe(directory)
        if audit_result is None:
            continue
        dirs_audited += 1
        if _write_audit_sentinel(directory, audit_result):
            sentinels_written += 1

    return {"directories_audited": dirs_audited, "sentinels_written": sentinels_written}


@observe(tier="stage", metric="tools.audit._count_anchors_for_dir")
def _count_anchors_for_dir(storage, directory: str) -> int:
    """Return anchor count for a directory (valid, non-expired)."""
    try:
        _now = storage._now_iso()
        cnt_rows = storage._q(
            "SELECT count() AS cnt FROM memory "
            "WHERE '_anchor' INSIDE tags "
            "AND directory_context = $dir "
            "AND (valid_until IS NONE OR valid_until > $now) "
            "GROUP ALL",
            {"dir": directory, "now": _now},
        )
        return int(cnt_rows[0]["cnt"]) if cnt_rows else 0
    except Exception:
        return 0


@observe(tier="stage", metric="tools.audit._audit_dir_safe")
def _audit_dir_safe(directory: str) -> dict | None:
    """Run audit_anchors(dry_run=True) on a directory; return None on error."""
    try:
        return audit_anchors(directory=directory, dry_run=True)
    except Exception:
        logger.debug("_run_anchor_audit_pass: audit failed for dir=%s", directory, exc_info=True)
        return None


@observe(tier="stage", metric="tools.audit._write_audit_sentinel")
def _write_audit_sentinel(directory: str, audit_result: dict) -> bool:
    """Write _audit_anchors sentinel memory for a directory (latest-wins). Returns True on success.

    R3 Car 3d: the sentinel CREATE/DELETE is a DB write — forwarded to the backend
    /admin op (write_audit_sentinel). No epoch bump (system marker, matches prior
    behaviour). Any transport error degrades to False (non-fatal for the audit pass).
    """
    try:
        result = _forward_admin(
            "write_audit_sentinel",
            {"directory": directory, "audit_result": audit_result},
        )
        return bool(result.get("written", False))
    except Exception:
        logger.debug("_write_audit_sentinel: forward failed for dir=%s", directory, exc_info=True)
        return False
