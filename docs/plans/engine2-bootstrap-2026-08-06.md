# Engine-#2 operational bootstrap — plan (2026-08-06)

Status: DRAFT — written for adversarial audit before any code.

Binding decisions: ADR-0195 (split store), ADR-0196 (backup half — engine #2
leaves the vacuum pipeline), ADR-0203 (this is its own train; creates `config`
schema-only), ADR-0204 (quiesce mechanism), ADR-0205 (MariaDB verified viable).

> **Landmine when following those citations.** Split-store §5.1 still teaches
> `SELECT MAX(number)+1 … FOR UPDATE` for ADR numbering. That mechanism is
> **RETIRED by ADR-0197** — the `AUTO_INCREMENT` id IS the number. §5.2 (the
> backup half) is unaffected and still binds. Car 11 fixes the doc; until then,
> do not implement §5.1's identity half.

> **Train-shape note.** ADR-0203 says this is its OWN train. The user directed
> on 2026-08-06 that it ship as cars 2–7 of the combined strict-typing train
> instead. That is a deliberate override, not drift — car 11 carries an
> amending ADR so the record matches reality rather than leaving 0203
> contradicted.

---

## 1. Why this exists at all

Split-store §2 requires four operational arms — backup, restore-verification
enumeration, migrations, cross-engine `check_invariants` — to be in place
before the first engine-#2 row. The forcing incident is 2026-06-16: a partial
restore **passed a `>=` check** and destroyed 3,622 memories. The check was
satisfied while the data was not. That is the specific failure this train
exists to make impossible, and it is why restore-verification is an arm rather
than a test.

The second reason is ordering. Both the spine train (0047) and the knob train
(0035) block on this, so every week it is not done is a week two trains cannot
start.

## 2. Scope

Creates the `config` table in MariaDB, **schema-only, zero rows**, and ships
the four arms against it.

**Why zero rows is load-bearing:** per knob plan §0.1.3 the gate on task 0095's
free-re-key window is the first `config_set`, NOT the schema. An empty table
exercises every arm while leaving the project-identity window open. Seeding is
the knob train's job, and it must not happen here.

**Why `config` and not a ledger table:** it has zero rows and zero production
readers today (`config_list()` → `[]`, verified), so a restore-verification bug
found here costs nothing. The same bug found while migrating 195 ADRs costs the
ADR corpus.

## 3. Non-goals

- **No seeding.** Not one row. See §2.
- **No second vacuum arm.** ADR-0196: InnoDB has no dead-row-version problem;
  the export → side-build → atomic-swap machinery does not get a twin.
- **No ABC / StorageProtocol wiring.** Two concrete storage classes at the
  existing composition root. `StorageProtocol` has zero consumers and is
  read-only, so it could not carry engine #2 even if wired.
- **No ledger tables.** `task`, `adr`, `agent_pattern`, `agent_discipline` are
  the spine train's (0047), on arms this train proves.
- **No float widening.** That gates Batch 1 seeding (ADR-0207), not the schema.

## 4. The four arms

### 4.1 Backup (car 4)
MariaDB-native `mariadb-backup`: full, plus incremental via LSN. Engine #2
never enters the vacuum pipeline.

### 4.2 Quiesce (car 5) — ADR-0204
Assert the existing maintenance write-gate → drain the queue → snapshot
MariaDB → snapshot Surreal → release.

Reads stay available; only writes block.

**The gate already exists, and so does most of the belt** (verified
2026-08-06). `_maintenance_mode` lives at `_shared/runtime/state.py:172`,
toggled by `POST /api/control/maintenance/enter|exit`
(`core/server/routes/control.py:631`) and enforced at `core/server/_app.py:540`,
where every DB-backed MCP tool fast-fails with a structured error. The
release-on-abort belt is `_maintenance_deadline` (task:0113): `_app.py`
treats an expired deadline as "not in maintenance" and self-clears LOUDLY.
Its docstring is explicit that this is the only backstop covering SIGKILL,
OOM-kill and power loss, which `cmd_vacuum_impl`'s `finally` does not. So car
5 REUSES this deadline — it must not invent a second belt.

**The gap this exposes, and it is the real design question:** the gate makes
*MCP tools* fast-fail. It does not stop backend-internal writers — the nightly
cycle, the queue drainer, consolidation. "Gate asserted" is therefore NOT
"no writes in flight", and a snapshot taken on that assumption is exactly the
inconsistency ADR-0204 exists to prevent. Car 5 must establish what silences
those writers, or prove they cannot write during the window.

Draining before snapshotting is what makes the two snapshots describe one
instant; an undrained queue means in-flight writes belong to neither.

### 4.3 Restore-verification enumeration (car 6)
The arm that exists because a `>=` check passed on a partial restore.
**Enumeration, not aggregate comparison**: per-table row identity, not counts.
`count(restored) >= count(expected)` is precisely the shape that failed.

### 4.4 Cross-engine `check_invariants` (car 7)
Assertions spanning both engines. ADR-0209 hands this a cheap one for free:
`content_hash` is mirrored page-and-row, so a mismatch is desync, and a row
whose hash names absent content is a detectable row-without-page orphan
(ADR-0201's accepted failure direction).

## 5. Cars

| car | scope | depends on |
|---|---|---|
| 2 | MariaDB service in docker-compose; re-add `asyncmy` to pyproject (it left with PR #32 and is currently declared nowhere); connection + composition root | — |
| 3 | Alembic adoption + first revision: `config`, schema-only | 2 |
| 4 | Backup arm (§4.1) | 3 |
| 5 | Quiesce + release-on-abort belt (§4.2) | 4 |
| 6 | Restore-verification enumeration (§4.3) | 4 |
| 7 | Cross-engine `check_invariants` (§4.4) | 3 |

Cars 4/6 and 5 and 7 are parallelisable in worktrees once 3 lands.

**On "same commit as the first row":** split-store §2's wording is about
ordering relative to the first ROW, and this train writes none. Cars are
commits merged as ONE PR, so the arms land together at PR granularity. Nothing
here licenses seeding a row before the arms exist.

## 6. Exit criteria

1. The `config` table exists **in MariaDB** and is empty, asserted against the
   engine directly — AND the read path is proven to resolve to it.
   `config_list() == []` is NOT sufficient evidence: it returned `[]` before
   this train too, so it cannot distinguish "MariaDB table exists and is empty"
   from "the read still goes to Surreal".
2. An Alembic migration runs green — this closes task 0051 and most of 0048.
3. A full backup is taken and **restored into a scratch instance**, verified by
   enumeration, not counts.
4. A deliberately PARTIAL restore is **rejected** — the 2026-06-16 regression
   test. Without this the arm is unproven.
5. The write-gate releases on abort, proven by killing the process mid-window.
6. `check_invariants` passes cross-engine and FAILS on an injected desync.
7. Task 0095's window is still open (no `config_set` has run).

## 7. Risks

- **Restore-verification is the one path that cannot be tested in production.**
  It must be exercised against a scratch instance, and the partial-restore
  rejection (exit 4) is the only evidence that it works.
- **The gate can wedge writes.** Highest-blast-radius new failure mode here.
- **MariaDB idle RSS is measured at 86.6 MB (ADR-0205) but that is a FLOOR** —
  empty DB, default config, zero connections. Re-measure under load before
  the knob train seeds.
- **Container discipline.** Any container this train starts must be
  timeout-wrapped, resource-capped and torn down; an agent that starts one owns
  stopping it.

## 8. Questions — resolved by inspection, and what is genuinely open

**Answered 2026-08-06 by reading the tree** (recorded so the audit attacks
decisions rather than re-deriving facts):

- **Write-gate location / ADR-0078 reachability.** Core-side state, flipped
  over the control HTTP route — see §4.2. The backup path asserts it the same
  way the nightly cycle already does, so no new core→DB path is created.
- **`asyncmy` sync-vs-async.** SETTLED: `asyncmy` is an async driver and MUST
  be paired with `create_async_engine`. PR #32 paired `mysql+asyncmy://` with a
  sync `create_engine`, which fails at runtime; that defect must not recur.
- **Alembic vs the 26 hand-rolled Surreal migrations.** Independent version
  state — Surreal's own path holds `.migration.lock`
  (`_shared/storage/migrations.py:1192`), Alembic's version table lives in
  MariaDB. They do not need to agree with each other, but `check_invariants`
  SHOULD assert each is at head, because task 0115 records that migrations run
  in BOTH processes and that lock does not serialise them.

**Genuinely open — for the audit:**

1. **What silences backend-internal writers during the snapshot window?**
   (§4.2.) The maintenance gate only fast-fails MCP tools. This is the highest-
   value question here, because a wrong answer produces a corrupt-but-passing
   backup, which is the 2026-06-16 shape.
2. What is the scratch-instance story for exit 3/4 — a throwaway container per
   run, or a persistent verification target?
3. Does `mariadb-backup` need a privileged container or host mount that the
   current 4 GB backend container cannot give it?
