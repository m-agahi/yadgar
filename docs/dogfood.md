# Dogfooding Yadgar during its own development

> Real cases where Yadgar surfaced useful context during its own development. No marketing prose. Specific incidents from git history. Verifiable.

## Why this matters

The signal from "we used our own product" is weak unless you can point at concrete moments. Below are 4 incidents from v5 development where Yadgar's memory and wiki saved real time. Each is anchored to a verifiable commit.

## v5 development scale

| Metric | Value | Notes |
|---|---:|---|
| Total memories | 1 509 | as of 2026-05-16 (`yadgar stats`) |
| Active memories | 550 | |
| Archived memories | 959 | |
| Avg heat | 0.164 | |
| Anchor / wiki / CLS counts | — | CLI exposes only the above; direct DB access required for breakdown |

## Incidents

### Incident 1 — benchmark inventory guided by cached Zikkaron lineage
*Evidence:* commit `b97ac35` (benchmarks: revive scripts + add README, 2026-05-16)
*Context:* The benchmark scripts had fallen out of use after forking from Zikkaron. When reviving them, the project-context memory included a note that LoCoMo evaluation scripts were inherited from Zikkaron and that the import paths had changed. That cached fact prevented a dead-end investigation into why the original import structure no longer matched.
*Outcome:* Import-path fix was targeted rather than exploratory. README written in one pass against the current state; no doc-vs-code drift.

### Incident 2 — Meek R2 fix strategy guided by v4.9 rebase-cascade memory
*Evidence:* commit `488b9b2` (fix(§10): CRITICAL Meek R2 wrong index + R3 + boundary match, 2026-05-15)
*Context:* The causal-discovery bug was serious — all persisted causal DAGs were affected. The fix needed to land on master cleanly without entangling unrelated in-flight PRs. Yadgar held a wiki note on v4.9's parallel-PR rebase cascade pain, specifically that own-commit isolation (cherry-pick-safe commits with no cross-file entanglement) was the safe path. That note shaped how the fix was scoped to two files only.
*Outcome:* Fix merged as a self-contained commit with regression tests. No rebase cascade.

### Incident 3 — docs gap surfaced by querying for branch-aware retrieval coverage
*Evidence:* commit `750ef91` (docs: v5 sections in architecture, retrieval, memory-lifecycle, 2026-05-16)
*Context:* A query for "what docs reference branch-aware retrieval" returned `configuration.md` as the only hit. That single result made it immediately clear that architecture.md, retrieval.md, and memory-lifecycle.md — all recently updated in code — had not been updated to match. Three silent documentation gaps in one query.
*Outcome:* All three docs updated in one commit instead of being discovered piecemeal across future sessions.

### Incident 4 — user style feedback anchored and reused during README rewrite
*Evidence:* commit `031fdeb` (docs: reorganize docs/, 2026-05-16)
*Context:* An earlier session captured feedback that the user dislikes long sentences and marketing phrasing. That preference was an anchored memory. When the docs/ reorganization happened and new feature bullets were drafted, the anchor surfaced early in the session context, preventing the usual cycle of writing prose, receiving "too long" feedback, and rewriting.
*Outcome:* Feature bullets written terse from the first draft. Zero style-revision round trips for that commit.

---

Cap: all incidents cite real commits. No memory IDs available via CLI; `yadgar stats` does not expose anchor IDs without direct DB access.
