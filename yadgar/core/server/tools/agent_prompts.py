"""Agent-prompt library MCP tools — v5.85 S4/S5 rework.

Tool:
  agent_prompt_save(pattern, content) — upserts one page per pattern

Storage convention (v5.85 rework):
  - Slug pattern: agent-prompt-<task-pattern>  (deterministic, no -vN suffix)
  - Tags: ["agent-prompt", "task:<pattern>"]
  - Category: "reference"
  - page_type: "agent_pattern" (ADR-0209; was "agent_prompt" pre-split.
    Discipline pages carry "agent_discipline", the TOC "agent_index").
  - wiki versioning (wiki_page_version table) carries history.

Retrieval (S4/S5 collapse):
  - Semantic lookup is now `recall(type="wiki", tags=["agent-prompt"])`
    (the bespoke agent_prompt_search tool was removed; the SQL pre-filter lives
    in WikiStore.query via search_wiki_vectors_tagged).
  - Exact-key lookup is the internal helper `_read_agent_prompt(slug, storage)`
    (the bespoke agent_prompt_get tool was removed; dispatch_helper uses this).
"""

from __future__ import annotations

import logging
import re

from yadgar._shared.observability.observe import observe
from yadgar._shared.security.secrets import gate_or_reject
from yadgar._shared.wiki.wiki_meta import (
    PAGE_TYPE_AGENT_DISCIPLINE,
    PAGE_TYPE_AGENT_PATTERN,
)
from yadgar.core.forward import _forward_admin
from yadgar.core.server._app import _tool

logger = logging.getLogger(__name__)

# ── S6 discovery surface ──────────────────────────────────────────────────────
# Global TOC page: title "Agent Prompt TOC" → slug "agent-prompt-toc" (_slugify).
_TOC_TITLE = "Agent Prompt TOC"
_TOC_SLUG = "agent-prompt-toc"
# One row per pattern: `- `<pattern>` → <purpose>`. Regex pins the pattern column
# so re-save scan-replaces the existing line (idempotent upsert, no dupes).
_TOC_ROW_RE = re.compile(r"^- `(?P<pattern>[^`]+)` → .*$", re.MULTILINE)
# Reason tag that identifies the single library-discovery anchor (create-if-absent).
_LIBRARY_ANCHOR_REASON = "agent-prompt-library"
_LIBRARY_ANCHOR_CONTENT = (
    "Agent-prompt library: see wiki [[agent-prompt-toc]] for available prompts; "
    "recall(type='wiki', tags=['agent-prompt']) to search; "
    "agent_prompt_save to add."
)


_DOUBLE_WRAP_RE = re.compile(
    r"^##\s+Purpose\b.*?\n##\s+Prompt\s*\n+(.*)",
    re.DOTALL | re.IGNORECASE,
)


@observe(tier="hot", metric="tools.agent_prompts._unwrap_purpose_prompt")
def _unwrap_purpose_prompt(content: str) -> str:
    """Strip a leading ## Purpose / ## Prompt wrapper from content if present.

    If content already looks like a fully-wrapped agent-prompt page
    (starts with '## Purpose ...' followed by '## Prompt ...'), extract and
    return just the body text that follows '## Prompt'.  This prevents
    double-wrapping when a caller passes pre-wrapped content to agent_prompt_save.

    Conservative: only strips when BOTH headers are present in the leading region.
    Passes bare content through unchanged.
    """
    stripped = content.lstrip()
    m = _DOUBLE_WRAP_RE.match(stripped)
    if m:
        return m.group(1).rstrip("\n")
    return content


_PURPOSE_EXTRACT_RE = re.compile(
    r"^##\s+Purpose\b\s*\n+(.*?)\n##\s+Prompt\b",
    re.DOTALL | re.IGNORECASE,
)


@observe(tier="hot", metric="tools.agent_prompts._extract_purpose")
def _extract_purpose(wrapped_content: str) -> str | None:
    """Extract the '## Purpose' text from a wrapped agent-prompt page, if present.

    Companion to _unwrap_purpose_prompt (which extracts the '## Prompt' body).
    Used by discipline_save so an update that omits purpose= reuses the
    existing stored purpose instead of silently overwriting it with the
    generic default — a discipline write path exists specifically to avoid
    silent content loss, so the purpose line deserves the same care as the
    prompt body.

    Returns None if content isn't wrapped in the expected Purpose/Prompt form.
    """
    m = _PURPOSE_EXTRACT_RE.match(wrapped_content.lstrip())
    return m.group(1).strip() if m else None


# R3 Car 3c: the TOC-upsert + library-anchor writes (previously _upsert_toc_row /
# _ensure_library_anchor here) moved backend-side into
# yadgar.backend.admin_exec.wiki (they are DB writes on the agent_prompt_save path,
# which now forwards to POST /admin). The _TOC_* / _LIBRARY_ANCHOR_* constants
# above stay: _TOC_SLUG + _TOC_ROW_RE are still read core-side (project.py TOC scan).


@_tool()
def agent_prompt_save(
    pattern: str,
    content: str,
    directory: str | None = None,
    branch_hint: str | None = None,
    purpose: str | None = None,
    storage=None,  # noqa: ARG001 — kept for API back-compat (seed_agent_prompts passes it);
    # R3 Car 3c: the DB write forwards to backend /admin, which uses its own storage.
) -> dict:
    """Save (upsert) an agent-prompt for the given task pattern.

    v5.85 rework: one page per pattern, deterministic slug agent-prompt-<pattern>.
    Wiki versioning (wiki_page_version) carries history. Second save → version 2, etc.

    Args:
        pattern: Task pattern identifier (e.g. "dispatch-fix-bug").
                 ASCII alphanumeric, hyphens, underscores only.
        content: The prompt text content.
        directory: Absolute project path or 'global'. Required (v5.42.5).
        branch_hint: Caller branch context (optional).
        purpose: One-line description for the TOC. Derived from pattern if omitted.
        storage: Accepted for API back-compat (R3 Car 3c: the storage write forwards
                 to the backend /admin op, which resolves its own storage). Unused.

    Returns:
        {"saved": True, "version": N, "slug": "...", "page_id": ...}
    """
    # v5.42.5: directory required — reject at MCP boundary (same contract as wiki_add)
    _effective_dir = (directory or "").strip() or None
    if not _effective_dir:
        return {
            "error": "missing_directory",
            "saved": False,
            "message": (
                "directory required and must be non-empty. "
                "Pass the absolute project path or 'global'."
            ),
            "field": "directory",
            "op_type": "agent_prompt_save",
        }

    # I26 secret gate — STAYS core (scan content before any state mutation).
    _gate = gate_or_reject(content)
    if _gate is not None:
        return _gate

    slug = f"agent-prompt-{pattern}"
    title = f"Agent Prompt: {pattern}"
    tags = ["agent-prompt", f"task:{pattern}"]
    _purpose = purpose or f"Agent prompt for {pattern} tasks."
    # Strip any pre-existing Purpose/Prompt wrapper before composing — prevents
    # double-wrapping when the caller passes already-wrapped content (#68).
    content = _unwrap_purpose_prompt(content)
    # Wrap content with required headings so wiki_lint passes for page_type="agent_prompt"
    full_content = f"## Purpose\n\n{_purpose}\n\n## Prompt\n\n{content}"

    # R3 Car 3c: directory-validation + I26 secret-gate + content-wrap stay core;
    # the DB writes (wiki.add + TOC upsert + library anchor) forward to the backend
    # /admin op. All three go backend-side (wiki + replay are in the slim engine
    # set). The wiki.add → _bump_wiki_epoch hook busts the core agent_prompt_prelude
    # cache namespace cross-process (file-backed epoch, Car 2). The `storage=` test
    # seam is dropped on the forward path — bypass tests use admin_backend_bypass.
    return _forward_admin(
        "agent_prompt_save",
        {
            "slug": slug,
            "title": title,
            "full_content": full_content,
            "tags": tags,
            "pattern": pattern,
            "purpose": _purpose,
            "branch_hint": branch_hint,
            "directory": _effective_dir,
            # ADR-0209: the CALLER decides the family — the backend op keys
            # everything else off the payload slug, and re-deriving the type
            # from a slug prefix backend-side would rebuild the string-matching
            # the split exists to remove. The contract is seeded through this
            # same function (_seed_contract_page) but belongs to the discipline
            # type, so it is excepted by slug here rather than typed as a
            # pattern (ADR-0209: flagged, not promoted to a third type).
            "page_type": (
                PAGE_TYPE_AGENT_DISCIPLINE if slug == CONTRACT_SLUG else PAGE_TYPE_AGENT_PATTERN
            ),
        },
    )


# ── S8 starter library ───────────────────────────────────────────────────────
# Pinned patterns and content for the 15 built-in dispatch starters (v5.122.0:
# plan-executing-build added so the contract's rule-4 pointer resolves on
# fresh installs; v5.123.0 seed backflow: 10 battle-tested live patterns
# promoted into the genesis corpus).
# Slug for each: agent-prompt-<pattern>  (MUST match test assertions exactly).
#
# v5.88 seed consolidation: the editable content lives in the canonical seed
# materials dir (yadgar/seed/materials/agent_prompts.yaml), not inline here.
# This module only loads it — edit the yaml, not this file. STARTER_PROMPTS keeps
# its public shape: list[tuple[pattern, purpose, content]].
#
# v5.122.0: the prelude contract genesis also lives in agent_prompts.yaml under
# the "contract:" key (NOT in "prompts:" — excluded from STARTER_PROMPTS so the
# 4-starter semantics are preserved). _load_contract_genesis() reads it;
# _seed_contract_page() seeds it idempotently alongside the starters.


@observe(tier="stage", metric="tools.agent_prompts._load_genesis_yaml")
def _load_genesis_yaml() -> dict:
    """Load + parse materials/agent_prompts.yaml (the packaged genesis corpus).

    Read via importlib.resources so it works both from source and from an
    installed wheel (the yaml ships as package data under yadgar/core/seed/materials/).
    Uses ruamel.yaml — yadgar's only declared YAML dependency (see pyproject).
    PyYAML is NOT used here: it is not a declared dependency (present only
    transitively via the optional `ml` extra), so preferring it would make
    this loader's behavior depend on which packages happen to be installed
    (v5.169.1 fix).
    """
    from importlib.resources import files  # noqa: PLC0415

    from ruamel.yaml import YAML  # noqa: PLC0415

    text = (
        files("yadgar.core.seed").joinpath("materials").joinpath("agent_prompts.yaml").read_text()
    )
    return YAML(typ="safe").load(text)


@observe(tier="stage", metric="tools.agent_prompts._load_starter_prompts")
def _load_starter_prompts() -> list[tuple[str, str, str]]:
    """Load the built-in starter prompts from materials/agent_prompts.yaml.

    Returns a list of (pattern, purpose, content) 3-tuples in file order.
    """
    return [(e["pattern"], e["purpose"], e["content"]) for e in _load_genesis_yaml()["prompts"]]


@observe(tier="stage", metric="tools.agent_prompts._load_contract_genesis")
def _load_contract_genesis() -> tuple[str, str, str]:
    """Load the prelude contract genesis from materials/agent_prompts.yaml.

    Returns (pattern, purpose, content) for the 'contract:' entry.
    This is the packaged genesis copy — the authoritative source for seeding
    and for the in-memory fallback in dispatch_helper when the wiki page is absent.

    Separated from STARTER_PROMPTS so the 4-starter semantics are preserved.
    """
    entry = _load_genesis_yaml()["contract"]
    return (entry["pattern"], entry["purpose"], entry["content"])


@observe(tier="stage", metric="tools.agent_prompts._load_disciplines")
def _load_disciplines() -> list[tuple[str, str, str]]:
    """Load the discipline-page genesis entries from materials/agent_prompts.yaml.

    Stage 2 (2026-07-10): cross-cutting rule pages extracted from the pattern
    corpus. Returns a list of (name, purpose, content) 3-tuples in file order.
    Slug convention: agent-discipline-<name> (NOT agent-prompt-<name> — these
    are not dispatch patterns).
    """
    return [(e["name"], e["purpose"], e["content"]) for e in _load_genesis_yaml()["disciplines"]]


# Module-level genesis tuple: (pattern, purpose, content).
CONTRACT_GENESIS: tuple[str, str, str] = _load_contract_genesis()

# Discipline slugs whose rules the contract text already carries — composition
# (Stage 3) dedups these out of assembled preludes. Loaded from the contract
# entry's `covers:` key in the genesis yaml.
CONTRACT_COVERS: tuple[str, ...] = tuple(_load_genesis_yaml()["contract"].get("covers", []))

STARTER_PROMPTS: list[tuple[str, str, str]] = _load_starter_prompts()

# Discipline genesis entries: (name, purpose, content) per page.
DISCIPLINES: list[tuple[str, str, str]] = _load_disciplines()

# Slug for the prelude contract wiki page (global scope, like other seeded prompts).
CONTRACT_SLUG = f"agent-prompt-{CONTRACT_GENESIS[0]}"  # "agent-prompt-contract"

# Slug prefix for discipline pages (Stage 2).
DISCIPLINE_SLUG_PREFIX = "agent-discipline-"


@observe(tier="stage", metric="tools.agent_prompts._seed_contract_page")
def _seed_contract_page(
    storage,
    branch_hint: str | None = None,
) -> bool:
    """Idempotently seed the prelude contract wiki page (create-if-absent).

    Separate from seed_agent_prompts so the 4-starter counts/assertions are
    not disturbed. Returns True if a new page was created, False if skipped.
    """
    existing = _read_agent_prompt(CONTRACT_SLUG, storage=storage)
    if existing is not None:
        return False

    pattern, purpose, content = CONTRACT_GENESIS
    agent_prompt_save(
        pattern,
        content,
        directory="global",
        purpose=purpose,
        branch_hint=branch_hint,
        storage=storage,
    )
    return True


@observe(tier="stage", metric="tools.agent_prompts._save_discipline_page")
def _save_discipline_page(
    name: str,
    purpose: str,
    content: str,
    branch_hint: str | None = None,
) -> dict:
    """Save (upsert) a discipline page under slug agent-discipline-<name>.

    Same write path as agent_prompt_save (I26 secret gate core-side, DB write
    forwarded to the backend agent_prompt_save admin op — the op keys everything
    off the payload slug, so discipline slugs ride the existing machinery, incl.
    the TOC row + wiki-epoch bump). Disciplines keep the same Purpose/Prompt
    wrap + lint shape as patterns, but carry their OWN page_type since ADR-0209
    (``agent_discipline``) — ADR-0208 gives them different governance (the
    asymmetric removal guard), and page_type is the lever that governance keys
    off, not the slug prefix.
    """
    _gate = gate_or_reject(content)
    if _gate is not None:
        return _gate

    slug = f"{DISCIPLINE_SLUG_PREFIX}{name}"
    title = f"Agent Discipline: {name}"
    tags = ["agent-prompt", "agent-discipline", f"discipline:{name}"]
    content = _unwrap_purpose_prompt(content)
    full_content = f"## Purpose\n\n{purpose}\n\n## Prompt\n\n{content}"
    return _forward_admin(
        "agent_prompt_save",
        {
            "slug": slug,
            "title": title,
            "full_content": full_content,
            "tags": tags,
            # TOC row keys on the full slug so discipline rows are unambiguous
            # next to dispatch-pattern rows.
            "pattern": slug,
            "purpose": purpose,
            "branch_hint": branch_hint,
            "directory": "global",
            "page_type": PAGE_TYPE_AGENT_DISCIPLINE,
        },
    )


@observe(tier="hot", metric="tools.agent_prompts._removed_prompt_lines")
def _removed_prompt_lines(old_body: str, new_body: str) -> list[str]:
    """Return non-empty lines present in old_body but absent (verbatim) from new_body.

    ADR-0208 asymmetric guard, precise definition: an update is additions-only
    when every non-empty existing line survives *somewhere* in the incoming
    body — order and duplication don't matter. This mirrors
    scripts/check_test_weakening.py's delta-counting shape (count what changed,
    don't ban edits outright) rather than a line-position diff: a rule that
    moved to a different spot in the file is not a removal.

    Deduplicated: a repeated identical old line is only reported once.
    """
    new_lines = {ln for ln in new_body.splitlines() if ln.strip()}
    seen: set[str] = set()
    removed: list[str] = []
    for ln in old_body.splitlines():
        if not ln.strip() or ln in seen:
            continue
        seen.add(ln)
        if ln not in new_lines:
            removed.append(ln)
    return removed


@_tool()
def discipline_save(
    name: str,
    content: str,
    purpose: str | None = None,
    confirm_removal: bool = False,
    branch_hint: str | None = None,
) -> dict:
    """Save (upsert) a discipline page under agent-discipline-<name>.

    This is the MCP write path ADR-0208 calls a hard prerequisite: disciplines
    are the rule sets that bind every future dispatch (see agent-prompt-toc /
    the prelude contract's ``covers:`` list), and until now the only writer was
    the seeder's create-if-absent path (_seed_discipline_pages) — updating a
    discipline required a code change plus a release.

    ADR-0208 asymmetric guard: because a discipline binds every future
    dispatch, an instance able to rewrite it unguarded could weaken its own
    constraints. Additions flow freely; a net REMOVAL of an existing rule
    requires explicit ratification. Precise definition: compare the existing
    page's '## Prompt' body against the incoming content (after stripping any
    accidental Purpose/Prompt wrapper). If every non-empty existing line
    survives somewhere in the new body, the update is additions-only and is
    allowed. If any non-empty existing line is absent, it is a removal and the
    save is REJECTED — naming exactly which line(s) would be lost — unless
    confirm_removal=True ratifies it. Creating a page that does not exist yet
    is never a removal (nothing to compare against).

    Out of scope (ADR-0209, a later car): baseline_hash, content_hash, drift
    detection against the packaged seed, and three-way merge. This tool is
    purely the additions-flow / removal-needs-ratification gate.

    Args:
        name: Discipline name (e.g. "adr-consult"). Page slug:
              agent-discipline-<name>.
        content: The discipline's prompt text. May be bare or already wrapped
                 in '## Purpose' / '## Prompt' headers (unwrapped automatically,
                 same double-wrap guard as agent_prompt_save).
        purpose: One-line description for the TOC. When omitted on an UPDATE,
                 the existing page's stored purpose is reused (never silently
                 clobbered). When omitted on a CREATE (no existing page to
                 reuse from), falls back to a generic default string.
        confirm_removal: Ratify a detected net removal of existing rule
                 line(s). Ignored when the guard detects no removal.
        branch_hint: Caller branch context (optional).

    Returns:
        On success: {"saved": True, "version": N, "slug": "agent-discipline-<name>", ...}
            (the underlying agent_prompt_save result).
        On guard rejection: {"saved": False, "error": "removal_requires_confirmation",
            "slug": "...", "removed_lines": [...], "message": "..."}.
        On secret-gate rejection: the gate_or_reject dict (same as agent_prompt_save).
    """
    _gate = gate_or_reject(content)
    if _gate is not None:
        return _gate

    slug = f"{DISCIPLINE_SLUG_PREFIX}{name}"
    new_body = _unwrap_purpose_prompt(content)
    existing = _read_agent_prompt(slug)

    if existing is not None and not confirm_removal:
        old_body = _unwrap_purpose_prompt(existing["content"])
        removed = _removed_prompt_lines(old_body, new_body)
        if removed:
            return {
                "saved": False,
                "error": "removal_requires_confirmation",
                "slug": slug,
                "removed_lines": removed,
                "message": (
                    f"{len(removed)} existing rule line(s) would be removed from "
                    f"{slug!r}. Pass confirm_removal=True to ratify the removal:\n"
                    + "\n".join(f"  - {ln}" for ln in removed)
                ),
            }

    if purpose is not None:
        _purpose = purpose
    elif existing is not None:
        _purpose = _extract_purpose(existing["content"]) or f"Agent discipline: {name}."
    else:
        _purpose = f"Agent discipline: {name}."
    return _save_discipline_page(name, _purpose, new_body, branch_hint=branch_hint)


@observe(tier="stage", metric="tools.agent_prompts._seed_discipline_pages")
def _seed_discipline_pages(
    storage,
    branch_hint: str | None = None,
    only: str | None = None,
) -> tuple[int, int]:
    """Idempotently seed the discipline pages (create-if-absent per name).

    Args:
        storage: StorageEngine used for the existence check.
        branch_hint: Caller branch context (optional).
        only: When set, seed just this discipline name (Stage-3 seed-on-miss
              path from prelude composition). Unknown names are a no-op.

    Returns:
        (created, skipped) counts over the names considered.
    """
    created = 0
    skipped = 0
    for name, purpose, content in DISCIPLINES:
        if only is not None and name != only:
            continue
        slug = f"{DISCIPLINE_SLUG_PREFIX}{name}"
        if _read_agent_prompt(slug, storage=storage) is not None:
            skipped += 1
            continue
        _save_discipline_page(name, purpose, content, branch_hint=branch_hint)
        created += 1
    return created, skipped


@_tool(power=True)
def seed_agent_prompts(
    storage=None,
    branch_hint: str | None = None,
) -> dict:
    """Idempotently seed the 15 starter agent-prompts + contract + disciplines (global).

    Skips any pattern whose page already exists (create-if-absent per pattern).
    Calling twice is safe: second call returns created=0, skipped=15.

    The TOC and global discovery anchor are managed by agent_prompt_save —
    this function does NOT duplicate that logic.

    The prelude contract page (agent-prompt-contract) is seeded separately via
    _seed_contract_page, and the discipline pages (agent-discipline-<name>,
    Stage 2) via _seed_discipline_pages — so the starter counts/pattern list
    are unchanged; discipline counts are reported under their own keys.

    Args:
        storage: StorageEngine instance (injected for testing; otherwise
                 resolved from server lifecycle).
        branch_hint: Caller branch context (optional).

    Returns:
        {"seeded": True, "created": N, "skipped": M, "patterns": [...all 15...],
         "disciplines_created": D, "disciplines_skipped": E, "disciplines": [...]}
    """
    if storage is None:
        from yadgar._shared.runtime.lifecycle import _get_storage  # noqa: PLC0415

        storage = _get_storage()

    created = 0
    skipped = 0
    for pattern, purpose, content in STARTER_PROMPTS:
        slug = f"agent-prompt-{pattern}"
        existing = _read_agent_prompt(slug, storage=storage)
        if existing is not None:
            skipped += 1
        else:
            agent_prompt_save(
                pattern,
                content,
                directory="global",
                purpose=purpose,
                branch_hint=branch_hint,
                storage=storage,
            )
            created += 1

    # Seed the prelude contract page alongside (idempotent, does not affect counts).
    _seed_contract_page(storage=storage, branch_hint=branch_hint)

    # Stage 2: seed the discipline pages (idempotent; separate count keys).
    disciplines_created, disciplines_skipped = _seed_discipline_pages(
        storage=storage, branch_hint=branch_hint
    )

    return {
        "seeded": True,
        "created": created,
        "skipped": skipped,
        "patterns": [p for p, _, _ in STARTER_PROMPTS],
        "disciplines_created": disciplines_created,
        "disciplines_skipped": disciplines_skipped,
        "disciplines": [n for n, _, _ in DISCIPLINES],
    }


@observe(tier="stage", metric="tools.agent_prompts._read_agent_prompt")
def _read_agent_prompt(slug: str, storage=None) -> dict | None:
    """Internal exact-key slug read for an agent-prompt page.

    v5.85 S4/S5: replaces the removed agent_prompt_get MCP tool. This is a
    deterministic key read (slug == agent-prompt-<pattern>), NOT semantic recall —
    used by agent_dispatch_prelude. Semantic lookup is recall(type="wiki",
    tags=["agent-prompt"]) instead.

    Args:
        slug: Deterministic page slug, e.g. "agent-prompt-dispatch-fix-bug".
        storage: StorageEngine instance (injected for testing; otherwise
                 resolved from server lifecycle).

    Returns:
        {"version": N, "slug": "...", "content": "...", "page_id": ..., "tags": [...],
         "title": "..."} or None if no page exists for the slug.
    """
    if storage is None:
        from yadgar._shared.runtime.lifecycle import _get_storage  # noqa: PLC0415

        storage = _get_storage()

    try:
        page = storage.get_wiki_page_by_slug(slug)
    except Exception as e:
        logger.debug("_read_agent_prompt slug lookup failed: %s", e)
        return None

    if page is None:
        return None

    page_id = storage._extract_id(page.get("id"))
    if page_id is None:
        return None

    version = storage.get_max_version_for_page(page_id)

    return {
        "version": version,
        "slug": slug,
        "content": page.get("content", ""),
        "page_id": page_id,
        "tags": page.get("tags", []),
        "title": page.get("title", ""),
    }
