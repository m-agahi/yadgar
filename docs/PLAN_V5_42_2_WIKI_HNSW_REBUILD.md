# PLAN — v5.42.2: wiki_page HNSW index rebuild post-backfill

**Status:** drafted 2026-06-02 night. Critical hotfix — completes v5.42.1's incomplete fix.

**Origin:** v5.42.1 shipped migration 014 + `backfill_null_embeddings` + `_run_wiki_embedding_backfill` (lifecycle:204) to populate NULL embedding fields. Live re-verification 2026-06-02 evening shows similarity gate STILL non-functional. cavecrew-investigator root cause:

> SurrealDB HNSW indexes do NOT auto-update on row UPDATE statements outside the index insert path. Backfill issued `UPDATE wiki_page SET embedding = $emb` — wrote real embeddings to DB rows BUT did NOT add them to HNSW index. KNN query `WHERE embedding <|fetch_k,40|> $qv` only sees rows present in the HNSW index → returns 0 candidates for backfilled rows.

**Confirmed via:** parallel pattern exists for memory table — `rebuild_vector_indexes` in `yadgar/storage/vector.py:195` — but NO wiki equivalent ever shipped.

**Effort estimate:** 0.5 calendar day.

**Branch:** `fix/v5.42.2-wiki-hnsw-rebuild` off master.

---

## 1. Problem

| Layer | State |
|---|---|
| Migration 014 — runs at startup | ✓ logged, version recorded |
| Backfill — encodes + UPDATEs rows | ✓ 0 failures in counter |
| DB rows — embedding field populated | ✓ (presumed; no direct verify) |
| **HNSW index — backfilled rows registered** | ❌ MISSING — SurrealDB HNSW doesn't auto-update on UPDATE |
| KNN query — finds backfilled rows | ❌ 0 candidates (index empty for these rows) |
| Similarity gate — fires on real near-clones | ❌ never triggers |

Plus secondary: any UPDATE path on wiki_page that mutates embedding (post-migration writes, restores, etc.) hits the same index-staleness bug — needs systemic rebuild trigger.

## 2. Goal

1. Add `rebuild_wiki_embedding_index()` mirroring `rebuild_vector_indexes` (memory) pattern.
2. Call it after `_run_wiki_embedding_backfill` completes (one-time on startup).
3. Call it after batch UPDATEs that mutate embedding (drainer post-write hook).
4. E2E smoke verifies gate actually fires.

## 3. Scope

### 3.1 New storage helper

`yadgar/storage/wiki.py`:
- New function `rebuild_wiki_embedding_index()`:
  - SurrealQL: `REBUILD INDEX wiki_embedding_idx ON wiki_page`
  - Returns count of indexed rows + duration
  - Logged at INFO level (operators see the rebuild happened)
- Pattern: mirror memory equivalent at `yadgar/storage/vector.py:195`

### 3.2 Lifecycle integration

`yadgar/server/lifecycle.py`:
- After `_run_wiki_embedding_backfill` completes successfully → call `rebuild_wiki_embedding_index()`
- If backfill is a no-op (0 NULL rows) → still safe to call (idempotent, fast)
- Log progress: "rebuilding HNSW index for {N} wiki_page rows..."

### 3.3 Drainer post-write hook (defense-in-depth)

`yadgar/file_queue/dlq.py` or write-path equivalent:
- After bulk write of N rows with embedding → trigger rebuild
- Threshold: rebuild every M writes (e.g. M=50) OR after consolidation cycle
- Knob: `WIKI_HNSW_REBUILD_BATCH_THRESHOLD: int = 50` (I25 three-way)

Lean: defer the drainer hook to v5.42.3 if E2E passes after 3.1+3.2. Smaller surface; faster ship. Document as known-limitation: single-row UPDATEs (e.g. wiki_update via MCP) may need explicit rebuild call OR manual `yadgar admin rebuild-wiki-index` CLI.

### 3.4 New admin MCP tool (optional)

`mcp__yadgar__rebuild_wiki_embedding_index()`:
- Power-gated
- Triggers rebuild on demand
- Returns indexed-row count + duration
- Useful for ops + recovery if drift detected

## 4. Non-goals

- Not changing SurrealDB HNSW behavior (out of our scope).
- Not changing migration 014 semantics — keep backfill as-is, just add rebuild after.
- Not retroactively fixing past missed-gate writes (forward-only).
- No backwards-compat with v5.42.0/v5.42.1 broken state — those just had silent gate; v5.42.2 makes gate work.

## 5. Acceptance criteria (CRITICAL — implementer MUST verify all)

1. `rebuild_wiki_embedding_index()` storage helper ships, returns `{indexed_rows: N, duration_ms: M}`.
2. Lifecycle invokes it after `_run_wiki_embedding_backfill` completes — INFO log line in container output.
3. **E2E SMOKE TEST** (NEW or extended `test_v5_42_1_gate_verification_e2e.py`):
   - Create wiki page A via `wiki_add(content=X, wait=True)`
   - Call `wiki_check_duplicate(title="diff", content=X)` — assert **candidates >= 1** with cosine >= 0.95
   - Create near-clone page B via `wiki_add(content=X+minor_tweak, wait=False)`
   - Wait 5s for drainer
   - Call `dlq_inspect(filter="rejections")` — assert exactly 1 entry with B's slug
   - Call `dlq_dismiss(entry_id)` cleanup
   - Delete A
   - **Test MUST pass against live yadgar instance.** Run in CI integration mode (`pytest.mark.integration`).
4. Existing 72 v5.42.1 tests still pass.
5. Existing v5.39 calibration test still passes.
6. v5.41.5 transactional atomicity tests still pass.
7. New optional MCP tool `rebuild_wiki_embedding_index` (power-gated, secret-gated per I26) if implemented.
8. Version bumped 5.42.1 → 5.42.2.
9. CHANGELOG + MIGRATION_NOTES document the HNSW gap + rebuild fix.

## 6. Iterate-until-passing mandate (implementer instruction)

The agent dispatched MUST:
1. Write the E2E test FIRST (RED).
2. Implement fix.
3. Run E2E test.
4. If FAILS → diagnose → adjust → re-run. Repeat.
5. Same fix fails twice → STOP and report ("can't get gate to fire after 2 attempts; need human input").
6. Do NOT mark phase complete based on unit tests alone — E2E against live behavior is the gate.
7. Do NOT skip the cleanup step (smoke test must dismiss DLQ entry + delete test page) — otherwise pollutes prod DB.

## 7. No-shortcuts / no-breakage mandate

- Do NOT alter v5.41.5 transactional path (the `BEGIN; CREATE wiki_page; CREATE wiki_page_version; COMMIT` pattern).
- Do NOT alter v5.42.1 backfill semantics (keep backfill_null_embeddings as-is — just add rebuild AFTER it).
- Do NOT remove or weaken `WIKI_EMBED_FAILURE_BLOCKS_WRITE` knob.
- Do NOT modify HNSW index DDL in migration_001 (re-using existing index definition).
- Do NOT add LLM/heavy work to MCP handler (I1).

## 8. Risks

- **SurrealDB REBUILD INDEX semantics:** may lock the table during rebuild. For 2063 rows + HNSW, expect 1-10 seconds. Acceptable for startup but visible in latency if invoked online via MCP tool. Document.
- **REBUILD may not be the right SQL:** SurrealDB docs may use `RESTRUCTURE INDEX` or other. Implementer MUST verify the SurrealDB syntax via `claude-code-guide` OR via SurrealDB docs OR by reading existing memory `rebuild_vector_indexes` for the canonical syntax. Don't guess.
- **Race condition:** if migration runs concurrently with first user wiki_add, the new row may insert into a partially-rebuilt index. Mitigation: rebuild AFTER backfill, before lifecycle marks ready.

## 9. Phases (3 commits)

1. **RED E2E test** — `test_v5_42_2_gate_fires_e2e.py` (integration mark). Reproduces the smoke from §5.3. Currently fails. → COMMIT `test(wiki): RED E2E test — gate fires on real near-clone (currently broken)`

2. **HNSW rebuild helper + lifecycle integration.** New `rebuild_wiki_embedding_index()` in storage. Call from lifecycle after backfill. Verify E2E test now passes. Add unit tests for the helper. → COMMIT `feat(wiki): rebuild HNSW index after embedding backfill (post-v5.42.1 fix)`

3. **Optional MCP tool + version bump + docs.** `rebuild_wiki_embedding_index` MCP tool (power-gated). Version 5.42.1 → 5.42.2. CHANGELOG. MIGRATION_NOTES. → COMMIT `chore: bump version 5.42.1 → 5.42.2 + admin rebuild tool + HNSW gap docs`

## 10. Pre-deploy report (REQUIRED before nix-apply)

Implementer agent MUST produce, in its final report:

### Section A: v5.42.1 (previous fix) — what was actually done
- Files touched
- Tests added
- Why it didn't fix the live bug
- Backfill log/telemetry confirming rows were UPDATEd
- Counter values confirming embed succeeded
- Confirmation HNSW gap was the root miss

### Section B: v5.42.2 fix
- Files touched (delta from v5.42.1 only)
- Tests added (RED + passing E2E + unit tests for rebuild helper)
- Lifecycle order: migration → backfill → rebuild → ready
- Confirmation E2E smoke PASSES end-to-end (with concrete output)
- Confirmation no v5.41.x/v5.42.x tests regressed

### Section C: System safety
- v5.41.5 transactional path: unchanged ✓ / changed (explain)
- v5.42.1 backfill semantics: unchanged ✓ / changed (explain)
- HNSW DDL in migration_001: unchanged ✓ / changed (explain)
- I1/I9/I26 invariants: held ✓ / violation (explain)
- Test counts: before vs after delta
- Known-limitation list (e.g., single-row UPDATEs needing explicit rebuild deferred to v5.42.3)

## 11. References

- v5.42.1 plan + commits — context for backfill mechanism
- cavecrew-investigator report 2026-06-02 — root cause file:line refs
- `yadgar/storage/vector.py:195` — `rebuild_vector_indexes` (memory pattern to mirror)
- `yadgar/storage/wiki.py:464` — `search_wiki_vectors` KNN query (what we need to make work)
- `yadgar/server/lifecycle.py:204` — `_run_wiki_embedding_backfill` (extension point)
- `yadgar/storage/migrations.py:427` — `_migration_014_wiki_page_embedding_backfill` (status marker)
- SurrealDB HNSW docs — verify REBUILD INDEX syntax before shipping
