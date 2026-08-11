"""Project-aware predicate helpers for the recall read path.

Car C7 (0047 §5 C7) re-keyed this module from ``directory_context`` onto
``project_id``. The mechanism also MOVED: v5.62–v6 filtered in Python AFTER the
query returned (``is_directory_eligible``), which spent the query's LIMIT before
filtering — a scoped recall over a 15%-share project could come back empty
because the top-N was entirely out of scope. Filtering now happens in the
stage-1 ``WHERE``; this module builds that clause.

The predicate has two arms and BOTH are load-bearing:

  ``project_id = $p``   the caller's project.
  ``'global' IN tags``  the cross-project REACH tag. C6's backfill adds it to
                        every row that used to live at
                        ``directory_context='global'``. Dropping this arm
                        silently narrows ~429 rows down to one project — the
                        failure looks like "recall got worse", not like a bug.

An UNSTAMPED row (``project_id`` absent — the column is ``option<string>``, so
it reads as ``None``, NOT as ``"global"``) matches NEITHER arm unless it carries
the reach tag. That is deliberate. See ``build_project_scope_clause``.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from dataclasses import dataclass, field

from yadgar._shared.observability.observe import observe


@dataclass(frozen=True)
class RecallScope:
    """The caller's read scope, as ONE value.

    Car C7. Bundled rather than passed as loose parameters because they always
    travel together and several signatures on the path were already at the I30
    parameter cap — and, more to the point, because splitting them is how an arm
    gets dropped: a caller that threads ``project_id`` and forgets
    ``opt_in_tags`` silently loses the agent-prompt library, with no error and
    no obviously-wrong result.

    Attributes:
        project_id: Resolved project id, or ``None``/empty for an explicitly
            unscoped read (legacy whole-corpus mode).
        opt_in_tags: Tags the caller explicitly requested; relaxes the
            policy-derived ``page_type`` exclusion for the types that declare
            them.
        excluded_slugs: Car C8 — wiki slugs this read must not surface. Today
            this carries the SUPERSEDED ADR set, loaded ONCE per recall from
            the SQL ledger (``adr.status``) in the async route, BEFORE the
            ``asyncio.to_thread`` boundary, and passed down as plain data. It
            is deliberately not a per-candidate lookup: filtering after the
            providers return spends the query's LIMIT first, which is the exact
            defect C7 exists to delete.
    """

    project_id: str | None = None
    opt_in_tags: list[str] | None = field(default=None)
    excluded_slugs: tuple[str, ...] | None = field(default=None)

    @observe(exempt="pure dataclass copy; no I/O, no branching beyond one guard")
    def with_default_opt_in(self, tags: list[str] | None) -> RecallScope:
        """Return self, or a copy defaulting ``opt_in_tags`` to *tags*.

        Car C7. A caller that only set ``tags=`` still means "I asked for these"
        — the row-level ``is_recall_visible`` has always read it that way — so
        the derived exclusion must see them too, or the same request reaches the
        agent-prompt library through one gate and not the other.

        Car C8: ``dataclasses.replace``, NOT an explicit constructor. This runs
        on every recall, and an explicit constructor listing two of three fields
        drops the third SILENTLY — recall keeps returning results, just the
        wrong ones. ``replace`` cannot forget a field that is added later.
        """
        if self.opt_in_tags is not None or not tags:
            return self
        return dataclasses.replace(self, opt_in_tags=tags)

    @observe(exempt="thin delegation to build_recall_scope_clause; no I/O")
    def clause(self, *, page_types: bool = True, prefix: str = "sc") -> tuple[str, dict]:
        """Build this scope's stage-1 ``WHERE`` fragment."""
        return build_recall_scope_clause(
            self.project_id,
            opt_in_tags=self.opt_in_tags,
            excluded_slugs=self.excluded_slugs,
            page_types=page_types,
            prefix=prefix,
        )


# Car C7: the reach tag. A row carrying it is visible from EVERY project.
# C6's backfill adds it to rows leaving ``directory_context='global'``.
GLOBAL_REACH_TAG = "global"


@observe(tier="hot")
def build_project_scope_clause(
    project_id: str | None,
    *,
    prefix: str = "sc",
) -> tuple[str, dict]:
    """Return ``(sql_fragment, params)`` scoping rows to *project_id*.

    Car C7. This is the half of the WHERE clause that EARNS the change: it
    KEEPS a minority (yadgar = 358 of 2,343 wiki pages = 15.3% measured
    2026-08-10), so a Python post-filter discards ~85% of everything it paid to
    fetch — and, on the HNSW arm, may discard 100% of a top-K that never
    contained an in-scope row.

    UNSTAMPED ROWS (``project_id`` IS NONE) DO NOT MATCH — the decision, stated:

        C6 made ``project_id`` an ``option<string>``, so a row the backfill has
        not reached reads as ``None``, not as ``"global"``. Admitting
        ``project_id IS NONE`` as a sentinel — the shape the old
        ``_ALWAYS_ELIGIBLE = {"global", "", None}`` had — would rebuild exactly
        the permissive fallback ADR-0227 exists to delete: every unattributed
        row would leak into every project's recall, forever, and it would look
        like it was working.

        The cost is bounded and sanctioned: in the window between boot and the
        operator running C6's backfill (§8 step 5b), a project-scoped recall
        over an un-backfilled corpus returns ZERO ROWS rather than raising.
        §8 step 5b names zero-results as an acceptable outcome for that window
        (a degraded window, not a maintenance one); the runbook records it.

        ``legacy_directory`` rows carry no ``project_id`` BY DESIGN (C6
        quarantine). They correctly stay invisible here — that is the
        quarantine working, not a backfill gap to close.

    An empty/None *project_id* yields ``("", {})`` — no filtering. Callers that
    must not fall back silently resolve their project upstream, where the
    resolver raises ``UnresolvedProjectError`` (C5).

    Args:
        project_id: The caller's resolved project id (e.g. ``"m-agahi/yadgar"``).
        prefix: Bind-parameter prefix so this fragment can be AND-ed with
            others without a param-name collision.

    Returns:
        ``(sql_fragment, params_dict)``.
    """
    if not project_id:
        return "", {}
    key = f"{prefix}_pid"
    tag_key = f"{prefix}_reach"
    clause = f"(project_id = ${key} OR ${tag_key} IN tags)"
    return clause, {key: project_id, tag_key: GLOBAL_REACH_TAG}


@observe(tier="hot")
def build_slug_exclusion_clause(
    excluded_slugs: Iterable[str] | None,
    *,
    prefix: str = "sc",
) -> tuple[str, dict]:
    """Return ``(sql_fragment, params)`` excluding *excluded_slugs* by slug.

    Car C8. The mechanism the user chose over the two designs that died:

        "we dont remove superseeded adrs i said we keep them but in the stage 1,
         we exclude them (and/or) in surrealdb query use where clause (using the
         policy) to not include them in results for much faster results."

    The set is TINY and PRE-COMPUTED — 14 superseded ADRs in yadgar, order tens
    across every project — and it arrives as plain data from the async route,
    which read it from the SQL ledger before the ``to_thread`` boundary.
    SurrealDB carries NOTHING about ADR status (ADR-0206: one writer, in SQL),
    so nothing here consults a wiki tag to decide what to drop.

    THE ORDER IS SORTED ON PURPOSE. Callers may hand in a set; an unordered
    bind value makes the emitted SQL differ between two identical recalls,
    which defeats plan caching and makes every SQL-comparing test flaky.

    An empty/None set yields ``("", {})``: no superseded ADRs is a normal
    state, and ``slug NOT IN []`` is a no-op predicate at best.

    Args:
        excluded_slugs: Wiki slugs to drop from the stage-1 result set.
        prefix: Bind-parameter prefix so this fragment can be AND-ed with
            others without a param-name collision.

    Returns:
        ``(sql_fragment, params_dict)``.
    """
    if not excluded_slugs:
        return "", {}
    key = f"{prefix}_excl_slugs"
    return f"(slug NOT IN ${key})", {key: sorted(excluded_slugs)}


@observe(tier="hot")
def build_recall_scope_clause(
    project_id: str | None,
    *,
    opt_in_tags=None,
    excluded_slugs: Iterable[str] | None = None,
    page_types: bool = True,
    prefix: str = "sc",
) -> tuple[str, dict]:
    """Return the ONE stage-1 recall WHERE clause: project + reach + type + slug.

    Car C7 built arms 1–3; Car C8 added arm 4. Four things, one clause, one
    place:

      1. ``project_id = $p``      the project predicate
      2. ``'global' IN tags``     the cross-project reach tag
      3. ``page_type NOT IN …``   the policy exclusion, DERIVED from
                                  ``POLICY_BY_TYPE`` — never a second
                                  hand-maintained list (see
                                  ``policy.excluded_page_types``)
      4. ``slug NOT IN …``        the caller-supplied slug exclusion — today
                                  the SUPERSEDED ADR set, loaded from the SQL
                                  ledger in the async route (Car C8)

    Arms 3 AND 4 are wiki-only: the ``memory`` table has neither a ``page_type``
    nor a ``slug`` column, so memory callers pass ``page_types=False`` and both
    are omitted. (One flag governs both because it means the same thing — "this
    is the wiki table" — and a second boolean would let a caller enable an arm
    against a table that cannot answer it.) Arms 1–2 apply to both tables.

    Arm 4 is deliberately NOT conditional on arm 1. A daemon-internal caller
    with no project id still must not surface superseded ADRs when a set was
    supplied; coupling them would make the exclusion vanish on exactly those
    paths, invisibly.

    The ``opt_in_tags`` pass-through is what keeps
    ``recall(type="wiki", tags=["agent-prompt"])`` working: without it the
    derived exclusion would hide the entire agent-prompt library from its own
    documented lookup.

    Args:
        project_id: Caller's resolved project id. Falsy → arms 1–2 omitted.
        opt_in_tags: Tags the caller explicitly requested.
        excluded_slugs: Slugs to exclude (arm 4). Falsy → arm omitted.
        page_types: Include the wiki-only arms 3 and 4.
        prefix: Bind-parameter prefix.

    Returns:
        ``(sql_fragment, params_dict)``; ``("", {})`` when nothing to filter.
    """
    fragments: list[str] = []
    params: dict = {}

    proj_sql, proj_params = build_project_scope_clause(project_id, prefix=prefix)
    if proj_sql:
        fragments.append(proj_sql)
        params.update(proj_params)

    if page_types:
        # Local import: keeps the module-level layering one-directional
        # (``_shared.wiki`` already imports ``_shared.storage``).
        from yadgar._shared.wiki.policy import (  # noqa: PLC0415
            build_page_type_exclusion_clause,
        )

        pt_sql, pt_params = build_page_type_exclusion_clause(opt_in_tags, prefix=prefix)
        if pt_sql:
            fragments.append(pt_sql)
            params.update(pt_params)

        slug_sql, slug_params = build_slug_exclusion_clause(excluded_slugs, prefix=prefix)
        if slug_sql:
            fragments.append(slug_sql)
            params.update(slug_params)

    if not fragments:
        return "", {}
    return " AND ".join(fragments), params


@observe(tier="hot")
def is_project_eligible(
    row_project_id: str | None,
    row_tags,
    caller_project_id: str | None,
) -> bool:
    """Row-level mirror of ``build_project_scope_clause``, for non-SQL signals.

    Car C7. The WHERE clause covers the vector + FTS arms, which is where the
    limit was being spent. It CANNOT cover candidates that arrive from the
    graph walks (PPR, spreading activation) — those traverse edges and hand
    back memory ids the scope clause never saw. This function is the residual
    guard for exactly those, and it is NOT the old post-filter in new clothes:
    it agrees with the SQL arm by construction (same two arms, same treatment
    of unstamped rows) instead of implementing a wider sentinel set.

    ``caller_project_id`` of ``None`` means no filtering (daemon-internal /
    legacy callers), matching the clause builder's empty-fragment case.

    Args:
        row_project_id: The row's ``project_id`` (``None`` when unstamped).
        row_tags: The row's ``tags`` sequence (``None``/absent tolerated).
        caller_project_id: The caller's resolved project id.

    Returns:
        True when the row is in scope for the caller.
    """
    if not caller_project_id:
        return True
    if row_project_id == caller_project_id:
        return True
    return GLOBAL_REACH_TAG in (row_tags or ())


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
