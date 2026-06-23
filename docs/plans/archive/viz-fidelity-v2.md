# viz-fidelity-v2 — the visualization shows reality

**Status:** PLANNED (2026-06-22). Task #80. Follow-on to #33 (`viz-data-fidelity.md`).
**Position:** next v5.x train. Independent of the recall/measurement work; can ship standalone.
**Principle:** every element the viz draws must map to a **stored fact** the memory system reads or writes, and must carry a **role label** so the viewer knows whether it drives recall, is informational, or is a derived overlay. **No data invented at render time may be presented as a graph edge or as a cluster.**

---

## 1. Why (audit findings, 2026-06-22)

#33 delivered real nodes (live heat, typed ids `mem:`/`entity:`/`wiki:`, single-source-of-truth `/api/graph`, BC-VZ1 ✅) and real **retrieval** edges. But three classes of fiction survived:

### Taxonomy of current viz elements

| Element | Source | Written by | Read by retrieval/consolidation | Truth status |
|---|---|---|---|---|
| node: memory | `memory` | write path | YES (primary unit) | REAL |
| node: entity | `entity` | KG/consolidation | YES (PPR/spreading seed) | REAL |
| node: wiki | `wiki_page` | wiki path | wiki recall (separate path) | REAL |
| edge: **transition** | `memory_transition` | `cognitive_map.record_transition` | **YES** — `cofire_prior` → WRRF (`fusion.py:234`) | REAL · retrieval-signal |
| edge: **co_occurrence/imports/calls/resolved_by/caused_by** | `entity_relationship` | `knowledge_graph.add_relationship` | **YES** — PPR + spreading (`retrieval/core.py:100-233`) | REAL · retrieval-signal |
| edge: temporal | `memory.slot_index` co-membership | write path | no | REAL · informational |
| edge: causal | `causal_edge` (PC-algorithm) | causal discovery | no | REAL · informational |
| edge: wiki_crossref | `wiki_crossref` | `[[link]]` parse | no | REAL · informational |
| edge: memory_wiki | `wiki_page.source_memory_ids` | wiki create/edit | no | REAL · informational |
| edge: **semantic** | **cosine KNN computed at render** | **nobody** | no | **DECORATION** |
| cluster: **"disconnected components" sidebar** | **client-side BFS over force-layout** | **nobody** | no | **DECORATION** |
| cluster: `memory_cluster` (hierarchical, consolidation) | `memory_cluster` | consolidation | **not rendered, not read by retrieval** | **DORMANT** |
| cluster: `memory_similarity_link` (near-dup) | `memory_similarity_link` | consolidation | **not rendered, not read** | **DORMANT** |
| astrocyte-pool domains | astrocyte pool | consolidation | not rendered | **DORMANT (in viz)** |

References: `yadgar/graph_api.py` (JSON build), `yadgar/viz_meta.py` (EDGE_TYPES registry), `yadgar/static/index.html:1906-2012` (BFS cluster panel), `yadgar/storage/cluster.py` (memory_cluster, similarity_link), `yadgar/server/http.py:1388` (`/api/graph`), `yadgar/server/_phase_post_write.py:202` (SSE `memory_added`).

### The three problems
1. **Decoration-as-data:** `semantic` edges are invented at render time (cosine ≥0.75 over embeddings). They look identical to stored edges but nothing in the memory system stores or reads them — they duplicate the vector-search signal as fake graph structure.
2. **Fake clusters / invisible real clusters:** the only "clusters" shown are a BFS over the *visual layout* (zero DB backing). The memory system's actual groupings — `memory_cluster`, `memory_similarity_link`, astrocyte domains — are never drawn.
3. **No role distinction:** retrieval-driving edges (transition, entity-relations) are drawn the same as informational-only edges (temporal, causal, provenance), so the viewer can't tell what actually affects recall.

---

## 2. Design — the fidelity contract

**Every edge in `/api/graph` carries a `role`:**
- `retrieval` — this edge's table is read by the recall pipeline and changes ranking (transition, entity-relationship types).
- `informational` — backed by a real table, but retrieval does not consume it (temporal, causal, wiki_crossref, memory_wiki).
- `derived` — computed at request time, not stored (only if explicitly kept; see §3).

**Every cluster comes from a real table** with a `source` field (`memory_cluster` | `similarity_link` | `astrocyte_domain`). The render-time BFS grouping, if kept at all, is labeled "layout grouping — not a memory cluster" and visually separated from real clusters.

The frontend legend reflects role: retrieval edges prominent/solid, informational edges muted/dashed, derived (if any) clearly marked "computed, not stored."

---

## 3. Per-element decisions

| Element | Decision | Rationale |
|---|---|---|
| **semantic edges** | **REMOVE** from the graph edge set. (Optionally re-add later as an explicit, default-off "similarity overlay" layer clearly labeled "computed, not stored" — NOT a graph edge.) | Redundant with the vector retrieval signal; invented at render; currently indistinguishable from real edges = the core fidelity lie. Remove first; an honest overlay can be a separate follow-up. |
| **BFS "disconnected components" panel** | **REPLACE** with real clusters (see below). If a connectivity view is still wanted, relabel explicitly "layout grouping (not memory clusters)". | It is the thing the user *thinks* shows memory structure but doesn't. |
| **`memory_cluster`** | **SURFACE** in `/api/graph` as `clusters[]` with `source:"memory_cluster"`; render as node grouping/hull + sidebar list. | This IS the memory system's real hierarchical clustering; making it visible is the point of "show reality". Flips it DORMANT→LIVE (consumed by viz). |
| **`memory_similarity_link`** | **SURFACE** as an edge type `role:"informational"` (near-duplicate links) OR as cluster membership. Decide during build (one or the other, not both). | Real consolidation output, currently invisible. |
| **astrocyte-pool domains** | **SURFACE** as cluster `source:"astrocyte_domain"` IF the pool actually assigns domains in prod; else omit + note. Verify via `astrocyte` tables before wiring (don't draw an empty feature). | Avoid drawing a dormant-empty feature; confirm it has data first. |
| **transition + entity-relation edges** | KEEP, stamp `role:"retrieval"`, visually prominent. | The honest core. |
| **temporal / causal / wiki_crossref / memory_wiki** | KEEP, stamp `role:"informational"`, visually muted. | True data, just not ranking signals — keep but mark. |
| **SSE heat staleness (F2 residual)** | Add a `heat_updated` SSE event (or periodic re-sync) so heat on connected clients isn't stale until reload. | Closes the last #33 F2 gap; "live heat" should stay live. |

---

## 4. Implementation

### Backend (`graph_api.py`, `viz_meta.py`, `storage/cluster.py`)
1. **Drop semantic-edge computation** (`graph_api.py:369-445, 559-690`) from the default payload. Remove its EDGE_TYPES entry or move behind an explicit `?overlay=similarity` param (default off) that returns it under a separate `overlays` key, never `edges`.
2. **Stamp `role`** on every emitted edge (extend `viz_meta.py` EDGE_TYPES with a `role` field; `graph_api.py` copies it into each edge dict).
3. **Add `clusters[]`** to the payload: query `memory_cluster` (+ membership) and astrocyte domains via new storage helpers (`get_memory_clusters()`, `get_cluster_members()` — `storage/cluster.py` already has the tables, add read methods if missing). Each cluster: `{id, source, label, member_node_ids[], level?}`.
4. **`memory_similarity_link`** → new edge type (role informational) or cluster membership; add storage read method.
5. Keep the single-source-of-truth contract: clusters/edges all derived live from DB per request (no caching).

### Frontend (`static/index.html`, `graph-detail.js`, viz JS)
1. **Remove the BFS cluster detector** (`index.html:1906-2012`) or relabel it "layout grouping (not memory clusters)" in a clearly separate panel.
2. **Render real `clusters[]`** — convex hulls / color-grouping by `source`, sidebar list grouped by `source` (memory_cluster / astrocyte_domain).
3. **Legend by edge `role`** — retrieval (solid, prominent), informational (dashed, muted), derived-overlay (only if the optional similarity overlay is toggled on; labeled "computed, not stored").
4. **SSE**: handle `heat_updated` → update node heat without full reload.

### SSE (`server/_phase_post_write.py` + consolidation)
- Emit `heat_updated` events on heat changes (write-time boost + nightly decay batch). Frontend patches node heat.

---

## 5. Contract + registry

**BEHAVIOR_CONTRACT.md — new/updated BC-VZ rows (each ✅ via live e2e):**
- BC-VZ-R1: every `/api/graph` edge carries a valid `role` ∈ {retrieval, informational, derived}; transition + entity-relationship edges are `retrieval`.
- BC-VZ-R2: `/api/graph` emits no edge whose data is computed at render time (semantic edge absent from `edges`; only under explicit overlay param if kept).
- BC-VZ-R3: `clusters[]` reflects `memory_cluster` rows (seed N clusters → all appear with correct membership); the BFS layout grouping is NOT presented as a memory cluster.
- BC-VZ-R4 (if astrocyte domains wired): domains in payload match the astrocyte pool's assignments.
- BC-VZ-F2: a heat change emits an SSE `heat_updated`; client heat matches DB without reload (may stay ⏳ if SSE e2e infeasible — be honest).

**CAPABILITY_REGISTRY.md (I32):**
- Flip `memory_cluster` viz consumption DORMANT→LIVE (now read by graph_api); update CAP-VIZ-* entry refs.
- Note semantic-edge removal (or overlay relocation) in the viz capability entry.
- Any new storage read methods are not new surfaces (not Settings/tool/migration/BC) but update the viz CAP entry's refs.

---

## 6. Steps (test-first, phased)

0. **Verify real data exists** before wiring: query prod (read-only) — does `memory_cluster` have rows? does the astrocyte pool assign domains? If a structure is empty in prod, don't render it (note it instead). Avoids drawing dormant-empty features.
1. **e2e first (red):** `tests/e2e/test_viz_fidelity_v2_e2e.py` — seed memories + a `memory_cluster` + entity relations + a transition; call `GraphAPI.get_full_graph()`; assert (a) every edge has a valid `role`, (b) no `semantic` edge in `edges`, (c) `clusters[]` contains the seeded memory_cluster with correct members. Red against current code.
2. **Backend:** role stamping + clusters[] + drop/relocate semantic + storage read methods. Green the e2e.
3. **Frontend:** real clusters render, role legend, remove/relabel BFS panel, `heat_updated` handler. (Frontend not e2e-gated the same way; manual smoke + the API contract test is the gate.)
4. **SSE heat_updated** + client patch.
5. Contract rows ✅ + registry I32 + version bump + CHANGELOG.
6. `make e2e` green; viz-tests CI job green; ruff/I13/I32/contract lints green.

---

## 7. Scope / risk

- **Backend** (graph_api + storage reads + role stamping): moderate, well-bounded.
- **Frontend** (cluster render + legend + remove BFS): moderate; the convex-hull/grouping render is the largest piece.
- **Risk:** drawing a real-but-empty structure (mitigated by Step 0 verify). Removing semantic edges may surprise anyone who liked them — mitigated by the optional labeled overlay path.
- **No retrieval-path changes** — this is a read/visualization train; it does not touch recall ranking. Safe to run alongside measurement work.

Ships as the next available v5.x (assigned at integration). One PR.
