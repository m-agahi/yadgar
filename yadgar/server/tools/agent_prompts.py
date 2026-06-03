"""Agent-prompt versioning MCP tools — v5.3.0 A4.

Tools:
  agent_prompt_get(pattern)          — returns latest version for pattern
  agent_prompt_save(pattern, content) — saves new version, increments N

Storage convention:
  - Slug pattern: agent-prompt-<task-pattern>-v<N>
  - Tags: ["agent-prompt", "task:<pattern>"]
  - Category: "reference"
  - Versions are discrete wiki pages — no mutation of existing pages.

Retrieval:
  List all pages tagged "agent-prompt" + "task:<pattern>", sort by N descending,
  return the first (highest N).
"""

from __future__ import annotations

import logging
import re

from yadgar.secrets import gate_or_reject
from yadgar.server._app import _tool

logger = logging.getLogger(__name__)

# Slug prefix all agent-prompt pages share
_SLUG_PREFIX = "agent-prompt-"
# Pattern to extract N from slug: agent-prompt-<pattern>-vN
_VERSION_SUFFIX_RE = re.compile(r"-v(\d+)$")


def _slug_for(pattern: str, version: int) -> str:
    """Return the slug for a given pattern + version."""
    return f"agent-prompt-{pattern}-v{version}"


def _next_version(storage, pattern: str) -> int:
    """Return next version number for pattern (1 if none exist yet)."""
    tag = f"task:{pattern}"
    try:
        rows = storage._q(
            "SELECT slug FROM wiki_page WHERE tags CONTAINS $tag",
            {"tag": tag},
        )
    except Exception:
        rows = []

    if not rows:
        return 1

    max_v = 0
    for row in rows:
        slug = row.get("slug", "")
        m = _VERSION_SUFFIX_RE.search(slug)
        if m:
            v = int(m.group(1))
            if v > max_v:
                max_v = v
    return max_v + 1


@_tool()
def agent_prompt_save(
    pattern: str,
    content: str,
    directory: str | None = None,
    branch_hint: str | None = None,
    storage=None,
) -> dict:
    """Save a new version of an agent-prompt for the given task pattern.

    Creates a new wiki page with slug agent-prompt-<pattern>-v<N> where N is
    auto-incremented. Existing versions are never modified.

    v5.42.5: routes through wiki_add machinery to inherit branch + directory
    validation and similarity gate. Accepts directory + branch_hint params.

    Args:
        pattern: Task pattern identifier (e.g. "dispatch-fix-bug").
                 ASCII alphanumeric, hyphens, underscores only.
        content: The prompt text content.
        directory: Absolute project path or 'global'. Required (v5.42.5).
        branch_hint: Caller branch context (optional).
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

    version = _next_version(storage, pattern)
    slug = _slug_for(pattern, version)
    tags = ["agent-prompt", f"task:{pattern}"]

    # v5.42.5: route through wiki_add machinery (branch + directory + similarity gate).
    # Option A: call _wiki.add() directly — inherits branch + directory provenance.
    import yadgar.server._state as _st_mod  # noqa: PLC0415

    wiki = _st_mod._wiki
    if wiki is not None:
        result = wiki.add(
            title=f"Agent Prompt: {pattern} v{version}",
            content=content,
            category="reference",
            tags=tags,
            source_memory_ids=[],
            confidence="high",
            branch=branch_hint,
            directory_context=_effective_dir,
        )
        page_id = result.get("id")
    else:
        # Fallback: direct storage insert when wiki not initialised (tests)
        page_id = storage.insert_wiki_page(
            {
                "slug": slug,
                "title": f"Agent Prompt: {pattern} v{version}",
                "content": content,
                "tags": tags,
                "links": [],
                "category": "reference",
                "confidence": "high",
                "source_memory_ids": [],
                "directory_context": _effective_dir,
            }
        )

    return {
        "saved": True,
        "version": version,
        "slug": slug,
        "page_id": page_id,
    }


@_tool()
def agent_prompt_get(
    pattern: str,
    storage=None,
) -> dict | None:
    """Return the latest version of the agent-prompt for the given pattern.

    Looks up wiki pages tagged ["agent-prompt", "task:<pattern>"] and returns
    the page with the highest version number N in its slug suffix.

    Args:
        pattern: Task pattern identifier (e.g. "dispatch-fix-bug").
        storage: StorageEngine instance (injected for testing; otherwise
                 resolved from server lifecycle).

    Returns:
        {"version": N, "slug": "...", "content": "...", "page_id": ...}
        or None if no pages exist for pattern.
    """
    if storage is None:
        from yadgar.server.lifecycle import _get_storage

        storage = _get_storage()

    tag = f"task:{pattern}"
    try:
        rows = storage._q(
            "SELECT * FROM wiki_page WHERE tags CONTAINS $tag AND tags CONTAINS 'agent-prompt'",
            {"tag": tag},
        )
    except Exception as e:
        logger.debug("agent_prompt_get query failed: %s", e)
        return None

    if not rows:
        return None

    # Find the row with the highest version number
    best_row = None
    best_v = -1
    for row in rows:
        slug = row.get("slug", "")
        m = _VERSION_SUFFIX_RE.search(slug)
        if m:
            v = int(m.group(1))
            if v > best_v:
                best_v = v
                best_row = row

    if best_row is None:
        return None

    best_row.pop("embedding", None)  # strip binary field

    return {
        "version": best_v,
        "slug": best_row.get("slug", ""),
        "content": best_row.get("content", ""),
        "page_id": best_row.get("id"),
        "tags": best_row.get("tags", []),
        "title": best_row.get("title", ""),
    }
