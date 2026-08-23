# Pre-migration backup at backend boot — protect the one write nothing protects

**Date:** 2026-08-01
**Task:** #0115 (schema migrations run on backend boot with the data unprotected)
**Status:** DRAFT — not started.
**Target train:** `feat/v5.172-bug-train`.
**Blocking dependency:** `docs/plans/archive/0027c-core-startup-backend-retry-2026-08-01.md`
(SHIPPED in the v5.172 train, `fe84e3ec`) — both cars edit the
`StorageEngine.__init__` → `_init_schema()` seam (`yadgar/_shared/storage/__init__.py:292`).
See §8.

---

## 0. Verdict up front

The gap is real and the brief's framing survives verification:

```
$ grep -rniE "pre_migration|backup_before|snapshot.*migrat|migrat.*backup" \
      yadgar/backend/ yadgar/_shared/storage/
(no output)
```

`_run_migrations` (`yadgar/_shared/storage/migrations.py:1175`) →
`_run_migrations_locked` (`:1195`) walks 27 entries in `_MIGRATIONS` (`:1070`) and calls
`migration["fn"](self)` with no backup step anywhere above it. Several of those functions
are destructive-in-place: `_migration_001_hnsw_indexes` issues `REMOVE INDEX`
(`:35-37`), `_migration_004_branch_field` and `_migration_005_provenance_agent_field`
issue `UPDATE memory SET …` over the whole table (`:105-110`, `:88-94`).

Everything else in this system protects itself. This one does not:

| Operation | Protection | Where |
|---|---|---|
| nightly consolidation + vacuum | `backup → consolidate → vacuum → backup`, logical `/export` labelled `nightly-pre` / `nightly-post` | `yadgar/core/scripts/nightly_cycle.py:270`, `:386` |
| vacuum swap | `surreal_db.pre-vacuum-<TS>` quiesced copytree, keep-N | `yadgar/core/vacuum/phases.py:158` |
| named-volume DB move | `cp -a` to a temp sibling, rename into place, source volume kept as rollback | `yadgar/core/daemon/db_migrate.py:62-78` |
| **schema migration** | **none** | — |

Three hard parts, all worked below and none hand-waved: the only moment the store can
safely be copied is **before `surreal` opens it** (§2.1), which is *before* anything can
read `schema_version` to know whether a migration is pending (§2.2); and the process that
runs the migration is not necessarily the process that owns the store directory (§2.3).

---

## 1. Problem statement — with evidence

### 1.1 Who runs migrations, and when

`_init_schema` ends with `self._run_migrations()` (`migrations.py:1363`), and
`_init_schema` is called **inline from the `StorageEngine` constructor**
(`yadgar/_shared/storage/__init__.py:292` in server mode, `:350` in embedded mode).
`init_engines` constructs it unguarded at `yadgar/_shared/runtime/lifecycle.py:409`.

Two production processes reach that line:

| Process | Path to `init_engines` | When, relative to the store being open |
|---|---|---|
| **backend** (`yadgar-backend` container) | `embed_service` `lifespan` → `_start_queue_drainer` (`embed_service.py:467`) → `_ensure_recall_engines` → `StorageEngine` | uvicorn starts only after `entrypoint-backend.sh:202-222` sees `surreal` healthy — so **after** surreal opened `/data/surreal_db` |
| **core** (`yadgar` container) | `yadgar/core/server/_startup.py:86` → `core_init_engines` → `init_engines` | any time; it talks to the backend over `YADGAR_DB_URL` |

So the migration is an HTTP write against a **live, lock-held** surrealkv store, in a
process that may not even be on the same host filesystem as it.

### 1.2 The only safe copy point

ADR-0090 (open) records the load-bearing constraint: surrealkv skips its async store close
on every SIGTERM (`WARN surrealkv::lsm: No runtime available for closing the store
correctly` on 3/3 stops since Jul 1), and a half-flushed or half-copied surrealkv directory
is **corrupt-on-reopen**. `yadgar/core/daemon/db_migrate.py:16-30` states the same rule in
code, and refuses the copy outright when any container is running
(`:224-230`: *"Copying a live surrealkv store yields a corrupt-on-reopen directory
(ADR-0090)"*). `yadgar/core/backup/backup.py:14-25` splits `create_snapshot` into exactly
these two kinds for the same reason — logical export while live, copytree only when
quiesced.

The nightly ring's mechanism is therefore **unavailable at boot**: `create_snapshot(...,
backend_url=…)` is a `GET /export` over HTTP (`backup.py:170`), and at boot there is no
HTTP layer yet. The boot backup is a filesystem copy or it is nothing, and the copy must
happen while nothing holds the directory.

`entrypoint-backend.sh` already has that moment, and already uses it: the
`safe_start preflight` invocation at `:156` runs **before** `_start_surreal` at `:183`,
under an explicit exit-code protocol. That is the seam.

### 1.3 The chicken-and-egg

Gating on "a migration will actually run" needs the current applied set, which lives in the
`schema_version` table (`migrations.py:1196-1201` — one row per applied version, queried
with `SELECT version FROM schema_version WHERE version = $v`). There is no scalar "current
version"; it is a **set membership** test per migration. Reading it requires the store open,
which is after the safe copy point. §2.2 is the answer, and it is the crux of this car.

### 1.4 The migration flock does not serialise core against backend

`_run_migrations` takes `fcntl.LOCK_EX` on `STATE_DIR / ".migration.lock"`
(`migrations.py:1188-1193`). `STATE_DIR` is `_xdg_state_home() / "yadgar"`
(`yadgar/_shared/paths/paths.py:_state_dir`) — **not** under `DATA_DIR`, and not on any
shared mount: the core unit mounts `{volume}:/data` and the backend mounts
`{backend_data_dir}:/data` (`yadgar/core/daemon/systemd.py`, core `ExecStart` vs backend
`ExecStart`). Each container flocks a file in its own filesystem. The lock is real within a
process tree and inert across the two.

Flagged as evidence for §2.3, **not** fixed in this car.

---

## 2. The decided approach

### 2.1 Where the copy happens: `entrypoint-backend.sh`, after preflight, before `_start_surreal`

One new invocation between `:165` and `:172`:

```
python3 -m yadgar.backend.safe_start preflight --data-dir "${SURREAL_DATA_ROOT}"   # :156, existing
python3 -m yadgar.backend.pre_migration_backup --data-dir "${SURREAL_DATA_ROOT}"   # NEW
_start_surreal                                                                     # :183, existing
```

Why here and nowhere else:

- It is the only point in the lifecycle where the store is guaranteed unopened by this
  container **and** the code that knows the migration list is present. `safe_start` proves
  the pattern works (importable module + `python3 -m` + exit-code branch), so this car
  copies a shape that already ships rather than inventing one.
- It is a **funnel**. Every install surface — `yadgar-setup`'s generated units, `yadgar
  daemon start`, the `.in` templates, docker-compose — runs the same backend image and
  therefore the same entrypoint. Compare 0113 §1.4: one funnel, one implementation site.

**Rejected: start surreal, read `schema_version`, stop it cleanly, copy, restart, migrate.**
This is the obvious design and it is *wrong under ADR-0090*: every SIGTERM stop is a
corruption dice roll (3/3 unclean since Jul 1, the 2026-07-10 crashloop is one of them). It
would add a stop/start cycle to every boot-with-pending-migration, making the boot path more
dangerous than the migration it protects. This rejection is the strongest justification for
§2.2 — state it in the ADR, not just here.

**Rejected: a systemd `ExecStartPre` doing the copy.** The entrypoint comment at
`entrypoint-backend.sh:6-11` claims snapshots are "handled outside the container by the
systemd `ExecStartPre` `cp -r`". No such directive exists — the generated backend unit's
only `ExecStartPre` lines are `network create` / `stop` / `rm`
(`yadgar/core/daemon/systemd.py:186-188`). That comment is **stale and should be corrected
in this car**. Rebuilding it for real would mean re-implementing the gate on every install
surface (0110 is converging nine units precisely because that never stays in sync).

### 2.2 The gate: an inode-keyed stamp cache — the crux

Write a stamp file **beside** the store, at `{SURREAL_DATA_ROOT}/.schema-stamp.json`:

```json
{
  "applied": ["001_hnsw_indexes", …, "027_runtime_config_table"],
  "store_ino": 17301513,
  "written_at": "2026-08-01T03:15:00Z",
  "written_by": "backend 5.44.1"
}
```

The boot gate, entirely pre-open:

1. `surreal_db` absent → **first boot**, no-op, exit 0. (§ constraint 6.)
2. Stamp absent → **back up.**
3. `os.stat(surreal_db).st_ino` ≠ the recorded value → the directory has been *replaced*
   since the stamp was written, so the stamp describes a different store → **back up.**
4. Any version in the image's `_MIGRATIONS` not in `stamp["applied"]` → **back up.**
5. Otherwise → skip, and prune to the floor (§2.4).

**`st_dev` was considered and deliberately dropped — do not add it back.** The stamp is
written from two mount namespaces (the backend container writes `/data/surreal_db` from
`_run_migrations_locked`; the vacuum re-stamps `~/.local/share/yadgar/surreal_db` from the
**host**) and read from a third context. `st_ino` is preserved across a bind mount of the
same filesystem, but `st_dev` is a mount-scoped kernel value with no guarantee that a
rootless-podman userns sees the number the host wrote. A mismatching `st_dev` would fail
check 3 on **every** boot — the daily-full-copy regression this section exists to prevent,
surfacing only on a real host. `st_ino` alone is sufficient: ADR-0076 D4 already requires
these artifacts to be same-filesystem siblings, so a cross-filesystem replace is not
reachable, and a rename-in always allocates a fresh directory inode.

The stamp is a **cache whose validity key is the store directory's identity**. Every way the
canonical dir gets replaced — `safe_start`'s restore (`safe_start.py:189` copytree into
`canonical` after the old was moved aside), the vacuum swap's `os.rename`, `db_migrate.py:77`'s
`mv "$DST/$TMP" "$DST/surreal_db"` — produces a new directory inode, so an unhooked replace
site fails **conservative**: one spurious full copy, never an unprotected migration. That
asymmetry is the whole reason for the inode key.

`_run_migrations_locked` writes the stamp on completion (success path only, after the last
`CREATE schema_version`), reading the applied set it just observed.

**Two hooks are still required, and one of them is not optional:**

- **Vacuum swap — MUST re-stamp.** The nightly vacuum renames a rebuilt store into place
  *daily*. Without a hook, every post-vacuum boot fails check 3 and takes a full DB copy —
  a daily 250 MB copy for nothing. The vacuum knows the schema is unchanged (the side DB is
  built from an export of the same store), so after a retained swap it rewrites the stamp
  with the `applied` list **read from the pre-swap stamp** and the new `st_ino`. Ship this in
  the same commit; a deferred hook makes the car a disk-space regression.
  **If no pre-swap stamp exists, the hook writes nothing** and leaves the stamp absent, so
  the next boot takes one conservative copy. A hook that invents an `applied` list it never
  observed is the single way this design can silently skip a real migration — assert it
  (§4.4 test 20b).
- **`safe_start recover` — MUST invalidate.** A restore rolls the store back to an older
  point whose applied set is genuinely unknown, so it `unlink`s the stamp. Check 3 would
  catch it anyway; the explicit delete makes the intent readable and survives someone
  restoring by copying *into* the existing directory (the one path that preserves the
  inode — see the runbook constraint in §2.8).

**Rejected: put the stamp INSIDE `surreal_db/`.** It would travel with `cp -a`, copytree and
rename, needing zero hooks. Two reasons against, one fatal: (a) the live store contains
exactly `LOCK`, `manifest`, `sstables`, `vlog`, `wal` (verified by `ls -a` on the
workstation) — introducing a foreign file into a surrealkv store directory is unproven and
this is not the car to prove it; (b) a vacuum side-build produces a fresh store with no
stamp, so the daily-copy problem returns anyway. If someone later proves surrealkv tolerates
a dotfile, the in-dir variant is a strict simplification and this ADR should say so.

**Rejected: gate on a marker written by the *image* version rather than the applied set.**
Image version does not determine the applied set (a rollback to an older image leaves newer
migrations applied), and the applied set is what `_run_migrations_locked` actually keys on.

### 2.3 `_run_migrations` becomes backend-only

The gate in §2.2 is evaluated with the **backend image's** `_MIGRATIONS`. The core and
backend are versioned independently (`_backend_version()` reads `server.json`,
`yadgar/core/daemon/systemd.py:153`), so a core shipping migration `028` against a backend
image that knows `001–027` would migrate with the gate reporting "nothing pending". The
gate is unsound until one process owns migrations.

**Check first that this is codification, not a behaviour change.** The generated core unit
carries `Requires=yadgar-backend{suffix}.service` + `After=` (`systemd.py`, core `[Unit]`),
and the backend is readiness-gated — `--sdnotify=healthy` on podman, an `ExecStartPost`
health gate on docker — where `/health` returns 200 only when `db_ok and engine_loaded`
(ADR-0187's own reading of the same endpoint), i.e. after the backend's lifespan already ran
`_start_queue_drainer` → `StorageEngine` → `_init_schema` → `_run_migrations`. If that
holds on the VM, **under systemd the backend already always migrates first** and the core's
`_run_migrations` is reachable only under version skew or on non-systemd/dev setups. Then
this is codifying observed behaviour, which is a far cheaper change to justify.

Keep the split surgical:

- The core still runs `_init_schema`'s DDL — every statement is `DEFINE … IF NOT EXISTS`,
  idempotent and non-destructive, and dev/embedded modes depend on it.
- The core skips **only** `_run_migrations` — the part that does `UPDATE … SET` backfills
  and `REMOVE INDEX`.
- Gate it on a setting (`MIGRATIONS_OWNER`, or the simpler `RUN_MIGRATIONS` bool defaulting
  to true and set false in the core unit's env) so embedded/dev/test keeps today's
  behaviour and a skewed deployment can be un-wedged without a rebuild.

§1.4's finding — the flock is per-container and does not serialise core against backend —
is the second argument for this: today two processes can enter `_run_migrations_locked`
concurrently and nothing stops them.

### 2.4 Prefix and lifecycle — deliberately unlike the other two rings

Name: **`surreal_db.pre-migration-<from>-<to>-<TS>`**, e.g.
`surreal_db.pre-migration-026-027-20260801_031500`, where `<from>` is the highest migration
number in the stamp's applied list (`none` when the stamp is absent or invalid), `<to>` is
the highest number in the image's `_MIGRATIONS`, and `<TS>` is `%Y%m%d_%H%M%S` (matching
`surreal_db.pre-vacuum-*`). The name encodes **the schema transition**, which is the thing
that makes these different — a reader can tell at a glance which one is "the last good state
before 027".

Placement: a **sibling of `surreal_db`**, not under `{DATA_DIR}/backups/`. ADR-0076 D4:
*".old-* and .pre-vacuum-* STAY siblings of surreal_db — atomic-rename artifacts must remain
same-filesystem-adjacent to the DB dir"* (`docs/plans/archive/data-dir-hygiene-2026-07-09.md:23`),
and 0046 §9 forbids tidying them into the backups tree. Same-FS adjacency is also what makes
the `.incomplete-` → final rename atomic (§2.5) and what makes reflink possible (§2.6).

The three rings, side by side — this table is the answer to "different prefix, different
lifecycle policy":

| Ring | Artifact | Location | Retention keyed to | Caps |
|---|---|---|---|---|
| nightly | `surreal_db.nightly-{pre,post}-<TS>.surql` | `{DATA_DIR}/backups/surql/` | **time** | `YADGAR_BACKUP_RETENTION=3`; `scripts/cleanup-backups.sh` adds age/count/size caps |
| pre-vacuum | `surreal_db.pre-vacuum-<TS>/` | DB-adjacent | **operation** (last N vacuum runs) | `VACUUM_SNAPSHOT_RETENTION` (3 today, → 2 in 0046) |
| **pre-migration** | `surreal_db.pre-migration-<from>-<to>-<TS>/` | DB-adjacent | **schema transition** | keep newest per distinct `<from>`, at most `PRE_MIGRATION_BACKUP_KEEP=2` transitions, **floor 1**, **no age cap** |

Why no age cap: the value of one of these is "the last good state before schema N". That
value does not decay with wall-clock time — a host that has not migrated in six months
should still hold the artifact for the transition it last made. An age cap would delete
exactly the thing an operator reaches for when a six-month-old migration turns out to have
eaten data.

"Prune once a later migration has proven itself" needs a signal, and there is a cheap honest
one: **a boot that reaches gate step 5** (stamp valid, nothing pending) is proof that the
store opened and the migrated schema is what the running image expects. Prune to the floor
there.

Before taking a new copy, prune to **`KEEP - 1`**, not `KEEP` — prune-to-2 then add one
lands at 3. Getting the arithmetic right is also what actually frees the room the 1.5×
free-space check (§2.6) then measures, so the order is: reap `*.incomplete-*` → prune to
`KEEP-1` → free-space check → copy. Reclaiming after the disk check already failed is the
mistake 0046 §2.1 names on the vacuum's low-disk branch. §4.1 test 9 asserts the resulting
**count**, not merely the call order.

A naming wrinkle worth knowing, not worth fixing: an inode-mismatch backup has a complete
`applied` list but no *previous* stamp to read `<from>` from, so it is named
`…pre-migration-none-027-<TS>`. Repeated mismatches therefore collapse into one retention
slot under "newest per `<from>`" — harmless for disk, mildly confusing in a directory
listing.

**`scripts/cleanup-backups.sh` will not eat these** — verified: it globs `surreal_db_*`
(underscore) inside `${YADGAR_BACKUP_DIR:-~/.backups/yadgar/db}` (`:35`, `:67`, `:77`).
Different directory, and `surreal_db.` (dot) does not match `surreal_db_`. Nothing to change
there; say so in the plan so the next reader does not re-check. Same for 0046's reapers,
which glob `surreal_db.pre-vacuum-*` and `vacuum_export_*` — add an assertion, not code.

### 2.5 Atomicity, and only then the `safe_start` glob

The copy lands at `surreal_db.pre-migration-<from>-<to>-<TS>.incomplete-<pid>` and is
`os.rename`d to the final name on success. Stale `*.incomplete-*` siblings are reaped at the
top of every attempt. `db_migrate.py` is the precedent for all three moves —
`rm -rf "$DST"/surreal_db.migrating-*` then `cp -a` then `mv` (`:75-77`) — and its docstring
already carries the reasoning (*"so an interrupted copy can never leave a partial surreal_db
for the next backend start to open as the live DB"*).

One divergence from that precedent: `db_migrate` reaps behind a proven "nothing is running"
check (`:216-230`), and this car has no such precondition — a `yadgar daemon start` can race
a unit start, so two entrypoints can be staging at once. The reap must therefore only delete
an `*.incomplete-<pid>` whose pid is **dead** (`os.kill(pid, 0)`), with an age floor as the
fallback for a recycled pid. A blind `rm -rf` of the glob would delete a live staging copy
out from under a concurrent boot.

This is a **precondition** for the next change, not an adjacent nicety:

**Add `surreal_db.pre-migration-*` to `safe_start._CANDIDATE_GLOBS` (`safe_start.py:58-61`)
— but only once the rename-atomic copy is in place.** `choose_restore_source` picks by
**newest inner-file mtime** and `is_structurally_complete` only requires `manifest` plus one
non-empty subdir among `sstables`/`vlog`/`wal` (`:94-105`). A partial copy killed by
`TimeoutStartSec` mid-`cp` would be the newest thing on disk and can trivially satisfy both
checks while missing most of `sstables` — a corrupt restore source promoted to first choice,
a data-safety hazard this car would otherwise *introduce*. With the `.incomplete-` staging,
a partial never carries the final name and never matches the glob.

Earned once the glob is added: a pre-migration backup is arguably the **best** restore
candidate on the host, because it is the only one taken while provably nothing held the
store. `.old-*` and `.pre-vacuum-*` are quiesced by the vacuum's own stop sequence, which
ADR-0090 says is the unreliable part.

Two smaller siblings of the same issue:

- `entrypoint-backend.sh:320`'s inode-guard `case` enumerates sibling names explicitly
  (`surreal_db.old-*|.new-*|.building-*|.pre-vacuum-*|.CORRUPT-*`). An fd pointing at the new
  prefix would not be flagged. Add the pattern.
- `safe_start`'s split-brain preflight compares the canonical against `surreal_db.old-*`
  only; confirm the new prefix does not need to join that comparison (it should not — a
  pre-migration copy is by construction never newer than the canonical).

### 2.6 The start budget, honestly

The backend unit's `TimeoutStartSec=180` (`systemd.py:114`, `:121`; ADR-0187). The copy runs
**before** `surreal start` and long before the model load, so it is strictly additive to the
same budget that ADR-0187 sized for a cold model load.

**Do not invent a throughput number.** ADR-0187's own stance is that there is *no* measured
backend cold start anywhere in the repo and it says so plainly rather than implying one. Do
the same here:

- **Instrument the copy** — bytes copied and wall duration, logged at INFO on the normal
  path and included in the failure message. After one real migration on the VM there is a
  measurement, and the `TimeoutStartSec` question can be answered with data.
- **Do not bump `TimeoutStartSec` in this car.** Any bump belongs in 0110's converged
  renderer (touching the literal in two generators before they converge is exactly the drift
  0110 exists to end). That makes a bump a post-0110 follow-up, not a dependency of this car.
- **Reflink is opportunistic, not the design.** Verified on the workstation:
  `stat -f -c %T ~/.local/share/yadgar/` → `xfs`, and `xfs_info /` reports `reflink=1`, so
  `cp -a --reflink=auto` is a near-instant CoW clone *here*. On ext4 `--reflink=auto`
  silently falls back to a full byte copy, so **the full-copy path is the real design** and
  reflink is a bonus where the filesystem allows it. Two consequences to write down: the
  copy must be a `cp -a --reflink=auto` subprocess (`shutil.copytree` cannot reflink), and
  reflinked copies make `du` under-report — which is fine because the retention policy in
  §2.4 is count-keyed, deliberately not size-keyed.
- **Free-space preflight**, mirroring `_has_free_space`'s shape
  (`yadgar/core/vacuum/__init__.py:1433-1456`) but with a smaller multiplier: a vacuum peaks
  at ~2.5× the DB, this needs ~1× plus headroom → **1.5×**, knob
  `PRE_MIGRATION_BACKUP_MIN_FREE_MULTIPLIER`. Reflink availability is not detectable ahead
  of the copy, so the check assumes a full copy — conservative in the right direction.

Live sizes for scale: `surreal_db` is 250 MB, `{DATA_DIR}/backups` 435 MB, three
`surreal_db.pre-vacuum-*` siblings, 1.2 T free. The DB was 2.4 GB before the vacuum work, so
the multi-GB case is a real regime, not a hypothetical.

### 2.7 Failure semantics: fail closed on a genuine refusal, fail open on a tool break

The module's exit-code protocol extends `safe_start`'s (`safe_start.py:66-69`):

| Code | Meaning | Entrypoint action |
|---|---|---|
| 0 | backed up, or correctly skipped | continue |
| 5 | a migration is pending and the backup **failed** | **abort startup**, print the runbook pointer + the override |
| 6 | a migration is pending and there is **not enough free space** | **abort startup**, print the exact prune command |
| other non-zero (e.g. 127, packaging break) | the tool itself did not run | **WARN and continue** |

This mirrors `safe_start preflight` exactly — fail closed only on a genuine detection
(its exit 4), fail open on tool error (`entrypoint-backend.sh:163-165`) — and the reasoning
transfers: a bug in this module must not wedge every boot on every host, and ADR-0180 is the
in-repo precedent for a dead `python3 -m` packaging path shipping unnoticed. The split is
narrow: 5 and 6 are reached only after the module has *proved* a migration is pending, so
the fail-closed branch cannot fire on a host with nothing to migrate.

On fail-open the module writes a `PRE_MIGRATION_BACKUP_SKIPPED` marker into
`${YADGAR_LOG_DIR}` (the shape `TORN_STOP_MARKER` / `SPLIT_BRAIN_MARKER` already use,
`entrypoint-backend.sh:60-61`) so "we migrated unprotected" is observable rather than
inferred from a log line that has since rotated.

**Disk-full is handled explicitly** (code 6) because "abort loudly" and "wedge every boot on
a full disk" are the same behaviour if the message does not tell the operator how to get
out. The message must name, verbatim: the free bytes and the required bytes; the exact
`rm -rf` of the oldest `surreal_db.pre-migration-*` and `surreal_db.pre-vacuum-*` (never the
newest); and the `YADGAR_PRE_MIGRATION_BACKUP=off` escape hatch. Retention prunes before the
copy (§2.4), so the common shape of this failure is already self-healing.

### 2.8 What this backup is NOT — and how it is restored

**It is a copy of whatever state the store is in at boot.** If the previous stop was torn —
ADR-0090's chronic case, 3/3 unclean stops — the copy is a faithful copy of a torn store.
Its guarantee is *"the pre-migration state"*, not *"a known-good state"*. It does not replace
`safe_start`'s torn-manifest recovery, the nightly logical export, or the pre-vacuum
snapshot. Say this in one paragraph in the ADR so nobody mistakes it for a corruption guard.

**Restore procedure** (this is ADR-0090's own 2026-07-10 recovery, re-pointed):

1. `systemctl --user stop yadgar.service yadgar-backend.service`
2. `mv {DATA_DIR}/surreal_db {DATA_DIR}/surreal_db.CORRUPT-<TS>` — **move aside, never
   delete**; and note that copying *into* the existing directory instead is the one restore
   shape that preserves the directory inode and so leaves a stale stamp behind. The runbook
   must say move-aside for that reason as well as for forensics.
3. `cp -a --reflink=auto {DATA_DIR}/surreal_db.pre-migration-<from>-<to>-<TS> {DATA_DIR}/surreal_db`
4. `rm -f {DATA_DIR}/surreal_db/LOCK`
5. `rm -f {DATA_DIR}/.schema-stamp.json` — belt and braces. Step 3 already created a new
   directory inode, so gate check 3 would invalidate the stamp anyway; the explicit delete
   documents intent and covers an operator who deviates by copying *into* the existing
   directory. Do not read this step as load-bearing — step 2's move-aside is.
6. Pin the image back to the version whose migrations match, then start backend, then core,
   then `memory_stats()` to verify.

**No new CLI subcommand.** There is no `yadgar backup` surface today
(`yadgar/core/cli/` has no backup module) and adding one is a bigger seam than this car
justifies. Ship the runbook as a section in `docs/runbooks/` (or alongside ADR-0090's
existing pointer, `docs/plans/surrealkv-safe-stop-2026-07-10.md §6`) plus a
`MIGRATION_NOTES.md` entry, and get the *automatic* path for free by adding the glob to
`_CANDIDATE_GLOBS` (§2.5) — that is the cheap 80%.

---

## 3. Exact files and functions to change

| File | Change |
|---|---|
| `yadgar/backend/pre_migration_backup/` (new package) | `pre_migration_backup.py` + `__main__.py`, mirroring `yadgar/backend/safe_start/`'s layout exactly. Functions: `read_stamp(data_dir)`, `write_stamp(data_dir, applied)`, `pending_migrations(stamp, migrations)`, `stamp_is_valid(stamp, store_path)` (the `(st_dev, st_ino)` check), `snapshot_name(from_v, to_v, ts)`, `reap_incomplete(data_dir)`, `prune_pre_migration(data_dir, keep)`, `copy_store(src, dst)` (`cp -a --reflink=auto` subprocess + rename), `has_free_space(data_dir, size, multiplier)`, `main(argv)` returning the §2.7 exit codes. |
| `entrypoint-backend.sh` | new `python3 -m yadgar.backend.pre_migration_backup` block between `:165` and `:172`, with the 5/6-vs-other exit branch. Add `surreal_db.pre-migration-*` to the inode-guard `case` at `:320`. Correct the stale "systemd ExecStartPre `cp -r`" claim at `:6-11`. |
| `yadgar/_shared/storage/migrations.py` | `_run_migrations_locked` (`:1195`) — write the stamp after the loop completes, on the success path only; collect the applied set it observed. `_run_migrations` (`:1175`) — early-return when the process is not the migration owner (§2.3). |
| `yadgar/_shared/storage/__init__.py` | nothing structural; the constructor call at `:292` is untouched — but this is the line 0027c also edits (§8). |
| `yadgar/backend/safe_start/safe_start.py` | `_CANDIDATE_GLOBS` (`:58-61`) gains `surreal_db.pre-migration-*`. The `recover` path unlinks `.schema-stamp.json` after a restore. |
| `yadgar/core/vacuum/__init__.py` | after a **retained** swap in `_vacuum_finalize`, re-stamp `.schema-stamp.json` with the applied set read from the pre-swap stamp and the new store `st_ino`; write nothing when there was no prior stamp (§2.2). Non-fatal on error (a missing stamp costs one copy). |
| `yadgar/_shared/config/config.py` + `config_registry.py` + `config_yaml.py` | register `PRE_MIGRATION_BACKUP_ENABLED=True`, `PRE_MIGRATION_BACKUP_KEEP=2`, `PRE_MIGRATION_BACKUP_MIN_FREE_MULTIPLIER=1.5`, and the §2.3 migration-owner knob — for discoverability, docs and the pre-commit three-way sync (all three files or the hook fails). **The boot module does NOT read them through the settings machinery** — see the note below. |

**The boot module must read its knobs with `os.getenv`, not `get_settings()`.** `safe_start`
imports nothing from `yadgar._shared.config` — its imports are `argparse`/`shutil`/`sys`/
`datetime`/`pathlib` plus `observe` (`safe_start.py:43-50`) — and that is not an accident:
migration `027_runtime_config_table` plus the `config_get` resolver mean settings resolution
can be **DB-backed**, and at this point in the entrypoint surreal is not running. The in-repo
precedent for exactly this shape is `yadgar/core/backup/backup.py:51-56`
(`int(os.getenv("YADGAR_BACKUP_RETENTION", "3"))`, read live so tests can monkeypatch without
a reload). Pin the module's import list with a test (§4.1 test 11) so a later refactor cannot
quietly reintroduce a DB dependency into a pre-DB code path.
| `docs/reference/configuration.md` | rows for the four knobs (the backup block near `:606`). |
| `docs/CHANGELOG.md` | the new boot-time artifact class and its retention contract, stated explicitly (the v5.169 entry at `:161` is the house form for this). |
| `docs/runbooks/` (or ADR-0090's §6 pointer) + `MIGRATION_NOTES.md` | the §2.8 restore procedure; the one-time note that the first boot after upgrade takes a full copy (§5). |

---

## 4. The TDD story

**CI gating asymmetry.** `.forgejo/workflows/ci-pr.yaml` runs by directory: `test-fast` =
`yadgar/tests/{scripts,server,hooks,_meta,clients}/`, `test-shared` = `yadgar/tests/_shared/`,
`test-backend` = `yadgar/tests/backend/`, `test-core` = `yadgar/tests/core/`. Nothing under
`yadgar/tests/integration/` is gated in `ci-pr`. Putting the logic in an importable module
(§3) is what makes this car testable in gated directories at all — the shell reduces to
`python3 -m …` plus an exit branch, exactly as `safe_start` does.

### 4.1 RED first — `yadgar/tests/backend/test_pre_migration_backup.py` (gated `test-backend`)

1. **`test_no_backup_when_stamp_matches`** / **`test_backup_when_stamp_lists_fewer_versions`**
   — the pair that **encodes the whole car**. Seed a fake store dir + a stamp; in the first
   case `applied` covers every version in a stubbed `_MIGRATIONS`, in the second it is short
   by one. Assert exactly zero / exactly one `surreal_db.pre-migration-*` afterwards. If
   either passes with the gate deleted, it is mis-written.
2. **`test_backup_when_store_inode_changed`** — write a valid stamp, then replace the store
   dir and assert the backup fires despite `applied` being complete. Spell the sequence out,
   because a single `os.rename(sibling, surreal_db)` **fails on POSIX** against an existing
   non-empty directory: `os.rename(store, aside)` then `os.rename(sibling, store)`. This is
   the §2.2 restore-rollback hole and the load-bearing test for the whole gate — it must fail
   if the `st_ino` check is deleted; verify that locally before believing it.
3. **`test_first_boot_is_a_clean_noop`** — no `surreal_db`, no stamp: exit 0, nothing
   created, no marker written. (§ constraint 6.)
4. **`test_partial_copy_never_takes_the_final_name`** — make `copy_store`'s subprocess fail
   part-way; assert only a `*.incomplete-*` remains and that `list_restore_candidates`
   (imported from `safe_start`) does **not** return it. This is the §2.5 hazard, asserted
   across the two modules rather than within one.
5. **`test_stale_incomplete_dirs_are_reaped_before_a_new_attempt`**.
6. **`test_insufficient_space_exits_6_and_names_the_prune_command`** — monkeypatch
   `shutil.disk_usage`; assert the exit code *and* that stderr contains the free/required
   bytes and an `rm -rf` that cannot match the newest snapshot.
7. **`test_backup_failure_exits_5`** and **`test_unexpected_error_does_not_exit_5`** — the
   §2.7 split. The second is the one that keeps a module bug from wedging every boot.
8. **`test_retention_keeps_newest_per_from_version_and_never_reaches_zero`** — parametrised
   over `keep ∈ {0, -1}` and over a set where every artifact is months old; at least one
   `surreal_db.pre-migration-*` survives in every case. Same weight as 0046 §4.3's floor
   test: a data-safety artifact must never be pruned to nothing.
9. **`test_prune_to_keep_minus_one_then_copy_lands_at_keep`** — the §2.4 arithmetic. Seed
   `KEEP` artifacts, run with a pending migration, assert the **final count is exactly
   `KEEP`** and (ordered call recorder) that the prune preceded the free-space check.
   Asserting only the call order lets the off-by-one through.
10. **`test_snapshot_name_encodes_the_transition`** — `026`→`027`, and `none`→`027` when the
    stamp is absent.
11. **`test_module_does_not_import_the_settings_machinery`** — assert
    `yadgar._shared.config` is absent from `sys.modules` after importing the module in a
    fresh interpreter (or pin the import list). This is what keeps a pre-DB code path from
    acquiring a DB dependency (§3).
12. **`test_incomplete_dir_of_a_live_pid_is_not_reaped`** — the §2.5 concurrency guard.

### 4.2 RED first — `yadgar/tests/_shared/test_schema_stamp.py` (gated `test-shared`)

13. **`test_run_migrations_locked_writes_the_stamp_on_success`** — and only on success:
    make the last migration raise and assert **no** stamp is written (a stamp written after
    a partial run would claim protection that does not exist).
14. **`test_stamp_records_the_store_inode`** — `store_ino` matches `os.stat` of the DB path,
    and no `store_dev` key is written (§2.2).
15. **`test_core_process_does_not_run_migrations`** / **`test_core_still_runs_init_schema_ddl`**
    — the §2.3 split, both halves. The second stops a naive implementation from taking the
    DDL out with the migrations.

### 4.3 RED first — `yadgar/tests/scripts/test_entrypoint_backend_contract.py` (gated `test-fast`)

16. **`test_backup_invocation_precedes_start_surreal`** — text assertion over
    `entrypoint-backend.sh`: the `pre_migration_backup` line's index is greater than the
    `safe_start preflight` line's and less than `_start_surreal`'s. Same technique the repo
    already uses for unit text (`test_runtime_readiness_cross_generator.py`).
17. **`test_exit_5_and_6_abort_but_other_nonzero_continues`** — assert the shell branch
    shape, not just the presence of the call.
18. **`test_inode_guard_case_covers_the_new_prefix`**.

### 4.4 RED first — `yadgar/tests/core/test_vacuum_schema_stamp.py` (gated `test-core`)

19. **`test_retained_swap_restamps_with_the_new_inode`** — without this, the daily vacuum
    costs a daily full copy (§2.2). This is the test that keeps the car from being a disk
    regression.
20. **`test_restamp_failure_is_non_fatal_to_the_vacuum`**.
20b. **`test_swap_with_no_prior_stamp_writes_no_stamp`** — the §2.2 "never fabricate an
    `applied` list" rule. A hook that invents one is the single way this design can silently
    skip a real migration, so it needs a test rather than a comment.
21. **`test_0046_reapers_do_not_match_the_pre_migration_prefix`** — an assertion, since §2.4
    concluded no code change is needed there; it pins that conclusion.

### 4.5 Mutation-sensitivity note

Tests 1, 2, 4, 8, 19 and 20b are the ones that would survive a plausible-looking wrong
implementation. Test 2 in particular must fail if the `st_ino` check is dropped — verify
that by deleting the check locally before believing it.

---

## 5. Verification

**Local**

1. `pytest yadgar/tests/backend/ yadgar/tests/_shared/ yadgar/tests/scripts/ yadgar/tests/core/ -k "migration or stamp or entrypoint or safe_start"`.
2. `pytest yadgar/tests/server/test_config_three_way_sync.py yadgar/tests/server/test_config_default_values.py` — separately gated CI steps; both fail on a half-registered knob.
3. Pre-commit `check-config-three-way-sync`.
4. Every filesystem behaviour (staging rename, reap, retention, free-space, first boot) is
   provable in `tmp_path`. Only the three items below actually need a VM.

**Fresh VM — `192.168.122.101`**
(`sshpass -p 'Aa1234.' ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password root@192.168.122.101`)

5. **The §2.3 premise.** Install, boot both units, and read the journal: confirm the backend
   completes `_run_migrations` before the core's `StorageEngine` is constructed. If it does
   not, §2.3 is a behaviour change rather than codification and the ADR must say so.
6. **A real migration, end to end.** Install an image at N−1 migrations, let it settle, then
   upgrade to N. Assert: one `surreal_db.pre-migration-<N-1>-<N>-<TS>` exists; the stamp's
   `applied` now includes N; the backend reached healthy; and the **measured copy duration
   and byte count** appear in the log — this is the number ADR-0187's arithmetic is missing,
   and it is what a later `TimeoutStartSec` decision would be built on.
7. **Idempotence.** Restart the backend twice more: zero new artifacts, and the second
   no-migration boot pruned to the floor.
8. **The restore drill.** Follow §2.8 verbatim against the artifact from step 6 and confirm
   `memory_stats()` returns the pre-migration counts. A backup nobody has restored is
   theatre; do this once, on the VM, before the car is called done.
9. **The disk-full drill.** Fill the data filesystem, restart with a pending migration,
   assert exit 6, the message names the prune command, and the unit fails **without** having
   touched the store.

**On the workstation** — read-only: confirm the FS facts this plan is built on still hold
(`stat -f -c %T`, `xfs_info | grep reflink`), and check `du` of the DB before planning the
first upgrade.

---

## 6. Rollback story

Three independently revertible pieces:

- **The entrypoint block** reverts by deleting the invocation; the backend boots exactly as
  today.
- **The stamp write** in `_run_migrations_locked` is additive; a leftover
  `.schema-stamp.json` is inert once nothing reads it, and can be deleted by hand.
- **`_CANDIDATE_GLOBS`** reverts to two globs, and `safe_start` stops considering
  pre-migration copies as restore sources.

The §2.3 migration-owner change is the one with a live-state asymmetry: reverting it while a
skewed deployment is running restores the concurrent-migration hazard §1.4 describes. That
hazard exists on `master` today, so the revert is a return to the status quo, not a new
failure — but say it out loud in the ADR.

Nothing here deletes data on the revert path. The one irreversible action this car can take
is retention pruning an old `surreal_db.pre-migration-*`, which is why §4.1 test 8 (the
never-zero floor) carries the most weight of any test in the car.

---

## 7. ADRs

- **A new ADR is warranted**, and it should be about the *policy*, not the plumbing:
  "schema migrations take a pre-migration filesystem snapshot at backend boot; it is
  schema-keyed, DB-adjacent, never age-pruned below one, and its absence aborts the boot."
  That is a standing operational contract, and the reasoning a future reader will otherwise
  re-litigate is specifically: why not stop/start surreal to read `schema_version` (§2.1),
  why the stamp is inode-keyed (§2.2), and why the retention is not the nightly ring's
  (§2.4).
- **ADR-0090 (open)** is the binding prior and is **not** superseded. It supplies the
  copy-point rule (§2.1), the reason a stop/start gate is rejected, the restore procedure
  (§2.8), and the caveat that a copy of a torn store is still torn (§2.8). Cite it in the
  code comment at the copy site, not only in the plan.
- **ADR-0076 D4** governs placement (DB-adjacent siblings for atomic-rename artifacts) and
  is **extended, not superseded** — a third sibling class joins `.old-*` and `.pre-vacuum-*`.
- **ADR-0187** is untouched: no `TimeoutStartSec` literal changes here (§2.6). If a later
  measurement justifies a bump, that supersedes ADR-0187's arithmetic and belongs to whoever
  owns the converged renderer after 0110.
- **ADR-0078** (backend owns the DB) is the standing prior that §2.3 finally makes true for
  migrations. Cite; do not supersede.
- **ADR-0180** is the precedent for the fail-open-on-packaging-break branch (§2.7).

---

## 8. Ordering / dependencies vs the rest of the train

Train head today: **0107 → 0111 → 0113 → 0046**, with 0110 after 0111.

- **HARD: after 0027c.** 0027c restructures core startup around
  `StorageEngine.__init__` → `_init_schema()` (`yadgar/_shared/storage/__init__.py:292-294`,
  quoted in its §1.1) — the same constructor this car gates `_run_migrations` behind (§2.3).
  Two cars editing that seam concurrently is a guaranteed conflict and, worse, a guaranteed
  *semantic* conflict: 0027c wants the constructor to tolerate an absent backend, this car
  wants it to not migrate at all in the core. Land 0027c first and build the owner gate on
  top of whatever shape it leaves.
- **After 0046, or coordinate the glob.** 0046 rewrites the vacuum reapers
  (`_reap_export_pairs`, the single `_reap_vacuum_residue` call site). §2.4 concludes those
  globs cannot match `surreal_db.pre-migration-*`, but that conclusion is about code 0046 is
  actively rewriting — so land after it and keep §4.4 test 21 as the pin.
- **Interacts with 0110, but does not depend on it.** 0110 converges nine unit generators;
  this car changes `entrypoint-backend.sh`, which is inside the image and rendered by
  neither generator. No textual conflict. The *only* coupling is §2.6's deferred
  `TimeoutStartSec` question, which is deliberately pushed past 0110 so the literal is
  changed once, in one renderer.
- **Independent of 0111 and 0113** (vacuum write-gate / core-up-during-vacuum) except for
  §2.2's vacuum re-stamp hook, which lands in `_vacuum_finalize` — a function 0113 wraps and
  0046 edits. Sequencing after both keeps that hook a two-line addition instead of a
  three-way merge.
- **0107 and 0112 are unrelated.**

**One-time cost on every existing install:** the first boot after this ships finds no stamp
and a populated store, so it takes one full pre-migration copy even when nothing is pending.
That is the correct conservative behaviour and it should not be special-cased away — but it
must appear in `MIGRATION_NOTES.md` with the expected size and duration so an operator is
not surprised by a slow first restart and 250 MB of new disk.

---

## 9. Explicitly out of scope

- Fixing the `STATE_DIR` migration flock so it actually serialises core against backend
  (§1.4). Flagged as evidence for §2.3; a real fix means relocating the lock onto a shared
  mount, which is its own decision.
- A `yadgar backup` / `yadgar restore` CLI surface (§2.8). Runbook plus the `safe_start`
  glob is the cheap 80%.
- Any change to what a migration *does*, to `_MIGRATIONS` ordering, or to the
  append-only-never-reorder rule at `migrations.py:1070`.
- Backing up before *every* boot, or before consolidation / vacuum — those already have
  their own protection (§0 table).
- Logical (`.surql`) pre-migration exports. The whole point of §1.2 is that the HTTP layer is
  not up at the safe copy point; a logical export would have to run after surreal opens the
  store, which is after the migration window has already been entered.
- Moving `.pre-migration-*` under `{DATA_DIR}/backups/` — ADR-0076 D4 and 0046 §9 both
  forbid it, for the same same-filesystem-adjacency reason.
- Compressing or deduplicating the snapshots beyond whatever `--reflink=auto` gives for free.
