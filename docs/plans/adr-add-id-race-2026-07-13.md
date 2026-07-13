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
