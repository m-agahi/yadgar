# PLAN — v5.69.0 nightly-safety bundle (#44 / #45 / #43)

Status: **DONE 2026-06-17 (v5.69.0).** P0–P6 complete; P2–P5 shipped, P6
green-up done. BC-E1/E2/E3/F1/F2/F3 ✅ (e2e-green). BC-D1 DEFERRED — surrealdb
SDK 2.0.0 / server 3.0.5 surrealkv format skew blocks embedded consolidation;
tracked as an SDK/server-alignment follow-up (its own release). BC-D3 resolved
via CPython 3.14.4 (no dedicated e2e, status stays ❌ until a test proves it).
Highest-priority track — a vacuum bug caused real
data loss on 2026-06-16 (3622 memories). Bundles three board tasks into one
release (team prefers all-in-one). Depends on the v5.68 e2e harness (now on
master). Drives every fix from a RED behavior-contract e2e test first
(real-path, isolated temp `YADGAR_DATA_DIR`, no bending).

## Corrected root causes (from the Opus plan, against HEAD 5.68)

1. **Vacuum already has deferred-rename + rollback** (`yadgar/vacuum/__init__.py
   ::_vacuum_restart_and_import`): renames `surreal_db → surreal_db.bloated-<ts>`,
   starts backend empty, `POST /import`, and on import failure `_restore_db`
   renames `.bloated` back. The RESIDUAL gap (the 06-16 failure mode): during the
   import window the canonical path is a fresh **empty** DB, and if `_restore_db`
   **itself** fails (the systemctl/D-Bus failure that hit its backend stop/start),
   the empty dir stays live and the original is stranded at `.bloated`.
2. **Nightly "exit 30" is NOT a D-Bus error — it's a unit-coupling bug.**
   `yadgar.service` declares only weak `After`+`Wants` on `yadgar-backend`
   (not `BindsTo`/`PartOf`), and the nightly cycle stops only `yadgar`, never
   `yadgar-backend`. So the backend keeps the surrealkv file lock → nightly step 3
   pops `YADGAR_DB_URL` and opens `StorageEngine` **embedded** → lock contention →
   consolidation fails (exit 30). The same live-held dir → `create_snapshot`
   copytree captures a torn backup. **This unifies #43 and #45.**
3. **Latent harness defect:** the e2e `service_stub` patches
   `nightly_cycle._stop_service` / `._start_service` which **don't exist** (real
   seam: `_step_stop_core` → `_run_systemctl`); the `hasattr` guard silently
   no-ops. Phase-0 blocker — must align the seam before any BC-D/E3 test is
   trustworthy.

## Phases

- **P0 — seam alignment (blocking, no behavior change).** Add real, patchable
  `_stop_service(unit)` / `_start_service(unit)` wrappers in
  `yadgar/scripts/nightly_cycle.py`, route `_step_stop_core`/`_step_start_core`
  through them; also have the e2e `service_stub` patch `ops.ServiceController`
  (vacuum's service path). Only the host service boundary (systemctl/podman) is
  stubbed — never the DB code path.
- **P1 — RED e2e** (`yadgar/tests/e2e/test_vacuum_backup_safety.py`):
  - BC-E1 vacuum preserves per-table row counts.
  - BC-E2 atomicity — inject failure at `/import` **and** make the restore's
    service-control raise (simulate 06-16). Assert canonical `surreal_db` still
    exists, **non-empty**, original rows queryable. RED today.
  - BC-E3 sensitive-job lock blocks an EXTERNAL shutdown mid-vacuum (vacuum's own
    stop still allowed). RED today.
  - BC-F1 backup restores to same row count.
  - BC-F3 (NEW) backup taken under concurrent writes restores **self-consistent**
    (no torn surrealkv segment). RED today.
- **P2 — atomic vacuum (#44a). DONE.** Build the compacted DB in a side path on an alt
  port, verify row counts, stop the throwaway surreal (release lock), then atomic
  same-dir `os.replace` swap so the canonical path is never empty/partial.
  `_restore_db` becomes a thin fallback. (M2 hardening: the side build writes
  UNVERIFIED content under `surreal_db.building-<ts>` and is renamed to
  `surreal_db.new-<ts>` ONLY after the exact-count verify + clean stop — so "a
  `.new-*` exists" structurally means verified-complete; crash-recovery promotes
  `.new` without re-verifying and must NEVER promote a `.building-*`.)
  RISK: surrealkv portability of a freshly-closed dir under `os.replace` — prove
  in BC-E2; fallback = import-into-canonical-after-export-verify. Same-fs only;
  preflight free-space check (peak disk doubles).
- **P3 — sensitive-job lock + signal drain (#44b). DONE** (`yadgar/sensitive_lock.py`). Lock file under
  `YADGAR_DATA_DIR` (atomic write-tmp-`os.replace`, payload {job,pid,started_at},
  TTL + PID-liveness reaping). Extend `server/lifecycle.py::_signal_handler` to
  drain/refuse external SIGTERM while the lock is held. HARD AMBIGUITY: the vacuum
  itself stops core via the same SIGTERM — distinguish vacuum-initiated (lock owned
  by live vacuum pid → authorized drain) from operator stop. Lock-pid approach for
  5.69 (narrow race documented); systemd `RefuseManualStop` as a follow-up.
- **P4 — quiesced backup (#45). DONE.** `create_snapshot` from a consistent point —
  prefer `GET /export` (shares the vacuum export path) OR stop-both-units then
  copytree. Re-validate BC-F2. NOTE: the host `cp -r` backup lives in the external
  home-manager module (`yadgar-backup-snapshot`) — same torn bug, flag for a
  separate deploy fix, out of this repo's tree.
- **P5 — nightly stop BOTH units (#43). DONE** (fixes the exit-30 unit-coupling half). `_step_stop_core`/`_step_post_backup`
  stop `yadgar` AND `yadgar-backend` → releases the surrealkv lock → kills the
  exit-30 contention AND enables a consistent copytree. Add bounded retry around
  `_run_systemctl`. Optional/split: `flake.nix` `BindsTo`/`PartOf` coupling
  (deploy blast radius — separate PR).
- **P6 — verify + green-up. DONE.** `.venv` python confirmed 3.14.4 — the
  `_asyncio` finalize SEGV (CPython 3.14.3 bug) is gone; BC-D3 left ❌ (no
  dedicated e2e asserts clean exit). Flipped BC-E1/E2/E3/F1/F2/F3 → ✅ referencing
  `tests/e2e/test_vacuum_backup_safety.py`. BC-D1 NOT flipped — surrealdb SDK
  2.0.0 / server 3.0.5 surrealkv skew blocks embedded consolidation; e2e ships
  skipped, deferred to a follow-up. Bumped `pyproject.toml` → 5.69.0.
  `make e2e` green (e2e safety file: 10 passed, BC-D1 skipped).

## Anti-bending / data-safety
Real vacuum/backup code against real embedded surreal in temp `YADGAR_DATA_DIR`;
`_assert_not_real_data_dir` guard MUST stay; only the host service boundary is
stubbed. Never touch `~/.local/share/yadgar`. RED-first; weakening an assertion
to go green is rejected.

## Splits / follow-ups
- BC-D3 SEGV → own ticket if it survives 3.14.4.
- `flake.nix` systemd coupling + external host-backup `cp -r` → separate deploy PR.

## Related
- `[[db-audit-fix]]`, the v5.68 e2e net, `docs/BEHAVIOR_CONTRACT.md` BC-E*/F*/D*.
- Source plan: agent af8da614 (2026-06-17). Board tasks #43/#44/#45 → #50.
