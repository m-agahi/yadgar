"""Subagent dispatch helper — v5.44.0 (X1 extension).

Provides agent_dispatch_prelude() which returns a markdown-formatted prelude
for the orchestrator to prepend to a subagent's prompt.

The prelude includes:
  1. Yadgar protocol contract (read-first, report findings).
  2. Latest agent_prompt for the given pattern (if one exists in the wiki).
  3. A recall hint for the task_topic so the subagent knows what to recall.
  4. (v5.44.0 X1) — when branch_hint + directory are supplied and the agent's
     frontmatter declares prompt_uses_yadgar_context: true, the prelude also
     embeds auto-prefetched context (anchors + recent_memories from recall +
     wiki_pages from wiki_query).

Usage:
    prelude = agent_dispatch_prelude("dispatch-fix-bug", "vacuum regression")
    agent_prompt = prelude + "\\n\\n" + actual_task_description

    # v5.44.0 X1 — with auto-prefetch context:
    prelude = agent_dispatch_prelude(
        "dispatch-fix-bug", "vacuum regression",
        branch_hint="feat/v5.44.0-subagent-mcp-wiring",
        directory="/home/user/git/yadgar",
        subagent_type="general-purpose",
        include_context=True,
    )
"""

from __future__ import annotations

import logging

from yadgar.observability.observe import observe
from yadgar.server._app import _tool

logger = logging.getLogger(__name__)

# Fixed-cost contract section (always present, ~500 chars)
_YADGAR_CONTRACT = """\
## Yadgar subagent contract

Before substantive work:
1. `recall("…relevant topic…")` — surface hot memories, anchors, prior findings.
2. Observed state always wins over recalled state (update if contradicted).
3. For most agents: do NOT call `memorize()` directly — emit findings in report instead.
   Exception: long_running agents may call memorize with provenance_agent set.

REQUIRED: your final message MUST end with this section (even if empty):

## Yadgar findings
- <fact/anchor/insight> or "none"
""".strip()

# Budget: contract + recall hint ≈ 600 chars. Leave ~1 400 chars for prompt body.
_AGENT_PROMPT_BUDGET = 1_400
_TOTAL_BUDGET = 2_000
# X1 extension: extra budget for auto-fetched context block
_CONTEXT_BUDGET = 2_000


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
        from yadgar.server.tools._recall_shadow import _current_epoch  # noqa: PLC0415

        return _current_epoch(None)
    except Exception:
        return 0


def _make_prompt_cache():
    from yadgar.cache import TTL, Cache  # noqa: PLC0415

    return Cache(
        name="agent_prompt_prelude",
        max_entries=128,  # per pattern
        invalidation=TTL(_PROMPT_CACHE_TTL),  # epoch in key + TTL backstop
        deep_copy=True,  # prompt-result dict handed to caller / mutated downstream
        obs_tier="cold",  # low call rate
    )


_prompt_cache = _make_prompt_cache()


@observe(tier="stage", name="tools.dispatch_helper._cached_agent_prompt")
def _cached_agent_prompt(pattern: str, storage) -> dict | None:
    """Epoch-cached wrapper around _read_agent_prompt for the prelude's pattern-static
    lookup. Cache-miss result is IDENTICAL to a direct _read_agent_prompt call."""
    from yadgar.server.tools.agent_prompts import _read_agent_prompt  # noqa: PLC0415

    key = (pattern, _current_wiki_epoch())
    hit = _prompt_cache.get(key)
    if hit is not None:
        return hit
    result = _read_agent_prompt(f"agent-prompt-{pattern}", storage=storage)
    if result is not None:  # do not cache None misses (cheap; create bumps epoch)
        _prompt_cache.put(key, result)
    return result


@observe(tier="stage", name="tools.dispatch_helper._record_prelude_marker")
def _record_prelude_marker(storage, directory: str | None) -> None:
    """Best-effort record of agent_dispatch_prelude call (read-side nudge, #69).

    Writes a _dispatch_prelude marker so _apply_dispatch_prelude_signal can
    determine when agent_dispatch_prelude was last called.  Never raises.
    """
    if not directory:
        return
    try:
        storage.upsert_dispatch_prelude_marker(directory)
    except Exception as _e:  # noqa: BLE001
        logger.debug("agent_dispatch_prelude: upsert_dispatch_prelude_marker failed: %s", _e)


@_tool(always_load=True)
def agent_dispatch_prelude(
    pattern: str,
    task_topic: str,
    storage=None,
    # v5.44.0 X1 — auto-prefetch context params (all optional for backward compat)
    branch_hint: str | None = None,
    directory: str | None = None,
    subagent_type: str | None = None,
    include_context: bool = False,
) -> str:
    """Return a markdown prelude to prepend to a subagent prompt.

    The prelude contains the Yadgar protocol contract, the latest stored
    agent-prompt for *pattern* (if any), and a recall hint for *task_topic*.
    Total length is capped at 2 000 characters (orchestrator context budget).

    v5.44.0 X1 extension: when include_context=True (opt-in, per DP-X1-1),
    auto-fetches yadgar context using recall(directory, branch_hint) and
    wiki_query(directory, branch_hint) and embeds a structured context block
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
        branch_hint:    Caller's branch name (v5.43.0 surface). Used when
                        include_context=True for recall + wiki_query calls.
        directory:      Caller's working directory. Used for branch detection
                        when include_context=True.
        subagent_type:  Agent type label injected into the context block header.
        include_context: When True, embed auto-prefetched yadgar context block
                        (anchors + recent_memories + wiki_pages). Default False
                        per DP-X1-1 (opt-in only). Set by caller when the agent's
                        frontmatter declares prompt_uses_yadgar_context: true.

    Returns:
        Markdown string. Base cap: 2 000 chars. With context: up to 4 000 chars.
    """
    if storage is None:
        from yadgar.server.lifecycle import _get_storage  # noqa: PLC0415

        storage = _get_storage()

    # Record that agent_dispatch_prelude was called (read-side nudge, #69).
    _record_prelude_marker(storage, directory)

    sections: list[str] = [_YADGAR_CONTRACT]

    # Optional: latest agent_prompt for pattern.
    # v5.85 S5: deterministic internal slug-read (the agent_prompt_get tool was
    # removed). This is an exact-key read, NOT semantic recall.
    # S6 kill-gate: AGENT_PROMPT_LIBRARY_ENABLED=False → inject no prompt (inert).
    from yadgar.config import get_settings  # noqa: PLC0415

    if pattern and get_settings().AGENT_PROMPT_LIBRARY_ENABLED:
        try:
            prompt_result = _cached_agent_prompt(pattern, storage)
            if prompt_result and prompt_result.get("content"):
                raw_content = prompt_result["content"]
                version = prompt_result.get("version", "?")
                # Truncate if needed to respect overall budget
                used = sum(len(s) for s in sections) + 100  # 100 for separators
                available = _AGENT_PROMPT_BUDGET - used
                if available > 80:
                    snippet = raw_content[:available]
                    sections.append(f"## Agent-prompt [{pattern} v{version}]\n\n{snippet}")
        except Exception as _e:
            logger.debug("agent_dispatch_prelude: _read_agent_prompt failed: %s", _e)

    # Recall hint
    if task_topic:
        sections.append(f'## Recall hint\n\nSuggest: `recall("{task_topic}")` before starting.')

    # v5.44.0 X1: auto-prefetch context block (opt-in per DP-X1-1)
    if include_context:
        context_block = _build_context_block(
            task_topic=task_topic,
            branch_hint=branch_hint,
            directory=directory,
            subagent_type=subagent_type,
            storage=storage,
        )
        if context_block:
            sections.append(context_block)

    prelude = "\n\n".join(sections)

    # Hard cap — extended when context block included
    budget = _TOTAL_BUDGET + _CONTEXT_BUDGET if include_context else _TOTAL_BUDGET
    if len(prelude) > budget:
        prelude = prelude[: budget - 3] + "..."

    return prelude


@observe(tier="stage", name="tools.dispatch_helper._build_context_block")
def _build_context_block(
    task_topic: str,
    branch_hint: str | None,
    directory: str | None,
    subagent_type: str | None,
    storage,
) -> str:
    """Fetch and render a yadgar context block for auto-prefetch (X1).

    Calls recall(directory, branch_hint) + wiki_query(directory, branch_hint)
    using the v5.43.0 signatures. Returns empty string on any error.
    """
    import datetime  # noqa: PLC0415

    lines: list[str] = []
    ts = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    agent_label = subagent_type or "unknown"

    try:
        from yadgar.server.tools.recall import recall  # noqa: PLC0415

        memories = recall(
            query=task_topic or "context",
            max_results=5,
            directory=directory,
            branch_hint=branch_hint,
        )
        if memories:
            lines.append("### Recent memories")
            for m in memories[:5]:
                content = m.get("content", "")[:200] if isinstance(m, dict) else str(m)[:200]
                lines.append(f"- {content}")
    except Exception as _e:
        logger.debug("agent_dispatch_prelude: recall failed: %s", _e)

    try:
        from yadgar.server.tools.wiki import wiki_query  # noqa: PLC0415

        pages = wiki_query(
            query=task_topic or "context",
            max_results=3,
            directory=directory,
            branch_hint=branch_hint,
        )
        if pages:
            lines.append("### Wiki pages")
            for p in pages[:3]:
                if isinstance(p, dict):
                    title = p.get("title", p.get("slug", ""))
                    lines.append(f"- [[{title}]]")
    except Exception as _e:
        logger.debug("agent_dispatch_prelude: wiki_query failed: %s", _e)

    if not lines:
        return ""

    body = "\n".join(lines)
    header = f"### Yadgar Context (auto-prefetched {ts}, agent: {agent_label})"
    return f"{header}\n\n{body}"
