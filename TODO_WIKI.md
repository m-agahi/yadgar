# Wiki Phase 3 — Detailed TODO

## Step 1: Storage Layer (`storage.py`)

### Tables & Indexes
- [ ] Add `wiki_page` to table creation loop in `_init_tables()`
- [ ] Add `wiki_crossref` to table creation loop in `_init_tables()`
- [ ] Add FTS index: `wiki_content_idx` on `wiki_page.content` (BM25, same analyzer as memory)
- [ ] Add MTREE index: `wiki_embedding_idx` on `wiki_page.embedding` (384-dim, COSINE)
- [ ] Add lookup index: `wiki_slug_idx` on `wiki_page.slug`
- [ ] Add index: `wiki_crossref_from_idx` on `wiki_crossref.from_slug`
- [ ] Add index: `wiki_crossref_to_idx` on `wiki_crossref.to_slug`

### CRUD Methods
- [ ] `insert_wiki_page(page: dict) -> int` — insert with `_next_id()`, return ID
- [ ] `update_wiki_page(page_id: int, updates: dict) -> bool` — partial update
- [ ] `get_wiki_page(page_id: int) -> dict | None` — by ID
- [ ] `get_wiki_page_by_slug(slug: str) -> dict | None` — by slug
- [ ] `delete_wiki_page(page_id: int) -> bool` — delete by ID
- [ ] `list_wiki_pages(category: str | None = None) -> list[dict]` — list all, optional filter

### Search Methods
- [ ] `search_wiki_fts(query: str, limit: int = 10) -> list[dict]` — BM25 full-text search
- [ ] `search_wiki_fts_scored(query: str, limit: int = 10) -> list[tuple[int, float]]` — scored FTS
- [ ] `search_wiki_vectors(embedding: bytes, top_k: int = 5) -> list[tuple[int, float]]` — vector KNN

### Cross-Reference Methods
- [ ] `replace_wiki_crossrefs(from_slug: str, to_slugs: list[str]) -> None` — atomic replace
- [ ] `get_wiki_backlinks(slug: str) -> list[str]` — all pages linking TO this slug
- [ ] `get_all_wiki_crossrefs() -> list[dict]` — for graph API

---

## Step 2: WikiStore Class (`yadgar/wiki.py`)

### New File
- [ ] Create `yadgar/wiki.py` with `WikiStore` class
- [ ] `__init__(self, storage, embeddings)` — store refs, validate

### Public API
- [ ] `add(title, content, category, tags, source_memory_ids, confidence)` — create/upsert by slug
- [ ] `read(slug) -> dict | None` — get page by slug
- [ ] `query(query, tags, category, max_results)` — hybrid FTS + vector search
- [ ] `delete(slug) -> bool` — delete by slug
- [ ] `list_pages(category) -> list[dict]` — list all pages
- [ ] `ingest(content, title, tags, source_memory_ids)` — append to existing or create new
- [ ] `lint() -> dict` — health check (orphans, broken refs, stale, low confidence)

### Internal Methods
- [ ] `_slugify(title) -> str` — lowercase, alphanumeric + hyphens, max 64 chars
- [ ] `_extract_wikilinks(content) -> list[str]` — regex `\[\[([^\]]+)\]\]`
- [ ] `_compute_embedding(title, content) -> bytes | None` — semantic anchoring (title prepended)
- [ ] `_sync_crossrefs(slug, links)` — update wiki_crossref table
- [ ] Handle BM25 negative scores with min-max normalization (same pattern as retrieval.py)

### Merge Strategy (ingest)
- [ ] If slug exists: append content with `\n\n---\n\n## Update (ISO-TIMESTAMP)\n\n{content}`
- [ ] Union tags (deduplicated)
- [ ] Union source_memory_ids
- [ ] Keep higher confidence
- [ ] Recompute embedding on merged content
- [ ] Re-extract and sync wikilinks

---

## Step 3: MCP Tools (`server.py`)

### WikiStore Initialization
- [ ] Add `_wiki: WikiStore | None = None` global
- [ ] Initialize in `init_engines()`: `_wiki = WikiStore(_storage, _embeddings)`
- [ ] Cleanup in shutdown

### 7 MCP Tools
- [ ] `wiki_add(title, content, category, tags, source_memory_ids, confidence)` → WikiStore.add()
- [ ] `wiki_query(query, tags, category, max_results)` → WikiStore.query()
- [ ] `wiki_read(slug)` → WikiStore.read()
- [ ] `wiki_delete(slug)` → WikiStore.delete()
- [ ] `wiki_list(category)` → WikiStore.list_pages()
- [ ] `wiki_ingest(content, title, tags, source_memory_ids)` → WikiStore.ingest()
- [ ] `wiki_lint()` → WikiStore.lint()

### SSE Events
- [ ] Push `wiki_added` event on add/ingest (new page)
- [ ] Push `wiki_updated` event on add/ingest (existing page)
- [ ] Push `wiki_deleted` event on delete
- [ ] Strip embeddings from all MCP tool responses

---

## Step 4: Recall Integration (`server.py`)

- [ ] Add wiki blending block AFTER heat boost loop and SR transitions
- [ ] Query wiki with `_wiki.query(query, max_results=3)`
- [ ] Tag results with `_source: "wiki"`
- [ ] Apply curated boost (+0.15 to `_retrieval_score`)
- [ ] Interleave wiki results at positions 1, 3, 5 between memories
- [ ] Trim to `max_results`
- [ ] Guard: skip if `_wiki is None` or `WIKI_RECALL_BLEND_ENABLED` is False
- [ ] Verify: when wiki is empty, recall() is identical to before

---

## Step 5: Graph API (`graph_api.py`)

### Wiki Nodes
- [ ] Query `wiki_page` table in `get_full_graph()`
- [ ] Create nodes with `type: "wiki"`, include slug, title, category, tags
- [ ] Track `wiki_slug_to_id` mapping for edge creation

### Wiki Edges
- [ ] Query `wiki_crossref` table for cross-reference edges (`type: "wiki_crossref"`)
- [ ] Create memory→wiki edges from `source_memory_ids` (`type: "memory_wiki"`)

### Stats
- [ ] Add `wiki_page_count` to `get_graph_stats()`

---

## Step 6: Visualization (`static/index.html`)

### Rendering
- [ ] Add hexagon drawing function for `type: "wiki"` nodes
- [ ] Color by category (architecture=blue, decision=orange, pattern=green, etc.)
- [ ] Fixed size (no heat — wiki pages don't decay)

### Controls
- [ ] Add "Wiki" checkbox to edge types / node types section
- [ ] Add `wiki_crossref` and `memory_wiki` edge colors to EDGE_COLOR
- [ ] Update `applyFilters()` with wiki visibility
- [ ] Update `nodeVisibility` / `linkVisibility` for wiki types

### Detail Panel
- [ ] Show wiki page content, category, tags, cross-references, source memories on click
- [ ] Update `nodeLabel` for wiki nodes

### Stats
- [ ] Add wiki page count to left panel stats

---

## Config (`config.py` + `config_yaml.py`)

- [ ] Add `WIKI_RECALL_BLEND_ENABLED: bool = True`
- [ ] Add `WIKI_RECALL_MAX_RESULTS: int = 3`
- [ ] Add `WIKI_RECALL_CURATED_BOOST: float = 0.15`
- [ ] Add `WIKI_SEMANTIC_ANCHOR: bool = True`
- [ ] Add `WIKI_STALE_DAYS: int = 90`
- [ ] Add FIELD_META entries for all wiki settings in `config_yaml.py`

---

## Tests (`yadgar/tests/test_wiki.py`)

- [ ] Test `_slugify`: normal titles, special chars, CJK, empty, max length
- [ ] Test `_extract_wikilinks`: single, multiple, nested, empty, no links
- [ ] Test `add`: create new page, verify all fields
- [ ] Test `add`: upsert existing page by slug
- [ ] Test `read`: existing slug, nonexistent slug
- [ ] Test `query`: FTS match, semantic match, combined, no results
- [ ] Test `query`: tag filter, category filter
- [ ] Test `delete`: existing, nonexistent
- [ ] Test `list_pages`: all, by category, empty
- [ ] Test `ingest`: new page, append to existing (verify merge strategy)
- [ ] Test `lint`: orphan detection, broken refs, stale pages
- [ ] Test cross-ref sync: add page with links, verify wiki_crossref table
- [ ] Test cross-ref sync: update page links, verify old refs removed
- [ ] Test semantic anchoring: embedding includes title
- [ ] Test recall integration: wiki results blended with memories

---

## Verification Checklist

### MVP (Steps 1-3)
- [ ] `wiki_add` + `wiki_read` round-trip
- [ ] `wiki_add` + `wiki_query` (FTS) returns page
- [ ] `wiki_add` + `wiki_query` (semantic) returns page
- [ ] `wiki_ingest` appends to existing page
- [ ] `wiki_list` shows all pages
- [ ] `wiki_delete` removes page
- [ ] `wiki_lint` detects orphans and broken refs
- [ ] Pages persist across daemon restart
- [ ] All tests pass

### Integration (Steps 4-6)
- [ ] `recall()` returns wiki + memory blend
- [ ] Wiki results have `_source: "wiki"`
- [ ] Empty wiki = recall unchanged
- [ ] Hexagon wiki nodes in force graph
- [ ] Wiki toggle works
- [ ] Cross-ref edges visible
- [ ] Memory→wiki edges visible
- [ ] SSE events fire on wiki operations
