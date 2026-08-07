"""Wiki policy resolver — maps page_type to routing behaviour.

Car A of #83 (repo-wiki page-type). The repo_wiki page_type itself was
decommissioned (#33/ADR-0162, superseded by code_graph); this resolver
mechanism remains in use by other page types (e.g. agent_prompt).

Design rationale
----------------
Each ``page_type`` has a set of pipeline knobs that control how writes,
retrievals, and gate-checks behave.  Keeping these knobs in CODE (keyed by
``page_type``) rather than per-row data gives:

- A single source of truth for all routing decisions.
- Safe extensibility: adding a new ``page_type`` means adding one entry here.
- Security: ``page_type`` is schema-validated at the write seam (Car B), so
  routing is safe.  It remains NON-canonical for security decisions (e.g.
  branch scoping, ownership) per ADR §0.6 — this module is for BEHAVIOUR
  routing only.

Knob axes
---------
gate_mode
    ``"similarity"`` — default content-similarity gate (cosine threshold).
    ``"identity"``   — slug+schema gate; skip content-similarity entirely.
                        Was used for structural pages (repo_wiki, decommissioned
                        #33) where two thin ``logging.py`` modules from
                        different projects are NOT duplicates despite high
                        cosine similarity.

recall_disposition
    ``"include"``    — pages appear in normal fanout recall (default).
    ``"exclude"``    — pages dropped from SEARCH results (unified-recall fanout
                        AND ``wiki_query``, task 0134) unless the caller opted
                        in by tag — see ``is_recall_visible``. Always reachable
                        by exact key: ``wiki_read`` / ``wiki_get`` /
                        ``wiki_list`` never apply the filter.
    ``"downweight"`` — reserved for future tuning (treat as include for now).

dir_scope
    ``"strict"``     — gate and retrieval scoped to ``directory_context``.
    ``"global"``     — no directory scoping (e.g. cross-project knowledge).

merge
    ``"allow"``      — LLM-merge allowed on content conflict (default, #19).
    ``"never"``      — upsert / overwrite only; LLM must not touch content.
                        Stub for future #19 guard.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from yadgar._shared.observability.observe import observe
from yadgar._shared.wiki.wiki_meta import (
    PAGE_TYPE_AGENT_DISCIPLINE,
    PAGE_TYPE_AGENT_INDEX,
    PAGE_TYPE_AGENT_PATTERN,
    PAGE_TYPE_AGENT_PROMPT_LEGACY,
)


@dataclass(frozen=True)
class WikiPolicy:
    """Immutable routing policy for a wiki page_type.

    Field order: gate_mode, recall_disposition, dir_scope, merge, storage_scope.
    Positional construction is intentional — keep field order stable.
    New fields MUST be appended with defaults so existing 4-arg positional
    callers (tests) keep working without changes.
    """

    gate_mode: str
    """``"similarity"`` or ``"identity"``."""

    recall_disposition: str
    """``"include"``, ``"exclude"``, or ``"downweight"``."""

    dir_scope: str
    """``"strict"`` or ``"global"``."""

    merge: str
    """``"allow"`` or ``"never"``."""

    storage_scope: str = "project"
    """``"project"`` — stamped with caller's directory_context (default).
    ``"global"`` — directory_context is overridden to ``"global"`` at write time,
    regardless of what the caller supplied. Used for cross-project shared pages
    (e.g. agent-prompt library) so every project can recall them.

    C2 (#83): enforcement lives in ``WikiStore.add`` — the single write chokepoint
    used by both agent_prompt_save and wiki_add's replay path.
    """


# ── Policy registry ──────────────────────────────────────────────────────────

DEFAULT_POLICY = WikiPolicy(
    gate_mode="similarity",
    recall_disposition="include",
    dir_scope="strict",
    merge="allow",
    storage_scope="project",
)
"""Fallback policy for all page types not explicitly listed."""

_AGENT_LIBRARY_POLICY = WikiPolicy(
    gate_mode="similarity",
    recall_disposition="exclude",
    dir_scope="strict",
    merge="allow",
    storage_scope="global",
)
"""Shared routing for every agent-prompt-library page type.

ADR-0209 splits the TYPE, not the routing: all library pages stay excluded from
search fanout (they are dispatch scaffolding, not knowledge) and global-scoped
(ADR-0159 — the library is a cross-project shared resource, and a caller-dir
stamp made it invisible from every other project). One shared instance so the
three entries below cannot drift apart silently.
"""

POLICY_BY_TYPE: dict[str, WikiPolicy] = {
    # Pre-ADR-0209 type. Rows on an install that has not run migration 028 still
    # carry it, so the entry must stay until that migration is universal.
    PAGE_TYPE_AGENT_PROMPT_LEGACY: _AGENT_LIBRARY_POLICY,
    PAGE_TYPE_AGENT_PATTERN: _AGENT_LIBRARY_POLICY,
    PAGE_TYPE_AGENT_DISCIPLINE: _AGENT_LIBRARY_POLICY,
    # The TOC index. Registered HERE ONLY (no wiki_page_types.yaml entry) —
    # task 0134: a null page_type fell through to DEFAULT_POLICY include, which
    # made the index recall-visible. See PAGE_TYPE_AGENT_INDEX's docstring for
    # why it gets no lint schema.
    PAGE_TYPE_AGENT_INDEX: _AGENT_LIBRARY_POLICY,
}
"""Explicit overrides keyed by page_type string.

(repo_wiki's ``identity``/``exclude``/``never`` override was removed when the
repo_wiki generator was decommissioned — #33/ADR-0162, superseded by
code_graph's injected-memory-block model which stores no wiki pages at all.)
"""


# ── Resolver ─────────────────────────────────────────────────────────────────


@observe(tier="stage")
def get_policy(page_type: str | None) -> WikiPolicy:
    """Return the ``WikiPolicy`` for *page_type*, or ``DEFAULT_POLICY``.

    Args:
        page_type: The ``page_type`` field from a wiki page.  ``None`` and
            any unrecognised string both return ``DEFAULT_POLICY``.

    Returns:
        A frozen ``WikiPolicy`` instance.
    """
    if page_type is None:
        return DEFAULT_POLICY
    return POLICY_BY_TYPE.get(page_type, DEFAULT_POLICY)


@observe(tier="hot")
def is_recall_visible(page: dict, opt_in_tags: Sequence[str] | None = None) -> bool:
    """Return whether *page* may appear in SEARCH results.

    The single rule shared by the unified-recall wiki provider and
    ``wiki_query`` (task 0134 fixed both call sites diverging).

    A page whose ``page_type`` resolves to ``recall_disposition="exclude"``
    is dropped UNLESS the caller opted into it by tag — i.e. the page carries
    at least one of *opt_in_tags*. The opt-in is deliberately PER PAGE: the
    documented ``recall(tags=["agent-prompt"])`` lookup is consent to see
    agent-prompt pages, not consent to see every excluded page that happens to
    rank alongside them. (The pre-0134 code gated the whole filter on "were any
    tags passed at all", so one unrelated tag disabled the exclusion wholesale
    — and the ``agent-prompt-toc`` index, tagged ``agent-prompt-toc`` rather
    than ``agent-prompt``, surfaced on every targeted prompt lookup.)

    Args:
        page: A wiki page dict; reads ``page_type`` and ``tags``.
        opt_in_tags: Tags the caller explicitly asked for, if any.

    Returns:
        True when the page may be returned by a search path.
    """
    if get_policy(page.get("page_type")).recall_disposition != "exclude":
        return True
    if not opt_in_tags:
        return False
    return bool(set(page.get("tags") or []) & set(opt_in_tags))
