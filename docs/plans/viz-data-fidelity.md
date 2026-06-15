# PLAN v5.81.0 — Viz connectivity visibility + filters + physics reheat (SKELETON)

Status: **SKELETON — scoping.** Re-scoped 2026-06-15 from "polish" to **data-fidelity refactor** after user directive ("the viz must show actual reality, not something made up"). The fidelity refactor (see `## REFACTOR` below: F1-F5) is now the priority driver and gates the cosmetic items 1-10. Follow-up on v5.54.3 (all-edges/role-styling). Still needs final scoping before build.

## Origin

User feedback on the deployed v5.54.3 viz ("nicer now, but…"). v5.54.3 shipped all-edges-toggleable + role-styling + lazy + (claimed) physics-reheat-on-toggle, but live testing found gaps. Tracked here per I27 (user-visible issues land in a plan).

## Items (problem statements — scope later)

1. **Better connectivity visibility — "show what is actually connected."** The headline want. v5.54.3 renders the real entity graph, but it's still hard to see a given node's actual connections. Ideas to scope: hover/select → highlight + isolate the selected node's neighborhood (fade the rest); connection-count badge per node; a "focus mode" that pins a node + shows only its edges; directional emphasis.

2. **Edge toggle does NOT reheat the physics engine (BUG).** v5.54.3 wired `d3ReheatSimulation()` guarded by `linksChanged`, but live: ticking edge types on/off does NOT re-layout — nodes stay put, layout goes stale. The reheat isn't firing on toggle (guard too strict, or links removed from render but not from `graphData()`, or reheat called on the wrong graph instance). Verify + fix so edge toggles reheat.

3. **Node-type filter incomplete — can only toggle `wiki`, not `memory`/`entity`.** Currently the only node-type filter is wiki (legacy). Add per-node-type toggles: `memory`, `wiki`, `entity` (mirror the per-edge-type toggle pattern from v5.54.3). Turning off a node type hides those nodes (+ their edges) and reheats.

4. **Node selection on/off does NOT reheat physics.** Selecting/deselecting a node doesn't re-layout. If selection drives a focus/isolate behavior (item 1), it should reheat (or animate focus). Wire selection → reheat/focus.

5. **Hold-click dims floating panels but they snap back while still holding (BUG).** Holding a click (drag-start) on the graph briefly dims the floating overlay panels, then they immediately return to full opacity even though the click is still held. Intended: panels stay dimmed for the duration of the hold/drag (so the graph is readable while dragging). Fix the dim-during-interaction state to persist while the mouse button is down (mousedown→mouseup, not a transient flash).

6. **Cluster panel: needs max-height + scroll.** The cluster/connections panel renders an unbounded "loooong list." Cap its height (max-height) + make it scrollable (overflow-y:auto) so it doesn't run off-screen.

7. **Better Info tab.** The Info tab (author card / bio / logo, added v5.50.7) needs improvement. Vague — scope later: what content/layout is wanted (richer bio? stats? links? project overview? live daemon info?). Capture concrete wants before building.

8. **Semantic search highlight is imprecise — false positives stay bright (BUG/UX, 2026-06-13).** Searching in the Home/graph tab (e.g. "ostad") dims non-matches, BUT among the bright/highlighted set, only SOME items actually match — many unrelated nodes AND edges are still shown bright, so the filtering is hard to read (live screenshots confirm). Two problems: (a) the highlight criterion is too loose (matching/keeping nodes that aren't real hits, or highlighting every edge incident to a matched node regardless of the other endpoint), and (b) the dim opacity for non-matches is too high (irrelevant stuff still visible). Want: crisp search filtering — ONLY true matches bright; non-matches fully dimmed (near-invisible) or hidden behind a toggle; edges bright ONLY when BOTH endpoints match (intra-match edges), not edges from a match to an unrelated node. Consider a "filter mode" that HIDES non-matches entirely (isolate the matched subgraph) rather than just dimming. Where: the search handler in `index.html` (`/api/viz/search` → `api_viz_search`) + the highlight/dim styling. Verify what the search endpoint returns vs what gets highlighted client-side — the false positives may be server-side (loose match) or client-side (over-broad highlight set).

9. **Anchored/protected nodes need a distinct SHAPE (not just color) (2026-06-15).** User request. Protected memories (`is_protected=true`, excluded from `get_all_memories_for_decay`, heat frozen) should be visually distinguishable by shape (e.g. diamond/star vs circle for normal nodes) so the heat-immortal set reads at a glance in the graph.

10. **Show `last_accessed` per node (2026-06-15).** User wants recency visible alongside heat (hover tooltip + detail panel). Heat alone is ambiguous after the v5.59 decay-idempotency fix — `last_accessed` (and now `last_decay_at`) explains *why* a node's heat is where it is. Surface both in the node detail panel; consider an optional recency-based fade/tint.

11. **Node detail panel shows "0 connections" for entity nodes though the graph draws their edges (BUG, 2026-06-15).** `graph-detail.js` (~line 259) counts only memory edge types when computing a node's connection count, so entity nodes report 0 even when co_occurrence/relationship edges are visibly drawn. This caused real confusion debugging "why does node X show heat 1.0 and 0 connections" — the node was an entity, and the panel undercounted. Fix: count all incident edge types (memory + entity/relationship) for the panel's connection tally.

12. **Bookmarked wiki viewer needs a Refresh (2026-06-15).** User request. In the Bookmarks tab, when viewing a bookmarked wiki page, add a Refresh control that re-fetches the page content AND its version history (`wiki_read` + `wiki_history` for the slug) so edits made elsewhere (e.g. via the v5.61 `wiki_*` edit primitives) appear without reloading the whole viz. The bookmarked wiki view is currently a static snapshot from load time — after a `wiki_replace_text`/`wiki_append_section` edit it's stale until a full reload. Where: the Bookmarks-tab wiki reader in `index.html` + the `/api/viz` wiki/bookmark endpoints. Same class as F2 (stale-heat): a rendered view diverges from the DB after a write — here a per-page refresh button is the fix.

## REFACTOR — viz data fidelity (single source of truth) — PRIORITY (2026-06-15)

User directive: **"the viz must show actual reality, not something made up."** The viz currently derives/freezes several values that drift from canonical DB state. This is the headline refactor; items 1-11 above are polish on top of it. Mapped root causes (verified):

**F1. Connection count diverges from rendered edges (client-side, `static/graph-detail.js:259-264`).** The detail panel tallies only 4 edge types (`semantic`, `temporal`, `transition`, `memory_wiki`) but the canvas renders up to 11 (adds entity relations `co_occurrence`/`imports`/`calls`/`resolved_by`/`caused_by`, plus `causal`, `wiki_crossref`). Entity nodes wired only by `co_occurrence`/`causal` therefore show "0 semantic · 0 temporal · 0 transition" while their edges are visibly drawn. **Fix:** derive the count from the SAME incident-edge set that is rendered (all types in `_edgeToggleState`), grouped dynamically — never a hardcoded subset. The count and the render must read one edge list.

**F2. Heat is frozen at write-time for SSE-injected nodes (server + client).** `/api/graph` reads heat LIVE from DB (correct), but the SSE stream (`/api/graph/events`) pushes `memory_added` nodes carrying write-time heat (`yadgar/server/.../_phase_post_write.py:202`) and there is NO `heat_updated` event. After consolidation/decay rewrites heat, already-rendered nodes keep their stale value until a full Reload. So a node shown at heat 1.0 may be 0.6 in the DB — "made up." **Fix options to scope:** (a) emit a `heat_updated`/`decay_applied` SSE event after the decay batch write so the client patches `allNodes` heat in place; (b) periodic lightweight heat re-sync; (c) at minimum, a visible "stale — reload" indicator with last-refresh age. Canonical heat = DB at read time; the client must never display write-time heat indefinitely.

**F3. Node identity is typed in the payload but bare in lookups (server + tooling).** Graph nodes are namespaced — `mem:{id}`, `entity:{id}`, `wiki:{id}` (`graph_api.py:145/224/494` via `_extract_id` 552-577) — so no collision in the payload. But the detail panel surfaces the raw integer and MCP tools take bare ints: `memory_get(3103)` returns null when the node is `entity:3103` (queries the wrong table). The viz is internally correct but presents an id that doesn't round-trip to a lookup → reads as "made up." **Fix:** panel shows the typed id explicitly (`entity:3103`) + node `type`; route lookups by type (or teach a unified `node_get(typed_id)`), so what's displayed maps back to a real row.

**F4. Edges silently dropped below threshold (server-side, `graph_api.py:195`).** `_build_transition_edges` filters `count < 2`, so count=1 co-recall edges exist in the DB but never render — the graph under-represents reality. **Fix:** either render them (thinner) or surface a "N weak edges hidden" affordance; don't silently hide DB truth.

**F5. Principle / invariant.** Establish a single-source-of-truth contract: every displayed value (id, type, heat, connection count, label) is derived from the canonical DB record at request/refresh time, or carries an explicit freshness marker. No value computed from a divergent subset (F1) or frozen past a DB write (F2). Add a fidelity test: build a known graph, mutate heat + add an entity edge via the DB, assert the served `/api/graph` payload and the panel-rendered count match the DB exactly. (Server payload is already live for `/api/graph`; the gaps are SSE freshness + client-side derivation.)

**Scope note:** F1 + F3-panel are client-side (`graph-detail.js`, `index.html`) — quick. F2 needs a server-side SSE event + client patch — the real work. F4 is a one-line policy + UI affordance. Bundle as the v5.81 fidelity pass; do this BEFORE the cosmetic items (1-10), since shape/last_accessed display are only meaningful once the underlying values are real.

## Common thread
Items 2 + 4 are the same root: **physics reheat isn't reliably triggered on viz state changes** (edge toggle, node-type filter, selection). v5.54.3's reheat-on-toggle likely has a guard/instance bug. Items 1 + 3 are missing filter/focus UX. Scope as one viz-polish pass.

## Notes for scoping
- Where: `yadgar/static/index.html` (toggles, selection, reheat) + `yadgar/static/viz_filters.js` (the v5.54.3 filter module) + the edge-legend overlay.
- Confirm `graphData()` is actually mutated (nodes/links removed) on toggle, not just visually hidden — d3-force only reheats meaningfully when the link/node set changes.
- Reuse v5.54.3's `EDGE_TYPES`/role infra; add a parallel node-type registry.
- JS tests run from `viz-tests/` (NOT repo root).
- Physics-reheat UX choice already decided in PLAN_V5_54 (gentle reheat vs freeze+button) — apply consistently.

## Effort (guess)
S–M. Mostly frontend. No backend/schema/migration. Bundle items 1-4 as one viz-polish release when picked up.
