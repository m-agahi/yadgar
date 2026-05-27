# PLAN — v5.8.0: Anchor Hygiene Foundation (TTL + tier + new signals)

**Status:** drafted 2026-05-27 (evening, post-v5.7.12 ship).

**Master at draft time:** core v5.7.12 + backend v5.3.1.

**Builds on:** v5.7.12 `project_brief(mode="signals")` + `recommended_actions` channel.

**Related prior decision:** [[yadgar-anchor-memory-design-scopes-and-surfacing]] (2026-05-18) — established two-scope anchor model (global vs project). v5.8 layers hygiene mechanics on top.

---

## Why

Anchors are heat-immortal by design — `is_protected=True` exempts them from decay. Without expiry mechanics they accumulate forever. Real-world observation 2026-05-27 (`/home/max/aws-work`): 17 project-scope anchors accumulated in ~3 weeks. Manual audit + subagent assistance collapsed to 11 anchors, saving ~800 tokens/session. At observed creation rate (≈1/workday), a year of work = 250+ anchors per project = unusable session-start payload + diluted signal.

Five failure modes observed:

1. **Near-duplicates** (id 146 + 148, ~0.95 cosine, created 31 sec apart, same `directory_context`).
2. **Wiki-redundant inventory snapshots** (4 anchors restating wiki index counts).
3. **Wiki-sized workflow rules misanchored** (id 257 + 311992, 600+ words, multiple section headers — these belong in a wiki page).
4. **Completed-plan anchors** (1 dry-run plan whose execution completed; zero re-access since creation).
5. **Ticket-bound anchors with no expiry** (5 anchors tied to in-flight QRND tickets — valid only until ticket closes; no mechanism surfaces them after close).

Root cause: **`is_protected=True` is binary and irreversible at write-time.** Caller must guess "is this fact immortal" at the moment of anchoring, with no second chance. Hygiene = entirely manual.

---

## What ships

### 1. `valid_until` field on memorize + anchor

New optional field on `memorize()` and `anchor()` MCP tools:

```python
valid_until: datetime | None = None  # ISO-8601 UTC; null = no expiry
ttl_days: int | None = None          # shorthand: now + ttl_days days
```

Persisted on memory row. `valid_until < now()` → memory excluded from `restore()`, hot ranking, and `project_brief(restore)` anchor payload. Still queryable via `recall(min_heat=0)` for explicit lookup; never auto-surfaced after expiry.

Migration: existing rows get `valid_until = NULL` (no behavior change).

### 2. `tier` enum on anchor

New required field on `anchor()` and on `memorize(is_protected=True)`:

```python
tier: Literal["semantic_immortal", "conditional", "ephemeral"] = "conditional"
```

| tier | Semantics | Default `valid_until` if unset |
|---|---|---|
| `semantic_immortal` | Truly cross-session forever (credentials locations, hard rules, account IDs). User must explicitly request. | `NULL` (never expires) |
| `conditional` (default) | Currently true but may stale. The 80% case. | `now() + ANCHOR_CONDITIONAL_TTL_DAYS` (default 90) |
| `ephemeral` | Valid only during in-flight work (ticket state, dry-run plans, current incident). | `now() + ANCHOR_EPHEMERAL_TTL_DAYS` (default 14) |

Migration: all existing anchors → `tier="conditional", valid_until=now()+90d`. Surfaced via new signal `anchor_tier_migrated_count` so user can audit during one-time grace period.

**Behavioral change:** `is_protected=True` no longer the discriminator. Anchor surfacing logic queries `tier IN ('semantic_immortal', 'conditional', 'ephemeral') AND (valid_until IS NULL OR valid_until > now())`. `is_protected` retained as legacy column — set automatically when `tier IS NOT NULL`. Repurposed in v5.10.

### 3. New signals in `project_brief(mode="signals")`

Three additions to the existing signals dict:

| Field | Type | Computation |
|---|---|---|
| `anchor_count_project` | int | `count(*) WHERE _anchor IN tags AND directory_context = <dir> AND (valid_until IS NULL OR valid_until > now())` |
| `anchor_redundancy_candidates` | `list[{id_a, id_b, similarity}]` | Pairs in same `directory_context` with cosine ≥ `ANCHOR_REDUNDANCY_COSINE` (default 0.92). Capped at top-5 by similarity. |
| `anchor_promote_candidates` | `list[int]` | IDs satisfying: word_count > `ANCHOR_PROMOTE_WORDS` (default 500) AND markdown_header_count ≥ `ANCHOR_PROMOTE_HEADERS` (default 2) AND tags ∩ {`rule`, `pattern`, `convention`, `playbook`, `workflow`, `recipe`} ≠ ∅. |

Token-budget impact on `signals` mode: +30-80 tokens worst case (capped lists). Stays under 100-token budget except in pathological cases — add hard truncation if needed.

### 4. New `recommended_actions` action types

| action | trigger | reason field |
|---|---|---|
| `audit_anchors` | `anchor_count_project > ANCHOR_AUDIT_THRESHOLD` (default 15) | `count=N > threshold=15` |
| `merge_redundant_anchors` | `len(anchor_redundancy_candidates) ≥ 1` | `redundancy_pairs=N` |
| `promote_anchor_to_wiki` | `len(anchor_promote_candidates) ≥ 1` | `oversized=N` |
| `forget_expired_anchors` | exists anchor with `valid_until < now()` and grace window passed | `expired=N` |

Stop hook (yadgar/hooks/stop-memory-checkpoint.py) already iterates `recommended_actions` (v5.7.12). The audit/merge/promote/forget actions surface but caller (Claude in session) still decides — **NO auto-mutation in v5.8** (mutations land in v5.9 `consolidate_now` extension).

### 5. Five new env knobs (I25 three-way registered, reason=category)

| Knob | Default | Type | Purpose |
|---|---|---|---|
| `ANCHOR_CONDITIONAL_TTL_DAYS` | 90 | int | Default `valid_until` offset for `tier=conditional` |
| `ANCHOR_EPHEMERAL_TTL_DAYS` | 14 | int | Default `valid_until` offset for `tier=ephemeral` |
| `ANCHOR_REDUNDANCY_COSINE` | 0.92 | float | Min cosine for redundancy candidate pair |
| `ANCHOR_PROMOTE_WORDS` | 500 | int | Min word count for promote-to-wiki candidate |
| `ANCHOR_PROMOTE_HEADERS` | 2 | int | Min markdown header count for promote-to-wiki candidate |
| `ANCHOR_AUDIT_THRESHOLD` | 15 | int | Min anchor count to emit `audit_anchors` action |

All 6 (I miscounted — six knobs total) three-way registered per I25 with reason=category. Backwards-compatible defaults preserve current behavior for unchanged installs.

---

## What does NOT ship in v5.8

| Item | Why deferred |
|---|---|
| `audit_anchors()` MCP tool | v5.9 — needs `consolidate_now` extension first |
| Auto-mutation of expired anchors | v5.9 — gated on `consolidate_now` extension |
| Promote-to-wiki draft generator | v5.9 — needs decision on draft slug/tag conventions |
| Cross-project anchor dedup | v5.10 — needs scope=both audit semantics from v5.7.12 stabilized first |
| Optional Jira MCP integration | v5.10 — graceful-degrade design needs separate plan |
| `is_protected` repurpose (verified-by-audit flag) | v5.10 — depends on v5.9 audit history existing |
| Anchor TTL field at `wiki_add` level | Out of scope. Wiki pages have own lifecycle (deprecation flag, see wiki_lint). |

---

## Implementation order

1. **TDD scaffolding** — `yadgar/tests/test_anchor_hygiene.py`:
   - `memorize(is_protected=True)` accepts `valid_until` + `tier` + `ttl_days`.
   - `anchor(...)` requires `tier`, defaults to `conditional`.
   - `valid_until` correctly excludes expired rows from `restore()` + `project_brief(restore)`.
   - `project_brief(signals)` returns three new fields.
   - Redundancy detection: cosine threshold + same `directory_context`.
   - Promote detection: triple AND (words + headers + tag intersection).
   - `recommended_actions` emits 4 new action types under correct conditions.
   - Token budget: `signals` ≤100 holds in pathological case (10+ redundancy pairs).
2. **Schema migration** — `valid_until` + `tier` columns. SurrealDB schema bump. Migration path normalizes existing anchors → `tier=conditional, valid_until=now()+90d`. Emit one-time log line per migrated row.
3. **`memorize` + `anchor` MCP tool signatures** — add fields, validate `tier` enum, compute `valid_until` from `ttl_days` if both unset and `tier != semantic_immortal`.
4. **Query updates** — `restore()`, hot ranking, `project_brief(restore)` top_anchors query — all add `AND (valid_until IS NULL OR valid_until > datetime::now())` clause.
5. **Signals computation** — three new fields in `project_brief(mode="signals")`. Cosine pairing uses existing embedding column; cap to top-5 by similarity for token budget.
6. **`recommended_actions` extension** — 4 new action types, deterministic order, threshold-driven.
7. **6 env knobs three-way registered (I25)** — yaml schema + Settings + registry. New section `anchor_hygiene` in yaml.
8. **Version bump** — 5.7.12 → 5.8.0 (minor bump: schema change + new tools surface). Backend version unchanged (no embedding/recall logic moves to backend).
9. **MIGRATION_NOTES.md** v5.8.0 section — schema migration + new fields documented. **Backwards-compatible**: existing `anchor()` calls without `tier` still work (default `conditional`).
10. **Wiki update** — [[yadgar-anchor-memory-design-scopes-and-surfacing]] gets v5.8 amendment section linking back to this plan.

---

## Acceptance criteria

- `pytest yadgar/tests/test_anchor_hygiene.py` green.
- Schema migration tested end-to-end on a copy of live DB (no row loss; correct `tier`/`valid_until` defaults).
- `project_brief(mode="signals")` token budget ≤100 with 15+ anchors, 5+ redundancy pairs.
- `restore()` excludes expired anchors (negative test).
- All 6 env knobs I25 green (three-way registered, reason=category).
- `python scripts/check_versions.py` exit 0.
- I13 + I23 + I24 + I25 lints green.

---

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Schema migration loses existing anchor metadata | Dry-run migration script in PR-1; backup snapshot mandatory pre-migration. |
| `tier=conditional` default with 90-day TTL silently expires legit immortal anchors | One-time grace period after migration: ALL pre-migration anchors marked with `migration_grace=True`, surfaced in `recommended_actions` as `verify_anchor_tier(id=X)` for first session post-migration. User explicitly upgrades to `semantic_immortal` or accepts conditional. |
| Cosine threshold 0.92 false-positives | Threshold is env-knob configurable; redundancy detection is **flag-only** in v5.8 (no auto-merge). User confirms in v5.9 via `audit_anchors(dry_run=True)`. |
| Promote-to-wiki triple AND too strict (misses legit candidates) | Document the heuristic + env knobs in MIGRATION_NOTES. User can tune. |
| Signals budget overflow on pathological anchor sets | Hard truncate candidate lists to K (env knob) before serialization. Document overflow in `_truncated: true` field. |
| Backend version drift if `valid_until` filter pushed to backend | All `valid_until` filtering done at yadgar/core query layer — no backend change. Verified in PR scope. |

---

## Estimate

~600 LOC implementation + ~400 LOC tests. **Schema migration is the long pole** (live DB has anchors going back months). Single agent dispatch feasible but two-PR split recommended:

- **PR-A (schema + migration + tier/valid_until plumbing):** isolated, mergeable independently. Existing `project_brief` behavior unchanged.
- **PR-B (signals + recommended_actions + knobs):** depends on PR-A merged, smaller scope.

---

## Sequencing vs other trains

| Plan | Status | Order |
|---|---|---|
| **v5.8.0 anchor hygiene foundation (this)** | drafted | Ship after v5.7.13 hotfix sweep clears. Schema change = needs clean baseline. |
| v5.9.0 anchor audit + consolidation | drafted | After v5.8.0 has 2+ weeks of real-world `tier`/`valid_until` data to validate defaults. |
| v5.10.0 anchor cross-project + Jira | drafted | After v5.9.0 audit history exists. |
| Backend v5.4.0 recall caching | drafted | Independent of anchor work. Can run in parallel. |

---

## Open / parked questions

- **`tier` migration grace duration** — first session? first week? first 100 anchor surface events? Default: first session post-migration. User accepts/upgrades, then grace cleared.
- **`semantic_immortal` write gate** — should `anchor(tier="semantic_immortal")` require an additional `reason` argument explaining why it's truly immortal? Forces deliberate thought. **Lean yes** — add as required field.
- **Cosine source for redundancy** — yadgar already stores embedding bytes per row. Use the existing embedding model (all-MiniLM-L6-v2) directly. Confirm column exists + dimensions match before implementation.
- **Markdown header counter** — regex `^#{1,6}\s` per line on `content`. Edge case: code blocks containing `#` comments. Strip fenced code blocks first.
- **`valid_until` timezone semantics** — UTC always. Reject naive datetimes at MCP boundary.

---

## v5.9 / v5.10 follow-up (deferred)

See [[PLAN_V5_9_ANCHOR_AUDIT]] and [[PLAN_V5_10_ANCHOR_CROSS_PROJECT]] (drafted same session).
