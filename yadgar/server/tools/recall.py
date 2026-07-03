"""recall MCP tool registration."""

from __future__ import annotations

import logging

import yadgar.server._state as _st
from yadgar.config import get_settings
from yadgar.retrieval.providers.base import Scope
from yadgar.retrieval.providers.fusion import fuse_candidates
from yadgar.retrieval.providers.memory import MemoryProvider
from yadgar.retrieval.providers.wiki import WikiProvider
from yadgar.server._app import _tool
from yadgar.server._helpers import _bounded_set, _is_episodic_query
from yadgar.server.lifecycle import _get_embeddings, _get_storage
from yadgar.storage.directory import is_directory_eligible

logger = logging.getLogger(__name__)

settings = get_settings()

# Valid type filter values for recall(type=) param (Step 5).
_VALID_RECALL_TYPES: frozenset[str] = frozenset({"all", "memory", "wiki"})

# Valid mode values for recall(mode=) param (v6 BC-AC3a landscape).
# None = normal (default). "landscape" = consensus_retrieve across astrocyte domains.
_VALID_RECALL_MODES: frozenset[str] = frozenset({"landscape"})


# ---------------------------------------------------------------------------
# v5.62.0 recall quality helpers — quality floor + dedup
# ---------------------------------------------------------------------------


def _apply_quality_floor(memories: list[dict], threshold: float) -> list[dict]:
    """Drop results whose cross-encoder score is below *threshold*.

    Only acts when ``_cross_encoder_score`` is present on a row.  Rows without
    the key (fallback path, rows beyond CE_TOP_K, wiki-blend entries) always
    pass through — missing score must never drop a result.

    When threshold is 0.0, returns the list unchanged (fast path).

    Args:
        memories: Candidate result list (may be mutated in place by other steps;
            this function does NOT mutate — returns a new list).
        threshold: Minimum cross-encoder score.  0.0 disables the floor.

    Returns:
        Filtered list.  May be shorter than the input.
    """
    if threshold <= 0.0:
        return memories
    kept = []
    for m in memories:
        ce = m.get("_cross_encoder_score")
        if ce is None:
            # Missing CE score — always keep (fallback path / beyond top-k).
            kept.append(m)
        elif ce >= threshold:
            kept.append(m)
    return kept


def _dedup_by_content(memories: list[dict]) -> list[dict]:
    """Collapse repeated memories with identical content.

    Co-occurrence rows ("X and Y frequently modified together") are generated
    for every modification event, so the same content can accumulate dozens of
    rows with different ``created_at`` values.  Keep the first occurrence
    (highest score, since the list is already sorted descending before dedup).

    Dedup key: exact content string.  Two memories with the same content but
    different IDs are treated as duplicates; the first (highest-scored) survives.

    Args:
        memories: Input list, assumed sorted by score descending.

    Returns:
        Deduplicated list preserving insertion order of first occurrences.
    """
    seen_content: set[str] = set()
    result = []
    for m in memories:
        content = m.get("content", "")
        if content not in seen_content:
            seen_content.add(content)
            result.append(m)
    return result


# ---------------------------------------------------------------------------
# v5.80 helper — convert Candidate objects to raw dicts for fan-out output
# ---------------------------------------------------------------------------


def _candidates_to_dicts(candidates: list) -> list[dict]:
    """Convert Candidate objects to raw dicts, stamping _source and stripping embeddings.

    Shared by both the single-provider bypass and the multi-provider fuse path.

    - Stamps ``raw["_source"] = cand.type`` (idempotent for wiki; MemoryProvider
      does not set it, so this is the canonical write for memory results).
    - Strips ``embedding`` bytes — never returned to callers.

    Args:
        candidates: Candidate objects from a provider or fuse_candidates output.

    Returns:
        List of raw dicts with _source stamped and embedding removed.
    """
    pooled: list[dict] = []
    for cand in candidates:
        raw = cand.raw
        raw["_source"] = cand.type  # "memory" or "wiki" — idempotent for wiki
        raw.pop("embedding", None)
        pooled.append(raw)
    return pooled


# ---------------------------------------------------------------------------
# v6 BC-AC3a landscape recall — consensus_retrieve exposed as opt-in mode
# ---------------------------------------------------------------------------


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
# v6 T6 fan-out recall orchestrator (UNIFIED_RECALL_ENABLED=True path only)
# ---------------------------------------------------------------------------


def _fanout_recall(
    query: str,
    max_results: int,
    min_heat: float,
    directory: str,
    current_branch: str | None,
    default_branch: str | None,
    type_filter: str = "all",
    tags: list[str] | None = None,
) -> list[dict]:
    """Fan out recall to MemoryProvider + WikiProvider, fuse + dedup results.

    Called ONLY when UNIFIED_RECALL_ENABLED=True.  Flag-False takes the
    legacy body below — this function is never entered in production until
    the flag is explicitly enabled.

    Step 3 additions (directory scoping):
      - MemoryProvider applies Python-side is_directory_eligible filter.
      - WikiProvider applies Python-side is_directory_eligible filter.

    Step 4 additions (cross-type fusion):
      - Per-type quotas (RECALL_MEMORY_QUOTA / RECALL_WIKI_QUOTA) applied
        before pooling so neither source starves.
      - Cross-encoder rerank over query↔Candidate.content (GTE reranker via
        Retriever, falls back to native_score when unavailable).
      - Additive prior boost per type (RECALL_MEMORY_PRIOR_WEIGHT /
        RECALL_WIKI_PRIOR_WEIGHT) after CE scoring.
      - Cross-type provenance dedup: memory.id ∈ wiki.source_memory_ids →
        keep higher-CE candidate.

    Step 5 additions (type_filter routing):
      - type_filter="all" (default): both providers active.
      - type_filter="memory": only MemoryProvider, skip WikiProvider.
      - type_filter="wiki": only WikiProvider, skip MemoryProvider.

    NOTE: MemoryProvider, WikiProvider, fuse_candidates are module-level imports
    (NOT function-local) so monkeypatch targets bind correctly in tests. This
    directly fixes the Finding-C AttributeError failure mode from the first attempt.

    Args:
        query: Search query.
        max_results: Maximum results to return.
        min_heat: Minimum heat threshold forwarded to MemoryProvider.
        directory: Caller directory (required, validated upstream).
        current_branch: Active git branch or None.
        default_branch: Repo default branch or None.
        type_filter: One of {"all", "memory", "wiki"}. Selects provider subset.
        tags: Tag include filter for wiki retrieval. When set, triggers SQL pre-filter
              (search_wiki_vectors_tagged) and suppresses the default agent-prompt exclude.
              None (default) = general recall with agent-prompt exclusion active.

    Returns:
        List of raw memory/wiki dicts from providers, fused by CE rerank,
        deduped by content, trimmed to max_results.
    """
    scope = Scope(
        directory=directory,
        branch=current_branch,
        default_branch=default_branch,
        min_heat=min_heat,
    )

    # Candidate pool size: ask for more than max_results so fusion + dedup
    # has material to work with.  3× matches the legacy retriever multiplier.
    pool_limit = max_results * 3

    memory_candidates = []
    wiki_candidates = []

    # S6 kill-gate: when AGENT_PROMPT_LIBRARY_ENABLED is False the library is INERT.
    # Strip the agent-prompt include tag so the include path never fires; the exclude
    # below then naturally turns back on (tags becomes None) → tagged recall returns
    # nothing. Read the flag at call time off the cached settings singleton.
    # Read the flag via get_settings() fresh at call time (NOT the module-level
    # captured `settings`): the flag is runtime-flippable and the cached singleton
    # may be re-resolved, so the live lookup is the correct source of truth.
    if not get_settings().AGENT_PROMPT_LIBRARY_ENABLED and tags:
        tags = [t for t in tags if t != "agent-prompt"] or None

    # S3 precedence: tags=["agent-prompt"] suppresses the default exclude.
    # Without tags, general recall excludes agent-prompt pages to avoid polluting
    # general wiki results with tool-prompt fragments. S6: also exclude the global
    # agent-prompt-toc page (tag "agent-prompt-toc" ≠ "agent-prompt") — otherwise the
    # TOC would reintroduce the every-project leak S3 exists to kill. Targeted
    # recall(tags=["agent-prompt"]) still won't pull the TOC (SQL pre-filter on
    # "agent-prompt"), so excluding it here is safe for both directions.
    _wiki_exclude = None if tags else ["agent-prompt", "agent-prompt-toc"]

    # Memory provider — active when type_filter is "all" or "memory"
    if type_filter in ("all", "memory") and _st._retriever is not None:
        memory_provider = MemoryProvider(_st._retriever)
        memory_candidates = memory_provider.candidates(query, scope, limit=pool_limit)

    # Wiki provider — active when type_filter is "all" or "wiki".
    # Parity with the legacy path (recall() body): wiki blending is skipped for
    # episodic/temporal queries ("what happened yesterday"), which want memories
    # not reference docs. Only suppress under type="all" (the default that mirrors
    # legacy); an explicit type="wiki" honors the caller's stated intent.
    _skip_wiki_episodic = type_filter == "all" and _is_episodic_query(query)
    if type_filter in ("all", "wiki") and _st._wiki is not None and not _skip_wiki_episodic:
        wiki_provider = WikiProvider(_st._wiki, tags=tags, exclude_tags=_wiki_exclude)
        wiki_candidates = wiki_provider.candidates(query, scope, limit=pool_limit)

    # Step 4: fuse candidates — but ONLY when multiple providers are active.
    #
    # Single-provider bypass (v5.80 regression fix):
    # When type_filter is "memory" or "wiki", exactly one provider is active and
    # that provider already returns a properly-ranked list (MemoryProvider wraps
    # Retriever.recall() which applies WRRF + GTE ranking; WikiProvider wraps
    # WikiStore.query() which applies hybrid BM25+vector ranking). Calling
    # fuse_candidates on a single-provider pool cross-encoder-reranks the
    # candidates a SECOND time, producing a different order that degrades MRR
    # (measured: type=memory MRR dropped from 0.84 → 0.74 before this fix).
    #
    # Fix: when only one pool has candidates, return them in NATIVE order,
    # converting Candidate→dict with _source stamping. Skip fuse entirely.
    # This covers BOTH the explicit single-provider filters (type="memory"/
    # "wiki") AND type="all" where one pool came back empty (e.g. no relevant
    # wiki). Cross-type CE interleaving only makes sense with two non-empty
    # pools; with one pool, fusing CE-reranks it a SECOND time and reorders it,
    # which is exactly the 0.84→0.74 MRR regression this guard prevents.
    if not memory_candidates or not wiki_candidates:
        # Single non-empty pool: native ordering preserved, no CE double-rank.
        candidates = memory_candidates or wiki_candidates
        pooled = _candidates_to_dicts(candidates)
    else:
        # Multi-provider path (type="all"): cross-type CE fusion needed.
        fused = fuse_candidates(
            memory_candidates=memory_candidates,
            wiki_candidates=wiki_candidates,
            query=query,
            retriever=_st._retriever,
            max_results=max_results,
            settings=settings,
        )
        pooled = _candidates_to_dicts(fused)

    # Final dedup by content (handles any duplicates fusion didn't catch)
    pooled = _dedup_by_content(pooled)
    return pooled[:max_results]


def _record_recall_sr_transition(merged: list[dict]) -> None:
    """Record an SR (successor-representation) transition: previous recall → this one.

    Links the top MEMORY of the current recall to the top memory of the prior recall
    on the cognitive map (wiki rows are not map nodes). Split out of
    _apply_recall_side_effects to keep nesting within the I13 cap.
    """
    if _st._cognitive_map is None or not merged:
        return
    session_key = "default"
    top_id = next(
        (m.get("id") for m in merged if m.get("_source") != "wiki" and m.get("id") is not None),
        None,
    )
    if top_id is None:
        return
    prev_id = _st._last_recalled_ids.get(session_key)
    if prev_id is not None and prev_id != top_id:
        try:
            _st._cognitive_map.record_transition(prev_id, top_id, session_key)
            _st._cognitive_map.incremental_update(prev_id, top_id)
        except Exception:
            logger.debug("SR transition recording failed")
    _bounded_set(_st._last_recalled_ids, session_key, top_id)


def _apply_recall_side_effects(merged: list[dict], query: str, storage) -> None:
    """Post-retrieval bookkeeping shared by the legacy and fan-out recall paths.

    Boosts heat (+0.1) and updates last_accessed for each recalled MEMORY (wiki
    rows and synthetic dicts without a persisted id are skipped), records
    metamemory access, records SR (successor-representation) transitions on the
    cognitive map, captures the recall action, and ticks the auto-checkpoint
    counter.

    Extracted in v5.80 so the fan-out path (which early-returns from recall())
    performs the SAME side effects as the legacy path. Without this, fan-out
    recalls would not reinforce heat on access — breaking the heat-ranking model
    (regression caught by test_server::test_recall_boosts_heat under the flip).
    """
    now = storage._now_iso()
    thermo = _st._thermo
    for m in merged:
        mid = m.get("id")
        if mid is None or m.get("_source") == "wiki":
            continue
        new_heat = min(m.get("heat", 0.0) + 0.1, 1.0)
        storage.update_memory_heat(mid, new_heat)
        storage.update_memory_last_accessed(mid, now)
        m["heat"] = new_heat
        m["last_accessed"] = now
        if thermo is not None:
            thermo.record_access(mid, was_useful=True)

    # SR transitions: link previous recall → current recall.
    _record_recall_sr_transition(merged)

    # Action stream: log this recall operation.
    buffer = _st._buffer
    if buffer is not None:
        result_count = len(merged)
        buffer.capture_action(
            "recall",
            "",
            f"query='{query[:80]}' results={result_count}",
            f"found_{result_count}",
        )

    # Track tool call for auto-checkpoint interval.
    if _st._replay is not None:
        _st._replay.record_tool_call()


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
