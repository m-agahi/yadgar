"""recent_memories MCP tool + duration parser.

Extracted from ``admin_other.py`` (Car C7b) to keep that module under the
1000-LOC I13 cap. The tool itself (``recent_memories``) and its parser
helper (``_parse_since_duration``) live here as a self-contained pair;
``admin_other.py`` re-exports ``recent_memories`` so the MCP tool
registration in ``@_tool`` sees it from the same module path it expects.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.lifecycle import _get_storage
from yadgar.core.server._app import _tool
from yadgar.core.server.tools._project_param import accept_project_param

logger = logging.getLogger(__name__)


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
