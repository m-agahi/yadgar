# adr_add ID-assignment race — design plan (2026-07-13)

**Status: DRAFT — awaiting audit. No code changes made.**

---

## BLUF

`adr_add` has a read-modify-write race on the ADR log wiki page. Two concurrent
calls can: (1) both read the same page content, (2) both derive the same
`_next_adr_id`, and (3) both write — producing duplicate IDs. The backend
`/admin` route runs `wiki_append_section` in `asyncio.to_thread`, providing no
serialization between concurrent requests. **In production today the race is
theoretical**: `YADGAR_OFFLOAD_TOOLS` defaults to `False`, so sync tool bodies
run inline on the single event loop and serialize via the blocking `httpx.post`.
The race window opens only when `YADGAR_OFFLOAD_TOOLS=1` with two concurrent
agent sessions on the shared core daemon. Fix anyway — the mechanism is broken,
the multi-agent fan-out scenario is an intended use mode, and the fix is small.

---

## Race mechanism — verified file:line

### Step 1: `adr_add` reads the log page (core process)

`yadgar/core/server/tools/adr.py`, lines 217–222:

```python
log_page = wiki_read(slug, directory=resolved, branch_hint=default_branch)
existing_content = log_page.get("content", "") if log_exists else ""
adr_id = _next_adr_id(existing_content)   # max(headers) + 1
```

`wiki_read` calls the backend via HTTP to fetch the current page content.
`_next_adr_id` (line 94–104) scans `^## ADR-(\d{4})` headers and returns
`max + 1`. This is a **pure read with no lock**.

### Step 2: ID is assigned (core process, in memory)

`_next_adr_id` returns a string like `"ADR-0004"`. No persistence yet.

### Step 3: `wiki_append_section` writes (backend process)

`yadgar/core/server/tools/adr.py`, lines 242–254 → forwards to
`yadgar/core/server/tools/_forward.py`, line 67 → HTTP POST to backend
`/admin` → `yadgar/backend/embed_service/embed_service.py`, line 1528:

```python
result = await asyncio.to_thread(run_admin_op, req.op, req.payload)
```

`run_admin_op` → `yadgar/backend/admin_exec/wiki.py`, lines 207–222 →
`_st._wiki.append_section(...)` → `yadgar/_shared/wiki/store.py`, lines
1559–1636.

`append_section` at line 1595 does another `get_wiki_page(page_id)` (a fresh
read), then computes `new_content` by appending the section, then calls
`update_wiki_page` (line 1623). The storage-level `update_wiki_page` wraps its
UPDATE in a `BEGIN/COMMIT` transaction — but this only protects the DB write
itself, **not** the read→compute→write triple in `append_section`.

### The gap

```
Thread A (adr_add call 1)          Thread B (adr_add call 2)
-----------------------------      -----------------------------
wiki_read → content="...ADR-0003"
_next_adr_id → "ADR-0004"
                                   wiki_read → content="...ADR-0003"
                                   _next_adr_id → "ADR-0004"  ← same!
append_section → writes ADR-0004
                                   append_section → writes ADR-0004  ← duplicate!
```

The storage `update_wiki_page` comment (storage/wiki.py line 234) explicitly
acknowledges this: "Pre-txn reads happen outside the transaction. In embedded
single-writer mode this is safe." That claim assumed a single writer thread; the
backend `/admin` route breaks that assumption via `asyncio.to_thread` (each
call gets its own thread, no serialization between them).

---

## Real severity assessment — is it reachable in practice?

**Today: theoretical only — offload is OFF by default.**

`YADGAR_OFFLOAD_TOOLS` defaults to `False` (`offload.py:63`,
`config_registry.py:491`). With offload OFF, `adr_add` (a sync tool body) runs
**inline on the single core event loop**. The `_forward_admin` call in core is a
synchronous `httpx.post`, which blocks the event loop for its duration — so two
`adr_add` calls from any client sharing the same core process serialize
naturally. Concurrency requires offload to be ON (`YADGAR_OFFLOAD_TOOLS=1`)
AND two callers reaching the same core daemon simultaneously.

The drainer (file-queue path) is irrelevant — `wiki_append_section` does NOT go
through the file queue (`wiki.py` line 1059: "This tool writes synchronously (no
queue)").

**Multi-agent scenario: real risk when offload is enabled.**

The core server runs as a **persistent shared daemon** (`streamable-http`
transport, `_startup.py:49,123`). Multiple Claude Code agent sessions connect to
the same single process. If `YADGAR_OFFLOAD_TOOLS=1`, the
`ThreadPoolExecutor` (default 2 workers) can run two `adr_add` calls
concurrently, each blocking the event loop at a different point inside the
critical section. Two agents doing `adr_add` for the same project in the same
window would both reach `wiki_read` before either writes — race is live.

**Verdict: fix now. Cost is low, mechanism is broken, and multi-agent fan-out
with offload enabled is an intended use mode. The fix also protects against
future inadvertent offload-ON deployments.**

---

## Fix options

### Option A — DB-side atomic sequence / counter

Add a `adr_sequence` counter table in SurrealDB and use `UPDATE ... INCREMENT`
or a SurrealQL `DEFINE FUNCTION` to atomically bump and return the next ID.

- **Pro:** race-free at the storage layer, no application-level locking.
- **Con:** requires a new schema migration, a new migration file, and new
  storage-layer code. SurrealKV does not expose `SELECT ... FOR UPDATE`, so
  the atomic-increment must be done via a SurrealQL function or a
  compare-and-swap loop. Higher complexity. Also breaks the "ID is derived from
  the log content" invariant — the counter could diverge from the actual headers
  if a write partially fails.

### Option B — Advisory lock around read-modify-write (CHOSEN)

Add a **process-level `threading.Lock`** in `_shared/wiki/store.py` (or in the
backend `admin_exec/wiki.py` dispatcher) scoped to the `append_section`
operation on a given slug. Lock is acquired before `get_wiki_page`, released
after `update_wiki_page`.

Alternatively: a **`threading.Lock` in `adr.py` (core side)** around the full
`wiki_read → _next_adr_id → wiki_append_section` triple. This is simpler
because the race is in the read-modify-write at the tool level, not just the
storage level.

- **Pro:** minimal code change (one lock, one `with` block). Zero schema change.
  Preserves "ID derived from log content" invariant. Fits the existing
  single-backend-process model.
- **Con:** lock is process-local — does NOT protect against two backend
  processes (multi-replica scenario). Not a concern today (single-process
  backend).

### Option C — Retry-on-collision with re-scan

After `wiki_append_section` completes, re-read the page and verify the written
header appears exactly once. If a duplicate is found, re-derive the next ID and
patch the duplicate heading.

- **Pro:** no lock, no schema change.
- **Con:** complex recovery path, leaves a window where duplicate IDs exist in
  the store (even briefly), harder to test reliably.

### Option D — Unique-constraint + retry

Add a uniqueness constraint on `(slug, section_heading)` at the storage level.
Write fails if the section already exists; caller retries with `max+1`.

- **Con:** `append_section` with `position=new_section_bottom` already rejects
  if the heading exists (line 1604–1606), but the heading includes the full
  title (e.g. "ADR-0004: Use SurrealDB"), not just "ADR-0004". Two concurrent
  calls with the same ID but different titles would not collide on heading. Also
  requires schema change.

---

## Chosen approach: Option B — process-level lock in `adr.py` (core tool)

**Lock in `adr.py`, wrapping the full
`wiki_read → _next_adr_id → wiki_append_section` triple.**

**Why not backend (`admin_exec/wiki.py` or `store.py`)?**

The ID is assigned in `adr.py` (core), not in the backend. `_next_adr_id`
(line 94–104) reads the page content and computes `max + 1` entirely in the
core process, *before* the HTTP forward to the backend. A lock at the backend
`wiki_append_section` boundary would serialize the DB writes but by then both
callers have already derived the same ID from a stale read — the collision is
already baked in. A backend lock cannot fix the race without also moving
`_next_adr_id` to the backend (a larger, more invasive change).

A direct `/admin` client (bypassing core) invokes raw `wiki_append_section`
with its own heading — it does no ADR ID assignment at all. There is no ID race
on that path to protect. The "backend is the correct choke point" argument does
not apply here.

Locking in `store.py` `append_section` has the same problem: it serializes the
DB write, not the read-and-derive that precedes the HTTP hop.

**Why core lock is sufficient:**

The core runs as a **single persistent process** (streamable-http daemon, not
spawned-per-call). A `threading.Lock` in core serializes all in-process
concurrent callers — which is the only concurrent-`adr_add` scenario that
exists today (offload ON + multi-session on one daemon). This is exactly the
threat the severity section identifies.

**Revised flow with lock:**

```
Thread A (adr_add call 1)          Thread B (adr_add call 2)
-----------------------------      -----------------------------
acquire lock for "myproject"
  wiki_read → content="...ADR-0003"
  _next_adr_id → "ADR-0004"
  wiki_append_section → writes ADR-0004
release lock
                                   acquire lock for "myproject"  ← was waiting
                                     wiki_read → content="...ADR-0004"
                                     _next_adr_id → "ADR-0005"  ← correct
                                     wiki_append_section → writes ADR-0005
                                   release lock
```

**Implementation (`adr.py`):**
- Covers the full read-modify-write.
- Keeps lock scope tight (only `adr_add` tool, not all wiki ops).
- Is testable by calling `adr_add` concurrently from two threads.
- Documented as "single-core-process guarantee" in the module docstring.

```python
# yadgar/core/server/tools/adr.py
import threading as _threading

_ADR_LOG_LOCKS: dict[str, _threading.Lock] = {}
_ADR_LOG_LOCKS_GUARD = _threading.Lock()

def _adr_log_lock(resolved: str) -> _threading.Lock:
    with _ADR_LOG_LOCKS_GUARD:
        if resolved not in _ADR_LOG_LOCKS:
            _ADR_LOG_LOCKS[resolved] = _threading.Lock()
        return _ADR_LOG_LOCKS[resolved]
```

In `adr_add`, after resolving the project root:
```python
with _adr_log_lock(resolved):
    log_page = wiki_read(...)
    adr_id = _next_adr_id(existing_content)
    # ... build section ...
    result = wiki_append_section(...)  # or wiki_add(...)
```

---

## Acceptance criteria — unit tests

1. **Concurrent-collision test (repro):** spawn two threads simultaneously calling
   `adr_add` against the same project root with `wiki_read` / `wiki_append_section`
   patched to use real embedded storage (or a real `threading.Lock`-threaded mock
   that injects a sleep inside the critical section to force interleaving). Assert
   that both return distinct ADR IDs (e.g. "ADR-0001" and "ADR-0002"), not the
   same ID.
2. **Sequential correctness** (existing `test_adr_add_create_then_append_sequential_ids`
   in `tests/core/test_adr.py`): must still pass green.
3. **Lock keyed by project root:** two concurrent `adr_add` calls for *different*
   project roots must NOT block each other (parallelism preserved across projects).

---

## Test plan

### Repro test (RED before fix, GREEN after)

```python
# tests/core/test_adr.py — new class TestAdrAddConcurrentIdAssignment

class TestAdrAddConcurrentIdAssignment:
    def test_concurrent_calls_produce_distinct_ids(self, tmp_path):
        """Two simultaneous adr_add calls must not produce duplicate IDs.

        RED before fix: both threads may derive the same _next_adr_id.
        GREEN after: process-level lock serializes the critical section.
        """
        import threading
        from yadgar.core.server.tools.adr import adr_add

        project_dir = str(tmp_path / "myproj")
        os.makedirs(project_dir, exist_ok=True)

        results = []
        barrier = threading.Barrier(2)

        def _call(title: str) -> None:
            barrier.wait()   # both threads enter the critical section together
            r = adr_add(**{**_VALID_ADR_PARAMS, "directory": project_dir, "title": title})
            results.append(r.get("adr_id"))

        threads = [
            threading.Thread(target=_call, args=(f"ADR title {i}",))
            for i in range(2)
        ]
        with (
            patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=project_dir),
            patch("yadgar.core.server.tools.adr._get_default_branch", return_value="master"),
        ):
            for t in threads: t.start()
            for t in threads: t.join()

        assert len(set(results)) == 2, (
            f"Both threads produced the same ADR ID (race): {results}"
        )
        assert set(results) == {"ADR-0001", "ADR-0002"}, (
            f"Expected ADR-0001 and ADR-0002, got: {results}"
        )
```

This test uses the real embedded wiki store (via the `_engines` module fixture)
so `wiki_read → wiki_append_section` use real storage, making the race
observable. The `threading.Barrier(2)` ensures both threads reach the
`wiki_read` call simultaneously.

### Existing tests

All existing tests in `TestAdrAddRoundTrip`, `TestAdrAddIdAssignment`,
`TestAdrAddAppend`, `TestAdrAddAutoCreate` must remain green.

---

## Risks

1. **Lock dict grows unbounded:** one `threading.Lock` per unique `resolved`
   path, never evicted. Mitigated by: the number of distinct yadgar projects on
   a single daemon is small (typically one); add a comment noting the non-eviction
   and accept it.
2. **Deadlock:** impossible (one lock per resource, no nested acquisition order).
3. **Multi-process backend:** if a future deployment runs multiple backend
   processes, the process-level lock does not protect cross-process access. This
   is explicitly out-of-scope; document the assumption.
4. **Test flakiness:** the concurrent test requires `threading.Barrier` and real
   embedded storage. The barrier guarantees simultaneous entry but cannot force
   a specific interleaving order. The RED state (before fix) may not reliably
   reproduce on fast hardware for two reasons: (a) SurrealKV embedded holds an
   exclusive file lock / single writer connection, so concurrent threads may
   serialize or error at the storage layer before the application-level race
   manifests; (b) the critical section (read → compute) completes too fast to
   interleave reliably on a single-core CI runner. Mitigate by patching
   `_next_adr_id` or `wiki_read` with a `time.sleep(0.05)` inside the barrier
   window to force the interleaving rather than relying on timing alone.

---

## Scope IN / OUT

**IN:**
- `yadgar/core/server/tools/adr.py` — add `_adr_log_lock` helper and wrap the
  critical section.
- `yadgar/tests/core/test_adr.py` — add `TestAdrAddConcurrentIdAssignment`.
- Module docstring update in `adr.py` noting the lock and its scope.

**OUT:**
- No change to `wiki_append_section`, `WikiStore.append_section`, or storage layer.
- No schema migration.
- No change to the file-queue drainer (irrelevant — `wiki_append_section` is sync).
- No change to the backend `/admin` route.
- Multi-process / multi-replica protection (explicitly deferred).
- The `wiki_add` CREATE path (`log_exists=False`) also has a first-call race
  (two concurrent calls on a fresh project). The lock covers this path too since
  both paths are inside the `with _adr_log_lock(resolved):` block.

---

## Version impact

- **Core version bump required** (the fix is in `yadgar/core/server/tools/adr.py`).
- Backend: no change.
- No DB migration.
- Suggested version: `core 5.X+1.0` (next available minor on the master branch).
- PR: single commit, `fix(adr): serialize adr_add read-modify-write with per-project lock`.

---

## AUDIT (2026-07-13)

**Status verdict: ACCEPT WITH MINOR FIXES.** The race is real, the mechanism
narrative is accurate to source, and — the crux — the plan picked the *correct*
lock primitive. Every substantive claim VERIFIED first-hand (core-side facts by
the auditor directly; backend-side line numbers by a verification pass). Only
cosmetic line-drift and one repro-test subtlety need attention before build.

### Crux verdict — is `threading.Lock` the right primitive? YES.

This is the plan's central question and the answer is unambiguous. The four
load-bearing facts:

1. `adr_add` is a **sync `def`**, not `async def` (`adr.py:143`).
2. `run_offloaded` runs the body **inline on the loop** when offload is OFF
   (`offload.py:318-319`: `return fn(*args, **kwargs)`) and on a **real
   `ThreadPoolExecutor`** when ON (`offload.py:327`:
   `loop.run_in_executor(pool, _ctx_wrap(call))`).
3. The core→backend forward is a **blocking** `httpx.post` (`_forward.py:67`),
   sync `def _forward_admin` (`_forward.py:31`) — no `await`, no `AsyncClient`.
4. Both modes therefore reduce to threads (1 loop-thread OFF; ≥2 pool-threads
   ON). A `threading.Lock` serializes both: OFF it is a no-contention
   pass-through (harmless — the blocking post already serializes the single
   loop); ON it is the only thing that serializes the pool threads.

`asyncio.Lock` is disqualified twice over: (a) you cannot `await` it inside a
sync body, and (b) under offload the body runs on a pool thread with no running
loop to bind the lock to. The plan rejected the async lock implicitly by
choosing `threading.Lock`; that choice is CORRECT. **Do not change it.**

### Fix-scope completeness — is `adr_add`-only sufficient? YES.

The only other reader of the ADR-log helper is `project.py::_build_adr_log`
(`project.py:1836-1863`). Verified **read-only**: it is a restore-mode metadata
builder that calls `wiki_read` + `parse_adr_ids` and returns
`{"slug", "latest_ids"}` — it assigns no ID and writes nothing. A direct
`/admin` `wiki_append_section` client also does no ID derivation. So there is no
competing writer path; a lock scoped to `adr_add` covers the entire ID-race
surface. The plan's "core is the right layer" argument holds: `_next_adr_id`
runs in core (`adr.py:222`) *before* the HTTP hop, so a backend-side lock would
serialize the DB write after both callers already baked in the same stale ID —
verified ordering, plan is right to reject the backend lock.

### Per-claim verification table

| # | Claim | Location | Status |
|---|-------|----------|--------|
| 1 | `adr_add` reads via `wiki_read` then `_next_adr_id(existing_content)` | `adr.py:217` (read) + `:222` (id) | VERIFIED — but plan writes "lines 217–222" as one 3-line snippet; those lines are non-contiguous in source (read at 217, id at 222). Cosmetic STALE. |
| 2 | Appends via `wiki_append_section` (create path uses `wiki_add`) | `adr.py:242`; create `:271` | VERIFIED |
| 3 | `_next_adr_id` scans `^## ADR-(\d{4})`, returns max+1 | `adr.py:93-104`; regex `:45` | VERIFIED — plan cites "94–104"; `def` is 94, `@observe` decorator 93. Cosmetic. |
| 4 | No existing lock in `adr.py` | grep — zero hits | VERIFIED |
| 5 | Backend forward is **sync blocking** `httpx.post` to `/admin` | `_forward.py:67`, `def` `:31` | VERIFIED (crux) |
| 6 | `adr_add`/`wiki_read`/`wiki_append_section` all sync `def` | `adr.py:143`, `wiki.py:510`, `:1022` | VERIFIED (crux) |
| 7 | `/admin` route: `await asyncio.to_thread(run_admin_op,…)` | `embed_service.py:1528` | VERIFIED |
| 8 | Admin dispatch → `_st._wiki.append_section(...)` | `admin_exec/wiki.py:206-222` | VERIFIED — plan cites "207–222"; `@observe` on 206, `def` on 207. Cosmetic. |
| 9 | `append_section` re-reads (`get_wiki_page` ~1595), rejects existing heading (~1604), then `update_wiki_page` (~1623) | `store.py:1595 / 1603-1606 / 1623` | VERIFIED |
| 10 | Storage comment: "Pre-txn reads … single-writer mode this is safe"; `update_wiki_page` in BEGIN/COMMIT | `storage/wiki.py:233-236`, txn `:292-301` | VERIFIED — and the comment itself already states "In server mode a race window exists between read and txn open" (`:235`). Strengthens the plan. |
| 11 | `YADGAR_OFFLOAD_TOOLS` default **False** | `offload.py:63`, `config_registry.py:491` (`"false"`), `config.py:753` | VERIFIED |
| 12 | Offload OFF ⇒ inline; ON ⇒ `run_in_executor` ThreadPool | `offload.py:318-319 / 327` | VERIFIED (crux) |
| 13 | Core is a persistent shared daemon, `streamable-http`, stateless | `_startup.py:49,123,129` | VERIFIED — `stateless_http=True`; each POST independent, so a `threading.Lock` (process-global module state) correctly serializes across sessions. |
| 14 | `wiki_append_section` writes synchronously (no file queue) | `wiki.py:1059-1060` | VERIFIED |
| 15 | `_build_adr_log` shares helper but is a competing writer? | `project.py:1836-1863` | VERIFIED READ-ONLY — plan did not raise this; auditor confirms lock scope is complete. |
| 16 | Test `test_adr_add_create_then_append_sequential_ids` + `_VALID_ADR_PARAMS` exist; classes `TestAdrAddRoundTrip` etc. | `tests/core/test_adr.py:366,67,357…` | VERIFIED |
| 17 | Patch targets `adr._resolve_project_root` / `._get_default_branch` exist | `adr.py:33` import | VERIFIED |
| 18 | Test path | plan §Test-plan says `tests/core/test_adr.py`; §Scope says `yadgar/tests/core/test_adr.py` | STALE (internal inconsistency) — actual path is `yadgar/tests/core/test_adr.py`. |

**STALE/WRONG count: 0 WRONG, 4 STALE — all cosmetic** (three line-number
groupings off by ≤1 for a decorator; one internal test-path inconsistency). No
STALE affects the fix design or the verdict.

### Repro-test correctness — one real subtlety (not fatal)

The plan's repro spawns two threads calling `adr_add` on a **fresh** project.
Both hit `log_exists=False` → both take the **create** branch, which calls
`wiki_add` with a *full-page overwrite* (`adr.py:271`), NOT append. So:

- **Without the lock:** both derive `ADR-0001`, both create-overwrite → the
  second `wiki_add` clobbers the first's page → final log has a single
  `ADR-0001` (or a duplicate, depending on store dedup). The assertion
  `set(results) == {"ADR-0001","ADR-0002"}` FAILS → RED. Good — it reproduces.
- **With the lock:** thread A creates page (ADR-0001); thread B now reads
  `log_exists=True` → appends ADR-0002. GREEN.

So the test *does* discriminate. BUT the assertion phrasing conflates two
different failure modes (duplicate ID vs. clobbered page). Recommend the repro
assert **both**: (a) `len(set(results)) == 2` AND (b) the final rendered log
contains both `## ADR-0001` and `## ADR-0002` headers — the clobber only shows
in (b), the ID-dup only in (a). The plan's Risk-4 hand-wave about RED
reliability is legitimate: on the create-clobber path the race is a lost-update,
not a timing-sensitive interleave, so the `time.sleep` injection the plan
suggests is the right mitigation to force the window. Keep it.

Note the fixture question: the real-store test patches only the two helpers, yet
`_forward.py` raises without `YADGAR_EMBED_URL`. The existing
`TestAdrAddRoundTrip` already runs against "the real embedded wiki store" the
same way, so the conftest must either stand up a backend or patch the forward —
the new test should reuse that exact fixture path (it does, by mirroring
`TestAdrAddRoundTrip`). Since the repro calls `adr_add` on raw threads (never
through `run_offloaded`), the lock is exercised correctly regardless.

### User decisions required

1. **Confirm lock primitive = `threading.Lock`** (audit says correct — this is
   just a sign-off, not a re-open). Async lock is wrong; do not entertain it.
2. **Repro assertion:** adopt the two-part assertion (distinct IDs AND both
   headers present) + retain the `time.sleep` interleave injection. Approve?
3. **Lock-dict eviction (Risk 1):** accept unbounded `_ADR_LOG_LOCKS` growth
   (one entry per project path, tiny) as documented, or add a cap? Auditor:
   accept — the eviction machinery would cost more than it saves.
4. **Version string:** plan says `core 5.X+1.0` — pick the concrete next minor
   off master before the PR (current master core is 5.116.0 per git log;
   suggest `core 5.117.0`).
5. **Ship-gate:** the race is "theoretical today" (offload OFF by default,
   `config_registry.py:491`). Confirm fixing now anyway — auditor agrees: cost
   is one lock, multi-agent fan-out with offload ON is an intended mode, and the
   fix hardens against inadvertent offload-ON deploys.

**Bottom line:** sound plan, correct lock, complete scope. Apply the four
cosmetic line-number corrections, tighten the repro assertion, then build.
