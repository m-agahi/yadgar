> ARCHIVED 2026-07-27 — executing on chore/decommission-repo-wiki, ships with this PR

# repo-wiki page-type + policy resolver — 2026-07-22

**Task:** #83 rework (write-seam bug found live in v5.160.0).
**Branch:** `feat/repo-wiki-page-type`
**Status:** SHIPPED — Cars A–E done. Core 5.161.0 / Backend 5.58.4.

## Car Status

| Car | Description | Status |
|-----|-------------|--------|
| A | Shared slugify, wiki_policy, repo_wiki schema | ✓ done (commit 692084ea) |
| B | Backend gate dir-scope + identity + upsert-at-slug | ✓ done (commit 24843b8b, backend bump 5.58.2→5.58.4) |
| C | wiki_add slug+upsert params + policy-driven recall exclusion | ✓ done (commit b22b2712) |
| C2 | storage_scope schema enforcement — agent_prompt pages always global | ✓ done (commit 006a756a) |
| D | Generator/CLI/prompt via repo_wiki schema | ✓ done (commit e17f1140) |
| E | Integration proof + core version bump + plan finalize | ✓ done (this commit) |

**Final versions:** core 5.161.0 / backend 5.58.4

## Why (the live bug)

v5.160.0 shipped the repo-wiki refresh loop, but it is **broken at the write
seam** and would produce garbage on first bulk regen. Proven live:

1. Generator emits pages with slug `mod-<modpath>` + crossrefs `[[mod-*]]`;
   stale-diff keys on those slugs.
2. But `wiki_add` derives the stored slug from the **title**, and `replace_slug`
   **silently falls back to the title-slug when the target slug is absent**
   (probed: `replace_slug="mod-zzz-…"` on a new page → stored at `zzz-…`).
3. So first bulk regen (all pages "new") lands 431 pages at **title-slugs**, not
   `mod-*`. Consequence: every `[[mod-*]]` crossref dead, AND every later
   stale-diff sees baseline title-slugs vs generator `mod-*` → all "new" + all
   baseline "deleted" → **delete-all/re-add-all churn every cadence.**

Three further defects surfaced during design:

- **Slug collision (cross-project):** `mod-benchmarks-*` has no project namespace;
  a second project with `benchmarks/` collides in the global slug keyspace.
- **Slugify drift:** generator has its own `_slugify` (swaps `._`, no 64-cap,
  hardcoded `mod-`) diverging from live `WikiStore._slugify`
  (`re.sub(r"[^a-z0-9]+","-", …)`, 64-cap). Long paths already mismatch.
- **Similarity-gate cross-block:** `find_similar_wiki_pages` (store.py:1063) →
  `search_wiki_vectors` (storage/wiki.py:734) is **branch-scoped only, no
  `directory_context`**. Two projects' thin `logging.py` (cosine ≥0.80) →
  second rejected `duplicate_detected`. The original prompt papered this over
  with `force=True` — a safeguard bypass. **Rejected.** Structural pages are
  identity-keyed; content-similarity is the wrong guard for them.

## Design principle

Do **not** circumvent the similarity gate. Make repo-wiki pages a **first-class,
schema-backed page type** (like ADR / task_list) whose write is *validated*, not
*bypassed*. Behaviour of every gate/pipeline is routed by a **single policy
resolver keyed by `page_type`** — knobs in code, not in per-row data.

Key invariants:
- `directory_context` is the hard scoping key (baked into slug + honored by gate).
- `page_type` routes behaviour but is **schema-validated** (can't forge
  `repo_wiki` without `source_file`+`hash`+`{project}-mod-*` slug). It stays
  **non-canonical** for security decisions (branch/canonical-write) per ADR §0.6.

## Slug scheme (locked)

`{project}-mod-<slugify(module_name)>` — project-first, TOC-consistent
(`{project}-repo-wiki-index`). e.g. `yadgar-mod-yadgar-shared-embeddings`,
`yadgar-mod-benchmarks-od-diagnostic`. Built via one shared `slugify()` over the
full string (uniform charset + 64-cap). Crossrefs + stale-diff key + TOC all use
the same fn.

## Components

### Car A — shared foundation (no behaviour change; pure additions)
- Hoist `WikiStore._slugify` → shared pure `slugify(title)` in
  `yadgar/_shared/wiki/` (single source). `WikiStore._slugify` delegates.
- `wiki_policy` module (`yadgar/_shared/wiki/policy.py`): `page_type` →
  `{gate_mode, recall_disposition, dir_scope, merge}`. Axes now:
  - `gate_mode`: `similarity` (default) | `identity` (slug+schema, skip sim)
  - `recall_disposition`: `include` (default) | `exclude` | `downweight`
  - `dir_scope`: `strict` | `global`
  - `merge`: `allow` (default) | `never`   *(stub for #19)*
  - `repo_wiki` → `{identity, exclude, strict, never}`. One-field flips.
- `repo_wiki` schema (`yadgar/_shared/wiki/repo_wiki_schema.py` or fold into
  contract): deterministic slug fn `repo_wiki_slug(project, module_name)`,
  required fields (`source_file`, `hash`), `validate_repo_wiki_page()`.
- **TDD:** slugify parity vs old daemon behaviour; policy resolver returns
  correct disposition per type + unknown-type default; schema validation
  accept/reject; `repo_wiki_slug` cross-project distinctness.

### Car B — backend gate + upsert (depends A; backend bump)
- `find_similar_wiki_pages` / `search_wiki_vectors`: accept + apply
  `directory_context` in the candidate filter (folds the general free-text
  cross-project gate fix).
- `_sim_gate_for_drainer` (dlq.py): read `wiki_policy.gate_mode(page_type)`.
  `identity` → skip content-similarity; enforce schema-valid + slug-unique.
- Write path (`write_exec` / `wiki_add_impl`): `upsert` semantics — create-or-
  overwrite at the **caller-supplied slug** (no title fallback), gate per policy.
  Reject malformed `repo_wiki` page (schema).
- **TDD:** gate dir-scoped (proj-A vs proj-B `logging.py` both stored);
  identity-mode skips sim but rejects schema-invalid; upsert creates at slug when
  absent + overwrites when present; upsert on non-structural still honors sim.

### Car C — core wiki_add param + recall exclude (depends A,B)
- `wiki_add` tool: `upsert: bool` param; plumb `page_type` → write path.
- `recall_pipeline.py:432` exclusion: drive from
  `wiki_policy.recall_disposition` (extend the existing agent-prompt exclusion),
  switchable. `wiki_query`/`wiki_read`/`wiki_list` still reach the pages.
- **TDD:** upsert param round-trips; recall excludes `repo_wiki` from fanout but
  `wiki_query` still returns it; flip disposition → included.

### Car D — generator + CLI + prompt (depends A)
- Generator: drop own `_slugify`; slug + crossrefs + TOC via shared
  `repo_wiki_slug` / `slugify`; build pages through the schema, not ad-hoc dicts;
  `page_type="repo_wiki"`.
- CLI `repo-wiki --stale-only`: keys align with the new slug (stale-diff
  reconciles).
- Prompt (`repo_wiki_refresh_prompt.md`): one write line
  `wiki_add(page_type="repo_wiki", upsert=True, hash=…, source_file=…, wait=True)`
  — no `force`, no `replace_slug` dance.
- **TDD:** generator slug == `repo_wiki_slug`; crossref targets all resolve to
  emitted slugs; TOC slug stable.

### Car E — integration + versions + docs
- Integration: full round-trip (generate → wiki_add upsert → wiki_list baseline →
  **second stale-diff of unchanged tree = empty pages+deleted**, proves no
  churn); cross-project no-collision (gate-level AND slug-level).
- Version bumps: core + backend (config 3-way sync if any new config).
- ADR: page-type policy resolver + `directory_context` gate-scoping.
- `#19` guard-note: future LLM-merge must respect `dir_scope`/`merge=never`.

## Sequence

A → (B, C, D can overlap once A lands) → E → PR → redeploy (core+backend) →
**then** live bulk regen (431 pages, background agent, `wait=True` per page) +
project pointer-anchor to `[[{project}-repo-wiki-index]]`.

No live damage meanwhile: repo-wiki is opt-in (ASK on absent TOC), nothing
generated yet.

## Follow-ups (not this PR)
- Recall disposition for `repo_wiki` revisit after dogfood (exclude → maybe
  downweight/include). One-field flip in `wiki_policy`.
- #101 full multi-language extractors on the registry seam.
- 64-char slug truncation-collision on very deep paths — round-trip dup-check
  catches if real; else defer.
