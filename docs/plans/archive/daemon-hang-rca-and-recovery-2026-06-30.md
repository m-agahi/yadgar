> ARCHIVED 2026-07-09 — SHIPPED: P0 healthcheck-kill shipped (e783510, nix); P1 hook-route inline git wrapped in asyncio.to_thread shipped v5.90.0 (#134, 2febcedb). Both fixes live.

# Core MCP Daemon Hang — RCA + Recovery Plan (2026-06-30)

**Status:** IN REVIEW. Read-only RCA + fix design.

> **CORRECTION (2026-06-30, post-research) — P0 approach changed.** The proposed
> P0 Layer-B **sd_notify `WATCHDOG=1` watchdog is NON-VIABLE on podman 5.8.2**:
> podman's notify proxy (`pkg/systemd/notifyproxy`) forwards only `READY=1`/`BARRIER=1`
> to host systemd and **silently drops `WATCHDOG=1`** in *every* `--sdnotify` mode
> (verified against the v5.8.2 source). A watchdog ping would go into a void. The
> watchdog code that was prototyped (`server/watchdog.py`, branch
> `fix/v5.88.3-daemon-watchdog`) was therefore **abandoned, not merged**.
> **Shipped P0 (nix only, commit `e783510`):** use the healthcheck path, which DOES
> reach systemd — `--health-on-failure=kill` + `--health-retries 3` on the core
> `docker run` (a hung loop fails the existing `curl /health` probe) + `Restart=always`.
> Hung loop → /health fails 3×15s → podman kills the container → systemd restarts it.
> No daemon code, no version bump. Layer A (`Restart=always`) folded in. **P1**
> (git off the event loop) is unchanged and still the real bug fix.

Investigation date: 2026-06-30. Advisor-vetted (one BLOCKING constraint — FastMCP
sync-dispatch — verified before this doc was written; see RCA Part 1 §"Linchpin").

> **Scope note.** The yadgar MCP server was disconnected during this
> investigation (only worktree tools resolved). Prior-incident memory could not
> be consulted; the RCA is grounded entirely in the live git source. After the
> MCP reconnects, cross-check against any existing hang/perf wiki pages and fold
> in corrections.

---

## 1. Incident timeline

All times 2026-06-30, core MCP daemon (`yadgar` container, `127.0.0.1:8765`).

| Time | Event |
|------|-------|
| …–07:59:00 | Normal serving. `GET /health` 200, `GET /metrics` 200, `POST /mcp` 200 @79ms, two `POST /hooks/auto-capture` 200. |
| **07:59:00** | **Dead silence.** No error, no traceback. Logs stop mid-stream. |
| 07:59:00+ | `/health` returns **HTTP 000** (connection accepted/loop frozen — not 503). podman healthcheck flips **unhealthy**. |
| ~07:59–10:11 | Daemon hung. Backend (`yadgar-backend`, :8000 SurrealDB) stays **healthy** throughout — hang is **core-only**. |
| **10:11** | Unit exits **clean (status=0)**. systemd does **NOT** restart it. |
| 10:11+ | Unit `inactive (dead)`. Container gone (`docker run --rm`). Recovery needed manual `systemctl --user restart yadgar`. |

**Load context:** the hang occurred under heavy parallel MCP load — ~10+
concurrent subagents calling `recall`/`wiki_read`/`wiki_query` plus main-thread
writes (`adr_add`/`checkpoint`/`memorize`/`agent_prompt_save`), all hitting :8765
at once. Two earlier MCP timeouts the same session → recurring under concurrency.

**Two distinct problems:**
1. **The hang** — the asyncio event loop blocks under concurrency (root bug).
2. **No auto-recovery** — a hung/unhealthy daemon does not self-heal (availability gap).

---

## 2. RCA Part 1 — why the event loop blocks under concurrency

### Primary mechanism (#1, strong evidence)

**Synchronous MCP tool bodies run blocking `git` subprocesses _inline on the
event-loop thread_; cache misses are synchronized by a time-bucketed `lru_cache`,
so under sustained concurrency a wave of serialized 2-second git calls freezes the
loop.**

End-to-end chain, with file:line evidence:

1. **Every MCP tool is registered through a SYNC wrapper.**
   `yadgar/server/_app.py:344` — `def _instrumented(*args, **kwargs):` (plain
   `def`, **not** `async def`), registered at `_app.py:383` via
   `return mcp_server.tool()(_instrumented)`. Because this is yadgar's own `def`,
   the dispatch behaviour is robust to FastMCP internals — but the consumer side
   was verified directly (see Linchpin below).

2. **FastMCP runs a sync tool body inline on the loop — no offload.**
   The MCP SDK (`mcp>=1.23.0`, pinned `pyproject.toml:42`; container ships
   **1.28.0**) dispatch path:
   - `mcp/server/fastmcp/server.py:343` `async def call_tool` → `:346`
     `await self._tool_manager.call_tool(...)`
   - `mcp/server/fastmcp/tools/tool_manager.py:81` `async def call_tool` → `:93`
     `await tool.run(...)`
   - `mcp/server/fastmcp/tools/base.py:93` `async def run` → `:101`
     `await self.fn_metadata.call_fn_with_arg_validation(self.fn, self.is_async, …)`
   - `mcp/server/fastmcp/utilities/func_metadata.py:92-95`:
     ```python
     if fn_is_async:
         return await fn(**arguments_parsed_dict)
     else:
         return fn(**arguments_parsed_dict)   # <-- INLINE. No anyio.to_thread, no run_sync.
     ```
   The sync branch calls `fn(...)` **directly**, with no threadpool offload
   anywhere in the chain. `call_fn_with_arg_validation` is itself a coroutine
   awaited on the event loop, so the sync tool body — and anything it blocks on —
   executes **on the event-loop thread**.

3. **Tool bodies fire `git` subprocesses inline.** On a cache miss:
   - `recall` — `yadgar/server/tools/recall.py:541-542` calls `_detect_branch(_cwd)`
     + `_get_default_branch(_cwd)` per request.
   - `wiki_*` — same pattern at `yadgar/server/tools/wiki.py:635, 723, 1040`.
   - `memorize`, `misc.py` — same.
   Each resolves to `subprocess.check_output(["git", …], timeout=2)`:
   - `yadgar/server/tools/project.py:119-122` (`_detect_branch_cached`)
   - `yadgar/server/tools/project.py:163-177` (`_get_default_branch_cached`)

4. **The "normal 200s THEN silence" timeline is explained by a TIME-BUCKETED
   cache, not new directories.** `yadgar/server/tools/project.py`:
   - `:110-111` `@functools.lru_cache(maxsize=128)` on
     `_detect_branch_cached(directory, _ts_bucket)`.
   - `:144` wrapper passes `int((time.time() + hash(directory) % 30) // 30)` →
     bucket **rolls over every 30s** per hot directory (phase-shifted by
     `hash(directory) % 30` — the "thundering-herd prevention, v5.1 C3" comment at
     `:137-139`).
   - `:159-160, 197` `_get_default_branch_cached(directory, _ts_bucket)` keyed on
     `int(time.time() // 300)` → globally aligned **5-min rollover, no phase shift**.

   Warm cache → normal 200s. At a bucket boundary, all concurrent requests for
   that directory miss **simultaneously** → N serial 2s git subprocesses queue on
   the single loop thread → "dead silence." Concurrency is exactly what converts a
   single 2s blip into a multi-second loop freeze. The 5-min default-branch bucket
   is globally phase-aligned, so its rollover hits every hot directory at once —
   the worst-case synchronized miss.

5. **The block is on the LOOP THREAD → explains `/health` = HTTP 000.**
   `subprocess.check_output` is a blocking C call; while it runs, no other
   coroutine — including the pure-async `/health` route — can execute. A merely
   thread-pool-saturated daemon would still answer `/health` (the loop keeps
   spinning). The HTTP-000 observation is the discriminator that rules out
   pool-exhaustion and embedding theories — **and it is only valid because the
   work is genuinely on the loop**, which the Linchpin verification confirms.

#### Linchpin — verified, not assumed

The whole of #1 rests on: _a sync `def` tool body runs inline on the loop thread._
This was the one BLOCKING gap the advisor flagged (FastMCP/Starlette commonly
offload sync handlers via `anyio.to_thread.run_sync`, which would keep `/health`
answerable and collapse #1). **Verified against the container's shipped SDK
(`mcp` 1.28.0):** `func_metadata.py:92-95` runs the sync branch as bare
`fn(**kwargs)` with no `to_thread`/`run_sync`, and the full call_tool → run →
call_fn chain contains no threadpool wrapper. Linchpin holds. (If the SDK is ever
upgraded to a version that offloads sync tools, re-test — #1 and P1 below would
need reframing; P0 and Part 2 would not.)

### Honest caveat — trigger pattern vs. 2-hour duration

The on-loop-git mechanism cleanly explains the **trigger pattern** (recurring
multi-second stalls; the two earlier MCP timeouts this session; the
normal-then-silence shape). It does **not**, on its own, explain the **~2-hour
non-recovery**. Each git call self-releases at `timeout=2`, recurring every 30s
— that is a stall storm, not a permanent freeze. A 2h dead daemon requires an
**additional factor** on top of the on-loop-git theory, most plausibly:

- **Load never drained** — requests arriving faster than the loop clears the
  backlog, so the freeze looks permanent until upstream callers give up; OR
- **`git` itself stuck past its `timeout`** — `.git/index.lock` contention, an
  fs/NFS D-state stall, or a credential-helper waiting on stdin. `timeout=2` only
  bounds well-behaved git; a git process wedged in uninterruptible I/O can exceed
  it.

This distinction is **unresolvable from the code alone** — no incident log,
benchmark, or RCA note for this hang exists in the repo (`docs/`, `benchmarks/`
searched). It must be settled by runtime observation, which was never captured.
**Implication for the plan:** P0 (auto-recovery) is correct and sufficient
regardless of which factor caused the 2h duration — it bounds *any* hang. P1
(get git off the loop) removes the *trigger*. Neither is contingent on resolving
the duration question; the regression guard (§5) should capture the missing
runtime evidence going forward.

### Runners-up

- **#2 — Inline git on the hook routes.** `yadgar/server/http.py:838`
  (`_detect_branch` inline in async `hook_subagent_stop`; note `_memorize` beside
  it at `:849` **is** wrapped in `to_thread`, the branch-detect is **not**),
  `http.py:1203` (`subprocess.run(["git","rev-parse"], timeout=3)` inline),
  `http.py:1234`. Same loop-thread block, but fires only on `/hooks/*` POSTs —
  contributory only if load includes hook traffic. The timeline shows two
  `POST /hooks/auto-capture` just before the freeze, so this is plausibly
  **co-firing** with #1, not the sole cause.

### Ruled out (with evidence)

- **Cold-start embedding model load** — `embed_service.py:329-339` `_get_engine()`
  is double-checked-locking cached and loaded eagerly at startup
  (`embed_service.py:454`). Cannot produce a freeze that begins *after* steady
  200s.
- **Sync-over-async** — zero `.result()` / `asyncio.run` / `run_until_complete`
  in `yadgar/`.
- **`threading.Lock` on the loop** — `_metrics_lock`/`_event_lock`
  (`http.py:1627, 1764, 1772`) wrap microsecond `dict()` copies only;
  `_action_batch_lock` is correctly an `asyncio.Lock`.
- **SSE stream** — `_make_event_stream` (`http.py:1745`) `while True` has
  `await asyncio.sleep(0.5)` + `await request.is_disconnected()` each iteration;
  cooperative.
- **Thread-pool saturation / SurrealDB pool exhaustion** — would NOT produce
  `/health` = 000; the loop keeps spinning. (And the backend stayed healthy.)

---

## 3. RCA Part 2 — why there was no auto-recovery

**Three independent gaps; all three had to fail for non-recovery — and all
three did.** Evidence in `/home/max/git/nix/modules/home/yadgar.nix`.

### Gap 1 — `Restart=on-failure` ignores a clean exit (`yadgar.nix:508`)

`Restart=on-failure` restarts only on: non-zero exit, signal kill, watchdog
timeout, or start-condition failure. A systemd result of `success` (exit 0) is
explicitly excluded. The loop hung, then at 10:11 the container/unit exited
**cleanly (status=0)** — systemd classified it `success`, skipped restart, unit
went `inactive (dead)`.

### Gap 2 — `--sdnotify=healthy` is one-shot, not continuous (`yadgar.nix:506`)

`--sdnotify=healthy` makes podman wait until the healthcheck first reports
**healthy**, emit `READY=1` to systemd **once**, then go silent. It does **not**
continuously enforce health. When `/health` later returned 000 and the
healthcheck flipped **unhealthy**, systemd had no visibility — the unit stayed
`active (running)` while the container was dead inside. There is no
`FailureAction`, no health-event binding, no podman-socket watcher in the unit.

### Gap 3 — no watchdog at all (absent across `modules/home/`)

- `WatchdogSec` — not set in `yadgar.nix` or any other home module.
- App-side `WATCHDOG=1` keepalive — the daemon emits nothing.
- `NotifyAccess=all` exists (`yadgar.nix:466`) but only lets the graceful-stop
  orchestrator emit `STOPPING=1` — unrelated to liveness.
- Zero matches for `WatchdogSec` / `WATCHDOG` / `liveness` / `restart-on-unhealthy`
  / `auto-heal` anywhere under `/home/max/git/nix/modules/home/`.

Without `WatchdogSec` + app-side `WATCHDOG=1` pings, systemd has no stall
detector. A hung asyncio loop that keeps the process alive but answers nothing is
invisible to it.

### Compounding context (comments, `yadgar.nix:~438–464`, ~500)

- `BindsTo` + `Requires` were **removed** after a 2026-05-20 backend OOMKill
  cascaded into a core shutdown via `BindsTo`; replaced with `Wants=` only.
  Correct fix for that incident — but it also removed the one mechanism that
  would have propagated a container-failure signal to systemd.
- `--health-interval 30s → 15s`: detection window halved (good) — but a detected
  unhealthy state does nothing post-startup (Gap 2).
- `--health-timeout 5s → 8s`: buffer widened per the C2 `/health`-timing context.
- Healthcheck retries kept at podman default 3 to preserve the C1/C2 anti-flap
  margin — do **not** lower.
- `StartLimitIntervalSec` / `StartLimitBurst` not set → systemd default
  (5 starts / 10s) applies; not a blocking factor here, but relevant to P0
  (see Open decisions).

### The availability gap, precisely

```
asyncio loop hangs (on-loop git, §2)
  → container stays running (no crash signal)
  → /health returns HTTP 000
  → podman healthcheck → UNHEALTHY
  → sdnotify already fired READY=1 at startup, silent now (Gap 2)
  → systemd: unit still active (running); no watchdog (Gap 3); no event binding
  → ~2h later: container exits clean (status=0)
  → Restart=on-failure: result=success → no-op (Gap 1)
  → unit: inactive (dead)  →  manual `systemctl --user restart yadgar` required
```

---

## 4. Phased fix plan

### P0 — Auto-recovery (stop the bleeding) — **ship first, independent of P1**

P0 bounds *any* hang regardless of cause; it does not depend on the §2 mechanism
or the duration question. Two layers — land at least Layer A immediately.

**Layer A — restart on clean/abnormal exit.** Change `yadgar.nix:508`
`Restart=on-failure` → `Restart=always` (or `Restart=on-abnormal`).
- Effort: trivial (one-line nix change; user applies — Claude does not run nix).
- Risk: `Restart=always` also restarts after an intentional `systemctl --user
  stop`. Mitigate by relying on systemd's stop-state suppression (a manual `stop`
  sets the unit inactive and `Restart=` does not fight an explicit stop) and/or
  pairing with `StartLimitBurst`. `on-abnormal` is the lower-blast-radius
  alternative (restarts on signal/timeout/watchdog but not clean `stop`); but
  note it would **not** have caught this incident's clean status=0 exit — only
  `always` covers that. Recommend `always` + a sane `StartLimitIntervalSec` /
  `StartLimitBurst` to cap restart storms.
- This alone fixes the *observed* incident (clean exit + on-failure no-op).

**Layer B — real liveness watchdog (closes Gap 3; catches the hang BEFORE it
exits).** Add `WatchdogSec=<N>s` to the unit **and** have the daemon emit
`sd_notify("WATCHDOG=1")` on a cadence < `WatchdogSec/2`. On a missed ping
systemd kills (`SIGABRT`) and — with `Restart=always` — re-runs the unit.
- **Critical design constraint (advisor):** the ping MUST be driven **by the
  event loop** — an `asyncio` task, or piggy-backed on each `/health` success —
  **never a background thread**. A thread-driven ping keeps firing even when the
  loop is dead, which defeats the entire purpose. The ping firing must be proof
  the loop is alive.
- Container caveat: `WatchdogSec` requires `NOTIFY_SOCKET` to reach the daemon
  inside the container. Verify podman propagates it (`--sdnotify` already implies
  a notify socket is wired; confirm the in-container process can write
  `WATCHDOG=1`, not just podman's startup `READY=1`). If podman intercepts the
  socket and only forwards startup readiness, Layer B needs the daemon's pings to
  reach systemd through podman's `--sdnotify=container` mode (not `healthy`) —
  this is a **design decision**, see Open decisions.
- Effort: moderate — nix unit change + a small app-side async watchdog pinger +
  notify-socket plumbing through podman. App code is test-driven (red→green).
- Risk: misconfigured cadence/socket → false-positive restarts (flap). Mitigate
  with `WatchdogSec` generous relative to worst-case healthy `/health` latency
  (respect the C1/C2 anti-flap history) and a startup grace before the first ping.

**Layer C (optional fallback) — podman health-event → restart binding.** A
systemd path/socket unit (or sidecar) watching `podman events` for
`health_status=unhealthy` that calls `systemctl --user restart yadgar`.
- Effort: moderate; **no existing pattern in `modules/home/` to borrow** (greps
  found none) — net-new.
- Risk: more moving parts; event-watcher itself becomes a thing to keep alive.
- Use only if Layer B's in-container watchdog socket proves impractical. Layer A
  + B is the recommended target; C is a belt-and-suspenders fallback.

### P1 — Concurrency fix (remove the trigger)

Get the blocking `git` subprocess **off the event-loop thread**. Options, in
order of preference:

1. **Offload at the dispatch boundary (broadest fix).** Make `_instrumented`
   (`_app.py:344`) register tools as `async def` whose body does
   `return await asyncio.to_thread(_traced_func, *args, **kwargs)`. This takes
   FastMCP's `fn_is_async` branch (`func_metadata.py:92` → `await fn(...)`) and
   moves **every** sync tool body to a worker thread in one change.
   - Effort: moderate. Risk: changes the threading model for *all* tools — any
     tool relying on loop-thread affinity (e.g. touching an `asyncio.Lock`
     directly) would break. Audit tool bodies for loop-thread assumptions first.
     This is the highest-leverage but highest-blast-radius option.
2. **Offload only the git calls (surgical, lower risk).** Wrap each
   `_detect_branch` / `_get_default_branch` / `_resolve_project_root` call (and
   the inline ones at `http.py:838, 1203, 1234`) in `asyncio.to_thread(...)` —
   but the tool bodies are sync, so this requires the caller chain to be async.
   Simplest surgical form: pre-resolve branch/default-branch **once per request
   off-loop** (or make `project.py` helpers async + offload internally) so the
   subprocess never runs on the loop.
   - Effort: moderate; touches `recall.py`, `wiki.py`, `project.py`, `http.py`.
     Risk: lower than option 1 (scoped to git), but more call sites to change.
3. **Eliminate the synchronized-miss storm.** Pre-resolve branch info at request
   ingress and cache per-(directory) with a background async refresher, so a
   bucket rollover never triggers N simultaneous on-request subprocesses. Pairs
   well with 1 or 2 — reduces *frequency* even if a stray call lands on the loop.
   - Effort: higher (new refresh task + cache lifecycle). Risk: cache staleness
     vs. branch switches mid-session.

**Recommended P1:** option 1 (dispatch-boundary `to_thread`) **after** an audit
for loop-thread affinity, with a per-request timeout cap around the tool body so
a wedged git can't hold a worker thread indefinitely. Add a bounded concurrency
cap (`asyncio.Semaphore`) on in-flight MCP tool calls so a worker-pool can't be
exhausted by the same load that caused this incident.

### Sequencing

`P0 Layer A` (one line, ship today) → `P0 Layer B` (watchdog, this week) →
`P1` (concurrency, test-driven). P0 is the priority: it makes *any* future hang
self-heal in ≤`WatchdogSec`, converting a 2h outage into a sub-minute blip while
P1 removes the trigger.

---

## 5. Reproduction + regression guard

### Reproduce the hang

1. Point a client at a hot git directory (so branch caches are populated).
2. Wait for a cache-bucket boundary (≤30s; or force by clearing
   `_detect_branch_cached.cache_clear()`).
3. Fire **N≥10 concurrent** `recall`/`wiki_read`/`wiki_query` calls for that same
   directory at the boundary, so they miss simultaneously.
4. Concurrently poll `GET /health`. Expectation on unpatched code: `/health`
   goes **HTTP 000 / times out** for ~N×2s while the serial git subprocesses
   drain on the loop thread.
5. To reproduce the **non-recovery**: SIGSTOP the container's python (simulate a
   wedged loop) → confirm the unit does *not* restart on unpatched nix; confirm
   it *does* (within `WatchdogSec`) after P0 Layer B.

### Regression guard

- **P1 unit/integration test (test-driven, app code):** assert that while a
  tool's git call is in-flight (monkeypatch `subprocess.check_output` to block on
  an event), a concurrent `/health` request still returns 200 within a tight
  budget (e.g. 200ms). On unpatched code this **fails** (loop blocked); after the
  `to_thread` fix it **passes**. This is the red→green guard for "git is off the
  loop."
- **Concurrency soak:** a benchmark firing N concurrent recall/wiki calls across
  a bucket boundary, asserting p99 `/health` latency stays bounded.
- **Runtime evidence (closes the §2 duration gap going forward):** add structured
  logging of (a) per-tool wall-time, (b) git-subprocess wall-time, and (c) a
  loop-lag metric (scheduled-vs-actual `asyncio` callback delay). The 2h-vs-2s
  question was unanswerable *only* because none of this was captured. Emit it so
  the next incident is diagnosable from logs alone.

---

## 6. Open decisions for the user

1. **`Restart=always` vs `on-abnormal`.** `always` covers the clean-exit case
   that caused *this* incident; `on-abnormal` is lower-blast-radius but would
   **not** have caught it. Recommend `always` + `StartLimitIntervalSec`/
   `StartLimitBurst`. Confirm acceptable restart-after-manual-stop behaviour.
2. **Watchdog socket through podman.** Layer B needs the in-container daemon to
   write `WATCHDOG=1` to systemd. Current unit uses `--sdnotify=healthy`
   (startup-only). Decide between: (a) switch to `--sdnotify=container` + app-side
   watchdog pings, or (b) keep `healthy` for startup and add Layer C
   (health-event watcher) instead. Needs a quick podman notify-socket
   propagation test.
3. **P1 blast radius.** Dispatch-boundary `to_thread` (broad, one change) vs.
   surgical per-git offload (scoped, more sites). Pick based on the loop-thread-
   affinity audit of tool bodies.
4. **MCP SDK pin.** SDK 1.28.0 runs sync tools inline (verified). If the pin is
   ever bumped, re-verify `func_metadata.py` dispatch — a future version that
   offloads sync tools changes P1's premise.
5. **Reconcile with yadgar memory.** MCP was down during this RCA; once back,
   cross-check against existing hang/perf wiki pages and reconcile.

---

## Key files

- `yadgar/server/_app.py` — `_instrumented` sync wrapper (344, 383).
- `yadgar/server/tools/project.py` — git subprocesses + time-bucket cache
  (110-122, 144, 159-197).
- `yadgar/server/tools/recall.py` — per-request branch detection (541-542).
- `yadgar/server/tools/wiki.py` — same pattern (635, 723, 1040).
- `yadgar/server/http.py` — inline git on hook routes (838, 1203, 1234).
- `mcp/server/fastmcp/utilities/func_metadata.py` — SDK sync-dispatch linchpin
  (92-95, container SDK 1.28.0).
- `/home/max/git/nix/modules/home/yadgar.nix` — unit; recovery gaps
  (Restart 508, sdnotify/health 506, NotifyAccess 466, BindsTo history ~438-464).
