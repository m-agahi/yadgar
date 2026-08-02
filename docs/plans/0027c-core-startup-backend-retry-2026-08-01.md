# Plan: bounded wait-for-backend at core startup — replace the schema-init crashloop

**Date:** 2026-08-01
**Task:** #0027c
**ADR:** new (next free id, ≈ADR-0189) — see §7
**Coupled to:** #0111 (`Requires=` → `Wants=`), #0110 (changes the race regime)
**Status:** design proposed, not started

---

## 1. Problem

When the core starts while the backend is down, it crashloops forever instead of waiting.

### 1.1 The chain, with evidence

`yadgar/_shared/storage/__init__.py:294` — `StorageEngine.__init__` calls `self._init_schema()`
**inline**, on the constructor, immediately after building the `httpx.Client`:

```python
self._http = httpx.Client(base_url=self._db_url, …)
self._init_schema()
```

`yadgar/_shared/storage/migrations.py:1215-1221` — `_init_schema` issues HTTP on its first
statement:

```python
def _init_schema(self):
    # ---- Analyzers ----
    self._q("""
        DEFINE ANALYZER IF NOT EXISTS mem_analyzer …
```

`yadgar/_shared/runtime/lifecycle.py:409` — `init_engines` constructs it unguarded:

```python
_st._storage = StorageEngine(db_path or _settings.DB_PATH)
```

`yadgar/core/server/_startup.py:86` — `main()` calls that (as
`from yadgar.core.bootstrap import core_init_engines as init_engines`, `_startup.py:32`) with no
try/except.

So an `httpx.ConnectError` propagates out of the constructor, out of `init_engines`, out of
`main()`; the process exits non-zero; `Restart=on-failure` + `RestartSec=5` brings it straight
back into the same failure. With a ~1s crash and a 5s restart delay the cycle is ~6s, which stays
under systemd's default `StartLimitBurst=5` per `StartLimitIntervalSec=10s`, so the unit never
enters `failed` and **loops indefinitely**.

### 1.2 Scope: startup only

At runtime the core survives a backend outage — the storage layer's request path handles
connection errors per call. This is exclusively the *construction* path. Do not widen the fix into
runtime behaviour.

### 1.3 Why the race exists at all — and why it is about to get wider

| regime | backend `Type=` | what `After=` guarantees | race? |
|---|---|---|---|
| today, shell path (`yadgar-backend.service.in:51`) | `simple` | the `podman run` **fork** succeeded | **yes, wide.** Backend cold model load runs entirely after the core is released to start. Note: this car does **not** close that window — see §3.1; #0110 does |
| today, Python path (`systemd.py:112-126`) | `notify` / `exec`+gate | backend is HEALTHY | narrow — only a crashed-backend restart window |
| after **#0110** | `notify` / `exec`+gate on both | backend is HEALTHY | narrow on both |
| after **#0111** (`Requires=` → `Wants=`) | — | **ordering still holds** (`After=` is unaffected by `Wants=` vs `Requires=`), but a *failed* backend no longer stops the core from starting | wider: the core now starts into a backend that failed, where before it would have been held back |

0111 is the reason this car is needed even after 0110. `Wants=` is the right call — it stops one
failed unit taking the stack down — but its cost is precisely that the core must tolerate a
backend that is not there. That tolerance is what this car builds.

---

## 2. Decision

**D1 — a bounded readiness gate before the storage engine is constructed, on the CORE path only,
then fail cleanly.**

**D2 — placement: `core_init_engines` (`yadgar/core/bootstrap/bootstrap.py:63`), immediately
before it delegates to `_shared_init_engines`.**

The discriminating constraint: the **backend's own bootstrap calls the same
`lifecycle.init_engines`** —
`yadgar/backend/embed_service/embed_service.py:928` calls
`_init_engines(local_engines=True, engine_set="slim")`. A gate placed in `init_engines` would make
the backend wait for itself at startup. `core_init_engines` is the core composition root and is
**not** on the backend's path (the backend imports `lifecycle.init_engines` directly), so a gate
there is core-only by construction, needs no flag, and runs before `StorageEngine` exists.

`_startup.main()` before its `init_engines(…)` call is an acceptable second choice — same
core-only property — but it misses any other core caller of `core_init_engines`. Prefer the
composition root.

**D3 — do NOT put the retry inside `StorageEngine.__init__`.** Every construction site pays it:
tests, CLI one-shots (`yadgar vacuum`, `yadgar seed`, the nightly cycle), and the backend's own
slim bootstrap. Wrong blast radius, and it would turn a fast, clear "backend is down" error in a
CLI one-shot into a two-minute hang.

**D4 — poll the backend's `/health`, not SurrealDB directly.** `/health` on the backend returns
200 only when `db_ok and engine_loaded` (per ADR-0187's rationale), which is exactly the condition
the core's schema init needs. There is already a precedent to copy in-repo:
`yadgar/core/daemon/daemon.py:340-363` polls the embed service's `/health` on
`DEFAULT_BACKEND_EMBED_PORT` with `time.sleep(2.0)` and a timeout that returns a
`"health check timed out — model may still be loading"` warning. Reuse that shape (and, where
practical, `_embed_health_ok` itself, `daemon.py:697`) rather than inventing a second poller.

**D5 — fail cleanly at the end of the budget.** On exhaustion: log at ERROR naming the URL and the
elapsed budget, then raise a typed error that `main()` turns into a non-zero exit with an
actionable message. The unit still restarts — that is correct, a backend that is down for ten
minutes should not keep a core process parked — but each cycle now costs one budget instead of
one second, so the journal shows a small number of legible attempts rather than an unreadable
flood.

### 2.1 Alternatives rejected

| option | verdict |
|---|---|
| retry inside `StorageEngine.__init__` | D3 — every construction site pays |
| retry inside `_init_schema` / `_q` | Same problem, plus it conflates "connection refused at startup" with "transient error mid-query", which the request path already handles differently |
| catch in `main()` and `sys.exit(0)` | Turns a crashloop into a silent no-op unit that reports `active` and does nothing. Worse |
| rely on systemd alone (`Requires=` + `Type=notify`) | This is what exists, and #0111 is deliberately removing half of it. Also never worked on the `Type=simple` shell path |
| `ExecStartPre=curl … /health` on the core unit | Moves the problem into the unit file, i.e. into the generators — the surface #0110 is trying to shrink. It also does not help `yadgar daemon start`, which runs the core outside any unit |
| unbounded retry | Never fails, so a genuine misconfiguration (wrong `YADGAR_DB_URL`) presents as a permanent hang inside `TimeoutStartSec`, then a timeout kill with no diagnostic |

---

## 3. Budget — and what the gate is NOT for

**There is no measured backend cold start anywhere in this repo.** State that plainly, per
ADR-0187's norm, rather than implying a measurement exists. The budget is derived, not observed.

### 3.1 The gate does not cover cold boot. Ordering does.

This distinction decides the number, so make it before picking one.

A **cold boot** where the backend is loading its model for the first time can take, by ADR-0187's
quantisation arithmetic, until t≈90s before READY=1 is even *observed* (60s
`--health-start-period` grace, then podman's default 30s health tick). That is why the backend
carries `TimeoutStartSec=180`.

A gate large enough to absorb that would need ~90-120s — which does **not** fit inside the core's
own `TimeoutStartSec=120` alongside the rest of core init (§3.2). So a retry budget cannot be the
cold-boot mechanism, and must not be sized as though it were.

It does not need to be. **`After=yadgar-backend.service` is the cold-boot mechanism**, once the
backend is `Type=notify`/gated — which is exactly what #0110 delivers. Before #0110, on the
`Type=simple` shell path, `After=` is satisfied at fork and cold boot genuinely races; this car
**cannot** fully fix that, and pretending otherwise would size the budget wrong. #0110 is the fix
for cold boot; 0027c is the fix for the crashloop.

What the gate *is* for:

| scenario | covered by the gate? |
|---|---|
| backend crashed and is restarting while the core starts | **yes** — the backend's `RestartSec=10` plus a warm restart is well inside a modest budget |
| #0111's `Wants=`: backend failed outright, core released to start anyway | **yes** — the gate is what turns an infinite crashloop into one bounded, legible attempt |
| operator restarted the core alone (`systemctl --user restart yadgar`) while the backend was down | **yes** — the observed reproduction, §6.1 |
| first-ever cold boot, backend loading its model | **no** — `After=` covers it post-#0110; pre-#0110 it is not fixable inside the core's 120s |

### 3.2 The ceiling

The core unit carries `TimeoutStartSec=120` (`scripts/install/yadgar.service.in:78` and
`systemd.py:117,124`). That 120s must cover **retry + everything else core init does**, notably:

* `_st._embeddings._ensure_model()` — an explicit eager warm-up (`lifecycle.py:427`)
* `_run_wiki_embedding_backfill` (`lifecycle.py:430`)
* the core-only engine set built in `_build_core_only_engines`

A 110s retry starves a legitimately slow start and turns a slow-but-fine boot into a
`TimeoutStartSec` kill — the same "under-budgeted gate turns a slow start into a crashloop"
failure ADR-0185 records for `ExecStartPost`.

### 3.3 The number

**60s — 30 attempts × 2s**, matching `daemon.py:343`'s 2s interval. Rationale, all arithmetic:

* it comfortably covers every §3.1 "yes" row — a warm backend restart is seconds, not a minute
* it leaves 60s of the core's 120s for the rest of core init, i.e. the relation
  `budget < TimeoutStartSec` holds with real headroom rather than by a hair
* a backend that is *still* loading a cold model when the gate expires will have finished long
  before the core's `RestartSec=5` cycle has retried a handful of times, so the "no" row in §3.1
  degrades to a few legible bounded attempts rather than the current unreadable 6s flood

Make the budget **configurable** (settings key, e.g. `BACKEND_READY_WAIT_SEC`, default 60) and
leave the literal unpinned in tests — ADR-0187's precedent: *"the literals stay unpinned so an
evidence-backed retune need not fight a test."* Assert the **relation** (retry budget strictly
less than `TimeoutStartSec`), not the number.

Backoff: fixed 2s, not exponential. The wait is for an external process to finish loading a model;
exponential backoff only adds latency after readiness, and 2s matches the in-repo precedent.

---

## 4. Files to change

| file | change |
|---|---|
| `yadgar/core/bootstrap/bootstrap.py` | new `_await_backend_ready()` call at the top of `core_init_engines`, before `_shared_init_engines` |
| new helper (module TBD — `yadgar/core/bootstrap/` or `yadgar/_shared/runtime/`) | the poll loop; reuse `daemon.py:697` `_embed_health_ok` if it can be imported without pulling the daemon module's weight into startup |
| `yadgar/_shared/config/config.py` | `BACKEND_READY_WAIT_SEC` (default 60), `BACKEND_READY_POLL_SEC` (default 2) |
| `yadgar/core/server/_startup.py` | turn the exhaustion error into a legible non-zero exit; keep the raise, do not swallow |

**Skip the gate** when the backend is local/in-process — if `YADGAR_EMBED_URL` is unset there is
no remote backend to wait for. Check the actual config resolution before wiring this; do not
assume the variable name.

---

## 5. TDD story

CI gates `yadgar/tests/core/` (`.forgejo/workflows/ci-pr.yaml:259`) and `yadgar/tests/_shared/`
(`:128`) in separate jobs. The gate is core-only, so tests belong in **`yadgar/tests/core/`** —
putting them in `_shared/` would be both wrong-owner and, worse, would suggest the gate is shared.

RED first, in order:

1. **`test_core_startup_waits_for_backend`** — patch the readiness probe to fail N times then
   succeed; assert `core_init_engines` completes, that the probe was called >1 time, and that
   `StorageEngine` was constructed **after** the last probe (ordering is the whole point — assert
   it, don't infer it).
2. **`test_core_startup_gives_up_after_budget`** — probe always fails; assert the typed error is
   raised, that elapsed time is bounded by the configured budget (with a small tolerance), and
   that the error message names the URL. Run it with a tiny injected budget so the test is fast.
3. **`test_backend_slim_bootstrap_does_not_wait_for_itself`** — the regression guard for the D2
   constraint. Call `lifecycle.init_engines(local_engines=True, engine_set="slim")` with the probe
   patched to a spy, and assert the spy was **never called**. This is the test that stops a future
   refactor from "simplifying" the gate down into `init_engines` and deadlocking the backend.
4. **`test_retry_budget_is_inside_core_unit_timeout`** — a relation assertion. Read
   `TimeoutStartSec` from the rendered core unit (`yadgar/tests/_unit_render.py:47` after #0110,
   or the `systemd.py` render) and assert `BACKEND_READY_WAIT_SEC` is strictly less. Mutation-prove
   it by setting the budget to 200 and confirming RED. Use `[^\S\n]` not `\s` around `=` in the
   unit regex.

Test 4 spans two dirs by nature. Put it in `yadgar/tests/core/` with the renderer imported; do not
split it.

---

## 6. Verification

**Locally provable:** all four tests above; the full `yadgar/tests/core/` suite for regressions in
startup ordering.

**Requires the fresh VM** (`192.168.122.101`,
`sshpass -p 'Aa1234.' ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password root@192.168.122.101`):

1. **Reproduce the bug first, on the shipped build.** `systemctl --user stop yadgar-backend`,
   then `systemctl --user restart yadgar`, then `journalctl --user -u yadgar -f`. Expect the
   ~6s crash cycle. Capture it — this is the before-picture and it is the only place the real
   crashloop is observable.
2. **After the fix, same sequence.** Expect the core to sit in `activating` while logging waiting
   attempts, then either come up when the backend is started, or exit once with a legible error
   after the budget.
3. **Ordered cold boot.** Reboot the VM. Both units come up; the core does not crash even once.
   Check `systemctl --user show yadgar -p NRestarts` — expect `0`.
4. **The 0111 interaction, explicitly.** With `Wants=` in place, `systemctl --user start yadgar`
   with the backend **masked**. The core must start, wait its budget, and exit cleanly — not hang
   past `TimeoutStartSec` and not crashloop.

Snapshot the VM before each run.

---

## 7. Rollback

Self-contained and reversible: one call site plus a helper and two settings. Revert the commit and
the previous behaviour returns exactly.

Two escape hatches worth building in, cheaply:

* `BACKEND_READY_WAIT_SEC=0` disables the gate entirely (immediate pass-through to today's
  behaviour). A user hitting an unexpected hang has a one-env-var out that does not need a
  downgrade.
* the gate logs one line per attempt at INFO with attempt number and elapsed — so a support
  question is answerable from `journalctl` without a rebuild.

No data is touched. No migration.

---

## 8. ADRs

**New ADR** (next free id, ≈ADR-0189). Content:

* **decision** — bounded readiness gate at the core composition root, poll the backend's
  `/health`, then fail cleanly
* **rationale** — the backend shares `lifecycle.init_engines`, so the gate must sit above it in
  `core_init_engines`; `/health` is the right signal because it means `db_ok and engine_loaded`;
  the budget is arithmetic (§3), **not** a measurement, and there is none in the repo
* **alternatives** — §2.1, especially "systemd alone", which is what #0111 is removing
* **consequences** — the core unit now spends up to 60s in `activating` where it previously
  crashed in 1s, so `NRestarts` stops being the signal that the backend is down and the journal
  becomes it; `BACKEND_READY_WAIT_SEC` must stay strictly inside `TimeoutStartSec` or a slow start
  becomes a timeout kill; the gate is core-only by construction and test 3 (§5) is what keeps it
  that way
* **revisit_trigger** — someone measures a real backend cold start (which would also retire
  ADR-0187's arithmetic), or the core stops sharing `init_engines` with the backend

**ADR-0187** — reference it for the budget-derivation norm and for the `[^\S\n]` regex rule. No
change to it from this car.

---

## 9. Ordering

* **After #0111.** 0111 is what makes this urgent; landing this first is harmless but the ADR's
  rationale reads oddly. Prefer 0111 → 0027c.
* **Independent of #0110** in code — nothing here touches a generator. But 0110 changes the regime
  (§1.3), so if 0110 lands first, this car's ADR should say the race is narrow-but-real on both
  paths rather than wide on the shell path.
* **Independent of #0112.** Parallel.
