# PLAN — v5.10.5: Nightly cycle remaining bugs (vacuum URL second site + prune logic)

**Status:** drafted 2026-05-29 evening after tonight's 19:00 UTC fire revealed v5.10.2 nightly-cycle fix was partial. Renumbered v5.10.4→v5.10.5 on 2026-05-30: v5.10.4 slot claimed by consolidate_now heavyweight fix.

**Master at draft time:** core v5.10.2 deployed + tagged.

**Sequencing:** v5.10.5 patch. Slots between v5.10.4 (consolidate_now heavyweight fix) and v5.10.6 (session-end capture).

---

## Why

Tonight's 2026-05-29 21:00 CEST (19:00 UTC) nightly-cycle fire (post-v5.10.2 deploy):

```
nightly_cycle pre_backup → ok
nightly_cycle stop_core_post → ok
nightly_cycle post_backup → ok
nightly_cycle prune → ok (removed 2)
nightly_cycle start_core → ok
nightly_cycle complete → ERROR (exit_code: 30)

[vacuum] ERROR: backend at http://127.0.0.1:8080 is not reachable: HTTP 307
```

Two unresolved bugs:

### Bug 1: vacuum URL `:8080` SECOND call site

v5.10.2 commit `1586dd4 fix(vacuum): use YADGAR_DB_URL in _log_consolidation_row not :8080 literal` fixed `_log_consolidation_row` site. The vacuum CLI path has ANOTHER `:8080` literal somewhere upstream that the v5.10.2 fix missed.

**Grep target:**
```bash
grep -rnE ':8080|127\.0\.0\.1:8080' yadgar/ --include='*.py' | grep -vE 'test_'
```

Probably in `yadgar/vacuum/__init__.py` or `yadgar/__main__.py::cmd_vacuum` — the CLI entry point that the nightly-cycle wrapper invokes. The error message format (`[vacuum] ERROR: backend at http://127.0.0.1:8080 is not reachable: HTTP 307`) tells us:
- It comes from a print-statement (matches the `[vacuum] ERROR:` prefix)
- Hard-coded URL string
- HTTP 307 = redirect, meaning something IS bound to :8080 but it's not the backend (backend on :8000)

### Bug 2: prune logic deletes just-created post snapshot

From tonight's journal:
```
event: "backup snapshot created: /home/max/.yadgar/surreal_db.nightly-post-2026-05-29-190041"
...
event: "backup pruned snapshot: /home/max/.yadgar/surreal_db.nightly-post-2026-05-29-190041"
event: "backup pruned snapshot: /home/max/.yadgar/surreal_db.nightly-pre-2026-05-27-094005"
removed: 2
```

The just-created `post-2026-05-29-190041` snapshot was deleted by prune. Prune deletes 2 entries: today's post + 2 days ago's pre. If retention is N=N then N+1 should be the deletion target — not today's post. Likely off-by-one in prune sort order OR pre/post name collision OR retention treating them as same-pool.

`scripts/migrate_v5_7_to_v5_8.py` not relevant here — this is `yadgar/backup.py::prune_snapshots`.

Possible fix: separate retention pools for `pre` vs `post` snapshots, OR sort by timestamp DESC + keep top-N regardless of suffix, OR keep latest pre/post pair always.

---

## What ships

1. **Bug 1 fix:** locate + replace second `:8080` literal with `YADGAR_DB_URL` env read (matches v5.10.2 pattern).
2. **Bug 2 fix:** prune logic refactor — keep latest N timestamp-sorted snapshots, treat pre+post as one pool. OR: keep latest N PAIRS (1 pre + 1 post per night).
3. **Tests:**
   - `yadgar/tests/test_vacuum_url.py` — add assertion that ANY vacuum code path reads from env, never hard-codes `:8080`.
   - `yadgar/tests/test_backup_prune.py` — extend to assert just-created snapshots not deleted by next prune call.
4. **Verification:** manual `systemctl --user start yadgar-nightly-cycle.service` — assert exit 0 + new snapshot persists.
5. **Version bump** 5.10.3 → 5.10.4 (after v5.10.3 ships first).
6. **MIGRATION_NOTES + CHANGELOG.**

---

## What does NOT ship

| Item | Why deferred |
|---|---|
| Refactor vacuum to use shared HTTP client | Scope creep; surgical literal-replace is enough. |
| Convert nightly cycle to container exec (PLAN_NIGHTLY_BACKUP_NIX_FIX Tier 2 Candidate 2) | v5.12.x train. |
| Strict exit code semantics (promote vacuum-fail from warn-only to fatal) | Was discussed in v5.7.0 PR-2 design. v5.X+ candidate. Out of scope for v5.10.4. |

---

## Implementation order

1. **Diagnose Bug 1.** Run the grep above. Identify file:line. Confirm via dry-run that env-read works.
2. **TDD test for Bug 1.** Assertion-only test against vacuum module — no literal `:8080` in production code paths.
3. **Fix Bug 1.** ~5 LOC.
4. **Diagnose Bug 2.** Read `yadgar/backup.py::prune_snapshots`. Identify sort + retention logic. Reproduce with fixture.
5. **TDD test for Bug 2.** Seed N+1 snapshots including just-created post; assert post not deleted.
6. **Fix Bug 2.** Probably ~15-30 LOC.
7. **Lint sweep** + check_versions.
8. **Release commit** + MIGRATION_NOTES + CHANGELOG.

---

## Acceptance criteria

- `grep -rnE ':8080|127\.0\.0\.1:8080' yadgar/ --include='*.py' | grep -vE 'test_'` returns ZERO lines (after Bug 1 fix).
- `pytest yadgar/tests/test_vacuum_url.py yadgar/tests/test_backup_prune.py` green.
- Manual `systemctl --user start yadgar-nightly-cycle.service` exits 0 (or ≠30); all 7 steps reach "ok" outcome.
- Tomorrow's 19:00 UTC fire: snapshot persists past midnight prune; consolidation + vacuum complete.
- I13 + I23 + I24 + I25 + I26 + VER lints exit 0.

---

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Prune fix retains too many snapshots (disk bloat) | YADGAR_BACKUP_RETENTION env knob (existing) caps at N=3 by default. Test with N=3. |
| Vacuum still fails for unrelated reason after URL fix | If yes — escalate to investigate full vacuum stack as v5.10.7 or v5.11 prereq. |
| The :8080 literal might actually be intentional (some other binding) | Verify via `ss -tlnp | grep 8080` what's bound there. If yadgar-backend is incorrectly bound to BOTH 8000 + 8080, address binding instead. |
| Backup retention semantics change breaks operator expectations | Document in MIGRATION_NOTES. Keep `YADGAR_BACKUP_RETENTION` as the knob. |

---

## Estimate

~30-50 LOC + ~60 LOC tests. 30-45 min agent dispatch.

---

## Sequencing

After v5.10.3 (scan script fix in flight). Possibly fold into v5.10.3 release if agent finishes quickly AND has spare context. Otherwise: standalone v5.10.4.

---

## Open / parked questions

- **Why :8080?** Check if any old config referenced it. Possibly stale from v4.x when backend used a different port.
- **Retention semantics** — keep latest N total OR keep latest N pre + N post separately? Lean: latest N pairs (1 pre + 1 post per fire) preserves round-trip integrity.
- **Should prune be transactional?** If process dies mid-prune, partial deletion. Acceptable for backups (idempotent re-run next cycle).
