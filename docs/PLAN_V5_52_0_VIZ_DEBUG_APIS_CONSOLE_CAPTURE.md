# PLAN — v5.52.0: Debug Viz Interaction APIs + Console Log Capture

**Status:** drafted 2026-05-31. REVISED 2026-06-02 post-opus-review (minor). RESCOPED 2026-06-12 post-audit. Plan-first per I27.

**Revision notes (opus reviewer):**
- ADD I24 `@trace_span` per endpoint (14 new HTTP handlers). Acceptance criterion.
- ADD I9 budget assertion for `_publish_state` handler (≤5ms p50) — POSTs 1Hz hot path if `YADGAR_DEBUG_APIS_ENABLED=on`.
- BYTE-cap ring buffer (1MB cap) instead of entry-count (1000). High log rates blow entry-count cap. Memory budget must be bounded.
- ADD XSS escape regression test for console-capture rendering path — `window.console` proxy is XSS-vulnerable if captured strings rendered without escape. Acceptance criterion.
- v5.50.2 introduces `YADGAR_DEBUG_APIS_ENABLED` — v5.52 does NOT re-register. Document dependency.

**Renumbered:** v5.43.0 → v5.52.0 on 2026-05-31. User explicitly bumped the viz train forward so the setup-refactor (v5.45-v5.47) ships first. Numbering is locked at v5.50 / v5.51 / v5.52. Do NOT revert to v5.43 anywhere.

**Depends on:** v5.50.2 shipped — `YADGAR_DEBUG_APIS_ENABLED` gate was introduced in v5.50.2 (see CHANGELOG). This plan extends the Debug tab surface introduced in v5.50.8. Also extends the Control tab actions/config/restart surface shipped in v5.50.2.

**Downstream:** none yet. This is the closing slot of the v5.50-v5.52 viz train.

**Effort estimate:** ~L (2.6-3 calendar days) for full scope. The high-value slice (screenshot + console capture — see §Rescope) is significantly smaller.

---

## Rescope 2026-06-12 (post-audit)

Audit performed 2026-06-12 against current source and CHANGELOG. Summary of findings:

**Scope validity confirmed.** This plan is NOT subsumed by the v5.50.8/.9 "Debug tab" work. The v5.50.8/.9 Debug tab is a JSON API *inspector* (introspection UI for existing MCP/HTTP responses). The v5.52.0 scope is a suite of *programmatic debug APIs* that let an external script or agent drive the viz and capture state. Name collision only — distinct functionality. All v5.52.0 scope is NOT STARTED.

**Gate version corrected.** `YADGAR_DEBUG_APIS_ENABLED` shipped **v5.50.2** (see CHANGELOG), not v5.50.0 as the original plan stated. Every reference in this plan is corrected to v5.50.2.

**Path drift corrected (IMPORTANT — repo is flat, no `api/` or `js/` subdirs):**
- `yadgar/server/api/debug_viz.py` → `yadgar/server/routes/debug_viz.py`
- `yadgar/server/api/logs.py` → `yadgar/server/routes/logs.py`
- `yadgar/server/api/control.py` → `yadgar/server/routes/control.py`
- `yadgar/server/static/js/control.js` → `yadgar/static/control.js`
- `yadgar/server/static/js/console_capture.js` → `yadgar/static/console_capture.js`
- `yadgar/server/static/js/viz_state_publisher.js` → `yadgar/static/viz_state_publisher.js`
- `yadgar/server/static/js/home.js` → `yadgar/static/home.js`

**Log-panel home updated.** The original plan debated Control vs Info tab, assuming Control tab was empty. Control tab is now FULL (actions/config/restart since v5.50.2) and a dedicated **Debug nav tab exists** (v5.50.8). The two log panels belong in the **Debug tab**, not Control.

**Auth middleware note added.** Extending the debug gate to `/api/debug/viz/` and `/api/logs/` requires adding those prefixes to the auth middleware prefix list. Currently the middleware only gates `/api/control/*`.

**Value rescope — build the high-value slice first.** The 12-endpoint programmatic viz-control API (camera/select/overlay) is speculative and low-priority: no current consumer, uncertain ROI. The **high-value slice is the screenshot endpoint + browser console capture**. Together they let an agent self-verify viz changes and catch JS errors, which closes the "headless Chromium has no WebGL" gap. Recommendation: build the screenshot + console-capture slice first; defer the camera/select/overlay API until something concretely needs it.

---

## Goal — programmatic + visual debug surface for the viz

Ship two things:

1. **Debug viz interaction APIs.** Four categories of HTTP endpoints that let an external script (or a Claude agent) drive the viz programmatically: select / inspect a node, simulate overlay interactions, control the camera, query event log + take screenshots. Same auth gate as the Control tab (`YADGAR_DEBUG_APIS_ENABLED=on` + bearer token). **Recommend building only the screenshot endpoint in the first pass — see §Rescope above.**

2. **Console log capture.** Two stacked panels in the **Debug tab** (see §Rescope — not Control tab): a top panel streaming daemon log lines via SSE or polling, a bottom panel capturing browser console output via a `window.console` proxy. Each panel has level filter (DEBUG/INFO/WARN/ERROR), pause, clear, copy-all.

Why now: agents driving yadgar through the MCP can't see the viz. They can read memory + wiki state via tools but have no introspection into "what does the graph look like, what's selected, why is camera here." This closes that gap and unblocks remote agent-driven viz QA.

---

## Non-goals (explicit)

- **No public API surface.** All endpoints are debug-only, gated on `YADGAR_DEBUG_APIS_ENABLED=on`. They are not documented in the public README. They are documented in `docs/DEBUG_APIS.md`, marked unstable.
- **No persistence of debug events.** The event log is an in-memory ring buffer, byte-capped at 1MB. Restart wipes it.
- **No remote screenshot upload.** Screenshots return a base64-encoded PNG in the response body. The caller decides what to do with it.
- **No console capture before page load.** The `window.console` proxy installs at DOMContentLoaded. Anything before that uses the native console and is invisible to the capture panel.
- **No log persistence in the browser panel.** Refresh wipes the captured console. Use the daemon log panel for persistent records.
- **No SSE fallback negotiation.** Use SSE if the deployment supports it; otherwise long-poll. Detection is a one-time GET to `/api/logs/_capabilities`.

---

## Current state (verified from code, 2026-05-31; paths corrected 2026-06-12)

| Asset | Path | Status |
|---|---|---|
| Debug API gate | `YADGAR_DEBUG_APIS_ENABLED` | Introduced in **v5.50.2** (CHANGELOG). Bool, default `False`. Auth middleware gates `/api/control/*`. |
| Existing viz introspection | none | No endpoint exposes camera state, selection, or event log. |
| Existing screenshot mechanism | none | Browser-side `canvas.toDataURL()` is available but not exposed via HTTP. |
| Daemon logs | structured logs to stderr → systemd journal | Not exposed via HTTP. Operators read with `journalctl -fu yadgar`. |
| Browser console output | native, not captured | Lost on page navigation. |
| Debug tab | shipped v5.50.8 — JSON API inspector (introspection only) | NOT the same as v5.52.0 programmatic APIs. Dedicated nav tab exists. |
| Control tab | FULL as of v5.50.2 — actions/config/restart | Log panels go in Debug tab, not Control. |

---

## Scope — concrete file changes

### Backend

| File | Change |
|---|---|
| `yadgar/server/routes/debug_viz.py` | NEW. Four endpoint groups (12 endpoints total — see API surface below). All gated on `YADGAR_DEBUG_APIS_ENABLED=on` + bearer token. Add `/api/debug/viz/` + `/api/logs/` to auth middleware prefix list (currently only `/api/control/*` is gated). |
| `yadgar/server/routes/logs.py` | NEW. `GET /api/logs/_capabilities` (SSE vs poll detection). `GET /api/logs/stream` (SSE — `text/event-stream` of daemon log lines, level-tagged). `GET /api/logs/poll?since=<seq>` (fallback long-poll). |
| `yadgar/server/viz_state.py` | NEW. In-memory state mirror: `selected_node_id`, `camera` (position + look-at), `overlays` (per-name `{collapsed, position}`), `event_log` (ring buffer, byte-capped at 1MB). Updated via WebSocket from the browser tick loop OR via a polling endpoint the browser hits at 1Hz. |
| `yadgar/server/routes/control.py` | EXTEND (from v5.50.2). Mount debug viz routes under the same auth gate. No new endpoints in `control.py` itself. |
| `yadgar/config.py` | Register `YADGAR_DEBUG_LOG_BUFFER_SIZE` (int, default 1MB in bytes = 1048576), `YADGAR_DEBUG_VIZ_STATE_POLL_HZ` (int, default 1). Three-way per I25. |
| `config.yaml` | Add `debug.log_buffer_size=1048576`, `debug.viz_state_poll_hz=1`. |

### Frontend

| File | Change |
|---|---|
| `yadgar/static/control.js` | No log panels here — log panels moved to Debug tab (see below). |
| `yadgar/static/debug_tab.js` | EXTEND (from v5.50.8). Add two stacked log panels in the Debug tab. Top: daemon log via SSE/poll. Bottom: browser console proxy. Each panel: level filter row + pause/clear/copy-all controls + scrolling list of entries. |
| `yadgar/static/console_capture.js` | NEW. Installs `window.console` proxy at DOMContentLoaded. Captures `log`, `info`, `warn`, `error`, `debug`. Buffers up to 1000 entries in memory. Exposes a subscribe API for `debug_tab.js` to pull. XSS-safe rendering required (regression test in acceptance). |
| `yadgar/static/viz_state_publisher.js` | NEW. Hooked into `yadgar/static/home.js`. On graph events (node select, camera move, overlay toggle), POST a delta to `/api/debug/viz/_publish_state`. Throttled to 1Hz. Skipped entirely if `YADGAR_DEBUG_APIS_ENABLED=off`. |
| `yadgar/static/home.js` | EXTEND. Hook viz events to `viz_state_publisher.js`. |

### Tests

| File | Change |
|---|---|
| `yadgar/tests/test_debug_viz_apis.py` | NEW. All 12 endpoints return 403 when gate off, return 200 when on. Each endpoint's response shape validated. |
| `yadgar/tests/test_logs_sse.py` | NEW. SSE stream emits lines for log records emitted during the test. Poll fallback returns the same lines. |
| `yadgar/tests/test_console_capture.py` | NEW. Jsdom. `console.log("foo")` captured. Level filter works. Pause halts new entries. Clear empties the buffer. XSS regression: `console.log("<script>")` renders escaped. |

### Docs

| File | Change |
|---|---|
| `docs/DEBUG_APIS.md` | NEW. API reference for the 12 debug endpoints + 2 log endpoints. Marked **unstable** in the header. Includes curl examples. Not linked from README. |

---

## Debug viz API surface

All paths under `/api/debug/viz/`. All require `YADGAR_DEBUG_APIS_ENABLED=on` + bearer token. All return 403 with `{"error": "debug APIs disabled"}` when off.

Note: `/api/debug/viz/` and `/api/logs/` must be added to the auth middleware's prefix list alongside the existing `/api/control/*` gate.

### Category 1 — Select / inspect

| Method + path | Body | Returns |
|---|---|---|
| `POST /api/debug/viz/select` | `{"node_id": "mem:42"}` | `{"selected": "mem:42"}` |
| `GET /api/debug/viz/selected` | — | `{"selected": "mem:42" \| null}` |
| `POST /api/debug/viz/inspect` | `{"node_id": "mem:42"}` | `{"id": "mem:42", "kind": "memory", "content": "...", "heat": 0.73, "edges_in": 5, "edges_out": 3, "tags": [...]}` |

### Category 2 — Overlay simulation

| Method + path | Body | Returns |
|---|---|---|
| `POST /api/debug/viz/overlay/{name}/toggle` | — | `{"name": "heat", "collapsed": true}` |
| `POST /api/debug/viz/overlay/heat/filter` | `{"min": 0.5}` | `{"min": 0.5, "applied": true}` |
| `POST /api/debug/viz/overlay/edge/toggle` | `{"type": "semantic", "visible": false}` | `{"type": "semantic", "visible": false}` |

### Category 3 — Camera control

| Method + path | Body | Returns |
|---|---|---|
| `POST /api/debug/viz/camera` | `{"position": [x,y,z], "look_at": [x,y,z], "duration_ms": 800}` | `{"applied": true}` |
| `GET /api/debug/viz/camera` | — | `{"position": [x,y,z], "look_at": [x,y,z]}` |

### Category 4 — Event log + screenshot

**This is the high-value category.** The screenshot endpoint + event log let an agent verify what the viz shows without a human. Build Category 4 before Categories 1-3.

| Method + path | Body | Returns |
|---|---|---|
| `GET /api/debug/viz/events` | query `?since=<seq>&max=<n>` | `{"events": [{"seq": 1, "ts": ..., "kind": "node_select", "data": {...}}], "next_seq": 2}` |
| `POST /api/debug/viz/screenshot` | — | `{"png_base64": "iVBOR...", "width": 1920, "height": 1080}` |
| `POST /api/debug/viz/_publish_state` (internal) | `{...delta...}` | `{"accepted": true}` — used by the frontend publisher; not for external use |

---

## Open questions (must resolve during implementation)

1. **Where do the two log panels live?** Resolved by rescope: **Debug tab** (v5.50.8). Control tab is now full (actions/config/restart since v5.50.2); Debug tab is the right home for developer tooling.
2. **SSE vs WebSocket for daemon log stream.** SSE is simpler (HTTP/1.1 long-lived response, browser handles reconnect). WebSocket allows bidirectional but we don't need that. Lean SSE. Fallback to long-poll for environments behind buffering proxies (`/api/logs/poll?since=<seq>`).
3. **Screenshot mechanism — server-side or browser-side?** Server-side requires headless Chrome (heavy). Browser-side uses `canvas.toDataURL("image/png")` and POSTs back to a holding endpoint. Lean browser-side: `POST /api/debug/viz/screenshot` triggers a publish-screenshot WebSocket message to the browser, browser captures, POSTs base64 back to the holding endpoint, that endpoint completes the original POST. Trades latency for simplicity. Decide during Step 5.
4. **Event log scope.** Which events go into the ring buffer? First pass: `node_select`, `node_deselect`, `camera_move`, `overlay_toggle`, `heat_filter_set`, `edge_type_toggle`, `tab_switch`, `bookmark_added`, `error`. Anything else? Lean: add `render_tick` only if explicitly opted in via query (massive volume).
5. **Console capture — capture stack traces?** Yes for `console.error`. The proxy reads `new Error().stack` to get the call site. Pro: actionable. Con: increases per-entry size by ~500 bytes. Limit stack depth to 10 frames.
6. **Browser console persistence across page navigations.** Lean no — refresh wipes. If user needs persistence, they enable the daemon log panel instead (daemon is the durable side). Reconsider in v5.54+ if demand exists.

---

## Step plan (TDD per HARD RULE)

### Step 0 — Pre-flight (≤ 0.1 day)
- Confirm v5.50.2 shipped on master (gate `YADGAR_DEBUG_APIS_ENABLED` exists).
- Confirm v5.50.8 shipped (Debug tab exists — log panels will go there).
- Confirm auth middleware prefix list location (add `/api/debug/viz/` + `/api/logs/`).
- Snapshot `yadgar/static/debug_tab.js` size pre-extension for regression comparison.

### Step 1 — Debug viz API skeleton + auth gate (≤ 0.5 day)
- TDD: `test_debug_viz_apis.py` — all 12 endpoints return 403 when gate off (write first, all red).
- Stub all 12 endpoints in `yadgar/server/routes/debug_viz.py` returning `501 Not Implemented` (gate-passing) so the 403 tests go green.
- Add `/api/debug/viz/` + `/api/logs/` prefixes to auth middleware.
- Mount under `/api/debug/viz/` with the v5.50.2 auth gate.

### Step 2 — Viz state mirror (≤ 0.5 day)
- TDD: state publisher accepts a delta, GET returns the merged state.
- Implement `viz_state.py` in-memory mirror with byte-capped ring buffer (1MB).
- Implement `yadgar/static/viz_state_publisher.js` frontend hook. 1Hz throttle.
- Wire into `yadgar/static/home.js` event handlers.

### Step 3 — Select / inspect / overlay / camera endpoints (≤ 0.5 day)
- Implement Category 1-3 (8 endpoints) reading from + writing to `viz_state.py`.
- Camera POST publishes a delta to the frontend (via WebSocket or short-poll). Frontend `home.js` reacts and animates camera.
- TDD: each endpoint round-trips state.

### Step 4 — Event log + screenshot (≤ 0.5 day)
**Build this before Category 1-3 if prioritising the high-value slice (§Rescope).**
- Implement `GET /api/debug/viz/events` reading from the ring buffer.
- Implement `POST /api/debug/viz/screenshot` per Open Question 3 (likely browser-side via WebSocket round-trip).
- TDD: screenshot returns a non-empty PNG.

### Step 5 — Log SSE + poll (≤ 0.5 day)
- TDD: `test_logs_sse.py` — emit a log line, SSE consumer receives it within 1s.
- Implement `yadgar/server/routes/logs.py`: `/api/logs/stream` (SSE) and `/api/logs/poll` (long-poll fallback).
- Hook into the daemon's logging filter chain via a structured handler that pushes records into the ring buffer.

### Step 6 — Console capture frontend (≤ 0.25 day)
- TDD: `test_console_capture.py` — jsdom round-trip + XSS regression.
- Implement `yadgar/static/console_capture.js` proxy. Install at DOMContentLoaded.
- Capture `log`, `info`, `warn`, `error`, `debug`. `error` also captures stack trace.
- Ensure all captured strings are XSS-escaped before rendering.

### Step 7 — Two log panels UI in Debug tab (≤ 0.5 day)
- Extend `yadgar/static/debug_tab.js` (not `control.js`). Two stacked panels.
- Each panel: level filter row, pause toggle, clear button, copy-all button, scrolling list.
- Top panel: subscribes to `/api/logs/stream` or polls `/api/logs/poll`.
- Bottom panel: subscribes to `console_capture.js`.

### Step 8 — Acceptance + DEBUG_APIS.md + CHANGELOG (≤ 0.25 day)
- Run full pytest suite.
- Write `docs/DEBUG_APIS.md` with curl examples for all 14 endpoints (12 debug + 2 log).
- `CHANGELOG.md` v5.52.0 entry. `MIGRATION_NOTES.md` block on enabling the debug APIs.

---

## Effort estimate (calendar days)

| Step | Days |
|---|---:|
| Step 0 pre-flight | 0.1 |
| Step 1 API skeleton + auth | 0.5 |
| Step 2 viz state mirror | 0.5 |
| Step 3 select/inspect/overlay/camera | 0.5 |
| Step 4 event log + screenshot | 0.5 |
| Step 5 log SSE + poll | 0.5 |
| Step 6 console capture frontend | 0.25 |
| Step 7 two log panels UI (Debug tab) | 0.5 |
| Step 8 acceptance + docs | 0.25 |
| **Total** | **2.6 – 3 calendar days** |
| **High-value slice only (Steps 0, 4, 5, 6, 7, 8)** | **~2 calendar days** |

---

## Acceptance criteria

v5.52.0 ships when ALL are true:

- [ ] v5.50.2 confirmed shipped (`YADGAR_DEBUG_APIS_ENABLED` gate exists). v5.50.8 confirmed shipped (Debug tab exists).
- [ ] Auth middleware gates `/api/debug/viz/` and `/api/logs/` in addition to `/api/control/*`.
- [ ] All 12 debug viz endpoints under `/api/debug/viz/` exist, gated on `YADGAR_DEBUG_APIS_ENABLED=on` + bearer token. Return 403 with documented error body when off.
- [ ] `POST /api/debug/viz/select` round-trips with `GET /api/debug/viz/selected`.
- [ ] `POST /api/debug/viz/inspect` returns full node detail (kind, content, heat, edges, tags).
- [ ] `POST /api/debug/viz/overlay/{name}/toggle` flips collapsed state; observable in the browser.
- [ ] `POST /api/debug/viz/camera` animates the browser camera; `GET` returns current position.
- [ ] `GET /api/debug/viz/events` returns a ring buffer of ≥9 event kinds.
- [ ] `POST /api/debug/viz/screenshot` returns a base64 PNG of the current viewport (mechanism per Open Question 3).
- [ ] `/api/logs/stream` SSE delivers daemon log lines tagged with level + timestamp.
- [ ] `/api/logs/poll?since=<seq>` long-poll fallback returns the same lines for environments without SSE.
- [ ] Debug tab shows two stacked log panels (daemon top, browser console bottom).
- [ ] Each panel has level filter (DEBUG/INFO/WARN/ERROR), pause toggle, clear button, copy-all button.
- [ ] Browser console proxy captures `log/info/warn/error/debug`; `error` includes stack trace (≤10 frames).
- [ ] XSS regression: `console.log("<script>")` renders escaped in the panel.
- [ ] `docs/DEBUG_APIS.md` documents all 14 endpoints with curl examples. Marked unstable.
- [ ] `CHANGELOG.md` v5.52.0 entry. `MIGRATION_NOTES.md` debug-APIs block.
- [ ] Three new test files green: `test_debug_viz_apis.py`, `test_logs_sse.py`, `test_console_capture.py`.
- [ ] `python scripts/check_versions.py` exit 0.

---

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Debug APIs accidentally exposed to public when operator forgets to disable `YADGAR_DEBUG_APIS_ENABLED` | Default OFF. Logged on every startup whether the gate is on. README + MIGRATION_NOTES warn explicitly. Bearer token is still required even when gate is on. |
| State mirror drifts from actual browser state (browser crashes mid-publish) | Mirror is best-effort, not authoritative. GET endpoints return last-known. Document staleness in DEBUG_APIS.md. |
| SSE breaks behind a buffering reverse proxy (e.g. nginx default config) | Capability probe `GET /api/logs/_capabilities` reports SSE support. Frontend falls back to long-poll automatically. |
| Screenshot round-trip latency (browser-side mechanism) exceeds HTTP timeout | 30s timeout on the POST. Document the latency profile. If it consistently breaks, fall back to server-side headless Chrome in a future minor. |
| Console capture proxy double-logs (proxy + native both fire) | Proxy stores the call, then forwards to the native `console.<level>` via the saved reference. One log line in the actual browser console, one entry in the buffer. Test asserts no double-logging. |
| Event ring buffer overflows on a busy session, losing early events | Byte-capped at 1MB (not entry count). Bumpable via `YADGAR_DEBUG_LOG_BUFFER_SIZE`. Document the wrap-around behavior; callers must read with `since=<seq>` and check `next_seq` for gap detection. |
| Log SSE consumes a worker thread per connected client | Limit to N concurrent SSE clients (default 4). Beyond that, return 503. Configurable via `YADGAR_DEBUG_SSE_MAX_CLIENTS`. (Not in this plan's env knob list — add if it becomes a problem.) |
| Camera/select API builds complexity with no current consumer | Per §Rescope: defer Categories 1-3 until something concretely needs them. Build Category 4 (screenshot + event log) first. |

---

## TDD test list (write red, then implement green)

1. `test_debug_viz_apis.py::test_all_endpoints_403_when_disabled` — parametrized over all 12 endpoints + 2 log endpoints.
2. `test_debug_viz_apis.py::test_all_endpoints_200_when_enabled` — same parametrize, gate on.
3. `test_debug_viz_apis.py::test_select_then_selected_round_trip` — POST select, GET selected returns the same id.
4. `test_debug_viz_apis.py::test_inspect_returns_full_detail` — POST inspect on a seeded node returns kind, content, heat, edges_in, edges_out, tags.
5. `test_debug_viz_apis.py::test_overlay_toggle_flips_state` — toggle twice, state returns to original.
6. `test_debug_viz_apis.py::test_camera_get_after_post` — POST camera, GET returns the posted position.
7. `test_debug_viz_apis.py::test_events_since_seq` — emit 3 events, GET with `since=0` returns 3, `since=1` returns 2.
8. `test_debug_viz_apis.py::test_screenshot_returns_png` — POST screenshot returns base64 with PNG magic bytes (`\x89PNG\r\n\x1a\n`).
9. `test_logs_sse.py::test_sse_delivers_log_line` — emit a log record, SSE consumer receives it within 1s.
10. `test_logs_sse.py::test_poll_fallback_returns_same_lines` — same scenario via long-poll endpoint.
11. `test_logs_sse.py::test_capabilities_advertises_sse_support` — capability probe returns `{"sse": true, "poll": true}`.
12. `test_console_capture.py::test_console_log_captured` — `console.log("foo")` → buffer contains `{level:"log", message:"foo"}`.
13. `test_console_capture.py::test_console_error_has_stack` — `console.error("x")` → entry has non-empty `stack` array.
14. `test_console_capture.py::test_level_filter` — set filter to ERROR-only, only error entries surface.
15. `test_console_capture.py::test_pause_halts_new_entries` — pause, log, resume — entry count unchanged across pause.
16. `test_console_capture.py::test_clear_empties_buffer` — clear, buffer length 0.
17. `test_console_capture.py::test_native_console_still_fires_once` — assert no double-logging.
18. `test_console_capture.py::test_xss_escape_in_rendered_output` — `console.log("<script>alert(1)</script>")` renders escaped. **NEW per rescope audit.**

---

## Dependencies + blockers

- **v5.50.2 must ship first.** `YADGAR_DEBUG_APIS_ENABLED` gate introduced there (not v5.50.0).
- **v5.50.8 must ship first.** Debug tab nav target introduced there — log panels belong there.
- **No backend schema changes.** All state is in-memory.
- **No new external dependencies** — SSE is stdlib `text/event-stream`. No `sse-starlette` or similar required.

---

## Coordination notes for main thread

- Plan-only doc → direct to master per workflow rule (wiki slug `yadgar-workflow-plan-commits-direct-to-master`).
- Implementation feature branch: `feat/v5.52.0-viz-debug-apis-console-capture` after this plan commits.
- Related plans: `docs/PLAN_V5_50_0_*.md` (parent), `docs/PLAN_V5_51_0_*.md` (parallel viz train slot).
- This plan unblocks remote agent-driven viz QA — agents can now screenshot the viz and capture console errors without a human in the loop. Worth a separate wiki note post-ship.
- **Build priority:** screenshot + console capture (Steps 4-7) before camera/select API (Steps 2-3). The camera/select API has no current consumer and should defer until needed.
