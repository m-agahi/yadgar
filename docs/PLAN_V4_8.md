# Yadgar v4.8 Plan — SurrealKV GC Automation + Consolidation Starvation Fix

## Context

Two operational problems caused by the same daemon:

1. **SurrealKV bloat.** SurrealDB v3.0.5's embedded storage engine never garbage-collects dead row-versions. After the 2026-05-12 manual rebuild the DB grew 60 MB → 495 MB in 24 hours. Manual rebuild every 1–2 weeks: `curl /export` → strip `action_log` from the `.surql` → stop both daemons → `mv` old DB dir → start `yadgar-backend` → `curl -X POST /import` → start `yadgar`.
2. **Consolidation cycle starvation.** `ConsolidationScheduler._daemon_loop` fires every ~80 s indefinitely whenever the user is idle. Logs from 2026-05-13 23:00–23:49 show back-to-back cycles, each 43–62 s of CPU (sentence-transformers `encode()` + numpy matmul + 50+ DB writes), separated by ~30 s gaps. Fan spin-up / spin-down rhythm at night matches this exactly. Two compounding bugs:
   - `last_activity` is updated only by `record_activity()` (called from external API hits). After a cycle completes, `last_activity` stays stale → idle_seconds is still ≥ `IDLE_THRESHOLD_SECONDS` (300 s) → next 30 s wake-up fires another cycle.
   - `_memify_derive` (`curation.py:627`) hits `413 Payload Too Large` on `/sql` every single run. The cycle wastes ~50 s only to crash inside the curator. The v4.5.0 `MAX_BATCH_STATEMENTS=500` chunking does not cover this path.

v4.8 ships:
1. A working `yadgar vacuum` (the existing CLI is broken — targets a directory that no longer exists in surrealkv ≥ v2 / SurrealDB v3.0.5).
2. DB-size telemetry on every consolidation cycle.
3. A nix systemd timer that wraps `yadgar vacuum` on a weekly cadence.
4. **Cooldown on the consolidation daemon** so idle-triggered cycles fire at most once per `CONSOLIDATION_COOLDOWN_SECONDS` (default 1800 s = 30 min).
5. **Memify batch chunking** so `_memify_derive` stops 413-crashing every cycle.
6. **Time-bound backup retention** — current `tail -n +8 | xargs rm -rf` keeps the 7 most recent snapshots regardless of age. After the 2026-05-12 rebuild incident this left **5 × 5.5 GB pre-rebuild snapshots from the bloated DB stuck in retention for 2+ days** (28 GB total `~/.backups/yadgar/`). Add an age cap.
7. **Fix `check_invariants: memory_similarity_link check failed: timed out`** — the table check hits the default HTTP timeout. Auto-repair never runs when this happens. Chunked or indexed query path.
8. **Fix `NLI reranking failed: No module named 'sentence_transformers'`** — reranker imports `sentence_transformers` at runtime but core image only installs base deps; package lives behind the `[ml]` extra that v4.7.0 added to CI but not the runtime image.
9. **Suppress the `socket.send() raised exception` cascade** (74 events in a single second at 23:18:00 last night) — SSE/websocket clients disconnect and the server logs the full traceback for each. Catch and log-once at DEBUG.

Version bump: `4.7.0 → 4.8.0`. No breaking API changes. v5.0.0 (security + observability rewrite) remains the next planned release after this.

---

## Investigation findings (2026-05-13)

### Storage layout

`~/.yadgar/surreal_db/` on the live host:

```
4.0K   LOCK
4.0K   manifest
 36M   sstables
316M   vlog
4.2M   wal
355M   total
```

No `clog/` directory. The existing `cmd_vacuum` in `yadgar/__main__.py:685` inspects `db_path / "clog"` and exits with `"No clog directory found — nothing to vacuum."` as the first action. The CLI has likely been dead since the SurrealDB upgrade that renamed clog → wal (or the surrealkv layout change that promoted vlog as the value store).

`vlog/` is the bloat driver — 89% of total size. The wal is small (4.2M) and bounded by SurrealKV's compaction of the write-ahead log into sstables. dead row-versions accumulate in the value log because surrealkv never collects them.

### cmd_vacuum bugs (must fix before automation)

| ID | Bug | Impact |
| --- | --- | --- |
| B1 | Targets `clog/` which no longer exists | CLI is a no-op |
| B2 | `KEEP_TABLES` misses `wiki_page`, `wiki_crossref`, `memory_similarity_link`, `memory_link`, `caused_by`, `causal_belief`, `dlq_*` | Auto-scheduling would silently nuke ≈1,588 wiki pages on first run |
| B3 | RELATE edges round-tripped through `UPSERT type::record($tbl, $rid)` | Graph-traversal edges silently dropped; reimport produces document rows in an edge table, not edges |
| B4 | Only the clog gets a `.bak`; no full DB-dir snapshot before destructive phase | If reimport crashes mid-loop, partial restore + lost vlog = corruption. `nix yadgar-backend.service:ExecStartPre` only covers the *next* start |
| B5 | No record of `yadgar vacuum` ever running successfully on a production-sized DB | Untested |

### Manual procedure (2026-05-12) — what actually works

1. `curl http://127.0.0.1:8080/export > backup.surql`
2. Strip `TABLE DATA action_log` section (its rows contain raw shell-command text that breaks the SurrealQL re-parser on import — `surreal export`/`import` is not a clean round-trip).
3. `systemctl --user stop yadgar yadgar-backend`
4. `mv ~/.yadgar/surreal_db ~/.yadgar/surreal_db.bloated`
5. `systemctl --user start yadgar-backend` — entrypoint bootstrap creates `yadgar`/`viewer` ON ROOT and a fresh empty DB.
6. `curl -X POST http://127.0.0.1:8080/import --data-binary @backup.filtered.surql`
7. `systemctl --user start yadgar`

This is the spec `cmd_vacuum` should implement. The existing CLI's "export tables to JSON, drop one subdir, reimport via UPSERT" approach is a different algorithm that does not address the vlog and loses RELATE edges.

---

## Scope (ordered)

### 1. Rewrite `cmd_vacuum` to mirror the manual procedure

Replace `yadgar/__main__.py:cmd_vacuum` (lines 685–813). New behavior:

1. **Preflight checks**
   - Confirm `~/.yadgar/surreal_db/` exists and `LOCK` is free (daemon stopped). If the daemon is still up, error out with `"systemctl --user stop yadgar yadgar-backend"`.
   - Resolve config: `db_path`, backend HTTP endpoint (default `http://127.0.0.1:8080`), credentials from `~/.yadgar/credentials` or env.

2. **Phase 1 — export (daemon must be running)**
   - The current CLI runs against a stopped daemon by opening the DB file directly. That path can't talk to `/export`. Switch the flow: vacuum starts a fresh `yadgar-backend` subprocess in read-only mode against the existing DB (or requires the daemon already running, depending on what surrealkv allows for a second reader). Realistically the cleaner sequencing is:
     - Vacuum command requires both daemons *running*.
     - It calls `/export` over HTTP.
     - Then it triggers the daemon stop + reimport.
   - `curl /export` (HTTP, root creds for now — v5.0 will replace with bearer token) → `~/.yadgar/vacuum_export_<ts>.surql`.
   - Pipe through `strip_action_log()`: regex-delete the `-- TABLE DATA: action_log ----...\n... -- TABLE: ...` block, plus the matching `DEFINE TABLE action_log ...` line if its row insert syntax is what breaks the parser.

3. **Phase 2 — snapshot + drop**
   - Full DB dir snapshot: `cp -r ~/.yadgar/surreal_db ~/.yadgar/surreal_db.pre-vacuum-<ts>`.
   - Retain N=3 pre-vacuum snapshots (tail+xargs rm); independent from the `yadgar-backend.service:ExecStartPre` snapshot ring (which keeps 7 daily backups).
   - `systemctl --user stop yadgar yadgar-backend` — when running outside systemd context, fall back to sending SIGTERM via PID file.
   - `mv ~/.yadgar/surreal_db ~/.yadgar/surreal_db.bloated-<ts>` (atomic rename, kept for one cycle as belt-and-braces — removed by next vacuum).

4. **Phase 3 — restart + reimport**
   - `systemctl --user start yadgar-backend` (or the equivalent for non-systemd installs — Docker-compose: `docker compose start yadgar-backend`).
   - Wait for `/health` (poll, 120s max, abort if backend doesn't come up).
   - `curl -X POST /import --data-binary @export.filtered.surql`. Capture HTTP status; on non-200, do not start `yadgar`, leave the bloated dir in place, and exit with the response body printed.
   - On success: `systemctl --user start yadgar`, wait for `/healthz`, then `rm -rf ~/.yadgar/surreal_db.bloated-<ts>` once a follow-up `check_invariants` returns `ok: true`.

5. **Reporting**
   - Log `before_bytes`, `after_bytes`, `saved_bytes`, `saved_pct`, `duration_seconds` to stdout *and* into the `consolidation_log` table so `memory_stats` can surface it.

Service control abstraction: a small `yadgar/ops.py` module that wraps `systemctl --user`, `docker compose`, or a "manual" mode (prints commands, no-op) selected by `--service-mode={systemd,docker,manual}` flag. Default detected from `os.environ.get("INVOCATION_ID")` (systemd) vs `Path("/.dockerenv").exists()` (docker) vs manual.

### 2. DB size telemetry

Extend `check_invariants` (`yadgar/server.py`) — add `db_size` block:

```json
{
  "db_size_bytes": 372293632,
  "vlog_size_bytes": 331350016,
  "sstables_size_bytes": 37748736,
  "wal_size_bytes": 4194304,
  "vlog_pct_of_total": 89,
  "size_warning": false
}
```

`size_warning: true` when `db_size_bytes > settings.DB_SIZE_WARNING_BYTES` (default `1073741824` = 1 GiB). Logged at WARN level by the daily consolidation cycle.

New config keys (`yadgar/config.py`):

- `DB_SIZE_WARNING_BYTES`, default `1_073_741_824`.
- `VACUUM_SNAPSHOT_RETENTION`, default `3`.

Computed via `os.walk()` + `f.stat().st_size`. Avoid `du` subprocess to stay portable.

### 3. nix systemd timer

In `/home/max/git/nix/modules/home/yadgar.nix`:

```nix
systemd.user.services.yadgar-vacuum = {
  Unit = {
    Description = "Yadgar SurrealKV vacuum (export → fresh DB → reimport)";
    Wants = [ "yadgar-backend.service" "yadgar.service" ];
  };
  Service = {
    Type = "oneshot";
    ExecStart = "${cfg.package}/bin/yadgar vacuum --service-mode=systemd";
    # ExecStart owns the stop/start of yadgar + yadgar-backend internally.
    TimeoutStartSec = "30min";
  };
};

systemd.user.timers.yadgar-vacuum = {
  Unit.Description = "Weekly Yadgar vacuum";
  Timer = {
    OnCalendar = "Sun *-*-* 04:00:00";
    RandomizedDelaySec = "30min";
    Persistent = true;
  };
  Install.WantedBy = [ "timers.target" ];
};
```

The vacuum command stops/starts the daemons itself rather than declaring `Conflicts=` on `yadgar.service` so the failure-recovery path (don't restart `yadgar` if reimport failed) stays inside one binary.

### 4. Consolidation cycle cooldown

Edit `yadgar/consolidation.py:_daemon_loop` (line 171):

```python
def _daemon_loop(self) -> None:
    while not self._stop_event.is_set():
        self._stop_event.wait(timeout=self._settings.DAEMON_CHECK_INTERVAL)
        if self._stop_event.is_set():
            break
        now = datetime.now(UTC)
        today = now.date()

        # Cooldown — don't re-fire idle cycles until enough time has passed
        # since the last completed cycle. Independent from last_activity (which
        # only reflects external API hits and never resets after consolidation).
        cooldown = self._settings.CONSOLIDATION_COOLDOWN_SECONDS
        since_last_cycle = (now - self._last_cycle_completed_at).total_seconds()
        if since_last_cycle < cooldown:
            continue

        idle_seconds = (now - self.last_activity).total_seconds()
        if idle_seconds >= self._settings.IDLE_THRESHOLD_SECONDS:
            new_episodes = self._storage.get_episodes_since(self._last_consolidated_episode_id)
            if new_episodes:
                try:
                    self._consolidation_cycle()
                except Exception:
                    logger.exception("Idle consolidation cycle failed")
                finally:
                    self._last_cycle_completed_at = datetime.now(UTC)
        # Daily 18:30 UTC block stays untouched — that path is gated by date.
```

New attribute on the scheduler, initialised in `__init__`:

```python
self._last_cycle_completed_at = datetime.fromtimestamp(0, UTC)
```

New config setting (`yadgar/config.py`):

- `CONSOLIDATION_COOLDOWN_SECONDS`, default `1800` (30 min). Document that setting to `0` restores the legacy back-to-back behaviour.

The daily 18:30 UTC cycle path is left alone; cooldown applies only to the idle-triggered branch. The 18:30 cycle then also sets `_last_cycle_completed_at` so a manual `force_consolidate()` immediately afterwards still respects the cooldown.

Side effect: `force_consolidate()` (called from MCP `consolidate_now`) ignores the cooldown — explicit user request beats throttling. Add a comment to that effect.

### 5. Memify batch chunking

`yadgar/curation.py:_memify_derive` (line ~600–630) builds one `batch_writes` call with potentially every derived-belief insert. Empirical evidence (2026-05-13 23:00–23:49 logs): every single cycle 413s.

Fix:

1. Chunk the batch by `MAX_BATCH_STATEMENTS` (already imported, default 500) **and** by serialised body size. New setting `MAX_BATCH_BYTES`, default `1_000_000` (1 MB — well below the SurrealDB default `/sql` body limit). `batch_writes` in `yadgar/storage.py:551` accepts a list; wrap call sites that build large lists with a helper `_chunk_by_bytes(batch, max_bytes)`.
2. Apply the same chunking helper at all `batch_writes` call sites — grep `git -C /home/max/git/yadgar grep -n "batch_writes("` and audit.
3. Add a regression test that fabricates 5 000 derive statements, runs `_memify_derive`, asserts no `HTTPStatusError`.

Optional but cheap: bump `SURREAL_HTTP_BODY_LIMIT` env var in `entrypoint-backend.sh` from default (currently unset, meaning SurrealDB's compiled-in default) to e.g. 16 MiB so the client-side chunking has more headroom. Document in MIGRATION_NOTES.

### 6. Time-bound backup retention

Current `yadgar-backend.service:ExecStartPre` in `/home/max/git/nix/modules/home/yadgar.nix:209`:

```bash
ls -dt "$BK"/surreal_db_* 2>/dev/null | tail -n +8 | xargs -r rm -rf
```

Keeps 7 most recent. Live state today:

```
5.5G  surreal_db_20260512_133621   # pre-rebuild bloat, 2 days old
5.5G  surreal_db_20260512_140741
5.5G  surreal_db_20260512_143854
5.5G  surreal_db_20260512_150939
5.5G  surreal_db_20260512_154239
137M  surreal_db_20260512_184240   # post-rebuild
479M  surreal_db_20260513_233214
─────
 28G  total (out of $HOME budget)
```

Fix: combine count cap + age cap + size cap.

```bash
# Drop snapshots older than N days
find "$BK" -maxdepth 1 -name 'surreal_db_*' -type d -mtime +${YADGAR_BACKUP_MAX_AGE_DAYS:-7} -exec rm -rf {} +
# Drop oldest beyond count cap
ls -dt "$BK"/surreal_db_* 2>/dev/null | tail -n +$((${YADGAR_BACKUP_MAX_COUNT:-7} + 1)) | xargs -r rm -rf
# Drop oldest while total > size cap (GiB)
while [ "$(du -sBG "$BK" 2>/dev/null | awk '{print $1}' | tr -d G)" -gt ${YADGAR_BACKUP_MAX_GIB:-10} ]; do
  ls -dt "$BK"/surreal_db_* 2>/dev/null | tail -1 | xargs -r rm -rf
done
```

Defaults: 7 days, 7 count, 10 GiB. Whichever is tightest wins.

New nix module options surfaced as env vars so users can override via `programs.yadgar.backup.maxAgeDays/maxCount/maxGiB`.

The new `cmd_vacuum` pre-vacuum snapshots (`~/.yadgar/surreal_db.pre-vacuum-<ts>`) use the same retention helper extracted to `scripts/cleanup-backups.sh` — single source of truth.

MIGRATION_NOTES: one-shot manual cleanup command for the existing 28 GB:

```bash
find ~/.backups/yadgar/db -maxdepth 1 -name 'surreal_db_*' -type d -mtime +1 -size +1G -exec rm -rf {} +
```

### 7. `check_invariants: memory_similarity_link check failed: timed out`

Grep for the check in `yadgar/server.py` / `yadgar/storage.py`. Two likely root causes:

1. `SELECT count() FROM memory_similarity_link` over a million-row RELATE table without an index. SurrealDB v3 doesn't cache aggregates; full scan every call. Mitigation: store last-known count + dirty flag, recompute only on writes — *or* page through with `LIMIT 100000` and sum.
2. Dangling-edge check `SELECT id FROM memory_similarity_link WHERE in IS NONE OR out IS NONE OR record::exists(in) = false`. `record::exists` evaluated per row → O(N) lookups. Mitigation: rewrite as set-difference (`SELECT id FROM memory_similarity_link WHERE in NOT IN (SELECT id FROM memory)`) so the planner can use index.

Add a settable `CHECK_INVARIANTS_QUERY_TIMEOUT_SECONDS` (default 60). On timeout, log WARN + skip auto-repair for that table this cycle, do not abort the whole `check_invariants` pass. Currently a timeout in one table seems to abort the rest.

### 8. NLI reranking dependency

Logs: `NLI reranking failed: No module named 'sentence_transformers'`.

Reranker code (grep `yadgar/retrieval/` for `sentence_transformers` or `NLI`) imports `sentence_transformers` lazily. The package ships only in the `[ml]` install extra, which v4.7.0 added to *CI* but the core runtime image `openfantasy/yadgar:4.7.0` still installs `pip install yadgar` (no extra) per `Dockerfile`. Two options:

1. **Bundle the extra** — change `Dockerfile`'s `pip install .` → `pip install .[ml]`. Adds ~400 MB to the image (sentence-transformers + torch + numpy already there). Reranker works out of the box.
2. **Drop the silent fall-through** — surface a one-time WARN at startup if the import fails (`Reranker disabled: install yadgar[ml] to enable`). User explicitly opts in via a different image tag.

Decision: option 1. Reranker quality matters enough; image-size argument is weak since the embedding model is already shipped. New image tag still `openfantasy/yadgar:4.8.0` — no rename.

### 9. `socket.send() raised exception` cascade

`grep -rn "socket.send" yadgar/` to locate. Almost certainly in the SSE event publisher (`yadgar/server.py` event stream) or the viz_server. Each disconnected client raises and the handler logs the full traceback.

Fix: catch `(ConnectionResetError, BrokenPipeError, asyncio.CancelledError)` at the per-client send site, log once at DEBUG with the client id, drop them from the broadcaster's subscriber set. No traceback in INFO/WARN. Add unit test that simulates 100 disconnects and asserts the log capture sees one summary line, not 100 tracebacks.

### 10. MIGRATION_NOTES.md

Hand to user (per the no-auto-apply rule):

- `nix-update` (or equivalent) to install the new `yadgar-vacuum.service` + `.timer`.
- Manual trigger: `systemctl --user start yadgar-vacuum.service` then `journalctl --user -u yadgar-vacuum -f`.
- Rollback: `systemctl --user stop yadgar`, `mv ~/.yadgar/surreal_db.pre-vacuum-<ts> ~/.yadgar/surreal_db`, restart.
- Disable: `systemctl --user disable --now yadgar-vacuum.timer`.

---

## Test plan

1. **Characterization test** — script that takes a DB dir, runs `yadgar vacuum`, then snapshots per-table row counts both sides via `SELECT count() FROM <table> GROUP ALL`. Assert no table loses rows except the explicit blacklist (`action_log`, `consolidation_log`, `dlq_*`).
2. **RELATE preservation** — fixture: write three memories, run `wiki_add` linking two memories, count `wiki_crossref` rows pre, vacuum, count post. Must be equal.
3. **Failure injection** — simulate `/import` HTTP 500. Assert the bloated dir is retained, `yadgar` is not started, exit code != 0.
4. **Size report** — assert `before > after` for a DB seeded with 100k UPSERT/DELETE cycles.
5. **Timer dry-run** — `systemctl --user start yadgar-vacuum.service` once on staging host, verify log output, verify `~/.yadgar/surreal_db.pre-vacuum-*` snapshot is created and pruned to 3.
6. **Cooldown** — stub `last_activity = epoch`, run `_daemon_loop` two iterations 30 s apart with cooldown=1800. Assert exactly one cycle fires. Set `_last_cycle_completed_at` to 31 min ago, run again, assert second cycle fires.
7. **Memify chunking** — fabricate 5 000 derive statements; assert `_memify_derive` completes without `HTTPStatusError` and emits ≥10 separate `batch_writes` calls.

All tests run inside the existing pytest harness; the failure-injection test uses an HTTP stub for `/import`.

---

## Deferred

- MCP `vacuum_now()` tool — v4.9.
- Threshold auto-trigger (vacuum fires when `db_size_bytes > threshold` without waiting for the timer) — v4.9.
- HTTP `/admin/vacuum` endpoint — bundle with the bearer-token middleware in v5.0.
- Native online compaction — track upstream SurrealKV; nothing to do until they ship it.
- Root-cause fix for episode growth during idle (consolidation cycle's own writes generate episodes that feed the next cycle's `new_episodes` gate). The v4.8 cooldown is a sufficient bandage; the underlying self-feeding loop is a v4.9 audit target.

---

## Open decisions

1. **Action_log retention during vacuum.** Current option: drop entirely (matches manual procedure). Alternative: re-export with the rows base64-encoded so the SurrealQL parser doesn't choke. Decision: drop. The 7-day prune means we lose at most one cycle's worth of unprocessed entries; consolidation runs daily.
2. **Snapshot location.** `~/.yadgar/surreal_db.pre-vacuum-<ts>` next to the live dir, or `~/.backups/yadgar/db/`? Decision: same parent dir, so `mv` is rename-only (atomic on same fs).
3. **Cadence.** Weekly Sun 04:00 default. Make `OnCalendar` overridable via a nix module option for users who run the daemon less frequently.

---

## Order of work

1. Consolidation cooldown (smallest blast radius, single-file diff, immediate fan-noise relief). Land + deploy first.
2. Memify chunking fix (stops 413 churn).
3. `socket.send()` cascade silencer (one-line per-client; trivial, unblocks readable logs).
4. `check_invariants` similarity-link timeout — rewrite the dangling-edge query as set-difference + per-table timeout setting.
5. Backup retention: time + count + size caps, extract shared helper, one-shot manual cleanup of the 28 GB stuck snapshots.
6. NLI reranker — `Dockerfile` `pip install .[ml]`.
7. Write the vacuum characterization test (failing — current `cmd_vacuum` no-ops on this DB).
8. Rewrite `cmd_vacuum` to the manual-procedure spec.
9. Run it once manually on the live 539 MB DB, confirm `~/.yadgar/surreal_db/vlog/` drops to < 50 MB.
10. Add `db_size` block to `check_invariants`.
11. Add nix timer + service for weekly vacuum.
12. Write MIGRATION_NOTES.md.
13. Bump `pyproject.toml:version` 4.7.0 → 4.8.0. `server.json:backend_version` stays 4.7.0 — backend image is untouched in v4.8. The `Dockerfile` `[ml]` install changes the core image only.
14. Open PR.
