# Engine-#2 operational bootstrap — plan (2026-08-06)

Status: AUDITED — verdict build-with-changes; all blockers and concerns
resolved by the user 2026-08-06. Ready to build.

Binding decisions: ADR-0195 (split store), ADR-0196 (backup half — engine #2
leaves the vacuum pipeline), ADR-0203 (own train; `config` schema-only),
ADR-0204 (quiesce — **read the amendment ADR-0210 first**), ADR-0205
(MariaDB viable), **ADR-0210** (audit corrections: gate blocks reads, backup
is a nightly step, train shape).

> **Landmine when following those citations.** Split-store §5.1 still teaches
> `SELECT MAX(number)+1 … FOR UPDATE` for ADR numbering. That is **RETIRED by
> ADR-0197** — the `AUTO_INCREMENT` id IS the number. §5.2 (backup) is
> unaffected and still binds. Car 11 fixes the doc; do not implement §5.1's
> identity half.

> **Train-shape note.** ADR-0203 says this is its OWN train. The user directed
> it ship as cars of the combined strict-typing train. Deliberate override,
> recorded in **ADR-0210** — ADR-0203 remains the citation for WHY this is a
> distinct body of work with four arms landing together, but no longer for how
> it is packaged.

---

## 1. Why this exists

Split-store §2 requires four operational arms — backup, restore-verification
enumeration, migrations, cross-engine `check_invariants` — before the first
engine-#2 row. Forcing incident: 2026-06-16, a partial restore **passed a `>=`
check** and destroyed 3,622 memories. The check was satisfied while the data
was not. That is why restore-verification is an arm, not a test.

Both the spine train (0047) and the knob train (0035) block on this.

## 2. Scope

Creates `config` in MariaDB, **schema-only, zero rows**, and ships the arms.

**Zero rows is load-bearing:** knob plan §0.1.3 puts task 0095's re-key gate at
the first `config_set`, NOT the schema. An empty table exercises every arm
while leaving that window open. Seeding is the knob train's job.

**The table will be UNREFERENCED when this train ends, and that is correct.**
Config reads are core-in-process (`core/server/tools/_runtime_config.py:23-26`)
and backend config ops are write-only (`backend/admin_exec/runtime_config.py`).
Repointing reads at MariaDB needs ADR-0200's backend-read-op + PTC + core
forward, which ADR-0200 itself calls a BUILD. That is the **knob train's**
work. Stating it here so an unreferenced table does not later read as dead code.

> Knob plan §G's claim that "nothing above those four mixin methods changes"
> is FALSE for the read half. Car 11 corrects it.

## 3. Non-goals

- **No seeding.** Not one row in `config`.
- **No second vacuum arm** (ADR-0196).
- **No ABC / StorageProtocol wiring** — two concrete storage classes at the
  composition root. `StorageProtocol` (`_shared/contracts/protocols.py:167`)
  has zero non-test consumers and is read-only.
- **No ledger tables** — spine train (0047).
- **No float widening** — gates Batch 1 seeding (ADR-0207), not the schema.
- **No read-path repoint** — see §2.

## 4. The arms

### 4.1 Backup — `mariadb-dump`, physical DEFERRED not dropped
For a table whose target state is zero rows, logical dump over TCP proves the
arm end-to-end. ADR-0196 explicitly sanctions `mariadb-dump` for logical
snapshots.

`mariadb-backup` (physical, full + incremental via LSN) is **deferred to the
spine train**, when there is data worth incrementing over. It is not dropped —
ADR-0196's rationale for leaving the vacuum pipeline rests on that capability.
Physical backup needs datadir filesystem access and **cannot** run in the
backend container (`docker-compose.yml:49,70` — `/data:ro`, `read_only: true`);
it needs the MariaDB container or a datadir-volume sibling. Undesigned, and
deliberately so for now.

### 4.2 Quiesce — a nightly step (amends ADR-0204)

**ADR-0204's decision text is FALSE; ADR-0210 withdraws it.** It says "reads stay
available; only writes block". The gate short-circuits EVERY MCP tool including
reads (`core/server/_app.py:517-518`, enforcement `:540-559`); no read exemption
exists. During the window `recall`, `wiki_read` and `config_get` all fast-fail.
Only ungated viz HTTP endpoints survive.

**Accepted:** the window is a full MCP outage. Stop-both-engines is still
rejected, but for a different reason than the ADR gave — the daemon stays up so
connected MCP clients keep their connections (Car 0111's property). Read/write
tool classification is filed as a follow-up, not built here.

**Backup runs as a step of the nightly cycle.** Maintenance windows NEST by
design (`core/server/routes/control.py:675-692`), so an independently-scheduled
backup could snapshot mid-consolidation-write. Folding it into nightly makes
overlap structurally impossible — one holder, one window — and reuses the
outage that already exists rather than adding a second.

Sequence: assert gate → **verified-empty** drain → snapshot MariaDB → snapshot
Surreal → release.

- Drain is verified via the existing `drain_now` admin op
  (`backend/admin_exec/drain.py:25-50`), which returns `items_processed`.
- The backup **HARD-FAILS if it cannot hold the gate.** Nightly's own entry is
  best-effort — it proceeds ungated if core is unreachable
  (`core/scripts/nightly_cycle.py:259-275`). A backup must not inherit that:
  proceeding ungated yields a silently corrupt snapshot.

The free-running-writer fear was checked and is mostly unfounded: the drainer
writes only on a non-empty queue (`backend/queue_drainer/__init__.py:130`) and
consolidation is request-driven (`backend/consolidation/service.py`). The real
hazard was the concurrent gate-holder, addressed above.

### 4.3 Restore-verification enumeration
**Enumeration, not aggregate comparison** — per-table row identity, not counts.
`count(restored) >= count(expected)` is precisely the shape that failed on
2026-06-16.

Must live **inside the real restore path**, the way the 06-16 guard sits inside
the vacuum swap gate (`core/vacuum/__init__.py:101-102, :1966`). A check that
exists only as pytest against a scratch container is unproven exactly where the
incident happened.

### 4.4 Cross-engine `check_invariants`
Assertions spanning both engines. ADR-0209 supplies a cheap one: `content_hash`
is mirrored page-and-row, so a mismatch is desync and a row whose hash names
absent content is a detectable row-without-page orphan (ADR-0201's accepted
failure direction). Wiring exists — `backend/admin_exec/invariants.py:1-14`.

## 5. Cars

Re-derived after the audit; car contents moved materially.

| car | scope | depends on |
|---|---|---|
| A | Deps (`asyncmy`, `sqlalchemy`, `alembic` — all three absent today) + MariaDB in `docker-compose.yml` **and** the nix-repo podman/systemd unit | — |
| B | **Async op-dispatch rewrite** — own car, own tests | — |
| C | MariaDB connection + composition root; two concrete storage classes | A, B |
| D | Alembic + `config` schema-only; `alembic upgrade head` at backend boot | C |
| E | **Gate primitive fix** — nested TTL takes the min, exit restores `previous` | — |
| F | Backup arm (§4.1) + quiesce (§4.2), as a nightly step | D, E |
| G | Restore-verification enumeration (§4.3), on the real restore path | F |
| H | Cross-engine `check_invariants` (§4.4) | D |

Parallelism: A and B and E may start together. C after A+B. D after C. H after
D. F after D+E. G after F.

**Car B exists because** every backend storage op body is sync, dispatched via
`asyncio.to_thread` (`backend/embed_service_routes.py:309-312`); op bodies are
sync throughout `admin_exec/`. `asyncmy` is async-only, so it cannot be awaited
from those bodies without rewriting `run_admin_op` and every op signature. That
is a cross-cutting change to the path EVERY existing admin op runs through, so
it gets its own car and its own regression tests rather than hiding inside C.

**Car E exists because** the belt has holes that bite once backup nests inside
nightly: a no-TTL enter leaves `deadline=None` (no belt at all), and a
TTL-bearing enter nested inside a no-TTL window silently discards its TTL
(`control.py:689-690`). Inner exit also clears unconditionally
(`control.py:711-719`), so it un-gates the outer window unless it honors
`previous` — vacuum already hand-rolls that workaround
(`core/vacuum/__init__.py:1735, 1869`), which is evidence the primitive is
missing something rather than that callers are careless. Fixing it once serves
backup, vacuum and nightly; vacuum's hand-rolled handling must be reconciled,
not left to double up.

### Acceptance rule (standing, every car)
**No car is done until its deliverable has a named caller in the running
system.** PR #32 shipped five modules with tests and no invocation path. Wiring
points are fixed here, not left to build time:

- Car D — `alembic upgrade head` at backend boot, mirroring Surreal's
  `_init_schema` (`_shared/storage/__init__.py:292`).
- Car F — backup driver on the host-side systemd path, like vacuum's
  trigger-file → `yadgar-vacuum.service`.
- Car G — inside the real restore path (§4.3).
- Car H — existing backend admin op.

## 6. Exit criteria

1. The `config` table exists **in MariaDB** and is empty, asserted
   engine-direct (`SHOW TABLES` / `SELECT COUNT(*)`), with alembic at head.
   `config_list() == []` is NOT evidence — it returned `[]` before this train,
   so it cannot distinguish engines.
2. An Alembic migration runs green — closes task 0051, most of 0048.
3. A dump is taken and **restored into a scratch instance**, verified by
   enumeration, not counts.
4. A deliberately PARTIAL restore is **REJECTED** — the 2026-06-16 regression
   test. Without this the arm is unproven.
5. The gate releases on abort, proven by killing the **backup driver**
   mid-window. (Killing core proves nothing: the gate is an in-memory core
   global, `_shared/runtime/state.py:172`, so core dying clears it trivially.)
6. `check_invariants` passes cross-engine and FAILS on an injected desync.
7. `config` row count is 0, asserted. `alembic_version` rows are EXPECTED and
   do not touch task 0095's gate, which is scoped to the first `config_set`.
   The MariaDB healthcheck must stay ping/SELECT so it cannot become a writer.
8. The MariaDB service exists in **both** compose and the nix prod unit.

## 7. Risks

- **Restore-verification cannot be tested in production.** Exit 4 is the only
  evidence it works.
- **Car B touches every admin op.** A regression there breaks things unrelated
  to MariaDB.
- **The gate can wedge writes** — highest-blast-radius new failure mode.
- **MariaDB idle RSS 86.6 MB (ADR-0205) is a FLOOR** — empty DB, default
  config, zero connections. Re-measure under load before the knob train seeds.
- **Nix work is cross-repo** and this repo's tests cannot cover it; task 0122
  records the unit renderers already disagree on activation topology, so car A
  must state WHICH renderers it touches.
- **Container discipline:** anything started must be timeout-wrapped,
  resource-capped and torn down.
