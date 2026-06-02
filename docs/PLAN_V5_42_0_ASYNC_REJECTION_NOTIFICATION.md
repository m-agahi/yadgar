# PLAN — v5.42.0: Async drainer-rejection MCP notification

**Status:** drafted 2026-06-02. **Even-minor convention-correct slot** — even minors were reserved precisely for surprise/insert features that emerge between planned odd-minor work. Not a convention exception; using as designed. (Corrected 2026-06-02; earlier framing as "exception" was wrong.)

**Origin:** v5.41.5 moved similarity gate (and any future drainer-side rejections) OFF the MCP handler request path to restore I9. Side effect: `wait=False` callers lose sync rejection signal. v5.41.5 documented 3 migration options (`wait=True` sync, fire-and-forget, pre-flight `wiki_check_duplicate`). User flagged that `wait=True` materially increases write latency (handler <1ms → ~100-200ms under load), defeating the I9 win for safety-conscious callers.

**v5.42.0 closes the loop:** drainer emits MCP notification on rejection; Claude Code routes it back to the agent's next-turn context. Best-of-both — handler fast AND caller hears about rejections.

**Effort estimate:** 1.5-2 calendar days.

**Branch:** `feat/v5.42.0-async-rejection-notification` off master.

---

## 1. Problem

v5.41.5 trade-off:
- `wait=False` handler: <1ms (I9 restored), BUT rejections invisible to caller until next yadgar interaction
- `wait=True` handler: ~100-200ms (drainer round-trip + gate + storage), restores sync rejection contract but undoes I9

Either option leaves a meaningful caller cohort under-served:
- Bulk-write callers (skill-installers, batch wiki imports) want async/fast
- Safety-conscious callers (interactive agents creating canonical pages) want rejection feedback
- Currently must choose latency-vs-feedback

## 2. Goal

Add a third path: handler fast + async rejection notification back to client. Caller sees rejection in next agent turn without paying drainer round-trip per call.

## 3. Mechanism

**MCP server-initiated notification** (JSON-RPC `notifications/` method, standards-aligned).

```json
{
  "jsonrpc": "2.0",
  "method": "notifications/yadgar/wiki_rejected",
  "params": {
    "job_id": "abc123",
    "slug": "yadgar-roadmap-future-improvements",
    "reason": "duplicate_detected",
    "candidates": [{"slug": "...", "score": 0.94, ...}],
    "session_id": "<original-caller-session>"
  }
}
```

**Transport:** streamable-http already supports server-initiated frames (SSE-style). Reuse existing yadgar HTTP MCP transport.

**Client routing:** Claude Code hook intercepts the notification and injects the content into the agent's next-turn context. Exact hook depends on Claude Code 2026 schema — pre-flight verification required (Phase 0).

## 4. Pre-flight (Phase 0 — MANDATORY)

Verify Claude Code 2026 supports the routing path via `claude-code-guide` agent:

1. Does a `NotificationReceived` hook event exist? (or similar — `OnNotification`, `MessageReceived`, etc.)
2. If yes: documented schema for `params` field?
3. If no: what's the closest workaround?
   - **Workaround A:** server-side queue; piggyback notifications into next response to that session (yadgar transport injects `_pending_notifications` field into next tool-result envelope)
   - **Workaround B:** Claude Code agent SDK Notification subscriber — register handler at SDK level (Python SDK only)
   - **Workaround C:** ship server-emit side now; wait for Claude Code to add hook; document as planned-incomplete

Output: `docs/V5_42_0_PREFLIGHT_REPORT.md` with verification + recommended path. Commit as Phase 0 deliverable BEFORE writing implementation.

## 5. Scope (assumes Phase 0 confirms feasibility)

### Drainer side

- Extend drainer's similarity-gate stage (added in v5.41.5) to emit notification on rejection
- New emit function: `emit_mcp_notification(method, params, session_id)` — routes via the streamable-http transport's notification channel
- Per-job session_id tracking: when MCP handler enqueues, capture session_id of caller; drainer reads it from job metadata
- Reuses existing `wait_for_job` infrastructure from v5.41.2 (which already tracks job_id ↔ session)

### Transport side

- Transport buffers notifications keyed by session_id while no active stream
- On next request from that session: deliver buffered notifications BEFORE the response
- OR if active stream: deliver immediately
- Per-session buffer cap (e.g. 100 notifications) to prevent unbounded growth

### Client side (depends on Phase 0)

Most likely path: PostToolUse-equivalent hook on incoming notification frames. Hook content:
- Parse notification params
- Format human-readable line: "Wiki write rejected: '{slug}' duplicate of '{existing_slug}' (score {score}). Job {job_id}. Use force=True to override."
- Inject into next-turn context (mechanism depends on hook surface)

### Scope expansion (DP-E)

Should this cover ONLY `wiki_add` rejections OR all async write failures?

Options:
- Just `wiki_add` similarity rejections (narrow, v5.42 scope)
- All wiki writes (incl. `wiki_update` similarity if gate ever applies)
- All async writes (memorize, block_create, etc.) — generic notification framework

Lean: narrow to `wiki_add` first; document generic notification framework as v5.42.x or v5.50+ follow-up if other callers ask for it.

## 6. Decision points (resolve before impl)

- **DP-A** notification scope: `wiki_add` only / all wiki writes / all async writes? Lean: `wiki_add` only.
- **DP-B** client routing path (depends on Phase 0): hook event / piggyback / SDK subscriber / wait-for-hook? Lean: pick best per Phase 0.
- **DP-C** server-side buffer cap per session: 10 / 100 / 1000? Lean: 100 with CRITICAL log on overflow.
- **DP-D** notification persistence across reconnects: persist to disk OR memory-only? Lean: memory-only for v5.42; revisit if drops are observed.
- **DP-E** handler missing fallback: log + Prometheus only / error queue read at SessionStart / both? Lean: both (defense in depth — Prometheus for ops + queue replay for missed-notification recovery).

## 7. Acceptance criteria

1. Phase 0 verification report committed.
2. Drainer emits `notifications/yadgar/wiki_rejected` on similarity gate rejection.
3. Transport correctly tags + routes notification to the original caller's session.
4. Per-session buffer with cap enforced.
5. Client-side mechanism (per Phase 0 outcome) surfaces notification in next agent turn.
6. E2E test: `wiki_add(wait=False)` against existing duplicate → drainer rejects → next agent message contains rejection notice.
7. `wait=False` handler latency remains ≤5ms p50 (no regression from notification machinery).
8. Prometheus counters: `yadgar_notifications_emitted_total{method}` + `yadgar_notifications_buffered_dropped_total` (overflow).
9. Version bumped 5.41.5 → 5.42.0.
10. CHANGELOG + MIGRATION_NOTES updated; document new opt-in pattern.

## 8. Non-goals

- No retroactive notification for events that occurred BEFORE caller's session started.
- No exact-once delivery guarantee (best-effort; overflow + reconnect drops are acceptable with telemetry).
- No new MCP tool — entirely server-push.
- No replacement of `wait=True` semantics — still available for callers that prefer sync block.
- No generic notification framework for ALL yadgar events — scoped to wiki_add rejections in v5.42.

## 9. Risks

- **Phase 0 reveals no hook surface in Claude Code:** ship server-emit side; document client-side as TBD. Slot v5.42.x follow-up when hook lands. Caller gains nothing in agent context until then but ops gains Prometheus visibility.
- **Notification ordering:** multiple in-flight writes may produce out-of-order notifications. Mitigation: include job_id + timestamp; caller responsible for ordering if it matters.
- **Session-id leak across multi-session deployments:** transport must NOT broadcast notifications to other sessions. Test explicitly.
- **Buffer overflow under load:** CRITICAL log + counter; caller missed notifications recoverable via job-status endpoint OR error queue.
- **Notification storms:** many duplicates in one batch → many notifications → agent context bloat. Mitigation: client-side dedup (group by slug); document as agent-side concern.

## 10. Dependencies

- v5.41.2 wait_for_job infrastructure (✓ shipped)
- v5.41.5 drainer similarity gate placement (✓ shipped — gate emit point already exists)
- Streamable-http transport notification frame support (verify in Phase 0)
- Claude Code 2026 client-side hook OR SDK subscriber (verify in Phase 0)

## 11. Phases

0. **Phase 0 pre-flight** — verify Claude Code hook surface; output `docs/V5_42_0_PREFLIGHT_REPORT.md`. → COMMIT `docs(v5.42.0): pre-flight Claude Code notification routing verification`

1. **Drainer-emit + session tracking** — extend similarity-gate stage to emit; add session_id to job metadata; reuse wait_for_job tracking. → COMMIT `feat(drainer): emit notifications/yadgar/wiki_rejected on similarity gate fire`

2. **Transport routing + buffer** — per-session notification buffer; cap enforcement; deliver-on-next-response OR active-stream paths. → COMMIT `feat(transport): MCP server-initiated notification routing with per-session buffer`

3. **Client-side hook (per Phase 0)** — implement chosen path (hook event / piggyback / SDK subscriber). → COMMIT `feat(client): inject async rejection notifications into next agent turn`

4. **Tests** — unit (drainer emit, buffer cap), integration (E2E rejection flow), regression (wait=False latency stays ≤5ms p50, wait=True still works). → COMMIT `test(v5.42.0): async rejection notification e2e + regression`

5. **Docs + version bump** — CHANGELOG + MIGRATION_NOTES + README. Update v5.41.5 migration options to highlight v5.42.0 as preferred async path. Version 5.41.5 → 5.42.0. → COMMIT `chore: bump version 5.41.5 → 5.42.0 + async rejection docs`

## 12. References

- v5.41.2 plan `wait_for_job` — `yadgar/queue/drainer.py`
- v5.41.5 plan — similarity gate drainer placement
- MCP spec — JSON-RPC notification methods
- Anchor 491682 — Claude Code 2026 hook event schemas (verify via claude-code-guide for `NotificationReceived` equivalent)
- Streamable-http transport — `yadgar/server/http.py` (server-initiated frame support)

## 13. Coordination

Single agent dispatch after Phase 0 completes (or split: Phase 0 standalone → dispatch impl agent after user reviews verification report). Sonnet for impl. NO worktree isolation.

After ship: update v5.41.5 MIGRATION_NOTES Migration options section to add Option 4: "v5.42.0+ async notification pattern — `wait=False` + drainer notification routes rejection to next agent turn. Best for safety-conscious callers that can't afford `wait=True` latency."

## 14. Open questions (for opus reviewer + user)

1. Phase 0 outcome dependency: should v5.42.0 hold until Claude Code 2026 hook surface confirmed? OR ship server-emit independently and accept client-side TBD?
2. Even-minor slot: convention-correct per design intent (even minors reserved for surprise inserts between planned odd-minor work). No question to resolve.
3. Session-id propagation through queue: needs schema field on job metadata. Migration concern for existing in-flight jobs at upgrade time?
4. DP-A scope (wiki_add vs all wiki writes vs all async writes): reviewer's call on minimum-viable.
5. Telemetry detail: per-method counter sufficient OR per-session label too?
