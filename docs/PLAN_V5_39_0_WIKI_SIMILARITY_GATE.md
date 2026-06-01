# PLAN — v5.39.0: wiki_add title + content similarity gate (anti-duplicate)

**Status:** drafted 2026-06-01 night. Plan-first per I27.

**Origin:** 2026-05-30 saw duplicate roadmap pages slip in via `wiki_add` (different slugs, near-identical content). Caught manually. Bug class: `wiki_add` accepts any non-colliding slug regardless of content overlap with existing pages.

**Why now:** v5.41 (wiki versioning) ships next and depends on a clean page set. Better to land the gate first so v5.41 doesn't need to handle duplicates retroactively.

**Effort estimate:** 1-2 calendar days.

**Branch:** `feat/v5.39.0-wiki-similarity-gate` off master.

---

## 1. Problem

Current `wiki_add` semantics:

```python
wiki_add(slug, title, content, tags, category, confidence)
```

Validates: slug uniqueness within scope (branch + directory), required fields, length caps. Does NOT check: title similarity to existing pages, content overlap with existing pages.

Observed failure (2026-05-30):
- Page A: slug `yadgar-roadmap-future-improvements`, content X
- Agent dispatches second roadmap regen
- Page B: slug `yadgar-future-roadmap` (different slug, same content X with minor edits)
- Both pages persist. `wiki_query("roadmap")` returns both — agent confused which is canonical → next regen overwrites wrong one → corruption pattern triggered.

## 2. Goal

Block `wiki_add` from creating a near-duplicate of an existing page unless explicitly overridden.

Detect via:
- **Title similarity**: cosine on title embeddings ≥ `TITLE_SIM_THRESHOLD` (default 0.85)
- **Content similarity**: cosine on content embeddings ≥ `CONTENT_SIM_THRESHOLD` (default 0.80)
- Combined: either above threshold → flag as candidate duplicate

When flagged:
- Return error with the existing page slug + similarity scores
- Caller must explicitly pass `force=True` (or new `replace_slug=<existing>`) to proceed

Soft mode: log + warn but allow, for opt-in. Hard mode: reject.

## 3. Scope

### MCP tool signature change

```python
wiki_add(
    slug,
    title,
    content,
    tags=None,
    category="reference",
    confidence="medium",
    source_memory_ids=None,
    branch=None,
    branch_hint=None,
    append=False,
    force=False,                   # NEW — bypass similarity gate
    replace_slug=None,             # NEW — explicit "update this existing page"
)
```

- `force=False` (default): similarity gate enforces; raise if duplicate found.
- `force=True`: skip gate; log warning at INFO.
- `replace_slug=<slug>`: bypass gate, treat call as overwriting the named existing page (equivalent to `wiki_update` but accepts content+title+tags via wiki_add signature).

### Storage

No schema change. Embeddings already exist on `wiki_page` table.

### Helper: `find_similar_wiki_pages`

New internal function:
```python
def find_similar_wiki_pages(
    title: str,
    content: str,
    directory: str,
    branch: str,
    title_threshold: float = 0.85,
    content_threshold: float = 0.80,
    top_k: int = 5,
) -> list[dict]:
    """Return wiki pages in same scope with title OR content similarity above threshold."""
```

Uses existing embed service. Scope = same `directory_context` + branch resolution (current → default → unscoped, same as `wiki_read`).

### MCP tool: `wiki_check_duplicate` (NEW, power=False)

```python
wiki_check_duplicate(title, content, directory=None, branch=None) -> list[dict]
```

Caller-facing dry-run: returns candidate duplicates without writing anything. Useful for the v5.45 seed-anchors discussion + general agent caution.

### Config knobs (I25 registered)

- `WIKI_SIM_GATE_ENABLED: bool = True` — master switch
- `WIKI_SIM_TITLE_THRESHOLD: float = 0.85`
- `WIKI_SIM_CONTENT_THRESHOLD: float = 0.80`
- `WIKI_SIM_MODE: str = "hard"` — `hard` (reject) or `soft` (warn + allow)
- `WIKI_SIM_TOP_K: int = 5` — how many candidates to return on hit

## 4. Non-goals

- No retroactive deduplication of existing pages (separate v5.45+ work).
- No fuzzy matching on slug (slug is exact-match by design).
- No content edit-distance — use embedding similarity which already exists.
- No LLM-based duplicate resolution (that's v6 curator territory).
- No automatic merge of duplicates.

## 5. Open design questions

1. **Threshold defaults.** 0.85 / 0.80 are conservative guesses. Need empirical calibration: pull ~10 known-duplicates from history + ~10 known-distinct pairs, run similarity, pick thresholds that separate them. Time-box: 30min. Resolve during TDD.

2. **`replace_slug` semantics overlap with `append=True`?** Current `append=True` merges content into existing page (slug match). New `replace_slug` OVERWRITES content of a DIFFERENT named slug. Different ops, name clearly. Lean: keep both; document distinction. Open: maybe consolidate to single `mode=` param with values `new` / `append` / `replace`.

3. **Cross-scope check?** Should `wiki_add` flag near-duplicates in OTHER directories? Risk: false positives across unrelated projects. Lean: NO, same-scope only. Add `cross_scope=False` as future opt-in.

4. **Error vs structured response.** Raise ValueError vs return `{"status": "duplicate_detected", "candidates": [...]}`? Lean: raise — explicit contract violation. Agents handle errors better than implicit-result dicts.

5. **What does `wiki_update` (existing tool) do with similarity?** Should it ALSO gate? Probably not — `wiki_update` is page-id-targeted (no slug ambiguity); user is explicitly editing a known page. Leave unchanged.

6. **Performance.** Embedding the new title + content at write time + KNN over wiki_page table. SurrealKV HNSW index already exists. Latency budget: <100ms for the check. If wiki_page row count grows past ~10k, may need tier-1 filter (tag overlap) before vector search. Defer until benchmarked.

## 6. Test plan (TDD red-first)

`yadgar/tests/test_wiki_similarity_gate.py`:

- `test_distinct_pages_allowed` — wiki_add on title+content that has no neighbor passes.
- `test_near_duplicate_title_blocked` — title similarity > threshold raises with candidate list.
- `test_near_duplicate_content_blocked` — content similarity > threshold raises.
- `test_force_bypasses_gate` — `force=True` allows write + logs warning.
- `test_replace_slug_bypasses_gate` — `replace_slug=` allows overwrite of named existing page.
- `test_soft_mode_allows_with_warning` — `WIKI_SIM_MODE=soft` permits write, emits warning.
- `test_disabled_gate_no_check` — `WIKI_SIM_GATE_ENABLED=0` skips check entirely.
- `test_check_duplicate_dry_run` — `wiki_check_duplicate` returns candidates without writing.
- `test_threshold_boundary` — at exactly threshold, treated as duplicate (>=, not >).
- `test_cross_scope_isolation` — page in different directory doesn't trigger flag.
- `test_scope_branch_resolution` — current-branch + default-branch pages both candidate.
- `test_caller_distinguishes_append_vs_replace` — append=True still works; replace_slug=different is a different op.

I25 lint test: 5 new knobs registered three-way.

## 7. Acceptance criteria

1. `wiki_add` rejects near-duplicates by default (similarity ≥ thresholds).
2. `force=True` + `replace_slug=` bypass cleanly.
3. `wiki_check_duplicate` MCP tool works as dry-run.
4. 5 new knobs registered in `config.py` + `config_registry.py` + `config_yaml.py`; I25 lint passes.
5. All new tests green; existing wiki tests still pass.
6. CHANGELOG + MIGRATION_NOTES updated.
7. Version bumped 5.37.0 → 5.39.0 (skip 5.38 — D2/D3 moved to v5.57/v5.58).
8. Empirically-calibrated thresholds documented in plan + commit (record the 30min calibration session results).

## 8. Risks

- False positives. Calibration mitigates; soft mode escapes; user can lower thresholds.
- Performance on large wiki_page table. HNSW + tag pre-filter handles.
- Backward compat. Existing callers that produce duplicates will newly fail — intentional. Migration note guides toward `force=True` or `wiki_update` as appropriate.
- Interaction with v5.41 versioning. v5.41 will add history/diff/restore for explicit edits — orthogonal to dedup at create time. They compose cleanly.

## 9. Dependencies

- None hard.
- Soft: v5.31 plugin recall pipeline (not needed for this; standalone embed similarity).
- Composes with: v5.41 wiki versioning (orthogonal — versioning catches in-place edits; similarity gate catches duplicate creates).

## 10. References

- `yadgar/server/tools/wiki.py` — `wiki_add` implementation point
- `yadgar/storage/wiki.py` — storage layer for wiki_page
- `yadgar/storage/vector.py` — embedding + HNSW infrastructure
- 2026-05-30 corruption incident — `docs/PLAN_V5_41_0_WIKI_VERSIONING.md` references the same bug class

## 11. Implementation phases (for agent dispatch)

Phase 1 — Helper + tests
- `find_similar_wiki_pages()` in `yadgar/storage/wiki.py`
- `wiki_check_duplicate` MCP tool
- Tests for both
→ COMMIT `feat(wiki): find_similar_wiki_pages helper + wiki_check_duplicate MCP tool`

Phase 2 — Gate enforcement in wiki_add
- Add `force` + `replace_slug` params to `wiki_add`
- Wire similarity check
- Tests covering all paths
→ COMMIT `feat(wiki): similarity gate in wiki_add (force + replace_slug bypass)`

Phase 3 — Config knobs (I25)
- 5 knobs in config.py + config_registry.py + config_yaml.py
- Refactor gate to read from settings
→ COMMIT `feat(wiki): I25 env knob registration for WIKI_SIM_*`

Phase 4 — Calibration + docs
- 30min empirical threshold calibration with sample pairs; commit results
- CHANGELOG + MIGRATION_NOTES + README pointer
- Version bump 5.37.0 → 5.39.0
→ COMMIT `chore: bump version 5.37.0 → 5.39.0 + calibrated thresholds + docs`

Estimated 1-2d.
