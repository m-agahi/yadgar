# PLAN — v5.64: Wiki edit primitives + metadata maintenance (SKELETON)

**Status:** SKELETON drafted 2026-06-04. Origin: live UX failure 2026-06-04 — user attempted roadmap update; legacy preamble (pre-v5.41.1) was unreachable without 46k-char `wiki_update` full-content replace (corruption-class risk). Memory anchor id 523183 (heat=1, importance=1, protected) captured initial scope.

**Supersedes:** `docs/PLAN_V5_63_WIKI_CORPUS_MAINTENANCE_TOOLS.md` (skeleton on branch `docs/v5.63-skeleton` @ 0881700). v5.63's two metadata tools fold into `wiki_set_metadata`. Migration 019 carries over. Delete v5.63 branch once v5.64 lands.

**Not blocking.** Adjacent to v5.61 (repo-wiki native) + v5.62 (yadgar CLI) + v5.65 (fresh memory access UX).

## Problem

Current toolbox cannot surgically edit wiki pages:
- `wiki_add` = create or overwrite (whole content)
- `wiki_update` = full-content replace OR fields allowlist (`tags`, `category`, `confidence`, `content`)
- `wiki_append_section` = only `##` heading targets
- `wiki_restore` = version revert
- `wiki_delete` = whole page

Gaps:
- Text-level edits BEFORE the first `##` heading (preambles, bold blocks, blockquotes)
- Surgical fixes within long pages (one paragraph in a 40k-char roadmap)
- Metadata fields `directory_context` + `branch` excluded from `wiki_update` allowlist
- Off-by-one caller bugs corrupt page silently (no anchor verification)

## Design notes (settled via design discussion 2026-06-04)

- **Pages live in SurrealDB.** No filesystem editing. DB serializes writes within a transaction. Concurrent-edit race is rare in single-user yadgar (single Claude main thread + few subagents). Last-writer-wins is acceptable.
- **`expected_version` not mandatory.** Adds caller overhead, defends against a low-probability race. Make it an opt-in safety belt only; default off. If a real production user hits a race, ship `wiki_compare_and_swap_content` later as v5.66 escape hatch.
- **`anchor_hint` MANDATORY for positional ops.** Real failure mode is caller arithmetic bugs (off-by-one in line/col), not concurrent writes. anchor_hint = "expected text at coords starts with X" verification catches that. Min length 20 chars enforced server-side.
- **Anchor-text-first API.** Caller never computes line/col for the common case. Positional is escape hatch for duplicate-text disambiguation, multi-line blocks, blank-line edits.
- **No `wiki_apply_diff`.** Server-side unified-diff parser is high complexity; primitives cover practical cases. Caller can compose multiple primitive calls in a sequence.
- **All edits go through v5.41 versioning.** Each call creates a new `wiki_page_version` row. v5.39 similarity gate bypassed for revisions (revision != novel page). Each edit tagged with `provenance_agent`. Atomic at transaction level.
- **Conformance:** P1-P11 architecture invariants required. Caller-context (`directory` + `branch_hint`) per v5.42.3 + v5.42.5 contracts.

## Scope

Four tool layers + optional migration.

### Layer 1 — Anchor-text primitives (primary API)

Caller never computes coords. Server finds + applies. No `wiki_read` round-trip required.

```python
wiki_replace_text(slug, old_text, new_text, occurrences=1|N|'all', directory, branch_hint)
wiki_delete_text(slug, text, occurrences=1|N|'all', directory, branch_hint)
wiki_insert_after(slug, anchor_text, new_text, directory, branch_hint)
wiki_insert_before(slug, anchor_text, new_text, directory, branch_hint)
```

Contract:
- `old_text` / `anchor_text` must be unique unless `occurrences` is explicit
- Server rejects if found-count mismatches `occurrences` (e.g., caller said `occurrences=1` but text matches 3 places)
- `occurrences='all'` replaces every match
- Idempotent no-op if `old_text == new_text` or text already absent (delete case)
- Returns: `{ok, page_id, version_id, replaced_count, length_delta}`

### Layer 2 — Positional escape hatch (anchor_hint mandatory)

For ambiguous cases anchor-text can't solve.

```python
wiki_replace_at(slug, line, col, length, new_text, anchor_hint, directory, branch_hint)
wiki_delete_at(slug, line, col, length, anchor_hint, directory, branch_hint)
wiki_insert_at(slug, line, col, new_text, anchor_hint, directory, branch_hint)
```

Contract:
- `anchor_hint` MUST be ≥20 chars. Server rejects shorter hints.
- For `replace_at` / `delete_at`: server verifies actual text at `(line, col, length)` starts with `anchor_hint`. Mismatch → reject.
- For `insert_at`: `anchor_hint` = expected text immediately BEFORE the insertion point. Mismatch → reject.
- `line` / `col` are 1-indexed (markdown convention). `length` measured in chars, not bytes.
- Returns: `{ok, page_id, version_id, applied, length_delta}`. Mismatch returns `{ok: false, reason: "anchor_hint mismatch", actual_text_preview: "..."}`

### Layer 3 — Structural

```python
wiki_replace_markdown_block(slug, block_type, block_index, new_content, directory, branch_hint)
```

- `block_type`: `paragraph`, `heading`, `code_fence`, `blockquote`, `list`, `table`
- `block_index`: 0-based, scoped to `block_type`
- Server parses markdown structure, locates the Nth block of `block_type`, replaces it
- Useful for: replace the 3rd code fence, swap a heading, rewrite a blockquote

**Extension of existing tool:**

```python
wiki_append_section(slug, heading, content, ...)  # already exists
```

Extend matcher to accept `**bold**` and `> blockquote` first-line patterns (opt-in via new param `heading_type=h2|h3|bold|blockquote`, default `h2` for backward compat).

### Layer 4 — Metadata (folds in v5.63)

```python
wiki_set_metadata(slug, field, value, directory, branch_hint)
```

- `field` ∈ `{directory_context, branch}`
- `value` validated per field:
  - `directory_context`: `"global"` or absolute path
  - `branch`: `null` (canonical) or non-empty string
- Idempotent no-op if current value matches
- Logs old + new + caller_agent

**Replaces:** `wiki_reclassify_directory` and `wiki_set_branch` from v5.63 plan. Single tool, parameterized.

### Optional — Migration 019 (carryover from v5.63)

Bulk-NULL legacy `branch="master"` rows where `directory_context != "global"`. Gated by env knob `YADGAR_MIGRATION_019_AUTO_NULL_BRANCH=true` (default `false`).

Origin: ~200 pages stamped `branch="master"` by pre-v5.42.2 drainer default. Architecture says project-tagged pages are branch-invariant by default; `NULL` (canonical) is the correct slot.

Pages with `branch="master"` AND `directory_context="global"` left alone (intentional pre-v5.42.5 default).

## Out of scope

- `wiki_apply_diff` — dropped per 2026-06-04 design discussion
- `expected_version` mandatory — dropped; optional opt-in safety belt only
- `wiki_compare_and_swap_content(slug, expected_hash, new_content)` — v5.66 if race ever observed in production
- Auto-classification UI / repo-wiki regen drivers — v5.50 viz + v5.61
- CLI subcommands (`yadgar wiki-edit ...`) — v5.62
- `wiki_find_position(slug, search_text)` helper — caller can compute line/col from `wiki_read` if needed for positional layer; trivial caller-side

## Acceptance

- 4 anchor-text tools + 3 positional tools + 1 structural tool + 1 metadata tool callable via MCP
- Positional tools reject `anchor_hint` < 20 chars
- Positional tools reject when `anchor_hint` doesn't match actual text at coords
- Anchor-text tools reject when `occurrences` mismatches found count
- All edits create new `wiki_page_version` row (v5.41 versioning)
- All edits log `provenance_agent`
- Caller-context validated (P1-P11)
- Audit-found misclassified page corrected: `wiki_set_metadata('aws-vpc-terraform-...', 'directory_context', '/home/max/aws-work')` succeeds, page findable in aws-work bucket
- Migration 019 (optional) bulk-cleans `branch="master"` legacy when knob is on
- Roadmap maintenance flow (origin failure) works: `wiki_replace_text(roadmap_slug, "legacy preamble v5.41.1...", "...new preamble...")` succeeds without 46k-char round-trip

## Effort estimate

~3-4 cal-days. 8 tools + parser for markdown blocks + 15-20 tests + optional migration.

## Cross-references

- `docs/PLAN_V5_63_WIKI_CORPUS_MAINTENANCE_TOOLS.md` — **superseded** by this plan; delete branch once v5.64 lands
- `docs/PLAN_V5_65_FRESH_MEMORY_ACCESS.md` — adjacent (memory access UX, separate scope)
- `[[yadgar-directory-branch-contract-v5-42-3-5-architecture]]` — semantic categories model
- `[[yadgar-wiki-200-page-corpus-analysis-reclassification-plan-2026-]]` — audit findings
- `docs/PLAN_V5_42_5_DIRECTORY_CONTRACT.md` — directory contract baseline
- `docs/PLAN_V5_42_6_DIRECTORY_BACKFILL_AND_RESOLUTION_FIX.md` — hotfix
- `docs/PLAN_V5_61_REPO_WIKI_YADGAR_NATIVE.md` — repo-wiki regen
- `docs/PLAN_V5_62_YADGAR_CLI.md` — CLI surfaces these tools
- Memory anchor id 523183 — initial v5.64 slot idea (origin)

## Defer rationale

Functional state is correct — pages durable, retrievable, editable via full-content `wiki_update`. v5.64 is UX + safety polish. Schedule after v5.42.x cleanup cycle settles.
