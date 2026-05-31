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

## 2026-05-31 — v5.26.0 Adopt-1 Benchmark Ship + D2/D3 RECONSIDER

**Source:** v5.26.0 ship — Phase 1 (retrieval) + Phase 2 (QA) LongMemEval pilot.
**Commit:** v5.26.0 release commit (see CHANGELOG).
**Plan:** `docs/PLAN_V5_26_0_BENCHMARK_QA_PUBLICATION.md`

### Adopt-1: LongMemEval benchmark — SHIPPED

Adopt item 1 ("Formal benchmarking (LongMemEval / LoCoMo)") is now SHIPPED as of v5.26.0.

- **Phase 1 gate:** PASS. MRR=0.935, Recall@10=0.964 (gate: mrr>0.1 AND r@10>0.3).
- **Phase 2 headline QA accuracy:** PENDING Phase 2 run completion (running at ship time).
  See `docs/BENCHMARK_RESULTS.md` for live numbers.
- **Dataset:** LongMemEval `s` variant, 96 stratified questions (16/type × 6 types).
- **Model:** `claude-haiku-4-5-20251001` (reader + judge).
- **Result files:** `benchmarks/results/longmemeval_v5.26.0_s_retrieval.json` (Phase 1),
  `benchmarks/results/longmemeval_v5.26.0_s_full.json` (Phase 2, pending).

### D2 — NLI diversity stage: RECONSIDER

D2 revisit trigger "Adopt-1 benchmarks produce baseline numbers" has fired.

- **Current status:** RECONSIDER (was DEFER)
- **Baseline with NLI ON:** see Phase 2 QA accuracy in `docs/BENCHMARK_RESULTS.md`.
- **NLI settings in benchmark:** `NLI_RERANKING_ENABLED=True` in `make_benchmark_settings()`.
- **Next action:** Run D2 A/B (NLI OFF) per `docs/PLAN_V5_25_X_D2_NLI_AB.md`.
  Decision rule: delta < 5pp → flip default OFF; >= 5pp → keep ON.
- **Note:** Refactor-2 (v5.31.0 plugin arch) NOT yet shipped — A/B doable via env var toggle.

### D3 — PC algorithm causal discovery: RECONSIDER (with caveat)

D3 revisit trigger "Adopt-1 benchmarks produce causal-on vs causal-off accuracy numbers" has
technically fired, but the v5.26.0 benchmark does NOT test causal discovery impact on retrieval.

**Why:** `make_benchmark_settings()` sets `WRRF_PPR_WEIGHT=0.0` and `WRRF_SPREADING_WEIGHT=0.0`
(graph signals disabled). The PC algorithm builds a causal DAG used only by graph signals.
The v5.26.0 baseline is implicitly "causal-off" for retrieval purposes.

- **Current status:** RECONSIDER (was DEFER) — with caveat (see above)
- **Next action:** Follow `docs/PLAN_V5_25_X_D3_PC_AB.md`.
  Primary question: does PC algorithm phase take > 30s in nightly cycle? Check production logs.
  Secondary: if D2 A/B run happens, consider enabling `WRRF_PPR_WEIGHT > 0` to get true causal-on data.
- **CPU burst watch:** D3 revisit trigger "CPU bursts traced to PC algorithm" has NOT fired.
  As of v5.25.3, no PC-algorithm-related CPU burst events in production journal.

---

## 2026-05-30 — Competitor Audit (mem0 / chroma / pinecone / zep / letta / postgres / DW)

**Audit doc:** `docs/competitor-audit-2026-05-30.md` (commit `635781e`)
**Scan doc:** `docs/competitor-audit-scan-2026-05-30.md`

### Adopt items (decisions pending — being planned by parallel agents)

| Item | Status |
|---|---|
| 1. Formal benchmarking (LongMemEval / LoCoMo) | SHIPPED v5.25.0 (Phase 1 infra); Phase 2 QA → v5.26.0 |
| 2. Write-time conflict resolution | SHIPPED v5.17.0 |
| 3. Bi-temporal edges on all relationships | Planned → v5.29.0 |
| 4. In-context memory blocks (Letta) | Planned → v5.33.0 |
| 5. JavaScript / TypeScript SDK | Planned → v5.35.0 |
| 6. DuckDB analytics export | Planned → v5.27.0 |

### Refactor items

#### R1. Decouple consolidation from sleep cycle
- **Recommendation:** Separate consolidation cycle (deterministic, fast) and sleep cycle (LLM/CPU-heavy, slow) into distinct orchestrators with separate triggers.
- **Decision:** PARTIAL-ADOPT (limited scope only)
- **What was adopted:** `consolidate_now(mode='light'|'full')` param + 6h gate respect (SHIPPED v5.10.4). Stops at param-level switch; no full structural separation.
- **What was NOT adopted:** full split into separate orchestrator classes (`ConsolidationOrchestrator` + `SleepCycleOrchestrator`). Audit recommended this; user decided current scope is enough.
- **Reason:** v5.10.4 mode param solves the immediate bug (13-min surprise + design inversion). Full structural separation is bigger blast radius without clear additional value. Preserve as audit-recorded future option.
- **Evidence:** `docs/PLAN_V5_10_4_CONSOLIDATE_NOW_HEAVYWEIGHT.md`; shipped 2026-05-30.
- **Revisit triggers:** sleep cycle grows enough phases that mode param becomes unwieldy; or new use cases require running sleep-cycle phases independently of consolidation; or LLM curator tier (v6) needs different scheduling model.

#### R2. Modularize 8-stage retrieval pipeline for pluggability
- **Recommendation:** Make each stage of recall() pipeline a registered plugin to enable A/B testing.
- **Decision:** ADOPT
- **Scope:** full plugin architecture. Each stage = `RetrievalStage` interface with `name`, `apply(state)`, `enabled` flag. Pipeline = list of stages. Per-call profiles (fast/full/debug) + per-stage metrics.
- **Reason:** A/B testing of individual stages currently impossible without code surgery. Pays off once Adopt item 1 (benchmarks) lands — enables data-driven pipeline tuning.
- **Evidence:** `docs/competitor-audit-2026-05-30.md` Refactor section R2. Current pipeline coupled in single `recall()` function.
- **Revisit triggers:** none expected — forward commitment. If implementation hits unexpected friction, reassess.
- **Version slot:** v5.31.0 (after benchmarks land in adopt #1 plan — `docs/PLAN_V5_25_0_BENCHMARK_PUBLICATION.md`).

#### R3. Replace file-based write queue with DB-native pub/sub
- **Recommendation:** Replace `file_queue/` with SurrealDB `LIVE SELECT` or Postgres LISTEN/NOTIFY.
- **Decision:** REJECT (accept eventual consistency everywhere instead)
- **What was rejected:** the migration itself. File queue stays. No `flush_only()` MCP primitive added either.
- **Reason:** SurrealDB LIVE SELECT is experimental; pgvector migration is multi-version refactor. File queue works. Callers must design around eventual consistency.
- **Evidence:** `docs/PLAN_V5_99_0_ROADMAP_FRESHNESS.md` documents the constraint; user explicitly chose this option.
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
- **Revisit triggers:** Adopt-1 benchmarks (v5.25.0) produce baseline numbers; Refactor-2 plugin arch (v5.31.0) ships; A/B run shows NLI contributes less than 5 percentage points accuracy gain (then flip default) OR more than 5pp gain (then keep default and close revisit).

#### D3. PC algorithm causal discovery
- **Recommendation:** Validate that causal discovery improves recall accuracy. If not, retire or gate.
- **Decision:** DEFER
- **Reason:** same posture as D2 — need benchmark data first. Unique-moat feature; removing without measurement also removes architectural distinction.
- **Evidence:** `yadgar/causal_discovery/` (5 files: pc.py, meek.py, independence.py, dag_io.py, __init__.py). No recall A/B data exists.
- **Revisit triggers:** Adopt-1 benchmarks (v5.25.0) produce causal-on vs causal-off accuracy numbers; Refactor-2 plugin arch (v5.31.0) ships; CPU bursts traced to PC algorithm phase; or PC algorithm completion duration more than 30s on typical state.

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
- **Revisit triggers:** anchor cross-project feature (v5.21.0) ships — anchors become more prominent in the data model.

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
- **Revisit triggers:** a performance regression ships to master and is caught only by manual observation; or Adopt-1 benchmark plan (v5.25.0) creates infra reusable for this.

### From `docs/PLAN_V5_21_0_ANCHOR_CROSS_PROJECT.md`

**PD-22. Tier auto-upgrade (`conditional` to `semantic_immortal` after N clean audits)**
- **Source:** `docs/PLAN_V5_21_0_ANCHOR_CROSS_PROJECT.md` "What does NOT ship"
- **Decision:** DEFER — v5.21+
- **Reason:** needs more real-world audit history before designing auto-upgrade thresholds.
- **Revisit triggers:** cross-project audit (v5.21.0) ships and runs for 30+ days; tier distribution data available.

**PD-23. `migration_grace=true` graceful expiry design hole**
- **Source:** `docs/PLAN_V5_21_0_ANCHOR_CROSS_PROJECT.md`
- **Decision:** PLANNED — v5.21.x candidates documented
- **Background:** v5.8 backfill set `migration_grace=true` on ALL pre-v5.8 `_anchor` rows. After 90d TTL, rows become invisible but persist as dead weight indefinitely, counting toward `anchor_count_project` signal threshold. This is a silent data leak. CRITICAL: first affected rows expire 2026-08-26 (anchored 2026-05-27 + 90d).
- **Candidates:** (a) `verify_grace_expired_anchor` recommendation type in `audit_anchors` — surfaces grace-protected rows past `valid_until` for user-gated review, auto-clears after N skipped audits; (b) auto-upgrade to `semantic_immortal` if heat above threshold at grace-expiry, else re-enter normal expiry. Lean (a).
- **Revisit triggers:** must ship v5.21.x grace handler before 2026-08-26.

**PD-24. Multi-language ticket tag patterns (Linear, GitHub Issues)**
- **Source:** `docs/PLAN_V5_21_0_ANCHOR_CROSS_PROJECT.md` "What does NOT ship"
- **Decision:** PLANNED — start with Jira; expand on demand
- **Revisit triggers:** user actively uses Linear or GitHub Issues for task tracking alongside yadgar.

**PD-25. Anchor reorganization UI / web frontend (`yadgar-tui`)**
- **Source:** `docs/PLAN_V5_21_0_ANCHOR_CROSS_PROJECT.md` "What does NOT ship"
- **Decision:** DEFER (permanent for v5.x)
- **Reason:** out of yadgar core scope. CLI and MCP surface is the primary interface.
- **Revisit triggers:** yadgar-tui becomes a real project with scope.

### From `docs/PLAN_V5_23_0_WIKI_BOOKMARKS.md`

**PD-26. Multi-user bookmarks**
- **Source:** `docs/PLAN_V5_23_0_WIKI_BOOKMARKS.md` section Non-goals
- **Decision:** DEFER — v6+ concern
- **Reason:** yadgar is single-user. Per-user bookmarks require auth model that does not exist.
- **Revisit triggers:** yadgar gains multi-user concept.

**PD-27. Playwright automated browser tests for viz/bookmark UI**
- **Source:** `docs/PLAN_V5_23_0_WIKI_BOOKMARKS.md`
- **Decision:** DEFER
- **Reason:** big infra add for a cosmetic/UX feature. Manual smoke test acceptable per v5.10.7 viz plan precedent.
- **Revisit triggers:** recurring browser-regression bugs caught only by manual testing; or test suite standardizes on headless browser.

### From `docs/PLAN_V5_99_0_ROADMAP_FRESHNESS.md`

**PD-28. v5.99.0 roadmap freshness mechanism**
- **Decision:** DEFER — to v5.99.0
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

## 2026-05-31 — Setup mechanism decision (v5.45 plan-derived)

**PD-37. Setup mechanism for non-NixOS installs**
- **Source:** `docs/PLAN_V5_45_0_SETUP_FOUNDATION.md` (v5.45.0 plan), `docs/PLAN_V5_46_0_DISTRIBUTION.md` (v5.46.0 plan), `docs/PLAN_V5_47_0_UPDATE_MECHANISM.md` (v5.47.0 plan)
- **Decision:** ADOPT — Compose-canonical + systemd opt-in + interactive installer + auto-detect runtime + auto-detect OS
- **Scope:** Three-ship train:
  - **v5.45.0** ships the foundation: portable Makefile (`make setup` / `make uninstall` / `make uninstall-purge`), interactive `yadgar install` CLI, container-runtime auto-detect (podman/docker), OS auto-detect (Linux/macOS/others), systemd opt-in path with new `yadgar.target`, macOS launchd plist path. Data preserved by default on uninstall; `--purge` for full wipe. Hooks delegated to existing MCP `install_hooks` tool. NixOS hosts refused with suggestion to use v5.46 nix flake.
  - **v5.46.0** ships the distribution: PyPI metadata polish, new Homebrew tap (separate `homebrew-yadgar` Codeberg repo), Nix flake at yadgar repo root (packages/apps/nixosModules/homeManagerModules outputs), Codeberg release automation via Forgejo Actions (sdist + container manifest + checksums + CycloneDX JSON SBOM + brew/nix bump PRs). Container source-of-truth stays at `docker.io/openfantasy/yadgar`; release manifest mirrors. Single-source-of-truth version bumper (`scripts/bump_version.py`).
  - **v5.47.0** ships the update mechanism: `yadgar update [--check | --install]` CLI subcommand (detects install method: pipx / brew / nix-flake / container / source), opt-in anonymous version-only auto-check on daemon start (`update.check_on_start: false` default OFF; no IP, no user-ID, no telemetry — strictly version probe), `/api/control/update` HTTP route (gated by `YADGAR_DEBUG_APIS_ENABLED=on` + bearer middleware). v5.50 viz Control-tab Update button wires to this API.
- **Reason:**
  - Compose is portable across Linux/macOS/Windows/WSL2 — single deployment model.
  - systemd opt-in path supports power users + matches NixOS-managed pattern without forcing it.
  - Interactive installer (no curl-pipe-sh) eliminates supply-chain attack surface.
  - Auto-detect runtime/OS removes per-distro tribal knowledge from documentation.
  - macOS launchd path bundles the same UX as Linux systemd — first-class macOS support.
  - Homebrew + Nix flake first-class install paths cover the long-tail user base.
  - Codeberg release automation removes manual asset-attachment toil; SBOM (CycloneDX JSON) satisfies enterprise security scanners.
  - Anonymous version-only update check respects privacy (opt-in, no telemetry) while letting users discover updates.
- **Alternatives considered + rejected:**
  - **Per-service systemd units only** — rejected; excludes macOS users entirely.
  - **Detect-OS hybrid without compose path** — rejected; loses portability across Linux distros (Alpine, RHEL, Ubuntu, NixOS each differ).
  - **Compose-only without systemd opt-in** — rejected; loses daemon supervision on power-user Linux installs; doesn't match NixOS pattern.
  - **curl-pipe-sh installer** — rejected; supply-chain attack surface unacceptable for a memory engine that handles user data.
  - **Phone-home telemetry with usage data** — rejected; privacy violation. Version-only opt-in probe is the maximum acceptable.
  - **SPDX SBOM** — deferred to v5.47+ variant; CycloneDX is v5.46 default (broader enterprise scanner support).
  - **Signed release artifacts (sigstore/cosign)** — deferred to v5.48+ candidate.
- **Lower-priority opens resolved:**
  - Data preservation: `make uninstall` preserves `~/.yadgar/` (DB + queue) by default; `make uninstall-purge` for full wipe.
  - Hooks delegation: Makefile delegates to existing `mcp__yadgar__install_hooks` MCP tool.
  - Versioning sync: bump script keeps pyproject + server.json + nix module + brew formula + Codeberg tag in sync; single source of truth.
  - Container source-of-truth: image stays at `docker.io/openfantasy/yadgar`; release manifest in Codeberg points to it (mirror reference, not duplicate hosting).
  - Phone-home: explicit anonymous version-only check (no IP / user-id telemetry); opt-in via `update.check_on_start: false` in config.yaml.
  - NixOS user migration: installer detects existing nix-managed install via `/etc/NIXOS` or `command -v nixos-version`; if detected, refuses to overwrite + suggests using nix flake derivation instead.
- **Open questions retained in plan docs (not blockers for PD-37):**
  - macOS launchd plist exact content + management commands (resolved during v5.45 Step 4 implementation).
  - Python 3.14 availability on macOS Homebrew core (resolved during v5.46 Step 0 implementation; fallback to 3.13 if needed).
  - Anonymous version-check payload exact wire shape — corporate firewalls + privacy auditors (resolved during v5.47 Step 0 implementation; documented in `docs/PRIVACY.md`).
- **Revisit triggers:** macOS launchd path proves unreliable in field; OR compose v3 spec deprecates a depended-on feature; OR user demand for FreeBSD / Windows-native paths; OR Codeberg releases API rate-limits the update-check probe; OR privacy posture must extend (e.g. SBOM transparency on update check); OR multi-host / multi-user yadgar deployment becomes a real use case (current scope: single-user).

---

## 2026-05-30 — Open architectural questions

Questions raised during design reviews, plan drafting, or session investigations that do not yet have a decision. Each needs an owner or an expected resolution path.

| ID | Question | Raised in | Resolution path |
|---|---|---|---|
| OQ-1 | Should nightly cron PR-1 wire `_maybe_sleep_cycle()`? Post-v5.10.4, sleep cycle currently never runs. | `docs/PLAN_V5_10_4_CONSOLIDATE_NOW_HEAVYWEIGHT.md` section Open Questions | Decide in v5.10.9 plan or next nightly cycle review. |
| OQ-2 | What is `DREAM_REPLAY_PAIRS` set to in production? If more than 500, dream_replay is significant CPU contributor. | `docs/PLAN_V5_10_9_CPU_BURSTS_RESIDUAL.md` section Open Questions | Check `~/.yadgar/config.yaml` before v5.10.9 dispatch. |
| OQ-3 | Is F5 (embed_service lazy-load rerankers OR cap batch OR cgroup bump) shipped? | `docs/PLAN_V5_10_9_CPU_BURSTS_RESIDUAL.md`, line 29 | Check CHANGELOG for v5.4.2+ embed_service changes. |
| OQ-4 | `consolidate_now mode='full'` — should respect 6h gate or run unconditionally? | `docs/PLAN_V5_10_4_CONSOLIDATE_NOW_HEAVYWEIGHT.md` section Open Questions | Resolved in v5.10.4 — gate respected. Mark DONE. |
| OQ-5 | v5.23.0 wiki bookmarks page — refresh-on-focus, `[[slug]]` clickable, pre-seed defaults? | `yadgar-roadmap-future-improvements` section Open Questions | Decide before v5.23.0 agent dispatch. |
| OQ-6 | `flush_only()` MCP primitive — design now or wait for clear use case? | `docs/PLAN_V5_99_0_ROADMAP_FRESHNESS.md` (deferred for it) | Wait for v5.99.0 to become active; design as prerequisite. |
| OQ-7 | `reason` kwarg on `memorize()` — keyword-only? | `docs/PLAN_V5_10_X_MEMORIZE_ANCHOR_PARITY.md` section Open / parked questions | Lean: keyword-only. Confirm in v5.10.x implementation. |
| OQ-8 | Auto-prepend `_anchor` to tags vs reject without it in `memorize(is_protected=True)` | Same plan | Auto-prepend (advisor lean). Confirm in implementation. |
| OQ-9 | Allowlist YAML schema versioning strategy (v5.10.8) | `docs/PLAN_V5_10_8_SECRET_GATE_CONTEXT_AWARENESS.md` | Decide before dispatch. Lean: version field in YAML root. |
| OQ-10 | Allowlist audit log rotation: size-based vs date-based? | Same plan | Lean: date-based. Confirm before dispatch. |
| OQ-11 | Should `anchor(tier="semantic_immortal")` require a `reason` argument? | `docs/PLAN_V5_8_ANCHOR_HYGIENE.md` | Verify if shipped in v5.8. |
| OQ-12 | `migration_grace=true` row expiry — handler must ship before 2026-08-26 (earliest affected rows expire). | `docs/PLAN_V5_21_0_ANCHOR_CROSS_PROJECT.md` | PD-23. Ship v5.21.x grace handler before that date. |

---

## 2026-05-30 — Code-level architectural TODOs

Code comments representing architectural decisions — not trivial cleanups. Approximately 35 TODO/FIXME occurrences examined and excluded (test fixture strings, CI timeout tuning comments, version/history references in plan headers).

| ID | File:Line | Comment (quoted) | Category | Suggested action |
|---|---|---|---|---|
| CT-1 | `yadgar/storage/client.py:411` | "roll-your-own JSON escaping via LET $k = json.dumps bypasses SurrealDB's native bind facility. Migrate all _q callers to POST {"sql": stmt, "vars": params}" | DEFER | Architectural debt. Migrate when SurrealDB bind facility confirmed stable and no escaping edge cases. Affects all `_q()` callers. |
| CT-2 | `yadgar/tests/test_sleep_compute.py:509` | "tighten back to 5s once CI runner is faster; 15s is the hard ceiling" | OPEN-QUESTION | Test infra concern. Monitor CI runner speed. Tighten when 5s consistently passes without flake. |

---

## 2026-05-30 — Yadgar memory + wiki scan (consolidation addendum)

**Scan scope:** yadgar wiki pages (all yadgar-* slugs) + episodic memory recall. Prior pass (same date) scanned PLAN_*.md files and repo source. This pass explicitly targets yadgar's own memory store for deferrals not yet captured in DECISIONS.md.

**Noise filtered:** 40+ items excluded — adopt/PD/OQ items already present in this file, action-stream episodic memories, roadmap pipeline entries already in `yadgar-roadmap-future-improvements`, one-off planning notes without revisit triggers.

### Wiki-sourced deferrals

**YM-W-1: Anchor unconditional surfacing — implementation not shipped**
- **Source:** [wiki: yadgar-anchor-memory-design-scopes-and-surfacing] (2026-05-18)
- **Decision:** DEFERRED (design decided, code not shipped)
- **Background:** `restore()` and `session-start-context.py` rank-filter ALL anchors by relevance, dropping cross-project anchors (e.g. PR workflow anchor not surfaced during bug-fix task). Design specifies two scope buckets (global + project) surfaced unconditionally before ranked content. Implementation surface: `yadgar/restore.py` anchor query split + `dotfiles/common/yadgar-hooks/session-start-context.py` + one-time SQL migration of legacy `directory_context IN ("", "system")` rows to `"global"`. S6 from frozen v5.2 plan.
- **Revisit triggers:** next session-start context failure ("I forgot anchor X exists") OR v5.11 cross-project anchor work ships (natural time to wire unconditional surfacing alongside new anchor scope).

**YM-W-2: MCP + Supervisor container proxy (Idea 1) — deferred pending prerequisites**
- **Source:** [wiki: yadgar-deferred-architecture-ideas-half-baked-exploration] (2026-05-23)
- **Decision:** DEFER (prerequisites unmet)
- **Background:** split MCP transport into thin `yadgar-mcp` container that stays alive across daemon restarts, eliminating manual `/mcp` reconnect after vacuum / upgrades. Rejected for now: P8 idempotency markers (currently deferred v5.5) required to prevent double-writes on replay; MCP spec has no pause/resume notification; Claude Code has no auto-reconnect.
- **Revisit triggers:** P8 idempotency markers ship; OR SurrealKV gains online compaction (enables Alt A: move vacuum to separate service); OR Claude Code MCP plugin gains auto-reconnect; OR multi-host deployment.

**YM-W-3: Loki log ingestion blocked — Alloy DynamicUser home-dir permission**
- **Source:** [wiki: yadgar-obs-2026-05-23-investigation] Bug 2 (2026-05-23)
- **Decision:** OPEN-QUESTION (unresolved; three candidate fixes documented)
- **Background:** Alloy (DynamicUser) cannot traverse `~/.yadgar/logs/` because home dir is mode 700. Log shipper silently never ingests — Loki is empty. Dashboard Row 11 (logs) renders blank. Options: A (move log dir to `/var/log/yadgar/` + FHS bind-mount); B (switch to journald + `loki.source.journal` — cleanest for NixOS); C (chmod g+rx home — discouraged). Requires knowing whether yadgar already logs to stdout and whether log files are test-pinned.
- **Resolution path:** decide Option A vs B. Check `yadgar/log_config.py` stdout support + `tests/test_logs_*` coupling. Then implement in yadgar repo + nix repo in same cycle.

**YM-W-4: Tempo OTLP tracing not wired — spans produced but immediately dropped**
- **Source:** [wiki: yadgar-obs-2026-05-23-investigation] Bug 3 (2026-05-23)
- **Decision:** DEFER (needs yadgar version bump + TDD + nix-side Tempo OTLP receiver verification)
- **Background:** `yadgar/tracing.py` has `_OTEL_AVAILABLE` and `get_current_trace_id()` / `get_current_span_id()`. No `OTLPSpanExporter` or `BatchSpanProcessor` wired. No `OTEL_EXPORTER_OTLP_ENDPOINT` set on containers. Tempo OTLP receiver in `modules/observability/tempo.nix` unverified. Full wiring requires: `init_tracing()` in `yadgar/server/__main__.py` + `yadgar/embed_service.py`, env in `docker-compose.yml`, pyproject deps verify, nix receiver confirm.
- **Revisit triggers:** tracing becomes a debugging priority; OR F5-A semaphore CPU burst recurs and trace data would help root-cause; OR next observability session explicitly targets Tempo.

**YM-W-5: cAdvisor + rootless podman label mismatch — Row 9 dashboard queries may be wrong**
- **Source:** [wiki: yadgar-obs-2026-05-23-investigation] Bug 4 (2026-05-23)
- **Decision:** OPEN-QUESTION (predicted issue, not yet observed)
- **Background:** cAdvisor was enabled (v5.6.6 session). Dashboard Row 9 queries `container_cpu_usage_seconds_total{name=~"yadgar.*"}`. Rootless podman puts containers under user cgroup slice with auto-generated IDs; cAdvisor `name` label may be empty or different. Needs first scrape to inspect actual labels.
- **Resolution path:** after next nix apply, curl cAdvisor metrics endpoint, identify correct label for yadgar containers, update Row 9 queries + `$container` variable in `dotfiles/observability/dashboards/yadgar.json`.

**YM-W-6: Security findings S1–S3 — ALL SHIPPED in v5.2.0 (corrected 2026-05-30)**
- **Source:** [wiki: yadgar-v5-stabilize-strategy-tldr-gap-analysis] Security findings section (frozen 2026-05-20, predates v5.2.0)
- **Decision:** **DONE-ALREADY** (corrected from DEFER after security-planner agent verified code state on 2026-05-30)
- **Background:** Three H-level security findings from gap audit: (S1) `storage/ops.py:110,138` + `storage/client.py:375` — raw `json.dumps` in INSERT and raw `extra_where` interpolation (SQL injection). (S2) `rules_engine.py:445` — caller-supplied regex → ReDoS. (S3) `config_yaml.py:840` — config file written without `chmod 600`.
- **Verified shipped:**
  - **S1 SQL injection** — `yadgar/storage/ops.py:25-28,159-163` has `_EXTRA_WHERE_PATTERN` allowlist + `$data` bind param with `S1a`/`S1b` comments. Commit `bea40e2` (v5.2.0).
  - **S3 ReDoS** — `yadgar/rules_engine.py:7-19` imports third-party `regex` lib with `_REGEX_TIMEOUT_S = 1.0`; `:460-484` calls `_regex_lib.sub(..., timeout=_REGEX_TIMEOUT_S)` with `TimeoutError` handler. Commit `e7d231b` (v5.2.0).
  - **S2 chmod 600** — `yadgar/config_yaml.py:977-978` has `os.chmod(path, 0o600)` with `S2 (H-9)` comment. Commit `be1a653` (v5.2.0).
- **Root cause of false premise in original YM-W-6 entry:** the entry was synthesized from a frozen wiki page (`yadgar-v5-stabilize-strategy-tldr-gap-analysis`, frozen 2026-05-20) whose security section predated v5.2.0 ship. Observed state (code + git log) beats stale wiki snapshot.
- **Revisit triggers:** none — closed. If a follow-up regression hardening plan is desired (AST lint confirming no `_EXTRA_WHERE_PATTERN` bypass callers, `regex` lib pin enforcement, audit of any other config files lacking chmod), that's a separate plan and can claim the freed v5.10.11 slot.
- **Lesson recorded:** when consolidating memory-store / wiki content into DECISIONS.md, verify ALL claims against current code state, not just the wiki snapshot. Frozen wiki entries can be stale by multiple ship cycles.

**YM-W-7: repo-wiki DLQ escalation trigger — Option Y threshold**
- **Source:** [wiki: yadgar-repo-wiki-queue-drainer-validation-option-z-v5] (2026-05-15)
- **Decision:** PLANNED (trigger condition documented, not tracked in DECISIONS.md)
- **Background:** Option Z (queue boundary validation) ships as drainer gatekeeper for repo-wiki format drift. Escalation condition: if DLQ accumulates > 5 entries/week from repo-wiki path in v5 production → escalate to Option Y (in-daemon regen, coupling yadgar to repo-indexer CLI). No DLQ monitoring or alert exists yet for this threshold.
- **Revisit triggers:** DLQ monitoring added and first 7-day window with > 5 degenerate/missing-field/schema-old repo-wiki entries observed.

**YM-W-8: v6 depth saturation chunking — must design BEFORE first nightly LLM curator run**
- **Source:** [wiki: yadgar-v5-stabilize-strategy-tldr-gap-analysis] Open design forks #3 (frozen 2026-05-20); partially overlaps PD-36
- **Decision:** OPEN-QUESTION (PD-36 exists but resolution path vague; this entry sharpens it)
- **Background:** SleepGate paper: 16.5% accuracy at interference depth 15. Cluster-by-topic chunking (community detection; curate cluster-by-cluster; never whole-store batch) must be designed as part of v6 plan refinement, not improvised at first-run time. PD-36 says "design as part of v6 plan refinement post-soak" — accepted. This entry surfaces the design artifact needed: a separate `docs/PLAN_V6_CHUNKING_STRATEGY.md` before any v6 LLM curator dispatch.
- **Resolution path:** Draft `docs/PLAN_V6_CHUNKING_STRATEGY.md` as prerequisite gate blocking first `_dream_replay` LLM curator dispatch. **Supersedes:** PD-36 (adds artifact gate — not contradictory).

### Memory-sourced deferrals

**YM-M-1: I13 ruff pre-existing gap — `heuristic_rerank` C901=17 + `sample_system_metrics` PLR0913 noqa fix pending**
- **Source:** [memory id 495179] v5.4 P12 complexity audit anchor (recorded 2026-05-20)
- **Decision:** PLANNED — v5.4.3 (per anchor text "v5.4.3 noqa fix pending")
- **Background:** I13 enforcement shipped v5.4.2 with baseline-ratchet. Two pre-existing ruff violations survive as known gap: `heuristic_rerank` cyclomatic=17 (cap 15) and `sample_system_metrics` PLR0913 (too many args). Ratchet blocks NEW violations; these pre-existing ones need `# noqa: C901` / `# noqa: PLR0913` inline annotations to silence without worsening.
- **Revisit triggers:** v5.4.3 cycle or next complexity-touching PR. Low priority — ratchet prevents regression.

---

## Convention for future use

- **This file** lives at `docs/DECISIONS.md` on master (renamed from `docs/AUDIT_DECISIONS.md` on 2026-05-30).
- **Mirrored** to wiki page `yadgar-audit-decisions-log` (search-discoverable).
- **When dispatching an audit agent**, include in the prompt: "Read `docs/DECISIONS.md` first. Do not re-recommend items marked KEEP-AS-IS, REJECT, or DEFER unless their revisit triggers have fired."
- **When drafting any plan**, any "does not ship / out of scope / later" item of architectural significance must be extracted here before merge. Add a pointer in the plan file: "See `docs/DECISIONS.md` — [dated section]."
- **New audit/plan entries appended** at top under a new dated section.
- **Plan-only commits** go direct to master per yadgar workflow rule (set 2026-05-30).
