"""Bypass constants and helper for the predictive-coding write gate.

Content matching these patterns is ALWAYS stored regardless of surprisal —
bypass is checked before any embedding computation.
"""

import re

# ---------------------------------------------------------------------------
# Bypass keyword patterns
# ---------------------------------------------------------------------------

_ERROR_BYPASS_RE = re.compile(
    r"\b(error|exception|traceback|failed|bug|crash)\b",
    re.IGNORECASE,
)
_DECISION_BYPASS_RE = re.compile(
    r"\b(decided|chose|switched to|migrated|architecture)\b",
    re.IGNORECASE,
)
_BYPASS_TAGS = frozenset({"important", "critical"})


def _bypass_reason(content_lower: str, tags: list[str]) -> str | None:
    """Return a bypass reason string if content/tags trigger an always-store bypass.

    Returns None when no bypass applies (normal gate evaluation continues).

    Args:
        content_lower: Lowercased memory content (caller pre-lowercases once).
        tags:          Memory tags (compared case-insensitively).
    """
    if _ERROR_BYPASS_RE.search(content_lower):
        return "bypass_error_keywords"
    if _DECISION_BYPASS_RE.search(content_lower):
        return "bypass_decision_keywords"
    if _BYPASS_TAGS & {t.lower() for t in tags}:
        return "bypass_important_tag"
    return None
