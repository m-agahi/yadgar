# PLAN v5.54 — Graph Leverage + Viz Fidelity (umbrella)

Status: PLANNED 2026-06-12. UMBRELLA across v5.54.0 → v5.54.4. Diagnostic source: edge-mechanics investigation 2026-06-12 (file:line cited below). Sibling theme: `[[yadgar-knowledge-base-usability-rca-why-claude-doesn-t-read-firs]]` ("rich structure, thin leverage").

## Root cause: stored ≠ shown ≠ used

Yadgar computes/stores rich graph edges, but three layers disagree:
- **Retrieval ignores most edges.** Only the typed ENTITY graph (`co_occurrence/imports/calls/resolved_by/caused_by`) feeds recall, via PPR (weight 0.5) + spreading-activation (0.3) — and **only in `balanced`/`full` profiles**. The `fast` profile (used by the hooks: `prompt-recall`, `subagent-start`) drops both → **everyday auto-recall gets ZERO graph signal** (`retrieval/fusion.py:15-26` profiles; confidence-gating zeros PPR/spread per-query at 0.1, `fusion.py:128-148`).
- **The viz shows the WRONG graph.** All 5 rendered edge types (`semantic`/`temporal`/`transition`/`wiki_crossref`/`memory_wiki`, `graph_api.py:56-270`) are **computed-at-viz-time or stored-but-unread-by-recall**. The entity graph that actually powers retrieval is **invisible** (only the `causal` subset renders, `graph_api.py:230-251`).
- **Dead high-value edges.** `transition` (co-recall, `memory_transition` table) and the memory↔wiki bridges (`wiki_crossref` table + `wiki_page.source_memory_ids`) are stored but **never read by retrieval** — real unused leverage.

End state: every edge either earns retrieval value or is honest display; everyday recall benefits from the graph; the viz faithfully renders the graph that drives behavior.

## Brutal-honesty constraints (shape the whole plan)
1. **Latency is the boss.** Graph traversal at query time is why `fast` drops it (the 5.51 CPU-spike risk). So "edges help everyday use" = **precompute** the signal during consolidation into a cheap per-memory prior that `fast` recall adds in O(1). NEVER traverse-at-query on the hook path.
2. **Don't wire redundant signals for show.** `semantic` edges = KNN similarity = exactly the vector signal recall already runs. Feeding it to retrieval adds cost+noise, not signal. `temporal` (engram-slot co-membership) is weak. These stay **display-only** (or get GC'd) — NOT forced into retrieval.
3. **Viz fidelity ≠ raw dump.** Rendering every edge over thousands of nodes = an unreadable hairball. Fidelity = render the *real* (entity) graph with **level-of-detail**: neighborhood-of-selected-node, edge-type toggles, strength thresholds.

---

## v5.54.0 — Phase 1: Edge contract (audit + decide each edge's fate)

Foundational, cheap. Produce `docs/EDGE_CONTRACT.md` (+ mirror to wiki): for EVERY edge/relation type, declare its role — `retrieval` (feeds recall), `display` (viz only), or `drop` (compute nothing). Honest triage:
- **retrieval:** entity typed-relations (already used), `transition` (co-recall — wire in P3), memory↔wiki bridges (wire in P3).
- **display:** `semantic` (redundant w/ vector), `temporal` (weak), `causal` (already shown).
- **drop/GC candidates:** anything computed+stored that no consumer reads after P1-P3 (decided in P5).
Acceptance: a single table mapping edge → producer → consumer(s) → role. This is the coherence contract the rest of the plan enforces.

## v5.54.1 — Phase 2: Precomputed graph prior (everyday recall gets graph, no latency)

THE core fix. Move graph computation OFF the query path:
- During **consolidation** (`consolidation/cls.py`, the background cycle), compute a per-memory **graph prior**: PPR/entity-centrality + co-recall adjacency rollup → store as a cheap scalar/small-vector field on the memory row.
- `fast`-profile recall adds this prior as an O(1) boost in fusion (`retrieval/fusion.py`/`scoring.py`) — no traversal, no entity-extraction, fits the hook latency budget.
- Result: hook-driven everyday recalls (`prompt-recall`, `subagent-start`) finally benefit from the graph, cheaply.
Caveat: precomputed priors can lag the live graph between consolidation cycles — acceptable at consolidation cadence; document staleness window. TDD: prior is computed + stored in consolidation; fast recall ranking shifts measurably when a memory has high graph-prior vs none; latency budget unchanged (no per-query traversal).

## v5.54.2 — Phase 3: Activate the dead high-value edges

Using the P2 precompute infra (cheap, off-query):
- **`transition` (co-recall):** precompute a per-memory "frequently co-recalled with" boost. A hit on A lifts memories historically recalled with A. (Currently `memory_transition` is written on recall, `recall.py:242-251`, and never read back.)
- **Memory↔wiki bridge:** unify the two silos. Today recall is memory-only and `wiki_query` is wiki-only. Wire `wiki_crossref` + `source_memory_ids` so: recalling a memory can surface its linked wiki page, and `wiki_query` can surface source memories. (Spike first: confirm whether recall returns wiki nodes at all today; design the bridge.)
- Skip `semantic`/`temporal` per the contract (redundant/weak).
TDD: a co-recalled memory ranks higher; a memory with a linked wiki page surfaces it; bridge respects scope/branch.

## v5.54.3 — Phase 4: Viz fidelity (ALL edges visible, toggleable, role-distinguished)

Stance (user-confirmed 2026-06-12): show **all** edge types — it showcases the system's actual capabilities — with per-type toggles to manage convolution. The toggle dissolves the hairball objection; the goal is the FULL picture, honestly labeled.

- **Render EVERY edge type, each toggleable.** Including the currently-INVISIBLE entity graph (`co_occurrence/imports/calls/resolved_by/caused_by`, not just `causal` — `graph_api.py:230-251`) which is the biggest hidden capability (it powers retrieval). Per-type on/off toggles (extend the existing edge-legend overlay, `index.html:944`). Sensible default-on set (retrieval-active + structural); everything flippable on.
- **Role-distinguished styling (honesty / I29 coherence).** "Show all" must NOT style a decorative edge identically to a load-bearing one — that would misrepresent capability. Visually distinguish **retrieval-active** (entity, `transition`, memory↔wiki bridges — these move recall) from **structural/display** (`semantic`=KNN, `temporal`=slot). Drive styling + the per-type legend from the P1 EDGE_CONTRACT + `viz_meta.py`/`EDGE_TYPES` (v5.50.13 single source). Optionally surface each edge's retrieval-contribution weight so the viz TEACHES what matters.
- **Lazy-compute per toggle (performance).** Do NOT build every edge type upfront — `semantic` is ~O(n²) KNN; on thousands of nodes that's costly even when toggled off. Compute an edge type's data only when its toggle flips on (or for the selected-node neighborhood). Keep `/api/graph` cheap by default; fetch heavy edge types on demand.
- Optional strength/weight threshold slider per type to thin dense edge sets.
TDD: every edge type appears when its toggle is on + absent when off; entity edges render; role styling driven by the contract; heavy edge types are lazy (not computed until requested); legend covers all types with their role.

## v5.54.4 — Phase 5: GC the unused edges

Per the P1 contract, stop computing/storing anything no consumer reads (after P2-P3 wired the valuable ones). Reduce "tons of edges we don't use." Remove dead compute paths in `graph_api.py` + any orphan storage. TDD: dropped edge types no longer computed/stored; no consumer breaks.

---

## Sequencing & dependencies
P1 (contract) first — it governs the rest. P2 (precompute) is the enabling infra for P3. P4 (viz) can run after P1 (needs the contract) independently of P2/P3 but is best after P3 so it shows the newly-active edges. P5 last (GC after the keepers are wired). Ties: P2 must respect the 5.51 latency budget; P4 builds on 5.50.13 Help-tab legend.

## Honest caveats
1. Multi-release architecture work, not a patch.
2. Precompute, never traverse-at-query for `fast` — or you reintroduce the CPU spike.
3. Resist "use every edge" — redundant signals (semantic=vector) are noise; the contract (P1) is the discipline that prevents leverage-theater.
4. Viz "accurate" needs LOD or it's an unreadable hairball.
5. Memory↔wiki bridge is the highest-value new capability but needs a design spike (does recall return wiki today?).

## Success signal
(a) fast-profile recalls measurably improve with the precomputed prior; (b) co-recalled + wiki-linked items surface in everyday recall; (c) viz renders the entity graph (matches what retrieval uses); (d) zero stored edge types with no consumer (the contract holds).

## Ship discipline
Each sub-version = own core release (branch → master → tag → dockerhub → pypi → nix). Backend likely unchanged (retrieval is core). Update this table as phases land.
