"""Subagent dispatch helper — v5.44.0 (X1 extension).

Provides agent_dispatch_prelude() which returns a markdown-formatted prelude
for the orchestrator to prepend to a subagent's prompt.

The prelude includes:
  1. Yadgar protocol contract (read-first, report findings).
  2. Latest agent_prompt for the given pattern (if one exists in the wiki).
  3. A recall hint for the task_topic so the subagent knows what to recall.
  4. (v5.44.0 X1) — when directory is supplied and the agent's
     frontmatter declares prompt_uses_yadgar_context: true, the prelude also
     embeds auto-prefetched context (anchors + recent_memories from recall +
     wiki_pages from wiki_query).

Usage:
    prelude = agent_dispatch_prelude("dispatch-fix-bug", "vacuum regression")
    agent_prompt = prelude + "\\n\\n" + actual_task_description

    # v5.44.0 X1 — with auto-prefetch context:
    prelude = agent_dispatch_prelude(
        "dispatch-fix-bug", "vacuum regression",
        directory="/home/user/git/yadgar",
        subagent_type="general-purpose",
        include_context=True,
    )

An unknown ``pattern`` RAISES (C5, 0047 PR#40 §5)
-------------------------------------------------
Before C5 an unknown pattern flowed through ``_cached_agent_prompt`` and was
dropped by a truthiness guard, so the prelude came back as contract + recall
hint and **no prompt** — which the caller reads as *"no pattern exists for this
task-shape"* and which therefore **licenses a bespoke dispatch**. That is the
same defect class as the ``_local_fallback`` ADR-0227 deletes: a fallback
manufacturing a plausible-looking wrong answer. The TOC carries **exact slugs**
and the agent reads **by slug**, so an unavailable slug must fail loud rather
than let the agent invent one. (Pruning dead TOC entries is cleanup that
FOLLOWS; it is not the fix.) ``pattern=""`` remains the documented skip, and
``_build_context_block`` is deliberately untouched — its empty return is
best-effort enrichment whose caller already drops it.
"""

from __future__ import annotations

import logging

from yadgar._shared.errors import UnresolvedPatternError, UnresolvedProjectError
from yadgar._shared.observability.observe import observe
from yadgar._shared.storage._project_id_writer import observe_project_id_skip
from yadgar.core.forward import _forward_admin
from yadgar.core.server._app import _tool
from yadgar.core.server.tools._project_param import (
    InvalidProjectOverrideError,
    accept_project_param,
    resolve_effective_project,
)

logger = logging.getLogger(__name__)

# Budget: contract + recall hint ≈ 700 chars. Leave ~1 300 chars for prompt body.
# (v5.122.0: contract body now wiki-sourced; genesis is ~676 chars with header.)
_AGENT_PROMPT_BUDGET = 1_400
# v5.123.0 (train Car 1): base 2 000 → 3 500 so contract (~676) + a 3-discipline
# Composes set (~2 000) + pattern snippet + recall hint fit without dropping
# disciplines (observed live: stacked-car-parallel-build lost ALL disciplines at
# 2 000 — the composition was invisible; the 2 000 cap predates disciplines).
# Overflow rule (drop disciplines last-listed-first + warning) stays as the
# safety valve. With-context total: 3 500 + 2 500 = 6 000 (was 4 000).
_TOTAL_BUDGET = 3_500
# X1 extension: extra budget for auto-fetched context block
_CONTEXT_BUDGET = 2_500

# Slug for the prelude contract wiki page (global scope).
_CONTRACT_SLUG = "agent-prompt-contract"


# ── Car 2 (v5.113): agent-prompt lookup cache ─────────────────────────────────
#
# The prelude is per-pattern-mostly-static: the only expensive, pattern-dependent
# work is the agent-prompt slug read (_read_agent_prompt("agent-prompt-<pattern>")
# → a storage lookup). task_topic affects only the trivial recall-hint line, and
# include_context=True pulls in non-deterministic recall/wiki_query — so we cache
# ONLY the pattern-static prompt-lookup result, keyed by (pattern, wiki epoch).
# agent_prompt_save is itself a wiki write → it bumps the global wiki epoch (via
# storage._bump_wiki_epoch) → this key moves → a stale prompt can never be served.
_PROMPT_CACHE_TTL = 120.0


def _current_wiki_epoch() -> int:
    """Global structural wiki epoch (bumped on every wiki write, incl. agent_prompt_save)."""
    try:
        from yadgar._shared.runtime.cache_epoch import _current_epoch  # noqa: PLC0415

        return _current_epoch(None)
    except Exception:
        return 0


@observe(tier="stage", metric="tools.dispatch_helper._make_prompt_cache")
def _make_prompt_cache():
    from yadgar.core.cache import (  # noqa: PLC0415
        TTL,
        Cache,
        _core_cache_ram_pct,
        _core_cache_total_budget_bytes,
        _namespace_budget_bytes,
    )

    total = _core_cache_total_budget_bytes(_core_cache_ram_pct())
    budget = _namespace_budget_bytes("agent_prompt_prelude", total)
    return Cache(
        name="agent_prompt_prelude",
        max_bytes=budget,  # byte-bounded LRU (core RAM-% budget, #49)
        invalidation=TTL(_PROMPT_CACHE_TTL),  # epoch in key + TTL backstop
        deep_copy=True,  # prompt-result dict handed to caller / mutated downstream
        obs_tier="cold",  # low call rate
    )


_prompt_cache = _make_prompt_cache()


@observe(tier="stage", metric="tools.dispatch_helper._cached_slug_read")
def _cached_slug_read(slug: str, storage) -> dict | None:
    """Epoch-cached exact-slug page read (Stage 3 generalization of the Car 2
    pattern cache — discipline slugs share the same namespace + epoch key).
    Cache-miss result is IDENTICAL to a direct _read_agent_prompt call."""
    from yadgar.core.server.tools.agent_prompts import _read_agent_prompt  # noqa: PLC0415

    key = (slug, _current_wiki_epoch())
    hit = _prompt_cache.get(key)
    if hit is not None:
        return hit
    result = _read_agent_prompt(slug, storage=storage)
    if result is not None:  # do not cache None misses (cheap; create bumps epoch)
        _prompt_cache.put(key, result)
    return result


@observe(tier="stage", metric="tools.dispatch_helper._cached_agent_prompt")
def _cached_agent_prompt(pattern: str, storage) -> dict | None:
    """Epoch-cached wrapper around _read_agent_prompt for the prelude's pattern-static
    lookup. Cache-miss result is IDENTICAL to a direct _read_agent_prompt call."""
    return _cached_slug_read(f"agent-prompt-{pattern}", storage)


# ── Stage 3 (2026-07-10): ## Composes resolution ──────────────────────────────
#
# Pattern pages reference discipline pages ([[agent-discipline-*]]) under a
# ## Composes section. The prelude resolves those references and assembles
# contract → disciplines (Composes order) → pattern → recall hint, deterministic,
# deduped (CONTRACT_COVERS + repeated slugs), within the budget. Overflow drops
# disciplines last-listed-first with a warning. Seed-on-miss applies to
# referenced disciplines (genesis text is the last-resort fallback).
#
# 0047 Car I: the composed-order read (which discipline slugs an agent_pattern
# pulls in, in what order) is now the ``agent_pattern_composes`` ledger table,
# not the ## Composes regex over the wiki body. The wiki body still CARRY the
# section as human-readable doc, but the source-of-truth for composition is the
# ledger row (see ``_ledger_composes_for``). The regex + parsers below stay as
# a fallback when the ledger is unavailable (engine #2 down, test seam, etc.).


@observe(tier="hot", metric="tools.dispatch_helper._ledger_composes_for")
def _ledger_composes_for(pattern: str) -> list[str] | None:
    """Read composed discipline slugs from ``agent_pattern_composes`` (table-backed).

    Returns an ordered list of discipline slugs (per ``position`` ASC), or
    ``None`` when the ledger is unavailable — the caller falls back to the
    wiki-body regex in that case. An empty list is a real answer
    (pattern exists with no composes) and is returned as ``[]`` (NOT None).

    The ledger is the source-of-truth (D40, schema §3.3): the wiki body may
    carry a free-form ``## Composes`` section as documentation, but the order
    the prelude actually composes them in comes from this table.
    """
    try:
        result = _forward_admin("list_pattern_composes", {"pattern_name": pattern})
    except Exception as _e:  # noqa: BLE001
        logger.debug("agent_dispatch_prelude: list_pattern_composes forward failed: %s", _e)
        return None
    if not isinstance(result, dict):
        return None
    if result.get("ok") is False:
        return None
    rows = result.get("rows") or []
    return [str(r.get("discipline_name", "")) for r in rows if r.get("discipline_name")]


@observe(tier="hot", metric="tools.dispatch_helper._composes_for")
def _composes_for(pattern: str, content: str | None) -> list[str]:
    """Resolve the composed-discipline slug list for *pattern*.

    0047 Car I: ledger first (table-backed source-of-truth), wiki-body regex
    as fallback. Returns [] when neither yields an answer.
    """
    if pattern:
        ledger = _ledger_composes_for(pattern)
        if ledger is not None:
            return ledger
    if isinstance(content, str):
        return _parse_composes(content)
    return []


@observe(tier="hot", metric="tools.dispatch_helper._parse_composes")
def _parse_composes(content: str) -> list[str]:
    """Extract [[slug]] references from the ## Composes section, in order, deduped.

    Links outside the Composes section are ignored. Returns [] when the section
    is absent or the content is not a string (MagicMock-storage X1 safety).
    """
    if not isinstance(content, str):
        return []
    import re as _re  # noqa: PLC0415

    m = _re.search(
        r"^##+\s+Composes\s*$(?P<body>.*?)(?=^##+\s|\Z)",
        content,
        _re.MULTILINE | _re.DOTALL | _re.IGNORECASE,
    )
    if m is None:
        return []
    slugs: list[str] = []
    for slug in _re.findall(r"\[\[([a-zA-Z0-9_-]+)\]\]", m.group("body")):
        if slug not in slugs:
            slugs.append(slug)
    return slugs


@observe(tier="hot", metric="tools.dispatch_helper._strip_composes_section")
def _strip_composes_section(content: str) -> str:
    """Remove the ## Composes section from a pattern snippet (the resolved
    discipline sections replace it in the assembled prelude)."""
    if not isinstance(content, str):
        return content
    import re as _re  # noqa: PLC0415

    return _re.sub(
        r"^##+\s+Composes\s*$(?P<body>.*?)(?=^##+\s|\Z)",
        "",
        content,
        flags=_re.MULTILINE | _re.DOTALL | _re.IGNORECASE,
    ).rstrip()


@observe(tier="stage", metric="tools.dispatch_helper._resolve_discipline_text")
def _resolve_discipline_text(slug: str, storage, project: str | None = None) -> str | None:
    """Resolve a composed discipline slug to its prompt body.

    Resolution: epoch-cached slug read → seed-on-miss from the disciplines
    genesis (create-if-absent, mirrors the contract path) → genesis text as
    in-memory fallback. Unknown slugs (no page, no genesis) return None and
    are skipped. Never raises — composition must not crash the prelude.

    C13 (0047 PR#40 §5): ``project`` is threaded from ``agent_dispatch_prelude``,
    which already has it. Without it the seed-on-miss WRITE below raised
    ``UnresolvedProjectError`` straight into the ``except Exception`` here and
    the page was silently never reseeded — the prelude still rendered from
    genesis, so the degradation was invisible and permanent. A write inside a
    never-raises helper is exactly where a dropped identity hides.
    """
    from yadgar.core.server.tools.agent_prompts import (  # noqa: PLC0415
        DISCIPLINE_SLUG_PREFIX,
        DISCIPLINES,
        _read_agent_prompt,
        _seed_discipline_pages,
        _unwrap_purpose_prompt,
    )

    genesis = {f"{DISCIPLINE_SLUG_PREFIX}{n}": c for n, _, c in DISCIPLINES}
    try:
        result = _cached_slug_read(slug, storage)
        if result is None and slug in genesis:
            # Seed-on-miss: re-create the discipline page from packaged genesis.
            _seed_discipline_pages(
                storage=storage,
                only=slug[len(DISCIPLINE_SLUG_PREFIX) :],
                project=project,
            )
            logger.info("prelude_discipline_reseeded slug=%s", slug)
            result = _read_agent_prompt(slug, storage=storage)
        if result is None:
            return genesis.get(slug)  # genesis fallback, or None for unknown slugs
        body = _unwrap_purpose_prompt(result.get("content", "")).strip()
        return body or genesis.get(slug)
    except Exception as _e:  # noqa: BLE001
        logger.debug("prelude_discipline_resolution_failed slug=%s: %s", slug, _e)
        return genesis.get(slug)


@observe(tier="stage", metric="tools.dispatch_helper._build_discipline_sections")
def _build_discipline_sections(
    composes: list[str], storage, project: str | None = None
) -> list[str]:
    """Render composed discipline slugs into prelude sections.

    Dedup rule: slugs in CONTRACT_COVERS are never re-included (the contract —
    always present — already carries those rules).
    """
    from yadgar.core.server.tools.agent_prompts import CONTRACT_COVERS  # noqa: PLC0415

    sections: list[str] = []
    for slug in composes:
        if slug in CONTRACT_COVERS:
            continue
        body = _resolve_discipline_text(slug, storage, project)
        if body:
            sections.append(f"## Discipline [{slug}]\n\n{body}")
    return sections


@observe(tier="hot", metric="tools.dispatch_helper._drop_disciplines_over_budget")
def _drop_disciplines_over_budget(
    head: list[str],
    disciplines: list[str],
    tail: list[str],
    budget: int,
) -> str:
    """Assemble head + disciplines + tail; drop disciplines last-listed-first
    while the joined prelude exceeds *budget* (warning per drop)."""
    kept = list(disciplines)
    while True:
        prelude = "\n\n".join(head + kept + tail)
        if len(prelude) <= budget or not kept:
            return prelude
        dropped = kept.pop()
        logger.warning(
            "agent_dispatch_prelude: budget overflow — dropping discipline section %r",
            dropped.splitlines()[0],
        )


@observe(tier="stage", metric="tools.dispatch_helper._get_contract_text")
def _get_contract_text(storage) -> str:
    """Return the Yadgar subagent contract text for injection into the prelude.

    v5.122.0: contract is wiki-sourced via agent-prompt-contract page (same
    epoch-keyed cache as pattern pages). Three-tier resolution:

    1. Cache hit (normal path, fast) → unwrap Purpose/Prompt wrapper → return body.
    2. Cache miss / page absent → SEED-ON-MISS: re-seed from packaged genesis via
       _seed_contract_page(), INFO log 'prelude_contract_reseeded'. Re-read once.
       If re-read succeeds → return unwrapped body.
    3. Seed write failure, re-read still None, OR any unexpected error (bad
       storage seam, non-string content) → ERROR log, return genesis text from
       CONTRACT_GENESIS (in-memory fallback). Never emits a contract-less
       prelude and never lets contract resolution crash the prelude build.

    The contract header ("## Yadgar subagent contract") is prepended here so the
    caller receives a ready-to-inject section string.
    """
    from yadgar.core.server.tools.agent_prompts import (  # noqa: PLC0415
        CONTRACT_GENESIS,
        _read_agent_prompt,
        _seed_contract_page,
        _unwrap_purpose_prompt,
    )

    # Genesis fallback text — always available regardless of wiki state.
    _, _, genesis_content = CONTRACT_GENESIS
    genesis_text = f"## Yadgar subagent contract\n\n{genesis_content}"

    try:
        # Normal path: read via epoch-keyed cache (reuses pattern-prompt cache key space).
        result = _cached_agent_prompt("contract", storage)

        if result is None:
            # Seed-on-miss: contract page absent — re-seed from packaged genesis.
            _seed_contract_page(storage=storage)
            logger.info("prelude_contract_reseeded")
            # Re-read (cache doesn't cache None, epoch bumped by seed write).
            result = _read_agent_prompt("agent-prompt-contract", storage=storage)

        if result is None:
            # Seed succeeded but read-your-write gap: use genesis fallback.
            logger.error("prelude_contract_read_after_reseed_failed — using genesis fallback")
            return genesis_text

        # Unwrap Purpose/Prompt wrapper added by agent_prompt_save.
        raw = result.get("content", "")
        body = _unwrap_purpose_prompt(raw).strip()
        if not body:
            logger.error("prelude_contract_empty_after_unwrap — using genesis fallback")
            return genesis_text

        return f"## Yadgar subagent contract\n\n{body}"
    except Exception as _e:  # noqa: BLE001
        # Covers seed-write failure AND any unexpected resolution error (e.g. a
        # test double returning non-string content). The prelude must never crash.
        logger.error("prelude_contract_resolution_failed: %s — using genesis fallback", _e)
        return genesis_text


@observe(tier="stage", metric="tools.dispatch_helper._record_pattern_usage")
def _record_pattern_usage(pattern: str) -> None:
    """Best-effort per-pattern usage-counter increment (Stage 3.4, #33).

    Fires once per prelude assembly that RESOLVED a pattern page (unresolved
    patterns are not counted). The DB write forwards to the backend
    increment_agent_pattern_uses admin op (D40, schema §3.3): the memory-row
    read-modify-write path is gone; ``uses`` is a SQL integer bumped via
    ``UPDATE agent_pattern SET uses = uses + 1 WHERE name = :name``. Transport
    errors are swallowed — the counter is telemetry, never load-bearing.
    """
    try:
        _forward_admin("increment_agent_pattern_uses", {"pattern": pattern})
    except Exception as _e:  # noqa: BLE001
        logger.debug("agent_dispatch_prelude: increment_agent_pattern_uses forward failed: %s", _e)


@observe(tier="stage", metric="tools.dispatch_helper._record_prelude_marker")
def _record_prelude_marker(storage, directory: str | None, project: str | None = None) -> None:
    """Best-effort record of agent_dispatch_prelude call (read-side nudge, #69).

    Writes a _dispatch_prelude marker so _apply_dispatch_prelude_signal can
    determine when agent_dispatch_prelude was last called.  Never raises.

    R3 Car 3d: agent_dispatch_prelude itself STAYS core (a read/prompt-builder
    that calls recall + wiki_query). Only this best-effort marker WRITE forwards
    to the backend /admin op. ``storage`` is kept for signature stability but the
    write no longer touches it directly. Transport errors are swallowed — the
    marker is a nudge, never load-bearing.

    C5b (0047 PR#40 §2 amendment 2): the marker row used to reach the raw
    ``CREATE`` unattributed. It is a per-directory row, so its owner is the
    caller's project — but ``agent_dispatch_prelude`` is a READ tool, and
    raising there would break prompt assembly over telemetry. So the write
    takes C4's declared skip-and-count path instead: no identity, no row, one
    counted skip. **Stated consequence:** a caller that passes no ``project=``
    records no marker at all, so ``_apply_dispatch_prelude_signal`` reads
    "never called" for it — a real behaviour change, made observable by the
    metric rather than hidden.
    """
    if not directory:
        return
    try:
        project_id = resolve_effective_project(
            project=project,
            directory=directory,
            session_project=None,
            tool="agent_dispatch_prelude",
        )
    except (UnresolvedProjectError, InvalidProjectOverrideError):  # fmt: skip
        observe_project_id_skip("dispatch_prelude_marker")
        return
    try:
        _forward_admin("record_prelude_marker", {"directory": directory, "project_id": project_id})
    except Exception as _e:  # noqa: BLE001
        logger.debug("agent_dispatch_prelude: record_prelude_marker forward failed: %s", _e)


@_tool(always_load=True)
def agent_dispatch_prelude(
    pattern: str,
    task_topic: str,
    storage=None,
    # v5.44.0 X1 — auto-prefetch context params (all optional for backward compat)
    directory: str | None = None,
    subagent_type: str | None = None,
    include_context: bool = False,
    *,
    project: str | None = None,
) -> str:
    """Return a markdown prelude to prepend to a subagent prompt.

    The prelude contains the Yadgar protocol contract, the latest stored
    agent-prompt for *pattern* (if any), and a recall hint for *task_topic*.
    Total length is capped at 3 500 characters (orchestrator context budget).

    v5.44.0 X1 extension: when include_context=True (opt-in, per DP-X1-1),
    auto-fetches yadgar context using recall(directory) and
    wiki_query(directory) and embeds a structured context block
    in the prelude. This satisfies DP-X1-1: opt-in via caller flag (agent
    definition frontmatter field prompt_uses_yadgar_context maps to this).

    Args:
        pattern:        Task pattern identifier used to look up agent_prompt
                        (e.g. "dispatch-fix-bug"). Resolved to the deterministic
                        slug agent-prompt-<pattern> via the internal slug-read.
                        Pass "" to skip prompt lookup.
        task_topic:     Short description of the task — injected as a recall hint
                        so the subagent knows which yadgar memories to surface.
        storage:        StorageEngine instance (injected for testing; otherwise
                        resolved from server lifecycle).
        directory:      Caller's working directory. Used for the recall +
                        wiki_query calls when include_context=True.
        subagent_type:  Agent type label injected into the context block header.
        include_context: When True, embed auto-prefetched yadgar context block
                        (anchors + recent_memories + wiki_pages). Default False
                        per DP-X1-1 (opt-in only). Set by caller when the agent's
                        frontmatter declares prompt_uses_yadgar_context: true.

    Returns:
        Markdown string. Base cap: 3 500 chars. With context: up to 6 000 chars.

    Raises:
        UnresolvedPatternError: ``pattern`` is non-empty, the library is
            enabled, and no page exists at ``agent-prompt-<pattern>``. C5
            (0047 PR#40 §5): this used to return a prelude with the contract and
            no prompt, which the caller reads as "no pattern exists" — and that
            reading is what licenses a bespoke dispatch. ``pattern=""`` remains
            the documented skip.
    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; C7 re-keys
    # this tool's scope from ``directory`` onto the resolved project_id.
    # Car 3: the return value used to be discarded, so the context-block
    # fan-out below was issued with a directory and NO identity. It is the
    # validated override (``None`` when the caller named no project).
    validated_project = accept_project_param(project, directory)
    if storage is None:
        from yadgar._shared.runtime.lifecycle import _get_storage  # noqa: PLC0415

        storage = _get_storage()

    # Record that agent_dispatch_prelude was called (read-side nudge, #69).
    _record_prelude_marker(storage, directory, project)

    # v5.122.0: contract sourced from wiki page (agent-prompt-contract) via cache,
    # with seed-on-miss + genesis fallback. Fetched BEFORE the kill-gate so the
    # contract is always present even when AGENT_PROMPT_LIBRARY_ENABLED=False.
    # Assembly order (Stage 3, deterministic): contract → disciplines (Composes
    # order) → pattern → recall hint → context block.
    contract_text = _get_contract_text(storage)
    head: list[str] = [contract_text]
    discipline_sections: list[str] = []
    tail: list[str] = []

    # Optional: latest agent_prompt for pattern.
    # v5.85 S5: deterministic internal slug-read (the agent_prompt_get tool was
    # removed). This is an exact-key read, NOT semantic recall.
    # S6 kill-gate: AGENT_PROMPT_LIBRARY_ENABLED=False → inject no prompt (inert).
    from yadgar._shared.config import get_settings  # noqa: PLC0415

    if pattern and get_settings().AGENT_PROMPT_LIBRARY_ENABLED:
        # C5 (0047 PR#40 §5) — see the module docstring's "unknown pattern"
        # note. Read hoisted OUT of the try below (a raise inside it would be
        # swallowed by the handler this car exists to defeat); empty result
        # raises; storage errors are no longer reported as absence.
        prompt_result = _cached_agent_prompt(pattern, storage)
        if not (prompt_result and prompt_result.get("content")):
            raise UnresolvedPatternError(f"agent-prompt-{pattern}")

        raw_content = prompt_result["content"]
        version = prompt_result.get("version", "?")
        # Stage 3 + 0047 Car I: ledger-first composed-order read
        # (agent_pattern_composes is the source-of-truth); wiki-body
        # regex is the fallback when the ledger is unavailable.
        # Assembly stays best-effort: a discipline-composition or usage-counter
        # failure is genuinely non-fatal enrichment, and the RESOLUTION above is
        # what had to stop being best-effort.
        try:
            discipline_sections = _build_discipline_sections(
                _composes_for(pattern, raw_content), storage, project
            )
            body = _strip_composes_section(raw_content)
            # Truncate if needed to respect the pattern-snippet budget
            used = len(contract_text) + 100  # 100 for separators
            available = _AGENT_PROMPT_BUDGET - used
            if available > 80:
                snippet = body[:available]
                tail.append(f"## Agent-prompt [{pattern} v{version}]\n\n{snippet}")
            # Stage 3.4: count the assembly (pattern resolved → real usage).
            _record_pattern_usage(pattern)
        except Exception as _e:
            logger.debug("agent_dispatch_prelude: prompt assembly failed: %s", _e)

    # Recall hint
    if task_topic:
        tail.append(f'## Recall hint\n\nSuggest: `recall("{task_topic}")` before starting.')

    # v5.44.0 X1: auto-prefetch context block (opt-in per DP-X1-1)
    if include_context:
        context_block = _build_context_block(
            task_topic=task_topic,
            directory=directory,
            subagent_type=subagent_type,
            storage=storage,
            project=validated_project,
        )
        if context_block:
            tail.append(context_block)

    # Budget — extended when context block included. Overflow drops disciplines
    # last-listed-first (warning per drop); contract + pattern + hint survive.
    budget = _TOTAL_BUDGET + _CONTEXT_BUDGET if include_context else _TOTAL_BUDGET
    prelude = _drop_disciplines_over_budget(head, discipline_sections, tail, budget)

    # Hard cap backstop (contract/pattern alone exceeding the budget)
    if len(prelude) > budget:
        prelude = prelude[: budget - 3] + "..."

    return prelude


@observe(tier="stage", metric="tools.dispatch_helper._build_context_block")
def _build_context_block(
    task_topic: str,
    directory: str | None,
    subagent_type: str | None,
    storage,
    project: str | None = None,
) -> str:
    """Fetch and render a yadgar context block for auto-prefetch (X1).

    Calls ``recall`` + ``wiki_query``, both scoped by ``project`` when the
    caller named one. Returns empty string on any error.

    Car 3 — **the identity was validated and then dropped.**
    ``agent_dispatch_prelude`` ran ``accept_project_param(project, directory)``
    and discarded the return value, so both fan-outs were issued with a
    ``directory=`` and no ``project=``. Threading the validated override is the
    whole fix; ``None`` still means "fall back to the directory", which is
    exactly ``recall``'s own contract for that parameter.

    Both failures were swallowed into ``logger.debug``, so a raising fan-out was
    indistinguishable from an empty corpus: ``include_context=True`` returned a
    prelude with no context block and no way to tell why. They are now WARNINGs
    with a traceback.

    The catches stay BROAD on purpose. Narrowing them to storage errors would
    let ``UnresolvedProjectError`` — an expected outcome once no identity is
    named — escape and fail every ``include_context=True`` dispatch. Prelude
    assembly must not be failed by its own optional enrichment; it must only
    stop being silent about it.
    """
    import datetime  # noqa: PLC0415

    lines: list[str] = []
    ts = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    agent_label = subagent_type or "unknown"

    try:
        from yadgar.core.server.tools.recall import recall  # noqa: PLC0415

        memories = recall(
            query=task_topic or "context",
            max_results=5,
            directory=directory,
            project=project,
        )
        if memories:
            lines.append("### Recent memories")
            for m in memories[:5]:
                content = m.get("content", "")[:200] if isinstance(m, dict) else str(m)[:200]
                lines.append(f"- {content}")
    except Exception as _e:  # noqa: BLE001 — backstop: enrichment must not fail a dispatch
        logger.warning(
            "agent_dispatch_prelude: recall failed, context block will omit memories: %s",
            _e,
            exc_info=True,
        )

    try:
        from yadgar.core.server.tools.wiki import wiki_query  # noqa: PLC0415

        pages = wiki_query(
            query=task_topic or "context",
            max_results=3,
            directory=directory,
            project=project,
        )
        if pages:
            lines.append("### Wiki pages")
            for p in pages[:3]:
                if isinstance(p, dict):
                    title = p.get("title", p.get("slug", ""))
                    lines.append(f"- [[{title}]]")
    except Exception as _e:  # noqa: BLE001 — backstop: enrichment must not fail a dispatch
        logger.warning(
            "agent_dispatch_prelude: wiki_query failed, context block will omit pages: %s",
            _e,
            exc_info=True,
        )

    if not lines:
        return ""

    body = "\n".join(lines)
    header = f"### Yadgar Context (auto-prefetched {ts}, agent: {agent_label})"
    return f"{header}\n\n{body}"
