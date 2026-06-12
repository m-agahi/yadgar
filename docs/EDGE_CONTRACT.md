# Edge Contract (v5.54.0 — Phase 1)

Authoritative role declaration for every graph edge/relation in yadgar. Enforces invariant **I29** (leverage-completeness): every stored/computed edge has a named consumer or is dropped, and the viz must reflect what drives behavior. Source investigation: edge-mechanics audit 2026-06-12 (file:line cited). Governs the rest of `PLAN_V5_54`.

## Roles
- **retrieval** — feeds `recall()` ranking (PPR / spreading / a precomputed prior). Must reach the everyday/`fast` path (via precompute, 5.54.1).
- **display** — viz only. Legitimate (structure/teaching) but must be visually distinguished as NON-load-bearing so the viz doesn't misrepresent capability.
- **drop** — no consumer after the keepers are wired; stop computing/storing (5.54.4).

## Contract table

| Edge / relation | Producer (where created) | Stored? | Consumer TODAY | TARGET role | Action |
|---|---|---|---|---|---|
| **entity typed-relations** (`co_occurrence`, `imports`, `calls`, `resolved_by`, `caused_by`) | consolidation (`cls.py`), deferred | yes (entity graph) | `recall` PPR (w=0.5) + spreading (w=0.3) — **`balanced`/`full` only**, OFF in `fast` | **retrieval** | **5.54.1**: precompute per-memory prior in consolidation → `fast`/everyday recall adds O(1). The big "make it everyday" win. **5.54.3**: render in viz (currently INVISIBLE except causal). |
| **transition** (co-recall) | `recall.py:record_transition` on each recall | yes (`memory_transition` table) | **viz only** — recall never reads it back | **retrieval** | **5.54.2**: precompute "frequently co-recalled with" boost. Dead high-value edge. |
| **wiki_crossref** (`[[slug]]`) | `wiki_add` `_extract_wikilinks` → `wiki_crossref` table | yes | **viz only** | **retrieval** (memory↔wiki bridge) | **5.54.2**: let recall surface a memory's linked wiki + `wiki_query` surface source memories. (Spike: does recall return wiki today?) |
| **memory_wiki** (provenance) | `wiki_add` caller `source_memory_ids` | yes (`wiki_page.source_memory_ids` array) | **viz only** | **retrieval** (bridge) | **5.54.2**: same bridge. |
| **causal** | PC-algorithm causal discovery (`consolidation/causal.py`), deferred | yes | **viz only** (the one entity-subset currently rendered) | **display** | keep rendering; not fed to retrieval (causal ≠ relevance). |
| **semantic** | computed at viz-build time (`graph_api._compute_semantic_edges`, KNN ≥0.75) | **no** (display-only compute) | **viz only** | **display** | KEEP as display, but it's REDUNDANT with the vector retrieval signal — do NOT wire to retrieval (noise, not signal). Lazy-compute per toggle (5.54.3). |
| **temporal** | computed at viz-build from `memory.slot_index` (engram slot co-membership) | no (computed) | **viz only** | **display** | weak signal; display only. Lazy-compute per toggle. |

## Decisions (brutal honesty)
- **Not every edge becomes retrieval.** `semantic` = the vector signal recall already runs; wiring it in is cost+noise. `temporal` is weak. Both stay **display**. Resisting "use every edge" is the whole point of this contract (anti leverage-theater).
- **The biggest hidden capability** = the entity graph (drives retrieval, invisible in viz). 5.54.1 makes it help everyday recall; 5.54.3 makes it visible.
- **The dead high-value edges** = `transition` + the memory↔wiki bridges (`wiki_crossref`/`memory_wiki`). Stored, never read by retrieval today. 5.54.2 activates them.
- **Viz fidelity (5.54.3):** render ALL edge types, per-type toggleable, **role-styled** (retrieval vs display) so "show all" showcases real capability without implying a decorative edge does work it doesn't. Lazy-compute heavy types; reheat physics on toggle.

## Enforcement
Per I29: any future edge/relation added MUST land a row here with a declared role + consumer, or it's a rejectable orphan. 5.54.4 GCs anything that ends this train still in `drop`.
