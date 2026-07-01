# Test-suite speedup — measured cost-breakdown & prioritized plan (2026-07-01)

**Status:** plan only, NOT implemented. Measure-first.
**Pain:** ~1.5h CI per PR + ~20min pre-push `make e2e`. Goal: cut wall-clock via a
measured, prioritized lever list.

> **Provenance discipline.** Every number below is tagged `[measured]`,
> `[CI-config]`, `[code-read]`, or `[estimate]`. Do not treat estimates as
> measurements. The contention sweep ran on a **quiet box** — see the two traps
> in §3 before over-claiming.

---

## 0. Headline finding (overturns the prior hypothesis)

The working prior was: *surreal `read-only transaction` / `namespace does not
exist` flakes + `--reruns` are CPU/socket contention from xdist stacking; fewer
workers cuts both flakes AND wall-clock.* **Measurement refutes the wall-clock
half and refines the flake half:**

1. **Wall-clock is FLAT across worker counts.** Same 257-test surreal-heavy slice:
   `-n4` 437.7s · `-n8` 435.8s · `-n12` 434.7s · `-n auto (10, RAM-capped)` 441.0s
   `[measured]`. Adding or removing workers does **nothing**. **Tuning `-n` is not
   a speedup lever for this suite.**

2. **The wall-clock floor is a per-test `init_engines()` serial chain.** 91 of the
   ~460 test files call `server.init_engines()` inside a **function-scoped**
   `@pytest.fixture(autouse=True)` `[code-read]` — full SurrealDB schema init +
   migrations on **every test**, ~5–10s of setup per test `[measured, durations]`.
   Parallelism can't help below the point where per-worker surreal throughput
   saturates, so wall-clock is pinned by `N_tests × per-test-init`, not by cores.

3. **The severe flakes are CROSS-RUN, not in-run.** The classic `read-only
   transaction` / `namespace does not exist` cascade did **NOT reproduce** on the
   quiet box (0 RERUN, only ~1% transient `Connection reset by peer`) `[measured]`.
   Prior anchors (1847 errors / 3825 ConnectErrors, v5.58) were **CI-vs-local
   collision** on the shared 24-core/64GB box — 5 CI matrix jobs × up to 10 workers
   × 1 surreal each, running alongside a local run. The `make test` TEST_LOCK flock
   serializes *local* runs but **CI bypasses it** (`--splits` invoked directly)
   `[code-read Makefile:268]`.

**Consequence for the lever list:** the biggest wall-clock win is **fixture-scope
+ per-test teardown**, not `-n`. The biggest *reliability* win is **CI-vs-local
mutual exclusion**, not lower `-n`.

---

## 1. Measured cost-breakdown

| Cost source | Value | Provenance |
|---|---|---|
| Collection (import + collect, no exec) | 15.0s wall / 11.3s pytest, once per process | `[measured]` |
| Per-process startup (python+uv+pytest) | ~9s | `[measured, derived]` |
| `import sentence_transformers` | 2.03s | `[measured]` |
| `all-MiniLM-L6-v2` load | 0.876s | `[measured]` |
| Cross-encoder (`ms-marco-MiniLM-L-6-v2`) load | 1.608s, lazy (recall/rerank tests only) | `[measured]` |
| Model load — per-worker? | Yes; `EmbeddingEngine._model_cache` is class-level → paid once per worker (`YADGAR_MODEL_PRELOAD=false`) | `[code-read]` |
| **`init_engines()` per function-scoped fixture** (surreal connect + `_init_schema` + `_run_migrations`) | **~5–10s per test, in 91 files** | `[measured, durations]` + `[code-read]` |
| `_wipe_surrealdb_data` teardown (autouse, per test) | HTTP DELETEs per namespace, folded into per-test cost | `[code-read conftest:672]` |
| OTLP teardown noise | ~3–9s/test of DNS-retry drain when `setup_tracing()` wires prod `otlp_endpoint` before config isolation on first test/worker | `[measured, logs]` + `[code-read]` — **SIZE INDEPENDENTLY, see §5** |
| surreal fixture | SESSION-scoped per worker (1 SurrealDB/worker) | `[code-read conftest:278]` |
| In-run rerun tax | 0 RERUN on quiet box; ~1% transient ConnectError | `[measured]` |
| CI rerun tax (collision) | 1847 err / 3825 ConnectError under CI-vs-local collision | `[estimate, prior anchor v5.58]` — **not reproducible here** |
| e2e | 116 tests, `-n0` serial, ~20min | `[code-read]` + `[estimate]` |

**wall-clock ÷ Σdurations = 1.62×** `[measured]` (top-30 durations = 268s vs 436s
wall @ `-n8`). ~38% of wall-clock is outside even the 30 slowest tests → worker
startup, surreal spawn, collection, OTLP drain, and the long tail of per-test
`init_engines()`. **Confirms overhead-bound, not test-body-bound.**

---

## 2. Contention sweep (the discriminator)

| `-n` | wall (s) | pass | err | RERUN | flake strings |
|---|---|---|---|---|---|
| 4 | 437.7 | 253 | 4 | 0 | `Connection reset by peer` ×1 module |
| 8 | 435.8 | 253 | 4 | 0 | `Connection reset by peer` ×1 module |
| 8 (rerun) | 437.6 | 257 | 0 | 0 | none |
| 12 | 434.7 | 254 | 3 | 0 | `Connection reset by peer` ×1 module |
| auto (10) | 441.0 | 257 | 0 | 0 | none |

- **Flat scaling** — verified across four `-n` levels `[measured]`.
- **No oversubscription penalty** (RAM-cap holds workers at 10 on this box).
- **Contention hypothesis did NOT reproduce in-run.** The `read-only transaction`
  / `namespace does not exist` cascade is a CROSS-RUN artifact.
- **No `xdist_group` markers exist in the suite** `[code-read: 0 files]` — flat
  scaling is *not* group-pinning; it is the per-test `init_engines()` floor.

**Two traps named explicitly:**
1. In-run worker contention ≠ cross-run box contention. A clean single run does not
   reproduce the CI cascade — that is refinement, not refutation.
2. The rerun tax **cannot be sized from this run** (0 reruns observed). Its sizing
   comes from prior CI anchors `[estimate]` and should be re-measured from CI logs.

---

## 3. e2e

- `make e2e` → `pytest yadgar/tests/e2e/ -m e2e -p no:randomly -n0 --reruns 2
  --reruns-delay 2` `[code-read]`. 116 tests, 15 files, **serial**.
- Per-test `init_engines()` ≈ 5s × 116 ≈ 580s fixture overhead alone `[estimate]`;
  model load paid once (process-level cache).
- **Pre-push trigger:** `e2e-behavior-contract` hook in `.pre-commit-config.yaml`,
  `stages:[pre-push]`, `files:` filter → runs only when `.py` / `Makefile` /
  `pyproject.toml` / `conftest.py` / `BEHAVIOR_CONTRACT.md` change. Docs-only pushes
  already skip it `[code-read]`.
- **Not in CI** (`-m 'not e2e'`) — CI's embedded surreal "can't run these reliably".

---

## 4. Phantom-lever verdicts

- **HF model download cache → NON-ISSUE.** `all-MiniLM-L6-v2` + SurrealDB are baked
  into `yadgar-ci:5.73.0`; locally cached in `~/.cache/huggingface` `[code-read]`.
  Do not spend effort here.
- **CI shape:** 5-way `--splits 5 --group N` matrix, each chunk `-n auto --dist
  loadgroup --reruns 2 --reruns-delay 2` `[CI-config .forgejo/workflows/ci-pr.yaml]`.
  All 5 chunks run on the **same host** → the collision source.

---

## 5. Prioritized levers (sized against the breakdown)

Ordered by expected wall-clock/reliability win per unit effort.

### P0 — Module-scope the `init_engines()` fixture (biggest wall-clock lever)
- **What:** convert the function-scoped `@pytest.fixture(autouse=True)` that calls
  `init_engines()` to **module-scoped** in the 91 affected files, relying on the
  existing autouse `_wipe_surrealdb_data` for per-test data isolation (schema/
  namespace already survive the wipe by design — conftest:672 snapshot guard).
- **Expected speedup [estimate, grounded]:** per-test init (~5–10s) collapses to
  once-per-file. A 20-test file drops from ~20×7s=140s to ~7s + 20×(wipe only). If
  the pattern holds across 91 files this attacks the dominant floor directly.
  Realistic target: **large fraction of the 1.62× overhead**, i.e. the single
  highest-value change. Must be **measured per-file** after conversion.
- **Effort:** medium-high (91 files, but mechanical) — pilot on the 5 slowest files
  first, measure, then roll out.
- **Risk:** medium. Module-scoped engines share DB state across a file → tests that
  assume a pristine DB or call `server.shutdown()` mid-file will break. `_wipe`
  handles data, NOT schema/singletons. **Pilot + full-suite verify required.** Some
  files already resist module-scoping (that's why they're function-scoped today) —
  audit each, don't blanket-convert.

### P1 — Fix the OTLP per-test drain (cheapest high-value)
- **What:** ensure `setup_tracing()` never wires the **production** `otlp_endpoint`
  under test. Either force `OTEL_SDK_DISABLED`/no-op exporter for the test session
  via a conftest fixture ordered **before** any tracing setup, or guarantee
  `_isolate_yaml_config` runs before first `setup_tracing()`. (Mind the known
  gotcha: OTEL reads `OTEL_SDK_DISABLED` at `TracerProvider` construction, process-
  wide — set it at session start, not module scope. See wiki *OTEL_SDK_DISABLED
  module-scope setdefault poisons xdist test workers*.)
- **Expected speedup [measured range]:** removes ~3–9s/test of DNS-retry drain on
  affected teardowns. If broadly present, potentially **rivals P0**; if localized to
  first-test-per-worker, smaller. **Size before P0 rollout** — it may be the cheaper
  headline.
- **Effort:** low (fixture ordering / one env guard). **Risk:** low.

### P2 — CI-vs-local mutual exclusion (biggest RELIABILITY lever)
- **What:** extend the TEST_LOCK discipline to CI, or gate CI so its 5 chunks + any
  local run cannot co-run on the same box. Options: (i) CI acquires the same flock;
  (ii) reduce CI matrix concurrency (run chunks 2-at-a-time) so total surreal procs
  stay under core/RAM budget; (iii) move CI to a dedicated runner.
- **Expected win:** eliminates the cross-run cascade (the 1847-err class) → removes
  the `--reruns` tax and the false-failure triage cost. **Not a wall-clock win on a
  quiet box; a reliability + CI-flakiness win** `[estimate, prior anchors]`.
- **Effort:** medium (CI/infra). **Risk:** low-medium. Dual-purpose with ADR-0022
  daemon-freeze safety (protects the live daemon from test-box thrash).

### P3 — Smart pre-push e2e gate (already mostly done — tighten)
- **What:** the `files:` filter already skips docs-only pushes. Tighten to
  `BEHAVIOR_CONTRACT.md` + `yadgar/tests/e2e/` + touched-subsystem source only, so a
  narrow code change doesn't trigger the full 20min e2e.
- **Expected win:** removes ~20min from pushes that don't touch contract surface
  `[estimate]`. **Effort:** low. **Risk:** low-medium (a missed contract change
  reaches CI instead of pre-push — acceptable, CI still gates).

### P4 — Shard CI across *separate* runners (not same box)
- **What:** the 5-way split already exists; the problem is co-location. Move chunks
  to distinct runners (or serialize on one) to remove intra-CI contention.
- **Expected win:** reliability + possibly wall-clock if the box is the bottleneck
  `[estimate]`. **Effort:** medium-high (infra). **Risk:** medium.

### Deprioritized (measured to be low-value here)
- **Tune `-n` / cap runner:** flat scaling → **~0 wall-clock win** `[measured]`.
  Keep the RAM-cap for daemon-safety, but do not expect speedup. (Dual-purpose only
  with ADR-0022.)
- **Mock-embeddings-by-default:** model is already class-cached; per-worker load is
  ~2.9s once, negligible vs the 436s floor `[measured]`. Low value **for speed**
  (may still be worth it for determinism — separate motivation).
- **Shared embed-server (`YADGAR_EMBED_URL`):** saves ~2.9s × workers one-time
  (~29s) — negligible `[measured]`. Skip for speed.
- **HF cache in CI:** non-issue (baked in) `[code-read]`. Skip.

---

## 6. Recommended sequence

1. **P1 (OTLP)** first — cheapest, low-risk, and its measured size (3–9s/test) tells
   you how much P0 still needs to carry. Measure the delta on the standard slice.
2. **P0 pilot** — module-scope the 5 slowest files, measure per-file, verify full
   suite green. If the win lands and risk is containable, roll out in batches.
3. **P2 (CI mutual exclusion)** — kills the flake/rerun tax + false-failure triage.
   Independent of P0/P1; can proceed in parallel (infra track).
4. **P3 (e2e gate tightening)** — quick pre-push win.
5. **P4 (runner separation)** — infra, do last / as capacity allows.

Re-run the standard 257-test slice sweep after each of P0/P1 to attribute the win.

---

## 7. Open questions

- **P0 blast radius:** how many of the 91 files actually *tolerate* module-scoped
  engines? Needs a per-file audit — some are function-scoped for a reason
  (`server.shutdown()` mid-file, per-test singleton reset). Pilot answers this.
- **OTLP size:** is the 3–9s drain per-test everywhere, or only first-test-per-
  worker? Determines whether P1 rivals or trails P0. Measure explicitly.
- **CI rerun tax, re-measured:** pull actual RERUN counts + ConnectError rates from
  recent CI logs (`benchmarks/reports/`, forgejo run artifacts) to size P2 with real
  numbers instead of the v5.58 anchor `[estimate]`.
- **Does P0 change the flat-scaling curve?** Once per-test init is gone, `-n` may
  start to matter again — re-sweep to see if `-n` becomes a lever post-P0.
