# PLAN — v5.10.0: Test Harness Hardening (deferred from v5.7.14)

**Status:** drafted 2026-05-28. Originally scoped as v5.7.14 hotfix; renamed v5.10 after v5.8.0 shipped first.

**Master at draft time:** core v5.8.0 + backend v5.3.1.

**Sequence:** ship AFTER v5.9.0 anchor audit. Test-harness work doesn't depend on anchor mechanics, but ordering puts the user-impacting features first.

---

## Why

Two production-relevant pain points surfaced 2026-05-27 → 2026-05-28:

### Pain 1 — Orphan SurrealDB processes

yadgar tests spawn one `surreal start --bind 127.0.0.1:<random>` subprocess per xdist worker. When pytest is killed mid-run (user `^C`, agent harness TaskStop, harness timeout, advisor kill), the SurrealDB child processes are NOT reaped — they survive their parent.

Each orphan idles at 0.1–45% CPU continuously depending on what state they were left in. Multiple parallel agent-driven dev sessions stack orphans across days.

**Observed mid-session 2026-05-28:** up to 25 SurrealDB orphans alive simultaneously. Combined CPU load ~300–400% across cores. CPU fan spin-up "every minute" pattern is pytest's own scheduler-tick poll on the hung workers, not a yadgar daemon.

Wiki ref: [[cpu-fan-spin-up-root-cause-orphan-pytest-surrealdb-workers]] (debugging category, 2026-05-28).

### Pain 2 — Multi-agent pytest contention false-regressions

When multiple claude sessions run pytest concurrently (typical dev workflow with 2-5 parallel agents):

- Shared `/tmp/pytest-of-max/` namespace → collisions on tmp-dir names.
- SurrealDB random-port pool → port-allocation races.
- DNS lookups to `host.containers.internal` from OTLP exporter race against each other.

Result: full-sweep `pytest -n auto` shows 14–47 "failures" that all PASS in isolation. False-regression chase costs ~30 min of agent dispatch time per occurrence.

Anchored lesson in memory id 519XXX (FEEDBACK 2026-05-28): "Multi-agent pytest stacking creates false-regression flakes."

---

## What ships

### 1. `pytest-timeout` plugin

Add to `pyproject.toml`:

```toml
[tool.pytest.ini_options]
timeout = 300
timeout_method = "thread"
timeout_func_only = false  # cover both setup and call phases
```

Default 300s per test. Hung tests get SIGTERM cleanly. Test fixtures' finalizers run. Children reaped.

Add `--timeout=0` override path for legit slow tests (the v5.7.13 perf test gating `test_merge_duplicates_under_5s_at_500_memories_with_embeddings` should set `@pytest.mark.timeout(60)` rather than relying on global default).

### 2. SurrealDB fixture teardown hardening

Locations: `yadgar/tests/conftest.py` + `yadgar/tests/test_memory_behavior.py::_engines` + any test that calls `_make_storage_engine()` / equivalent.

Pattern:

```python
import atexit
import subprocess

_SPAWNED_SURREAL_PIDS: list[int] = []

def _spawn_surreal(port: int, data_dir: str) -> subprocess.Popen:
    proc = subprocess.Popen([...])
    _SPAWNED_SURREAL_PIDS.append(proc.pid)
    return proc

def _kill_all_spawned_surreal():
    for pid in _SPAWNED_SURREAL_PIDS:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    time.sleep(0.5)
    for pid in _SPAWNED_SURREAL_PIDS:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

atexit.register(_kill_all_spawned_surreal)
```

Belt-and-suspenders: per-fixture finalizer also explicitly terminates the subprocess on test teardown OR test mid-setup raise.

### 3. xdist port-range determinism

Today: each fixture binds to `127.0.0.1:<random>` from kernel's ephemeral pool. Two concurrent claude sessions can collide.

Fix: partition the port space by xdist worker ID + `PYTEST_XDIST_TESTRUNUID`. Use deterministic range like `12000 + (worker_id * 100)` for worker, sequential within.

If a port is in use (rare collision with another claude session even with this scheme): retry up to 10 times with linear backoff. Log warning.

### 4. xdist worker cleanup conftest hook

```python
# yadgar/tests/conftest.py
def pytest_sessionfinish(session, exitstatus):
    """Final cleanup of any leftover yadgar test resources."""
    _kill_all_spawned_surreal()
```

Fires even on `exitstatus != 0`. Last-chance cleanup before pytest exits.

### 5. Operational watchdog systemd-user timer

NOT shipped in yadgar core. Ships as documentation in MIGRATION_NOTES + an optional unit file:

```ini
# ~/.config/systemd/user/yadgar-test-orphan-cleanup.service
[Unit]
Description=Kill orphan pytest SurrealDB workers
After=default.target

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'ps -eo pid,args | grep "surreal start" | grep pytest-of-max | awk "{print \\$1}" | xargs -r kill -9'
```

```ini
# ~/.config/systemd/user/yadgar-test-orphan-cleanup.timer
[Unit]
Description=Periodic orphan pytest SurrealDB cleanup

[Timer]
OnBootSec=5min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
```

User-managed install. Document in MIGRATION_NOTES with `systemctl --user enable --now yadgar-test-orphan-cleanup.timer`.

### 6. Multi-agent session isolation

Document in CLAUDE.md `agent_dispatch_prelude`:

> If another claude session is running pytest concurrently, EITHER wait for it OR pass `TMPDIR=/tmp/pytest-session-$AGENT_ID/` to your pytest invocation. The default `/tmp/pytest-of-max/` namespace collides across sessions.

Optionally: yadgar test conftest detects `YADGAR_TEST_NAMESPACE` env var and uses it for tmp dir + port-range offset. Agent harness sets it automatically per session.

---

## What does NOT ship in v5.10.0

| Item | Why deferred |
|---|---|
| pytest-xdist replacement (e.g. pytest-parallel) | Speculative. Current xdist works once these fixes land. |
| Container-level test isolation (per-worker container) | Heavy weight for the savings; conftest fixes cheaper. |
| Performance regression suite | Out of scope; if needed, separate plan. |
| Random port retry-on-EADDRINUSE for production yadgar (not tests) | Production yadgar binds to known port 8765/8000; not affected. |

---

## Implementation order

1. **TDD scaffolding** — `yadgar/tests/test_harness_hardening.py`:
   - Spawn 4 SurrealDB subprocesses; assert atexit handler kills all.
   - Hard-kill the pytest process (mimic mid-run termination); assert no orphans remain (use a subprocess test).
   - Port-allocation collision: have two fixtures attempt same port; assert retry works.
   - `pytest_sessionfinish` hook fires on exitstatus != 0.

2. **`pytest-timeout` in pyproject.toml** — single dep addition. Test that an intentionally-hung test SIGTERM's at 300s.

3. **SurrealDB spawn helper** — extract any subprocess spawn into central `yadgar/tests/_surreal_helpers.py`. All callers route through it. atexit registration is one place to maintain.

4. **xdist port-range** — env-knob `YADGAR_TEST_PORT_BASE` (default 12000), step 100 per worker. Document collision retry behavior.

5. **conftest `pytest_sessionfinish`** — final cleanup hook.

6. **Documentation** — `MIGRATION_NOTES.md` v5.10 section + the optional systemd-user timer unit file + an example `YADGAR_TEST_NAMESPACE` invocation.

7. **Version bump** — v5.9.x → v5.10.0 (minor: test infra rework). No backend change.

---

## Acceptance criteria

- `pytest yadgar/tests/test_harness_hardening.py` green.
- After killing pytest mid-run (`pkill -9 pytest`): `ps -eo args | grep 'surreal.*pytest-of-max'` returns nothing within 30 seconds.
- 5 concurrent `pytest -n 2` runs do NOT cause cross-run port collisions when each uses distinct `YADGAR_TEST_PORT_BASE`.
- I13 + I23 + I24 + I25 lints green.
- `python scripts/check_versions.py` exit 0.

---

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| `atexit` doesn't fire on SIGKILL | Belt-and-suspenders: also use per-fixture finalizer + `pytest_sessionfinish` hook + watchdog systemd timer. Defence in depth. |
| `pytest-timeout` interferes with legit long tests | Document per-test `@pytest.mark.timeout(N)` override + the v5.7.13 perf test as worked example. |
| Port-range bump breaks existing tests | All tests go through the central helper; tests use returned port, not hardcoded constants. Verify via grep. |
| Watchdog timer kills production yadgar's SurrealDB | Filter is `pytest-of-max` substring — production data dir is `~/.yadgar/surreal_db` (no `pytest-of-max`). Test the regex on production patterns before shipping. |
| Multi-session `YADGAR_TEST_NAMESPACE` adds friction to manual pytest invocation | Default value works fine for single-session use. Only matters when 2+ claude sessions concurrent. |

---

## Estimate

~400 LOC implementation + ~300 LOC tests. Single agent dispatch (small risk profile — test infra only, no production code path).

---

## Sequencing vs other trains

| Plan | Status | Order |
|---|---|---|
| v5.8.0 anchor hygiene foundation | SHIPPED 2026-05-28 | done |
| v5.9.0 anchor audit + consolidation | drafted | Next. |
| **v5.10.0 test harness hardening (this)** | drafted | After v5.9. Yields lower user-facing value than v5.9 but pays back dev velocity going forward. |
| v5.11.0 anchor cross-project + Jira | drafted | After v5.10. Was originally numbered v5.10. |
| Backend v5.4.0 recall caching | drafted | Independent track. Can run parallel to any of the above. |

---

## Open / parked questions

- **`pytest-timeout` thread method vs signal method** — `thread` doesn't kill via signal, just unwinds the thread. Pytests with C-extension blocking calls may not unwind. Test with real yadgar code before locking in. Fallback: `signal` method on POSIX (Linux/macOS).
- **Watchdog timer cadence** — 5 min is conservative. Tighter (1 min) catches orphans faster but adds noise. Wider (15 min) ok if dev workflow tolerates 15-min CPU drift before reaping.
- **Should the watchdog also kill orphan `pytest` parents** without children? — pytest with no SurrealDB child = harmless. Don't bother.
- **`YADGAR_TEST_NAMESPACE` propagation** — agent harness needs to set it. Coordinate with claude-code config.

---

## Cumulative state after v5.10.0

Multi-session pytest dev workflow stops accumulating orphans. False-regression chase rate drops to near zero. Recurring CPU fan spin-up issue closes out (~4 weeks of investigation, multiple anchored false-attribution memories — finally root-caused 2026-05-28 + fixed in this train).
