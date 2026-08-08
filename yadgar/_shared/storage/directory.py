"""Directory-aware predicate helpers.

Used by memory/wiki query methods to restrict results to the caller's project
directory, plus always-eligible sentinels (global, empty, None).

Kept separate from the full StorageEngine so retrieval.core can import
DirectoryFilter without pulling the engine in.

v5.62.0: Python-side post-filter only.  The SurrealQL-level DirectoryFilter
(pushed into WHERE clauses as an injected SQL fragment) is deferred to the
unified-scoped-recall rebuild so it is built once, not twice.
See docs/plans/recall-scoping-restamp.md §B note on DB-level filter.
"""

from __future__ import annotations

from yadgar._shared.observability.observe import observe


class DirectoryFilter:
    """Carry directory context for eligibility checks.

    Attributes:
        caller_dir: Absolute project path of the calling session, or None for
            legacy/daemon-internal contexts.

    Eligible set for Python post-filters (always_eligible_sentinels):
        {caller_dir, 'global', '', None}

    NOTE — 'system' was the mis-stamp sink prior to v5.64.0.  As of v5.64.0
    the three write sites (curation/strengthen.py, cls_store/promotion.py,
    sleep_compute/dream.py) NO LONGER stamp 'system' — they stamp the
    originating project dir or 'global' via dominant_directory().  As of
    v5.65.0 'system' is REMOVED from the eligible set: existing system-stamped
    rows are treated as mis-stamps (noise) and will no longer surface in
    directory-scoped recall / wiki_query / project_brief.  This is safe
    post-v5.64 because no production code path creates new 'system' rows.
    """

    __slots__ = ("caller_dir",)

    def __init__(self, caller_dir: str | None) -> None:
        self.caller_dir = caller_dir

    def __repr__(self) -> str:
        return f"DirectoryFilter(caller_dir={self.caller_dir!r})"


# Always-eligible sentinels — rows with these directory_context values are
# included regardless of caller_dir.  'system' removed in v5.65 (mis-stamp sink;
# v5.64 stopped new system writes; existing rows are noise, not signal).
_ALWAYS_ELIGIBLE: frozenset[str | None] = frozenset({"global", "", None})


@observe(tier="hot")
def is_directory_eligible(directory_context: str | None, caller_dir: str | None) -> bool:
    """Return True when a row is eligible for the given caller directory.

    Always eligible: global, '', system, None (sentinel rows).
    Eligible when caller_dir matches: exact string equality after normalisation.

    When caller_dir is None (legacy / no-filter mode): all rows are eligible.

    Args:
        directory_context: The ``directory_context`` field from the memory/wiki row.
        caller_dir: Normalised caller project path (trailing slash stripped),
            or None for legacy no-filter mode.

    Returns:
        True if the row should be included in results.
    """
    if caller_dir is None:
        # Legacy mode — no directory filter; all rows pass.
        return True

    if directory_context in _ALWAYS_ELIGIBLE:
        return True

    return directory_context == caller_dir


@observe(tier="hot")
def dominant_directory(candidates: list[str | None]) -> str:
    """Return the dominant project directory from a list of directory_context values.

    Rules:
    - Exclude sentinels (None, '', 'global', 'system') from the vote.
    - If exactly one distinct real directory remains → return it.
    - If multiple distinct real directories → return 'global' (genuinely cross-cutting).
    - If no real directory found → return 'global' (safe fallback).

    v5.64.0: used by write-time stamping in curation/strengthen.py,
    cls_store/promotion.py, and sleep_compute/dream.py to eliminate the
    'system' mis-stamp sink.
    """
    _SENTINELS: frozenset[str | None] = frozenset({None, "", "global", "system"})
    real_dirs: set[str] = set()
    for dc in candidates:
        if dc not in _SENTINELS and isinstance(dc, str):
            real_dirs.add(dc)
    if len(real_dirs) == 1:
        return next(iter(real_dirs))
    # 0 or ≥2 distinct real dirs → global (cross-cutting or unknown)
    return "global"


@observe(tier="hot")
def _build_directory_clause(
    directory_filter: DirectoryFilter | None,
) -> tuple[str, dict]:
    """Return (sql_fragment, params_dict) for a directory WHERE predicate.

    NOTE: This function is defined for structural parity with branch.py and
    for use in the unified-scoped-recall rebuild (deferred — that feature will
    push the directory clause into SurrealQL).  It is NOT wired into any
    SurrealQL query in v5.62.0.  Python-side post-filtering via
    ``is_directory_eligible`` is the active mechanism this release.

    When directory_filter is None: returns ('', {}) — no filtering.
    When caller_dir is set: generates a clause that allows sentinel rows
    (directory_context IS NONE, = '', = 'global') plus the caller directory.
    NOTE: 'system' removed from sentinels in v5.65 (mis-stamp sink).

    Args:
        directory_filter: DirectoryFilter instance, or None for no filtering.

    Returns:
        Tuple of (sql_fragment, params_dict).  sql_fragment is '' when no
        filtering is needed (caller_dir is None or directory_filter is None).
    """
    if directory_filter is None or directory_filter.caller_dir is None:
        return "", {}

    params: dict = {
        "df_caller": directory_filter.caller_dir,
    }
    clause = (
        "(directory_context IS NONE"
        " OR directory_context = ''"
        " OR directory_context = 'global'"
        " OR directory_context = $df_caller)"
    )
    return clause, params
