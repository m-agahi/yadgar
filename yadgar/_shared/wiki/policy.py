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
    ``"exclude"``    — pages excluded from recall fanout; still reachable via
                        ``wiki_query`` / ``wiki_read`` / ``wiki_list``.
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

from dataclasses import dataclass

from yadgar._shared.observability.observe import observe


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

    mutability: str = "free"
    """D26 — Car J: ``"free"`` (agents/tools can edit), ``"locked"`` (only
    sanctioned server-side lifecycle transitions can touch), or
    ``"derived"`` (regenerated on write, never edited directly).
    """

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

POLICY_BY_TYPE: dict[str, WikiPolicy] = {
    "agent_prompt": WikiPolicy(
        gate_mode="identity",  # D21: two agent_prompt pages from different projects aren't duplicates
        recall_disposition="exclude",
        dir_scope="strict",
        merge="allow",
        storage_scope="global",
        mutability="free",  # D26
    ),
    "task": WikiPolicy(
        gate_mode="identity",  # D21: structurally unique per project
        recall_disposition="downweight",  # D22: task pages are project-scoped, recalled less often
        dir_scope="strict",
        merge="allow",
        storage_scope="project",
        mutability="free",  # D26
    ),
    "adr": WikiPolicy(
        gate_mode="identity",
        recall_disposition="include",
        dir_scope="strict",
        merge="never",
        storage_scope="project",
        mutability="locked",  # D26: decisions are not edited by agents
    ),
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
