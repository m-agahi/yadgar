# PLAN — Native repo-wiki generation in yadgar (SKELETON)

Status: **SKELETON — drafted 2026-06-15.** Net-new (the old `PLAN_V5_61_REPO_WIKI_YADGAR_NATIVE` cross-ref was dangling — never written; the v5.61 slot went to wiki edit primitives). Needs scoping + a build-vs-keep-skill decision. Position: after the recall-scoping train (it depends on correct directory stamping) and alongside/after `[[unified-scoped-recall]]`.

theme: wiki-kb / tooling
priority: medium (quality-of-corpus; not blocking)

## Problem / motivation

Today the repo's code-structure wiki (the `fn-`/`mod-`/`api-` pages) is produced by an EXTERNAL Claude Code skill — `/repo-wiki:repo-wiki` — dispatched by the stop-hook for stale slugs. Consequences observed:
- **Directory mis-stamping at the source.** The skill writes via `wiki_add` without passing `directory_context` → the 364 yadgar `fn-`/`mod-` pages defaulted to `global` (the biggest single leak found in the recall-scoping audit). Native generation owns the stamp.
- **External dependency + opaque freshness.** Regen quality/cadence lives in a skill outside yadgar; yadgar can't guarantee it runs, stamps correctly, or stays fresh.
- **No structural model.** The skill emits prose pages; there's no code-graph (call graph, imports, god-nodes) — the kind of structure that makes a code wiki navigable.

## Vision

Yadgar natively generates + incrementally refreshes its own code-structure wiki for a repo: scan → extract structure → write/patch `code-*` wiki pages stamped to the repo's `directory_context`, with freshness tracking. No external skill required.

## Two scope options (DECIDE)

**Option A — Light (signatures + docstrings).** Walk the repo, emit one page per module/function with signature, docstring, and basic import/call refs. Cheap, deterministic, no LLM. Closest to what the skill does today, but native + correctly stamped.

**Option B — AST-graph (graphify §8.1 technique).** tree-sitter (multi-language AST) + `leidenalg` community detection → a code knowledge graph: call graph, import topology, "god nodes" (high centrality), communities → emit `code-graph-*` wiki pages + feed the viz graph. This is the technique the graphify competitor audit (`docs/competitor-graphify-2026-05-31.md` §8.1) flagged as a worthwhile spike — **the public technique (`py-tree-sitter` + `leidenalg`), NOT a graphify dependency** (audit verdict: nothing worth vendoring; ~50 LOC wrapper). Richer, but more build + an LLM pass for docs/PDFs if desired.

Lean: start Option A (correctness + ownership win), keep Option B as a follow-on spike once the corpus is clean.

## Leverage what now exists (didn't before)
- **v5.61 wiki edit primitives** — `wiki_set_metadata` (stamp `directory_context` correctly), `wiki_replace_text`/`wiki_append_section`/`wiki_replace_markdown_block` (incremental refresh without full rewrite). Native regen should PATCH pages, not overwrite.
- **`wiki_lint` hash-drift staleness** — already detects stale pages; the regen trigger.
- **recall-scoping write-time fixes** — native regen must pass `directory_context` (the root fix for the 364-page leak). This plan + recall-scoping-restamp §A converge here: fix the stamp at the write path.
- **`seed_project`** already scans a repo for `_seed` MEMORIES — native repo-wiki is the WIKI analog; consider sharing the scan layer (don't duplicate the repo walker).

## Where it runs (DECIDE)
- CLI subcommand (`yadgar repo-wiki [--repo PATH]`) — explicit, scriptable. (The old dangling `PLAN_V5_62_YADGAR_CLI` envisioned CLI surfaces.)
- and/or an MCP tool (agent-invocable).
- and/or a freshness-driven refresh (wiki_lint stale → regen) — but mind the v5.51 hook-latency budget; regen is heavy, not a hot-path hook.

## Relationship / dedupe
- **Replaces** the external `/repo-wiki:repo-wiki` skill (the stop-hook dispatch). Decide: deprecate the skill dispatch once native lands.
- **Shares** the repo-scan layer with `seed_project`.
- **Distinct from** `unified-scoped-recall` (that's retrieval; this is generation) — but native pages must be correctly stamped so unified recall scopes them right.

## Open questions
- Option A vs B (light vs AST-graph) — start A?
- Replace the skill outright, or native-generate + keep skill as fallback?
- Trigger: explicit CLI only, or freshness-driven (and where — nightly cycle vs hook)?
- Page granularity: per-function (many pages) vs per-module (fewer, denser)? The 364 existing `fn-` pages suggest per-function may be too granular / noisy.
- Does this make the per-function `fn-` page tier worth keeping at all, or collapse to per-module?

## Acceptance (once scoped)
- Native generation emits code-structure wiki pages stamped to the repo's `directory_context` (no `global` leak).
- Incremental refresh via edit primitives (patch, not overwrite); idempotent.
- Staleness-aware (wiki_lint integration).
- External repo-wiki skill dispatch retired or demoted to fallback.

## Related
- `docs/competitor-graphify-2026-05-31.md` §8.1 — the AST+Leiden spike (Option B technique)
- `[[wiki-edit-primitives]]` — the patch tooling this uses
- `[[recall-scoping-restamp]]` — converges on the write-time directory-stamp fix
- `[[wiki-kb-usefulness-snr]]` — corpus quality context
- `seed_project` — shared repo-scan layer
