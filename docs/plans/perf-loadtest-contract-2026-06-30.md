# Perf Load-Test Contract + Per-PR Regression Tracking — Plan

**Status:** PLAN ONLY (no implementation). **Date:** 2026-06-30. **Author:** agent (bot).
**Scope:** A repeatable end-to-end LOAD test for the yadgar MCP daemon that measures
performance against prod-like data on the user's local machine, saves the measurement
as a per-PR doc, and flags degradation over time.

---

## TL;DR

- **Feasibility: YES.** A pure-Python load harness is buildable on the *existing*
  benchmark + e2e infrastructure. No new heavyweight deps.
- **Recommended tool: Python-in-repo, NOT k6, NOT Allure.** The harness reuses the
  proven `test_offload_e2e.py` MCP-over-HTTP driver + the `run_longmemeval.py`
  report-saving pattern. k6 would need to re-implement the `/mcp` JSON-RPC+SSE
  envelope and add a Go dependency for zero benefit. Allure is a test-report UI —
  wrong layer entirely.
- **Snapshot mechanism: physical `cp -r` of a QUIESCED prod datadir** (the artifact
  the nightly backup already produces), spawned as a throwaway SurrealDB on a free
  port, driven by a subprocess daemon over HTTP. Logical `.surql` export is NOT
  usable — `GET /export` stack-overflows on the real dataset.
- **Top risk: local-machine measurement noise** (multi-agent pytest contention is a
  documented false-regression source). Mitigated by record-only-first, multiple runs
  + medians, and a ~10–15% noise floor before flagging.
- **Effort: ~2–3 focused days** to a record-only baseline harness; +1 day to add the
  baseline-diff + gate logic.

---

## PART 1 — FEASIBILITY (verified against the repo)

### 1.1 The existing e2e harness — can it host a restored prod DB?

**The `make e2e` target** (`Makefile:286-294`):
```
uv run --extra test --extra ml python -m pytest yadgar/tests/e2e/ -m e2e \
  -p no:randomly -n0 --reruns 2 --reruns-delay 2 --tb=short -q
```
Serialized under `flock -w 900`, pre-cleaned by `scripts/reap-test-surreal.sh`,
excluded from CI (`-m 'not e2e'`). Requires `~/.local/bin/surreal` on PATH.

**`scripts/reap-test-surreal.sh:1-17`** filters by process name (`comm == "surreal"`)
AND data path (`/tmp/pytest`) — so it never touches the production daemon (prod
surrealkv data lives under `/data`, not `/tmp/pytest`). Safe to reuse for cleanup.

**The session `surreal_server` fixture (`yadgar/tests/conftest.py:278-313`) is HOSTILE
to a restored DB** — three interlocking mechanisms make pre-seeded data invisible:
1. **Always starts empty** — `mktemp`'d fresh datadir (`conftest.py:294`).
2. **Namespace redirect** — `_isolate_surrealdb` (`conftest.py:451-488`) patches
   `StorageEngine._init_schema` so every *in-process* test queries a per-path-hash
   namespace `t{md5(db_path)}`, never prod's `ns=yadgar/db=main`.
3. **Auto-wipe** — `_wipe_surrealdb_data` (`conftest.py:672-755`) wipes `main` after
   every test.

**Conclusion:** the new perf harness MUST NOT use the in-process session fixture. It
uses the **subprocess-daemon-over-HTTP** pattern instead (§1.3) wired to a fresh
SurrealDB spawned over a *copied* prod datadir (the `dedicated_backend` pattern in
`yadgar/tests/e2e/test_vacuum_backup_safety.py:857-940`). A fresh daemon over a copied
datadir reads prod data cleanly: `_init_schema` (`yadgar/storage/migrations.py:1171-1320`)
defaults to `ns=yadgar/db=main` over an HTTP URL and is idempotent (`IF NOT EXISTS`
guards), so it skips table creation and only replays missing migrations/indexes.

### 1.2 The existing benchmark/report precedent — REUSE, don't reinvent

**Runners in `benchmarks/`:**
- `run_longmemeval.py` — `run_benchmark()` (`:813`); writes the report at `:1231-1232`
  via `json.dump(results, f, indent=2, default=str)`; output path
  `benchmarks/results/longmemeval_<variant>_<mode>_<YYYYMMDD_HHMMSS>.json` (`:882-887`).
- `run_eval.py` — `run_eval()` (`:600`); `write_json_report()` (`:566-594`); default
  output `benchmarks/reports/eval_<YYYYMMDD_HHMMSS>.json` (`:735-736`).

**Report JSON shape to mirror** (`run_longmemeval.py:907-921`): top-level `benchmark`,
`timestamp`, run config, `per_query` (list), `aggregated` (dict), `reproducibility`
(`yadgar_commit`, `surreal_version`, `embedding_model`, `python_version`, `run_date_utc`),
`elapsed_seconds`.

**Aggregation/percentile precedent already exists** — `run_eval.py:449-509`
`aggregate_metrics()` computes `latency_{p50,p95,mean}_ms` via `statistics.median()` +
`statistics.quantiles()` (`:472-477`). Reuse this directly.

**Version stamping** — `yadgar/__init__.py`: `__version__` resolved via
`importlib.metadata.version("yadgar")` (`:4`, falls back to `pyproject.toml`);
`BACKEND_VERSION` (`:21`). Runners use `datetime.now(UTC).isoformat()`.

**Microbench latency precedent** — `yadgar/tests/test_wiki_mcp_handler_perf.py:1-195`:
`time.perf_counter()`, 100 iters + 5 warm-up, p50 via `statistics.median()`, p90 by
sort. This is the latency-measurement template.

### 1.3 Speaking MCP from Python — PROVEN in-repo

`yadgar/tests/e2e/test_offload_e2e.py` already drives real concurrent recalls over HTTP.

**`_Daemon` class (`:110-175`):** spawns `sys.executable -m yadgar --transport
streamable-http --port <port>` as a subprocess on a free port (`_free_port()`), waits
on `/health`, passes env (`YADGAR_DB_URL`, `YADGAR_DB_USER/PASS`, `YADGAR_EMBED_URL`,
`YADGAR_DATA_DIR`, `YADGAR_DB_PATH`, `YADGAR_REQUIRE_AUTH=0`, `YADGAR_TEST_TOOLS=1`,
`YADGAR_PROFILE=minimal`). Teardown SIGTERM→wait(10)→SIGKILL.

**`_call_tool` JSON-RPC envelope (`:77-107`):**
```python
POST http://127.0.0.1:{port}/mcp
Accept: application/json, text/event-stream
{"jsonrpc":"2.0","id":req_id,"method":"tools/call",
 "params":{"name":name,"arguments":arguments}}
# response is an SSE stream — parse "data:" lines as JSON
```

**Concurrency (`:208-234`):** N `threading.Thread` targets each call `_call_tool`
independently; poll `/health` in-flight to measure latency/starvation under load. This
is the exact backpressure/`/health/live`-latency probe the design needs (§2.2).

**Because this is a subprocess driven over HTTP, it bypasses all the autouse fixtures**
(namespace redirect, main-wipe) — which is precisely why it can point at a restored
datadir that the in-process fixture cannot.

### 1.4 The prod-snapshot fixture — THE CRUX (resolved)

**Storage backend = surrealkv (RocksDB-based, embedded)** — `entrypoint-backend.sh:59-65`
starts `surreal start ... surrealkv:///data/surreal_db`. Data path `/data/surreal_db`
inside the container = `~/.local/share/yadgar/surreal_db` on host.

**surrealkv holds an EXCLUSIVE on-disk lock.** A live `cp -r` of the *running* prod
datadir captures torn segments — proven by the 2026-06-16 bug (1484 of 3622 records
partially lost on a hot copytree). So the copy MUST be of a **quiesced** source.

**Two snapshot artifact kinds** (`yadgar/backup.py:12-28, 61-134`):
1. **Logical export** (`GET /export` → `.surql`) — transactionally consistent BUT
   **stack-overflows on the real dataset** (the recursive value serializer blows the
   ~2 MiB tokio stack; `entrypoint-backend.sh:4-11` explicitly warns "no in-container
   backup loop"). **NOT usable for a full snapshot.** (Wiki-only logical export works
   because `wiki_page` is bounded — irrelevant here.)
2. **Quiesced copytree** (`shutil.copytree`, backend stopped) — the production-safe
   path. Nightly cycle v5.69 P5 (`backup.py:22-25`) **stops both services before
   snapshotting**, so the nightly artifact is consistent by construction.

**`restore.sh` (`scripts/install/restore.sh:115-160`)** is an advanced `.surql`
import flow (`--namespace yadgar --database main`), not the canonical path — listed for
reference only.

**Verdict:** the snapshot is a **physical copy of a quiesced datadir**, reused as a
*frozen pin* (§2.4). At bench-run time the harness does `cp -r` from the **static
pin** (no lock holder) into a throwaway tmpdir, spawns a fresh `surreal` on a free
port over the copy, and points a subprocess daemon at it. **Live prod is never touched
at run time** — the only disruption is the rare, deliberate pinning event (or simply
reusing the nightly backup artifact, which costs nothing extra).

### 1.5 Tool verdict — Allure NO, k6 NO, Python YES (confirmed)

- **Allure** is a test-result *reporting UI* (HTML dashboards over JUnit/pytest XML).
  It does not generate load and does not speak `/mcp`. Wrong layer. **No.**
- **k6** is a capable load generator, but it speaks HTTP/gRPC/WebSocket — it would have
  to re-implement the `/mcp` JSON-RPC+SSE envelope (`_call_tool` above) in JS, and adds
  a Go-binary dependency to a Python repo. The repo *already proves* concurrent
  `/mcp` load from Python threads. k6 buys nothing. **No** (optional only if a future
  need for distributed multi-machine load arises — not the case here).
- **Python-in-repo** reuses the `_Daemon`/`_call_tool` driver + `aggregate_metrics()`
  percentile helper + `run_longmemeval.py` report writer. Lowest new surface, prod-like
  by construction. **Yes — recommended.**

---

## PART 2 — DESIGN

### 2.1 The workload contract

A fixed mix that exercises the read hot-path, the concurrency that broke the offload,
and the write→drain path. Counts are the v1 proposal (tune after the first baseline).

| Phase | Op | Count | Concurrency | Purpose |
|---|---|---|---|---|
| W0 warm-up | `recall` | 10 | 1 | JIT/cache warm; discarded from stats |
| A | `recall` (varied queries) | 100 | 1 (sequential) | clean p50/p95/p99 read latency |
| B | `recall` | 50 | **8 concurrent** | the offload/backpressure regime |
| C | `wiki_query` | 30 | 1 | secondary read path |
| D | `memorize` → drain | 20 | 1, then await drain | write-drainer throughput |
| E | `/health/live` probe | continuous | during B | latency-under-load (starvation) |

- Queries drawn from a **fixed, committed query list** (small `.jsonl` of ~30 realistic
  recall/wiki prompts) so the workload is identical across PRs. The list IS committed
  (it's tiny prompts, not data); the DB snapshot is NOT (§2.4).
- Phase B concurrency (8) deliberately matches the regime that exposed the offload
  serialization bug, so the bench guards that fix.
- Each full contract run is repeated **N=5 times**; report medians (see noise, §2.7).

### 2.2 Metrics

Captured per phase, all latencies in ms via `time.perf_counter()`:

- **recall latency p50 / p95 / p99** (Phase A sequential, and separately Phase B
  concurrent — concurrent p95 is the load signal).
- **throughput** — recalls/sec sustained in Phase B.
- **`/health/live` latency p50/p99 under concurrent load** (Phase E) — the
  starvation/backpressure indicator; reuses the `/health` poll-in-flight pattern from
  `test_offload_e2e.py:208-234`.
- **rerank-gate backpressure** — observe whether concurrent recall p99 degrades
  super-linearly vs sequential (gate queuing).
- **write-drainer throughput** — Phase D: memorize-accepted/sec and time-to-drain
  (queue depth → 0).
- **drop / error rate** — non-200 `/mcp` responses, JSON-RPC errors, timeouts, per
  1000 calls.

> **In-band embedding latency caveat (measurement validity):** `recall` latency
> *includes* the embedding RPC (`YADGAR_EMBED_URL` is passed to the daemon; historical
> ~2ms p50 / ~50ms p95). The embed backend is therefore a bench dependency. Use the
> **real** embed service (mocking it makes numbers non-prod-like), and record its
> identity in the report — but note it's a shared resource whose own load can pollute
> recall percentiles. This is a stated limit, not a defect.

### 2.3 Thresholds / gate (phased)

**Phase 1 — RECORD ONLY (ship this first).** No gate. Run ≥3 times across a few PRs to
establish a stable baseline + observed noise band. Rationale: anchored lesson
(memory **518987**) — multi-agent pytest contention produced 14–47 *false* "regressions"
that all passed on solo rerun. Gating before a noise floor is known would be
noise-dominated.

**Phase 2 — ADD GATES (after baseline is stable).** Example gates (numbers set from the
baseline, not guessed):
- recall p95 (sequential) < `X` ms (X = baseline p95 × 1.15).
- `/health/live` p99 < 8 s @ 8 concurrent recalls.
- 0 crash-loops / 0 daemon restarts during a run.
- error rate < 0.5%.
- Regression flag when a metric exceeds **baseline + max(15%, noise-band)** across the
  median of N runs.

### 2.4 The snapshot fixture

- **Pin ONE frozen snapshot** for cross-PR comparability. Source = a copy of the
  **nightly backup artifact** (already quiesced — zero extra disruption) OR a one-time
  deliberate `stop prod → cp -r ~/.local/share/yadgar/surreal_db → restart`.
- **Location:** a fixed local path NOT in git, e.g.
  `~/.local/share/yadgar/perf-snapshots/pin-<id>/surreal_db/`. The harness reads the
  path from an env var (e.g. `YADGAR_PERF_SNAPSHOT_DIR`) with a clear skip-with-reason
  if unset. **Do NOT commit a multi-GB DB** — it's the user's own data, no
  anonymization needed, but it stays out of the repo.
- **Per-run isolation:** harness `cp -r` from the static pin into a throwaway tmpdir,
  spawns surreal on a free port over the copy. Pin is read-only; live prod untouched.
- **Refresh policy:** refreshing the pin is a **deliberate re-baselining event** (it
  invalidates cross-PR comparability with prior reports). Recommend refresh quarterly
  or when the dataset shape changes materially; record the new `snapshot_id` and reset
  the baseline.
- **Snapshot identity in the report** (`snapshot_id` = hash or pin label) so a
  baseline-diff cannot silently compare against a refreshed pin and report fake deltas.

### 2.5 Report format + baseline-diff

**Per-PR artifacts:**
- `docs/benchmarks/perf_<ver>_<YYYY-MM-DD>.md` — human-readable: the metric table, the
  delta vs baseline, pass/fail (Phase 2), snapshot_id, env caveats.
- `benchmarks/reports/perf_<ver>_<YYYY-MM-DD>.json` — machine-readable, mirroring the
  `run_longmemeval.py` schema:
  ```jsonc
  {
    "benchmark": "perf-loadtest",
    "timestamp": "...",
    "workload": { "...contract counts/concurrency..." },
    "runs": [ /* N raw runs */ ],
    "aggregated": { "recall_p50_ms": ..., "recall_p95_ms": ...,
                    "recall_concurrent_p95_ms": ..., "health_live_p99_ms": ...,
                    "throughput_rps": ..., "drainer_rps": ..., "error_rate": ... },
    "baseline_diff": { "<metric>": {"baseline":..., "current":..., "delta_pct":...,
                                    "flagged": false} },
    "reproducibility": { "yadgar_commit": ..., "snapshot_id": ...,
                         "surreal_version": ..., "embedding_model": ...,
                         "python_version": ..., "run_date_utc": ... }
  }
  ```
- Report writer reuses `json.dump(results, f, indent=2, default=str)` and the
  `aggregate_metrics()` percentile helper.

**Baseline storage + comparison:**
- A pinned `benchmarks/reports/perf_baseline.json` (committed — it's small numbers, no
  data) holds the current accepted baseline aggregate + its `snapshot_id`.
- The harness loads `perf_baseline.json`, computes `delta_pct` per metric, writes
  `baseline_diff`, and (Phase 2) flags on threshold.
- Promoting a run to baseline is a manual, deliberate commit (re-baseline event).
- **Hard guard:** if `current.snapshot_id != baseline.snapshot_id` OR
  `embedding_model` differs → emit a warning and DO NOT flag deltas as regressions
  (the comparison is invalid; see §2.7).

### 2.6 CI wiring

- **Manual / opt-in, NOT every-PR-gating.** CI is **forgejo** (`.forgejo/workflows/`).
  Mirror `eval.yaml` (a `workflow_dispatch`, non-gating job) — add
  `.forgejo/workflows/perf.yaml` triggered by `workflow_dispatch`. The PR-gating
  `ci-pr.yaml` (`make test-ci`) is NOT touched.
- A `make perf` Makefile target (mirroring `make eval`) is the primary local entry
  point. The CI job is a convenience wrapper around it.
- **Why not every-PR:** local-machine + shared-runner noise (§2.7). The artifact is
  *saved per PR* (the user runs `make perf` and commits the `docs/benchmarks/*.md`),
  but the *run* is deliberate, not an automatic blocking gate. This matches
  record-only-first.
- Each invocation runs the contract N=5× and reports medians; treat <~10–15% swings as
  noise, not signal.

### 2.7 Honest limits

- **Local-machine noise** (memory **518987**): concurrent pytest/agent sessions sharing
  `/tmp/pytest-of-max/` + the SurrealDB random-port pool produced 14–47 false
  "regressions" that all passed on solo rerun. Mitigation: N runs + medians, a
  ~10–15% noise floor, and a "run perf with no other pytest/agent load" operator note.
  The bench is **good at catching big regressions, noisy for small ones**.
- **MCP transport flakiness** (socket reuse / DNS race, same anchor): a flaky run can
  pollute results. Mitigation: per-run retry/quarantine (drop a run whose error rate >
  threshold and re-run it), surface error_rate prominently.
- **Snapshot staleness tradeoff:** a frozen pin is realistic *and* comparable, but
  drifts from current prod over time. Refreshing restores realism but breaks cross-PR
  comparability (re-baseline). Accept the tradeoff; refresh deliberately.
- **In-band embedding dependency** (§2.2): recall percentiles include the real embed
  RPC; the embed service's own load is an uncontrolled variable.
- **Embedding-model-change caveat:** if a PR changes the embedding model, the pin's
  stored vectors are stale (recall skew, possible `reembed_stale` trigger). Same model
  across PRs = comparable; a model change requires re-embedding the pin (or the
  comparison is invalid — guarded in §2.5).
- **The e2e seed pattern is the reproducibility-friendly complement** (memory
  **529344**): for a fully deterministic, machine-independent variant, the
  LongMemEval self-seed / behavioral-e2e seed approach (seed synthetic prod-*scale*
  data into the throwaway DB) can be a fallback when no pin is available — but the
  pinned real snapshot stays primary per the user's explicit "prod-like data" want.

---

## Build / no-build recommendation

**BUILD IT.** It's feasible on existing infra, the tool choice is clear (Python, not
k6/Allure), and it fills a real gap: there is currently no load/latency regression
guard for the concurrent recall path that the offload work touches. Ship **record-only
first** to earn a baseline, then add gates.

**Effort estimate:**
- ~2–3 focused days → record-only harness: the `cp -r`-pin fixture, the `_Daemon`/
  `_call_tool` reuse, the workload contract, the report writer + `make perf` target,
  the `.forgejo/workflows/perf.yaml`.
- +~1 day → `baseline_diff` + `perf_baseline.json` + snapshot/embedding-model guards.
- +~0.5 day → Phase 2 gates once a baseline is stable.

Test-driven: write the harness's metric-aggregation + baseline-diff logic against a
tiny synthetic DB first (red→green), THEN point it at the real pin.

---

## Advisor input (incorporated)

- Hold the feasibility verdict until the snapshot-lock fact landed — it's the
  discriminator. (Done: surrealkv exclusive lock → quiesced copy only.)
- Snapshot is a one-time *pinning* event, not a per-run quiesce; reuse the nightly
  backup artifact → "runs locally against prod-like data" with prod undisturbed at run
  time. (§2.4)
- Three folded-in blind spots: in-band embed latency is part of measured recall
  (§2.2); `snapshot_id` must be in the report to prevent fake deltas (§2.5);
  embedding-model change invalidates the pin's vectors (§2.7).
- `/health` poll-under-load IS the backpressure metric — wire it, don't re-investigate
  internals. (§2.2)
- Record-only-first + median-of-N + 10–15% noise floor are justified by earned scar
  tissue (memory 518987), not generic caveats. (§2.3, §2.7)

---

## Key file references

| Concern | File:line |
|---|---|
| `make e2e` target | `Makefile:286-294` |
| test-surreal reaper (prod-safe) | `scripts/reap-test-surreal.sh:1-17` |
| session fixture (do NOT use) | `yadgar/tests/conftest.py:278-313, 451-488, 672-755` |
| MCP-over-HTTP driver (reuse) | `yadgar/tests/e2e/test_offload_e2e.py:77-107, 110-175, 208-234` |
| copied-datadir backend (reuse) | `yadgar/tests/e2e/test_vacuum_backup_safety.py:857-940` |
| `_init_schema` idempotent restore | `yadgar/storage/migrations.py:1171-1320` |
| prod storage = surrealkv | `entrypoint-backend.sh:59-65` |
| backup artifacts (logical/copytree) | `yadgar/backup.py:12-28, 61-134` |
| logical export stack-overflow warn | `entrypoint-backend.sh:4-11` |
| nightly quiesced snapshot | `yadgar/scripts/nightly_cycle.py:273-296, 372-396` |
| report writer (reuse) | `benchmarks/run_longmemeval.py:1231-1232` |
| percentile aggregator (reuse) | `benchmarks/run_eval.py:449-509` |
| latency microbench template | `yadgar/tests/test_wiki_mcp_handler_perf.py:1-195` |
| version stamping | `yadgar/__init__.py:4, 21` |
| eval CI (forgejo, opt-in) | `.forgejo/workflows/eval.yaml`; PR-gate `.forgejo/workflows/ci-pr.yaml` |
