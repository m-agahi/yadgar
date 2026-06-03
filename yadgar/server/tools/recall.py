"""recall MCP tool registration."""

from __future__ import annotations

import logging
import os

import yadgar.server._state as _st
from yadgar.config import get_settings
from yadgar.server._app import _tool
from yadgar.server._helpers import _bounded_set, _is_episodic_query
from yadgar.server.lifecycle import _get_embeddings, _get_storage

logger = logging.getLogger(__name__)

settings = get_settings()


@_tool()
def recall(  # noqa: C901 - cohesive: MCP tool — single entry point for all recall variants
    query: str,
    max_results: int = 5,
    min_heat: float = 0.0,
    profile: str | None = None,
    stage_overrides: dict[str, dict] | None = None,
) -> list[dict]:
    """Semantic + keyword search filtered by heat. Boosts accessed memories.

    profile (v5.31.1): optional retrieval profile name. When provided, routes
    through the v5.31.0 plugin pipeline instead of the legacy monolithic path.
    Valid values: "fast", "balanced", "full", "debug". When None (default),
    behavior is identical to pre-v5.31.1 — zero change for existing callers.

    stage_overrides (v5.31.1): per-call stage disable map, e.g.
    {"nli": {"enabled": False}}. Only used when profile is set. Passed
    through to Retriever.recall_via_pipeline(stage_overrides=...).

    Raises ValueError immediately (before any retrieval work) if profile is
    set to an unrecognised value.
    """
    import time as _time  # noqa: PLC0415

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

        # §25 Branch context — detect before retrieval so we can push the filter into
        # SurrealQL (C2) rather than post-filtering in Python.
        # Look up via yadgar.server (where tests patch these) so monkeypatches on
        # "yadgar.server._detect_branch" / "yadgar.server._get_default_branch" take effect.
        try:
            import sys as _sys  # noqa: PLC0415

            _cwd = os.getcwd()
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

        # Boost heat, update last_accessed, and record metamemory access
        now = storage._now_iso()
        thermo = _st._thermo
        for m in merged:
            new_heat = min(m["heat"] + 0.1, 1.0)
            storage.update_memory_heat(m["id"], new_heat)
            storage.update_memory_last_accessed(m["id"], now)
            m["heat"] = new_heat
            m["last_accessed"] = now
            if thermo is not None:
                thermo.record_access(m["id"], was_useful=True)

        # Record SR transitions: link previous recall → current recall
        if _st._cognitive_map is not None and merged:
            session_key = "default"
            top_id = merged[0]["id"]
            prev_id = _st._last_recalled_ids.get(session_key)
            if prev_id is not None and prev_id != top_id:
                try:
                    _st._cognitive_map.record_transition(prev_id, top_id, session_key)
                    _st._cognitive_map.incremental_update(prev_id, top_id)
                except Exception:
                    logger.debug("SR transition recording failed")
            _bounded_set(_st._last_recalled_ids, session_key, top_id)

        # Reconsolidation disabled: memories are never rewritten on retrieval.
        # Content integrity must be preserved exactly as stored.

        # Action stream: log this recall operation
        buffer = _st._buffer
        if buffer is not None:
            result_count = len(merged)
            buffer.capture_action(
                "recall",
                "",
                f"query='{query[:80]}' results={result_count}",
                f"found_{result_count}",
            )

        # Track tool call for auto-checkpoint interval
        if _st._replay is not None:
            _st._replay.record_tool_call()

        # Wiki blending — relevance-gated, skip for episodic/temporal queries
        if _st._wiki is not None and not _is_episodic_query(query):
            try:
                wiki_results = _st._wiki.query(query, max_results=5)
                qualifying = [wr for wr in wiki_results if wr.get("_retrieval_score", 0.0) > 0.3]
                # §25 branch filter: exclude wiki pages outside allowed branches
                qualifying = [wr for wr in qualifying if wr.get("branch") in _allowed_branches]
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
