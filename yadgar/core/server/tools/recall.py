"""recall MCP tool registration — Phase 2a: pure forwarder."""

from __future__ import annotations

import logging

import yadgar._shared.runtime.state as _st
from yadgar._shared.config import get_settings
from yadgar._shared.observability.observe import observe

# T2 Car E2: the recall PIPELINE (retrieval executor) sank to
# yadgar.backend.retrieval.recall_pipeline — core imports ONLY the session-side
# bookkeeping half (SR transitions / action buffer / auto-checkpoint tick),
# which is _shared by the dual-import law. The old F401 re-exports of the
# pipeline internals are gone: core must not bind the executor.
from yadgar._shared.runtime.recall_session import _apply_recall_session_side_effects

# T3 Car 2: the session half (SR transition storage writes + action buffer +
# replay tick) is deferred off the tool-response critical path via a single-FIFO
# worker (per-session ordering preserved). Imported under a private alias so the
# integration test can patch the seam.
from yadgar._shared.runtime.recall_side_effects_fork import (
    submit_session_side_effect as _submit_session_side_effect,
)
from yadgar.core.server._app import _tool

# Car M (0047 §7, §16.6): cross-project ``project=`` override. Resolves the
# effective project_id (override → session → directory → "global") so the
# recall scope can be addressed by project_id even when working in another
# project's tree.
from yadgar.core.server.tools._project_param import (
    InvalidProjectOverrideError,
    resolve_effective_project,
)

logger = logging.getLogger(__name__)

settings = get_settings()

# Valid type filter values for recall(type=) param (Step 5).
_VALID_RECALL_TYPES: frozenset[str] = frozenset({"all", "memory", "wiki"})

# Valid mode values for recall(mode=) param (v6 BC-AC3a landscape).
# None = normal (default). "landscape" = consensus_retrieve across astrocyte domains.
_VALID_RECALL_MODES: frozenset[str] = frozenset({"landscape"})


# ---------------------------------------------------------------------------
# task:0085 — output-size shaping (presentation-only, runs AFTER retrieval)
# ---------------------------------------------------------------------------
# recall() used to return the backend rows raw, with `max_results` as the only
# lever — a ROW-COUNT proxy for a BYTE problem, so a 3-row recall can be larger
# than a 10-row one. On an unlucky topic a single call emitted ~78 KB, exceeded
# the harness tool-output cap and came back unusable, pushing agents off the
# memory system and back to grep.
#
# Shaping is strictly presentational: it runs after retrieval, rerank and
# fusion, so ranking is untouched.

# Scoring / thermodynamic / write-side internals no caller reads.
#
# DENYLIST, not allowlist — deliberate. An allowlist silently deletes any field
# the retrieval pipeline adds later, and this pipeline adds fields often. The
# live trap: mode="landscape" stamps `consensus_score` / `voting_domains` per row
# (_shared/astrocyte_pool/astrocyte_pool.py:330-331), a documented part of this
# tool's return contract. With a denylist new fields default to VISIBLE, and the
# hard size guarantee comes from the byte budget below rather than from the
# projection — making the projection a pure constant-factor win at zero
# correctness risk.
#
# `contextual_prefix` is write-side-only: generated at ingest and concatenated to
# build the embedding text (backend/write_exec/_memorize_phases/_phase_embed.py:70-75,
# backend/curation/ingestion.py:105). Nothing on the retrieval or hook-render
# path reads it back, and its [Project:]/[Tags:] segments merely restate
# `directory_context` and `tags`, which are already structured fields on the row.
#
# `original_content` is denylisted deliberately: on a compressed memory it holds
# the full pre-compression text and would roughly DOUBLE the row.
_RECALL_PROJECTION_DENYLIST: frozenset[str] = frozenset(
    {
        "contextual_prefix",
        "vector_clock",
        "sr_x",
        "sr_y",
        "plasticity",
        "stability",
        "excitability",
        "last_excitability_update",
        "cofire_prior",
        "graph_prior",
        "surprise_score",
        "emotional_valence",
        "reconsolidation_count",
        "last_reconsolidated",
        "slot_index",
        "source_episode_id",
        "file_hash",
        "embedding_model",
        "compression_level",
        "access_count_since_decay",
        "original_content",
        "valid_until",
        "_rerank_score",
        "_cross_encoder_score",
        "_retrieval_confidence",
        "_chunk_id",
        "_position_reason",
        "wiki_schema_version",
    }
)


@observe(exempt="single resolve + 2 string operations; no I/O — called once at the recall boundary")
def _resolve_project_for_recall(
    *,
    project: str | None,
    directory: str | None,
) -> tuple[str, str]:
    """Car M: resolve the effective project_id and the wire-directory sentinel.

    Returns ``(effective_project_id, directory_sentinel)``:
      * ``effective_project_id`` — the resolved project namespace (override >
        session > directory > ``"global"``).
      * ``directory_sentinel`` — never empty. If the caller did not supply a
        usable directory, falls back to ``effective_project_id`` so the
        backend's RecallRequest contract (``directory: str`` non-empty) holds.

    Raises ``ValueError`` on a malformed ``project=`` — the type-level guard
    fires here so the MCP boundary surfaces a clean error envelope.
    """
    try:
        effective_project_id = resolve_effective_project(
            project=project,
            directory=directory,
            session_project=None,
            tool="recall",
        )
    except InvalidProjectOverrideError as exc:
        raise ValueError(f"recall: {exc}") from exc

    _dir_stripped = (directory or "").strip().rstrip("/") if directory else ""
    # Car C7 — THE GUARD IS DELETED, and this is the re-key C13d flagged.
    #
    # C13d measured the old ``if not _dir_stripped and not project`` branch as
    # DEAD both ways round: with ``project`` absent the resolver above raises
    # ``UnresolvedProjectError`` first (C5), and with ``project`` present the
    # second condition is false. It was retained pending C7's decision on this
    # tool's scope. That decision is made: the scope key is ``project_id``, the
    # resolver is its single chokepoint, and it already fails loud. A second
    # guard restating a weaker version of the same rule — in terms of the
    # DIRECTORY, which no longer scopes anything — could only ever be dead code
    # or a contradiction, so it goes.
    #
    # The directory sentinel below stays: the backend still carries ``directory``
    # as the caller's physical path (non-scoping), and it must be non-empty.
    if not _dir_stripped:
        _dir_stripped = effective_project_id
    return effective_project_id, _dir_stripped


@observe(exempt="trivial dict-lookup string builder; no I/O — and called per-row inside a loop")
def _fetch_hint(row: dict) -> str | None:
    """Exact-ID recovery path for a truncated row.

    Branches on ``_source``, NOT on key presence: wiki rows carry BOTH ``slug``
    and ``id``, so a ``"slug" in row`` test would mislabel memory rows that ever
    gain a slug and vice versa. Returns None when the row carries no usable
    identifier — the marker then omits `fetch` rather than lying about it.
    """
    if row.get("_source") == "wiki":
        slug = row.get("slug")
        if slug:
            return f'wiki_read("{slug}")'
        page_id = row.get("id")
        return f"wiki_get({page_id})" if page_id is not None else None
    mem_id = row.get("id")
    return f"memory_get({mem_id})" if mem_id is not None else None


@observe(exempt="single json.dumps len; no I/O — and called per-row inside a loop")
def _row_bytes(row: dict) -> int:
    """Compact-JSON size of one row, used for the total-byte budget."""
    import json  # noqa: PLC0415

    return len(json.dumps(row, separators=(",", ":"), default=str).encode("utf-8"))


@observe(tier="stage", metric="tools.recall._shape_recall_results")
def _shape_recall_results(rows: list[dict], *, max_chars: int, max_bytes: int) -> list[dict]:
    """Bound the serialised size of a recall result set.

    Three mechanisms, because memory rows and wiki rows fail differently:
    memory rows are METADATA-heavy (a measured real row spent 38.8% of its
    4132 B on internals), wiki rows are CONTENT-heavy (full page bodies). Only
    projection touches the first; only a content cap touches the second.

    1. Denylist projection — drops `_RECALL_PROJECTION_DENYLIST` fields.
    2. Per-row content cap — over-cap `content` is cut to `max_chars` and the row
       gains ONE visible `_truncated` marker: ``{"kept": N, "total": M,
       "fetch": "memory_get(<id>)"}``. Visibility is the point. The status quo is
       the harness cutting mid-JSON, opaquely, with no recovery path; the marker
       instead tells the model that the row is partial, HOW MUCH is missing, and
       how to pull the rest by exact ID — and the JSON stays well-formed. This
       improves on `recent_memories` (admin_other.py:220-221), which sets a bare
       "..." with neither count nor fetch hint.
    3. Total-byte backstop — per-row capping is not a hard bound (max_results=50
       x 1.9 KB still overflows). Rows are walked in RANK ORDER accumulating
       serialised size; once the budget is exhausted the remaining low-ranked
       rows are dropped behind one trailing ``_dropped`` marker. Dropping whole
       low-ranked rows beats truncating everything further — the top-ranked hits
       are what the caller came for, and CE rank order already says which rows
       are worth least. Same shape as the existing Cowan overflow behaviour
       (_shared/metacognition/cognitive_load.py:142).

    Never mutates `rows` or the dicts inside it: the deferred session-side-effect
    closure holds those same objects and needs them UNTRIMMED. Every returned row
    is a fresh shallow copy.

    Args:
        rows: Backend result rows, in rank order.
        max_chars: Per-row content cap. <= 0 disables content capping.
        max_bytes: Total serialised budget. <= 0 disables the backstop.

    Returns:
        A new list of new dicts. Rows under the cap are byte-identical to their
        input modulo the denylist — no `_truncated` key, so small results stay
        fully transparent.
    """
    shaped: list[dict] = []
    for row in rows:
        new_row = {k: v for k, v in row.items() if k not in _RECALL_PROJECTION_DENYLIST}
        content = new_row.get("content")
        if max_chars > 0 and isinstance(content, str) and len(content) > max_chars:
            new_row["content"] = content[:max_chars]
            marker: dict = {"kept": max_chars, "total": len(content)}
            hint = _fetch_hint(row)
            if hint is not None:
                marker["fetch"] = hint
            new_row["_truncated"] = marker
        shaped.append(new_row)

    if max_bytes <= 0 or not shaped:
        return shaped

    kept: list[dict] = []
    used = 0
    for idx, row in enumerate(shaped):
        size = _row_bytes(row)
        # `and kept` — the top hit always survives, however large. Returning an
        # empty list under a tiny budget would be strictly worse than one big row.
        if kept and used + size > max_bytes:
            kept.append(
                {
                    "_dropped": {
                        "rows": len(shaped) - idx,
                        "reason": "total_byte_budget",
                        "budget": max_bytes,
                    }
                }
            )
            return kept
        used += size
        kept.append(row)
    return kept


@observe(tier="stage", metric="tools.recall._resolve_shape_limit")
def _resolve_shape_limit(config_key: str, settings_field: str, directory: str) -> int:
    """Resolve a shaping knob: per-directory ADR-0163 row → global Settings default.

    `get_settings()` is called here rather than read off the module-level
    `settings` binding (line 29) — that binding is frozen at import, so an env
    override applied later would never reach it.

    Reads the runtime-config store via the plain resolver (`_runtime_config`),
    NOT the `@_tool`-decorated `config_get`, so no MCP tool dispatches inside a
    tool. Never raises: a bad row or a dead store falls back to the default.
    """
    from yadgar._shared.config import get_settings as _get_settings  # noqa: PLC0415

    default = int(getattr(_get_settings(), settings_field))
    try:
        from yadgar.core.server.tools._runtime_config import (
            config_get as _resolver_get,  # noqa: PLC0415
        )

        value = _resolver_get(config_key, directory=directory, default=default)
        return int(value)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Train 1 / Phase 2a: core-side forwarder to backend /recall endpoint
# ---------------------------------------------------------------------------


@observe(tier="boundary", metric="tools.recall._forward_to_backend")
def _forward_to_backend(  # noqa: PLR0913 — 11 args match full recall signature
    query: str,
    max_results: int,
    min_heat: float,
    directory: str,
    type_filter: str,
    tags: list[str] | None,
    mode: str | None = None,
    profile: str | None = None,
    timeout_s: float = 120.0,
    deadline_ms: int | None = None,
    project_id: str | None = None,
    unscoped: bool = False,
) -> list[dict]:
    """Forward recall to the backend /recall endpoint.

    Phase 2a: now forwards ALL params including mode (landscape dispatch) and
    profile (rerank_level — gated CE/NLI/MP and fusion-CE on the backend).

    Car M (0047 §7, §16.6): the ``project_id`` parameter is the cross-project
    override (resolve_effective_project → override → session → directory →
    "global"). When supplied, the backend scopes memories by ``project_id``
    and keeps the global wiki blend global (post-L semantics — pre-L only
    memories written AFTER Car M carry ``project_id``). When ``None`` the
    payload omits the key (wire-compatible with older backends; RecallRequest
    is extra="forbid" so the field is conditional).

    Backend URL: derived from YADGAR_EMBED_URL (the same base URL used by
    RemoteMLClient for /rerank).  If YADGAR_EMBED_URL is not configured,
    raises RuntimeError (forward-only: no fallback).

    timeout_s: httpx request timeout. Defaults to 120.0 for the MCP recall path.
        The prompt-recall HOOK path passes a SHORT timeout (HOOK_RECALL_TIMEOUT_S)
        so a hung backend cannot keep the hook's bounded-pool thread alive past
        its budget (#81 pool-starvation guard — see hook-recall-forward plan).

    deadline_ms: client compute budget forwarded to the backend (ADR-0077).
        When set, the backend converts it to a monotonic deadline and aborts
        remaining pipeline stages once exceeded (partial results) — so a hook
        whose httpx client already gave up does not keep the backend computing.
        Included in the payload ONLY when not None (wire-compatible with older
        backends whose RecallRequest is extra="forbid"). MCP recall path leaves
        it None.

    unscoped: Car H1 (§1.3) — the caller's DELIBERATE whole-corpus read.
        Included in the payload ONLY when True (same wire-compat pattern as
        ``deadline_ms``). ONLY ``_forward_hook_recall`` (core/server/http.py)
        sets this, and only when ``hook_project_id`` resolved to the explicit
        "no project, no directory" case — never inferred from a falsy
        ``project_id`` here, which would silently resurrect the exact failure
        mode Car H1 closed (an unresolved caller looking identical to a
        deliberate one).

    Raises:
        RuntimeError: if YADGAR_EMBED_URL is not configured.
        httpx.HTTPError: if the backend request fails.
    """
    import os  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    from yadgar.core.install.auth_token import resolve_auth_token  # noqa: PLC0415

    backend_base = os.environ.get("YADGAR_EMBED_URL", "").rstrip("/")
    if not backend_base:
        raise RuntimeError(
            "YADGAR_EMBED_URL is not set; cannot forward recall to backend. "
            "Phase 2a: recall is forward-only — no in-core fallback."
        )

    # Car 9: route through the ONE sanctioned bearer-token resolver (env var,
    # else secrets.env) rather than a bare os.environ.get — matches
    # core/forward.py's Car 5 fix for the same forward-only-call shape.
    token = resolve_auth_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    payload: dict = {
        "query": query,
        "directory": directory,
        "max_results": max_results,
        "min_heat": min_heat,
        "type": type_filter,
        "tags": tags,
        "mode": mode,
        "profile": profile,
    }
    if deadline_ms is not None:
        payload["deadline_ms"] = deadline_ms
    if project_id is not None:
        payload["project_id"] = project_id
    if unscoped:
        payload["unscoped"] = unscoped

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
    directory: str | None = None,
    type: str = "all",  # noqa: A002 — shadows built-in but matches MCP schema convention
    mode: str | None = None,
    tags: list[str] | None = None,
    max_chars: int | None = None,
    *,
    project: str | None = None,
) -> list[dict]:
    """Primary semantic + keyword retrieval tool. Use for discovery and context loading.

    Prefer recall() over memory_get()/wiki_get() when you don't already have a
    numeric ID — those tools are for direct ID lookups, not search. Prefer
    recall(type="wiki") over wiki_query() for wiki-only searches.

    Car M (0047 §7, §16.6): the ``project=`` override lets a caller address
    another project's memory namespace without leaving the current working
    tree. Precedence: ``project`` (override) > ``session_project`` (Car E
    SessionStart context) > ``directory``-derived (Car A0
    ``derive_project_id``) > ``"global"`` fallback. When BOTH ``project`` and
    ``directory`` are supplied, ``project`` wins and ``directory`` is logged-
    and-ignored. The resolved project_id is forwarded to the backend
    (``payload["project_id"]``) so post-Car-L scoping lands cleanly; pre-L
    the parameter is just carried through. The non-string / empty guards are
    enforced at the type level (raise ``InvalidProjectOverrideError``); the
    deep "is this project_id in the registry?" check lives at the backend
    write path (Car A0 `_ensure_project_exists_sync`, §15 / ADR-0078).

    Args:
        query: Search text. Combined semantic (embedding) + keyword scoring.
        directory: REQUIRED when ``project`` is None. Host-side project
            directory (e.g. "/home/user/myapp"). Results are scoped to this
            directory plus global wiki pages. Do NOT omit AND pass empty —
            daemon runs in a container and cannot detect the real path via
            os.getcwd(). Raises ValueError if absent/empty.
            Ignored with a logged INFO when ``project`` is supplied
            (project wins — §9 [VERIFY]).
        project: OPTIONAL cross-project override (Car M). When supplied,
            validation runs through ``resolve_effective_project``: must be a
            non-empty string. The backend uses this to scope memories
            (post-Car-L); wiki results stay global regardless (wiki pages
            are not project-stamped in the current model).
        max_results: Max results to return (default 5). Higher = slower + more tokens;
            keep <=10 for targeted lookups, <=20 for broad exploration.
        min_heat: Heat floor (default 0.0 = no filter). Pass >0.5 to restrict to
            stable, frequently-accessed memories only.
        type: Source type filter — "all" (default), "memory" (memories only), "wiki"
            (wiki pages only). Raises ValueError on unrecognised value.
        profile: Optional retrieval profile — "fast", "balanced", "full", "debug".
            Controls rerank stages on the backend (fast = no CE/NLI/MP; balanced =
            CE+MP; full = +NLI). Raises ValueError on unrecognised value.
        mode: Opt-in recall mode. None (default) = normal precise recall.
            "landscape" = broad cross-domain recall via consensus_retrieve across all
            astrocyte domains — each domain votes independently and results are merged
            into one ranked list with consensus_score and voting_domains per row.
            Use mode="landscape" for broad or exploratory queries where cross-domain
            breadth and diversity matter more than precise top-k ranking.
            Raises ValueError on unrecognised value.
        tags: Tag include filter, applied to BOTH result types (ledger task 82 —
            before it, this reached wiki results ONLY and memory rows came back
            unfiltered, with the tag token silently scored as ordinary text).
            Wiki: triggers the SQL pre-filter in WikiStore.query() — brute-force
            cosine over pages tagged with tags[0] — and suppresses the default
            agent-prompt exclude. Memory: rows must carry EVERY listed tag on
            their ``tags`` field (conjunctive); a row that merely mentions a tag
            in its content does not match.
            None (default) = general recall with agent-prompt pages excluded.

            NOT AN ENUMERATION. This filters the ranked candidate pool the query
            produced; it does not scan the tag index. Which tagged rows enter
            that pool is decided by the query and ``max_results``, so
            ``tags=["_anchor"]`` returns the best-matching anchors, never
            "every anchor in this project" — no ``max_results`` makes it
            exhaustive. Use ``audit_anchors`` (own gather) or ``db_inspect``
            (direct predicate) when completeness is what you need.
        max_chars: Per-row content cap, overriding the configured default for
            this call only. None (default) resolves per-directory config, then
            the RECALL_MAX_CONTENT_CHARS setting (1200). Raise it for a
            deliberate deep-dive that wants full row bodies. Must be > 0.

    Returns:
        List of memory/wiki dicts ranked by relevance + heat. landscape mode adds
        consensus_score (float) and voting_domains (list[str]) per row.

        Output is size-bounded (task:0085). Scoring/thermodynamic internals are
        projected away. A row whose content exceeded the cap carries
        `_truncated: {kept, total, fetch}` — `fetch` is the exact call
        (`memory_get(<id>)` / `wiki_read("<slug>")`) that returns the full row,
        so a truncated hit is recoverable rather than silently lossy. Rows under
        the cap carry no marker. If the whole result set exceeds the total-byte
        budget, the lowest-ranked rows are dropped and a single trailing
        `{"_dropped": {rows, reason, budget}}` object is appended.

    Raises:
        ValueError: if directory is empty, profile/type/mode is unrecognised, or
            max_chars is <= 0.
        RuntimeError: if the backend is unreachable (YADGAR_EMBED_URL unset or HTTP error).
    """
    import time as _time  # noqa: PLC0415

    # Car M: resolve the effective project_id (override → session → directory)
    # AND the wire-directory sentinel in one helper. Type-level guard fires
    # here so a malformed ``project=`` surfaces as ValueError (the tool
    # boundary never raises InvalidProjectOverrideError). When ``project`` was
    # supplied, the resolve helper also logs-and-ignores ``directory`` per §9
    # [VERIFY] (project wins).
    effective_project_id, _dir_stripped = _resolve_project_for_recall(
        project=project,
        directory=directory,
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

    # Validate max_chars early — alongside the other guards, before any I/O.
    if max_chars is not None and max_chars <= 0:
        raise ValueError(f"recall: max_chars={max_chars!r} must be > 0 (or None for the default)")

    # Validate profile BEFORE any expensive setup or DB access.
    if profile is not None:
        from yadgar._shared.retrieval.profiles import _VALID_PROFILES  # noqa: PLC0415

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

        # Phase 2a: forward-only — raise loud on backend error (no in-core fallback).
        #
        # Car C7 (0047 §5 C7) — THE RESOLVED PROJECT IS ALWAYS SENT NOW.
        # Car M shipped it only when the caller passed an explicit ``project=``
        # override (``... if effective_project_id and project is not None else
        # None``). That was survivable while the backend still scoped on
        # ``directory``; it is not survivable now that ``project_id`` IS the
        # scope key and ``RecallRequest`` requires it. Under the old condition
        # every ordinary ``recall(directory=…)`` — which is nearly all of them,
        # since only a cross-project lookup passes ``project=`` — would omit the
        # field and come back HTTP 422 against an ``extra="forbid"`` model.
        # The resolver above already raises ``UnresolvedProjectError`` when it
        # cannot produce a value (C5), so there is nothing left to guard.
        _project_payload = effective_project_id
        merged = _forward_to_backend(
            query=query,
            max_results=max_results,
            min_heat=min_heat,
            directory=_dir_stripped,
            type_filter=type,
            tags=tags,
            mode=mode,
            profile=profile,
            project_id=_project_payload,
        )
        # task:0085 — bound the RETURNED payload. Shaping is presentation-only:
        # it runs after retrieval/rerank/fusion, so ranking is untouched.
        #
        # `merged` MUST NOT be rebound here. The deferred closure below closes
        # over the NAME (Python closures capture variables, not values — the
        # pre-existing "captured by value" comment was wrong), so rebinding would
        # silently feed the session side-effects the shaped rows, which are
        # missing exactly the fields SR transitions and the action buffer read.
        # Rebinding would also make the `finally` block count the `_dropped`
        # marker as a result row. Hence a separate name.
        _shaped = _shape_recall_results(
            merged,
            max_chars=(
                max_chars
                if max_chars is not None
                else _resolve_shape_limit(
                    "recall.max_content_chars", "RECALL_MAX_CONTENT_CHARS", _dir_stripped
                )
            ),
            max_bytes=_resolve_shape_limit(
                "recall.max_total_bytes", "RECALL_MAX_TOTAL_BYTES", _dir_stripped
            ),
        )

        # Session-side bookkeeping runs in core (SR transitions, buffer, replay).
        # DB-side bookkeeping (heat boost, thermo) already ran in the backend.
        # T3 Car 2: defer the session half off the response critical path — the
        # SR transition storage writes are I/O on the 1-CPU core and were
        # blocking the tool response. The single-FIFO worker preserves the
        # per-session SR from→to chain ordering. The closure gets the UNTRIMMED
        # `merged` rows; the caller gets the shaped copies.
        _submit_session_side_effect(lambda: _apply_recall_session_side_effects(merged, query))
        return _shaped

    finally:
        # P11 Bug A fix: observe duration in finally — fires on success AND exception.
        try:
            import time as _time_f  # noqa: PLC0415

            from yadgar._shared.observability.metrics import (  # noqa: PLC0415
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
from yadgar.core.server._helpers import _is_episodic_query  # noqa: E402,F401
