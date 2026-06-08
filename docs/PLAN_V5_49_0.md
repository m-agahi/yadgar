# PLAN — v5.49.0: Upgrade Orchestrator + Memory Archive Retention

**Status:** drafted 2026-06-08. Bundles two strands the user explicitly merged into one release. **READY for impl** (Phase-0 audit-anchor extension already drafted; pipx file-replacement repro completed 2026-06-08 — see § 1.B).

**Strand A — Memory archive retention.** Carry-over of the original 2026-06-02 archive-retention plan (DPs A–E resolved; phase 0 `audit_anchors` extension already drafted). Backstop for the ~1300 heat=0 archives accumulating in `memory_archive` until v6 LLM curator ships.

**Strand B — Upgrade orchestrator (NEW, replaces the original v5.48 "graceful-restart" deferral framing).** Empirically verified 2026-06-08 that the v5.48 deferral note for `--install` is OBSOLETE for the v5.45+ container architecture: `pipx upgrade yadgar` against the live container daemon left it uninterrupted (uptime monotonic, no restart event). The real v5.48 `--install` blockers are different — see § 1.B below. v5.49 ships an upgrade-orchestrator that coordinates host CLI + container image + systemd unit + atomic rollback, with graceful-stop as a sub-task.

**Version bump:** 5.48.0 → 5.49.0 (split-version policy: core 5.49.0; backend stays 5.4.0 unless rebuild required — Phase 12 cross-cutting includes the `check-backend-bump` gate from existing pre-commit invariants).

**Branch:** `feat/v5.49.0-bundle` off master (long-lived integration branch per v5+ convention).

**Effort estimate:** 12–15 calendar days bundled (Strand A: 1.5–2d, Strand B: 10–12d — orchestrator state machine + rollback alone is ~3d; sd_notify/Type=notify migration adds another).

---

## 1. Background

### 1.A — Archive retention (carry-over)

2026-06-01 user observation — viz reports total > visible by ~1300 memories. `memory_archive` table has zero permanent-deletion policy:

- `prune_old_rows()` allowlist EXCLUDES `memory_archive` (`yadgar/storage/ops.py:110-118`)
- Schemaless table, no TTL (`yadgar/storage/migrations.py:434`)
- Cascade delete only fires when parent memory explicitly DELETEd (`yadgar/storage/memory.py:290-336`)
- v6 LLM curator was planned to handle scope-limited deletion — not shipped

### 1.B — Upgrade orchestrator (NEW)

v5.48 plan deferred `yadgar update --install` to v5.49 citing "pipx upgrade kills daemon mid-call." Two empirical repros 2026-06-08:

**Repro 1 — container daemon isolation.** Ran `pipx upgrade yadgar` against the live daemon:

```
$ podman ps --filter name=yadgar
yadgar          Up 16 hours    127.0.0.1:8765->8765/tcp     # unchanged
$ yadgar daemon status
Uptime: 59286.5s                                            # monotonic, no restart
```

Daemon container filesystem is disjoint from host pipx venv — pipx cannot touch container state. Confirmed.

**Repro 2 — host CLI file replacement race.** Built a throwaway venv with `yadgar==5.48.0` from PyPI, started a long-running Python process holding `import yadgar`, then ran `pip install --force-reinstall --no-deps yadgar==5.48.0` mid-flight:

```
[child] PID 2445941, starting
[child] initial import OK, version 5.48.0
[child] yadgar.config pre-loaded
[child] sleeping 12s — REPLACE FILES NOW         ← reinstall ran during this window
[child] post-replacement: attempting lazy import of yadgar.cli.update
[child] lazy import SUCCESS                                ← not-yet-loaded module imports fine
[child] post-replacement: attempting fresh import of yadgar.cli.daemon
[child] lazy import SUCCESS
[child] attempting reload of pre-loaded yadgar.config
[child] reload SUCCESS                                     ← importlib.reload works
[child] DONE                                               ← exit code 0
```

Linux open-inode semantics + Python's "read directory entry at import time" protect the running process. **Crash from `pipx upgrade` mid-CLI is not the failure mode.**

**Finding:** v5.48 deferral framing was written for a pre-v5.45 host-venv daemon that no longer exists, and overstated the host-CLI race even for the host-process case. Real risks are subtler:

**Real `--install` blockers (v5.45+ architecture):**

1. **Cross-version SKEW in the CLI process** (not crash). When `pipx upgrade` swaps `5.48.0 → 5.49.0`, already-loaded modules stay on 5.48.0 code (open-inode pinned); modules imported AFTER the swap load 5.49.0 code from disk. Mixed in-process state can produce silent wrong-version behaviour. Mitigation: `os.execvp` re-exec to fresh `yadgar update --finalize` process after pipx completes (B6).
2. **Container image not auto-pulled.** `pipx upgrade` updates only host CLI. Daemon stays on the old image tag (`docker.io/openfantasy/yadgar:<old>`).
3. **systemd unit ExecStart bakes the image tag.** `scripts/install/yadgar.service.in` references the tag literally. Routine upgrades currently need either unit-file rewrite + `daemon-reload` + restart, OR (preferred — see B2 below) an `EnvironmentFile`-carried tag so routine upgrades only rewrite the env file.
4. **No atomic rollback.** If container pull fails or new image fails health-check, the CLI is half-upgraded with no automated revert.

v5.49 ships the coordination layer that closes all four gaps. Graceful HTTP shutdown (in-flight drain, queue barrier, embed-cache snapshot) becomes a sub-task feeding the restart step, not the headline.

---

## 2. Resolved decisions

### Strand A — Archive retention (user-confirmed 2026-06-02)

| DP | Decision | Rationale |
|---|---|---|
| **A — default retention** | **90 days** | 3-month grace. User-tunable via `MEMORY_ARCHIVE_RETENTION_DAYS`. |
| **B — delete strategy** | **Hard delete + vacuum-snapshot recovery** | DELETE row. Existing snapshots in `~/.yadgar/archive/` provide the recovery path. |
| **C — circuit breaker** | **500 / cycle (CRITICAL log + tunable)** | Bulk cleanup cap. Higher than v6 curator's 20/night. |
| **D — anchor exclusion** | **Skip `is_protected=true` AND any `_anchor`-tagged memory** | Covers legacy anchored memories that lost `is_protected=true`. Also skip legacy `anchor` no-underscore tag. |
| **E — thrash protection** | **Skip if memory `created_at` <7d ago** | Recent creation = re-archival in flight. |

Also: `archived_at` is the age anchor (not `created_at`). `migration_grace=true` excluded until grace deadline (PD-23). `_active_work` blocks not affected (different table).

### Strand B — Upgrade orchestrator (user-confirmed 2026-06-08)

| DP | Decision | Rationale |
|---|---|---|
| **B1 — coordination model** | **CLI-driven orchestrator (not in-daemon)** | Host CLI sees the upgrade-then-restart sequence end-to-end; daemon process gets replaced. Avoids the chicken-and-egg of self-restarting. |
| **B2 — order of operations** | **Pull image → snapshot → write env-file (new tag) → graceful-stop → restart → health-check → host CLI upgrade last → re-exec to --finalize** | Container image change is the riskiest step; do it before the CLI is swapped so the old CLI can rollback. Host CLI swap is last because it's least likely to fail and most easily reverted. Routine upgrades touch only the env-file (carrying `YADGAR_IMAGE_TAG`), NOT the unit file itself — see B3 + § 3.B. Unit-file mutation is reserved for one-time `Type=simple` → `Type=notify` migration during `make setup`. |
| **B3 — systemd Type** | **`Type=notify` with sd_notify READY=1 / STOPPING=1** | Lets systemd know precisely when the daemon is ready or shutting down, instead of timing it out. Container needs `--sdnotify=container` or in-process notify socket; spec'd in § 3.B. |
| **B4 — graceful stop primitive** | **New `yadgar daemon graceful-stop [--timeout=30]`** | Signals daemon, waits for HTTP drain + queue barrier + embed cache snapshot, then exits 0. Used by orchestrator AND directly by ops. |
| **B5 — rollback** | **Snapshot of previous image tag + env-file + (only for one-time migration) unit file.** | Routine upgrade rollback = restore old `YADGAR_IMAGE_TAG` in env-file + `systemctl restart`. One-time `Type=simple` → `Type=notify` migration also snapshots the unit file. On post-restart health-check failure, automatic revert. Post-RE_EXECING failures are NOT auto-rolled — see § 8 risks. |
| **B6 — host CLI self-upgrade safety** | **`os.execvp` exec to new entry-point after pipx upgrade** | After `pipx upgrade` completes, orchestrator re-execs `yadgar update --finalize` so the new CLI handles the last-step health verification. Prevents mismatched-bytecode lazy-import race. |
| **B7 — launchd parity (macOS)** | **`ExitTimeOut=30` + soft-kill SIGTERM honored** | launchd has no `Type=notify` analog. Use `ExitTimeOut` + graceful-stop in CLI; document the gap. |
| **B8 — ships off by default** | **`update.install_enabled: false`** | First release; users opt in after dry-run. Same posture as v5.48 auto-check. |

---

## 3. Scope

### 3.A — Archive retention (carry-over)

#### Config knobs (I25 registered)

- `MEMORY_ARCHIVE_RETENTION_DAYS: int = 90` — 0 disables retention entirely.
- `MEMORY_ARCHIVE_RETENTION_CIRCUIT_BREAKER: int = 500` — max purges per cycle. CRITICAL log if hit.
- `MEMORY_ARCHIVE_RETENTION_THRASH_GUARD_DAYS: int = 7` — skip purge if `created_at` younger than this.

#### Storage layer

`yadgar/storage/ops.py`:
- New `purge_expired_archives(dry_run: bool = False) -> dict`
- Returns `{candidates, purged, skipped_protected, skipped_anchor, skipped_recent, circuit_breaker_hit}`
- Query (SurrealQL):

```surql
SELECT * FROM memory_archive
WHERE archived_at < (time::now() - $retention_days * 24h)
  AND is_protected != true
  AND NOT array::contains(tags, '_anchor')
  AND NOT array::contains(tags, 'anchor')
  AND created_at < (time::now() - $thrash_guard_days * 24h)
  AND (migration_grace != true OR valid_until < time::now())
LIMIT $circuit_breaker
```

- DELETE matched rows unless `dry_run=True`.

#### Consolidation phase

`yadgar/consolidation/cleanup.py:184-211`:
- Extend `_run_retention_tasks()` to call `purge_expired_archives()` when `MEMORY_ARCHIVE_RETENTION_DAYS > 0`.
- Telemetry: `yadgar_archive_purged_total` counter, `yadgar_archive_retention_skipped` counter w/ `reason` label.

#### MCP tool: `archive_purge`

`yadgar/server/tools/admin_archive.py` (new):
- `archive_purge(dry_run: bool = True, retention_days: int | None = None)` — `power=True`, secret-gated.
- `dry_run=True` (default): returns expected count + sample of 10 affected slugs.
- `dry_run=False`: performs purge. Circuit breaker enforced.

### 3.B — Upgrade orchestrator

#### New files

| Path | Purpose |
|---|---|
| `yadgar/cli/update.py` (extend existing) | Add `--install` action gated by `update.install_enabled: true`. Orchestrates: PyPI probe → image pull → unit rewrite → graceful-stop → restart → health-check → CLI pipx upgrade → re-exec. |
| `yadgar/update/orchestrator.py` | Pure-Python orchestrator: stepwise execution + rollback state machine. Records each step's "undo" before mutation. |
| `yadgar/update/snapshot.py` | Pre-upgrade snapshot: current image tag, unit-file contents, host CLI venv-state marker. Written to `~/.yadgar/upgrade-snapshots/<timestamp>/`. |
| `yadgar/cli/daemon_graceful.py` (or extend `yadgar/cli/daemon.py`) | `yadgar daemon graceful-stop [--timeout=30] [--finalize-hook=]` subcommand. |
| `yadgar/server/lifecycle.py` (extend) | Add `drain_in_flight_requests(timeout)` helper. Counts active HTTP requests, waits for completion w/ deadline. Wired into shutdown lifespan. |
| `yadgar/file_queue/queue.py` (extend `.stop()`) | Explicit `flush_barrier(timeout)` method. Waits for in-memory queue to fully apply or hit deadline before returning. |
| `yadgar/sd_notify.py` | Minimal sd_notify helper (READY=1, STOPPING=1, RELOADING=1, MAINPID, WATCHDOG=1). No external dep — write to `$NOTIFY_SOCKET` directly. |
| `scripts/install/yadgar.service.in` (rewrite, one-time migration) | `Type=notify`. `NotifyAccess=all`. `EnvironmentFile=-%h/.yadgar/upgrade.env` (carries `YADGAR_IMAGE_TAG`; leading `-` so missing file is non-fatal during first migration). ExecStart references `${YADGAR_IMAGE_TAG}` env var, not literal. `ExecReload=` (config-only reload; future use). `TimeoutStartSec=120`. `TimeoutStopSec=45`. |
| `~/.yadgar/upgrade.env` (orchestrator-managed, runtime) | Single-line `YADGAR_IMAGE_TAG=docker.io/openfantasy/yadgar:5.49.0`. Atomically rewritten by orchestrator on routine upgrade. Snapshotted before mutation. |
| `scripts/install/launchd/com.openfantasy.yadgar.plist.in` (extend) | Add `ExitTimeOut: 30`, `EnvironmentVariables` carries `YADGAR_IMAGE_TAG`. |
| `yadgar/tests/test_upgrade_orchestrator.py` | Tests for orchestrator state machine, rollback paths, graceful-stop barrier semantics, snapshot/restore. |
| `yadgar/tests/test_sd_notify.py` | Tests for sd_notify helper (mock socket). |

#### Modified files

| Path | Change |
|---|---|
| `yadgar/cli/update.py` | Wire `--install` action (currently raises `NotImplementedError` per v5.48 ship). Delegates to `orchestrator.run_install()`. |
| `yadgar/daemon.py` | (a) Pass `YADGAR_IMAGE_TAG` from `~/.yadgar/upgrade.env` to systemd unit; (b) Emit sd_notify READY=1 once container health-check passes; (c) On shutdown, emit sd_notify STOPPING=1 before exit. |
| `yadgar/server/_app.py` | Lifespan shutdown calls `drain_in_flight_requests(timeout=30)` before returning. |
| `yadgar/config_yaml.py` | New `update.install_enabled: bool = false` + `update.snapshot_retention: int = 3`. Three-way sync (I25). |
| `pyproject.toml`, `server.json` | Version bump 5.48.0 → 5.49.0 (core); backend stays unless rebuild required. |

#### Orchestrator state machine (`yadgar/update/orchestrator.py`)

Two modes — one-time migration (run by `make setup` / orchestrator only if existing unit is `Type=simple`) and routine upgrade (every `yadgar update --install` call).

```
ONE-TIME MIGRATION (rare; runs at most once per host):
  IDLE → DETECTING_UNIT_STYLE
       → (if Type=notify already) → SKIP
       → (if Type=simple) → SNAPSHOTTING_UNIT
       → REWRITING_UNIT → DAEMON_RELOADING
       → MIGRATED

ROUTINE UPGRADE (every --install):
  IDLE → ACQUIRING_LOCK → PROBING_PYPI → SNAPSHOTTING
       → PULLING_IMAGE → WRITING_ENV_FILE
       → GRACEFUL_STOPPING → RESTARTING_SERVICE
       → HEALTH_CHECKING → CLI_UPGRADING
       → RE_EXECING → FINALIZING (via new CLI process)
       → DONE

  on failure at any state ≥ PULLING_IMAGE and ≤ HEALTH_CHECKING:
    → ROLLING_BACK_DAEMON → ROLLED_BACK_OK | ROLLED_BACK_FAILED

  rollback order (routine upgrade):
    1. Restore previous YADGAR_IMAGE_TAG in env-file from snapshot
    2. systemctl --user restart yadgar.service       (boots old image; no daemon-reload needed)
    3. Wait health-check on old image
    4. (CLI not yet upgraded → nothing to revert on host)

  on failure at CLI_UPGRADING (pipx upgrade failed, but new image is healthy):
    → ROLLING_BACK_CLI_ONLY → attempts pipx install --force yadgar==<prev>
    → terminal: DONE_CLI_ROLLBACK_OK or DONE_CLI_ROLLBACK_FAILED
    (Daemon stays on new image. Documented as "operator may pin CLI manually.")

  on failure at FINALIZING (post-RE_EXECING; new CLI process fails health verify):
    → terminal: DONE_BUT_FINALIZE_FAILED
    Auto-rollback NOT attempted — at this point daemon is healthy on new image,
    new CLI is installed, only the verification handshake failed. Manual recovery:
    operator runs `yadgar update --rollback` (separate subcommand; pulls prev tag
    from latest snapshot in ~/.yadgar/upgrade-snapshots/).
```

**File lock semantics** (`~/.yadgar/upgrade.lock`):

- Lock content = `{"pid": <int>, "start_ts": <float>, "version_from": "5.48.0", "version_to": "5.49.0"}`.
- Before acquiring: read existing lock if present; if `kill -0 <pid>` succeeds AND lock age < `update.lock_max_age_seconds` (default 3600), refuse with "concurrent upgrade in progress (pid <pid>, started <Xs> ago)".
- Otherwise treat as stale, atomic-replace, proceed.
- Released by orchestrator on terminal state (DONE / ROLLED_BACK_*); not released on hard process kill (relies on stale-detection on next run).

#### Graceful stop barrier semantics

`yadgar daemon graceful-stop --timeout=30`:

1. Send sd_notify STOPPING=1 (signals systemd we're shutting down cleanly).
2. Close HTTP listener (no new connections accepted; existing kept).
3. Loop until either (a) in-flight HTTP request count = 0, or (b) timeout: 50% deadline.
4. Call `_queue_drainer.flush_barrier(timeout = 30% remaining deadline)`. Returns true if drained, false if deadline hit.
5. Call `embed_service.snapshot_caches()` (sync; already exists in lifespan but called explicitly here).
6. Call `storage_engine.close()` (closes SurrealDB connections gracefully).
7. Exit 0 on clean drain. Exit 1 if any step hit timeout — caller logs WARNING but proceeds (systemd will SIGKILL on TimeoutStopSec hit anyway).

#### Snapshot/rollback artifact

`~/.yadgar/upgrade-snapshots/2026-06-08T19-42-00Z/`:

```
prev_image_tag                    # "docker.io/openfantasy/yadgar:5.48.0"
prev_unit_file                    # full content of yadgar.service before mutation
prev_cli_version                  # "5.48.0"
forward_log.json                  # state transitions w/ timestamps
rollback_log.json                 # only present if rollback fired
```

Retention: keep last `update.snapshot_retention` (default 3) snapshots; older purged on next successful upgrade.

---

## 4. Non-goals

- **Strand A:** no soft-delete state machine, no retroactive UI for restoring purged memories, not changing heat decay, not changing `COLD_THRESHOLD`, not building v6 LLM curator.
- **Strand B:** no true zero-downtime socket handoff (container restart is fundamentally a brief gap; we minimise but don't eliminate — sd_notify lets systemd manage the gap precisely). No multi-version coexistence. No SBOM verification on update (advisory only; v5.50+ candidate). No phone-home telemetry (only PyPI probe per v5.48 contract).

---

## 5. Test plan (TDD — failing tests first)

### Strand A — Archive retention (19 tests)

`yadgar/tests/test_archive_retention.py`:

1. `test_purge_respects_retention_age`
2. `test_purge_skips_protected`
3. `test_purge_skips_anchor_tag`
4. `test_purge_skips_legacy_anchor_no_underscore` *(new)*
5. `test_purge_skips_recent_creation`
6. `test_purge_skips_migration_grace`
7. `test_purge_migration_grace_after_expiry`
8. `test_circuit_breaker_caps_purge_count`
9. `test_circuit_breaker_returns_indicator`
10. `test_dry_run_no_delete`
11. `test_retention_disabled`
12. `test_nightly_cycle_invokes_purge`
13. `test_metrics_emitted`
14. `test_archive_purge_dry_run_default`
15. `test_archive_purge_explicit_run`
16. `test_archive_purge_retention_override`
17. `test_archive_purge_power_gated`
18. `test_archive_purge_secret_gated`
19. `test_three_config_knobs_registered` (I25)

### Strand B — Upgrade orchestrator (21 tests)

`yadgar/tests/test_sd_notify.py`:

20. `test_notify_ready_writes_to_socket` — mock socket; assert `READY=1` payload.
21. `test_notify_stopping_writes_to_socket` — same w/ `STOPPING=1`.
22. `test_notify_no_socket_env_noop` — `$NOTIFY_SOCKET` unset → silent no-op.

`yadgar/tests/test_graceful_stop.py` (extend existing `test_graceful_shutdown.py`):

23. `test_graceful_stop_waits_for_in_flight` — inject 3 long-running requests; assert stop blocks until they complete.
24. `test_graceful_stop_honors_timeout` — single 60s request, timeout=5s; assert stop returns within 5s.
25. `test_graceful_stop_flushes_queue` — queue 10 items; assert all applied before stop returns.
26. `test_graceful_stop_snapshots_embed_cache` — assert snapshot file written.

`yadgar/tests/test_upgrade_orchestrator.py`:

27. `test_orchestrator_happy_path` — mock all external calls; assert state transitions IDLE → DONE.
28. `test_orchestrator_image_pull_fail_rollback` — mock pull failure; assert ROLLING_BACK → ROLLED_BACK_OK.
29. `test_orchestrator_health_check_fail_triggers_rollback` — mock health-check fail post-restart; assert old image restored.
30. `test_orchestrator_cli_upgrade_fail_attempts_pipx_force_install_prev` — mock pipx upgrade fail post-image; assert pipx revert attempted.
31. `test_orchestrator_snapshot_written` — assert `prev_image_tag`, `prev_unit_file`, `prev_cli_version` present.
32. `test_orchestrator_snapshot_retention` — set retention=2; create 5 snapshots over 5 runs; assert only 2 most recent remain.
33. `test_orchestrator_install_disabled_refuses` — `update.install_enabled: false`; assert orchestrator refuses + prints opt-in instructions.
34. `test_orchestrator_concurrent_run_blocked` — file-lock; assert second invocation refuses while first runs (process alive via `kill -0`).
35. `test_orchestrator_stale_lock_taken_over` — lock file present but PID dead (or older than `lock_max_age_seconds`); assert orchestrator overwrites lock and proceeds.
36. `test_re_exec_invokes_finalize_subcommand` — mock `os.execvp`; assert new CLI process called with `update --finalize`.
37. `test_finalize_subcommand_verifies_new_version_running` — assert `--finalize` checks `/health` returns new version.
38. `test_finalize_failure_logs_manual_recovery_hint` — mock `--finalize` health-check failure; assert exit non-zero + log includes `yadgar update --rollback` recovery hint (no auto-rollback at this terminal state).
39. `test_update_rollback_subcommand` — given a snapshot dir, assert `yadgar update --rollback` restores previous image tag in env-file + restarts daemon.

`yadgar/tests/test_systemd_unit_template.py`:

40. `test_unit_template_has_type_notify` — render unit file; assert `Type=notify`.
41. `test_unit_template_uses_image_tag_env_var` — assert `ExecStart` references `${YADGAR_IMAGE_TAG}`, not literal tag.
42. `test_unit_template_has_timeoutstopsec` — assert `TimeoutStopSec=45`.
43. `test_unit_template_environmentfile_optional_prefix` — assert `EnvironmentFile=-` prefix so missing env file doesn't fail unit start.

### Strand B — Integration (manual / opt-in)

44. (manual) `make upgrade-test` — runs orchestrator end-to-end against `:dev-test` image tag in a throwaway pipx venv. Documented in `MIGRATION_NOTES.md`.

**Total: 44 tests** (19 archive + 25 orchestrator).

---

## 6. Acceptance criteria

### Strand A

1. 3 archive config knobs registered three-way (I25).
2. `purge_expired_archives()` with all 5 DPs enforced.
3. Nightly consolidation invokes purge.
4. `archive_purge` MCP tool (power + secret gated, dry_run default True).
5. 19 archive tests green.
6. 2 new counters (`yadgar_archive_purged_total`, `yadgar_archive_retention_skipped{reason}`).
7. Operator dry-run on user prod: confirm ~1300 candidate count.

### Strand B

8. `yadgar daemon graceful-stop` exists, exits 0 on clean drain, 1 on timeout.
9. sd_notify READY=1 emitted on daemon ready; STOPPING=1 on shutdown initiation.
10. Systemd unit template uses `Type=notify` + env-var image tag.
11. `yadgar update --install` runs the orchestrator end-to-end when `update.install_enabled: true`.
12. Rollback restores prior image + unit + (best-effort) prior CLI version on any post-image-pull failure.
13. Snapshot artefacts written to `~/.yadgar/upgrade-snapshots/`; retention enforced.
14. 25 orchestrator tests green.
15. Manual `make upgrade-test` recipe documented + passes against a throwaway venv.

### Cross-cutting

16. All existing tests still pass.
17. CHANGELOG + MIGRATION_NOTES + README + AGENTS.md + config docs updated.
18. Pre-commit invariants (I13/I23/I24/I25/I26/I27/I28) clean.

---

## 7. Rollout

### Strand A

1. Ship v5.49.0 with `MEMORY_ARCHIVE_RETENTION_DAYS=0` (auto-purge OFF).
2. User runs `archive_purge(dry_run=True)` → validates candidate set.
3. User runs `archive_purge(dry_run=False)` → one-time cleanup of ~1300 backlog.
4. User sets `MEMORY_ARCHIVE_RETENTION_DAYS=90` → enables nightly auto-purge.

### Strand B

1. Ship v5.49.0 with `update.install_enabled: false` (orchestrator OFF; `--install` still refuses).
2. User runs `yadgar update --check` (existing v5.48 path) to confirm new version available.
3. User sets `update.install_enabled: true` + reads `MIGRATION_NOTES.md` rollback section.
4. User runs `yadgar update --install`.
5. Health-check verifies new daemon. Snapshot retained for 3 successful upgrades.

**Rationale for both ship-off-by-default:** archive auto-purge could nuke 1300 rows in one cycle; orchestrator does a multi-step infra mutation. Both need explicit user opt-in after dry-run.

---

## 8. Risks

### Strand A

- Aggressive default risks data loss. Mitigation: ships disabled; user opts in after dry-run.
- Re-archival thrash. Mitigation: DP-E 7-day thrash guard.
- v6 curator collision. Mitigation: v5.49 = backstop; v6 LLM proposes earlier deletes within 20/night cap.
- SurrealDB DELETE creates vlog garbage. Existing vacuum schedule covers.
- Anchored-by-prose-only memory slips through. Mitigation: Phase 0 `audit_anchors` extension detects + reports.

### Strand B

- **Systemd unit rewrite leaves user with a broken unit if rollback also fails.** Mitigation: snapshot unit file BEFORE any mutation; document manual recovery in MIGRATION_NOTES; `make setup` fully regenerates.
- **`pipx upgrade` succeeds but new CLI re-exec crashes.** Mitigation: B6 `--finalize` subcommand is the single failure surface; if it crashes, daemon is already on new image and healthy — user runs `yadgar update --rollback` (separate subcommand reading the latest snapshot). Documented in MIGRATION_NOTES.
- **PyPI rollback to prior version may fail if version was yanked.** PyPI does not auto-yank but PD-45 internal-dev no-tag policy means some intermediate versions may not exist on PyPI at all. Mitigation: orchestrator's CLI rollback step is best-effort — log WARNING + continue if `pipx install --force yadgar==<prev>` fails. User can pin manually via `pipx install --force <stored-wheel>` (orchestrator could cache last-N wheels in snapshot dir for true local rollback; deferred to v5.50+).
- **sd_notify socket not present** (non-systemd init, e.g. runit, OpenRC). Mitigation: B3 falls back to time-based readiness (current behavior); document the degradation.
- **Type=notify wedges if daemon never sends READY=1.** Mitigation: T22 test ensures notify path exists; TimeoutStartSec=120 in unit; document recovery (systemctl --user reset-failed).
- **Image pull network failure mid-upgrade.** Mitigation: orchestrator state machine catches at PULLING_IMAGE; rollback is a no-op (nothing mutated yet); user retries.
- **Concurrent `yadgar update --install` from two terminals.** Mitigation: file lock on `~/.yadgar/upgrade.lock`; T34 test.
- **launchd ExitTimeOut not honored on user-quit.** Mitigation: document the macOS gap; v5.50+ candidate to investigate launchd plumbing further.

---

## 9. Dependencies

- **Strand A:** none hard. v5.48 already shipped; archive retention is purely additive.
- **Strand B — internal:**
  - v5.48 update CLI + PyPI probe (shipped) — orchestrator extends.
  - v5.45+ container architecture (shipped) — orchestrator targets this surface.
  - Existing uvicorn graceful-shutdown timeout + lifespan hooks (shipped v5.3.9).
  - Existing file_queue drainer `.stop()` (shipped; clarify barrier semantics in T25).
  - Existing embed-cache snapshot on shutdown (shipped).
- **Strand B — external:**
  - `pipx` ≥ 1.0 (any modern version).
  - `podman` or `docker` 4.0+ (already required by current daemon).
  - systemd ≥ 234 (`Type=notify` + `NotifyAccess=all` widely available since 2017).

---

## 10. Phases (agent dispatch)

### Strand A

0. **`audit_anchors` extension — anchored-by-prose detection.** Extend `yadgar/server/tools/anchors.py::audit_anchors` to detect memories with: `_anchor` tag absent + `is_protected=false` + heat=0 + present in `memory_archive`. 4 tests. → COMMIT `feat(anchors): detect anchored-by-prose-only memories at-risk from v5.49 retention`
1. **Storage function + RED tests.** `purge_expired_archives()` w/ all 5 DPs + legacy `anchor` no-underscore exclusion. 10 storage tests. → COMMIT `feat(storage): purge_expired_archives helper w/ thrash guard + anchor skip`
2. **Config knobs (I25).** 3 knobs three-way. 1 test. → COMMIT `feat(config): I25 env knobs for MEMORY_ARCHIVE_RETENTION_*`
3. **Consolidation integration + telemetry.** Wire into `_run_retention_tasks()`. 2 tests + 2 Prometheus counters. → COMMIT `feat(consolidation): wire archive retention into nightly cycle + metrics`
4. **MCP tool.** `archive_purge` power-gated + secret-gated. 5 tests. → COMMIT `feat(mcp): archive_purge tool (dry_run default True)`

### Strand B

5. **sd_notify helper + RED tests.** Pure-Python writer to `$NOTIFY_SOCKET`. 3 tests. → COMMIT `feat(sd-notify): minimal sd_notify helper (READY/STOPPING/RELOADING)`
6. **Graceful-stop CLI + lifespan extensions.** `yadgar daemon graceful-stop`. `drain_in_flight_requests()` helper. `file_queue.flush_barrier()`. 4 tests (one new file + extension of existing `test_graceful_shutdown.py`). → COMMIT `feat(daemon): graceful-stop subcommand + request drain + queue barrier`
7. **systemd unit + launchd plist rewrite.** `Type=notify`, image-tag env var, `TimeoutStopSec=45`. macOS `ExitTimeOut=30`. 3 template tests. → COMMIT `feat(install): systemd Type=notify + launchd ExitTimeOut + env-var image tag`
8. **Snapshot + rollback artefact module.** `yadgar/update/snapshot.py`. Atomic writes to `~/.yadgar/upgrade-snapshots/`. 2 tests covered as part of orchestrator suite. → COMMIT `feat(update): upgrade snapshot artefact + retention`
9. **Orchestrator state machine.** `yadgar/update/orchestrator.py`. State machine, rollback handler, file lock. 10 tests. → COMMIT `feat(update): upgrade orchestrator state machine + rollback`
10. **CLI wire-up + `--finalize` + `--rollback` subcommands.** Extend `yadgar update --install`. Add `--finalize` (internal-use re-exec target). Add `--rollback` (operator-facing recovery: reads latest snapshot, restores env-file tag, restarts daemon). `update.install_enabled` + `update.lock_max_age_seconds` config knobs (I25). 4 tests. → COMMIT `feat(cli): yadgar update --install (gated) + --finalize re-exec + --rollback recovery`
11. **Manual end-to-end test recipe.** `make upgrade-test` Makefile target + `docs/UPGRADE_TEST.md`. No automated test (env-dependent); document the procedure. → COMMIT `docs(update): manual upgrade-test recipe`

### Cross-cutting

12. **Version bump + docs.** 5.48.0 → 5.49.0 core (backend unchanged unless rebuild required). CHANGELOG + MIGRATION_NOTES + README + AGENTS.md + config docs. → COMMIT `chore: bump version 5.48.0 → 5.49.0 + docs (orchestrator + archive retention both OFF)`

---

## 11. References

### Strand A

- `yadgar/consolidation/heat_decay.py:14-16` — decay constants
- `yadgar/storage/ops.py:110-118` — `prune_old_rows()` allowlist gap
- `yadgar/storage/memory.py:290-336` — `delete_memory()` cascade
- `yadgar/consolidation/cleanup.py:184-211` — `_run_retention_tasks()` extension point
- `yadgar/models.py:188` — `archived_at` timestamp
- `yadgar/server/tools/admin_invariants.py:168-188` — dangling-archive detection
- `yadgar/storage/migrations.py:434` — `memory_archive` schemaless table
- Memory 484431 — v6 LLM curator decisions
- v5.21.0 PD-23 — `migration_grace` handler

### Strand B

- `docs/PLAN_V5_48_0_UPDATE_MECHANISM.md:7-8` — original `--install` deferral note (now reframed per § 1.B).
- `yadgar/cli/update.py` — v5.48 `--check` action (extension point).
- `yadgar/daemon.py:149-453` — current container lifecycle (start/stop/restart).
- `scripts/install/yadgar.service.in` — current `Type=simple` template (to be rewritten).
- `scripts/install/launchd/com.openfantasy.yadgar.plist.in` — macOS plist template.
- `yadgar/tests/test_graceful_shutdown.py` — existing uvicorn `timeout_graceful_shutdown` coverage.
- `yadgar/server/lifecycle.py:382-396` — shutdown function (extension point for `drain_in_flight_requests`).
- `yadgar/embed_service.py:346-444` — existing cache-snapshot on shutdown.
- `yadgar/file_queue/queue.py` — drainer `.stop()` (needs explicit barrier).
- 2026-06-08 empirical repro (this plan, § 1.B).
- 2026-06-08 user decisions: bundle both strands, full scope (Type=notify + sd_notify + launchd + zero-downtime intent).
