# Port Tests to HTTP Transport

## Problem

Tests run in embedded mode (surrealkv, no `YADGAR_DB_URL`). Production runs HTTP mode
(httpx → SurrealDB `/sql`). Different code paths — bugs in the HTTP path go untested.

Additionally, 23 `storage._db.query()` calls in test files will raise
`RuntimeError: _db accessed in server mode` if tests ever run with `YADGAR_DB_URL` set.

## Goal

Tests run via the same HTTP transport as production by default.
Embedded mode remains available as a CI fallback when the `surreal` binary is absent.

---

## Changes

### 1. Replace 23 `_db.query()` calls — mechanical

All are verification/assertion queries in 7 test files. None mutate data in a way
that bypasses the public API.

Find-replace: `storage._db.query(` → `storage._q(`

Files affected:
- `tests/test_curation.py` (8 calls)
- `tests/test_knowledge_graph.py` (7 calls)
- `tests/test_metacognition.py` (2 calls)
- `tests/test_frontier_schema.py` (2 calls)
- `tests/test_cls_store.py` (1 call)
- `tests/test_restoration.py` (1 call)
- `tests/test_sleep_compute.py` (2 calls)

---

### 2. Rewrite `conftest.py` isolation fixture

Current fixture patches `_make_connection()` — dead code after the WS→HTTP migration.

New approach: post-init httpx header patch. After `StorageEngine.__init__()` completes
in server mode, override the `surreal-db` header to a per-test namespace derived from
`tmp_path`. Each test gets `yadgar/t{md5(db_path)[:12]}` instead of `yadgar/main`.
No data leaks between parallel tests.

```python
if os.environ.get("YADGAR_DB_URL"):

    @pytest.fixture(autouse=True)
    def _isolate_surrealdb(monkeypatch):
        from yadgar import storage as _sm

        original_init = _sm.StorageEngine.__init__

        def _patched_init(self, db_path, **kwargs):
            original_init(self, db_path, **kwargs)
            if self._db_url and hasattr(self, "_http"):
                path_hash = hashlib.md5(str(db_path).encode()).hexdigest()[:12]
                self._http.headers["surreal-db"] = f"t{path_hash}"

        monkeypatch.setattr(_sm.StorageEngine, "__init__", _patched_init)
```

---

### 3. Add `surreal_server` session-scoped fixture

One SurrealDB process shared across the entire test session. Isolation is handled by
per-test namespaces (step 2), not separate servers.

Add to `conftest.py`:

```python
import socket
import subprocess
import time

def _find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

def _wait_for_health(port: int, timeout: float = 10.0) -> None:
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
            return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError(f"SurrealDB did not start on port {port}")

@pytest.fixture(scope="session", autouse=True)
def surreal_server(tmp_path_factory):
    """Start a real SurrealDB HTTP server for the test session.

    Skip (fall back to embedded mode) if the `surreal` binary is not on PATH.
    """
    import shutil
    if not shutil.which("surreal"):
        yield  # embedded mode — no server started
        return

    db = tmp_path_factory.mktemp("surreal_data")
    port = _find_free_port()
    proc = subprocess.Popen(
        [
            "surreal", "start", "--no-banner",
            "--bind", f"127.0.0.1:{port}",
            "--user", "root", "--pass", "root",
            f"surrealkv://{db}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.environ["YADGAR_DB_URL"] = f"http://127.0.0.1:{port}"
    try:
        _wait_for_health(port)
        yield
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        os.environ.pop("YADGAR_DB_URL", None)
```

---

### 4. Add `surreal` binary to CI

`.github/workflows/*.yml` — add a download step before running tests:

```yaml
- name: Install SurrealDB
  run: |
    curl -sSf https://install.surrealdb.com | sh
    echo "$HOME/.surrealdb" >> $GITHUB_PATH
```

Or copy from the Docker image layer if CI already uses Docker:

```yaml
- name: Install SurrealDB binary
  run: |
    docker create --name surreal-tmp surrealdb/surrealdb:v2.6.5
    docker cp surreal-tmp:/surreal /usr/local/bin/surreal
    docker rm surreal-tmp
```

---

## Execution Order

```
1. Replace 23 _db.query() → _q()          mechanical, ~10 min
2. Rewrite conftest isolation fixture      ~20 lines
3. Add surreal_server fixture              ~40 lines
4. Add surreal binary to CI               1 step in workflow
5. Run full suite: pytest -x               validate, fix failures
```

## What Does NOT Change

- Test logic — public API calls are identical in both transports
- Embedded mode still works when `surreal` binary is absent
- No new Python deps required
