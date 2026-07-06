"""recall MCP tool registration — Phase 2a: pure forwarder."""

from __future__ import annotations

import logging

import yadgar.server._state as _st
from yadgar.config import get_settings
from yadgar.observability.observe import observe
from yadgar.server._app import _tool

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

logger = logging.getLogger(__name__)

settings = get_settings()

# Valid type filter values for recall(type=) param (Step 5).
_VALID_RECALL_TYPES: frozenset[str] = frozenset({"all", "memory", "wiki"})

# Valid mode values for recall(mode=) param (v6 BC-AC3a landscape).
# None = normal (default). "landscape" = consensus_retrieve across astrocyte domains.
_VALID_RECALL_MODES: frozenset[str] = frozenset({"landscape"})


# ---------------------------------------------------------------------------
# Train 1 / Phase 2a: core-side forwarder to backend /recall endpoint
# ---------------------------------------------------------------------------


@observe(tier="boundary", name="tools.recall._forward_to_backend")
def _forward_to_backend(  # noqa: PLR0913 — 10 args match full recall signature
    query: str,
    max_results: int,
    min_heat: float,
    directory: str,
    current_branch: str | None,
    default_branch: str | None,
    type_filter: str,
    tags: list[str] | None,
    mode: str | None = None,
    profile: str | None = None,
    timeout_s: float = 120.0,
) -> list[dict]:
    """Forward recall to the backend /recall endpoint.

    Phase 2a: now forwards ALL params including mode (landscape dispatch) and
    profile (rerank_level — gated CE/NLI/MP and fusion-CE on the backend).

    Backend URL: derived from YADGAR_EMBED_URL (the same base URL used by
    RemoteMLClient for /rerank).  If YADGAR_EMBED_URL is not configured,
    raises RuntimeError (forward-only: no fallback).

    Branch args come from the caller's _detect_branch resolution — the backend
    must NOT call _detect_branch (no host .git in container).

    timeout_s: httpx request timeout. Defaults to 120.0 for the MCP recall path.
        The prompt-recall HOOK path passes a SHORT timeout (HOOK_RECALL_TIMEOUT_S)
        so a hung backend cannot keep the hook's bounded-pool thread alive past
        its budget (#81 pool-starvation guard — see hook-recall-forward plan).

    Raises:
        RuntimeError: if YADGAR_EMBED_URL is not configured.
        httpx.HTTPError: if the backend request fails.
    """
    import os  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    backend_base = os.environ.get("YADGAR_EMBED_URL", "").rstrip("/")
    if not backend_base:
        raise RuntimeError(
            "YADGAR_EMBED_URL is not set; cannot forward recall to backend. "
            "Phase 2a: recall is forward-only — no in-core fallback."
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
        "mode": mode,
        "profile": profile,
    }

    resp = httpx.post(
        f"{backend_base}/recall",
        json=payload,
        headers=headers,
        timeout=timeout_s,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("results", [])


@_tool(always_load=True)
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
            (wiki pages only). Raises ValueError on unrecognised value.
        profile: Optional retrieval profile — "fast", "balanced", "full", "debug".
            Controls rerank stages on the backend (fast = no CE/NLI/MP; balanced =
            CE+MP; full = +NLI). Raises ValueError on unrecognised value.
        stage_overrides: Per-call stage disable map, e.g. {"nli": {"enabled": False}}.
        branch_hint: Caller-supplied branch name. Fallback when daemon cannot detect
            the branch (container scenario). Resolution order:
            _detect_branch(directory) → branch_hint → None.
        mode: Opt-in recall mode. None (default) = normal precise recall.
            "landscape" = broad cross-domain recall via consensus_retrieve across all
            astrocyte domains — each domain votes independently and results are merged
            into one ranked list with consensus_score and voting_domains per row.
            Use mode="landscape" for broad or exploratory queries where cross-domain
            breadth and diversity matter more than precise top-k ranking.
            Raises ValueError on unrecognised value.
        tags: Tag include filter for wiki results. When set, triggers SQL pre-filter
            in WikiStore.query() — brute-force cosine over pages tagged with tags[0].
            Also suppresses the default agent-prompt exclude.
            None (default) = general recall with agent-prompt pages excluded.

    Returns:
        List of memory/wiki dicts ranked by relevance + heat. landscape mode adds
        consensus_score (float) and voting_domains (list[str]) per row.

    Raises:
        ValueError: if directory is empty, or profile/type/mode is unrecognised.
        RuntimeError: if the backend is unreachable (YADGAR_EMBED_URL unset or HTTP error).
    """
    import time as _time  # noqa: PLC0415

    # v5.65 Fix D: hard-require directory — MUST be first check in body.
    _dir_stripped = (directory or "").strip().rstrip("/")
    if not _dir_stripped:
        raise ValueError(
            "recall: directory is required (caller must supply project dir; "
            "container cannot detect it via os.getcwd())"
        )

    # Validate type param early — before any expensive setup.
    if type not in _VALID_RECALL_TYPES:
        raise ValueError(
            f"recall: type={type!r} is not valid. Valid values: {sorted(_VALID_RECALL_TYPES)}"
        )

    # Validate mode param early.
    if mode is not None and mode not in _VALID_RECALL_MODES:
        raise ValueError(
            f"recall: mode={mode!r} is not valid. Valid values: {sorted(_VALID_RECALL_MODES)} or None"
        )

    # Validate profile BEFORE any expensive setup or DB access.
    if profile is not None:
        from yadgar.retrieval.profiles import _VALID_PROFILES  # noqa: PLC0415

        if profile not in _VALID_PROFILES:
            raise ValueError(
                f"Unknown retrieval profile {profile!r}. Valid profiles: {sorted(_VALID_PROFILES)}"
            )

    _recall_t0 = _time.monotonic()
    merged: list[dict] = []  # P11 Bug A fix: init before try so finally sees it

    try:
        # Record activity on consolidation engine
        if _st._consolidation is not None:
            _st._consolidation.record_activity()

        # Branch context — detect before forwarding so we pass resolved branch to backend.
        # backend must NOT call _detect_branch (no host .git in container).
        try:
            import sys as _sys  # noqa: PLC0415

            _cwd = _dir_stripped
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
            _default_branch = None

        # branch_hint fallback — mirrors wiki_read (v5.42.6 F1).
        if not _current_branch and branch_hint:
            _current_branch = branch_hint

        # v5.96.0: shadow recall result-cache hit-rate counter (instrumentation ONLY).
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

        # Phase 2a: forward-only — raise loud on backend error (no in-core fallback).
        merged = _forward_to_backend(
            query=query,
            max_results=max_results,
            min_heat=min_heat,
            directory=_dir_stripped,
            current_branch=_current_branch,
            default_branch=_default_branch,
            type_filter=type,
            tags=tags,
            mode=mode,
            profile=profile,
        )
        # Session-side bookkeeping runs in core (SR transitions, buffer, replay).
        # DB-side bookkeeping (heat boost, thermo) already ran in the backend.
        _apply_recall_session_side_effects(merged, query)
        return merged

    finally:
        # P11 Bug A fix: observe duration in finally — fires on success AND exception.
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
# Locally needed helpers re-exported for backwards compat
# ---------------------------------------------------------------------------
from yadgar.server._helpers import _is_episodic_query  # noqa: E402,F401
