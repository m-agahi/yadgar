# PLAN — v5.10.x: `memorize(is_protected=True)` ↔ `anchor()` parity fix

**Status:** drafted 2026-05-29 after advisor consult on "should we kill anchor() entirely?" Decision: keep `anchor()` (convenience shorthand has ergonomic value for in-chat use). Fix the underlying parity bug so the two surfaces produce identical row state.

**Master at draft time:** core v5.10.1 + backend v5.4.0 deployed.

**Sequencing:** v5.10.x patch. Order vs other pending plans is up to main thread (queue: v5.10.2 nightly-cycle hotfix already on `chore/v5.10.2-nightly-cycle-bugs` branch; session-end-capture plan still to be drafted).

---

## Why

Silent behavior divergence between `anchor()` and `memorize(is_protected=True)` despite docstring claim of equivalence (`memorize.py:95`: *"Equivalent to calling anchor() but inline."*).

Advisor (2026-05-29 session) caught this when scoping "kill anchor()" — the kill would have been a behavior regression, not a refactor.

**`anchor(content, context, reason)` does implicitly:**
- Sets `is_protected=True`
- Sets `tier="conditional"` (per v5.8 PR-A default) with implicit 90d TTL via `ANCHOR_CONDITIONAL_TTL_DAYS`
- Auto-adds tag `_anchor`
- Auto-adds tag `anchor:{reason}` when `reason` non-empty
- Validates `reason` non-empty when `tier="semantic_immortal"` (per `ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON`, default true)
- Branch re-detection (per `file_queue/apply.py:56`)

**`memorize(content, context, tags, is_protected=True)` today does:**
- Sets `is_protected=True`
- Does NOT auto-set `tier` — column stays NULL or whatever caller passed
- Does NOT auto-add `_anchor` to tags — caller must include manually
- Does NOT validate `reason` (no `reason` param)
- Has its own branch detection

**Consequence:** identical user intent ("mark this immortal") writes two different rows depending on which tool was called. `audit_anchors()` (v5.9) keys off `_anchor IN tags`, so memorize-without-explicit-_anchor-tag rows escape audit. Real silent bug.

---

## What ships

### 1. Upgrade `memorize()` to absorb anchor()'s implicit defaults

In `yadgar/server/tools/memorize.py::memorize()`, when `is_protected=True` (and equivalent: `tier` is non-None, or `_anchor` already in tags):

| Implicit default | Behavior |
|---|---|
| `tier` unset (None) | Set `tier="conditional"` |
| `_anchor` NOT in tags | Auto-prepend `_anchor` to tags list |
| `valid_until` unset AND `ttl_days` unset AND `tier="conditional"` | Compute `valid_until = now() + ANCHOR_CONDITIONAL_TTL_DAYS` (matches anchor() default) |
| `valid_until` unset AND `ttl_days` unset AND `tier="ephemeral"` | Compute `valid_until = now() + ANCHOR_EPHEMERAL_TTL_DAYS` |
| `tier="semantic_immortal"` AND caller didn't pass reason kwarg | Reject if `ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON=true` (current behavior matches; just port to memorize) |

`memorize()` doesn't currently take a `reason` kwarg — add it as optional. When set, also auto-adds `anchor:{reason}` to tags (matches anchor()).

After this fix: `memorize(content, context, tags, is_protected=True, reason="X")` is row-equivalent to `anchor(content, context, reason="X")`.

### 2. `anchor()` docstring demotion (NOT removal)

`anchor()` in `misc.py:142` stays callable but docstring updates:

```python
def anchor(content: str, context: str, reason: str = "", *, tier: str = "conditional", ...) -> dict:
    """Convenience shorthand for `memorize(is_protected=True, reason=...)`.

    Both surfaces produce identical row state as of v5.10.x. Use whichever
    feels natural — `anchor()` is shorter; `memorize(is_protected=True)`
    keeps you in the single-verb pattern.

    See [[memorize]] for the full parameter surface.
    """
```

### 3. CLAUDE.md / `sync_instructions` surface demote

`yadgar/server/tools/misc.py::sync_instructions` lists `anchor()` alongside `memorize()` today. Update to lead with `memorize()` and group `anchor()` under "Convenience shorthand" / "Legacy".

**`agent_dispatch_prelude`** identical update.

### 4. Tests

`yadgar/tests/test_memorize_anchor_parity.py`:
- `memorize(c, ctx, tags=[], is_protected=True)` row state == `anchor(c, ctx)` row state (post-fix).
- `memorize(c, ctx, is_protected=True, tier="ephemeral")` → `valid_until` matches `now() + ANCHOR_EPHEMERAL_TTL_DAYS`.
- `memorize(c, ctx, is_protected=True, reason="X")` → tags contain `_anchor` and `anchor:X`.
- `memorize(c, ctx, is_protected=True, tier="semantic_immortal")` WITHOUT reason → raises (when `ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON=true`).
- `memorize(c, ctx, tags=["custom"], is_protected=True)` → tags = `["_anchor", "custom"]` (auto-prepend, not replace).
- `memorize(c, ctx, is_protected=False)` → tags unchanged, `tier` stays None, `valid_until` None. (Negative test: defaults only fire for is_protected=True.)
- audit_anchors() picks up both anchor()-created AND memorize(is_protected=True)-created rows.

---

## What does NOT ship

| Item | Why deferred |
|---|---|
| Removing `anchor()` implementation | Reversibility cost too high. Convenience shorthand has ergonomic value. Keep callable. |
| Removing `anchor()` from `sync_instructions` | Demote, don't remove. Existing prompts referencing `anchor()` still need to work. |
| Telemetry for `anchor()` vs `memorize(is_protected=True)` call counts | Useful for eventual implementation removal decision (v5.X+ if call count drops to ~0). Out of this patch's scope. |
| Schema migration for legacy memorize(is_protected=True)-without-_anchor-tag rows | Audit_anchors() naturally picks up new rows post-fix. Old rows stay as-is — caller-managed. Optional one-shot script in v5.X+ if needed. |

---

## Implementation order

1. **TDD scaffolding** — `yadgar/tests/test_memorize_anchor_parity.py` (failing tests covering all 7 cases above).
2. **`memorize()` upgrade** — add `reason: str = ""` kwarg; expand existing `is_protected=True` branch to set tier/valid_until/tags. ~30 LOC.
3. **`anchor()` docstring update** — purely docs.
4. **`sync_instructions` + `agent_dispatch_prelude` surface demote** — docs strings in `misc.py`.
5. **MIGRATION_NOTES.md** v5.10.x section explaining parity fix + that both surfaces now produce identical rows.
6. **CHANGELOG.md** entry.
7. **Version bump** — 5.10.1 → 5.10.x (depending on sequencing with v5.10.2 nightly-cycle hotfix).

---

## Acceptance criteria

- `pytest yadgar/tests/test_memorize_anchor_parity.py` green.
- Existing `yadgar/tests/test_anchor.py` + `yadgar/tests/test_memorize.py` + `yadgar/tests/test_audit_anchors.py` + `yadgar/tests/test_anchor_hygiene_schema.py` still green (no regression in old surface).
- `audit_anchors(directory, dry_run=True)` on a directory with a mix of `anchor()`-created + `memorize(is_protected=True)`-created rows surfaces BOTH in candidate analysis.
- I13 + I23 + I24 + I25 + VER lints exit 0.

---

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Existing callers of `memorize(is_protected=True, tags=[...])` without `_anchor` tag get implicit tag injection — semantic change | Document in MIGRATION_NOTES. Pattern is forward-compat: callers that already passed `_anchor` get NO change. Callers who didn't were already inconsistent — fix is silently correct. |
| Existing `memorize(is_protected=True)` rows in DB lack new defaults | Old rows stay as-is (no retroactive migration). Audit picks up new rows; old rows audit-invisible until manually cleaned. Acceptable. |
| `reason` kwarg on memorize() conflicts with future "reason" semantic | Field name matches anchor()'s. Document. If conflict emerges later, refactor to `anchor_reason` then. |
| `tier` auto-default surprises caller who wanted is_protected=True WITHOUT tier (legacy v5.7 caller pattern) | The pre-v5.8 row state IS `tier=NULL`. Migration_008 already auto-set old _anchor rows to `tier="conditional"`. So new memorize(is_protected=True) callers post-v5.10.x will get `tier="conditional"` — matches the migration default. Backwards-coherent. |

---

## Estimate

~30 LOC implementation + ~80 LOC tests + ~20 lines docs. 30-45 min agent dispatch.

---

## Sequencing options

| Option | Trade-off |
|---|---|
| **A: Fold into v5.10.2 nightly-cycle hotfix** | Two small bugs ship together. One release ceremony. Mixed scope (test infra + memory API). |
| **B: Standalone v5.10.3 after v5.10.2 ships** | Clean release notes. Slight overhead. |
| **C: Standalone v5.10.2 (rename current nightly-cycle plan to v5.10.3)** | Memorize parity is arguably higher user-facing impact than nightly cycle (sole user already verified backups work post-Tier-1). |

**Lean A** — both are surgical, both are bug fixes, both fit a single small train. Pure dispatch efficiency.

---

## Open / parked questions

- **`reason` kwarg on memorize()** — should it be keyword-only? **Lean: keyword-only.** Avoids accidental positional confusion with `tags`.
- **Auto-prepend `_anchor` to tags vs reject without it** — auto-prepend (advisor lean). Reject-style breaks back-compat for v5.7-era code.
- **Migration script for legacy memorize(is_protected=True)-without-_anchor-tag rows** — defer until audit telemetry shows the gap is large. Right now we don't know how many such rows exist in live DB.

---

## v5.X+ follow-up (deferred)

- Call-count telemetry: `yadgar_memorize_is_protected_invocations_total` + `yadgar_anchor_invocations_total`. Track ratio over months. If `anchor()` drops to near-zero, candidate for implementation removal in v5.X+ release. Otherwise keep as legacy shorthand indefinitely.
- One-shot migration script `scripts/migrate_legacy_protected_to_anchor.py` for pre-v5.10.x memorize(is_protected=True)-without-_anchor rows. Runs idempotent backfill matching v5.8 migration_008 pattern.
