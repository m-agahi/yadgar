# PLAN — v5.21.0: Anchor Cross-Project Dedup + Optional Jira Integration

**Renumbered:** v5.15.0 → v5.21.0 on 2026-05-30. Reason: skip-1 minor convention adopted 2026-05-30 — odd-only minors for sequential features, even slots reserved for hotfix patches between them.

**Status:** drafted 2026-05-27 (evening). Renumber history: v5.10 → v5.11 on 2026-05-28 → v5.15.0 on 2026-05-30 first strict-semver pass → **v5.21.0** on 2026-05-30 skip-1 pass.

**Depends on:** v5.8.0 + v5.9.0 + v5.10.0 shipped. Audit history exists, `tier` + `valid_until` semantics validated in real-world use, test harness no longer leaks orphan SurrealDB workers.

**Related prior plans:** [[PLAN_V5_8_ANCHOR_HYGIENE]], [[PLAN_V5_9_ANCHOR_AUDIT]], [[PLAN_V5_10_TEST_HARNESS_HARDENING]], [[yadgar-anchor-memory-design-scopes-and-surfacing]].

---

## Why

Three gaps remain after v5.8 + v5.9:

1. **Cross-project anchor duplication.** Same workflow rule anchored in 3 different `directory_context` values (e.g. "build amd64 with full registry-prefixed tag" anchored in `/home/max/git/yadgar`, `/home/max/git/nix`, `/home/max/git/dotfiles`). Each project pays the token cost. v5.7.12 introduced `scope: global|project|both` on anchor records, but the audit pass (v5.9) only scopes within `directory_context`. Cross-project pairs go undetected.

2. **Ticket-bound anchors with no resolution detection.** v5.8 introduced `tier=ephemeral` with default 14-day TTL. But QRND tickets often span weeks; the right expiry signal is "ticket resolved", not "14 days passed". Without Jira awareness, users either over-extend TTLs (anchor lingers post-resolution) or under-extend (anchor expires mid-ticket, surfacing as audit noise).

3. **`is_protected=True` flag still semantic-dead.** Almost every anchor sets it → zero discriminative power. Repurposing it as "verified by audit history" turns a no-op flag into actionable signal.

---

## What ships

### 1. Cross-project anchor dedup detection

Extend `audit_anchors` + `project_brief(signals)` with new candidate type:

```python
"cross_project_redundancy_candidates": [
    {
        "primary_id": 257,           # the anchor that should become canonical
        "duplicate_ids": [311992, 489731],
        "similarity": 0.97,
        "directory_contexts": ["/home/max/git/yadgar", "/home/max/git/nix"],
        "recommended_action": "promote_to_global",  # OR "merge_to_primary"
    }
]
```

Two distinct cases:

**Case A — Same content, different project contexts.** Anchor that semantically applies to ALL projects (e.g. "build amd64 with full registry-prefixed tag"). Recommendation: `promote_to_global` — rewrite primary anchor with `directory_context="global"`, forget duplicates.

**Case B — Same content, narrowly project-specific.** False positive of Case A (e.g. an anchor that mentions a username happens to be ~0.95 similar across projects but is genuinely per-project). Recommendation: `merge_to_primary` — keep primary, mark duplicates as `is_redundant=True` for caller review. Never auto-mutated.

Detection: cosine ≥ `ANCHOR_CROSS_PROJECT_COSINE` (default 0.95, higher than within-project 0.92) AND content_length_ratio > 0.85 (rejects "fuzzy match on a common phrase" false positives).

### 2. Optional Jira MCP integration for ticket-bound anchors

**Convention:** anchor tag matching regex `/^(QRND|[A-Z]{2,}-)\d+$/` flagged as ticket-bound at `memorize`/`anchor` time. Auto-set `tier=ephemeral` if user didn't specify.

**Without Jira MCP (default):** ticket-bound anchors use `ANCHOR_TICKET_FALLBACK_TTL_DAYS` (default 30). On expiry, audit suggests `forget_expired_ticket_anchor`.

**With Jira MCP enabled (`JIRA_MCP_URL` env knob set):** audit pass queries the MCP for each ticket-bound anchor. Resolved tickets → suggest immediate forget regardless of `valid_until`. Open tickets → extend `valid_until` rolling 30 days. Hard rule: NEVER auto-forget on Jira response alone (mocks the MCP could be wrong). Always surface as `recommended_actions`, never auto-apply.

Graceful degradation: if `JIRA_MCP_URL` set but MCP unreachable, audit logs warning + falls back to TTL behavior. No errors propagated to caller.

### 3. `is_protected` repurpose: verified-by-audit

After 3 consecutive `audit_anchors` passes where an anchor receives NO recommendation (not merged, not promoted, not expired, not forgotten), audit upgrades the row:

```sql
UPDATE memory SET is_protected = true WHERE id = X
                  AND audit_pass_count >= 3
                  AND last_audit_recommendation = NULL
```

New column `audit_pass_count` (int, default 0) increments on each clean audit. Reset to 0 on any recommendation.

Semantic: `is_protected=True` now means "human or automated audit has verified this anchor as keep-as-is at least 3 times." Discriminative. Useful in `project_brief(restore)` top_anchors ranking (verified anchors surfaced first within scope).

Migration: existing `is_protected=True` rows initialized with `audit_pass_count=0`. Re-earns verification through audit cycles. **No grace-period exception** — true semantic of the flag was always weak; reset is appropriate.

### 4. New env knobs (I25 three-way registered)

| Knob | Default | Type | Purpose |
|---|---|---|---|
| `ANCHOR_CROSS_PROJECT_COSINE` | 0.95 | float | Min cosine for cross-project dedup candidate |
| `ANCHOR_TICKET_FALLBACK_TTL_DAYS` | 30 | int | TTL for ticket-bound anchors when no Jira MCP |
| `ANCHOR_TICKET_TAG_REGEX` | `^(QRND|[A-Z]{2,}-)\d+$` | str | Regex for ticket-tag detection |
| `JIRA_MCP_URL` | "" | str | Optional MCP endpoint for Jira integration (empty = disabled) |
| `JIRA_MCP_TIMEOUT_MS` | 2000 | int | Per-query timeout for Jira MCP |
| `ANCHOR_VERIFIED_AUDIT_PASSES` | 3 | int | Audits without recommendation before `is_protected=True` |

---

## What does NOT ship in v5.10

| Item | Why deferred |
|---|---|
| Tier auto-upgrade (`conditional` → `semantic_immortal` after N audits) | v5.11+. Needs more audit history data first. |
| Multi-language ticket tag patterns (Linear, GitHub Issues) | v5.11+. Start with Jira; expand on demand. |
| Anchor reorganization UI / web frontend | Out of yadgar core scope. Possible in `yadgar-tui` future. |
| Auto-`wiki_add` from `promote_to_global` action | Permanent NO. Same rationale as v5.9 — caller-gated. |

---

## Implementation order

1. **TDD scaffolding** — `yadgar/tests/test_cross_project_audit.py` + `test_jira_integration.py`:
   - Cross-project: cosine threshold, content_length_ratio gate, deterministic primary selection.
   - Ticket-tag regex matches expected formats, rejects false positives.
   - Auto-`tier=ephemeral` when ticket tag detected.
   - Jira MCP integration: mock MCP returning resolved/open → audit suggests forget/extend.
   - Jira MCP unreachable → graceful fallback to TTL.
   - `is_protected` repurpose: 3-pass verification → flag set. Recommendation → counter resets.
2. **Cross-project candidate detection** — new query in `audit.py` (yadgar/server/tools/audit.py from v5.9). Joins memory table to itself filtered by `_anchor IN tags AND directory_context != 'global'`. Pairwise cosine + content_length_ratio. Cap to top-K.
3. **Ticket-tag auto-detection on `memorize`/`anchor`** — regex check on tags, auto-set `tier=ephemeral` if user didn't specify.
4. **Jira MCP integration** — new module `yadgar/integrations/jira_mcp.py`. Optional dependency on `httpx` for MCP HTTP transport. Skip entire module if `JIRA_MCP_URL` empty.
5. **`audit_pass_count` column + verification logic** — schema migration. Increment in audit pass; reset on any recommendation; set `is_protected=True` at threshold.
6. **`recommended_actions` extensions** — 3 new action types: `promote_to_global`, `forget_resolved_ticket_anchor`, `verify_cross_project_dup`.
7. **6 env knobs three-way registered (I25)** — yaml + Settings + registry. New section `anchor_cross_project` + `jira_integration`.
8. **Version bump** — 5.9.0 → 5.10.0 (minor: schema column + new candidate type + optional integration).
9. **MIGRATION_NOTES.md** v5.10.0 section — schema migration + Jira opt-in instructions.

---

## Acceptance criteria

- `pytest yadgar/tests/test_cross_project_audit.py yadgar/tests/test_jira_integration.py` green.
- Schema migration adds `audit_pass_count` column with default 0; no row loss.
- Cross-project dedup detection finds known seeded pairs across 3 project contexts.
- Jira MCP integration works against mock + degrades gracefully when unreachable.
- `is_protected` flag correctly increments + sets after 3 clean audits.
- I13 + I23 + I24 + I25 lints green.
- `python scripts/check_versions.py` exit 0.

---

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Cross-project cosine misidentifies different-but-similar anchors as dupes | Higher threshold (0.95) + content_length_ratio > 0.85 gate. Recommendation is `merge_to_primary` (audit-flag, not auto-mutate). |
| Jira MCP integration becomes maintenance burden | Optional, opt-in via env knob. Default disabled. Module skipped entirely when knob empty. |
| Ticket tag regex too narrow (misses real ticket formats) | Env-knob configurable. Document common patterns (QRND, JIRA-, LINEAR-) in MIGRATION_NOTES. |
| `is_protected` repurpose breaks existing query semantics | All existing `is_protected=True` callers checked: `restore()` ranking, hot-memory filter, `project_brief(restore)` top_anchors. Reset to `False + audit_pass_count=0` is safe: anchors don't lose surfacing (tier-gated, not is_protected-gated post-v5.8). Verify in test. |
| Jira MCP latency spikes audit duration | Per-query timeout `JIRA_MCP_TIMEOUT_MS` 2s default. Total audit budget cap (env knob from v5.9). |
| `audit_pass_count` exposes anchors to bypass scrutiny if audit runs too frequently | Default `ANCHOR_VERIFIED_AUDIT_PASSES=3` + audit cadence tied to consolidation (1/day) = 3-day minimum. Tunable. |

---

## Estimate

~700 LOC implementation + ~500 LOC tests. **Jira MCP module is optional and isolated** — main payload is cross-project dedup + `is_protected` repurpose. Two-PR split feasible:

- **PR-A (cross-project + ticket-tag + verified-by-audit):** core yadgar changes, no external deps.
- **PR-B (Jira MCP integration):** isolated, opt-in. Can defer indefinitely if dependency cost rejected.

---

## Sequencing

After v5.9.0 has shipped + 4+ weeks of audit history collected. Validates audit pass mechanics + provides real cross-project dup data to test detection against.

---

## Open / parked questions

- **Primary anchor selection in cross-project dedup** — lean: anchor with highest `access_count + heat`, tie-broken by oldest `created_at` (preserves original intent). Alternative: per-`directory_context` length-weighted score. Decide during v5.10 TDD.
- **`promote_to_global` mutation semantics** — does the global-promoted anchor inherit ALL tags from the duplicates, or only intersection? Lean union (keeps searchability). Document.
- **Jira MCP auth** — does the MCP server itself handle auth (preferred), or does yadgar pass through env-var token? Lean: MCP handles. Don't add `JIRA_API_TOKEN` to yadgar Settings. Document required MCP setup in MIGRATION_NOTES.
- **`audit_pass_count` decrement on `recommendation_dismissed` action** — should the counter reset only on RECOMMENDED actions, or also when the user explicitly says "no, keep this anchor as-is"? Lean: only reset on mutation. Dismissal = positive verification signal.
- **Multi-tenancy** — if yadgar ever runs cross-user (it doesn't today), `is_protected` repurpose would need per-user audit count. Defer.
- **`migration_grace=true` graceful expiry** — DESIGN HOLE surfaced 2026-05-29. v5.8 backfill set `migration_grace=true` on ALL pre-v5.8 _anchor rows. v5.9 `audit_anchors().forget_expired_anchors` explicitly skips grace-protected rows. Result: after their backfilled 90d expiry, rows become invisible (excluded from restore/hot/signals) BUT persist in DB indefinitely as dead weight + count toward `anchor_count_project` signal threshold. Example: memory id 518764 (terraform-modules cloudfront mock TODO) — anchored 2026-05-27 pre-v5.8, original intent 15-day TTL, backfilled to 90d + grace=true, becomes invisible 2026-08-26, persists forever otherwise. **Candidates for v5.11.x:** (a) NEW recommendation type `verify_grace_expired_anchor` surfaces grace-protected rows past valid_until as user-gated review items, after N skipped audits auto-clear grace; (b) auto-upgrade tier to `semantic_immortal` if heat > threshold at grace-expiry, else clear grace and re-enter normal expiry path. Lean (a) — preserves user-curated semantic, no silent data motion.

---

## Cumulative state after v5.10.0

| Surface | v5.7.x | v5.8.0 | v5.9.0 | v5.10.0 |
|---|---|---|---|---|
| Anchor TTL/expiry | none | `valid_until` + `tier` | + audit-driven extend | + Jira-driven extend |
| Hygiene signals | none | 3 fields in `signals` | + audit history snapshot | + cross-project candidates |
| Mutation tools | manual `forget` | manual `forget` | `audit_anchors(dry_run)` | + cross-project actions |
| `is_protected` flag | binary, irreversible | binary, set by `tier` presence | binary, set by `tier` presence | verified-by-audit, dynamic |
| External integrations | none | none | none | optional Jira MCP |
| Token budget impact | baseline | +30-80 (signals) | +50-150 (recommendations) | +20-50 (cross-project) |

After v5.10.0, anchor hygiene = "audit runs nightly via consolidation, surfaces drift via signals, user approves mutations via `audit_anchors(dry_run=False)`, ticket-bound anchors auto-expire post-resolution." Manual chore → routine maintenance task with prompt.
