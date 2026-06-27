# Edge Contract (v5.54.0 — Phase 1)

Authoritative role declaration for every graph edge/relation in yadgar. Enforces invariant **I29** (leverage-completeness): every stored/computed edge has a named consumer or is dropped, and the viz must reflect what drives behavior. Source investigation: edge-mechanics audit 2026-06-12 (file:line cited). Governs the rest of `PLAN_V5_54`.

## Roles
- **retrieval** — feeds `recall()` ranking (PPR / spreading / a precomputed prior). Must reach the everyday/`fast` path (via precompute, 5.54.1).
- **display** — viz only. Legitimate (structure/teaching) but must be visually distinguished as NON-load-bearing so the viz doesn't misrepresent capability.
- **drop** — no consumer after the keepers are wired; stop computing/storing (5.54.4).

## Contract table

| Edge / relation | Producer (where created) | Stored? | Consumer TODAY | TARGET role | Action |
|---|---|---|---|---|---|
| **entity typed-relations** (`co_occurrence`, `resolved_by`, `caused_by`) | consolidation (`cls.py`), deferred | yes (entity graph) | `recall` PPR (w=0.5) + spreading (w=0.3) — **`balanced`/`full` only**, OFF in `fast` | **retrieval** | **5.54.1**: precompute per-memory prior in consolidation → `fast`/everyday recall adds O(1). The big "make it everyday" win. **5.54.3**: render in viz (currently INVISIBLE except causal). **5.86 Batch-2 (P0.4)**: `imports`/`calls` dropped from the viz registry (code-only, always empty on a prose corpus — the legend was lying); they remain valid entity-graph relations feeding retrieval, just not surfaced in viz. `resolved_by` extractor fixed (now emits the solution entity) so it's genuinely populated. |
| **transition** (co-recall) | `recall.py:record_transition` on each recall | yes (`memory_transition` table) | **retrieval** — cofire_prior precomputed in consolidation, O(1) read in fusion (done v5.54.2) | **retrieval** | **done (v5.54.2)**: `_compute_cofire_priors` in consolidation reads `memory_transition` once, normalizes co-recall frequency to `cofire_prior` field; fusion adds `WRRF_COFIRE_PRIOR_WEIGHT * cofire_prior` across all profiles including fast. No query-time traversal. |
| **wiki_crossref** (`[[slug]]`) | `wiki_add` `_extract_wikilinks` → `wiki_crossref` table | yes | **viz only** | **display** | **5.54.2 option A (2026-06-12)**: recall already surfaces wiki via parallel semantic query (`recall.py:273`); edge-bridge skipped per I29 (leverage-theater). Display only. |
| **memory_wiki** (provenance) | `wiki_add` caller `source_memory_ids` | yes (`wiki_page.source_memory_ids` array) | **viz only** | **display** | **5.54.2 option A (2026-06-12)**: same as wiki_crossref — recall already returns wiki via parallel semantic query (`recall.py:273`); edge-bridge skipped per I29 (leverage-theater). Display only. |
| **causal** | PC-algorithm causal discovery (`consolidation/causal.py`), deferred | yes | **viz only** (the one entity-subset currently rendered) | **display** | keep rendering; not fed to retrieval (causal ≠ relevance). |
| **semantic** | computed at viz-build time (`graph_api._compute_semantic_edges`, KNN ≥0.75) | **no** (display-only compute) | **viz only** | **display** | KEEP as display, but it's REDUNDANT with the vector retrieval signal — do NOT wire to retrieval (noise, not signal). Lazy-compute per toggle (5.54.3). |
| **temporal** | computed at viz-build from `memory.slot_index` (engram slot co-membership) | no (computed) | **viz only** | **display** | weak signal; display only. Lazy-compute per toggle. |
| **memory_similarity_link** | CLS phase (`consolidation/cls.py`), deferred | yes (`memory_similarity_link` table) | **viz only** (near-duplicate dedup signal) | **display** | v5.80 #80: rendered in viz as informational edge (role="informational" in EDGE_TYPES; role vocab renamed from "display"→"informational" in viz_meta.py — see note below). Not wired to retrieval ranking. |

## Decisions (brutal honesty)
- **Not every edge becomes retrieval.** `semantic` = the vector signal recall already runs; wiring it in is cost+noise. `temporal` is weak. Both stay **display**. Resisting "use every edge" is the whole point of this contract (anti leverage-theater).
- **The biggest hidden capability** = the entity graph (drives retrieval, invisible in viz). 5.54.1 makes it help everyday recall; 5.54.3 makes it visible.
- **The dead high-value edges** = `transition` + the memory↔wiki bridges (`wiki_crossref`/`memory_wiki`). Of these: **`transition` is now activated (v5.54.2)** via the cofire_prior precompute path. The wiki bridges (`wiki_crossref`/`memory_wiki`) are **NOT wired to retrieval** — option A confirmed 2026-06-12: recall already surfaces wiki via a parallel semantic query (`recall.py:273`), making an edge-bridge leverage-theater per I29. Both wiki edge rows downgraded from `retrieval` → **display**.
- **Viz fidelity (5.54.3):** render ALL edge types, per-type toggleable, **role-styled** (retrieval vs display) so "show all" showcases real capability without implying a decorative edge does work it doesn't. Lazy-compute heavy types; reheat physics on toggle.

## Vocabulary note — viz role rename (v5.80 #80)
The `display` role in this contract table describes the retrieval posture ("viz-display-only, not wired to retrieval"). In `viz_meta.py` the edge `role` field was renamed from `"display"` → `"informational"` for all non-retrieval types (v5.80 #80 viz-fidelity-v2). This contract table retains `display` as the canonical term for the posture; the viz `role` field uses `"informational"` for more accurate semantics. The two are equivalent: display-role edge ↔ role="informational" in viz JSON.

## Enforcement
Per I29: any future edge/relation added MUST land a row here with a declared role + consumer, or it's a rejectable orphan. 5.54.4 GCs anything that ends this train still in `drop`.
