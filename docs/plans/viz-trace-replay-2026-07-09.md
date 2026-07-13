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

---

## AUDIT (2026-07-13, opus adversarial auditor)

**Status:** AUDITED — ready for Phase 0-1 build; **Phase 2 gated on `car/npm-audit` (vitest 3.x) merge**; **Phase 1/3 data-reliability soft-gated on P-SB span-budget sweep (`feat/obs-quickwins-train`), audit_anchors-class tools only.** Architecture is sound — Tempo by-id data path is real and correct, simplify_trace aggregation is real, SSE channel is real, graceful degradation is designed in. No hard blockers. Original `Status: AGREED` retained above; this line supersedes it for build purposes.

Every load-bearing claim re-verified against master (core 5.132.0, backend 5.43.0, post-Reorg-Round-2). Findings: **1 WRONG conclusion I had to correct mid-audit (vitest merge state), 3 STALE paths, 0 fatal design flaws.**

### A.1 Verification table

| # | claim (plan) | verdict | evidence |
|---|---|---|---|
| 1 | Status "AGREED — user approved mockup + aesthetic 2026-07-09" | VERIFIED | plan L3; mockup `docs/plans/viz-trace-replay.mockup.html` exists (46KB). Note: task tracker calls #25 "APPROVED"; plan says "AGREED". Same intent. |
| 2 | `simplify_trace.py` is the aggregation source (keep ≥1%/10ms, storm ×N, two-lane, ALIASES) | VERIFIED | `docs/diagrams/simplify_trace.py` — docstring + code confirm keep-floor, `>=4 identical sibling → ONE box`, blue CORE / orange BACKEND lanes, MAX_BOXES cap. Exactly as plan describes. |
| 3 | Data source = Tempo, by-id fetch fresh ~100ms, name-SEARCH lags 60–90s | VERIFIED | `docs/diagrams/capture_trace.py`: `TEMPO="http://localhost:3200"`, `/api/traces/{tid}` by-id fetch (L119), `/api/search` with retry loop for async-export lag (L52-55). Plan's freshness split matches the by-id-vs-search reality. |
| 4 | Extract pure logic → `yadgar/_shared/trace_mesh.py`; simplify_trace becomes consumer | VERIFIED (feasible) | `yadgar/_shared/` exists; `trace_mesh.py` not yet present (expected — Phase 1 target). **Import-linter clears it:** viz endpoint lives in `yadgar/core/` → `core -> _shared` is ALLOWED by the `layered: core\|backend -> _shared` contract (pyproject). `docs/diagrams/simplify_trace.py` consuming it is UN-CONTRACTED (importlinter `containers=["yadgar"]` only; docs/ excluded) — allowed. No contract trips. |
| 5 | SSE channel `/api/graph/events` exists; gains `trace_complete` event | VERIFIED (endpoint) / EXPECTED-NEW (event) | `http.py:2136` `@mcp_server.custom_route("/api/graph/events", GET)`; consumed by `index.html:3060` EventSource. `trace_complete` event does not exist yet — Phase 3 work, correctly scoped. |
| 6 | Tempo client reuses `viz_server._proxy_request` auth pattern | STALE PATH | Real code: `yadgar/core/viz/viz_server.py:56` `_proxy_request` (moved T2 Car D3, ADR-0084). `yadgar/core/viz_server.py` is a PEP-562 back-compat SHIM — do NOT build against it. Pattern itself (`_proxy_request` proxy w/ auth) is real and reusable. |
| 7 | `static/index.html` (179KB) node/tab render; add `static/viz-theme.css`; keep tab JS in separate files | STALE PATH (size VERIFIED) | Actual path is **`yadgar/core/static/index.html`** (not `static/`). Size = 179,777 bytes = 179KB — exact. Separate-JS pattern already in use (`./lib/marked.min.js`, co-located `viz_filters.js`/`viz_helpers.js`/`viz_positions.js`). `viz-theme.css` target path corrects to `yadgar/core/static/viz-theme.css`. |
| 8 | `nav-group` pattern (Observability group) for the new tab | VERIFIED | `index.html:563-609` `#tab-bar .nav-group` + `.nav-group-menu` dropdown pattern present. |
| 9 | vitest test harness (viz-tests/); DOM wiring thin, no browser harness | VERIFIED (w/ path nuance) | `viz-tests/vitest.config.js` `include: ['../yadgar/core/static/**/*.test.js']` — tests are **co-located in `yadgar/core/static/*.test.js`**, run from `viz-tests/`. Plan's "vitest (viz-tests/)" is directionally right; the actual test files live under `core/static`. "No browser render harness" convention CONFIRMED (wiki `viz-frontend-has-no-browser-test-harness`). |
| 10 | (my mid-audit conclusion) vitest 3.2.7 already on master | **WRONG — CORRECTED** | master `viz-tests/package.json` = `^2.0.0`, `node_modules` = **2.1.9**. `git cherry master car/npm-audit` → `+ fbb48cec` (`+` = **NOT in master**). The `(#36)` is the branch's PR number, not merge proof. **The vitest 2→3.2.7 bump is PENDING on the unmerged `car/npm-audit` branch.** This is a real in-flight dependency (below). |
| 11 | Referenced `docs/diagrams/mcp-tool-traces-2026-07-09.md` (trace format + numbers) | VERIFIED | file exists (11KB). |
| 12 | ADR-0074 (span budget) cited | VERIFIED | accepted 2026-07-09; the span-storm→boundary-drop policy this plan's Risk section leans on. |
| 13 | 3d-force-graph NOT needed (lanes = fixed layout) | VERIFIED (sound) | index.html already loads `3d-force-graph@1.73.0` for the main graph; a fixed-lane canvas/SVG render is the right call for a waterfall — no force sim. Design-defensible. |

### A.2 Design challenge — is the animated span-flow feasible with the ACTUAL span data?

**YES — and the flush-truncation caveat does NOT block this plan.** This is the audit's central finding, stated precisely:

- The P-SB observability RCA (`psb-observability-2026-07-13.md:168`) notes BatchSpanProcessor **log-flush** truncation killed warm span reconstruction — but that is the **`podman logs` / `span_end` structured-log path**. Its own words: *"warm attribution needs Tempo."* The trace-replay mesh reads **from Tempo by-id** (claim #3), which receives the FULL exported span tree via OTLP off-thread. **The mesh is on the correct side of the caveat — the log-flush truncation is irrelevant to it.**
- **P-SB has two independent pieces; the mesh depends on only ONE.** P-SB's metric-arm revival (the dead `yadgar_observe_*` circular-import fix) runs on a DIFFERENT code path — `trace_span`/OTLP export does not touch the Prometheus registry (P-SB §1 states this explicitly). So the mesh is **NOT** blocked on the P-SB metric fix. Do not read this audit as "blocked on P-SB observability."
- The mesh depends only on P-SB's **hot-loop span-budget sweep** (ADR-0074 / §3.4): the real hazard is OTLP queue saturation from span storms (`audit_anchors` = ~42k `_cosine_similarity` spans) → **BOUNDARY SPANS DROPPED → `tool.audit_anchors` unfindable in Tempo**. If the boundary span is dropped, the mesh has no trace to fetch for that tool.
- **BUT that sweep is SCOPE-BOUNDED.** P-SB §3.4 seeds `_span_budget` with **only** `tools.project._cosine_similarity` (the audit_anchors storm); recall's `_row_to_dict`/`_extract_id` storms are claimed already exempt/undecorated. So P-SB fixes the *worst* storm, not all storms. **Data-reliability verdict is PER-TOOL:** clean for non-storm tools NOW, clean for audit_anchors POST-P-SB-sweep, unverified for any other future storm source. This is a **soft/partial dependency**, not a hard block — the plan's graceful-degradation design (Risk L41: empty state when Tempo down / trace absent) handles a missing trace correctly.

**The plan's Risk L39 UNDERSTATES this** — it frames dropped boundary spans as "#6's fix improves source data" (nice-to-have). Reality: for storm-prone tools, a dropped boundary span means the mesh **cannot render that trace at all** until the sweep lands. Recommend upgrading L39 to a named dependency (see A.4).

### A.3 Drill-down backed by real endpoints?

- Plan's Phase-1 endpoints `GET /api/traces/recent` + `GET /api/traces/{id}/mesh` do NOT exist yet (grep: zero `/api/traces` routes) — **expected**, they are the Phase-1 deliverable. Their feasibility rests on the `_proxy_request` Tempo-proxy pattern (real, claim #6) + LRU cache (standard). Buildable.
- Drill-down node→child-span expansion consumes the same by-id Tempo payload already fetched for the mesh (`capture_trace._extract_spans` flattens the full span table) — the child data is present in the fetched trace, so drill-down needs no additional endpoint. Sound.

### A.4 Corrections to fold into the build (non-blocking)

1. **Paths:** `static/index.html` → `yadgar/core/static/index.html`; `static/viz-theme.css` → `yadgar/core/static/viz-theme.css`; `viz_server._proxy_request` → `yadgar/core/viz/viz_server.py:56` (NOT the `yadgar/core/viz_server.py` shim).
2. **Phase 2 (vitest) is gated on `car/npm-audit` merge** (vitest 2.1.9 → 3.2.7). v2→v3 has config/API breaks — Phase-2 helper tests must be authored/validated against 3.x, sequenced AFTER that car merges. Phase 0-1 (Python + CSS + endpoints) are unaffected and can start now.
3. **Phase 1/3 Tempo data-reliability is soft-gated on P-SB's `_cosine_similarity` span-budget sweep** (`feat/obs-quickwins-train`) for `audit_anchors`-class storm tools only. Non-storm tools render now. Not a hard block (degrades gracefully); do not defer Phase 1 for it.
4. **Live-metrics badges (Phase 3):** plan already self-guards "ONLY if cheap from existing histograms — else drop." Reinforced by P-SB: per-stage recall histograms are currently metric-SILENT (dead `@observe` arm, P-SB P0) — do NOT assume per-stage p95/rate exists until P-SB P0 lands. Plan's "else drop" default is correct; keep it.

### A.5 Verdict

**AUDITED — build-ready for Phase 0-1 immediately; Phase 2 sequenced behind `car/npm-audit`; Phase 1/3 data reliability partial until P-SB sweep (storm tools only).** No fatal flaw. Architecture verified real end-to-end (Tempo by-id → simplify_trace aggregation → fixed-lane render → SSE live-wire). Correct the 3 stale paths in A.4 at build time.
