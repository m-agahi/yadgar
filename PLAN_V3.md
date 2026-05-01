# Yadgar v3.x — Migrate SurrealDB Client to HTTP

## Problem

The Python `surrealdb` package is stuck at v2.0.0 and couples the server version
to the client release cadence. SurrealDB v3.x is incompatible with the Python SDK,
meaning the server is permanently frozen at v2.3.5 until the Python package catches up
(no sign of that happening).

## Solution

Drop the `surrealdb` Python package entirely for server mode. Talk to the SurrealDB
server that's **already running at `http://127.0.0.1:8000`** using its HTTP API and
`httpx`. The WebSocket SDK is replaced by plain HTTP POST requests to `/sql`.

**SurrealDB SurrealQL is unchanged.** Every query stays identical — only the transport
layer changes.

---

## Why this works today

Phase 6.6 already moved to SurrealDB server mode inside Docker. `entrypoint.sh` starts
`surreal` at `http://127.0.0.1:8000` and the health check already uses `urllib.request`
(HTTP). The server is there. We're just switching the Python client from WebSocket to HTTP.

```
Before: Python → surrealdb SDK (WS) → ws://127.0.0.1:8000  ← version-coupled
After:  Python → httpx             → http://127.0.0.1:8000  ← version-free
```

---

## What changes

| File | Change |
|---|---|
| `yadgar/storage.py` | Replace `Surreal` WS client with `httpx.Client`; rewrite `_q()` and `_init_schema()` |
| `pyproject.toml` | Add `httpx`; demote `surrealdb` to `[dev]` optional dep (kept for embedded/test mode) |
| `Dockerfile` | Bump SurrealDB binary version (now free to track latest) |
| `YADGAR_DB_URL` env | Change default from `ws://` to `http://` |

**Not changing:** all SurrealQL queries, schema, table structure, `entrypoint.sh`,
daemon, hooks, tests (embedded mode stays for CI).

---

## HTTP API reference

```
POST http://127.0.0.1:8000/sql
Authorization: Basic cm9vdDpyb290        # base64("root:root")
NS: yadgar
DB: main
Content-Type: text/plain
Accept: application/json

<SurrealQL body>
```

Response shape:
```json
[
  {"status": "OK", "time": "142µs", "result": [ ... rows ... ]},
  {"status": "OK", "time": "12µs",  "result": null }
]
```

One entry per statement. LET statements return `null`. The data is always the last entry.

---

## Phase 1 — Core transport swap (storage.py)  ← main work

### 1.1  Add `httpx` dep, demote `surrealdb`

`pyproject.toml`:
```toml
dependencies = [
    # ...
    "httpx>=0.27",          # ADD: replaces surrealdb WS client in server mode
    # "surrealdb>=1.0.0",   # REMOVE from main deps
]

[project.optional-dependencies]
dev = [
    "surrealdb>=1.0.0",     # ADD here: still needed for embedded mode in tests
    # ... existing dev deps
]
```

### 1.2  Replace connection setup in `__init__`

**Remove:**
- `threading.local()` — not needed, `httpx.Client` is thread-safe and shared
- `_make_connection()` — WebSocket connection factory, gone
- The `test = self._make_connection(); test.close()` probe
- `from surrealdb import Surreal` import in server branch

**Add:**
```python
import base64, httpx

if self._db_url:
    _auth = base64.b64encode(b"root:root").decode()
    self._http = httpx.Client(
        base_url=self._db_url,
        headers={
            "Authorization": f"Basic {_auth}",
            "NS": "yadgar",
            "DB": "main",
            "Accept": "application/json",
        },
        timeout=30.0,
    )
    self._init_schema()
    atexit.register(self.close)
    return
```

Credentials should come from env vars if configured:
```python
_user = os.environ.get("YADGAR_DB_USER", "root")
_pass = os.environ.get("YADGAR_DB_PASS", "root")
_auth = base64.b64encode(f"{_user}:{_pass}".encode()).decode()
```

### 1.3  Rewrite `_db` property

**Remove** the `_db` property entirely in server mode (no longer needed — callers that
used `self._db` in server mode now use `self._http` via `self._q()`).

The `_db` property stays for embedded mode unchanged:
```python
@property
def _db(self):
    """Embedded mode only — raises if called in server mode."""
    if self._db_url:
        raise RuntimeError("_db accessed in server mode — use _q() instead")
    return self._embedded_db
```

### 1.4  Rewrite `_q()`

Replace the entire method body:

```python
def _q(self, surql: str, params: dict | None = None) -> list:
    """Run a parameterised query via HTTP and return rows as a flat list of dicts."""
    import json as _json

    # Prepend LET statements for all params — handles scalars, arrays, objects.
    if params:
        lets = [f"LET ${k} = {_json.dumps(v)};" for k, v in params.items()]
        body = "\n".join(lets) + "\n" + surql
    else:
        body = surql

    if self._db_url:
        resp = self._http.post("/sql", content=body.encode(),
                               headers={"Content-Type": "text/plain"})
        resp.raise_for_status()
        results = resp.json()
        # Last entry is always the actual query result (LET entries precede it)
        raw = results[-1].get("result") if results else None
    else:
        # Embedded mode — existing WebSocket path unchanged
        for attempt in range(2):
            try:
                raw = self._embedded_db.query(surql, params or {})
                break
            except Exception as exc:
                if attempt == 0:
                    _log.debug("Embedded DB error (%s), retrying…", exc)
                    continue
                raise

    # Normalise to flat list of dicts (same shapes as before)
    if raw is None:
        return []
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        if not raw:
            return []
        first = raw[0]
        if isinstance(first, list):
            return first
        return raw
    return []
```

### 1.5  Fix `_init_schema()`

`_init_schema` does `db = self._db` then calls `db.query(sql)` many times.
In server mode `self._db` no longer exists. Fix: replace the first line and
all `db.query(sql)` calls:

```python
def _init_schema(self):
    # Replace every:  db.query("...")
    # With:           self._q("...")
    # The _q() method handles both server and embedded mode transparently.
```

This is a mechanical find-replace inside `_init_schema` only:
- Remove `db = self._db`
- `db.query(sql)` → `self._q(sql)`

### 1.6  Fix `close()`

Server mode close should close the httpx client:
```python
def close(self):
    if self._db_url:
        if hasattr(self, "_http"):
            self._http.close()
        return
    # ... existing embedded close ...
```

---

## Phase 2 — Environment variable update

Change the default `YADGAR_DB_URL` in `Dockerfile`:
```dockerfile
ENV YADGAR_DB_URL=http://127.0.0.1:8000   # was: ws://127.0.0.1:8000
```

The `entrypoint.sh` starts SurrealDB with `--bind 127.0.0.1:8000` — no change needed
there. HTTP and WebSocket are both served on port 8000 by SurrealDB.

---

## Phase 3 — Upgrade SurrealDB server version

With the Python SDK no longer involved, the SurrealDB binary is free to upgrade.

### 3.1  New install (no existing data)

Just bump the Dockerfile:
```dockerfile
# was: COPY --from=surrealdb/surrealdb:v2.3.5 /surreal /usr/local/bin/surreal
COPY --from=surrealdb/surrealdb:v2.x.y /surreal /usr/local/bin/surreal
```

Start with the latest **v2.x** patch first (e.g. v2.3.5 → v2.latest) before jumping
to v3.x. Validate schema + queries work. Then consider v3.x.

### 3.2  Existing data migration (v2 → v3 server)

SurrealDB v3.x uses a different on-disk format. Existing data must be exported and
reimported. Steps for users upgrading:

```bash
# 1. Export from running v2 container
docker exec yadgar surreal export \
  --conn http://127.0.0.1:8000 \
  --user root --pass root \
  --ns yadgar --db main \
  /data/backup.surql

docker cp yadgar:/data/backup.surql ./backup.surql

# 2. Stop old container, start new v3 container with empty volume
yadgar daemon stop
docker volume create yadgar-data-v3

# 3. Import into v3
docker run --rm \
  -v yadgar-data-v3:/data \
  surrealdb/surrealdb:v3.x.y \
  surreal import \
  --conn http://127.0.0.1:8000 \
  --user root --pass root \
  --ns yadgar --db main \
  /data/backup.surql

# 4. Swap volumes in daemon config and start
```

Add `yadgar migrate` CLI command to automate steps 1–4. Until then, document manually.

### 3.3  Version decision matrix

| SurrealDB | Python SDK needed | Data migration | Recommendation |
|---|---|---|---|
| v2.3.5 (current) | After Phase 1: no | None | Safe, no change |
| v2.latest | No | None | Do this first |
| v3.x | No | Export/import | After v2.latest validated |

---

## Phase 4 — Remove surrealdb from Dockerfile (optional)

Once embedded mode is no longer needed in production, remove the package from the
image entirely:

```dockerfile
RUN pip install --no-cache-dir torch ... && \
    pip install --no-cache-dir /app    # surrealdb not in main deps anymore
```

Keep it in `[dev]` extras so `yadgar test` (inside the dev container) still works
with embedded mode.

**Deferred until tests are confirmed green in server mode.**

---

## Testing approach

### CI (no Docker)
Tests run in embedded mode (`YADGAR_DB_URL` not set). Embedded mode uses the
`surrealdb` package via `[dev]` extras. **No change to CI.** Tests continue to pass.

### Integration test (server mode)
Add one test that starts a real SurrealDB HTTP server and exercises `_q()`:

```python
# tests/test_http_transport.py
import pytest, subprocess, time, os

@pytest.fixture(scope="session")
def surreal_server(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("surreal_db")
    proc = subprocess.Popen([
        "surreal", "start", "--no-banner",
        "--bind", "127.0.0.1:19999",
        "--user", "root", "--pass", "root",
        f"surrealkv://{db_path}",
    ])
    time.sleep(1.0)
    yield "http://127.0.0.1:19999"
    proc.terminate()

def test_http_roundtrip(surreal_server, tmp_path):
    os.environ["YADGAR_DB_URL"] = surreal_server
    from yadgar.storage import StorageEngine
    s = StorageEngine(str(tmp_path / "db"))
    mid = s.insert_memory({"content": "http transport test", "heat": 0.9, ...}, ...)
    mem = s.get_memory(mid)
    assert mem["content"] == "http transport test"
    s.close()
```

Run with: `pytest tests/test_http_transport.py` (requires `surreal` binary on PATH).

---

## Execution order

```
Phase 1 — storage.py transport swap        ← core, ~1 session
    1.1  pyproject.toml: add httpx, demote surrealdb
    1.2  __init__: replace _make_connection / threading.local with httpx.Client
    1.3  _db property: guard + embedded-only
    1.4  _q(): HTTP path + keep embedded path
    1.5  _init_schema(): db.query() → self._q()
    1.6  close(): httpx client cleanup
    ↓
Phase 2 — env var (YADGAR_DB_URL ws:// → http://)    ← 1 line, 5 min
    ↓
Phase 3.1 — bump SurrealDB to v2.latest in Dockerfile ← 1 line, validate
    ↓
    [run full test suite — must be green before continuing]
    ↓
Phase 3.2 — v3.x upgrade (if desired, with data migration)
    ↓
Phase 4 — remove surrealdb from prod image (deferred)
```

---

## What this does NOT fix

- Embedded mode (`surrealkv://`) stays version-frozen — but embedded mode is only
  used in CI tests, never in production. This is acceptable.
- The SurrealDB server itself can still have bugs. This migration removes the
  **client version coupling** problem, not all SurrealDB bugs. If SurrealDB v3.x
  has regressions, stay on v2.latest.
- This is not a database migration. If SurrealDB keeps being painful after
  unlocking v3.x, the Weaviate/PostgreSQL migration is the next escalation path.
