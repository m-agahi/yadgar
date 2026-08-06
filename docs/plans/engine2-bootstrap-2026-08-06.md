# Engine-#2 operational bootstrap — plan (2026-08-06)

Status: DRAFT — written for adversarial audit before any code.

Binding decisions: ADR-0195 (split store), ADR-0196 (backup half — engine #2
leaves the vacuum pipeline), ADR-0203 (this is its own train; creates `config`
schema-only), ADR-0204 (quiesce mechanism), ADR-0205 (MariaDB verified viable).

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

Reads stay available; only writes block. **The release-on-abort belt is not
polish** — a gate that fails to release wedges every write, so it must survive
process death, not merely exception paths.

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

1. `config` exists in MariaDB with zero rows; `config_list()` still returns `[]`.
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

## 8. Open questions for the audit

1. Where exactly does the write-gate live today, and is it reachable from the
   backup path without a core→backend violation (ADR-0078/0200)?
2. What is the scratch-instance story for exit 3/4 — a throwaway container per
   run, or a persistent verification target?
3. Does `mariadb-backup` need a privileged container or host mount that the
   current 4 GB backend container cannot give it?
4. Alembic's migration table lives in MariaDB while 26 hand-rolled Surreal
   migrations remain — does `check_invariants` need to assert the two migration
   states are compatible, or are they genuinely independent?
5. Is `asyncmy` sync-vs-async settled? PR #32 paired `mysql+asyncmy://` with a
   sync `create_engine`, which fails at runtime. This train must not repeat it.
