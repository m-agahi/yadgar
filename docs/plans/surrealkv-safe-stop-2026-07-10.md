# SurrealKV Unclean-Stop RCA + Safe-Stop Mitigation Plan

- **Status:** DRAFT — investigation complete, awaiting sign-off. No code changes on master.
- **Task:** P0 #37 — surrealkv closes UNCLEANLY on every backend stop.
- **Date:** 2026-07-10
- **Author:** investigation subagent (read-only against live system; branch `docs/surrealkv-safe-stop-plan`)
- **Scope:** RCA + mitigation options + recovery runbook. Implementation is a follow-up PR.

---

## 0. TL;DR

1. **Root cause is upstream and unconditional.** SurrealKV's `impl Drop for Tree`
   (source: `surrealkv/src/lsm.rs`) tries `tokio::runtime::Handle::try_current()`.
   On SurrealDB v3.1.5's SIGTERM graceful-shutdown path the tokio runtime is
   already torn down by the time the store's `Tree` drops, so `try_current()`
   returns `Err`, the async `core.close().await` is **skipped**, and the warning
   `No runtime available for closing the store correctly` is logged. **Every stop.**
   The store is never flushed on close; it relies on WAL replay at next start.
2. **The warning is NOT the corruption.** It is benign on the *happy* path
   (WAL replay recovers). The rare crash is a **torn manifest**: when SIGTERM
   lands mid-compaction (an sstable/manifest rewrite in flight), the on-disk
   manifest references a table whose `.sst` was never fsynced →
   `Error loading table N: NotFound` → `Failed to load manifest` → crashloop.
3. **Our stop sequence does NOT widen a race.** The entrypoint traps SIGTERM,
   forwards it to `surreal`, and `wait`s; podman gives the process its full
   `--stop-timeout 30`. surreal itself logs the graceful path. The skipped close
   is upstream behaviour, not something our timeout truncates.
4. **No upstream fix version found.** The exact string returns no indexed issue;
   the corruption class (`surrealkv` unclean-shutdown → won't start) is tracked in
   open [surrealdb#5001](https://github.com/surrealdb/surrealdb/issues/5001)
   (still open, assigned `@arriqaaq`, remedy = "wipe + restore from backup").
5. **Recommended mitigation:** primary = **entrypoint SIGTERM trap that WAITS
   for surreal's clean exit before the container exits** (option B); belt-and-
   braces = **startup torn-manifest auto-recovery** (option D). See §5–6.

---

## 1. Evidence (2026-07-10, live host `nixos-quinyx`)

`journalctl --user -u yadgar-backend`, three stops since data was watched:

| When (local)     | Trigger              | `SIGTERM received … graceful` | `No runtime available …` (Δ) | Outcome |
|------------------|----------------------|-------------------------------|------------------------------|---------|
| 07-09 21:13:16   | nightly cycle        | yes                           | +108 ms                      | clean (WAL replay OK next start) |
| 07-10 13:18:50   | nix deploy (`hm-activate-max`) | yes                 | +109 ms                      | **torn manifest → crashloop** |
| 07-10 13:31:40   | apply (post-recovery)| yes                           | +104 ms                      | clean |

3/3 stops emit the warning ~100 ms after the graceful-shutdown log line →
**unconditional on this version.**

The 13:18 crashloop (systemd `Restart=on-failure`, `TimeoutStartSec=180` never
satisfied because `--sdnotify=healthy` can't fire on a store that won't open):

```
13:19:23 ERROR surrealkv::levels: Error loading table 11: Io(NotFound) "No such file or directory"
13:19:23 ERROR surrealdb_server::cli: There was a problem with the datastore: Failed to load manifest: IO error: No such file or directory (os error 2)
… repeats 13:21, 13:22, 13:25, 13:28 (systemd start-timeout kill cycle) …
13:29–13:31  manual recovery
13:31:42 INFO backend_started reason=clean   ← recovered
```

---

## 2. Upstream verdict (deliverable 1)

**Exact string `No runtime available for closing the store correctly`:** no indexed
GitHub issue (searched surrealdb + surrealkv repos). It is a **library log line**,
not a filed bug.

**Source of the warning (decisive).** `surrealkv` `impl Drop for Tree`:

```rust
impl Drop for Tree {
    fn drop(&mut self) {
        #[cfg(not(target_arch = "wasm32"))]
        {
            if let Ok(handle) = tokio::runtime::Handle::try_current() {
                let core = Arc::clone(&self.core);
                handle.spawn(async move {
                    if let Err(err) = core.close().await {
                        log::error!("Error closing store: {}", err);
                    }
                });
            } else {
                log::warn!("No runtime available for closing the store correctly");
            }
        }
    }
}
```

The store's clean close (memtable flush + manifest seal) is **async** and requires
a live tokio runtime at drop time. On SurrealDB's SIGTERM path the runtime is
shut down first; the `Tree` drop then hits the `else` branch → warning → **no
close at all**. This is the whole mechanism: a graceful close is only best-effort
and, on v3.1.5, effectively never happens on SIGTERM.

> Caveat (honest): `lsm.rs` was read from `surrealkv` `main`, not the exact commit
> pinned by SurrealDB v3.1.5. The exact-string match and the observed 3/3 behaviour
> make it overwhelmingly the same code path, but the line numbers may differ.

**Corruption class — related open bug.**
[surrealdb#5001](https://github.com/surrealdb/surrealdb/issues/5001) "SurrealDB
won't start after unexpected shutdown while using `surrealkv`" — same failure
family (surrealkv datastore unopenable after an unclean stop), **still open**,
labelled `beta` / `topic:surrealkv`, assigned `@arriqaaq`, reporter's only remedy
= wipe the data dir and restore from backup. No fix version. Adjacent reports:
[#5064](https://github.com/surrealdb/surrealdb/issues/5064) (surrealkv panics),
[#5146](https://github.com/surrealdb/surrealdb/issues/5146),
[#5063](https://github.com/surrealdb/surrealdb/issues/5063).

**Verdict:** there is **no known fixed release** to pin to. A version bump is a
speculative bet, not a confirmed fix. Do not gate the mitigation on it (option A
stays exploratory — §5).

---

## 3. Local signal-path audit (deliverable 2)

**PID 1 in the container is the entrypoint bash script** (`Dockerfile.backend`
`CMD ["/entrypoint-backend.sh"]`). `surreal start … &` runs backgrounded; the
script ends on `wait -n "$SURREAL_PID" "$EMBED_PID"`.

**SIGTERM delivery.** systemd `ExecStop = podman stop yadgar-backend` →
podman sends SIGTERM to PID 1 (the bash entrypoint) → the entrypoint's
`trap cleanup TERM INT` fires:

```bash
cleanup() {
  kill "$SURREAL_PID" "$EMBED_PID" "${WIKI_BACKUP_PID:-}" 2>/dev/null
  wait "$SURREAL_PID" "$EMBED_PID" 2>/dev/null
}
trap cleanup TERM INT
```

So SIGTERM **does reach `surreal` directly** (via `kill $SURREAL_PID`), and the
entrypoint `wait`s for it. podman's `--stop-timeout 30` and systemd's
`TimeoutStopSec = 45` both give ample time — surreal exited ~1 s after SIGTERM in
all three stops, nowhere near the timeout. The clean stop line
(`Stopped Yadgar Backend`) followed ~1 s later each time.

**Is the warning a race we widen, or unconditional upstream?** **Unconditional
upstream.** surreal logs `SIGTERM received. Waiting for a graceful shutdown`, runs
its shutdown, and the lsm warning fires ~100 ms later on the *graceful* path —
before any timeout could bite. Our sequence is orderly (trap → kill surreal →
wait). We are not truncating a close; the close is skipped by surrealkv's own
Drop ordering regardless of how much time we give it.

**Gap our stop *does* leave:** the entrypoint does **not** distinguish "surreal
exited cleanly" from "surreal was killed / exited non-zero," and it does not
sequence writers-before-store (uvicorn embed service and the surreal process are
killed together). Neither causes the corruption, but option B tightens both so a
future graceful-close upstream fix would actually take effect, and so a torn stop
is at least detectable.

---

## 4. The `.old-20260709_191332` choreography (deliverable 3)

**Question:** the dir restored on 07-10 was named for the 07-09 21:13 nightly, yet
contained live writes through 07-10 13:18. How, and was `.old` still the live inode?

**Answer — reconstructed from journal + filesystem, with proof:**

### 4a. What the nightly vacuum did (07-09 21:13:32)

The vacuum uses a **side-build + atomic same-dir swap**
(`yadgar/core/vacuum/phases.py::_atomic_swap`):

```
1. build compacted DB under surreal_db.building-<ts>, verify per-table counts
2. promote .building → .new
3. os.rename(surreal_db      → surreal_db.old-20260709_191332)   # rename 1
4. os.rename(surreal_db.new  → surreal_db)                       # rename 2
5. start backend on the swapped-in canonical
6. finalize: check_invariants → on pass, retire .old
```

The nightly-cycle journal confirms steps 1–5 ran and then:

```
21:14:26 WARNING: check_invariants returned non-ok (HTTP 404): Not Found —
         core may not be fully ready post-restart; previous DB retained for
         rollback: …/surreal_db.old-20260709_191332
21:14:26 [vacuum] complete.
```

`check_invariants` failed (core 404, not ready), so **the `.old` was intentionally
NOT retired** — it was kept as the rollback anchor. That is why it survived on disk
at all.

### 4b. Why `.old` accumulated live writes through 13:18

The backend process ran **continuously** from 07-09 21:14 to 07-10 13:18:50 —
one PID (700783), `Consumed … over 16h 5min wall clock`, emitting `_walk_db_sizes`
every 10 min, no restart in the gap. The 13:18 stop was the nix deploy
(`hm-activate-max[…]: - yadgar-nightly-cycle`), **not** a second vacuum.

Filesystem proof that `.old` was the live write target:
`surreal_db.old-20260709_191332` has fresh inner files at **07-10 13:18**
(`wal/…0009.wal` 31 MB, `vlog/…0002.vlog` 107 MB) — writes that landed the moment
before the deploy stop. A retired/rolled-out dir does not grow; a live one does.

### 4c. Was `.old` still THE live inode? (the crux)

**`.old` was the live directory the whole 16 h.** md5 proof after recovery:

```
b5066058…  surreal_db/vlog/00000000000000000001.vlog          (live, post-recovery)
b5066058…  surreal_db.old-20260709_191332/vlog/…0001.vlog     (byte-identical)
9bedbb63…  surreal_db/sstables/…0010.sst   ==  .old/sstables/…0010.sst
fb47e661…  surreal_db/sstables/…0011.sst   ==  .old/sstables/…0011.sst
```

Byte-identical content, **different inodes, link-count 1** → live `surreal_db` is a
**`cp -a` copy of `.old`**, made during the 13:29–13:31 recovery. So the choreography
was:

1. nightly swap renamed the ORIGINAL live dir → `.old-20260709_191332` and put the
   compacted `.new` at `surreal_db`, but the invariants check failed and the
   backend that came back up ended up writing to the `.old` inode (the swap's
   promotion did not durably take over as the write target — see uncertainty below).
   Net effect: `.old` remained the true, complete, actively-written DB for 16 h.
2. 07-10 13:18 deploy SIGTERM hit the live DB **mid-compaction** → the canonical
   `surreal_db` (what the deploy left) got a torn manifest referencing table 11's
   unfsynced sstable → `surreal_db.CORRUPT-20260710` (its inner files also stamped
   13:18: `sstables/…0012.sst`, torn `manifest`).
3. recovery: `mv surreal_db → surreal_db.CORRUPT-20260710`, then
   `cp -a surreal_db.old-20260709_191332 → surreal_db` (fresh inodes, identical
   bytes — the md5 match), `rm LOCK`, start backend → `backend_started reason=clean`
   at 13:31:42.

**Residual uncertainty (stated honestly, not smoothed over):** the exact reason the
running backend wrote into the `.old` inode rather than the swapped-in `.new` is not
fully pinned from logs alone. Two readings are consistent with the evidence:

- **(R1, favoured)** the post-swap backend restart on the new canonical failed its
  readiness/invariants (the 21:14 HTTP 404), and the recovery/rollback path — or a
  subsequent restart — pointed the live backend back at the original dir (now named
  `.old`), which then served all writes for 16 h. The compacted `.new` swap was
  effectively inert.
- **(R2, less likely)** the swap did take, `.new` became `surreal_db` and was live;
  the 13:18 writes in `.old` are an artifact of a later step. This is contradicted by
  the md5 byte-identity of live-`surreal_db` to `.old` (recovery clearly copied `.old`,
  not a distinct compacted dir), so R2 is disfavoured.

**Trust implication for future recoveries:** `.old-<ts>` dirs are named for the
vacuum-swap timestamp but their *contents* can be the true live DB (mtime lies —
`os.rename` preserves the dir's own mtime). When invariants fail post-swap, the
`.old` is the safest restore source precisely because it is the un-compacted
original that kept receiving writes. Confirm a restore source by **inner-file mtime +
per-table `memory_stats` count**, never by the dir name or dir mtime.

---

## 5. Mitigation options (deliverable 4)

Ordered; each with effort / risk.

### Option A — bump SurrealDB pin (IF upstream fixes the Drop ordering)
- **What:** track surrealkv/surrealdb for a release where the store close no longer
  depends on a live runtime at Drop (e.g. a synchronous close-on-shutdown, or a
  SIGTERM handler that closes the store before the runtime is torn down).
- **Effort:** low to bump `Dockerfile.backend` `COPY --from=surrealdb/surrealdb:vX`;
  high to validate (backup-first, full e2e, per-table count verify).
- **Risk:** HIGH / speculative — **no fixed version currently exists**. A blind bump
  risks a new format-skew or a different regression. **Do not rely on this.**
- **Verdict:** watch-only. Not the primary.

### Option B — entrypoint SIGTERM trap: writers-first, WAIT for clean store exit (PRIMARY)
- **What:** rework `entrypoint-backend.sh` `cleanup()` to:
  1. stop the writers first (SIGTERM uvicorn embed `$EMBED_PID` + wiki-backup loop),
     so no HTTP write is mid-flight against surreal;
  2. THEN SIGTERM `surreal` and **`wait` specifically on `$SURREAL_PID`**, capturing
     its exit status;
  3. if surreal exits non-zero (or the wait times out under an internal deadline
     shorter than podman's 30 s), log a loud `SURREAL_UNCLEAN_STOP` marker to
     `$YADGAR_LOG_DIR` so a torn stop is *detectable* (feeds option D).
- **Effort:** medium (one bash file; add a bounded internal wait, ~30–40 lines).
- **Risk:** LOW — it only reorders and observes an already-working stop. Does not
  touch surreal internals.
- **Payoff:** (a) removes the writers-vs-store race entirely; (b) makes any future
  upstream graceful-close actually fire on the correct ordering; (c) emits a
  detectable signal for torn stops. It does **not by itself** stop the upstream
  runtime-torn-down-before-close problem — that is upstream — but it maximises the
  chance the close succeeds and guarantees we notice when it didn't.

### Option C — systemd `ExecStop` pre-stop logical export (safety artifact)
- **What:** add an `ExecStop=` pre-step that takes a `GET /export` `.surql` snapshot
  *before* `podman stop`, so every stop leaves a transactionally-consistent logical
  backup independent of the on-disk store's health.
- **Effort:** low-medium (nix `ExecStop` list + a small export helper; must run while
  the backend is still up, before the stop signal).
- **Risk:** MEDIUM — `GET /export` has a documented stack-overflow failure mode on
  this dataset (the reason the in-container export loop was removed — see
  `entrypoint-backend.sh` header). Must run against a bounded/quiesced path or with
  the 512 MiB `SURREAL_RUNTIME_STACK_SIZE` already set for the unit, and must be
  time-boxed so a hung export can't block the stop past `TimeoutStopSec`.
- **Verdict:** good defence-in-depth, but the export fragility makes it secondary.

### Option D — startup torn-manifest detection → auto-restore (BELT-AND-BRACES)
- **What:** on backend start, before/around `surreal start`, detect the torn-manifest
  signature (`Failed to load manifest` / `Error loading table N: NotFound`) and, if
  found, automatically: move the corrupt canonical aside to `surreal_db.CORRUPT-<ts>`,
  restore the newest *verified* quiesced copy (`.old-<ts>` or `.pre-vacuum-<ts>` by
  inner-file recency + count), `rm LOCK`, and retry. This is the manual §6 runbook,
  automated and idempotent.
- **Effort:** medium-high (startup wrapper logic + a "which backup is complete" chooser
  that verifies per-table counts, not dir mtime).
- **Risk:** MEDIUM — auto-restore that picks the wrong source could roll back good data;
  mitigated by verifying counts and by always preserving the corrupt dir (never delete).
- **Payoff:** converts a 3-minute crashloop + manual intervention into a self-heal.
  Directly addresses the P0 pain (start-timeout kill cycle).

### Recommendation
- **Primary: Option B** — cheap, low-risk, correct-by-construction stop ordering, and
  it makes torn stops *detectable*.
- **Belt-and-braces: Option D** — self-healing startup so the next torn manifest does
  not become a crashloop requiring a human.
- **Watch: Option A**; **defence-in-depth if export fragility is resolved: Option C.**

Do NOT combine B+D into one PR blindly — land B first (small, verifiable), then D on
top (it depends on B's `SURREAL_UNCLEAN_STOP` marker to know when to act).

---

## 6. Manual recovery runbook (deliverable 5) — what worked on 2026-07-10

Torn manifest signature in `journalctl --user -u yadgar-backend`:
`Failed to load manifest` + `Error loading table N: NotFound`, with systemd
restarting the unit every ~3 min (start-timeout kill cycle).

```bash
DATA=~/.local/share/yadgar

# 1. Stop BOTH units (backend holds the DB; core alone won't release it).
systemctl --user stop yadgar
systemctl --user stop yadgar-backend
podman ps            # confirm neither yadgar nor yadgar-backend is running

# 2. Move the corrupt canonical aside — NEVER delete it (forensics + fallback).
mv "$DATA/surreal_db" "$DATA/surreal_db.CORRUPT-$(date +%Y%m%d_%H%M%S)"

# 3. Choose the restore source by CONTENT, not name.
#    List candidates by NEWEST INNER FILE (dir mtime lies — os.rename preserves it):
for d in "$DATA"/surreal_db.old-* "$DATA"/surreal_db.pre-vacuum-*; do
  newest=$(find "$d" -type f -printf '%TY-%Tm-%Td %TH:%TM  %p\n' | sort | tail -1)
  echo "$d  ->  $newest"
done
#    Pick the dir whose newest inner file is closest to the crash time AND that has a
#    complete manifest+sstables+vlog+wal set. On 2026-07-10 that was
#    surreal_db.old-20260709_191332 (live writes through 13:18).

# 4. Copy it in as the new canonical (cp -a = preserve, fresh inodes).
cp -a "$DATA/surreal_db.old-20260709_191332" "$DATA/surreal_db"

# 5. Remove the stale lock so the fresh backend can acquire it.
rm -f "$DATA/surreal_db/LOCK"

# 6. Start backend FIRST, then core.
systemctl --user start yadgar-backend
sleep 8                     # let surreal open + embed model warm (health-start-period 60s)
systemctl --user start yadgar

# 7. Verify. Expect backend_started reason=clean, and non-zero memory_stats.
journalctl --user -u yadgar-backend --since "-2min" | grep backend_started
#   then via MCP: memory_stats()  → expect ~2373 mem / ~2169 wiki (07-09 nightly counts)
```

**Do NOT:** trust dir name or dir mtime to pick a source; delete the CORRUPT dir before
verifying the restore; start core before backend (core will serve an empty/stale view).

---

## 7. Open follow-ups (not in scope here)

- File an upstream issue against surrealkv with the exact Drop-ordering repro (the
  `impl Drop for Tree` skip-on-no-runtime path) — currently only the corruption
  *symptom* is tracked (#5001), not the shutdown-close root cause.
- Decide whether `check_invariants`-fail-post-swap should ROLL BACK the swap
  (promote `.old` back to canonical) instead of merely retaining `.old` — the 07-09
  404-retain is exactly what left the ambiguous live-in-`.old` state.
- Consider `TimeoutStartSec` + `StartLimitIntervalSec` tuning so a torn-manifest
  crashloop trips a `failed` state fast instead of grinding 5× before a human notices.
```
