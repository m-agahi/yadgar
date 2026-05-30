# Decisions Log

**Purpose:** persistent record of ALL decisions — from audits, plan design, session-level open questions, and architectural code-level deferrals. Future agents (human or LLM) MUST consult this file before recommending changes, to avoid re-litigating already-decided questions and to surface revisit triggers.

**Format:** append-only chronological log. Each session or audit produces one section. Never edit prior entries; if a decision is reversed, add a NEW entry in the next applicable section that supersedes the old one (with `**Supersedes:**` link).

**Storage:** this file is canonical. Mirrored to wiki page `yadgar-audit-decisions-log` for searchable cross-session access. All "we said we'd do this later" notes live HERE — not in individual plan files (those rot).

---

## Protocol (how to use this file)

### When running an audit (you = audit agent or human)

1. **Read this file BEFORE recommending changes.** If a recommendation appears here with `Decision: KEEP-AS-IS` / `REJECT` / `DEFER`, do NOT re-recommend it unless its `Revisit triggers` have fired. Instead, write a one-line "previously decided, no new evidence" note in your audit output.
2. If a previously-rejected recommendation NOW has new evidence that triggers revisit, frame it as "RECONSIDER" not "NEW RECOMMENDATION". Link to the prior entry.
3. If your audit produces new recommendations (not previously seen), they're fair game — propose freely.

### When writing a plan, merging a feature, or deferring any "we'll do this later" item

1. Do NOT leave deferrals in plan files only — those rot and become invisible. For every significant deferral, extract it here under the appropriate dated section.
2. Add a one-line pointer in the plan file back to this file. For example: "See `docs/DECISIONS.md` — 2026-05-30 Plan-derived deferrals."
3. Required fields per DEFER / OPEN-QUESTION entry:
   - **Item** (short label)
   - **Source** (file:line or plan name)
   - **Decision** (from categories below)
   - **Reason** (why not now)
   - **Revisit triggers** (when to re-evaluate — REQUIRED for DEFER)
   - **Supersedes** (link if reversing prior decision — optional)

### When acting on an audit (you = main thread synthesizing)

1. For every recommendation in the audit, add an entry here. Even if the decision is "do nothing" — that IS a decision and needs the trail.
2. Commit to master per workflow rule (docs-only direct, set 2026-05-30).

---

## Categories

- **ADOPT:** will implement; assigned version slot. Plan file should exist in `docs/PLAN_V*.md`.
- **DEFER:** valid; not now. Revisit triggers REQUIRED.
- **REJECT:** disagree (strongest evidence required).
- **KEEP-AS-IS:** code already does this OR change rejected due to current evidence.
- **DONE-ALREADY:** audit missed prior implementation (link to commits/plans).
- **PLANNED:** in roadmap, no audit involvement — tracked here for consolidation.
- **OPEN-QUESTION:** raised but no decision yet. Must have an owner or expected resolution path.

---

## 2026-05-30 — Competitor Audit (mem0 / chroma / pinecone / zep / letta / postgres / DW)

**Audit doc:** `docs/competitor-audit-2026-05-30.md` (commit `635781e`)
**Scan doc:** `docs/competitor-audit-scan-2026-05-30.md`

### Adopt items (decisions pending — being planned by parallel agents)

| Item | Status |
|---|---|
| 1. Formal benchmarking (LongMemEval / LoCoMo) | Plan dispatched — `docs/PLAN_V5_13_0_*` |
| 2. Write-time conflict resolution | Agent dispatch pending |
| 3. Bi-temporal edges on all relationships | Agent dispatch pending |
| 4. In-context memory blocks (Letta) | Agent dispatch pending |
| 5. JavaScript / TypeScript SDK | Agent dispatch pending |
| 6. DuckDB analytics export | Agent dispatch pending |

### Refactor items

#### R1. Decouple consolidation from sleep cycle
- **Recommendation:** Separate consolidation cycle (deterministic, fast) and sleep cycle (LLM/CPU-heavy, slow) into distinct orchestrators with separate triggers.
- **Decision:** PARTIAL-ADOPT (limited scope only)
- **What was adopted:** `consolidate_now(mode='light'|'full')` param + 6h gate respect (in-flight as v5.10.4). Stops at param-level switch; no full structural separation.
- **What was NOT adopted:** full split into separate orchestrator classes (`ConsolidationOrchestrator` + `SleepCycleOrchestrator`). Audit recommended this; user decided current scope is enough.
- **Reason:** v5.10.4 mode param solves the immediate bug (13-min surprise + design inversion). Full structural separation is bigger blast radius without clear additional value. Preserve as audit-recorded future option.
- **Evidence:** `docs/PLAN_V5_10_4_CONSOLIDATE_NOW_HEAVYWEIGHT.md`; v5.10.4 in progress on branch `feat/v5.10.4-consolidate-now-mode-hook-schema`.
- **Revisit triggers:** sleep cycle grows enough phases that mode param becomes unwieldy; or new use cases require running sleep-cycle phases independently of consolidation; or LLM curator tier (v6) needs different scheduling model.

#### R2. Modularize 8-stage retrieval pipeline for pluggability
- **Recommendation:** Make each stage of recall() pipeline a registered plugin to enable A/B testing.
- **Decision:** ADOPT
- **Scope:** full plugin architecture. Each stage = `RetrievalStage` interface with `name`, `apply(state)`, `enabled` flag. Pipeline = list of stages. Per-call profiles (fast/full/debug) + per-stage metrics.
- **Reason:** A/B testing of individual stages currently impossible without code surgery. Pays off once Adopt item 1 (benchmarks) lands — enables data-driven pipeline tuning.
- **Evidence:** `docs/competitor-audit-2026-05-30.md` Refactor section R2. Current pipeline coupled in single `recall()` function.
- **Revisit triggers:** none expected — forward commitment. If implementation hits unexpected friction, reassess.
- **Version slot:** v5.14.x (after benchmarks land in adopt #1 plan).

#### R3. Replace file-based write queue with DB-native pub/sub
- **Recommendation:** Replace `file_queue/` with SurrealDB `LIVE SELECT` or Postgres LISTEN/NOTIFY.
- **Decision:** REJECT (accept eventual consistency everywhere instead)
- **What was rejected:** the migration itself. File queue stays. No `flush_only()` MCP primitive added either.
- **Reason:** SurrealDB LIVE SELECT is experimental; pgvector migration is multi-version refactor. File queue works. Callers must design around eventual consistency.
- **Evidence:** `docs/PLAN_V5_20_0_ROADMAP_FRESHNESS.md` documents the constraint; user explicitly chose this option.
- **Revisit triggers:** SurrealDB LIVE SELECT exits experimental; or yadgar suffers multiple production incidents traced to file-queue state; or migration to Postgres+pgvector becomes a separate priority.

### Ditch items

#### D1. MTREE corruption auto-repair
- **Recommendation:** Demote auto-rebuild to probe-only-LOUD-log; stop masking upstream SurrealDB bug.
- **Decision:** KEEP-AS-IS
- **Reason:** zero corruption events in production journal over 30 days. Production uses HNSW (since migration_001), not MTREE. Probe is one fast KNN query per nightly cycle — negligible cost. Auto-rebuild path never fires in current production.
- **Evidence:** `journalctl ... grep "MTREE index corruption detected" → 0 events`; `yadgar/storage/migrations.py:31` `_migration_001_hnsw_indexes`.
- **Revisit triggers:** any HNSW corruption event logged; SurrealDB upstream issue tracker opens HNSW corruption bug; switch to different vector backend (e.g. pgvector); probe becomes hot in profiles.

#### D2. NLI diversity stage as always-on
- **Recommendation:** Make NLI diversity (HEAVY_RERANK_ENABLED) opt-in rather than default-on.
- **Decision:** DEFER
- **Reason:** no benchmark data on NLI vs no-NLI recall accuracy. Tied to two prerequisites: Adopt-1 (benchmarks) and Refactor-2 (recall plugin arch — makes stages independently togglable).
- **Evidence:** `HEAVY_RERANK_ENABLED` env knob exists; cross-encoder model `cross-encoder/nli-deberta-v3-small` loaded eagerly when enabled.
- **Revisit triggers:** Adopt-1 benchmarks produce baseline numbers; Refactor-2 plugin arch ships; A/B run shows NLI contributes less than 5 percentage points accuracy gain (then flip default) OR more than 5pp gain (then keep default and close revisit).

#### D3. PC algorithm causal discovery
- **Recommendation:** Validate that causal discovery improves recall accuracy. If not, retire or gate.
- **Decision:** DEFER
- **Reason:** same posture as D2 — need benchmark data first. Unique-moat feature; removing without measurement also removes architectural distinction.
- **Evidence:** `yadgar/causal_discovery/` (5 files: pc.py, meek.py, independence.py, dag_io.py, __init__.py). No recall A/B data exists.
- **Revisit triggers:** Adopt-1 benchmarks produce causal-on vs causal-off accuracy numbers; Refactor-2 plugin arch ships; CPU bursts traced to PC algorithm phase; or PC algorithm completion duration more than 30s on typical state.

### Hold items (audit identified as unique moats — recorded for future agents)

- **H1** Branch-aware retrieval — no competitor has this. Keep and deepen.
- **H2** Wiki and memory pairing — Yadgar's structured knowledge base distinct from pure memory.
- **H3** Nightly multi-phase consolidation pipeline — most sophisticated batch in audit.
- **H4** Surprise-gated writes — prevents duplicates pre-write, unique to Yadgar.
- **H5** 32 MCP tool surface — far ahead of competitors (mem0 ~4, Zep 0).

---

## 2026-05-30 — Plan-derived deferrals (consolidated)

Items extracted from "What does NOT ship" / "Non-goals" / "Out of scope" sections in active plan files. These represent real design decisions that would otherwise rot in individual plan files.

### From `docs/PLAN_V5_10_4_CONSOLIDATE_NOW_HEAVYWEIGHT.md`

**PD-1. Full structural separation of ConsolidationOrchestrator and SleepCycleOrchestrator**
- **Decision:** DEFER (tracked under R1 above)
- **Revisit triggers:** same as R1 — mode param becomes unwieldy OR v6 LLM curator tier needs different scheduling.

**PD-2. Nightly cron PR-1 wiring of `_maybe_sleep_cycle()`**
- **Source:** `docs/PLAN_V5_10_4_CONSOLIDATE_NOW_HEAVYWEIGHT.md` section Open Questions
- **Decision:** OPEN-QUESTION
- **Background:** commit `bac9540` said `_maybe_sleep_cycle` is "preserved for PR-1 to wire". Current nightly cycle calls `force_consolidate()` only — no sleep cycle. Post-v5.10.4, sleep cycle no longer runs via `consolidate_now` default path either. Sleep cycle currently never runs unless `consolidate_now(mode="full")` is called.
- **Resolution path:** Decide in v5.10.9 plan or at next nightly cycle review. If sleep cycle is supposed to run nightly, add explicit wiring to `yadgar/scripts/nightly_cycle.py`. If not, delete `_maybe_sleep_cycle()` as dead code.

### From `docs/PLAN_V5_10_5_NIGHTLY_CYCLE_REMAINING.md`

**PD-3. Vacuum shared HTTP client refactor**
- **Decision:** DEFER
- **Reason:** surgical literal-replace (Bug 1 fix) is sufficient. Shared client is scope creep for the hotfix.
- **Revisit triggers:** vacuum gains a third call site; or multiple http-client-related bugs surface.

**PD-4. Strict exit code semantics (vacuum-fail from warn-only to fatal)**
- **Source:** `docs/PLAN_V5_10_5_NIGHTLY_CYCLE_REMAINING.md` "What does NOT ship"
- **Decision:** DEFER
- **Reason:** discussed in v5.7.0 PR-2 design; out of scope for hotfix. Currently nightly cycle exits 30 on vacuum fail (treated as non-fatal).
- **Revisit triggers:** operational pain from ambiguous exit codes; or next nightly cycle redesign.

### From `docs/PLAN_V5_10_6_SESSION_END_CAPTURE.md`

**PD-5. Cross-session "what did I work on this week" rollup**
- **Source:** `docs/PLAN_V5_10_6_SESSION_END_CAPTURE.md` section Out of scope, line 503
- **Decision:** PLANNED — v5.12 candidate
- **Reason:** requires multi-session data accumulated from session-end sentinels (v5.10.6 ships the data source first).
- **Revisit triggers:** v5.10.6 ships and sentinel data has 2 or more weeks of history.

**PD-6. CLI command `yadgar session-extract <transcript>` for manual extraction**
- **Source:** `docs/PLAN_V5_10_6_SESSION_END_CAPTURE.md` section Out of scope, line 504
- **Decision:** PLANNED — v5.12 candidate
- **Revisit triggers:** user requests manual extraction for historical sessions.

**PD-7. LLM synthesis in SessionEnd hook itself**
- **Source:** `docs/PLAN_V5_10_6_SESSION_END_CAPTURE.md` section Out of scope, line 501
- **Decision:** DEFER (indefinite)
- **Reason:** no model access from hook context; hook runs at exit-time when daemon may be down. Filesystem-first design was chosen instead (Q2 advisor recommendation).
- **Revisit triggers:** Claude Code SDK gains hook-context model access OR daemon-down-at-exit is resolved structurally.

### From `docs/PLAN_V5_10_7_VIZ_FIXES.md`

**PD-8. Viz performance for 5K+ node graphs**
- **Source:** `docs/PLAN_V5_10_7_VIZ_FIXES.md` section v5.X+ follow-up, line 158
- **Decision:** DEFER
- **Reason:** current 2K nodes renders smoothly. Not an observed bottleneck.
- **Revisit triggers:** graph grows to more than 3K nodes AND frame rate drops noticeably.

**PD-9. Viz dark mode toggle**
- **Source:** `docs/PLAN_V5_10_7_VIZ_FIXES.md` section v5.X+ follow-up, line 159
- **Decision:** DEFER
- **Reason:** cosmetic; no user request.
- **Revisit triggers:** user explicitly requests; or new viz major version (v6+).

**PD-10. Live anchor highlighting (red border for `_anchor`-tagged nodes)**
- **Source:** `docs/PLAN_V5_10_7_VIZ_FIXES.md` section v5.X+ follow-up, line 160
- **Decision:** DEFER
- **Reason:** UX enhancement; blocked on confirming ThreeJS per-node styling API.
- **Revisit triggers:** anchor cross-project feature (v5.11) ships — anchors become more prominent in the data model.

**PD-11. Viz "replay last session" mode via action_log**
- **Source:** `docs/PLAN_V5_10_7_VIZ_FIXES.md` section v5.X+ follow-up, line 161
- **Decision:** DEFER
- **Reason:** requires session-end capture (v5.10.6) as data source plus significant frontend work.
- **Revisit triggers:** session-end capture ships and there is clear user demand.

### From `docs/PLAN_V5_10_8_SECRET_GATE_CONTEXT_AWARENESS.md`

**PD-12. ML-based secret detection**
- **Source:** `docs/PLAN_V5_10_8_SECRET_GATE_CONTEXT_AWARENESS.md` section Non-goals, line 31
- **Decision:** REJECT (scoped to v5.10.8; may revisit for v6+)
- **Reason:** regex-based gate works. ML adds model dependency, false positive complexity, and latency. v5.10.2 tightened thresholds are sufficient.
- **Revisit triggers:** regex false-positive rate becomes operationally painful AND a well-tested pre-trained model is available with less than 10ms inference.

**PD-13. Allowlist YAML schema versioning strategy**
- **Source:** `docs/PLAN_V5_10_8_SECRET_GATE_CONTEXT_AWARENESS.md` section Open questions, line 59
- **Decision:** OPEN-QUESTION
- **Resolution path:** decide before v5.10.8 agent dispatch. Options: (a) version field in YAML root; (b) no versioning, break on malformed only; (c) semver major in filename.

**PD-14. Allowlist audit log rotation policy**
- **Source:** `docs/PLAN_V5_10_8_SECRET_GATE_CONTEXT_AWARENESS.md` section Open questions, line 60
- **Decision:** OPEN-QUESTION
- **Resolution path:** decide before v5.10.8 agent dispatch. Lean: date-based (one file per day), consistent with existing `~/.yadgar/*.jsonl` patterns.

**PD-15. Allowlist pattern overrides (threshold raise vs full-bypass)**
- **Source:** `docs/PLAN_V5_10_8_SECRET_GATE_CONTEXT_AWARENESS.md` section Open questions, line 61
- **Decision:** OPEN-QUESTION
- **Resolution path:** start with full-bypass only in v5.10.8; add threshold-raise as v5.10.9+ enhancement if operational need surfaces.

### From `docs/PLAN_V5_10_9_CPU_BURSTS_RESIDUAL.md`

**PD-16. F5 — embed_service saturation root fix (lazy-load rerankers OR cap batch OR cgroup bump)**
- **Source:** `docs/PLAN_V5_10_9_CPU_BURSTS_RESIDUAL.md`, lines 29 and 90
- **Decision:** OPEN-QUESTION (status unknown — needs verification)
- **Background:** Incident 501148 identified embed_service saturation at 32h+ uptime as root cause of CB-1 CPU burst. F5 was deferred at that time. As of v5.10.3 investigation, F5 ship status is unconfirmed.
- **Resolution path:** v5.10.9 acceptance criterion D5 — check CHANGELOG for v5.4.2+ for any embed_service lazy-load or cgroup changes. Open explicit issue if not found.

**PD-17. `DREAM_REPLAY_PAIRS` production default**
- **Source:** `docs/PLAN_V5_10_9_CPU_BURSTS_RESIDUAL.md`, line 127 and section Open Questions
- **Decision:** OPEN-QUESTION
- **Background:** if `DREAM_REPLAY_PAIRS` is high (more than 500), dream_replay is a significant CPU contributor in sleep cycle. Current production default unknown.
- **Resolution path:** check `~/.yadgar/config.yaml` config defaults before v5.10.9 agent dispatch.

**PD-18. Sleep cycle health metric ("last ran" timestamp)**
- **Source:** `docs/PLAN_V5_10_9_CPU_BURSTS_RESIDUAL.md` section Open Questions, line 186
- **Decision:** PLANNED
- **Background:** post-v5.10.4, sleep cycle no longer runs via default `consolidate_now`. If it also doesn't run via cron (PD-2 unresolved), there should be a metric to detect "sleep cycle has not run for more than 48h".
- **Version slot:** v5.10.9 or alongside PD-2 resolution.
- **Revisit triggers:** PD-2 (nightly cron wiring question) resolved.

### From `docs/PLAN_V5_10_X_MEMORIZE_ANCHOR_PARITY.md`

**PD-19. Call-count telemetry: `anchor()` vs `memorize(is_protected=True)` usage ratio**
- **Source:** `docs/PLAN_V5_10_X_MEMORIZE_ANCHOR_PARITY.md` section v5.X+ follow-up, line 160
- **Decision:** PLANNED
- **Background:** track `yadgar_memorize_is_protected_invocations_total` and `yadgar_anchor_invocations_total`. If `anchor()` drops to near-zero over months, candidate for implementation removal.
- **Version slot:** v5.X+ (after parity fix ships).
- **Revisit triggers:** parity fix ships for 3 or more months; then check ratio before deciding removal.

**PD-20. One-shot migration script for legacy `memorize(is_protected=True)` rows without `_anchor` tag**
- **Source:** `docs/PLAN_V5_10_X_MEMORIZE_ANCHOR_PARITY.md` section v5.X+ follow-up, line 161
- **Decision:** PLANNED — `scripts/migrate_legacy_protected_to_anchor.py`
- **Reason:** old rows lack `_anchor` tag injection; audit currently won't surface them.
- **Revisit triggers:** call-count telemetry (PD-19) reveals significant gap volume; or user manually discovers invisible anchors.

### From `docs/PLAN_V5_10_TEST_HARNESS_HARDENING.md`

**PD-21. Performance regression test suite**
- **Source:** `docs/PLAN_V5_10_TEST_HARNESS_HARDENING.md` "What does NOT ship", line 156
- **Decision:** DEFER
- **Reason:** scope creep for test hardening sprint; no current regression signal.
- **Revisit triggers:** a performance regression ships to master and is caught only by manual observation; or Adopt-1 benchmark plan (v5.13.0) creates infra reusable for this.

### From `docs/PLAN_V5_11_ANCHOR_CROSS_PROJECT.md`

**PD-22. Tier auto-upgrade (`conditional` to `semantic_immortal` after N clean audits)**
- **Source:** `docs/PLAN_V5_11_ANCHOR_CROSS_PROJECT.md` "What does NOT ship", line 92
- **Decision:** DEFER — v5.11+
- **Reason:** needs more real-world audit history before designing auto-upgrade thresholds.
- **Revisit triggers:** cross-project audit (v5.11) ships and runs for 30+ days; tier distribution data available.

**PD-23. `migration_grace=true` graceful expiry design hole**
- **Source:** `docs/PLAN_V5_11_ANCHOR_CROSS_PROJECT.md`, line 166
- **Decision:** PLANNED — v5.11.x candidates documented
- **Background:** v5.8 backfill set `migration_grace=true` on ALL pre-v5.8 `_anchor` rows. After 90d TTL, rows become invisible but persist as dead weight indefinitely, counting toward `anchor_count_project` signal threshold. This is a silent data leak. CRITICAL: first affected rows expire 2026-08-26 (anchored 2026-05-27 + 90d).
- **Candidates:** (a) `verify_grace_expired_anchor` recommendation type in `audit_anchors` — surfaces grace-protected rows past `valid_until` for user-gated review, auto-clears after N skipped audits; (b) auto-upgrade to `semantic_immortal` if heat above threshold at grace-expiry, else re-enter normal expiry. Lean (a).
- **Revisit triggers:** must ship v5.11.x grace handler before 2026-08-26.

**PD-24. Multi-language ticket tag patterns (Linear, GitHub Issues)**
- **Source:** `docs/PLAN_V5_11_ANCHOR_CROSS_PROJECT.md` "What does NOT ship", line 93
- **Decision:** PLANNED — start with Jira; expand on demand
- **Revisit triggers:** user actively uses Linear or GitHub Issues for task tracking alongside yadgar.

**PD-25. Anchor reorganization UI / web frontend (`yadgar-tui`)**
- **Source:** `docs/PLAN_V5_11_ANCHOR_CROSS_PROJECT.md` "What does NOT ship", line 94
- **Decision:** DEFER (permanent for v5.x)
- **Reason:** out of yadgar core scope. CLI and MCP surface is the primary interface.
- **Revisit triggers:** yadgar-tui becomes a real project with scope.

### From `docs/PLAN_V5_12_0_WIKI_BOOKMARKS.md`

**PD-26. Multi-user bookmarks**
- **Source:** `docs/PLAN_V5_12_0_WIKI_BOOKMARKS.md` section Non-goals, line 45
- **Decision:** DEFER — v6+ concern
- **Reason:** yadgar is single-user. Per-user bookmarks require auth model that does not exist.
- **Revisit triggers:** yadgar gains multi-user concept.

**PD-27. Playwright automated browser tests for viz/bookmark UI**
- **Source:** `docs/PLAN_V5_12_0_WIKI_BOOKMARKS.md` step 18, line 312
- **Decision:** DEFER
- **Reason:** big infra add for a cosmetic/UX feature. Manual smoke test acceptable per v5.10.7 viz plan precedent.
- **Revisit triggers:** recurring browser-regression bugs caught only by manual testing; or test suite standardizes on headless browser.

### From `docs/PLAN_V5_20_0_ROADMAP_FRESHNESS.md`

**PD-28. v5.20.0 roadmap freshness mechanism**
- **Decision:** DEFER — to v5.20.0
- **Reason:** fundamental design issue with yadgar's async wiki write queue: read-after-write race means splice operations corrupt wiki content. Requires `flush_only()` primitive or blocking write path first.
- **Revisit triggers:** yadgar gains `flush_only()` MCP primitive; OR SurrealDB blocking write path available; OR roadmap drift incident severe enough to justify accepting data loss risk.

### From `docs/PLAN_NIGHTLY_BACKUP_NIX_FIX.md`

**PD-29. Nightly backup Tier 2 permanent fix — pure-Python consolidation (Candidate 1)**
- **Source:** `docs/PLAN_NIGHTLY_BACKUP_NIX_FIX.md` line 167
- **Decision:** PLANNED — v5.12.0
- **Reason:** Tier 1 (nix-repo edit) applied as emergency fix. Permanent solution requires eliminating numpy dependency from nightly-cycle execution context.
- **Revisit triggers:** Tier 1 (nix fix) breaks OR numpy version conflict recurs in NixOS upgrade.

**PD-30. Nightly backup Tier 2 permanent fix — container-based nightly execution (Candidate 2)**
- **Source:** `docs/PLAN_NIGHTLY_BACKUP_NIX_FIX.md` line 168
- **Decision:** PLANNED — v5.12.1 (alternative to PD-29; only one will ship)
- **Revisit triggers:** same as PD-29.

### From `docs/PLAN_BACKEND_V5_4_CACHING.md`

**PD-31. N+1 `get_memory` hydration batching**
- **Source:** `docs/PLAN_BACKEND_V5_4_CACHING.md` section v5.4.1 follow-up, line 188
- **Decision:** PLANNED — backend v5.4.1
- **Background:** 51 sequential reads per recall replaces with single `WHERE id IN $ids` query. Approximately 1s win per recall.
- **Revisit triggers:** v5.4.0 cache hit-rate baseline established (target 30% or more CE, 50% or more embed). Ship after baseline confirmed.

**PD-32. BM25 / HNSW result caches**
- **Source:** `docs/PLAN_BACKEND_V5_4_CACHING.md` "What does NOT ship", line 100
- **Decision:** DEFER (indefinite)
- **Reason:** BM25 and HNSW stages already under 50ms. Write-invalidation cost exceeds cache benefit.
- **Revisit triggers:** BM25 or HNSW becomes observed bottleneck post-CE-cache baseline.

**PD-33. Full recall-pipeline cache**
- **Source:** `docs/PLAN_BACKEND_V5_4_CACHING.md` "What does NOT ship", line 101
- **Decision:** REJECT (for current architecture)
- **Reason:** recall results are freshness-sensitive. User memorizes and expects immediate visibility in next recall. TTL trade-off is unacceptable.
- **Revisit triggers:** use cases where stale recall is acceptable emerge; or pipeline gains a staleness-tolerance flag per query.

### From `docs/PLAN_V5_8_ANCHOR_HYGIENE.md`

**PD-34. `semantic_immortal` tier write gate (require `reason` argument)**
- **Source:** `docs/PLAN_V5_8_ANCHOR_HYGIENE.md` section Open / parked questions, line 185
- **Decision:** OPEN-QUESTION (from v5.8 design)
- **Background:** should `anchor(tier="semantic_immortal")` require an additional `reason` argument? Forces deliberate thought. Lean: yes.
- **Resolution path:** confirm whether this was implemented in v5.8. Check `yadgar/server/tools/memorize.py` or `anchor.py` for required `reason` field on `tier="semantic_immortal"`.

### From `docs/PLAN_V6.md`

**PD-35. LLM-in-the-loop curator (v6 LLM nightly curator via Ollama)**
- **Source:** `docs/PLAN_V6.md` lines 9, 52, 69
- **Decision:** DEFER — after v5.x train complete
- **Reason:** substrate (provenance_agent, bi-temporal edges, citation tracing, recall-modulated decay, agent prompts, hooks) ships in v5. v6 adds the LLM that uses it. Skeleton only pending v5.4 soak data.
- **Revisit triggers:** v5.x train stabilizes plus soak data lands; DeepSeek-R1 8B benchmark meets v6 task bar.

**PD-36. Depth saturation chunking strategy for v6 curator**
- **Source:** `docs/PLAN_V6.md` lines 46, 52
- **Decision:** OPEN-QUESTION (must resolve BEFORE v6 first nightly run)
- **Background:** SleepGate paper: 16.5% accuracy at depth-15 contradictions. Chunking plus per-cluster scope limit MUST land before first nightly run. Cluster by topic via community detection; curate cluster-by-cluster; never whole-store batch.
- **Resolution path:** design chunking strategy as part of v6 plan refinement post-soak. Do NOT implement v6 before v5.4 soak data arrives.

---

## 2026-05-30 — Open architectural questions

Questions raised during design reviews, plan drafting, or session investigations that do not yet have a decision. Each needs an owner or an expected resolution path.

| ID | Question | Raised in | Resolution path |
|---|---|---|---|
| OQ-1 | Should nightly cron PR-1 wire `_maybe_sleep_cycle()`? Post-v5.10.4, sleep cycle currently never runs. | `docs/PLAN_V5_10_4_CONSOLIDATE_NOW_HEAVYWEIGHT.md` section Open Questions | Decide in v5.10.9 plan or next nightly cycle review. |
| OQ-2 | What is `DREAM_REPLAY_PAIRS` set to in production? If more than 500, dream_replay is significant CPU contributor. | `docs/PLAN_V5_10_9_CPU_BURSTS_RESIDUAL.md` section Open Questions | Check `~/.yadgar/config.yaml` before v5.10.9 dispatch. |
| OQ-3 | Is F5 (embed_service lazy-load rerankers OR cap batch OR cgroup bump) shipped? | `docs/PLAN_V5_10_9_CPU_BURSTS_RESIDUAL.md`, line 29 | Check CHANGELOG for v5.4.2+ embed_service changes. |
| OQ-4 | `consolidate_now mode='full'` — should respect 6h gate or run unconditionally? | `docs/PLAN_V5_10_4_CONSOLIDATE_NOW_HEAVYWEIGHT.md` section Open Questions | Resolved in v5.10.4 — gate respected. Mark DONE. |
| OQ-5 | v5.12.0 wiki bookmarks page — refresh-on-focus, `[[slug]]` clickable, pre-seed defaults? | `yadgar-roadmap-future-improvements` section Open Questions | Decide before v5.12.0 agent dispatch. |
| OQ-6 | `flush_only()` MCP primitive — design now or wait for clear use case? | `docs/PLAN_V5_20_0_ROADMAP_FRESHNESS.md` (deferred for it) | Wait for v5.20.0 to become active; design as prerequisite. |
| OQ-7 | `reason` kwarg on `memorize()` — keyword-only? | `docs/PLAN_V5_10_X_MEMORIZE_ANCHOR_PARITY.md` section Open / parked questions | Lean: keyword-only. Confirm in v5.10.x implementation. |
| OQ-8 | Auto-prepend `_anchor` to tags vs reject without it in `memorize(is_protected=True)` | Same plan | Auto-prepend (advisor lean). Confirm in implementation. |
| OQ-9 | Allowlist YAML schema versioning strategy (v5.10.8) | `docs/PLAN_V5_10_8_SECRET_GATE_CONTEXT_AWARENESS.md` | Decide before dispatch. Lean: version field in YAML root. |
| OQ-10 | Allowlist audit log rotation: size-based vs date-based? | Same plan | Lean: date-based. Confirm before dispatch. |
| OQ-11 | Should `anchor(tier="semantic_immortal")` require a `reason` argument? | `docs/PLAN_V5_8_ANCHOR_HYGIENE.md` | Verify if shipped in v5.8. |
| OQ-12 | `migration_grace=true` row expiry — handler must ship before 2026-08-26 (earliest affected rows expire). | `docs/PLAN_V5_11_ANCHOR_CROSS_PROJECT.md`, line 166 | PD-23. Ship v5.11.x grace handler before that date. |

---

## 2026-05-30 — Code-level architectural TODOs

Code comments representing architectural decisions — not trivial cleanups. Approximately 35 TODO/FIXME occurrences examined and excluded (test fixture strings, CI timeout tuning comments, version/history references in plan headers).

| ID | File:Line | Comment (quoted) | Category | Suggested action |
|---|---|---|---|---|
| CT-1 | `yadgar/storage/client.py:411` | "roll-your-own JSON escaping via LET $k = json.dumps bypasses SurrealDB's native bind facility. Migrate all _q callers to POST {"sql": stmt, "vars": params}" | DEFER | Architectural debt. Migrate when SurrealDB bind facility confirmed stable and no escaping edge cases. Affects all `_q()` callers. |
| CT-2 | `yadgar/tests/test_sleep_compute.py:509` | "tighten back to 5s once CI runner is faster; 15s is the hard ceiling" | OPEN-QUESTION | Test infra concern. Monitor CI runner speed. Tighten when 5s consistently passes without flake. |

---

## Convention for future use

- **This file** lives at `docs/DECISIONS.md` on master (renamed from `docs/AUDIT_DECISIONS.md` on 2026-05-30).
- **Mirrored** to wiki page `yadgar-audit-decisions-log` (search-discoverable).
- **When dispatching an audit agent**, include in the prompt: "Read `docs/DECISIONS.md` first. Do not re-recommend items marked KEEP-AS-IS, REJECT, or DEFER unless their revisit triggers have fired."
- **When drafting any plan**, any "does not ship / out of scope / later" item of architectural significance must be extracted here before merge. Add a pointer in the plan file: "See `docs/DECISIONS.md` — [dated section]."
- **New audit/plan entries appended** at top under a new dated section.
- **Plan-only commits** go direct to master per yadgar workflow rule (set 2026-05-30).
