"""Agent-prompt library MCP tools — v5.85 S4/S5 + 0047 Car I.

Tools:
  agent_prompt_save(pattern, content) — upserts one page per pattern +
    mirrors the body in the ``agent_pattern`` ledger row
  agent_prompt_list(...) — uses-DESC list of patterns from the ledger
  agent_prompt_get(pattern, ...) — single-row lookup, returns ledger metadata +
    the wiki body page in one round-trip

Storage convention (v5.85 rework + 0047 Car I):
  - Slug pattern: agent-prompt-<task-pattern>  (deterministic, no -vN suffix)
  - Tags: ["agent-prompt", "task:<pattern>"]
  - Category: "reference"
  - page_type: "agent_pattern" (ADR-0209; was "agent_prompt" pre-split.
    Discipline pages carry "agent_discipline", the TOC "agent_index" RETIRED).
  - wiki versioning (wiki_page_version table) carries history.
  - ``agent_pattern`` ledger row mirrors the body via content_hash (D40); the
    row is the discovery surface (list/get) and the page is the editable body.

Retrieval (S4/S5 collapse + 0047 Car I):
  - Semantic lookup is now `recall(type="wiki", tags=["agent-prompt"])`
    (the bespoke agent_prompt_search tool was removed; the SQL pre-filter lives
    in WikiStore.query via search_wiki_vectors_tagged).
  - Ledger lookup is `agent_prompt_list` / `agent_prompt_get` — backed by the
    ``agent_pattern`` MariaDB table, replaces the old wiki-TOC page scan.
  - Internal exact-key helper stays `_read_agent_prompt(slug, storage)`
    (dispatch_helper uses this).
"""

from __future__ import annotations

import hashlib
import logging
import re

from yadgar._shared.errors import UnresolvedProjectError
from yadgar._shared.observability.observe import observe
from yadgar._shared.security.secrets import gate_or_reject
from yadgar._shared.wiki.prompt_guard import removed_prompt_lines
from yadgar._shared.wiki.wiki_meta import (
    PAGE_TYPE_AGENT_DISCIPLINE,
    PAGE_TYPE_AGENT_PATTERN,
)
from yadgar.core.forward import _forward_admin
from yadgar.core.server._app import _tool
from yadgar.core.server.tools._project_param import (
    accept_project_param,
    resolve_effective_project,
)

logger = logging.getLogger(__name__)

# ── S6 discovery surface (0047 Car I: pointer-only) ───────────────────────────
# The wiki-TOC page ("agent-prompt-toc") is RETIRED as the discovery surface —
# the agent_pattern ledger row IS the row, the wiki body page is the body, and
# ``agent_prompt_list`` / ``agent_prompt_get`` are the read tools. D35d keeps
# the slug available as a kept-ignored pointer so callers that still pin the
# old slug see an explanatory page rather than a 404 (one cycle of soft retire).
_TOC_POINTER_SLUG = "agent-prompt-toc"
_TOC_POINTER_TITLE = "Agent Prompt TOC (retired — see agent_prompt_list)"


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
# which now forwards to POST /admin).
#
# 0047 Car I: TOC-upsert + library-anchor are RETIRED entirely. The discovery
# surface is the ``agent_pattern`` ledger row written below in agent_prompt_save
# via ``save_agent_pattern_row`` (page-first ordering: a crash leaves an orphan
# page, not an orphan row; ``check_page_row_desync`` is the detection arm).
# project.py's TOC scan re-points to the ledger table.


@_tool()
def agent_prompt_save(
    pattern: str,
    content: str,
    directory: str | None = None,
    purpose: str | None = None,
    storage=None,
    *,
    project: str | None = None,
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

    # C5 (0047 PR#40 §5): resolve the identity BEFORE the secret gate so an
    # unnamed project fails as a structured envelope rather than after a scan.
    try:
        _project_id = resolve_effective_project(
            project=project,
            directory=_effective_dir,
            session_project=None,
            tool="agent_prompt_save",
        )
    except UnresolvedProjectError as exc:
        return {"saved": False, **exc.payload}

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
    # the wiki body write forwards to the backend /admin op. The `storage=` test
    # seam is dropped on the forward path — bypass tests use admin_backend_bypass.
    # The wiki.add → _bump_wiki_epoch hook busts the core agent_prompt_prelude
    # cache namespace cross-process (file-backed epoch, Car 2).
    page_result = _forward_admin(
        "agent_prompt_save",
        {
            "slug": slug,
            "title": title,
            "full_content": full_content,
            "tags": tags,
            "pattern": pattern,
            "purpose": _purpose,
            "directory": _effective_dir,
            # C4b (0047 PR#40 §5): the enqueue-time identity, resolved HERE —
            # the process that can see the session — because the backend op
            # that mints the row runs in a container with no git binary and no
            # host project mounts (ADR-0227 §1.1). C5: the fallback C4b
            # deliberately kept symmetric with ``wiki_add``'s is deleted on both
            # sides at once, so an unnamed project raises here too.
            "project_id": _project_id,
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
    # 0047 Car I: after the page write, mirror the body in the agent_pattern
    # ledger row (the discovery surface). Page-first ordering per §9: a crash
    # between the two forwards leaves an orphan page (detected by
    # check_page_row_desync), not an orphan row. content_hash pins the row
    # to the wiki body's bytes (D40). Disciplines are mirrored below in
    # discipline_save via _save_discipline_page.
    if page_result.get("saved"):
        try:
            _forward_admin(
                "save_agent_pattern_row",
                {
                    "name": pattern,
                    "body_slug": slug,
                    "content_hash": _content_hash(full_content),
                    "purpose": _purpose,
                    "status": "active",
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "agent_pattern ledger write failed for pattern=%s: %s",
                pattern,
                exc,
            )
            # Page-first ordering: surface the page result with a row-side note.
            return {
                **page_result,
                "ledger_warning": f"agent_pattern row not written: {exc}",
            }
    return page_result


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
    project: str | None = None,
) -> bool:
    """Idempotently seed the prelude contract wiki page (create-if-absent).

    Separate from seed_agent_prompts so the 4-starter counts/assertions are
    not disturbed. Returns True if a new page was created, False if skipped.

    C5 (0047 PR#40 §5): ``project`` is threaded from the seeder's invoker, not
    defaulted. ``directory="global"`` declares the page's REACH; §1.4 keeps
    reach and ownership separate, so it can no longer double as the owner.
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
        storage=storage,
        project=project,
    )
    return True


@observe(tier="stage", metric="tools.agent_prompts._save_discipline_page")
def _save_discipline_page(
    name: str,
    purpose: str,
    content: str,
    project_id: str | None = None,
) -> dict:
    """Save (upsert) a discipline page under slug agent-discipline-<name>.

    Same write path as agent_prompt_save (I26 secret gate core-side, DB write
    forwarded to the backend agent_prompt_save admin op — the op keys everything
    off the payload slug). Disciplines keep the same Purpose/Prompt wrap + lint
    shape as patterns, but carry their OWN page_type since ADR-0209
    (``agent_discipline``) — ADR-0208 gives them different governance (the
    asymmetric removal guard), and page_type is the lever that governance keys
    off, not the slug prefix.

    0047 Car I: AFTER the wiki page write, mirror the body in the
    ``agent_discipline`` ledger row (page-first ordering per §9). The ledger
    row keys on the slug (``agent-discipline-<name>``), same as the wiki
    body page. ``check_page_row_desync`` is the detection arm for the gap.

    C4b (0047 PR#40 §5): this is the SECOND ``agent_prompt_save`` forward site
    — C3's precedent on ``WikiAddOptions`` was explicitly "BOTH construction
    sites", and a stamp on only one of them leaves half the agent-prompt
    corpus unattributed. ``project_id`` is resolved by the caller.

    **C5: the ``GLOBAL_FALLBACK`` default is DELETED (C4b handoff #4).** It read
    the page's declared ``directory="global"`` reach as if it were an owner,
    which is exactly the conflation §1.4 separates — and it fed a value that C5
    also adds to the drainer's ``_SENTINEL_PROJECT_IDS``, so keeping the default
    while adding the sentinel would DLQ every seeded discipline page. Both edits
    land together. ``project_id`` is now required: the seeder threads its
    invoker's value down, and a caller that has none gets a raise.
    """
    if not project_id:
        raise UnresolvedProjectError(
            "discipline_save",
            detail=f"(discipline page {DISCIPLINE_SLUG_PREFIX}{name!s} has no owning project)",
        )

    _gate = gate_or_reject(content)
    if _gate is not None:
        return _gate

    slug = f"{DISCIPLINE_SLUG_PREFIX}{name}"
    title = f"Agent Discipline: {name}"
    tags = ["agent-prompt", "agent-discipline", f"discipline:{name}"]
    content = _unwrap_purpose_prompt(content)
    full_content = f"## Purpose\n\n{purpose}\n\n## Prompt\n\n{content}"
    page_result = _forward_admin(
        "agent_prompt_save",
        {
            "slug": slug,
            "title": title,
            "full_content": full_content,
            "tags": tags,
            # Ledger row keys on the slug so discipline rows are unambiguous
            # next to dispatch-pattern rows.
            "pattern": slug,
            "purpose": purpose,
            "directory": "global",
            # C5: required, guarded above. ``"global"`` is now a DLQ sentinel.
            "project_id": project_id,
            "page_type": PAGE_TYPE_AGENT_DISCIPLINE,
        },
    )
    if page_result.get("saved"):
        try:
            _forward_admin(
                "save_agent_discipline_row",
                {
                    "name": name,
                    "body_slug": slug,
                    "content_hash": _content_hash(full_content),
                    "meta": {"purpose": purpose, "status": "active"},
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "agent_discipline ledger write failed for name=%s: %s",
                name,
                exc,
            )
            return {
                **page_result,
                "ledger_warning": f"agent_discipline row not written: {exc}",
            }
    return page_result


#: ADR-0208 line-delta primitive. The BODY moved to
#: ``yadgar._shared.wiki.prompt_guard`` (task 23) so the wiki write chokepoint
#: in ``_shared/wiki/store.py`` can enforce the same rule on the generic edit
#: tools — ``_shared`` may not import core. Re-bound here (not reimplemented)
#: so this module's own callers and tests keep their import path.
_removed_prompt_lines = removed_prompt_lines


@_tool()
def discipline_save(
    name: str,
    content: str,
    purpose: str | None = None,
    confirm_removal: bool = False,
    *,
    project: str | None = None,
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
    # C4b (0047 PR#40 §5): this tool HAS a session, so it names the owner.
    # ``directory=None`` on purpose — a discipline page declares
    # ``directory="global"`` REACH, and reach is not ownership (§1.4).
    # C5: absent an override this now raises instead of answering "global".
    try:
        _project_id = resolve_effective_project(
            project=project,
            directory=None,
            session_project=None,
            tool="discipline_save",
        )
    except UnresolvedProjectError as exc:
        return {"saved": False, **exc.payload}
    return _save_discipline_page(name, _purpose, new_body, project_id=_project_id)


@observe(tier="stage", metric="tools.agent_prompts._seed_discipline_pages")
def _seed_discipline_pages(
    storage,
    only: str | None = None,
    project: str | None = None,
) -> tuple[int, int]:
    """Idempotently seed the discipline pages (create-if-absent per name).

    Args:
        storage: StorageEngine used for the existence check.
        only: When set, seed just this discipline name (Stage-3 seed-on-miss
              path from prelude composition). Unknown names are a no-op.
        project: Owning project_id, threaded from the invoker (C5). The seeder
            has no session of its own, so this is the ONLY source — absent it,
            ``_save_discipline_page`` raises rather than stamping ``"global"``.

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
        _save_discipline_page(name, purpose, content, project_id=project)
        created += 1
    return created, skipped


@_tool(power=True)
def seed_agent_prompts(
    storage=None,
    *,
    project: str | None = None,
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
        project: Owning project_id for every page this seeds. **Required in
                 practice (C5 / ADR-0227):** the library pages declare
                 ``directory="global"`` REACH, which used to double as their
                 owner via ``GLOBAL_FALLBACK``; §1.4 separates the two, so the
                 seeder must be told whose namespace the rows belong to. Absent
                 it, the first write raises ``UnresolvedProjectError``.

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
                storage=storage,
                project=project,
            )
            created += 1

    # Seed the prelude contract page alongside (idempotent, does not affect counts).
    _seed_contract_page(storage=storage, project=project)

    # Stage 2: seed the discipline pages (idempotent; separate count keys).
    disciplines_created, disciplines_skipped = _seed_discipline_pages(
        storage=storage, project=project
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


# ── 0047 Car I: ledger-backed discovery tools ────────────────────────────────


@observe(tier="hot", metric="tools.agent_prompts._content_hash")
def _content_hash(text: str) -> str:
    """Stable sha256 hex digest of text content.

    The ``agent_pattern.content_hash`` / ``agent_discipline.content_hash``
    column pins the ledger row to the wiki body bytes for ``check_page_row_desync``
    (invariant arm in admin_exec/invariants_cross_engine.py). Same algorithm
    everywhere — content_hash equality across engines is the contract.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@_tool(power=True)
def agent_prompt_list(
    status: str | None = None,
    directory: str | None = None,  # noqa: ARG001 — accepted for tool surface parity; ledger is reach-global (D3)
    limit: int = 20,
    *,
    project: str | None = None,
) -> dict:
    """List agent_prompt library entries from the ``agent_pattern`` ledger table.

    0047 Car I: replaces the S6 wiki-TOC scan (the TOC page
    ``agent-prompt-toc`` is retired; the kept-ignored pointer slug remains so
    legacy callers see an explanatory page rather than 404, D35d).

    Ordering: ``uses`` DESC, ``name`` ASC. ``uses`` is the D40 SQL integer
    bumped by ``increment_agent_pattern_uses`` on each dispatch — replaces the
    old memory-row read-modify-write path.

    Args:
        status: Optional filter (``"active"`` is the only known status today;
                ``None`` returns every row). Reaches the backend op as a passthrough.
        directory: Accepted for tool surface parity (recall/seed tools take it).
                    The ledger table is reach-global (D3); the argument is unused.
        limit: Max rows returned (default 20, mirrors the old TOC page's 20-row cap).

    Returns:
        On success: {"patterns": [{"name", "purpose", "uses", "status",
                                    "body_slug", "content_hash"}, ...],
                       "count": N, "engine": "mariadb"}.
        On engine unavailable: {"ok": False, "error": "...", "patterns": []}.
    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; C7 re-keys
    # this tool's scope from ``directory`` onto the resolved project_id.
    accept_project_param(project, directory)
    payload: dict = {"limit": int(limit)}
    if status is not None:
        payload["status"] = status
    result = _forward_admin("list_agent_pattern_rows_uses_desc", payload)
    if result.get("ok") is False:
        return result
    rows = result.get("rows") or []
    return {
        "patterns": [
            {
                "name": row.get("name"),
                "purpose": row.get("purpose") or "",
                "uses": int(row.get("uses") or 0),
                "status": row.get("status") or "active",
                "body_slug": row.get("body_slug") or f"agent-prompt-{row.get('name')}",
                "content_hash": row.get("content_hash") or "",
            }
            for row in rows
        ],
        "count": len(rows),
        "engine": "mariadb",
    }


@_tool(power=True)
def agent_prompt_get(
    pattern: str,
    directory: str | None = None,
    *,
    project: str | None = None,  # noqa: ARG001 — accepted for tool surface parity; ledger is reach-global (D3)
) -> dict:
    """Read a single agent_prompt library entry (ledger row + wiki body).

    0047 Car I: replaces the bespoke ``agent_prompt_get`` removal path (the old
    MCP tool was retired in v5.85 S5; this is its table-backed re-introduction,
    now reaching the ``agent_pattern`` ledger row first, then the wiki body
    page keyed by ``body_slug``). Returns both surfaces so callers can render
    the row metadata + the editable body without two round-trips.

    Args:
        pattern: Task pattern identifier, e.g. ``"dispatch-fix-bug"``.
                 Equivalent to ``agent_pattern.name``.
        directory: Accepted for tool surface parity; the ledger table is
                    reach-global (D3); the argument is unused.

    Returns:
        On success: {"name", "purpose", "uses", "status", "body_slug",
                     "content_hash", "baseline_hash",
                     "content" (wiki body), "version" (wiki version N),
                     "page_id", "title", "tags"}.
        On absent row: {"error": "not_found", "name": pattern}.
        On engine unavailable: {"ok": False, "error": "..."}.
    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; C7 re-keys
    # this tool's scope from ``directory`` onto the resolved project_id.
    accept_project_param(project, directory)
    row_result = _forward_admin("get_agent_pattern_row", {"name": pattern})
    if row_result.get("ok") is False:
        return row_result
    row = row_result.get("row")
    if row is None:
        return {"error": "not_found", "name": pattern}

    body_slug = row.get("body_slug") or f"agent-prompt-{pattern}"
    page = _read_agent_prompt(body_slug)
    content = page["content"] if page is not None else ""
    version = page["version"] if page is not None else 0
    page_id = page["page_id"] if page is not None else None

    return {
        "name": row.get("name"),
        "purpose": row.get("purpose") or "",
        "uses": int(row.get("uses") or 0),
        "status": row.get("status") or "active",
        "body_slug": body_slug,
        "content_hash": row.get("content_hash") or "",
        "baseline_hash": row.get("baseline_hash"),
        "content": content,
        "version": version,
        "page_id": page_id,
        "title": (page or {}).get("title", f"Agent Prompt: {pattern}"),
        "tags": (page or {}).get("tags", ["agent-prompt", f"task:{pattern}"]),
    }
