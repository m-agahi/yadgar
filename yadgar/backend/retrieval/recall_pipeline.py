"""Recall pipeline — shared by the MCP tool handler and the backend /recall route.

This module contains the pipeline-orchestration functions that both the in-core
MCP handler and the backend route need.

WHY this module is safe to import in the backend process
---------------------------------------------------------
Importing ``yadgar.server.tools._recall_pipeline`` transitively fires
``yadgar/server/__init__.py`` (which imports ``_app``), building a FastMCP
server and calling ``setup_tracing("yadgar-core")``.  This DOES happen in the
backend process on the first ``/recall`` request — the module is NOT
"app-free" in the sense of avoiding that import chain.

The backend is safe because of startup ORDER and OTel's set-once guarantee:

  1. The backend calls ``setup_tracing("yadgar-backend")`` at startup (in
     ``embed_service.py`` lifespan) BEFORE its lazy ``server.*`` imports fire
     on the first ``/recall`` request.
  2. OpenTelemetry's global TracerProvider is set-once: once a provider is
     registered, ``trace.set_tracer_provider()`` silently no-ops on any
     subsequent call (OTel logs "Overriding of current TracerProvider is not
     allowed" and leaves the original provider in place).
  3. Therefore, when the transitive ``yadgar.server.__init__`` import later
     calls ``setup_tracing("yadgar-core")``, the backend's "yadgar-backend"
     provider is already registered and is NOT replaced.

**Fragile ordering constraint**: if any future top-level import in
``embed_service.py`` (or ``ml_client.py``) causes ``yadgar.server`` to load
BEFORE the lifespan's ``setup_tracing("yadgar-backend")`` call, the provider
would be registered as "yadgar-core" instead and the backend would emit spans
under the wrong service name.
``test_backend_tracing_provider_not_clobbered`` in the unit-test suite catches
this regression by running in a subprocess with a clean OTel state.

Functions moved here (cut from recall.py):
  - _apply_quality_floor
  - _dedup_by_content
  - _candidates_to_dicts
  - _fuse_with_span
  - _fanout_recall
  - _record_recall_sr_transition
  - _apply_recall_db_side_effects   (new: DB-side half of the former _apply_recall_side_effects)
  - _apply_recall_session_side_effects (new: core-local half)
  - _apply_recall_side_effects      (thin combiner: calls both halves in original order)

recall.py re-imports all of these so its existing call sites are unchanged.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import yadgar._shared.runtime.state as _st  # sibling; avoid server/__init__ re-entry (Car 1)
from yadgar._shared.config import get_settings
from yadgar._shared.observability.observe import observe
from yadgar._shared.retrieval.profiles import PROFILES

# Car 3 (folder-split #17): _bounded_set / _is_episodic_query moved from
# yadgar.server._helpers to yadgar._shared.runtime.recall_utils. They now live in
# _shared, so a module-level import is safe (no yadgar.server.__init__ re-entry /
# backend-recall circular import) and this pipeline no longer imports
# yadgar.server (a _shared→server edge).
from yadgar._shared.runtime.cpu import recall_gather_budget

# T2 Car E2: the SESSION-side bookkeeping half lives in _shared (dual: the core
# recall forwarder calls it after every forwarded recall; this module's
# _apply_recall_side_effects combiner calls it on the legacy/test path).
from yadgar._shared.runtime.recall_session import (  # noqa: F401 (combiner + re-export)
    _apply_recall_session_side_effects,
    _record_recall_sr_transition,
)
from yadgar._shared.runtime.recall_utils import _is_episodic_query
from yadgar.backend.retrieval.providers.base import Scope
from yadgar.backend.retrieval.providers.fusion import fuse_candidates
from yadgar.backend.retrieval.providers.memory import MemoryProvider
from yadgar.backend.retrieval.providers.wiki import WikiProvider

logger = logging.getLogger(__name__)

settings = get_settings()


# ---------------------------------------------------------------------------
# v5.62.0 recall quality helpers — quality floor + dedup
# ---------------------------------------------------------------------------


@observe(tier="hot", metric="tools.recall._apply_quality_floor")
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


@observe(tier="hot", metric="tools.recall._dedup_by_content")
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


@observe(tier="hot", metric="tools.recall._candidates_to_dicts")
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
        # Preserve existing _source annotations (e.g. "profile", "belief") that
        # carry semantic meaning beyond "memory"/"wiki" type bucket.  Only stamp
        # cand.type when _source is absent or already equals the bucket type so we
        # don't overwrite structured-knowledge provenance injected by reranking.
        existing_src = raw.get("_source")
        if existing_src not in (None, "memory", "wiki"):
            pass  # keep the structured-knowledge annotation (profile, belief, …)
        else:
            raw["_source"] = cand.type  # "memory" or "wiki" — idempotent for wiki
        raw.pop("embedding", None)
        pooled.append(raw)
    return pooled


def _fuse_with_span(memory_candidates, wiki_candidates, query, max_results, profile=None):
    """Cross-type CE fusion, grouped under a named ``recall.fanout.fuse`` span.

    v5.102: the multi-provider fusion runs the cross-type cross-encoder pass —
    the ~5.4s ``rpc.rerank.ce`` that dominates the post-``retrieval.recall`` tail
    of a warm recall trace. Wrapping it in a span attributes that cost under
    ``tool.recall`` in Tempo as a labelled node instead of leaving it as a loose
    sibling of ``retrieval.recall`` (the "trace gap" this change closes).

    Instrumentation only — the second CE pass itself is NOT touched: it is
    load-bearing for cross-type ranking quality (the single-provider bypass in
    ``_fanout_recall`` records double-CE dropping MRR 0.84→0.74). Extracted to a
    helper so ``_fanout_recall`` stays under the I13/I30 complexity caps.

    Phase 1 §3.1: profile="fast" threads through to fuse_candidates to gate the
    cross-type CE pass (fast = use native_score proxy, no CE inference).
    """
    from yadgar._shared.observability.tracing import span  # noqa: PLC0415

    with span(
        "recall.fanout.fuse",
        memory_candidates=len(memory_candidates),
        wiki_candidates=len(wiki_candidates),
    ):
        return fuse_candidates(
            memory_candidates=memory_candidates,
            wiki_candidates=wiki_candidates,
            query=query,
            retriever=_st._retriever,
            max_results=max_results,
            settings=settings,
            profile=profile,
        )


# ---------------------------------------------------------------------------
# v6 T6 fan-out recall orchestrator (UNIFIED_RECALL_ENABLED=True path only)
# ---------------------------------------------------------------------------


@observe(tier="hot", metric="tools.recall._apply_fanout_boosts")
def _apply_fanout_boosts(
    pooled: list[dict],
    query: str,
    profile: str | None = None,
) -> list[dict]:
    """Apply the postmortem/incident boost to fanout results.

    Phase 1 §5.2: mirrors recall.py:526-552 (legacy B5/B2 path) to close the
    parity gap where the default fanout path (B3/B4) lacked these boosts.
    ADR-0215 removed the C4 branch boost that used to live alongside it; the
    postmortem boost is unaffected. Sort is applied only when at least one boost
    fires — preserves the retriever's native order otherwise (important: avoids
    the double-sort regression from PR #143).

    Postmortem/incident boost: convex combination with POSTMORTEM_BOOST_FACTOR
    for memories tagged _postmortem or _incident when the query contains a
    postmortem action keyword (POSTMORTEM_BOOST_KEYWORDS).

    FANOUT_BOOST_SCOPE gate (§45):
      - "off"    → skip all boosts, return pooled unchanged.
      - "scoped" → apply boosts only when profile is not None (profile-origin
                   callers: hook=fast). Default — reproduces pre-forward-only
                   prod parity where hook path boosted, default path did not.
      - "global" → apply boosts always regardless of profile.

    Args:
        pooled: Deduplicated fanout result list (mutated in-place for boosts).
        query: Recall query string (for postmortem keyword check).
        profile: Retrieval profile that triggered this fanout (None = default path).

    Returns:
        The pooled list with boosts applied (same list, possibly re-sorted).
    """
    scope = settings.FANOUT_BOOST_SCOPE
    if scope == "off" or (scope == "scoped" and profile is None):
        return pooled

    _pm_boost_factor = settings.POSTMORTEM_BOOST_FACTOR
    _pm_keywords = settings.POSTMORTEM_BOOST_KEYWORDS
    if _pm_boost_factor > 0.0 and _pm_keywords:
        _query_lower = query.lower()
        _has_action_verb = any(kw in _query_lower for kw in _pm_keywords)
        if _has_action_verb:
            _pm_tags = {"_postmortem", "_incident"}
            _pm_boosted = False
            for m in pooled:
                _mem_tags = set(m.get("tags", []))
                if _mem_tags & _pm_tags:
                    base = min(float(m.get("_retrieval_score", m.get("heat", 0.0))), 1.0)
                    m["_retrieval_score"] = base + (1.0 - base) * _pm_boost_factor
                    _pm_boosted = True
            if _pm_boosted:
                pooled.sort(key=lambda m: m.get("_retrieval_score", 0.0), reverse=True)

    return pooled


@observe(tier="hot", metric="tools.recall._should_skip_wiki", span=False)
def _should_skip_wiki(
    query: str,
    type_filter: str,
    profile: str | None,
    deadline: float | None,
) -> bool:
    """Return True when the WikiProvider arm of the fanout must be skipped.

    All suppression rules apply ONLY under type_filter="all" (the default that
    mirrors the legacy path) — an explicit type="wiki" honors caller intent.

    Rules (any one suffices):
      - Episodic/temporal query ("what happened yesterday") — wants memories,
        not reference docs (legacy-path parity).
      - ADR-0077: the profile declares wiki=False (fast does — the wiki arm cost
        ~450ms per hook recall and pushed hook p50 to the 2.0s budget). Unknown
        profile strings fail open (wiki stays on).
      - ADR-0077: the client deadline is already exceeded — abort remaining
        stages and return the partial (memory-only) result rather than keep
        computing past client abandonment.
    """
    if type_filter != "all":
        return False
    if _is_episodic_query(query):
        return True
    if deadline is not None and time.monotonic() >= deadline:
        return True
    if profile is not None:
        profile_def = PROFILES.get(profile)
        if profile_def is not None and not profile_def.get("wiki", True):
            return True
    return False


@observe(tier="stage", metric="tools.recall._gather_provider_candidates", span=False)
def _gather_provider_candidates(
    tasks: list[tuple[str, Callable[[], list]]], budget: int | None = None
) -> dict[str, list]:
    """Run each provider's candidate fetch and return a {slot: result} dict.

    T3 Car 3 — CPU-aware bounded-parallel gather. Each task is ``(slot, fn)``
    where ``fn`` is a zero-arg callable returning that provider's candidate list.
    Results are keyed by the task's slot NAME, so the caller reads
    ``result["memory"]`` / ``result["wiki"]`` regardless of completion order —
    the byte-identity contract: the same input tasks yield the same result dict
    at ANY budget.

    Budget (max concurrent arms) defaults to ``recall_gather_budget()``:
      - 1 at ncpu ≤ 2 (or ``YADGAR_RECALL_PARALLELISM=1``) → run the tasks
        SEQUENTIALLY in listed order — byte-identical to the pre-Car-3 code path.
      - ≥ 2 → submit to a BOUNDED ThreadPoolExecutor (max_workers = min(budget,
        len(tasks))). The pool is bounded (never an unbounded fan-out — the
        ADR-0011-class onnx-thrash lesson) and torn down on return.

    A single task always runs inline (no thread spawn). Provider exceptions
    PROPAGATE (a swallowed error would silently drop a candidate pool and skew
    the fused result) — ``future.result()`` re-raises in the caller.

    Thread-safety of the parallel arms: MemoryProvider and WikiProvider share the
    one ``_st._storage`` StorageEngine, but in the deployed (server-mode) backend
    that engine reads via a thread-safe ``httpx.Client`` singleton doing stateless
    per-request HTTP POSTs (no locks, no shared cursor). The offload path ALREADY
    runs concurrent memory+wiki reads on that shared engine in production when
    ``YADGAR_OFFLOAD_TOOLS`` + ``TOOL_POOL_WORKERS`` > 1, so this is the same,
    already-exercised concurrency — not a new hazard. (Embedded surrealkv, used
    only in tests, is single-threaded; the parallel branch never fires there
    because the test/dev floor keeps budget=1 unless ncpu > 2.)
    """
    if budget is None:
        budget = recall_gather_budget()

    # Sequential floor: budget 1, or ≤ 1 real task. This is the exact pre-Car-3
    # behavior — tasks run in listed order, no executor involved.
    if budget <= 1 or len(tasks) <= 1:
        return {slot: fn() for slot, fn in tasks}

    # Bounded-parallel: fan out at most `budget` arms (capped at the task count).
    import contextvars  # noqa: PLC0415
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    workers = min(budget, len(tasks))
    results: dict[str, list] = {}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="yadgar-recall-gather") as pool:
        # Submit in listed order; collect by slot name so completion order is
        # irrelevant to the returned mapping (byte-identity). Capture the current
        # contextvars Context HERE (submitting thread — has the recall trace's
        # parent span) and run each arm inside it, so the provider's @observe span
        # nests under the recall trace across the executor boundary. A raw submit
        # would orphan it — the offload/side-effect-fork precedent.
        futures = {}
        for slot, fn in tasks:
            ctx = contextvars.copy_context()
            futures[slot] = pool.submit(ctx.run, fn)
        for slot, fut in futures.items():
            results[slot] = fut.result()  # re-raises any provider exception
    return results


@observe(tier="stage", metric="tools.recall._build_provider_tasks", span=False)
def _build_provider_tasks(  # noqa: PLR0913 — mirrors _fanout_recall's threaded params
    query: str,
    scope: Scope,
    pool_limit: int,
    type_filter: str,
    profile: str | None,
    deadline: float | None,
    tags: list[str] | None,
) -> list[tuple[str, Callable[[], list]]]:
    """Build the ``(slot, fetch)`` task list for the CPU-aware provider gather.

    Extracted from ``_fanout_recall`` (T3 Car 3) so the fan-out body stays under
    the I30 fn-loc cap and the gather-task construction is independently testable.
    Provider CONSTRUCTION runs inline here (cheap; keeps monkeypatch targets bound
    and the listed memory→wiki order); only each ``.candidates()`` storage-I/O
    call is deferred into a zero-arg task that the gather runs (sequentially at
    ncpu ≤ 2, bounded-parallel above).
    """
    tasks: list[tuple[str, Callable[[], list]]] = []

    # S6 kill-gate: when AGENT_PROMPT_LIBRARY_ENABLED is False the library is INERT.
    # Strip the agent-prompt include tag so the include path never fires; the exclude
    # below then naturally turns back on (tags becomes None) → tagged recall returns
    # nothing. Read the flag via get_settings() fresh at call time (NOT the
    # module-level captured `settings`): the flag is runtime-flippable.
    if not get_settings().AGENT_PROMPT_LIBRARY_ENABLED and tags:
        tags = [t for t in tags if t != "agent-prompt"] or None

    # S3 precedence: tags=["agent-prompt"] suppresses the default exclude. Without
    # tags, general recall excludes agent-prompt pages so tool-prompt fragments
    # don't pollute general wiki results (the every-project leak S3 exists to
    # kill). Targeted recall(tags=["agent-prompt"]) pulls the actual prompt
    # bodies; the old wiki-TOC page is gone (0047 Car I, D35a: retired,
    # kept-ignored pointer slug). The exclude stays "agent-prompt" only.
    wiki_exclude = None if tags else ["agent-prompt"]

    # Memory provider — active when type_filter is "all" or "memory".
    if type_filter in ("all", "memory") and _st._retriever is not None:
        memory_provider = MemoryProvider(_st._retriever, profile=profile, deadline=deadline)
        tasks.append(
            ("memory", lambda mp=memory_provider: mp.candidates(query, scope, limit=pool_limit))
        )

    # Wiki provider — active when type_filter is "all" or "wiki". Suppression
    # rules (episodic query / fast profile / deadline exceeded) live in
    # _should_skip_wiki and apply ONLY under type="all" — an explicit type="wiki"
    # honors the caller's stated intent.
    if (
        type_filter in ("all", "wiki")
        and _st._wiki is not None
        and not _should_skip_wiki(query, type_filter, profile, deadline)
    ):
        wiki_provider = WikiProvider(_st._wiki, tags=tags, exclude_tags=wiki_exclude)
        tasks.append(
            ("wiki", lambda wp=wiki_provider: wp.candidates(query, scope, limit=pool_limit))
        )

    return tasks


@observe(tier="stage", metric="tools.recall._fanout_recall", span=False)
def _fanout_recall(  # noqa: PLR0913 — 8 params allowlisted (I30); Phase 2 wraps params
    query: str,
    max_results: int,
    min_heat: float,
    project_id: str,
    type_filter: str = "all",
    tags: list[str] | None = None,
    profile: str | None = None,
    deadline: float | None = None,
) -> list[dict]:
    """Fan out recall to MemoryProvider + WikiProvider, fuse + dedup results.

    This IS the production recall path: ``embed_service`` calls it
    unconditionally. (Docstring corrected T4 Car 0 — it previously claimed
    ``UNIFIED_RECALL_ENABLED``-gated entry with a "legacy body below"; the flag
    has been default-on since v5.80 and no legacy body exists.)

    Car C7 (0047 §5 C7) — SCOPING MOVED INTO THE QUERY:
      - The scope is the caller's ``project_id``, not their directory.
      - Both providers push ``project_id = $p OR 'global' IN tags`` (plus, for
        wiki, the ``POLICY_BY_TYPE``-derived ``page_type`` exclusion) into the
        stage-1 ``WHERE``. Before C7 both applied a Python post-filter AFTER the
        query had already spent its LIMIT, so a scoped recall over a
        minority-share project could return zero rows while the DB held plenty.

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
        project_id: Caller's resolved project id (required, validated upstream).
            Car C7 re-keyed this parameter from ``directory``.
        type_filter: One of {"all", "memory", "wiki"}. Selects provider subset.
        tags: Tag include filter for wiki retrieval. When set, triggers SQL pre-filter
              (search_wiki_vectors_tagged) and suppresses the default agent-prompt exclude.
              None (default) = general recall with agent-prompt exclusion active.
        deadline: Monotonic deadline from the client's deadline_ms budget
              (ADR-0077): exceeded → remaining stages skipped, partial result.
              Threaded into MemoryProvider → Retriever.recall. None = off.

    Returns:
        List of raw memory/wiki dicts from providers, fused by CE rerank,
        deduped by content, trimmed to max_results.
    """
    # Car C7 (0047 §5 C7): the scope is now the PROJECT, not the directory, and
    # it is pushed into the stage-1 WHERE rather than post-filtered. ``tags``
    # rides along as ``opt_in_tags`` so the policy-derived ``page_type``
    # exclusion can be relaxed per request — without that,
    # ``recall(type="wiki", tags=["agent-prompt"])`` returns nothing.
    scope = Scope(
        project_id=project_id or "",
        min_heat=min_heat,
        opt_in_tags=tags,
    )

    # Candidate pool size: ask for more than max_results so fusion + dedup
    # has material to work with.  3× matches the legacy retriever multiplier.
    pool_limit = max_results * 3

    # T3 Car 3: build the provider tasks (construction inline), then gather them
    # CPU-aware — sequential at ncpu ≤ 2 (byte-identical to the pre-Car-3 inline
    # calls, listed order), bounded-parallel above. Slot names map results back
    # deterministically regardless of completion order (the byte-identity contract).
    _gather_tasks = _build_provider_tasks(
        query, scope, pool_limit, type_filter, profile, deadline, tags
    )
    _gathered = _gather_provider_candidates(_gather_tasks)
    memory_candidates = _gathered.get("memory", [])
    wiki_candidates = _gathered.get("wiki", [])

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
        fused = _fuse_with_span(
            memory_candidates, wiki_candidates, query, max_results, profile=profile
        )
        pooled = _candidates_to_dicts(fused)

    # Final dedup by content (handles any duplicates fusion didn't catch)
    pooled = _dedup_by_content(pooled)

    # Phase 1 §5.2: apply the postmortem/incident boost.
    # §45: profile threaded through so FANOUT_BOOST_SCOPE gate can discriminate
    # profile-origin (hook=fast) callers from default (profile=None) callers.
    pooled = _apply_fanout_boosts(pooled, query=query, profile=profile)

    return pooled[:max_results]


@observe(tier="stage", metric="tools.recall._compute_db_boost", span=False)
def _compute_db_boost(merged: list[dict], storage) -> tuple[list[int], str]:
    """RESPONSE-FEEDING half of the DB side-effects — must stay INLINE.

    Applies the +0.1 heat boost + last_accessed stamp to each recalled MEMORY
    IN PLACE (wiki rows and id-less synthetic dicts skipped) and records the
    thermo access (local in-process). These mutations are visible in the recall
    RESPONSE payload, so they CANNOT be deferred — deferring them would ship an
    un-boosted (non-byte-identical) response (T3 Car 2 behavior contract).

    Returns ``(boosted_ids, now)`` so the caller can either issue the batched DB
    write inline (``storage.boost_memories_access``) or fork it off the response
    path via ``recall_side_effects_fork.schedule_db_write``. This function does
    NOT issue that write — it is the cheap, latency-safe compute only.
    """
    now = storage._now_iso()
    thermo = _st._thermo

    boosted_ids: list[int] = []
    for m in merged:
        mid = m.get("id")
        if mid is None or m.get("_source") == "wiki":
            continue
        m["heat"] = min(m.get("heat", 0.0) + 0.1, 1.0)
        m["last_accessed"] = now
        boosted_ids.append(mid)
        if thermo is not None:
            thermo.record_access(mid, was_useful=True)
    return boosted_ids, now


@observe(tier="stage", metric="tools.recall._apply_recall_db_side_effects")
def _apply_recall_db_side_effects(merged: list[dict], query: str, storage) -> None:  # noqa: ARG001
    """DB-side bookkeeping half: heat boost + thermo access record.

    Runs inside the combined _apply_recall_side_effects for the in-core paths
    (legacy/landscape/tests). The flag-ON backend handler decomposes this into
    the inline ``_compute_db_boost`` + a forked ``boost_memories_access`` write
    (T3 Car 2) — see embed_service.recall_route.

    Writes performed:
      - thermo.record_access(mid, was_useful=True) per memory id (local in-process)
      - storage.boost_memories_access(ids, now) — one batched DB write

    Does NOT touch core-local session state (_cognitive_map, _last_recalled_ids,
    _buffer, _replay) — those are the domain of _apply_recall_session_side_effects.

    Args:
        merged: Result list from _fanout_recall.  Wiki rows (no integer id) are skipped.
        query: Original query string (unused here; kept for signature parity).
        storage: StorageEngine instance used for the batched boost write.
    """
    from yadgar._shared.observability.tracing import span  # noqa: PLC0415

    # span(): curated landmark name — inline CM can't auto-derive (ADR-0061 exception)
    with span("recall.side_effects.db", results=len(merged)):
        boosted_ids, now = _compute_db_boost(merged, storage)
        if boosted_ids:
            storage.boost_memories_access(boosted_ids, now)


@observe(tier="stage", metric="tools.recall._apply_recall_side_effects", span=False)
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

    v5.102: the per-memory heat + last_accessed writes are collapsed into ONE
    batched ``storage.boost_memories_access(ids, now)`` call (was 2 sequential
    SurrealDB round-trips × N results — the ~407ms tail of the recall trace).
    The whole segment is wrapped in a ``recall.side_effects`` span so this
    tool-wrapper work is attributed under ``tool.recall`` in Tempo instead of
    appearing as loose siblings of ``retrieval.recall``.  Heat math is unchanged
    (``min(heat + 0.1, 1.0)`` in Python == ``math::min([heat + 0.1, 1.0])`` in
    SurrealQL) — speed only, zero quality change.

    Train 1 split: this function calls BOTH halves in original order so the
    landscape (:557/:564) and legacy (:800) call sites, which always run in-core,
    are behavior-unchanged.  Only the flag-ON fanout path calls the two halves
    separately: DB half runs in the backend; session half runs in core on the
    returned results.
    """
    from yadgar._shared.observability.tracing import span  # noqa: PLC0415

    # span(): curated landmark name — inline CM can't auto-derive (ADR-0061 exception)
    with span("recall.side_effects", results=len(merged)):
        _apply_recall_db_side_effects(merged, query, storage)
        _apply_recall_session_side_effects(merged, query)
