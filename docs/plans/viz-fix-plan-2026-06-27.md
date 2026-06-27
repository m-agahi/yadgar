# Viz fix plan — 2026-06-27

**STATUS: SHIPPED in v5.86 (PR #127) — Batch 1/2/3 + config-panel P1/P2 + the OT-C4 car. DEFERRED to v5.87 (task #60): config-panel P3/P4 (restart/destructive/audit) + Prometheus retention (#53, nix). Viz triage #55 (90-item wiki checklist) still ~22/90 user-verified.**

Post-refactor the viz has real regressions (user report 2026-06-27). Triage = 90-item
checklist in wiki `viz-triage-checklist-2026-06-27-needs-user-verdict` + task #55 (verdicts).
This plan covers the confirmed problems, root-caused by 3 code-verify agents.

## Investigation summary

Three read-only audits against the LIVE deployment (`/api/graph` = 2476 nodes / 1761 edges /
769 clusters; `role` present on every edge — [7] PASS):

1. **CPU (the #1 complaint)** — high CPU when tab focused even idle.
2. **Search bugs + interaction layer** — wrong-node highlight, edges-don't-dim, missing interactions.
3. **Empty/sparse edges + clusters** — why the graph looks barren.

## The reframe (agent A)

The graph is NOT broadly broken. The "empty" feel decomposes into:
- **Code-graph edges on a prose corpus** — `imports` / `calls` / `resolved_by` only populate from
  literal source code; the memory corpus is prose work-summaries → always empty. The viz *advertises*
  these edge types anyway, so the legend lies.
- **One real missing bridge** — `memory_wiki` (mem↔wiki) never populated.
- **Cluster heat-cap** — 761/769 clusters render empty because member intersection is capped to the
  top-500-hottest memories; producer is correct.
- **Structural sparsity** (not bugs) — `temporal` (engram-slot collisions rare), `wiki_crossref` (few
  `[[links]]` in pages). `semantic` is lazy-by-design (UI toggle). `near-duplicate` = `memory_similarity_link` (348 rows, working).

---

## Prioritized fix plan

Size = S (≤1 file, hours) / M (a feature, ≤1 day) / L (sub-project).

### P0 — Truth & correctness (cheap, high-value; the graph currently misrepresents reality)

| # | Item | Root cause | Fix location | Size |
|---|---|---|---|---|
| P0.1 | Legend honesty [1][8] | stale "Semantic" + unlabeled top group live in the **hardcoded HTML fallback** | drop fallback, let dynamic renderer own it — `index.html:975,1071`; `viz_meta.py:45,244` | S |
| P0.2 | Search lights WRONG node [59] | `api_viz_search` routes through `recall()` WRRF capped top-5 → exact-title node drops out | exact/prefix-title precedence prepended before WRRF — `http.py:1859` | S–M |
| P0.3 | Edges don't dim on search [61] | `_linkColor` ignores `__dimmed`; 3D never re-applies `.linkColor()` | `index.html:1699` + chain `.linkColor` in `_applySearchHighlight` `:2181` | S |
| P0.4 | Advertised-but-always-empty edge types | `imports`/`calls`/`resolved_by` are code-only; legend lies on a prose corpus | **DECISION (below)**: drop from viz list OR fix `resolved_by` | S (drop) / M (fix) |

### P1 — Performance (your #1 complaint, best ROI)

| # | Item | Root cause | Fix | Size |
|---|---|---|---|---|
| P1.1 | High CPU focused/idle | force-graph + 3d-force-graph run an **unconditional rAF render loop** — cooldown stops layout *ticks*, not the *redraw*; 60fps forever; nothing calls `pauseAnimation()` | `pauseAnimation()` on `onEngineStop` (static) + `resumeAnimation()` on pointer/wheel/drag/reheat — `index.html:1736,1776,1819,2096` | S–M |
| P1.2 | Invisible graph still renders on other tabs | `_switchTab` only toggles CSS display | pause on leave-home, resume on return — `index.html:3054` | S |

(Ruled out: sim-never-cools is FALSE — `cooldownTime=15000` stops it ~15s; blur-gating irrelevant since focused=visible.)

### P2 — Real data gaps (make the graph truer/richer)

| # | Item | Root cause | Fix location | Size |
|---|---|---|---|---|
| P2.1 | mem↔wiki bridge empty [15/67] | `source_memory_ids` defaults `[]`/None on every wiki write path | populate at wiki creation `wiki.py:656,709`, OR derive from reverse `memory.wiki_refs` (already written by `_link_memories` `wiki.py:1591`) | M |
| P2.2 | 761/769 clusters render empty [13] | member intersection capped to top-500-hottest mem_ids | raise/relax cap or emit off-screen-member count — `graph_api.py:436` | M |
| P2.3 | No per-node cluster tint | node payload omits `cluster_id` | add `cluster_id` to `_assemble_memory_nodes` — `graph_api.py:175` | S |

### P3 — Interaction layer (mostly never shipped)

| # | Item | Note | Fix location | Size |
|---|---|---|---|---|
| P3.1 | Reheat on edge/node toggle [3][4] | one-liner; frozen-by-design today | `d3ReheatSimulation()` in handlers — `index.html:1479,2739` | S |
| P3.2 | Side panel runs off-screen [50] | no max-height/overflow | CSS — `index.html:105` `.cluster-panel`, `922` `.overlay-body` | S |
| P3.3 | **3D render-path overhaul** | the single biggest structural weakness — no per-node dim, no shape variation, `linkColor` not re-wired; unblocks P3.4/P3.6 + Bug-B-3D | `_makeNodeThreeObject` `index.html:1646` | M–L |
| P3.4 | Hover → neighborhood highlight [45] | 2D dim machinery exists; 3D none; no `onNodeHover` | `index.html:1736,1776` + flag-then-render | M |
| P3.5 | Focus mode (isolate node) [47] | new (pin already overloaded) | `linkVisibility` `index.html:1756,2045` | M |
| P3.6 | Connection-count badge [46] | degree computed (`__deg` `:1986`), never rendered | `drawNode` `1856` / `_makeNodeThreeObject` `1646` | M |
| P3.7 | 3D anchor/protected SHAPE [54] | 3D only varies size, no shape | `_makeNodeThreeObject` `1646` | M |
| P3.8 | Complete node-type filters [51] | only `show-wiki` toggleable; memory/entity heat-gated | `index.html:2030,2739,3323` | S–M |
| P3.9 | Search hide-mode [60] | dim hardcoded; hide-mode branches `__visible=false` | `applyFilters` `index.html:2021` | M |
| P3.10 | Bookmarked-wiki refresh [57] | static snapshot from load | add refresh re-fetch | S–M |

*Cross-cutting:* P3.3–P3.7 all hit `drawNode`(2D ~1856) / `_makeNodeThreeObject`(3D ~1646) + the
`__dimmed/__visible/__pinned/__deg` flag convention (`applyFilters ~2021`). Fix the 3D path once → several unblock.

### P4 — Config panel (dead debug-gated shell)

| # | Item | Note | Fix | Size |
|---|---|---|---|---|
| P4.1 | `enum_choices` missing [34] | `description`/`category`/`locked` present | add from FIELD_META — `control.py:117` | S |
| P4.2 | Editor empty + gated behind `YADGAR_DEBUG_APIS_ENABLED` | scaffold exists (search/category/table/restart) but no usable data/edit/apply | **own sub-project**: un-gate (or document the gate), wire GET→populate→PATCH edit→apply→restart (G4/G5 phases P1–P4) | L |

### P5 — Polish
Fonts (Fraunces/IBM Plex Mono) [86,87], palette [88], staggered reveal [30], smooth transitions [90],
weak-edge affordance [89], edge-weight slider [70]. All S, low priority.

---

## Decisions (RESOLVED — user 2026-06-27)

1. **imports/calls/resolved_by** → **FIX `resolved_by`** (extractor emits "error", handler targets
   "solution" — make them agree, `cls.py:155` + `knowledge_graph.py:228`); **DROP imports + calls** from
   the viz edge-type list/legend (code-only, irrelevant to a prose corpus).
2. **Config panel (P4.2)** → **INVEST — build it real** (un-gate, GET→populate→edit→apply→restart). Own
   car in the v5.86 train.

All viz work is **added to the v5.86 train** as cars (Batch 1 quick-wins, Batch 2 data, Batch 3
interaction, Batch 4 config panel).

## Suggested sequencing

- **Batch 1 (quick wins, high impact):** P0.1–P0.3 + P1.1–P1.2 + P3.1 + P3.2 + P2.3 + P4.1. All S/S–M.
  Fixes the #1 CPU complaint, the search bugs, the lying legend, reheat, panel scroll, cluster tint, enum.
- **Batch 2 (data truth):** P2.1 mem-wiki bridge + P2.2 cluster heat-cap + P0.4 decision.
- **Batch 3 (interaction):** P3.3 3D render-path overhaul first (unblocks the rest) → P3.4–P3.10.
- **Batch 4 (separate):** P4.2 config panel sub-project. P5 polish anytime.

## Not bugs (do not "fix")
`semantic` lazy-by-design · `near-duplicate` = `memory_similarity_link` (working) · `temporal`/`wiki_crossref`
sparse-but-correct · `role` field present · clusters ARE real `memory_cluster` rows (not a render-time BFS).
