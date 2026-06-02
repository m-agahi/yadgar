# PLAN — v5.42.0: Async rejection tracking via DLQ + Stop hook signal

**Status:** REWRITTEN 2026-06-02 evening (3rd revision). DPs resolved. READY for impl.

**Slot:** v5.42.0 — even-minor convention-correct slot (even minors reserved for surprise-insert features between planned odd-minor work).

**Origin:** v5.41.5 moved similarity gate to drainer (restored I9, handler 28.89ms → ≤5ms). `wait=False` callers lost sync rejection signal. v5.41.5 documented `wait=True` as alternative — but bench (2026-06-02) measured `wait=True` p50 = 228ms, p99 = 607ms, worst-cell 542ms. Real UX problem for safety-conscious callers.

**Revision history:**
- v1 (2026-06-02 afternoon): MCP server-initiated JSON-RPC notification. **Blocked** by opus reviewer — yadgar transport is `stateless_http=True` (no server-push substrate) + Claude Code 2026 has no `NotificationReceived` hook.
- v2 (2026-06-02 evening, hypothetical): server-side per-caller pending queue + piggyback `_pending_rejections` field on next tool-result. Required new infrastructure.
- **v3 (2026-06-02 evening, THIS): reuse existing DLQ + new Stop hook signal.** Almost zero new infra. User-suggested.

**Effort estimate:** 0.5-1 calendar day.

**Branch:** `feat/v5.42.0-rejection-dlq` off master.

---

## 1. Problem (post-bench, post-v5.41.5)

| Scenario | Pre-v5.41.5 | v5.41.5 wait=False | v5.41.5 wait=True | Target |
|---|---|---|---|---|
| Handler p50 | 27ms (I9 violation) | 0.5ms ✓ | 228ms (block) | ≤5ms |
| Rejection feedback | sync candidate list | NONE — lost | sync (with block) | async after-the-fact OK |

v5.41.5 forced caller to choose:
- Fast (`wait=False`) → no rejection visibility ever
- Visibility (`wait=True`) → 228ms block per call

Need a third path: fast handler + eventual rejection visibility without per-call block.

## 2. Goal

Drainer rejections (similarity gate, future gates) routed to existing DLQ infrastructure with a clear `failure_reason` taxonomy. Stop hook surfaces pending rejections to the agent at natural checkpoints. Caller can also poll explicitly via existing `dlq_inspect()`.

## 3. Mechanism — reuse existing DLQ

**Existing infrastructure** (shipped v4.5+):
- DLQ table — disk-backed via SurrealKV, survives restart
- `dlq_inspect()` MCP tool — list entries
- `dlq_requeue()` MCP tool — retry from DLQ
- Drainer push-to-DLQ on permanent failure already in place

**v5.42.0 adds:**

### 3.1 Failure-reason taxonomy on DLQ entries

Existing entries: `failure_reason: "permanent_error"` (current default).
New values:
- `failure_reason: "duplicate_detected"` — similarity gate rejection
- (Future) `failure_reason: "policy_rejected"` — any other gate

Each entry carries `failure_metadata` JSON:
```json
{
  "candidates": [{"slug": "...", "score": 0.94, ...}],
  "rejection_threshold_used": 0.80,
  "caller_context": {"directory": "/home/max/git/yadgar", ...}
}
```

### 3.2 Drainer pushes rejection to DLQ

`yadgar/file_queue/dlq.py` similarity-gate code path (already exists post-v5.41.5):
- On rejection → instead of archive-and-emit-metric, push to DLQ with `failure_reason="duplicate_detected"`
- Existing metric `yadgar_wiki_add_rejected_total{reason}` continues firing (ops visibility)

### 3.3 Stop hook signal

`yadgar/server/tools/project.py::project_brief(mode="signals")`:
- New signal: `pending_rejections_count: int` — count of DLQ entries with `failure_reason` ∈ {"duplicate_detected", future "policy_rejected"} filtered by caller directory if available
- New `recommended_action: "review_rejections"` when count > 0:
  ```json
  {
    "action": "review_rejections",
    "reason": "{count} write rejection(s) pending review",
    "suggested_call": "dlq_inspect(filter='rejections')"
  }
  ```

### 3.4 `dlq_inspect` filter param

Extend existing `dlq_inspect(limit=20)` MCP tool:
- New optional param: `filter: str | None = None` — values: `"all"` (default, current behavior), `"rejections"` (failure_reason in rejection taxonomy), `"failures"` (permanent_error only)
- Returns same shape; just narrows the result set

### 3.5 `dlq_requeue` blocks rejection entries

`dlq_requeue(entry_id)`:
- If entry's `failure_reason` ∈ rejection taxonomy → return error: `{"requeued": false, "error": "rejection entry — use wiki_add(force=True, ...) to override gate, or wiki_delete the existing duplicate, then retry"}`
- Keeps requeue semantics for permanent_error entries unchanged.

### 3.6 New MCP tool (optional): `dlq_dismiss(entry_id)`

Removes entry from DLQ without retry. Useful for "I acknowledged this rejection, drop it." Power-gated.

Alternative: extend `dlq_requeue` with `dismiss=True` flag. Lean: separate tool — clearer intent.

## 4. Decision points (resolved per user direction)

- **DP-A** scope: wiki_add similarity rejections only (narrow). Future taxonomy values added as new gates ship.
- **DP-B** delivery mechanism: DLQ (disk-backed, persistent) + Stop hook signal (auto-surface) + explicit `dlq_inspect()` (synchronous poll).
- **DP-C** rejection-entry semantics: distinct `failure_reason` taxonomy, blocked from auto-requeue.
- **DP-D** persistence: DLQ is disk-backed (existing) — survives restart for free.
- **DP-E** caller-identity correlation: include `caller_context.directory` in `failure_metadata`. Stop hook filters by current directory. Cross-session rejections still findable via `dlq_inspect(filter='rejections')`.

## 5. Acceptance criteria

1. New `failure_reason` taxonomy: `"duplicate_detected"` added, code paths updated.
2. Drainer similarity-gate rejection routes to DLQ instead of archive (existing metric still fires).
3. `dlq_inspect(filter="rejections")` works (filter param added).
4. `dlq_requeue` blocks rejection entries with helpful error message.
5. `dlq_dismiss(entry_id)` MCP tool ships (power-gated, secret-gated per I26).
6. `project_brief(mode="signals")` returns `pending_rejections_count`.
7. `recommended_action: "review_rejections"` fires when count > 0.
8. Stop hook surfaces signal in agent context at next checkpoint.
9. 12 tests covering: drainer push, taxonomy, filter, requeue block, dismiss, signal, recommended action, restart persistence, cross-directory isolation, metric continuation, secret-gate, edge cases.
10. CHANGELOG + MIGRATION_NOTES updated. v5.41.5 migration options doc references v5.42 as recommended async path.
11. Version bumped 5.41.5 → 5.42.0.

## 6. Non-goals

- No replacement of `wait=True` semantics — still available for sync-block callers.
- No real-time push to agent (Stop hook latency acceptable; explicit polling via `dlq_inspect` for sync-enough cases).
- No automatic rejection resolution (user/agent decides: force, delete existing, dismiss).
- No retroactive DLQ entries for past pre-v5.42.0 rejections (forward-only).
- No cross-write-tool scope (wiki_add only; memorize / block_create / etc. follow if/when they grow gates).

## 7. Risks

- **Semantic stretching of DLQ:** historically "permanent error." Adding "rejection" widens. Mitigation: clear `failure_reason` taxonomy; tooling filters by reason; documentation.
- **Stop hook delivery latency:** signal surfaces at ~12-24h checkpoint windows or session boundaries. For interactive bulk writes, rejections accumulate before being seen. Mitigation: explicit `dlq_inspect()` for sync-enough; OR caller flips to `wait=True` if synchronous matters.
- **DLQ growth under rejection storms:** many duplicate writes → DLQ bloats. Existing DLQ row-ceiling check (v4.5) catches this. Add Prometheus gauge `yadgar_dlq_rejection_count` for ops visibility.
- **Cross-directory rejection visibility:** Stop hook filters by directory; rejections from other directories invisible. Mitigation: `dlq_inspect(filter='rejections', directory=None)` lists all.
- **`dlq_requeue` user surprise on blocked rejection:** error message must clearly explain the alternative (`force=True` or delete existing).

## 8. Dependencies

- v5.41.5 drainer-side similarity gate (✓ shipped — emission point exists)
- v4.5 DLQ infrastructure (✓ shipped, anchor [116496])
- v5.41.4 `project_brief(mode="signals")` extensible signal pattern (✓ shipped — same pattern as `roadmap_update_lag`)

No new transport / hook / MCP-protocol work. Purely composing existing surfaces.

## 9. Phases (4 commits)

1. **Failure-reason taxonomy + drainer rerouting.** Extend DLQ entry schema with `failure_reason` enum + `failure_metadata` JSON. Drainer similarity-gate code routes to DLQ with `failure_reason="duplicate_detected"` instead of archive. Existing metric continues firing. Tests for taxonomy + drainer push. → COMMIT `feat(dlq): add failure_reason taxonomy + route similarity rejections to DLQ`

2. **`dlq_inspect` filter + `dlq_requeue` block + `dlq_dismiss` tool.** Filter param on inspect. Block requeue on rejection entries (helpful error). New dismiss tool (power-gated, secret-gated). Tests. → COMMIT `feat(dlq): inspect filter + requeue block on rejections + dismiss tool`

3. **Stop hook signal + recommended action.** `pending_rejections_count` signal in `project_brief(mode="signals")`. `review_rejections` recommended action. Filtered by current directory. Tests. → COMMIT `feat(project_brief): pending_rejections_count signal + review_rejections action`

4. **Version bump + docs.** 5.41.5 → 5.42.0. CHANGELOG entry. MIGRATION_NOTES: update v5.41.5 migration options to add Option 4 (DLQ-based async). README docs DLQ rejection pattern. → COMMIT `chore: bump version 5.41.5 → 5.42.0 + DLQ rejection tracking docs`

## 10. References

- v5.41.5 bench: `docs/V5_42_LATENCY_BENCHMARK_REPORT.md` — concrete numbers (wait=True p50=228ms etc.)
- v5.41.5 plan + drainer code path: `yadgar/file_queue/dlq.py` (existing similarity-gate emit point)
- v4.5 DLQ infrastructure: `yadgar/file_queue/__init__.py` (DLQ implementation), anchor [116496]
- v5.41.4 signal pattern: `yadgar/server/tools/project.py::project_brief` (roadmap_update_lag as template)
- Existing MCP tools: `dlq_inspect`, `dlq_requeue` — extending, not replacing
- Opus reviewer reports (2026-06-02) — blockers documented + DLQ alternative validated
- User suggestion 2026-06-02 evening — DLQ reuse insight (this revision)

## 11. Coordination

Single agent dispatch. Sonnet. NO worktree isolation. Main thread parks on master.

After ship:
- v5.41.5 MIGRATION_NOTES gets Option 4 added: "DLQ-based async (v5.42.0+): use `wait=False`, check `dlq_inspect(filter='rejections')` when needed, OR rely on Stop hook signal at session boundaries."
- Roadmap wiki Pipeline section updated to mark v5.42.0 shipped.

## 12. Migration path for v5.39 contract change carriers

Callers that previously relied on `wiki_add(wait=False)` returning sync rejection now have FOUR options:

1. `wait=True` — sync (slow: 228ms p50)
2. `wiki_check_duplicate` pre-flight (unchanged from v5.39)
3. `force=True` / `replace_slug=...` to bypass gate explicitly
4. **NEW: trust Stop hook signal + `dlq_inspect(filter="rejections")` for retrospective review** ← v5.42.0

## 13. Open questions

1. Should rejections also fire as Prometheus gauge per-directory? Or just total count? Lean: total count only (cardinality limit).
2. Should `dlq_dismiss` require confirmation (typed entry_id)? Lean: no — power-gating is the safety; agents calling power tools accept responsibility.
3. TTL on rejection entries — should they auto-purge after N days? Lean: yes, compose with v5.43 memory_archive retention pattern (separate `DLQ_REJECTION_RETENTION_DAYS` default 30d). Add as follow-up note, not blocker for v5.42.0.
