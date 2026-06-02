# PLAN — v5.41.5: MCP handler I9 budget fix (~48ms → ≤5ms)

**Status:** drafted 2026-06-02. Hotfix patch. Real I9 violation surfaced by v5.41.2 perf test.

**Origin:** v5.41.2 perf attempt 2026-06-02 measured `wiki_add` MCP handler p50 = **~48ms** at `wait=False`. I9 budget = ≤5ms p50. **9.6x over.** Pre-existing violation, not introduced by v5.41.2 — but surfaced by the new test.

**Why now:** I9 is load-bearing — multiple handlers cluster on the request path. If `wiki_add` is 48ms, others likely similar. Fixing one teaches the pattern for the rest. Compose with v5.41.3 perf test (the regression guard).

**Effort estimate:** 1-2 days. Investigation phase is the unknown.

**Branch:** `fix/v5.41.5-handler-i9` off master.

---

## Problem

`wiki_add(...)` MCP handler returns in ~48ms p50 on `wait=False` path. I9 says ≤5ms. The work currently in the handler path that COULD be there (v5.39 similarity gate, secret-gate, branch resolution, queue enqueue) versus what SHOULD be there (just validate + enqueue) is unknown — needs profiling.

## Goal

Either (a) fix handler to ≤5ms p50 OR (b) make a deliberate, documented I9 budget exception for the wiki path with operator awareness.

## 1. Phase 0 — INVESTIGATION (mandatory before fix design)

Profile `wiki_add(...)` MCP handler. Break down the 48ms by sub-step. Likely suspects (in order of estimated cost):

1. **v5.39 similarity gate embed call.** Computes title + content embeddings → KNN over wiki_page table. Embed RPC alone is p50 2ms but JSON serialization + KNN + ranking could add 20-40ms.
2. **Secret-gate regex scan** (I26). Scans content against all-on patterns. Large content + many patterns = slow.
3. **Branch resolution** + directory_context detection.
4. **SurrealDB session setup** if cold per-request.
5. **OpenTelemetry span creation overhead** (multiple nested spans).
6. **Queue file write** (`Path.write_text` with JSON dump).

Output: `docs/V5_41_5_PROFILING_REPORT.md` with per-substep p50/p99 timings. Commit as Phase 0 deliverable BEFORE writing fix code.

## 2. Likely fix shapes (decide after Phase 0)

Depending on what dominates:

### Option A — similarity gate is the cost: move OFF request path

- Make similarity check run in DRAINER, not MCP handler.
- Handler enqueues immediately. Drainer rejects + emits `wiki_add_rejected_total{reason="duplicate"}` counter + writes rejection to error queue.
- Caller checks rejection via `wait=True` (already supported) OR polls error queue.
- v5.39 contract changes: sync `{stored: False, reason: "duplicate", candidates: [...]}` → async rejection notification.
- BREAKING CHANGE — needs MIGRATION_NOTES + v5.39 callers (just the MCP tool itself + any tests) updated.

### Option B — similarity gate stays in request path: tighten the embed path

- Cache embeddings (LRU on content-hash). Hit rate depends on agent usage but should be material for retry patterns.
- Switch to in-process embed (if HuggingFace transformer loaded in core process) — saves the embed RPC roundtrip.
- Lower `WIKI_SIM_TOP_K` default (5 → 2) to shrink KNN cost.
- Estimate: 48ms → maybe 20ms. Still over budget but closer.

### Option C — secret-gate is the cost: pre-compile patterns

- Move regex compilation to module load (one-time) instead of per-call.
- Use `re.Pattern.search` not `re.findall` where possible.
- Estimate: small. Probably <5ms savings.

### Option D — accept the budget violation; relax I9 per-handler

- Document `wiki_add` as exception to I9 with rationale.
- Add per-handler I9 budget config — e.g., `I9_BUDGET_OVERRIDES_MS = {"wiki_add": 50}`.
- v5.41.3 perf test reads from this map.
- Honest engineering: tells future contributors "this path is intentionally over budget."
- BAD — sets precedent that I9 is squishy.

### Option E — combination

- Phase 0 likely shows multiple costs. Mix A + C + maybe B-light.

## 3. Decision points

- **DP-A:** if Phase 0 shows similarity gate dominates (>30ms): move to drainer (Option A) OR cache + tighten (Option B)?
  - Lean: A — keeps handler thin per I1.
- **DP-B:** if A: how does drainer-side rejection surface to caller? Sync polling vs error queue vs `wait=True`-only?
  - Lean: `wait=True` returns rejection synchronously (already enqueued + drained inline). `wait=False` returns `{queued: true, similarity_check: "deferred"}` with caller responsible for checking.
- **DP-C:** breaking v5.39 contract — bump v5.39 → v5.41.5 in tool's MIGRATION_NOTES; document the new async-rejection path. Acceptable?
  - Lean: yes; v5.39 is 2 days old. No external consumers yet.
- **DP-D:** what's the I9 budget after fix? 5ms (original) OR a stretched-but-honest target (e.g. 10ms / 15ms)?
  - Lean: 5ms — the original budget exists for reasons (hooks-fast-profile latency budget cascades from it).
- **DP-E:** does the fix apply to OTHER MCP write tools? `memorize`, `block_create`, `wiki_update`, `wiki_append_section`? OR scope to `wiki_add` only?
  - Lean: profile + fix `wiki_add` first as exemplar; document pattern; slot follow-up for other tools if profile shows similar overruns.

## 4. Acceptance criteria

1. Phase 0 profiling report committed (`docs/V5_41_5_PROFILING_REPORT.md`) showing per-substep timings.
2. DPs A-E resolved (with user, in the plan or in commits).
3. Handler `wiki_add` p50 ≤5ms in v5.41.3 test (which currently fails RED on baseline).
4. If Option A: similarity gate moved to drainer; drainer rejection mechanism shipped.
5. If Option D: per-handler I9 override config registered three-way (I25); decision documented.
6. Existing v5.39 similarity gate tests still pass (with adjusted contract if Option A).
7. All v5.41.0 / v5.41.1 / v5.41.2 tests still pass.
8. Version bumped 5.41.4 → 5.41.5.
9. CHANGELOG entry documents the fix shape + breaking change (if any).
10. MIGRATION_NOTES: if v5.39 contract changes, document caller migration.

## 5. Non-goals

- Don't fix OTHER MCP write tools' I9 budget in this patch (separate slot).
- Don't change v5.41.2 wait flag semantics.
- Don't change v5.41.0 versioning semantics.
- Don't change v5.41.1 transactional atomicity.
- No retroactive timing data; only forward-looking improvement.

## 6. Risks

- Phase 0 profiling reveals the cost is structural (e.g., SurrealDB connection setup) — fix may require deeper changes than 1-2d.
- Option A breaks v5.39 contract — agent ecosystem unprepared.
- Option B (caching) introduces LRU eviction edge cases.
- Option D (budget exception) opens precedent for "I9 doesn't really apply" drift.

## 7. Phases

0. **Profiling.** Add OpenTelemetry spans per substep. Run 100 calls. Generate report. → COMMIT `docs(v5.41.5): MCP handler profiling report (wiki_add 48ms p50)`
1. **Resolve DPs** with user; commit decisions in plan file revisions.
2. **Implement fix.** Per chosen option(s). Tests + I25 + invariant-conformance. → COMMIT (1-2 commits depending on option)
3. **Verify v5.41.3 perf test passes** (or document new budget). → COMMIT `test(v5.41.5): MCP handler p50 ≤5ms verified`
4. **Version bump + docs.** → COMMIT `chore: bump version 5.41.4 → 5.41.5 + docs`

## 8. References

- v5.41.2 fix commit `7f1513d` — original 48ms measurement
- v5.41.3 plan — perf test (will fail RED until v5.41.5 fixes the handler)
- v5.39 similarity gate (`yadgar/server/tools/wiki.py::wiki_add`) — likely cost source
- README v5 latency anchors: "Embedding RPC: p50 2ms, p95 50ms" — sanity-check Phase 0 numbers
- I9 invariant — `docs/ARCHITECTURE_INVARIANTS.md`
- I1 + I2 invariants — request path thin, drainer single catch-up

## 9. Coordination

Ships LAST in v5.41.x train. Sequential implementation per user direction:
1. v5.41.2 (wait flag) merged
2. v5.41.3 (perf test + I9 correction) shipped
3. v5.41.4 (roadmap signal + convention) shipped
4. v5.41.5 (THIS plan — handler I9 fix) shipped
5. **User deploys v5.41.5 only.** Intermediate patches accumulate locally without nix-apply.
