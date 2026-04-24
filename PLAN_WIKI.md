# Yadgar Phase 3: Integrated Wiki / Knowledge Base — Implementation Plan

**Status**: Ready for implementation
**Created**: 2026-04-24
**Depends on**: Phase 1 (complete), Phase 2 (complete), Phase 8 (complete)

---

## 1. Architecture Decisions

### AD-1: Storage — SurrealDB SCHEMALESS (not SCHEMAFULL)

**Decision**: Store wiki pages in SurrealDB as a `wiki_page` table using `SCHEMALESS`, matching all 20 existing tables.

**Rationale**: PLAN.md Phase 3.2 specified `SCHEMAFULL`, but every existing table in `storage.py` uses `SCHEMALESS`. Switching to SCHEMAFULL for one table creates maintenance burden (field changes require migrations) and is inconsistent with the codebase. SCHEMALESS with disciplined insert/update code provides the same guarantees in practice.

**Consequence**: No `DEFINE FIELD` statements needed. Fields enforced at the Python layer.

### AD-2: Search — Hybrid (FTS + Vector), Keyword-First

**Decision**: Wiki pages get both a BM25 full-text index and an MTREE vector index on embeddings, matching the memory table pattern.

**Rationale**: Yadgar already has embedding infrastructure (sentence-transformers, 384-dim). Wiki pages are curated knowledge — semantic search adds real value for queries like "how does the memory system work" finding a wiki page titled "Architecture Overview". FTS catches exact terms; vector catches semantic intent.

**Implementation**: Same index patterns as `memory_content_idx` and `memory_embedding_idx`.

### AD-3: Recall Integration — Parallel Query, Not WRRF Injection

**Decision**: Wiki search runs as a **parallel query** alongside the existing `HippoRetriever.recall()`. Results are merged at the `server.py` `recall()` wrapper level (lines 872-967), not inside the 600-line WRRF pipeline.

**Rationale**: The WRRF pipeline has 8 signals, confidence gating, multiple reranking stages, NLI entailment, and cross-encoder passes. Injecting wiki results into that pipeline is high-risk and high-complexity. Running wiki search in parallel and merging at the wrapper level is:
- Reversible (easy to disable)
- Independently testable
- Zero risk to existing recall quality

**Merge strategy**: Wiki results tagged with `_source: "wiki"` and a slight score boost (curated knowledge > raw memories). Interleaved into the final result list respecting `max_results`.

### AD-4: Cross-References — Flat Array + Derived Table

**Decision**: Store `[[wikilink]]` references as a flat `links: array` field on the wiki_page record (source of truth), AND maintain a derived `wiki_crossref` table for graph traversal queries.

**Rationale**: Flat array enables fast single-record reads. Derived table enables graph API to query "all pages linking to X" and power visualization edges. Both are kept in sync on every save — no manual `wiki_link` tool needed.

### AD-5: Wikilink Extraction — Automatic on Save

**Decision**: Drop the `wiki_link(from_slug, to_slug)` manual tool from PLAN.md. Instead, auto-extract `[[slug]]` patterns from content on every `wiki_add` / `wiki_ingest` call.

**Rationale**: Both OMC and Obsidian research agree — manual link management falls out of sync. Auto-extraction keeps links and content aligned. The LLM (caller) embeds `[[wikilinks]]` in the content it writes; Yadgar extracts them.

### AD-6: Semantic Anchoring — Title Prepended to Embedding

**Decision**: Before computing the embedding for a wiki page, prepend `title + "\n"` to the content. This "semantic anchor" improves retrieval for title-related queries.

**Rationale**: Obsidian research demonstrates this yields better retrieval with zero cost. A query "architecture overview" will have higher cosine similarity to a page titled "Architecture Overview" even if the body uses different phrasing.

### AD-7: Deferred to v2

The following features are explicitly deferred:
1. **Graph-boosted retrieval** (1.2x wikilink edge score boost) — requires modifying the WRRF pipeline
2. **Auto-generation of wiki pages from consolidation** — risk of noise; needs user opt-in design
3. **Tags as graph hyperedges** — premature optimization at current scale
4. **MOC (Maps of Content) hub nodes** — useful at 100+ pages, not at 10-30

---

## 2. SurrealDB Schema

### 2.1 Table: `wiki_page`

```
DEFINE TABLE IF NOT EXISTS wiki_page SCHEMALESS;
```

Fields (enforced in Python, not DB):

| Field | Type | Description |
|---|---|---|
| `title` | `string` | Human-readable title |
| `slug` | `string` | URL-safe unique identifier (auto-generated from title) |
| `content` | `string` | Markdown body (may contain `[[slug]]` wikilinks) |
| `category` | `string` | One of: `architecture`, `decision`, `pattern`, `debugging`, `reference`, `convention`, `fact`, `analysis` |
| `tags` | `array<string>` | Freeform tags |
| `links` | `array<string>` | Extracted `[[slug]]` cross-references (auto-populated) |
| `confidence` | `string` | `high`, `medium`, `low` (default: `medium`) |
| `embedding` | `array<float>` | 384-dim sentence-transformer embedding |
| `source_memory_ids` | `array<int>` | Memory IDs this page was derived from (plain ints, matching existing convention) |
| `created_at` | `datetime` | Creation timestamp |
| `updated_at` | `datetime` | Last modification timestamp |

### 2.2 Indexes

```sql
-- FTS on wiki page content (BM25 keyword search)
DEFINE INDEX IF NOT EXISTS wiki_content_idx
    ON wiki_page FIELDS content
    SEARCH ANALYZER mem_analyzer BM25;

-- MTREE vector index on embedding (semantic search)
DEFINE INDEX IF NOT EXISTS wiki_embedding_idx
    ON wiki_page FIELDS embedding
    MTREE DIMENSION 384 DIST COSINE;

-- Slug lookup (fast reads by slug)
DEFINE INDEX IF NOT EXISTS wiki_slug_idx
    ON wiki_page FIELDS slug;
```

### 2.3 Table: `wiki_crossref` (derived, for graph traversal)

```
DEFINE TABLE IF NOT EXISTS wiki_crossref SCHEMALESS;
```

Fields:

| Field | Type | Description |
|---|---|---|
| `from_slug` | `string` | Source page slug |
| `to_slug` | `string` | Target page slug |
| `created_at` | `datetime` | When the link was detected |

```sql
DEFINE INDEX IF NOT EXISTS wiki_crossref_from_idx
    ON wiki_crossref FIELDS from_slug;
DEFINE INDEX IF NOT EXISTS wiki_crossref_to_idx
    ON wiki_crossref FIELDS to_slug;
```

---

## 3. New Files

| File | Purpose |
|---|---|
| `yadgar/wiki.py` | `WikiStore` class — CRUD, search, link extraction, embedding |
| `yadgar/tests/test_wiki.py` | Unit tests for WikiStore |

No other new files. Integration code goes into existing files (`storage.py`, `server.py`, `graph_api.py`).

---

## 4. Implementation Steps

### Step 1: Storage Layer (`storage.py`)

Add table and index definitions to `StorageEngine._init_tables()`:

```python
# In the table loop (line 392-413), add:
"wiki_page",
"wiki_crossref",

# After existing index definitions (line ~477), add:
# wiki_page: FTS on content
db.query("""
    DEFINE INDEX IF NOT EXISTS wiki_content_idx
        ON wiki_page FIELDS content
        SEARCH ANALYZER mem_analyzer BM25;
""")
# wiki_page: MTREE vector index on embedding
db.query(f"""
    DEFINE INDEX IF NOT EXISTS wiki_embedding_idx
        ON wiki_page FIELDS embedding
        MTREE DIMENSION {self._embedding_dim} DIST COSINE;
""")
# wiki_page: slug lookup
db.query("""
    DEFINE INDEX IF NOT EXISTS wiki_slug_idx
        ON wiki_page FIELDS slug;
""")
# wiki_crossref: from/to indexes
db.query("""
    DEFINE INDEX IF NOT EXISTS wiki_crossref_from_idx
        ON wiki_crossref FIELDS from_slug;
""")
db.query("""
    DEFINE INDEX IF NOT EXISTS wiki_crossref_to_idx
        ON wiki_crossref FIELDS to_slug;
""")
```

Add StorageEngine methods:

```python
# CRUD
insert_wiki_page(page: dict) -> int
update_wiki_page(page_id: int, updates: dict) -> bool
get_wiki_page(page_id: int) -> dict | None
get_wiki_page_by_slug(slug: str) -> dict | None
delete_wiki_page(page_id: int) -> bool
list_wiki_pages(category: str | None = None) -> list[dict]

# Search
search_wiki_fts(query: str, limit: int = 10) -> list[dict]
search_wiki_fts_scored(query: str, limit: int = 10) -> list[tuple[int, float]]
search_wiki_vectors(embedding: bytes, top_k: int = 5) -> list[tuple[int, float]]

# Cross-references
replace_wiki_crossrefs(from_slug: str, to_slugs: list[str]) -> None
get_wiki_backlinks(slug: str) -> list[str]
get_all_wiki_crossrefs() -> list[dict]
```

**Acceptance criteria**:
- Tables and indexes created on `StorageEngine.__init__`
- All CRUD methods work: insert returns int ID, get returns dict, update/delete return bool
- FTS search returns scored results
- Vector search returns (id, distance) tuples
- Cross-ref replacement is atomic (delete old + insert new)

### Step 2: WikiStore Class (`yadgar/wiki.py`)

Core class that encapsulates all wiki logic:

```python
class WikiStore:
    def __init__(self, storage: StorageEngine, embeddings: EmbeddingEngine):
        ...

    # -- Public API --
    def add(self, title: str, content: str, category: str, tags: list[str],
            source_memory_ids: list[int] | None = None,
            confidence: str = "medium") -> dict:
        """Create or update a wiki page. If slug exists, merge content."""

    def read(self, slug: str) -> dict | None:
        """Read a page by slug."""

    def query(self, query: str, tags: list[str] | None = None,
              category: str | None = None, max_results: int = 5) -> list[dict]:
        """Hybrid search: FTS + vector, filtered by tags/category."""

    def delete(self, slug: str) -> bool:
        """Delete a page by slug."""

    def list_pages(self, category: str | None = None) -> list[dict]:
        """List all pages, optionally filtered by category."""

    def ingest(self, content: str, title: str | None = None,
               tags: list[str] | None = None,
               source_memory_ids: list[int] | None = None) -> dict:
        """Ingest pre-processed content. If title matches existing page, append."""

    def lint(self) -> dict:
        """Check for: orphan pages (no inbound links), broken refs,
        stale pages (not updated in 90+ days), low confidence."""

    # -- Internal --
    def _slugify(self, title: str) -> str:
        """Convert title to URL-safe slug."""

    def _extract_wikilinks(self, content: str) -> list[str]:
        """Extract [[slug]] references from markdown content."""

    def _compute_embedding(self, title: str, content: str) -> bytes | None:
        """Semantic anchoring: embed title + content."""

    def _sync_crossrefs(self, slug: str, links: list[str]) -> None:
        """Update wiki_crossref table from extracted links."""
```

Key behaviors:
- **Slug generation**: `re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')`
- **Upsert on slug**: `add()` checks if slug exists. If yes, updates content/tags/category. If no, inserts.
- **Wikilink extraction**: `re.findall(r'\[\[([^\]]+)\]\]', content)` — extracted slugs stored in `links` field
- **Semantic anchoring**: Embedding computed on `f"{title}\n{content[:2000]}"` (title prepended)
- **Ingest vs Add**: `ingest()` is for appending to existing pages (merge strategy: append with timestamp separator). `add()` is for creating/replacing.
- **BM25 negative scores**: SurrealDB returns negative BM25 scores (known issue from Phase 1, fixed in 3 places in `retrieval.py`). The `query()` method MUST apply min-max normalization when combining FTS scores with vector similarity scores. See `HippoRetriever.recall()` signal 1 for the exact pattern.

**Acceptance criteria**:
- `add()` creates new page, returns dict with all fields populated
- `add()` with existing slug updates the page
- `query()` returns results from both FTS and vector search, deduplicated
- `_extract_wikilinks()` correctly parses `[[slug-name]]` patterns
- Cross-references auto-sync on every add/ingest
- `lint()` detects orphans, broken refs, stale pages
- Embedding is computed with title prepended (semantic anchoring)

### Step 3: MCP Tools (`server.py`)

Register 7 MCP tools (drop `wiki_link`, add `wiki_lint`):

```python
@mcp_server.tool()
def wiki_add(title: str, content: str, category: str = "reference",
             tags: list[str] | None = None,
             source_memory_ids: list[int] | None = None,
             confidence: str = "medium") -> dict:
    """Create or update a wiki page. Content can include [[slug]] cross-references."""

@mcp_server.tool()
def wiki_query(query: str, tags: list[str] | None = None,
               category: str | None = None, max_results: int = 5) -> list[dict]:
    """Search wiki pages by keyword + semantic similarity."""

@mcp_server.tool()
def wiki_read(slug: str) -> dict:
    """Read a specific wiki page by slug."""

@mcp_server.tool()
def wiki_delete(slug: str) -> dict:
    """Delete a wiki page by slug."""

@mcp_server.tool()
def wiki_list(category: str | None = None) -> list[dict]:
    """List all wiki pages, optionally filtered by category."""

@mcp_server.tool()
def wiki_ingest(content: str, title: str | None = None,
                tags: list[str] | None = None,
                source_memory_ids: list[int] | None = None) -> dict:
    """Ingest content into the wiki. Merges into existing page if title matches."""

@mcp_server.tool()
def wiki_lint() -> dict:
    """Check wiki health: orphan pages, broken cross-refs, stale pages, low confidence."""
```

Each tool:
- Calls the corresponding `WikiStore` method
- Pushes SSE events via `_push_event()` for `wiki_add`, `wiki_ingest`, `wiki_delete`
- Strips embeddings from response dicts

SSE events:
```json
{"event": "wiki_added",   "node": {"id": "wiki:{id}", "slug": "...", "title": "..."}}
{"event": "wiki_updated", "node": {"id": "wiki:{id}", "slug": "...", "title": "..."}}
{"event": "wiki_deleted", "id": "wiki:{id}"}
```

**Acceptance criteria**:
- All 7 MCP tools registered and callable
- `wiki_add` → `wiki_query` round-trip returns the created page
- SSE events fire on add/ingest/delete
- Error cases return structured error dicts (not exceptions)

### Step 4: Recall Integration (`server.py`)

Modify the `recall()` MCP tool (lines 872-967) to blend wiki results.

**IMPORTANT**: Wiki blending must happen AFTER the heat boost loop (lines 923-929), SR transition recording, and action stream logging. Wiki pages have no `heat` field — placing them in `merged` before the heat boost would cause a KeyError. Insert the blending block after line ~961 (after `_replay.record_tool_call()`) and before the final embedding strip loop.

```python
@mcp_server.tool()
def recall(query: str, max_results: int = 5, min_heat: float = 0.0) -> list[dict]:
    # ... existing retriever call (unchanged) ...
    # ... existing heat boost, SR transitions, action stream (unchanged) ...
    # ... existing _replay.record_tool_call() (unchanged) ...

    # Wiki blending (AFTER heat boost — wiki pages have no heat field)
    if _wiki is not None:
        wiki_results = _wiki.query(query, max_results=3)
        for wr in wiki_results:
            wr["_source"] = "wiki"
            wr["_retrieval_score"] = wr.get("_retrieval_score", 0.5) + 0.15  # curated boost
            wr.pop("embedding", None)
        # Interleave: insert wiki results at positions 1, 3, 5 (between memories)
        blended = []
        mem_iter = iter(merged)
        wiki_iter = iter(wiki_results)
        for i in range(max_results + len(wiki_results)):
            if i % 2 == 1:
                w = next(wiki_iter, None)
                if w:
                    blended.append(w)
                    continue
            m = next(mem_iter, None)
            if m:
                blended.append(m)
        merged = blended[:max_results]

    # Strip binary fields from response (not JSON-serializable)
    for m in merged:
        m.pop("embedding", None)
        m.pop("hdc_vector", None)

    return merged
```

**Acceptance criteria**:
- `recall()` returns a mix of memories and wiki pages when wiki has relevant content
- Wiki results have `_source: "wiki"` field
- When wiki is empty/uninitialized, recall behaves exactly as before
- Wiki results do not go through heat boost (they have no heat — blending is after the heat boost loop)

### Step 5: Graph API Integration (`graph_api.py`)

Add wiki nodes and edges to `get_full_graph()`:

```python
def get_full_graph(self, max_memories=500, top_k=100):
    # ... existing memory nodes and edges ...

    # ── Wiki nodes ──
    try:
        wiki_pages = self._s._q(
            "SELECT id, title, slug, category, tags, links, updated_at "
            "FROM wiki_page ORDER BY updated_at DESC LIMIT 200"
        )
    except Exception:
        wiki_pages = []

    wiki_slug_to_id = {}
    for wp in wiki_pages:
        raw_id = self._extract_id(wp.get("id"))
        if raw_id is None:
            continue
        node_id = f"wiki:{raw_id}"
        slug = wp.get("slug") or ""
        wiki_slug_to_id[slug] = node_id
        nodes.append({
            "id": node_id,
            "type": "wiki",
            "label": wp.get("title") or slug,
            "slug": slug,
            "category": wp.get("category") or "",
            "tags": wp.get("tags") or [],
            "updated_at": str(wp.get("updated_at") or ""),
        })

    # ── Wiki cross-reference edges ──
    try:
        crossrefs = self._s._q("SELECT from_slug, to_slug FROM wiki_crossref")
    except Exception:
        crossrefs = []

    for cr in crossrefs:
        src = wiki_slug_to_id.get(cr.get("from_slug"))
        tgt = wiki_slug_to_id.get(cr.get("to_slug"))
        if src and tgt:
            edges.append({
                "source": src,
                "target": tgt,
                "type": "wiki_crossref",
            })

    # ── Memory→Wiki edges (via source_memory_ids) ──
    for wp in wiki_pages:
        raw_id = self._extract_id(wp.get("id"))
        source_ids = wp.get("source_memory_ids") or []
        wiki_node_id = f"wiki:{raw_id}"
        for mid in source_ids:
            if isinstance(mid, int) and mid in mem_ids:
                edges.append({
                    "source": f"mem:{mid}",
                    "target": wiki_node_id,
                    "type": "memory_wiki",
                })

    return {"nodes": nodes, "edges": edges}
```

**Acceptance criteria**:
- Wiki pages appear as nodes with `type: "wiki"` in the graph
- Cross-reference edges appear between wiki nodes
- Memory-to-wiki edges appear when `source_memory_ids` is populated
- Graph stats include wiki page count

### Step 6: Visualization (`static/index.html`)

Add wiki node rendering:
- **Shape**: Hexagon (as specified in PLAN.md Phase 2.1)
- **Color**: By category (architecture=blue, decision=orange, pattern=green, etc.)
- **Size**: Fixed (wiki pages don't have heat)
- **Side panel**: Shows full markdown content, category, tags, cross-references, source memories
- **Toggle**: "Wiki pages" checkbox in the controls panel
- **Wiki cross-ref edges**: Dashed lines between hexagons
- **Memory→Wiki edges**: Dotted lines from circles to hexagons

**Acceptance criteria**:
- Hexagonal wiki nodes render in the force graph
- Clicking a wiki node shows its content in the side panel
- Wiki nodes can be toggled on/off
- Cross-reference edges and memory→wiki edges visible

---

## 5. Integration Points (Existing Code Changes)

| File | Change | Risk |
|---|---|---|
| `storage.py` `_init_tables()` | Add `wiki_page`, `wiki_crossref` tables + 5 indexes | Low — additive |
| `storage.py` | Add ~12 new methods for wiki CRUD/search | Low — new code |
| `server.py` `main()` / `init_engines()` | Initialize `WikiStore` global | Low — follows existing pattern |
| `server.py` `recall()` | Add wiki blending block after retriever returns | Medium — touches hot path, but isolated |
| `server.py` | Register 7 new MCP tools | Low — additive |
| `graph_api.py` `get_full_graph()` | Add wiki nodes + edges | Low — additive |
| `graph_api.py` `get_graph_stats()` | Add wiki_page_count | Low — additive |
| `static/index.html` | Hexagon rendering, wiki toggle, side panel | Medium — UI changes |

---

## 6. Implementation Order

```
Step 1: storage.py — tables, indexes, methods
    ↓
Step 2: wiki.py — WikiStore class
    ↓
Step 3: server.py — MCP tools + WikiStore init
    ↓
Step 4: server.py — recall() blending
    ↓
Step 5: graph_api.py — wiki nodes/edges
    ↓
Step 6: static/index.html — hexagon rendering
```

Each step is independently testable. Steps 1-3 form the MVP (wiki is usable via MCP tools). Steps 4-6 are integration (wiki compounds with existing systems).

---

## 7. Verification Criteria

### MVP (Steps 1-3)
- [ ] `wiki_add("Architecture Overview", "Yadgar is...", "architecture", ["core"])` creates a page
- [ ] `wiki_read("architecture-overview")` returns the page with all fields
- [ ] `wiki_query("how does yadgar work")` returns the page via semantic search
- [ ] `wiki_query("architecture")` returns the page via FTS
- [ ] `wiki_add` with same title updates existing page (upsert)
- [ ] `wiki_ingest` appends to existing page with timestamp
- [ ] `wiki_list()` shows all pages; `wiki_list("architecture")` filters
- [ ] `wiki_delete("architecture-overview")` removes the page
- [ ] Cross-references auto-extracted: `[[some-page]]` in content populates `links` field
- [ ] `wiki_lint()` detects orphan pages and broken `[[refs]]`
- [ ] Pages persist across server restarts
- [ ] All new StorageEngine methods have unit tests

### Integration (Steps 4-6)
- [ ] `recall("yadgar architecture")` returns wiki page alongside memories
- [ ] Wiki results in recall have `_source: "wiki"` marker
- [ ] When wiki is empty, `recall()` behaves identically to before
- [ ] Wiki hexagon nodes appear in the force graph
- [ ] Clicking wiki node shows markdown content in side panel
- [ ] Cross-reference edges visible between wiki nodes
- [ ] Memory→Wiki edges visible when source_memory_ids populated
- [ ] SSE events fire: `wiki_added` appears in event stream on `wiki_add()`
- [ ] Wiki toggle in controls hides/shows hexagon nodes
- [ ] `get_graph_stats()` includes wiki_page_count

---

## 8. v2 — Advanced Wiki Features

**Prerequisite**: v1 (Steps 1-6) complete and wiki has 20+ pages in active use.

### v2.1: Graph-Boosted Retrieval
Modify the WRRF pipeline to boost wiki results connected to query-mentioned entities via wikilink edges. When a recall query matches entities that appear in wiki cross-references, apply a 1.2x score multiplier to those wiki pages. This requires adding a new signal to `retrieval.py`'s fusion pipeline.

**Files**: `retrieval.py` (new WRRF signal), `wiki.py` (expose crossref graph)

### v2.2: Auto-Generation from Consolidation
During daily consolidation, cluster related memories by entity overlap and tag similarity. When a cluster of 5+ memories shares a theme not yet covered by a wiki page, propose a draft wiki page via a new `wiki_draft` table. Drafts require user approval before becoming real pages (no auto-publish).

**Files**: `consolidation.py` (new clustering pass), `wiki.py` (draft management), `server.py` (approve/reject tools)

### v2.3: Tags as Graph Hyperedges
Model tags as first-class graph nodes (`tag:{name}`) with RELATE edges from every wiki page carrying that tag. Enables graph queries like "find all pages sharing any two tags" and powers tag-based clustering in the visualization.

**Files**: `storage.py` (new `wiki_tag` table + RELATE edges), `graph_api.py` (tag nodes), `index.html` (tag node rendering)

### v2.4: MOC (Maps of Content) Hub Nodes
Special wiki pages that serve as curated index pages — contain only `[[links]]` to related pages. Modeled as hub nodes with typed `curates` edges. Provides human-navigable topic entry points and enables scoped search (restrict query to a MOC's subgraph).

**Files**: `wiki.py` (MOC detection from content analysis), `graph_api.py` (hub node styling)

### v2.5: Markdown Export/Import
CLI commands for bulk operations:
- `yadgar wiki export [--dir ./wiki-export/]` — dump all pages to markdown files with YAML frontmatter
- `yadgar wiki import [--dir ./wiki-import/]` — bulk ingest markdown files
- Enables migration, backup, and interop with Obsidian vaults.

**Files**: `__main__.py` (CLI commands), `wiki.py` (export/import methods)

### v2.6: Confidence & Fact Decay
Each wiki page tracks per-section confidence metadata. Sections sourced from multiple memories or reinforced by recent recall get higher confidence. Sections not reinforced in 90+ days get flagged in lint as potentially stale. Based on Karpathy v2 and Ebbinghaus forgetting curve.

**Files**: `wiki.py` (section-level confidence tracking), `consolidation.py` (confidence reinforcement during cycles)

### v2.7: Daily Notes (Temporal Anchors)
Auto-generated date-stamped wiki pages that link to memories and wiki pages touched during that day. Provides temporal navigation ("what did I work on last Tuesday?") and session clustering.

**Files**: `wiki.py` (daily note generation), `consolidation.py` (end-of-day trigger)

---

## 9. Config Settings

Add to `yadgar/config.py` Settings class:

```python
# Wiki
WIKI_RECALL_BLEND_ENABLED: bool = True
WIKI_RECALL_MAX_RESULTS: int = 3
WIKI_RECALL_CURATED_BOOST: float = 0.15
WIKI_SEMANTIC_ANCHOR: bool = True
WIKI_STALE_DAYS: int = 90
```

Add to `config_yaml.py` FIELD_META for `yadgar config` CLI support.

---

## 10. No New Dependencies

All required infrastructure already exists:
- `sentence-transformers` — embeddings (already in use)
- `re` — wikilink extraction (stdlib)
- SurrealDB BM25 + MTREE indexes — same patterns as memory table
- `_push_event()` — SSE events (already in use)
- `GraphAPI` — visualization data assembly (already in use)
