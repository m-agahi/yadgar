"""recall MCP tool registration."""

from __future__ import annotations

import logging

import yadgar.server._state as _st
from yadgar.config import get_settings
from yadgar.observability.observe import observe
from yadgar.server._app import _tool
from yadgar.server.lifecycle import _get_embeddings, _get_storage

# Pipeline functions extracted to _recall_pipeline.py (app-free; shared with backend).
# Import them here so existing call sites in this file and in tests are unchanged.
from yadgar.server.tools._recall_pipeline import (  # noqa: F401 (re-exported for monkeypatch)
    _apply_quality_floor,
    _apply_recall_db_side_effects,
    _apply_recall_session_side_effects,
    _apply_recall_side_effects,
    _candidates_to_dicts,
    _dedup_by_content,
    _fanout_recall,
    _fuse_with_span,
    _record_recall_sr_transition,
)
from yadgar.storage.directory import is_directory_eligible

logger = logging.getLogger(__name__)

settings = get_settings()

# Valid type filter values for recall(type=) param (Step 5).
_VALID_RECALL_TYPES: frozenset[str] = frozenset({"all", "memory", "wiki"})

# Valid mode values for recall(mode=) param (v6 BC-AC3a landscape).
# None = normal (default). "landscape" = consensus_retrieve across astrocyte domains.
_VALID_RECALL_MODES: frozenset[str] = frozenset({"landscape"})


# ---------------------------------------------------------------------------
# v6 BC-AC3a landscape recall — consensus_retrieve exposed as opt-in mode
# ---------------------------------------------------------------------------


@observe(tier="stage", name="tools.recall._landscape_recall")
def _landscape_recall(
    query: str,
    max_results: int,
    directory: str,
    storage,
) -> list[dict]:
    """Run AstrocytePool.consensus_retrieve and directory-scope the results.

    Called ONLY when mode="landscape".  Returns a ranked consensus list where
    each row carries ``consensus_score`` (float) and ``voting_domains`` (list[str]).

    Directory scoping (v5.65 contract): consensus_retrieve does NOT scope by
    directory itself, so results are post-filtered with is_directory_eligible()
    — the same predicate used by the fan-out and legacy paths.

    Returns [] gracefully when the pool is unavailable (ASTROCYTE_POOL_ENABLED=False
    or pool init failed).

    Args:
        query: Search query forwarded to consensus_retrieve.
        max_results: Number of rows to return after post-filtering.  Passed
            directly to consensus_retrieve as top_k so the pool trims first, then
            we directory-filter.  Pass a generous value (e.g. max_results * 3) in
            production callers to compensate for post-filter shrinkage; the
            MCP tool passes max_results directly (documented tradeoff).
        directory: Caller project directory (required, validated upstream).
        storage: StorageEngine instance (used by side-effects call).

    Returns:
        List of memory dicts with consensus_score / voting_domains, scoped to
        directory, trimmed to max_results.
    """
    if _st._pool is None:
        logger.debug("landscape_recall: pool unavailable — returning []")
        return []

    # Fetch a generous candidate pool; directory filter may shrink results below
    # max_results when top_k == max_results.  Documented known tradeoff: callers
    # wanting strict top-k across directory-filtered results should pass a larger
    # max_results.
    raw = _st._pool.consensus_retrieve(query, top_k=max_results)

    # Directory scope — same predicate as fan-out and legacy paths.
    scoped = [r for r in raw if is_directory_eligible(r.get("directory_context"), directory)]

    return scoped[:max_results]


# ---------------------------------------------------------------------------
# Train 1: core-side thin forwarder to backend /recall endpoint
# ---------------------------------------------------------------------------


@observe(tier="boundary", name="tools.recall._forward_to_backend")
def _forward_to_backend(  # noqa: PLR0913 — 8 args match fanout signature
    query: str,
    max_results: int,
    min_heat: float,
    directory: str,
    current_branch: str | None,
    default_branch: str | None,
    type_filter: str,
    tags: list[str] | None,
) -> list[dict]:
    """Forward the fan-out recall to the backend /recall endpoint.

    Called ONLY when RECALL_BACKEND_ENABLED=True.  Builds a RecallRequest,
    POSTs to the backend with the existing Bearer auth, and returns the
    deserialized result list.  The caller (recall()) then runs
    _apply_recall_session_side_effects on the returned list.

    Backend URL: derived from YADGAR_EMBED_URL (the same base URL used by
    RemoteMLClient for /rerank).  If YADGAR_EMBED_URL is not configured,
    raises RuntimeError (caught by the caller, which falls back to in-core).

    Branch args come from the caller's _detect_branch resolution — the backend
    must NOT call _detect_branch (no host .git in container).

    Args:
        query: Search query.
        max_results: Max results to return.
        min_heat: Minimum heat threshold.
        directory: Caller project directory (required).
        current_branch: Resolved git branch or None.
        default_branch: Repo default branch or None.
        type_filter: Source type filter ("all", "memory", "wiki").
        tags: Optional tag include filter.
    Returns:
        List of result dicts returned by the backend /recall endpoint.

    Raises:
        RuntimeError: if YADGAR_EMBED_URL is not configured.
        httpx.HTTPError: if the backend request fails.
    """
    import os  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    backend_base = os.environ.get("YADGAR_EMBED_URL", "").rstrip("/")
    if not backend_base:
        raise RuntimeError(
            "RECALL_BACKEND_ENABLED=True but YADGAR_EMBED_URL is not set; "
            "cannot forward recall to backend."
        )

    token = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    payload: dict = {
        "query": query,
        "directory": directory,
        "current_branch": current_branch,
        "default_branch": default_branch,
        "max_results": max_results,
        "min_heat": min_heat,
        "type": type_filter,
        "tags": tags,
    }

    resp = httpx.post(
        f"{backend_base}/recall",
        json=payload,
        headers=headers,
        timeout=120.0,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("results", [])


@_tool()
def recall(  # noqa: C901,PLR0913 - cohesive: MCP tool — single entry point for all recall variants
    query: str,
    max_results: int = 5,
    min_heat: float = 0.0,
    profile: str | None = None,
    stage_overrides: dict[str, dict] | None = None,
    directory: str | None = None,
    branch_hint: str | None = None,
    type: str = "all",  # noqa: A002 — shadows built-in but matches MCP schema convention
    mode: str | None = None,
    tags: list[str] | None = None,
) -> list[dict]:
    """Primary semantic + keyword retrieval tool. Use for discovery and context loading.

    Prefer recall() over memory_get()/wiki_get() when you don't already have a
    numeric ID — those tools are for direct ID lookups, not search. Prefer
    recall(type="wiki") over wiki_query() for wiki-only searches.

    Args:
        query: Search text. Combined semantic (embedding) + keyword scoring.
        directory: REQUIRED. Host-side project directory (e.g. "/home/user/myapp").
            Results are scoped to this directory plus global wiki pages. Do NOT omit
            or pass empty — daemon runs in a container and cannot detect the real path
            via os.getcwd(). Raises ValueError if absent/empty.
        max_results: Max results to return (default 5). Higher = slower + more tokens;
            keep <=10 for targeted lookups, <=20 for broad exploration.
        min_heat: Heat floor (default 0.0 = no filter). Pass >0.5 to restrict to
            stable, frequently-accessed memories only.
        type: Source type filter — "all" (default), "memory" (memories only), "wiki"
            (wiki pages only). Only effective when profile=None and mode=None, and only
            on the unified fan-out path (UNIFIED_RECALL_ENABLED=True); legacy path
            ignores it. Raises ValueError on unrecognised value.
        profile: Optional retrieval profile — "fast", "balanced", "full", "debug".
            When set, routes through the v5.31 plugin pipeline and ignores type.
            When None (default), uses legacy monolithic path. Raises ValueError on
            unrecognised value.
        stage_overrides: Per-call stage disable map, e.g. {"nli": {"enabled": False}}.
            Only used when profile is set.
        branch_hint: Caller-supplied branch name. Fallback when daemon cannot detect
            the branch (container scenario). Resolution order:
            _detect_branch(directory) → branch_hint → None.
        mode: Opt-in recall mode. None (default) = normal precise recall.
            "landscape" = broad cross-domain recall via consensus_retrieve across all
            astrocyte domains — each domain votes independently and results are merged
            into one ranked list with consensus_score and voting_domains per row.
            Use mode="landscape" for broad or exploratory queries where cross-domain
            breadth and diversity matter more than precise top-k ranking (e.g. "what
            do I know about X" spanning multiple unrelated past projects). SLOWER than
            normal recall (votes across all domains); opt-in only — omit for normal
            targeted retrieval. Mutually exclusive with profile/type: mode wins when
            set, those params are ignored. Raises ValueError on unrecognised value.
        tags: Tag include filter for wiki results. When set, triggers SQL pre-filter
            in WikiStore.query() — brute-force cosine over pages tagged with tags[0]
            (avoids HNSW global-pool dilution). Also suppresses the default
            agent-prompt exclude so tagged results are not accidentally hidden.
            None (default) = general recall with agent-prompt pages excluded from wiki
            results. Only effective on the unified fan-out path (UNIFIED_RECALL_ENABLED=True).

    Returns:
        List of memory/wiki dicts ranked by relevance + heat. landscape mode adds
        consensus_score (float) and voting_domains (list[str]) per row.

    Raises:
        ValueError: if directory is empty, or profile/type/mode is unrecognised.
    """
    import time as _time  # noqa: PLC0415

    # v5.65 Fix D: hard-require directory — MUST be first check in body (before
    # _get_storage() so the error fires even when StorageEngine is not initialised).
    # Container-safe: do NOT fall back to os.getcwd().
    _dir_stripped = (directory or "").strip().rstrip("/")
    if not _dir_stripped:
        raise ValueError(
            "recall: directory is required (caller must supply project dir; "
            "container cannot detect it via os.getcwd())"
        )

    # v6 T6 Step 5: validate type param early — before any expensive setup.
    if type not in _VALID_RECALL_TYPES:
        raise ValueError(
            f"recall: type={type!r} is not valid. Valid values: {sorted(_VALID_RECALL_TYPES)}"
        )

    # v6 BC-AC3a: validate mode param early — before any expensive setup.
    if mode is not None and mode not in _VALID_RECALL_MODES:
        raise ValueError(
            f"recall: mode={mode!r} is not valid. Valid values: {sorted(_VALID_RECALL_MODES)} or None"
        )

    # I3: validate profile BEFORE any expensive setup or DB access.
    if profile is not None:
        from yadgar.retrieval.profiles import _VALID_PROFILES  # noqa: PLC0415

        if profile not in _VALID_PROFILES:
            raise ValueError(
                f"Unknown retrieval profile {profile!r}. Valid profiles: {sorted(_VALID_PROFILES)}"
            )

    _recall_t0 = _time.monotonic()
    merged: list[dict] = []  # P11 Bug A fix: init before try so finally sees it

    try:
        storage = _get_storage()

        # Record activity on consolidation engine
        if _st._consolidation is not None:
            _st._consolidation.record_activity()

        # v6 BC-AC3a: landscape mode — exclusive dispatch, wins over type/profile.
        # Placed here: after storage init (side-effects need storage) and before
        # branch detection (landscape needs neither branch context nor fan-out flag).
        if mode == "landscape":
            merged = _landscape_recall(
                query=query,
                max_results=max_results,
                directory=_dir_stripped,
                storage=storage,
            )
            _apply_recall_side_effects(merged, query, storage)
            return merged

        # §25 Branch context — detect before retrieval so we can push the filter into
        # SurrealQL (C2) rather than post-filtering in Python.
        # v5.43.0: use caller-supplied directory for detection (avoids daemon-CWD bug).
        # branch_hint supplies branch when _detect_branch returns None (container scenario).
        # Resolution: _detect_branch(directory or os.getcwd()) → branch_hint → None.
        # Look up via yadgar.server (where tests patch these) so monkeypatches on
        # "yadgar.server._detect_branch" / "yadgar.server._get_default_branch" take effect.
        try:
            import sys as _sys  # noqa: PLC0415

            _cwd = (
                _dir_stripped  # v5.65 Fix D: directory is required; _dir_stripped always non-empty
            )
            _srv = _sys.modules.get("yadgar.server")
            if _srv is not None:
                _detect_branch_fn = getattr(_srv, "_detect_branch", None)
                _get_default_branch_fn = getattr(_srv, "_get_default_branch", None)
            else:
                _detect_branch_fn = None
                _get_default_branch_fn = None
            if _detect_branch_fn is None or _get_default_branch_fn is None:
                from yadgar.server.tools.project import _detect_branch as _detect_branch_fn
                from yadgar.server.tools.project import (
                    _get_default_branch as _get_default_branch_fn,
                )
            _current_branch = _detect_branch_fn(_cwd)
            _default_branch = _get_default_branch_fn(_cwd)
        except Exception:
            _current_branch = None
            _default_branch = None  # v5.42.4: canonical slot

        # v5.43.0: branch_hint fallback — mirrors wiki_read (v5.42.6 F1) and
        # _resolve_page_id_by_slug (v5.42.5). Use branch_hint when daemon-side
        # detection returns None (container scenario / no .git in _cwd).
        if not _current_branch and branch_hint:
            _current_branch = branch_hint

        # v5.96.0: shadow recall result-cache hit-rate counter (instrumentation
        # ONLY — caches nothing, changes no behaviour, fully guarded).  Measures
        # the hit-rate a hypothetical query→output cache (cache-refactor lever a,
        # deliberately NOT built) would achieve, to gate building it on evidence.
        # Placed after branch detection (a proper key needs the branch bucket) and
        # before dispatch, so it covers all three dominant paths (fan-out / pipeline
        # / legacy).  Landscape mode returns early above and is intentionally NOT
        # shadowed — it is experimental (#67), rare, and off this chokepoint.
        # v5.100.0: source="tool" — explicit MCP-tool calls from the /mcp endpoint.
        try:
            from yadgar.server.tools._recall_shadow import (  # noqa: PLC0415
                RecallShadowParams,
                observe_recall,
            )

            observe_recall(
                RecallShadowParams(
                    query=query,
                    directory=_dir_stripped,
                    branch=_current_branch,
                    type_filter=type,
                    mode=mode,
                    profile=profile,
                    max_results=max_results,
                    min_heat=min_heat,
                    tags=tags,
                    source="tool",
                )
            )
        except Exception:  # instrumentation must never break recall
            pass

        # v6 T6: UNIFIED_RECALL_ENABLED gate — early exit to fan-out path.
        # When True: fan out to MemoryProvider + WikiProvider, pool, dedup, return.
        # When False (default): fall through to the EXACT legacy body below.
        # The gate is intentionally placed AFTER branch detection so the fan-out
        # has _current_branch / _default_branch available.  _dir_stripped is
        # guaranteed non-empty by the v5.65 Fix D check at function top.
        # The `merged` assignment ensures the finally-block metrics see a count.
        # Fan-out applies only to the DEFAULT (no-profile) recall. An explicit
        # profile routes through the legacy plugin pipeline below: profiles tune
        # *memory* retrieval (e.g. the hook-supplied profile="fast" fast-path that
        # skips CE/NLI/MP), which is orthogonal to fan-out's cross-source (memory +
        # wiki) fusion. Gating on `profile is None` preserves the profile fast-path
        # with zero feature loss. NOTE: `type` is therefore ignored when a profile
        # is set (the legacy path is memory-only).
        if settings.UNIFIED_RECALL_ENABLED and profile is None:
            # Train 1: RECALL_BACKEND_ENABLED dual-path.
            # When ON: forward the fan-out to the backend /recall endpoint.
            # When OFF (default): run fan-out in-core as before.
            # landscape and profile paths never reach here (handled above or below).
            _recall_settings = get_settings()
            if _recall_settings.RECALL_BACKEND_ENABLED:
                try:
                    merged = _forward_to_backend(
                        query=query,
                        max_results=max_results,
                        min_heat=min_heat,
                        directory=_dir_stripped,
                        current_branch=_current_branch,
                        default_branch=_default_branch,
                        type_filter=type,
                        tags=tags,
                    )
                    # Session-side bookkeeping runs in core (SR transitions, buffer, replay).
                    # DB-side bookkeeping (heat boost, thermo) already ran in the backend.
                    _apply_recall_session_side_effects(merged, query)
                    return merged
                except Exception as _fwd_exc:
                    logger.warning(
                        "recall: backend forward failed (%s), falling back to in-core path",
                        _fwd_exc,
                    )
                    # Fall through to in-core fan-out below.

            merged = _fanout_recall(
                query=query,
                max_results=max_results,
                min_heat=min_heat,
                directory=_dir_stripped,
                current_branch=_current_branch,
                default_branch=_default_branch,
                type_filter=type,
                tags=tags,
            )
            # Same post-retrieval bookkeeping as the legacy path (heat boost,
            # last_accessed, metamemory, SR transitions, action log) — fan-out
            # must reinforce heat on access too.
            _apply_recall_side_effects(merged, query, storage)
            return merged

        # Use HippoRetriever for unified 4-signal recall
        retriever = _st._retriever
        if retriever is not None and profile is not None:
            # v5.31.1: profile set → route through plugin pipeline.
            from yadgar.metrics import (  # noqa: PLC0415
                yadgar_recall_profile_invocations_total,
            )

            yadgar_recall_profile_invocations_total.labels(profile=profile).inc()
            merged = retriever.recall_via_pipeline(
                query,
                max_results=max_results * 3,
                min_heat=min_heat,
                current_branch=_current_branch,
                default_branch=_default_branch,
                profile=profile,
                stage_overrides=stage_overrides,
            )
        elif retriever is not None:
            merged = retriever.recall(
                query,
                max_results=max_results * 3,
                min_heat=min_heat,
                current_branch=_current_branch,
                default_branch=_default_branch,
            )
        else:
            # Fallback to basic FTS + vector if retriever not initialized
            embeddings = _get_embeddings()
            try:
                fts_results = storage.search_memories_fts(
                    query, min_heat=min_heat, limit=max_results * 2
                )
            except Exception:
                fts_results = []

            semantic_results = []
            query_embedding = embeddings.encode(query)
            if query_embedding is not None:
                vec_hits = storage.search_vectors(
                    query_embedding, top_k=max_results * 2, min_heat=min_heat
                )
                for mid, _distance in vec_hits:
                    mem = storage.get_memory(mid)
                    if mem:
                        semantic_results.append(mem)

            seen = set()
            merged = []
            for m in fts_results + semantic_results:
                if m["id"] not in seen:
                    seen.add(m["id"])
                    merged.append(m)

            merged.sort(
                key=lambda m: m["heat"] * m.get("confidence", 1.0),
                reverse=True,
            )
            merged = merged[: max_results * 3]
            for m in merged:
                m.pop("embedding", None)

        # §25 Build allowed-branch set used both for the fallback filter (below) and
        # for wiki blending (line ~169).  Must be defined before any reference to it.
        _allowed_branches: set[str | None] = {_default_branch, None}
        if _current_branch is not None:
            _allowed_branches.add(_current_branch)

        # §25 Branch filter is now pushed into SurrealQL via BranchFilter (C2).
        # The fallback path also needs the filter applied in Python since it doesn't
        # go through the retriever.
        if retriever is None:
            # Fallback path: apply Python-side filter (retriever path uses SurrealQL filter)
            merged = [m for m in merged if m.get("branch") in _allowed_branches]

        # v5.42.5 / v5.62.0: directory filter — scope results to caller directory.
        # v5.62.0: replaces hand-rolled predicate with is_directory_eligible() from
        # storage/directory.py — single source of truth for the eligible-set rule.
        # Applied as a Python-side post-filter (retriever pipeline threading is v5.44+).
        # v5.65 Fix D: directory is now required (validated at function top), so
        # caller_dir is always a non-empty absolute path here.
        caller_dir = _dir_stripped  # guaranteed non-empty by earlier validation
        merged = [
            m for m in merged if is_directory_eligible(m.get("directory_context"), caller_dir)
        ]

        # v5.62.0: quality floor — drop results the cross-encoder scored below threshold.
        # Targets co-occurrence / keyword-only noise that survives with CE≈0.
        # Rows without _cross_encoder_score (fallback path, wiki-blend) always pass through.
        _quality_floor = getattr(settings, "RECALL_QUALITY_FLOOR", 0.0)
        merged = _apply_quality_floor(merged, _quality_floor)

        # v5.62.0: dedup — collapse repeated identical co-occurrence rows.
        # Same content can appear multiple times with different created_at values;
        # keep the first (highest-scored) occurrence.
        merged = _dedup_by_content(merged)

        # §25 C4: convex-combination boost for current-branch results.
        # boosted = score + (1 - score) * BRANCH_BOOST_WEIGHT — keeps scores in [0,1].
        # v5.1 C4: replaces hard 1.5x multiplier with bounded convex combination.
        if _current_branch is not None:
            _boost_weight = settings.BRANCH_BOOST_WEIGHT
            for m in merged:
                if m.get("branch") == _current_branch:
                    # Clamp to 1.0: WRRF fusion can produce scores > 1.0; without the
                    # clamp the convex formula inverts (e.g. base=1.5 → boosted=1.4).
                    base = min(m.get("_retrieval_score", m.get("heat", 0.0)), 1.0)
                    m["_retrieval_score"] = base + (1.0 - base) * _boost_weight
            merged.sort(key=lambda m: m.get("_retrieval_score", 0.0), reverse=True)

        # Q2 — Postmortem/incident tag retrieval boost (v5.3.5).
        # When the query contains an action verb AND a candidate has tag _postmortem
        # or _incident, boost via convex combination (same formula as branch boost).
        # boosted = score + (1 - score) * POSTMORTEM_BOOST_FACTOR
        _pm_boost_factor = getattr(settings, "POSTMORTEM_BOOST_FACTOR", 0.3)
        _pm_keywords = getattr(settings, "POSTMORTEM_BOOST_KEYWORDS", ())
        if _pm_boost_factor > 0.0 and _pm_keywords:
            _query_lower = query.lower()
            _has_action_verb = any(kw in _query_lower for kw in _pm_keywords)
            if _has_action_verb:
                _pm_tags = {"_postmortem", "_incident"}
                for m in merged:
                    _mem_tags = set(m.get("tags", []))
                    if _mem_tags & _pm_tags:
                        base = min(m.get("_retrieval_score", m.get("heat", 0.0)), 1.0)
                        m["_retrieval_score"] = base + (1.0 - base) * _pm_boost_factor
                merged.sort(key=lambda m: m.get("_retrieval_score", 0.0), reverse=True)

        merged = merged[:max_results]

        # Post-retrieval bookkeeping (heat boost, last_accessed, metamemory, SR
        # transitions, action log) — shared verbatim with the fan-out path via
        # _apply_recall_side_effects so both paths reinforce heat identically.
        _apply_recall_side_effects(merged, query, storage)

        # Reconsolidation disabled: memories are never rewritten on retrieval.
        # Content integrity must be preserved exactly as stored.

        # Wiki blending — relevance-gated, skip for episodic/temporal queries
        if _st._wiki is not None and not _is_episodic_query(query):
            try:
                wiki_results = _st._wiki.query(query, max_results=5)
                qualifying = [wr for wr in wiki_results if wr.get("_retrieval_score", 0.0) > 0.3]
                # §25 branch filter: exclude wiki pages outside allowed branches
                qualifying = [wr for wr in qualifying if wr.get("branch") in _allowed_branches]
                # v5.65.0: directory filter — same rule as memories (is_directory_eligible).
                # caller_dir is None when directory=None (legacy mode) → no filter applied.
                if caller_dir:
                    qualifying = [
                        wr
                        for wr in qualifying
                        if is_directory_eligible(wr.get("directory_context"), caller_dir)
                    ]
                for wr in qualifying:
                    wr["_source"] = "wiki"
                    wr.pop("embedding", None)
                if qualifying:
                    merged = sorted(
                        merged + qualifying,
                        key=lambda m: m.get("_retrieval_score", 0.0),
                        reverse=True,
                    )[:max_results]
            except Exception:
                logger.warning("Wiki blending failed for query %r", query[:80], exc_info=True)

        # Strip binary fields from response (not JSON-serializable)
        for m in merged:
            m.pop("embedding", None)

        return merged

    finally:
        # P11 Bug A fix: observe duration in finally — fires on success AND exception.
        # Previously the observation was inside a try/except block at the end of the
        # happy path, so any exception in the body left yadgar_recall_duration_ms.count=0.
        try:
            import time as _time_f  # noqa: PLC0415

            from yadgar.metrics import (  # noqa: PLC0415
                yadgar_recall_duration_ms,
                yadgar_recall_result_count,
            )

            yadgar_recall_duration_ms.observe((_time_f.monotonic() - _recall_t0) * 1000)
            yadgar_recall_result_count.observe(len(merged))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Locally needed helpers that recall() uses from the legacy path
# ---------------------------------------------------------------------------
# _is_episodic_query is imported from _recall_pipeline transitively via _helpers.
# Re-import here directly so the legacy path's wiki-blending check can use it.
from yadgar.server._helpers import _is_episodic_query  # noqa: E402,F401
