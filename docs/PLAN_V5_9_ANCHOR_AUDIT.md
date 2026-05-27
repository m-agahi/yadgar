# PLAN — v5.9.0: Anchor Audit + Consolidation Pass

**Status:** drafted 2026-05-27 (evening).

**Depends on:** v5.8.0 shipped (`tier`, `valid_until`, hygiene signals exist).

**Related prior plans:** [[PLAN_V5_8_ANCHOR_HYGIENE]], [[yadgar-anchor-memory-design-scopes-and-surfacing]].

---

## Why

v5.8.0 surfaces hygiene candidates via `project_brief(signals).recommended_actions`. Caller (Claude in session) must still manually invoke separate tools to act:

- Merge redundant pair → no dedicated tool; manual `forget()` + `anchor()` rewrite.
- Promote oversized anchor to wiki → manual `wiki_add()` + `forget(anchor_id)`.
- Forget expired anchor → `forget()` per ID, one at a time.

Result: hygiene actions are 3-5 tool calls per anchor. Multiplied by 5-10 candidates per audit, audit becomes a 30-tool-call chore. User skips it.

**Goal:** one tool call performs the audit + applies the safe mutations + returns the recommendations table.

Also: **`consolidate_now()` already runs as a periodic batch operation.** Adding an anchor pass to consolidation = zero new scheduling surface; rides existing channel.

---

## What ships

### 1. New MCP tool: `audit_anchors(directory, dry_run=True)`

```python
def audit_anchors(
    directory: str,
    dry_run: bool = True,
    cosine_threshold: float | None = None,  # override env knob
    include_global: bool = False,           # also audit directory_context="global"
) -> dict:
    """Audit anchors for redundancy, oversize, expiry, completion.

    Returns:
        {
            "scanned": int,
            "actions": [
                {"action": "merge", "ids": [a, b], "similarity": 0.94, "rationale": "..."},
                {"action": "promote", "id": X, "draft_slug": "...", "draft_body": "...", "rationale": "..."},
                {"action": "forget_expired", "id": Y, "expired_at": "...", "rationale": "..."},
                ...
            ],
            "dry_run": bool,
            "applied": [{action_index, status}],  # populated when dry_run=False
        }
    """
```

**Behaviour matrix:**

| dry_run | tier=semantic_immortal | What happens |
|---|---|---|
| True | any | All recommendations returned; nothing mutated. |
| False | semantic_immortal | Recommendations returned; semantic_immortal rows NEVER auto-mutated. Caller must explicit-override via separate `forget(id, force=True)`. |
| False | conditional/ephemeral | Safe mutations applied: `forget_expired` (always safe), `merge` (lower-similarity-score anchor of pair forgotten; higher kept). |

**NEVER auto-applied even with `dry_run=False`:**
- `promote_to_wiki` — returns draft only (wiki page creation = user-curated slug/tag/category decision).
- Forget on `tier=semantic_immortal`.
- Mutations on anchors with `is_protected=True` (legacy semantic, preserved for backwards-compat until v5.10 repurpose).

All mutations logged to `action_log` with full payload + before/after row state. Auditable via `recall` on the audit timestamp.

### 2. Extend `consolidate_now()` with anchor pass

`consolidate_now()` currently does: write-gate replay → embedding refresh → heat decay → SR cogmap update. Add **anchor audit pass** as final step:

- Runs `audit_anchors(directory=<every project context with >0 anchors>, dry_run=True)` per project.
- Writes results to a `_audit_anchors` memory (sentinel slot, one per directory).
- Surfaced in next `project_brief(mode="signals")` as `last_audit_findings` summary count.
- Does NOT auto-mutate (consolidation runs unattended; mutations stay user-gated).

**Cadence:** existing consolidation cadence (post-nightly-cycle in v5.7.0+). No new timer.

### 3. Promote-to-wiki draft generator

When `audit_anchors` flags `promote` candidate, return a structured draft:

```python
{
    "action": "promote",
    "id": 257,
    "draft": {
        "suggested_slug": "yadgar-workflow-rule-build-and-push",  # derived from first H1/title
        "suggested_title": "Yadgar Workflow Rule — Build and Push",
        "suggested_category": "convention",   # inferred from tag intersection
        "suggested_tags": ["yadgar", "workflow", "build", "deploy", "_anchor"],
        "body": "<anchor content verbatim>",
        "rationale": "600 words + 3 section headers + tag intersect {workflow}",
    },
    "next_step": "Call wiki_add(title, content, tags, category) with these values, then forget(257).",
}
```

Caller decides whether to accept draft. No auto-`wiki_add`. (Rationale: wiki pages have user-curated slugs + tag families + approval workflow. Auto-creating dirty pages reproduces hygiene debt one layer down.)

### 4. `recommended_actions` enhancement

v5.8 emitted `audit_anchors` action with no parameters. v5.9 extends:

```python
{
    "action": "audit_anchors",
    "reason": "anchor_count=18 > 15 OR redundancy_pairs=3 OR promote=2",
    "suggested_call": "audit_anchors(directory='/home/max/git/yadgar', dry_run=True)",
}
```

Caller copy-pastes `suggested_call`. Same pattern as v5.7.12 `restore` hint.

### 5. New env knobs (I25 three-way registered)

| Knob | Default | Type | Purpose |
|---|---|---|---|
| `ANCHOR_AUDIT_CONSOLIDATION_ENABLED` | true | bool | Toggle anchor pass inside `consolidate_now()`. |
| `ANCHOR_AUDIT_MAX_ACTIONS_PER_RUN` | 20 | int | Hard cap on actions returned per audit (token budget). |
| `ANCHOR_AUDIT_HISTORY_RETENTION_DAYS` | 30 | int | How long `_audit_anchors` snapshots retained for history. |

---

## What does NOT ship in v5.9

| Item | Why deferred |
|---|---|
| Cross-project anchor dedup | v5.10 (needs scope=both auditing semantics) |
| Optional Jira MCP integration | v5.10 |
| `is_protected` repurpose (verified-by-audit) | v5.10 (depends on audit history existing here) |
| Auto-promote-to-wiki | Permanent NO (caller-gated by design) |
| Tier auto-upgrade after N audits keep it | v5.10 (depends on history) |

---

## Implementation order

1. **TDD scaffolding** — `yadgar/tests/test_audit_anchors.py`:
   - `dry_run=True` returns recommendations; no DB mutations.
   - `dry_run=False` applies safe mutations; logs to `action_log`.
   - `tier=semantic_immortal` rows never auto-mutated.
   - `promote` action returns draft, never calls `wiki_add`.
   - Forget-expired only fires for `valid_until < now()`.
   - Merge picks the lower-similarity-rank anchor of pair (deterministic).
   - Promote draft slug + category derivation correct on sample inputs.
2. **`audit_anchors` MCP tool implementation** — yadgar/server/tools/audit.py (new module). Pure function over (directory, dry_run, knobs) → result dict. Reuses redundancy/promote/expiry detection from v5.8 signals computation.
3. **`consolidate_now()` extension** — add anchor pass as final step. Gate on `ANCHOR_AUDIT_CONSOLIDATION_ENABLED`. Write `_audit_anchors` sentinel memory per directory.
4. **`recommended_actions` extension** — emit `suggested_call` string for `audit_anchors`.
5. **`action_log` integration** — every mutation logged with before/after row snapshots for auditability.
6. **3 env knobs three-way registered (I25)** — yaml + Settings + registry. New section `anchor_audit`.
7. **Version bump** — 5.8.0 → 5.9.0 (minor: new tool surface).
8. **MIGRATION_NOTES.md** v5.9.0 section.

---

## Acceptance criteria

- `pytest yadgar/tests/test_audit_anchors.py` green.
- End-to-end test: seed DB with 15 anchors (5 redundant, 3 oversize, 2 expired, 5 fresh) → `audit_anchors(dry_run=False)` mutates exactly the 7 safe candidates, leaves 3 promote candidates as drafts, preserves 5 fresh.
- `consolidate_now()` with `ANCHOR_AUDIT_CONSOLIDATION_ENABLED=true` writes `_audit_anchors` memory.
- I13 + I23 + I24 + I25 lints green.
- `python scripts/check_versions.py` exit 0.

---

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Auto-merge picks wrong anchor of pair (drops the higher-quality one) | "Lower similarity rank" = anchor with more recent `last_accessed` + higher `access_count` is KEPT. Test covers tie-break order. |
| `consolidate_now` anchor pass slows nightly cycle | Cap per-directory work via `ANCHOR_AUDIT_MAX_ACTIONS_PER_RUN`. Skip directories with `anchor_count_project < ANCHOR_AUDIT_THRESHOLD`. |
| Promote draft slug collides with existing wiki page | `audit_anchors` does NOT create wiki page. Slug is "suggested" only; caller verifies via `wiki_list(slug_prefix=...)` before `wiki_add`. |
| `action_log` bloat from frequent mutations | Existing action_log retention applies. Audit-source actions tagged `source=audit_anchors` for filtering. |
| dry_run=False called on pristine DB by accident | `audit_anchors` is **idempotent** — second call on same state returns empty `applied` list. No destructive double-apply. |
| Drafts contain stale anchor body | Draft generated at audit time. Caller responsible for `wiki_add` within same session or re-run audit. Document. |

---

## Estimate

~400 LOC implementation + ~350 LOC tests. Single agent dispatch. Lower risk than v5.8 (no schema migration).

---

## Sequencing

After v5.8.0 has shipped + 2+ weeks of real-world anchor accumulation under new `tier`/`valid_until` semantics. Validates v5.8 defaults before adding mutations on top.

---

## Open / parked questions

- **`_audit_anchors` sentinel storage** — single row per directory or append-only history? Lean: latest-wins single row (matches v5.7.12 `_active_work` pattern). History via `action_log` filter.
- **Merge merge-mechanics for conflicting metadata** — when two anchors merge, do we union their tags? Pick latest reason? Currently: keep tags ∪, keep `reason` of survivor, log discarded reason in `action_log`.
- **Audit cost on huge DBs** — pairwise cosine is O(N²) per project. For projects with 50+ anchors this matters. Cap N or use ANN index?  Defer until observed.
- **Per-directory vs global audit pass** — current draft: per-directory. Global anchors audited separately when `include_global=True`. Should global audit run automatically in `consolidate_now()`? Lean yes, gated on `ANCHOR_AUDIT_CONSOLIDATION_ENABLED`.
