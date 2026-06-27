# PLAN — Wiki auto-linking pass (`wiki_autolink`)

**Status:** PLANNED (NEW, v5.85 train car #5). Scoped 2026-06-26, code-grounded
against `master`. No plan existed before.
**Theme:** wiki / cross-refs / curation
**Effort:** M · **Risk:** M (over-linking / mutating curated pages — both mitigated)

---

## Goal

Auto-insert `[[slug]]` cross-references across wiki pages: scan each page's body for
mentions of *other pages' titles* and wrap the first (or each) occurrence in
`[[slug]]`, so the existing crossref/graph machinery picks them up. Surface-level
"connect the corpus" pass; **does NOT rewrite prose, does NOT synthesize.**

## How `[[slug]]` works today (verified)

- **Extraction:** `_extract_wikilinks(content)` (`yadgar/wiki.py:561`, `:586`) regex
  `\[\[([^\]]+)\]\]`, slugifies + dedupes, stored on `wiki_page.links`.
- **Crossref sync:** on `add()`/`update()`, links are written to the `wiki_crossref`
  table (directed edges for the 3D graph). Re-runs automatically on every write — so
  an auto-linker that rewrites content + re-`add()`s gets crossref sync for free.
- **No render transform:** `[[slug]]` stays literal markdown; the value is purely the
  crossref edges + `links` array.
- **Validation is lint-time only:** there is NO write-time check that a `[[slug]]`
  resolves. `wiki_lint()` (`wiki.py:914`) flags broken refs as warnings
  post-hoc. **Implication for us: an auto-linker must validate target-slug-exists
  BEFORE inserting** (we have the title→slug map anyway), so we never manufacture a
  broken ref.
- **No auto-linker exists** — grep for auto/autolink/backlink/crosslink found only
  read-side `get_wiki_backlinks` + crossref sync. This is greenfield.

## Approach (the `wiki_autolink` tool)

New `@_tool()` in `yadgar/server/tools/wiki.py`, copying the `wiki_lint`
registration pattern (`wiki.py:816`; access store via `_st._wiki`).

```python
@_tool()
def wiki_autolink(
    directory: str | None = None,
    dry_run: bool = True,          # default SAFE — report, don't mutate
    min_title_len: int = 6,        # skip short/ambiguous titles ("API", "DB")
    max_links_per_page: int = 20,  # cap churn per page
) -> dict:
    """Auto-insert [[slug]] cross-refs by matching other pages' titles in body text.
    dry_run=True (default): return proposed insertions {page, line, title→slug}.
    dry_run=False: apply via wiki add()-upsert (re-syncs crossrefs, bumps version)."""
```

Algorithm:
1. **Build a `title → slug` map** from `list_wiki_catalog(directory=directory)`
   (`storage/wiki.py:526`) — metadata-only (slug/title/category), directory-scoped
   (caller dir + 'global'), no heavy content load. Drop titles shorter than
   `min_title_len`; drop ambiguous/duplicate titles (same title → multiple slugs).
2. **For each candidate target page**, load content (`list_wiki_pages` or
   `read(slug)`), parse blocks via `_parse_markdown_blocks` (`wiki.py:164`).
3. **Match titles in body text**, but ONLY:
   - outside fenced code blocks (the section-heading finder already tracks fences,
     `wiki.py:325-338` — reuse that fence-tracking),
   - not already inside a `[[...]]` (regex can't nest → double-wrap corrupts),
   - verbatim title substring (not fuzzy) — avoids "API" matching everything,
   - skip self-links (page linking to its own slug).
4. **Optional semantic guard:** before inserting `[[B]]` into page A, require
   `find_similar_wiki_pages(A, ...)` (`wiki.py:778`) to rank B above a threshold
   (e.g. 0.70) — kills coincidental title collisions. Make this a flag (default on);
   it's the main false-positive defense.
5. **Idempotency:** skip any target slug already in `page.links`; skip spans already
   wrapped. Second run on an already-linked corpus → "0 changes."
6. **Apply** (`dry_run=False`) via `WikiStore.add()` upsert-by-slug → re-extracts
   links, re-syncs crossrefs, bumps version. Tag modified pages `auto-linked` so
   editors know.

## TDD outline (write failing first)

Follow `tests/test_wiki.py` fixture (`:24-37`, `init_engines` on tmp db, `_wiki()`)
+ the `TestCrossReferences` pattern (`:216-256`).

1. `test_autolink_inserts_link` — two pages, title of A mentioned in B's body →
   `wiki_autolink(dry_run=False)` makes B's content contain `[[a-slug]]`; B's `links`
   includes `a-slug`; a `wiki_crossref` edge B→A exists. *(red first.)*
2. `test_autolink_dry_run_no_mutation` — `dry_run=True` returns proposals but content
   is unchanged.
3. `test_autolink_skips_code_fences` — title inside a ```` ``` ```` block is NOT
   wrapped.
4. `test_autolink_idempotent` — run twice; second run reports 0 insertions, no
   double-wrap.
5. `test_autolink_skips_short_titles` — a 3-char title is never auto-linked.
6. `test_autolink_directory_scoped` — page in project A's title not linked into
   project B's page.
7. `test_autolink_semantic_guard` (if guard on) — coincidental title match below
   similarity threshold is rejected.

## Contracts / config touched

- **No BEHAVIOR_CONTRACT change** — crossref semantics unchanged; we only add edges
  via the existing write path.
- **No new I25 knob strictly required.** Thresholds (`min_title_len`,
  similarity gate) are tool params with safe defaults. If we want a global
  enable/disable, that's a follow-up — keep car #5 knob-free.
- **Wiki write path** (`WikiStore.add` upsert) — reused, not modified.

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Over-linking (common words/titles) | `min_title_len`, verbatim match, semantic guard (find_similar ≥0.70), `max_links_per_page` cap |
| Mutating hand-curated pages | `dry_run=True` default; `auto-linked` tag; consider a `no-autolink` opt-out tag honored by the scan |
| Re-run duplicates / double-wrap | idempotency check (slug already in `page.links`; skip spans inside `[[...]]`) |
| Cross-directory leakage | `list_wiki_catalog(directory=...)` scopes to caller dir + global |
| Embedding cost on bulk re-`add()` | dry-run computes proposals without writes; apply in a batch; only pages that actually change get re-embedded |

## How this goes wrong like C1/C2

The trap is shipping with the semantic/length guards OFF "to keep it simple" → a
single run wraps every "API"/"memory"/"config" mention across 450 pages, corrupting
the curated corpus in one irreversible batch (writes bump versions). **Guard:**
`dry_run=True` default + the verbatim+length+similarity gates are part of the MVP,
not a follow-up. First real run is dry-run, human-reviewed.

## Related
- `wiki_lint` (broken-ref detection — the downstream check), `wiki_crossref` graph,
  [[yadgar-recall-pipeline-multi-signal]].
