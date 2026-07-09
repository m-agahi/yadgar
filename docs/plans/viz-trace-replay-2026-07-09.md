# Plan: Viz "Traces" tab — live trace-replay mesh + oscilloscope design language

**Status:** AGREED — user approved mockup + aesthetic 2026-07-09 ("i wish the entire viz ui could look as sleek" → design-language extraction is in scope as Phase 0/rolling restyle).
**Date:** 2026-07-09
**Mockup (design spec):** `docs/plans/viz-trace-replay.mockup.html` — phosphor-oscilloscope: dark instrument base, graticule + scanlines, phosphor-green Core lane / signal-cyan Backend lane, comet-trail pulses with duration-proportional dwell, Michroma display + Spline Sans Mono data, red reserved for faults.
**Feasibility study:** session 2026-07-09 (agent a51c646b) — verdict FEASIBLE, ~11–15 days, Option B data path.

## Scope decisions (locked)
- **"Live" = replay-on-completion**, not span streaming: existing SSE channel (`/api/graph/events`) gains a `trace_complete` event carrying trace-id; browser fetches that trace by id (Tempo by-id is fresh ~100ms; only name-SEARCH lags 60–90s). True streaming (OTLP tap) explicitly deferred.
- **Stage-first drill-down**: mesh shows ≤20 aggregated stages (simplify_trace selection: ≥1%-of-wall/≥10ms keep, storm aggregation ×N, lane by svc); node click expands child spans in a detail panel. Raw 42k-span meshes never rendered.
- **Data prep backend/cached, never hot-path** (daemon --cpus 1): mesh JSON computed on demand, LRU-cached (size ~20, TTL ~10min).

## Phases

### Phase 0 — Design tokens (the "entire UI could look as sleek" answer)
Extract the mockup's design system into `static/viz-theme.css`: CSS variables (palette, lane colors, fault red), font faces, panel/chrome treatments (graticule backdrop, glow, borders), typography scale. Apply to the SHARED chrome immediately: tab bar, panel frames, headers. Existing tab contents untouched this phase — they inherit the frame. Rolling rule from here: any tab that gets touched for other reasons adopts the tokens fully (config panel and settings redesign are the first natural adopters).

### Phase 1 — Mesh data pipeline (~3–4d)
- Extract simplify_trace.py's pure logic (tree build via start-containment, PLUMBING/LOWLEVEL collapse, storm aggregation, lane assignment, ALIASES) into `yadgar/_shared/trace_mesh.py` (~200 LOC, no DiagramSpec/DOT coupling). simplify_trace.py becomes a consumer.
- Core endpoints: `GET /api/traces/recent` (last N tool boundary traces: tool, total_ms, status) + `GET /api/traces/{id}/mesh` → {nodes:[{id, label(friendly), svc, rel_ms, dur_ms, storm_n?, error?}], edges, timeline_ms}. Tempo client reuses the viz proxy auth pattern (viz_server._proxy_request). LRU cache.
- Tests: trace_mesh pure functions (fixtures = existing out/*.json captures — including the wiki_read early-http-send monotonicity gotcha and the audit_anchors dropped-boundary flat forest), endpoint shape contract (model on test_graph_api_contract).

### Phase 2 — Traces tab + replay (~4–5d)
- New tab via existing nav-group pattern (Observability group). Canvas/SVG mesh render per mockup (vanilla JS; 3d-force-graph NOT needed — lanes are a fixed layout, not force-directed).
- Replay engine: pulse animation from rel_ms/dur_ms, play/pause/restart, ×0.5–×4, scrub over depth waterfall. Drill-down panel per mockup. Fault stages red with error subtitle.
- Testable logic (stage layout, timeline math, scrub mapping) in pure helpers → vitest (viz-tests/); DOM wiring thin per repo convention (no browser harness — validate by reasoning + live smoke).

### Phase 3 — Live wiring + polish (~2–3d)
- SSE `trace_complete` event (extend /api/graph/events queue) → sidebar auto-updates, optional auto-replay toggle.
- Live-metrics badges on stage nodes (p95/rate) ONLY if cheap from existing histograms — else drop (Prometheus lacks per-stage metrics today; do not build new metrics for this).
- Perf gate: tab load adds ≤1 API call; measure against the #91 slow-load baseline BEFORE ship (baseline measurement is a Phase 3 entry criterion).

## Fold-in verdicts (from feasibility study)
- `settings-panel-redesign` BUG A (yaml-aware source()): land FIRST as independent correctness fix — not part of this train, but its UI becomes a Phase-0 token adopter.
- Viz triage #55 (68 unverified items) + #60 config P3/P4: NOT blocking, run parallel/after.
- #91 slow-load: baseline measured in Phase 3; if baseline itself is bad, fixing it precedes ship.

## Risks
- Span storms upstream (task #6 span-budget policy) — the mesh aggregates regardless, but dropped BOUNDARY spans (audit_anchors class) make traces unfindable; #6's fix improves source data.
- Single-file index.html (179KB) — Traces tab adds to it; keep tab code in separate JS file(s) loaded from index to avoid deepening the monolith.
- Tempo availability: tab must degrade gracefully (empty state with reason) when Tempo is down — it's an optional observability dependency, never a viz-breaking one.

## References
Mockup: docs/plans/viz-trace-replay.mockup.html · Feasibility: session agent a51c646b (data-path options table) · simplify_trace.py (aggregation source) · ADR-0074 (span budget) · docs/diagrams/mcp-tool-traces-2026-07-09.md (trace format + numbers).
