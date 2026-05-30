# Audit Decisions Log

**Purpose:** persistent record of decisions made in response to competitor / code / security / architectural audits. Future audit agents (human or LLM) MUST consult this file before recommending changes — to avoid re-litigating already-decided questions and to surface revisit triggers.

**Format:** append-only chronological log. Each audit produces one section with one entry per recommendation. Never edit prior entries; if a decision is reversed, add a NEW entry under the next audit's section that supersedes the old one (with `**Supersedes:**` link).

**Storage:** this file is canonical. Mirrored to wiki page `yadgar-audit-decisions-log` for searchable cross-session access.

---

## Audit Protocol (how to use this file)

### When running an audit (you = audit agent or human)

1. **Read this file BEFORE recommending changes.** If a recommendation appears here with `Decision: KEEP-AS-IS` / `REJECT` / `DEFER`, do NOT re-recommend it unless its `Revisit triggers` have fired. Instead, write a one-line "previously decided, no new evidence" note in your audit output.
2. If a previously-rejected recommendation NOW has new evidence that triggers revisit, frame it as "RECONSIDER" not "NEW RECOMMENDATION". Link to the prior entry.
3. If your audit produces new recommendations (not previously seen), they're fair game — propose freely.

### When acting on an audit (you = main thread synthesizing)

1. For every recommendation in the audit, add an entry to the next section below. Even if the decision is "do nothing" — that IS a decision and needs the trail.
2. Required fields per entry:
   - **Recommendation** (quoted from audit, or paraphrased with link)
   - **Decision:** ADOPT / DEFER / REJECT / KEEP-AS-IS / DONE-ALREADY
   - **Reason:** the WHY, with evidence cited (commit SHAs, journal greps, benchmark numbers)
   - **Evidence:** specific data points that supported the decision
   - **Revisit triggers:** conditions under which this decision should be re-evaluated
   - **Supersedes:** link to prior entry if this reverses an earlier decision (optional)
3. Commit to master per workflow rule (docs-only → direct).

### Decision values defined

- **ADOPT:** will implement; assigned version slot. Plan file should exist in `docs/PLAN_V*.md`.
- **DEFER:** valid recommendation but not now. Note `revisit_triggers` clearly.
- **REJECT:** disagree with the audit; explain why. Strongest evidence required.
- **KEEP-AS-IS:** code already does this OR audit recommended change but evidence says current is fine.
- **DONE-ALREADY:** audit missed prior implementation. Link to existing commits/plans.

---

## 2026-05-30 — Competitor Audit (mem0 / chroma / pinecone / zep / letta / postgres / DW)

**Audit doc:** `docs/competitor-audit-2026-05-30.md` (commit `635781e`)
**Scan doc:** `docs/competitor-audit-scan-2026-05-30.md` (this file's companion — agent's findings on already-shipped work)

### Adopt items (decisions pending — being planned by parallel agents)

| Item | Status pending |
|---|---|
| 1. Formal benchmarking | Agent dispatch pending |
| 2. Write-time conflict resolution | Agent dispatch pending |
| 3. Bi-temporal edges on all relationships | Agent dispatch pending |
| 4. In-context memory blocks (Letta) | Agent dispatch pending |
| 5. JavaScript / TypeScript SDK | Agent dispatch pending |
| 6. DuckDB analytics export | Agent dispatch pending |

(This row will be updated to per-item decisions after planner agents return.)

### Refactor items

#### R1. Decouple consolidation from sleep cycle
- **Recommendation:** Separate consolidation cycle (deterministic, fast) and sleep cycle (LLM/CPU-heavy, slow) into distinct orchestrators with separate triggers.
- **Decision:** PARTIAL-ADOPT (limited scope only)
- **What was adopted:** `consolidate_now(mode='light'|'full')` param + 6h gate respect (in-flight as v5.10.4). Stops at param-level switch; no full structural separation.
- **What was NOT adopted:** full split into separate orchestrator classes (`ConsolidationOrchestrator` + `SleepCycleOrchestrator`). Audit recommended this; user decided current scope is enough.
- **Reason:** v5.10.4 mode param solves the immediate bug (13-min surprise + design inversion). Full structural separation is bigger blast radius without clear additional value. Preserve as audit-recorded future option.
- **Evidence:** `docs/PLAN_V5_10_4_CONSOLIDATE_NOW_HEAVYWEIGHT.md` (current implementation plan); v5.10.4 implementation in progress on branch `feat/v5.10.4-consolidate-now-mode-hook-schema`.
- **Revisit triggers:** sleep cycle grows enough phases that mode param becomes unwieldy; or new use cases require running sleep-cycle phases independently of consolidation; or LLM curator tier (v6) needs different scheduling model.

#### R2. Modularize 8-stage retrieval pipeline for pluggability
- **Recommendation:** Make each stage of recall() pipeline (FTS, KNN, PPR, spreading, temporal, WRRF fusion, CE rerank, NLI, MMR, adversarial, rules) a registered plugin to enable A/B testing.
- **Decision:** ADOPT
- **Scope:** full plugin architecture. Each stage = `RetrievalStage` interface with `name`, `apply(state)`, `enabled` flag. Pipeline = list of stages. Per-call profiles (fast/full/debug) + per-stage metrics.
- **Reason:** A/B testing of individual stages currently impossible without code surgery. mem0 (swappable vector backends) and Letta (pluggable archival stores) show the pattern works. Pays off once Adopt item 1 (benchmarks) lands — enables data-driven pipeline tuning. Without benchmarks the value is lower but architecture cleanup still positive.
- **Evidence:** `docs/competitor-audit-2026-05-30.md` Refactor section R2. Current pipeline coupled in single `recall()` function (multi-stage but not plugin-based).
- **Revisit triggers:** none expected — this is a forward commitment. If implementation hits unexpected friction, reassess.
- **Version slot:** likely v5.14.x (after benchmarks land in adopt #1 plan).

#### R3. Replace file-based write queue with DB-native pub/sub
- **Recommendation:** Replace `file_queue/` with SurrealDB `LIVE SELECT` or Postgres LISTEN/NOTIFY for transactional async writes.
- **Decision:** REJECT (with twist — accept eventual consistency everywhere)
- **What was rejected:** the migration itself. File queue stays. No `flush_only()` MCP primitive added either.
- **Approach instead:** document the constraint in every doc that interacts with it. Async eventual consistency IS the architecture. Callers (v5.20.0 roadmap freshness, v5.12.0 refresh button, etc.) must design around it.
- **Reason:** SurrealDB LIVE SELECT is experimental; pgvector migration is multi-version refactor. File queue works. The architectural concern is real but pragmatic cost of replacement exceeds benefit. v5.20.0 plan already documents this constraint.
- **Evidence:** `docs/PLAN_V5_20_0_ROADMAP_FRESHNESS.md` documents the issue; user explicitly chose this option after considering `flush_only()` + LIVE SELECT spike + full replace.
- **Revisit triggers:** SurrealDB LIVE SELECT exits experimental + becomes production-stable; or yadgar suffers multiple production incidents traced to file-queue state (DLQ growth, drain stalls, etc.); or migration to Postgres+pgvector becomes a separate priority for other reasons.

### Ditch items

#### D1. MTREE corruption auto-repair
- **Recommendation:** Demote auto-rebuild to probe-only-LOUD-log; stop masking upstream SurrealDB bug.
- **Decision:** KEEP-AS-IS
- **Reason:** zero corruption events logged in production journal over last 30 days. Production uses HNSW (since migration_001), not MTREE; the original MTREE bug doesn't apply. Probe is one fast KNN query per nightly cycle — negligible cost. Auto-rebuild path never fires in current production. Removing defensive code without observed problem is theoretical hygiene with real downside (silent loss of protection if HNSW ever corrupts).
- **Evidence:**
  - `journalctl --user -u yadgar.service --since '30 days ago' | grep "MTREE index corruption detected" → 0 events`
  - `yadgar/storage/vector.py:176-203` — probe is index-agnostic, rebuild uses generic `REBUILD INDEX` SQL
  - `yadgar/storage/migrations.py:31` `_migration_001_hnsw_indexes` — production migrated from MTREE → HNSW
  - Original MTREE auto-repair from commit `d8cab86` (PR #19, pre-HNSW era)
- **Revisit triggers:**
  - Any HNSW corruption event logged in production journal
  - SurrealDB upstream issue tracker opens HNSW corruption bug
  - Switch to different vector backend (e.g. pgvector)
  - Probe + rebuild becomes hot in profiles (currently sub-ms)

#### D2. NLI diversity stage as always-on
- **Decision:** pending — being discussed.

#### D3. PC algorithm causal discovery
- **Decision:** pending — being discussed.

### Hold items (audit identified as unique moats; no decision needed, recorded for future agents)

- H1. Branch-aware retrieval — no competitor has this. Keep + deepen.
- H2. Wiki + memory pairing — Yadgar's structured knowledge base distinct from pure memory.
- H3. Nightly multi-phase consolidation pipeline — most sophisticated batch in audit.
- H4. Surprise-gated writes — prevents duplicates pre-write, unique to Yadgar.
- H5. 32 MCP tool surface — far ahead of competitors (mem0 ~4, Zep 0).

(These DO NOT need revisit triggers — they're identified strengths to preserve.)

---

## Convention for future audits

- This file lives at `docs/AUDIT_DECISIONS.md` on master.
- Mirrored to wiki page slug `yadgar-audit-decisions-log` (search-discoverable).
- When dispatching an audit agent, INCLUDE in the prompt: "Read `docs/AUDIT_DECISIONS.md` first. Do not re-recommend items marked KEEP-AS-IS, REJECT, or DEFER unless their revisit triggers have fired."
- New audit entries appended at top under a new dated section.
- Plan-only commits per yadgar workflow rule (set 2026-05-30) → direct to master.
