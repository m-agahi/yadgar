"""Agent-prompt library MCP tools — v5.85 S4/S5 rework.

Tool:
  agent_prompt_save(pattern, content) — upserts one page per pattern

Storage convention (v5.85 rework):
  - Slug pattern: agent-prompt-<task-pattern>  (deterministic, no -vN suffix)
  - Tags: ["agent-prompt", "task:<pattern>"]
  - Category: "reference"
  - page_type: "agent_prompt"
  - wiki versioning (wiki_page_version table) carries history.

Retrieval (S4/S5 collapse):
  - Semantic lookup is now `recall(type="wiki", tags=["agent-prompt"])`
    (the bespoke agent_prompt_search tool was removed; the SQL pre-filter lives
    in WikiStore.query via search_wiki_vectors_tagged).
  - Exact-key lookup is the internal helper `_read_agent_prompt(slug, storage)`
    (the bespoke agent_prompt_get tool was removed; dispatch_helper uses this).
"""

from __future__ import annotations

import logging
import re

from yadgar.secrets import gate_or_reject
from yadgar.server._app import _tool
from yadgar.wiki import WikiAddOptions

logger = logging.getLogger(__name__)

# ── S6 discovery surface ──────────────────────────────────────────────────────
# Global TOC page: title "Agent Prompt TOC" → slug "agent-prompt-toc" (_slugify).
_TOC_TITLE = "Agent Prompt TOC"
_TOC_SLUG = "agent-prompt-toc"
# One row per pattern: `- `<pattern>` → <purpose>`. Regex pins the pattern column
# so re-save scan-replaces the existing line (idempotent upsert, no dupes).
_TOC_ROW_RE = re.compile(r"^- `(?P<pattern>[^`]+)` → .*$", re.MULTILINE)
# Reason tag that identifies the single library-discovery anchor (create-if-absent).
_LIBRARY_ANCHOR_REASON = "agent-prompt-library"
_LIBRARY_ANCHOR_CONTENT = (
    "Agent-prompt library: see wiki [[agent-prompt-toc]] for available prompts; "
    "recall(type='wiki', tags=['agent-prompt']) to search; "
    "agent_prompt_save to add."
)


def _toc_row(pattern: str, purpose: str) -> str:
    return f"- `{pattern}` → {purpose}"


def _toc_with_row(body: str, pattern: str, new_row: str) -> str:
    """Return TOC body with `pattern`'s row upserted (replace if present, else append)."""
    found = any(m.group("pattern") == pattern for m in _TOC_ROW_RE.finditer(body))
    if found:
        return _TOC_ROW_RE.sub(
            lambda m: new_row if m.group("pattern") == pattern else m.group(0), body
        )
    return body.rstrip() + "\n" + new_row + "\n"


def _upsert_toc_row(pattern: str, purpose: str, branch_hint: str | None) -> None:
    """Scan-replace-or-add the `pattern → purpose` row in the global TOC page.

    Idempotent: a second save for the same pattern replaces its row (no dupes).
    Best-effort — any failure is logged, never raised (TOC is a discovery aid,
    not a write barrier on the prompt save itself).
    """
    import yadgar.server._state as _st_mod  # noqa: PLC0415

    wiki = _st_mod._wiki
    if wiki is None:
        return
    try:
        existing = _st_mod._storage.get_wiki_page_by_slug(_TOC_SLUG)
        new_row = _toc_row(pattern, purpose)
        if existing and existing.get("content"):
            content = _toc_with_row(existing["content"], pattern, new_row)
        else:
            content = (
                "# Agent Prompt TOC\n\n"
                "Reusable subagent dispatch prompts. "
                "recall(type='wiki', tags=['agent-prompt']) to pull one.\n\n"
                f"{new_row}\n"
            )
        wiki.add(
            title=_TOC_TITLE,
            content=content,
            category="reference",
            tags=["agent-prompt-toc"],
            opts=WikiAddOptions(
                source_memory_ids=[],
                confidence="high",
                branch=branch_hint,
                directory_context="global",
            ),
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("agent_prompt_save: TOC upsert failed: %s", e)


def _ensure_library_anchor(branch_hint: str | None) -> None:
    """Create the global discovery anchor pointing at the TOC, if absent.

    Create-if-absent (keyed by reason tag) so repeat saves don't spam anchors.
    Best-effort — failures are logged, never raised. NOT gated by
    AGENT_PROMPT_LIBRARY_ENABLED (the anchor is a always-on signpost; only the
    three discovery surfaces are gated).
    """
    try:
        import yadgar.server._state as _st_mod  # noqa: PLC0415

        storage = _st_mod._storage
        if storage is None:
            return
        existing = storage._q(
            "SELECT id FROM memory WHERE '_anchor' INSIDE tags AND $reason INSIDE tags LIMIT 1",
            {"reason": f"anchor:{_LIBRARY_ANCHOR_REASON}"},
        )
        if existing:
            return
        from yadgar.server.lifecycle import _get_replay  # noqa: PLC0415

        replay = _get_replay()
        replay.anchor_memory(
            _LIBRARY_ANCHOR_CONTENT,
            "global",
            [f"anchor:{_LIBRARY_ANCHOR_REASON}"],
            _LIBRARY_ANCHOR_REASON,
            branch=branch_hint,
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("agent_prompt_save: library anchor ensure failed: %s", e)


@_tool()
def agent_prompt_save(
    pattern: str,
    content: str,
    directory: str | None = None,
    branch_hint: str | None = None,
    purpose: str | None = None,
    storage=None,
) -> dict:
    """Save (upsert) an agent-prompt for the given task pattern.

    v5.85 rework: one page per pattern, deterministic slug agent-prompt-<pattern>.
    Wiki versioning (wiki_page_version) carries history. Second save → version 2, etc.

    Args:
        pattern: Task pattern identifier (e.g. "dispatch-fix-bug").
                 ASCII alphanumeric, hyphens, underscores only.
        content: The prompt text content.
        directory: Absolute project path or 'global'. Required (v5.42.5).
        branch_hint: Caller branch context (optional).
        purpose: One-line description for the TOC. Derived from pattern if omitted.
        storage: StorageEngine instance (injected for testing; otherwise
                 resolved from server lifecycle).

    Returns:
        {"saved": True, "version": N, "slug": "...", "page_id": ...}
    """
    if storage is None:
        from yadgar.server.lifecycle import _get_storage

        storage = _get_storage()

    # v5.42.5: directory required — reject at MCP boundary (same contract as wiki_add)
    _effective_dir = (directory or "").strip() or None
    if not _effective_dir:
        return {
            "error": "missing_directory",
            "saved": False,
            "message": (
                "directory required and must be non-empty. "
                "Pass the absolute project path or 'global'."
            ),
            "field": "directory",
            "op_type": "agent_prompt_save",
        }

    _gate = gate_or_reject(content)
    if _gate is not None:
        return _gate

    slug = f"agent-prompt-{pattern}"
    title = f"Agent Prompt: {pattern}"
    tags = ["agent-prompt", f"task:{pattern}"]
    _purpose = purpose or f"Agent prompt for {pattern} tasks."
    # Wrap content with required headings so wiki_lint passes for page_type="agent_prompt"
    full_content = f"## Purpose\n\n{_purpose}\n\n## Prompt\n\n{content}"

    # v5.42.5: route through wiki_add machinery (branch + directory + similarity gate).
    import yadgar.server._state as _st_mod  # noqa: PLC0415

    wiki = _st_mod._wiki
    if wiki is not None:
        result = wiki.add(
            title=title,
            content=full_content,
            category="reference",
            tags=tags,
            opts=WikiAddOptions(
                source_memory_ids=[],
                confidence="high",
                branch=branch_hint,
                directory_context=_effective_dir,
                page_type="agent_prompt",
            ),
        )
        page_id = result.get("id")
        version = storage.get_max_version_for_page(int(page_id)) if page_id is not None else 1
    else:
        # Fallback: direct storage upsert when wiki not initialised (tests)
        existing = storage.get_wiki_page_by_slug(slug)
        if existing is not None:
            page_id = storage._extract_id(existing.get("id"))
            storage.update_wiki_page(
                page_id,
                {
                    "title": title,
                    "content": full_content,
                    "tags": tags,
                    "category": "reference",
                    "confidence": "high",
                    "directory_context": _effective_dir,
                    "page_type": "agent_prompt",
                },
            )
            version = storage.get_max_version_for_page(page_id)
        else:
            page_id = storage.insert_wiki_page(
                {
                    "slug": slug,
                    "title": title,
                    "content": full_content,
                    "tags": tags,
                    "links": [],
                    "category": "reference",
                    "confidence": "high",
                    "source_memory_ids": [],
                    "directory_context": _effective_dir,
                    "page_type": "agent_prompt",
                    "wiki_schema_version": 1,
                }
            )
            version = 1

    # S6 discovery surface (best-effort; failures never block the save):
    #   1. upsert the pattern → purpose row into the global TOC page.
    #   2. ensure the global discovery anchor exists (create-if-absent).
    _upsert_toc_row(pattern, _purpose, branch_hint)
    _ensure_library_anchor(branch_hint)

    return {
        "saved": True,
        "version": version,
        "slug": slug,
        "page_id": page_id,
    }


# ── S8 starter library ───────────────────────────────────────────────────────
# Pinned patterns and content for the 4 built-in dispatch starters.
# Slug for each: agent-prompt-<pattern>  (MUST match test assertions exactly).
STARTER_PROMPTS: list[tuple[str, str, str]] = [
    (
        "code-review",
        "Review a diff or PR for correctness and risk.",
        (
            "Review the given diff or PR. One finding per line, severity-tagged"
            " (critical/high/medium/low).\n"
            "Cite file:line for every finding.\n"
            "Flag only what changes correctness, security, or observable behavior.\n"
            "No praise, no scope creep, no unrelated cleanups.\n"
            "If nothing is wrong, report 'no issues found'."
        ),
    ),
    (
        "debug-investigate",
        "Root-cause a bug and ship a minimal fix with a regression test.",
        (
            "Reproduce the bug first — confirm failure before touching code.\n"
            "Isolate via bisection, logging, or binary-search; identify the true"
            " root cause, not the symptom.\n"
            "Apply the minimal fix — surgical edit, no opportunistic cleanups.\n"
            "Add a regression test that fails before the fix and passes after.\n"
            "Run the full suite; loop until green."
        ),
    ),
    (
        "explore-codebase",
        "Map where code lives / how a subsystem works (READ-ONLY).",
        (
            "READ-ONLY investigation — make zero edits.\n"
            "Locate where X lives or how Y works; start broad (grep/glob) then narrow.\n"
            "Return a file:line table with one row per relevant symbol or entry-point.\n"
            "Quote function signatures exactly as they appear in source.\n"
            "Do NOT propose or apply fixes; report the map, not opinions."
        ),
    ),
    (
        "implement-tdd",
        "Implement a feature test-first (red → green → refactor).",
        (
            "Write a failing test that pins the desired behavior (red) before any"
            " implementation code.\n"
            "Implement the minimal code needed to pass (green).\n"
            "Refactor with tests staying green — no behaviour change.\n"
            "Run the full check surface (tests/lint/types) and loop until clean.\n"
            "Done = tests pass and checks are green."
        ),
    ),
]


@_tool(power=True)
def seed_agent_prompts(
    storage=None,
    branch_hint: str | None = None,
) -> dict:
    """Idempotently seed the 4 built-in starter agent-prompts (global).

    Skips any pattern whose page already exists (create-if-absent per pattern).
    Calling twice is safe: second call returns created=0, skipped=4.

    The TOC and global discovery anchor are managed by agent_prompt_save —
    this function does NOT duplicate that logic.

    Args:
        storage: StorageEngine instance (injected for testing; otherwise
                 resolved from server lifecycle).
        branch_hint: Caller branch context (optional).

    Returns:
        {"seeded": True, "created": N, "skipped": M, "patterns": [...all 4...]}
    """
    if storage is None:
        from yadgar.server.lifecycle import _get_storage  # noqa: PLC0415

        storage = _get_storage()

    created = 0
    skipped = 0
    for pattern, purpose, content in STARTER_PROMPTS:
        slug = f"agent-prompt-{pattern}"
        existing = _read_agent_prompt(slug, storage=storage)
        if existing is not None:
            skipped += 1
        else:
            agent_prompt_save(
                pattern,
                content,
                directory="global",
                purpose=purpose,
                branch_hint=branch_hint,
                storage=storage,
            )
            created += 1

    return {
        "seeded": True,
        "created": created,
        "skipped": skipped,
        "patterns": [p for p, _, _ in STARTER_PROMPTS],
    }


def _read_agent_prompt(slug: str, storage=None) -> dict | None:
    """Internal exact-key slug read for an agent-prompt page.

    v5.85 S4/S5: replaces the removed agent_prompt_get MCP tool. This is a
    deterministic key read (slug == agent-prompt-<pattern>), NOT semantic recall —
    used by agent_dispatch_prelude. Semantic lookup is recall(type="wiki",
    tags=["agent-prompt"]) instead.

    Args:
        slug: Deterministic page slug, e.g. "agent-prompt-dispatch-fix-bug".
        storage: StorageEngine instance (injected for testing; otherwise
                 resolved from server lifecycle).

    Returns:
        {"version": N, "slug": "...", "content": "...", "page_id": ..., "tags": [...],
         "title": "..."} or None if no page exists for the slug.
    """
    if storage is None:
        from yadgar.server.lifecycle import _get_storage  # noqa: PLC0415

        storage = _get_storage()

    try:
        page = storage.get_wiki_page_by_slug(slug)
    except Exception as e:
        logger.debug("_read_agent_prompt slug lookup failed: %s", e)
        return None

    if page is None:
        return None

    page_id = storage._extract_id(page.get("id"))
    if page_id is None:
        return None

    version = storage.get_max_version_for_page(page_id)

    return {
        "version": version,
        "slug": slug,
        "content": page.get("content", ""),
        "page_id": page_id,
        "tags": page.get("tags", []),
        "title": page.get("title", ""),
    }
