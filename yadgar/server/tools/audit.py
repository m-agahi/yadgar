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

import json
import logging
import re
from datetime import UTC, datetime

from yadgar.config import get_settings
from yadgar.server._app import _tool
from yadgar.server.lifecycle import _get_storage
from yadgar.server.tools.project import (
    _ANCHOR_PROMOTE_TAGS,
    _cosine_similarity,
    _count_markdown_headers,
    _resolve_project_root,
)

logger = logging.getLogger(__name__)

# ── Slug derivation helpers ───────────────────────────────────────────────

_NONALPHA_RE = re.compile(r"[^a-z0-9]+")


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


def _derive_title(content: str) -> str:
    """Derive a human-readable title from anchor content."""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    # Fallback: first sentence or 80 chars
    first_line = content.splitlines()[0].strip() if content.strip() else content
    return first_line[:80]


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
        except Exception:
            pass
    return access_count * 1_000_000.0 + epoch


# ── Core audit logic ─────────────────────────────────────────────────────


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


def _log_audit_action(storage, directory: str, action: str, payload: dict) -> None:
    """Log an audit mutation to action_log using the existing insert_action_log API."""
    try:
        summary = json.dumps({"source": "audit_anchors", "action": action, **payload})[:500]
        storage.insert_action_log(
            tool_name="audit_anchors",
            tool_input_summary=summary,
            directory=directory,
            session_id="audit",
            timestamp=storage._now_iso(),
        )
    except Exception:
        logger.debug("_log_audit_action failed (non-fatal)", exc_info=True)


# ── Per-directory action builders ────────────────────────────────────────


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


def _apply_forget_expired(storage, resolved: str, action_entry: dict) -> dict | None:
    """Apply a single forget_expired action. Returns applied entry or None on skip/error."""
    mid = action_entry["id"]
    try:
        row = storage._q(
            "SELECT id FROM memory WHERE id = type::record('memory', $id) LIMIT 1",
            {"id": mid},
        )
        if not row:
            return None
        storage.delete_memory(mid)
        _log_audit_action(
            storage,
            resolved,
            "forget_expired",
            {"memory_id": mid, "expired_at": action_entry.get("expired_at", "")},
        )
        return {"action": "forget_expired", "id": mid, "status": "deleted"}
    except Exception:
        logger.debug("forget_expired failed for id=%s", mid, exc_info=True)
        return None


def _apply_merge(storage, resolved: str, action_entry: dict) -> dict | None:
    """Apply a single merge action. Returns applied entry or None on skip/error."""
    forget_id = action_entry.get("forget_id")
    keep_id = action_entry.get("keep_id")
    if forget_id is None:
        return None
    try:
        row = storage._q(
            "SELECT id FROM memory WHERE id = type::record('memory', $id) LIMIT 1",
            {"id": forget_id},
        )
        if not row:
            return None
        storage.delete_memory(forget_id)
        _log_audit_action(
            storage,
            resolved,
            "merge",
            {
                "kept_id": keep_id,
                "forgotten_id": forget_id,
                "similarity": action_entry.get("similarity"),
            },
        )
        return {
            "action": "merge",
            "kept_id": keep_id,
            "forgotten_id": forget_id,
            "status": "merged",
        }
    except Exception:
        logger.debug("merge failed for forget_id=%s", forget_id, exc_info=True)
        return None


def _apply_mutations(storage, resolved: str, actions: list[dict]) -> list[dict]:
    """Apply all non-skipped, non-promote mutations. Returns list of applied entries."""
    applied: list[dict] = []
    _APPLY: dict = {
        "forget_expired": _apply_forget_expired,
        "merge": _apply_merge,
    }
    for action_entry in actions:
        if action_entry.get("skipped"):
            continue
        act = action_entry.get("action")
        fn = _APPLY.get(act)  # type: ignore[arg-type]
        if fn is None:
            continue  # promote and unknown actions not applied
        result = fn(storage, resolved, action_entry)
        if result is not None:
            applied.append(result)
    return applied


# ── Public MCP tool ───────────────────────────────────────────────────────


@_tool(power=True)
def audit_anchors(
    directory: str,
    dry_run: bool = True,
    cosine_threshold: float | None = None,
    include_global: bool = False,
) -> dict:
    """Audit anchors for redundancy, oversize, expiry, and completion.

    Returns:
        {
            "scanned": int,
            "actions": [...],  # forget_expired / merge / promote entries
            "dry_run": bool,
            "applied": [...],  # populated when dry_run=False
            "_truncated": bool,  # True when MAX_ACTIONS_PER_RUN cap hit
        }

    NEVER auto-applies promote_to_wiki (draft only), tier=semantic_immortal,
    or is_protected=True legacy anchors. Idempotent: second call on unchanged
    state returns empty applied list.
    """
    cfg = get_settings()
    storage = _get_storage()
    resolved = _resolve_project_root(directory)
    threshold = (
        cosine_threshold if cosine_threshold is not None else float(cfg.ANCHOR_REDUNDANCY_COSINE)
    )
    max_actions = int(cfg.ANCHOR_AUDIT_MAX_ACTIONS_PER_RUN)

    actions: list[dict] = []
    scanned = 0

    for audit_dir in [resolved] + (["global"] if include_global else []):
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
        actions.extend(_build_promote_actions(storage, audit_dir, _now, cfg))
        actions.extend(_build_merge_actions(storage, audit_dir, _now, threshold))

    truncated = len(actions) > max_actions
    if truncated:
        actions = actions[:max_actions]

    applied = _apply_mutations(storage, resolved, actions) if not dry_run else []

    result: dict = {"scanned": scanned, "actions": actions, "dry_run": dry_run, "applied": applied}
    if truncated:
        result["_truncated"] = True
    return result


# ── Consolidation anchor audit pass ──────────────────────────────────────


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
        if _write_audit_sentinel(storage, directory, audit_result):
            sentinels_written += 1

    return {"directories_audited": dirs_audited, "sentinels_written": sentinels_written}


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


def _audit_dir_safe(directory: str) -> dict | None:
    """Run audit_anchors(dry_run=True) on a directory; return None on error."""
    try:
        return audit_anchors(directory=directory, dry_run=True)
    except Exception:
        logger.debug("_run_anchor_audit_pass: audit failed for dir=%s", directory, exc_info=True)
        return None


def _write_audit_sentinel(storage, directory: str, audit_result: dict) -> bool:
    """Write _audit_anchors sentinel memory for a directory (latest-wins). Returns True on success."""
    import json as _json  # noqa: PLC0415

    try:
        sentinel_content = _json.dumps(
            {
                "actions": audit_result.get("actions", []),
                "scanned": audit_result.get("scanned", 0),
                "_truncated": audit_result.get("_truncated", False),
                "audited_at": storage._now_iso(),
            }
        )
        now = storage._now_iso()
        storage._q(
            "DELETE FROM memory WHERE directory_context = $dir AND '_audit_anchors' INSIDE tags",
            {"dir": directory},
        )
        sid = storage._next_id("memory")
        storage._q(
            "CREATE type::record('memory', $id) SET "
            "content = $content, directory_context = $dir, "
            "tags = $tags, heat = 0.0, is_protected = false, "
            "created_at = $now, last_accessed = $now, access_count = 0",
            {
                "id": sid,
                "content": sentinel_content,
                "dir": directory,
                "tags": ["_audit_anchors", "_system"],
                "now": now,
            },
        )
        return True
    except Exception:
        logger.debug("_write_audit_sentinel: failed for dir=%s", directory, exc_info=True)
        return False
