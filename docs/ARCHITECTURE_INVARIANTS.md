# Yadgar Architectural Invariants

Authoritative source: this file (`docs/ARCHITECTURE_INVARIANTS.md`).
Mirrored in wiki: `yadgar-architectural-invariants`.
Anchored memory: project-scoped, `/home/max/git/yadgar`.
Version-execution-order lives in the `yadgar-roadmap-future-improvements` wiki.

Last updated: 2026-05-22 (V1a backend /metrics shipped — v5.5.0; V1b/V1c/V1d unblocked).

---

## Purpose

Any planning for yadgar (vX.Y feature scope, refactor proposal, hotfix) MUST satisfy every invariant below. A plan that violates one is rejected and re-scoped. Override path: edit this file + propose a migration. No silent overrides.

Created because v5.1 module decomposition (commit `7c29a33`, 2026-05-17) silently moved drainer-deferred work into the memorize request path, and v5.3.4 (`263bfa3`) added more inline sync I/O. Result: writes feel non-async despite a working queue. Invariants below codify the lessons.

Later triggered by 2026-05-20 backend OOM cascade — surfaced systemd `BindsTo=` coupling as a hidden cascade-failure mode. v5.3.9 hotfix.

---

## Invariants

### I1. Request path is THIN

MCP tool handlers return in O(10ms) p99. Allowed: validation, secrets gate, WriteGate, `FileQueue.enqueue` + ack. NOT allowed: encode, vector search, LLM, KG extraction, multi-hop traversal, curator, engram, astrocyte, prospective, thermo DB writes (beyond inline atomic counters), reinjection.

### I2. Drainer is the SINGLE catch-up lane

`QueueDrainer._drain_once` owns LEAN fan-out: `insert_memory`, `encode_document_enriched`, `insert_vector`, `extract_entities_typed`, `archive`. NOT drainer's job: curator, engram, astrocyte, reinjection, conflict resolver. Those run on a SEPARATE deferred pass (`ConsolidationScheduler`). Drainer must not compete with request-path reads for the DB connection pool.

### I3. Opt-in features short-circuit BEFORE expensive setup

Env-gated feature checks happen in O(1) BEFORE module import, client construction, or DB query. Off = zero overhead.

### I4. ML compute is `asyncio.to_thread` or drainer-thread ONLY

`SentenceTransformer.encode` blocks the event loop. Async contexts must use `await asyncio.to_thread(...)`. Drainer thread may call directly. Never inline in a coroutine.

### I5. Module decomposition NEVER moves work across boundaries

Splitting a module must preserve sync/async/queue topology of every call. Decomposition is structural; it must not change WHEN or WHERE work runs. v5.1 decomp violated this — banned pattern. Future refactors prove no topology regression by listing every moved call.

### I6. No double-pay

Heavy ops run once per write. Inline fallback + drainer replay must not both run curator/engram/astrocyte. Use idempotency markers (e.g. `consolidation_state` field).

### I7. Queue is the durability boundary

`FileQueue` atomic-rename = durability contract. Sync fallback must enqueue FIRST, then process. Never process-then-enqueue. **Verified 2026-05-20 crash: zero new DLQ entries from crash window.**

### I8. Backpressure must be observable

`/metrics` + `memory_stats` MUST surface queue_depth, drainer_lag_ms, dlq_size, drain_cycle_duration_ms. Alerts in `docs/configuration.md`.

### I9. New write-path code budget ≤5ms p50

Hard latency budget. Exceeds → moves to drainer or consolidation.

### I10. Overrides are explicit

Override path: edit this file with override + reasoning + migration, reference commit in PR, get user approval. No silent drift.

### I11. Heavy stable artifacts live in backend, not core

ML models, datasets, large reference data (multi-hundred-MB, monthly-or-slower cadence) belong in backend image or runtime-mounted volume. NEVER bake into core image. Check `docker history docker.io/openfantasy/yadgar:VER`. Backend currently 6.78GB (v5.4 F0 scope). **Note 2026-05-20 crash:** backend image bloat is real but NOT the OOM cause — OOM was load-induced spike during /rerank (768MB idle baseline). F0 + F5-fix are separate work.

**P9 ratchet (v5.4.2):** `scripts/check_image_size.py` enforces I11 size caps on every backend dep bump. Run after `podman build` via `pre-commit run check-image-size-backend --hook-stage manual`. Caps: backend ≤2.0 GB, core ≤0.8 GB. See P9 section below.

### I12. Measure before optimize

Any perf claim, cache, threadpool, batching, async refactor MUST be preceded by stage-level profiling p50/p95/p99 data. PR artifacts: profile output / `/metrics` histogram showing hot stage, before/after numbers, regression test. Paired with I8. Validated 2026-05-20: F5 was nearly scoped as "F0 fixes OOM" without data; docker stats baseline disproved that assumption.

### I13. Bounded file + function complexity

Hard + soft caps so diffs stay reviewable AND decomposition doesn't drift into I5 violations.

**Function caps:** cyclomatic ≤15 hard / ≤10 soft; LOC ≤150 hard / ≤80 soft; params ≤8 hard / ≤5 soft; nesting ≤4 hard.

**File caps:** LOC ≤1000 hard / ≤500 soft; public symbols ≤30 soft.

**Class caps:** methods ≤30 soft; instance attrs ≤15 soft; inheritance depth ≤3 hard.

**Test files exempt** from LOC + params. Cyclomatic + nesting still enforced.

**Justified-cohesion override** (soft caps only): single cohesive flow + decomposition would create shared mutable state / cross thread-async boundary / lose error-handling context + documented inline `# noqa: C901 – cohesive: <reason>`.

Hard caps NO override. If hit, decomposition design must prove I5 preservation.

**Critical anti-pattern (per v5.1 incident):** decomposition creating implicit shared state OR moving work across thread/async boundaries is WORSE than the mega-function. Decomposing without preserving topology = banned.

**Enforcement:** pre-commit `ruff check --select=C901 --max-complexity=15` (cyclomatic hard cap) + `PLR0913 max-args=8` (params hard cap); custom `check-complexity` hook covers LOC / nesting / file-LOC / public-symbols / class-methods / class-attrs / inheritance-depth; soft warns (exit 0), hard blocks (exit 1). Existing violations in `docs/complexity-audit.md` (P12). Baseline file: `.complexity-baseline.json` (4819 entries) records pre-existing violations so the hook only blocks NEW or WORSENED violations. Regenerate: `python scripts/check_complexity.py --update-baseline --all-files`.

**v5.4.3 grandfathering (b27d218 gap):** `pyproject.toml` `[tool.ruff.lint.per-file-ignores]` lists 31 pre-existing C901/PLR0913 violators that existed before b27d218 enabled ruff selectors without a grandfathering pass. Refactor target: v5.4.4. DO NOT add new entries — fix violations in new code.

**v5.4.6 update — LOW-risk batch (P12 catalog):** 4 functions decomposed from LOW-risk subset. 2 files dropped from per-file-ignores (`curation/ingestion.py`, `storage/entity.py`); 1 partial (`restoration.py` PLR0913 removed, C901 remains). Remaining 29 per-file-ignore entries are scoped to MEDIUM/HIGH cyclomatic violations or MEDIUM PLR0913 (`curate_on_remember`). Dataclass pattern used for PLR0913 violations: `RelationshipMeta`, `NewMemorySpec`, `CheckpointContext`. Dispatch-dict pattern used for nesting violations: `cmd_config`. All helpers comply with I13 caps. Baseline regenerated (4667 → 4819 entries reflecting refactored + new test functions).

**Decision log — 2026-05-22 — v5.4.6 LOW-risk refactor batch:** 4 functions / 2 files fully removed from per-file-ignores / 1 partial removal. No topology drift per I5 — all helpers remain in same module, same sync/async context. MEDIUM and HIGH violations deferred (topology proof required per I5 or metrics gate per I12).

### I14. Structured logging contract (SCOPED)

Every log entry = JSON with `ts, level, component, action, outcome, latency_ms?, error?`. NEVER log memory `content` (PII risk), tokens/passwords, user-supplied strings as metric labels. Log at boundaries (request in/out, drainer cycle, DB op), NOT inside hot loops. **trace_id propagation across MCP → core → drainer → backend is a SEPARATE v5.5 P-item, NOT in scope of I14.** Ratchet: new code conforms; old conforms when touched; full conformance by v5.6.

**Shipped v5.4.2 prep:**
- `yadgar/log_config.py`: `JSONLogFormatter` (I14 schema: `ts/level/event/component/action/outcome`), `ContentRedactor` (logging.Filter; strips sensitive fields by substring denylist + one-level dict redaction), `TRACEBACK_MAX_CHARS=2000` constant. `configure_logging()` updated: default format changed `human→json`, idempotent handler install, redactor wired as handler filter.
- `yadgar/config.py`: `LOG_FORMAT: str = "json"` setting with `field_validator` enforcing `{"json","text","human"}`. Env var: `YADGAR_LOG_FORMAT`.
- Env-gated: `YADGAR_LOG_FORMAT=text` for local dev human-readable output.
- Integration sites converted (ratchet): `ml_client._CircuitBreaker._open`, `embed_service.rerank` semaphore-busy 503, `file_queue.QueueDrainer._drain_once` cycle-end log, `server.tools.memorize.memorize` enqueue-fallback log.
- `yadgar/embed_service.py`: `lifespan` now calls `configure_logging()` at backend boot.
- Tests: `yadgar/tests/test_structured_logging.py` — 18 tests covering all I14 contracts.
- Ratchet status: 4 call sites converted; full conformance target unchanged (v5.6). Known non-conformance: `RequestLoggingMiddleware` (pre-existing, not touched this round). **RESOLVED v5.4.7.**
- Known sharp edge: denylist uses substring match — `content_type`/`content_length` are redacted. Revisit at v5.6 conformance round. **RESOLVED v5.4.7.**

**v5.4.3 update — framework-logger coverage extended:**
- Root-logger approach: `configure_logging()` now attaches `JSONLogFormatter` + `ContentRedactor` handler to the **root** logger (not just `yadgar`). All child loggers — `uvicorn`, `uvicorn.access`, `uvicorn.error`, `mcp`, `fastmcp`, `httpx`, `starlette`, and any future framework additions — propagate to root automatically.
- `yadgar` logger changed: `propagate=True` (was `False`), own handlers cleared; root handler covers output.
- Two helper functions extracted: `_configure_yadgar_logger()`, `_suppress_noisy_framework_loggers()`.
- Noisy namespaces capped at WARNING: `uvicorn.access`, `httpx`, `httpcore`, `asyncio` (suppress DEBUG/INFO chatter; WARNING+ still emits JSON).
- `YADGAR_LOG_FORMAT=text` / `=human` still disables JSON everywhere (root gets text formatter instead).
- Tests: `TestFrameworkLoggerCoverage` (8 tests) added to `test_structured_logging.py` — covers root handler install, uvicorn.access JSON emission, fastmcp JSON emission, yadgar propagation, human/text fallback, idempotency, redactor on root handler.

**v5.4.7 update — I14 ratchet cleanup (shipped):**
- `RequestLoggingMiddleware` migrated to I14 schema: emits `component="http_server"`, `action="request"`, `outcome` (via `_outcome_from_status`), `latency_ms` (renamed from `duration_ms`), `http_status` (renamed from `status`). Fields `request_id`, `tool_name`, `trace_id` retained. **BREAKING: dashboards reading `duration_ms` must update to `latency_ms`.** See `MIGRATION_NOTES.md`.
- `ContentRedactor` denylist tightened: two-tier exact/substring model replaces flat substring match. `_EXACT_DENYLIST` = `{content, auth, token, secret, bearer}` (exact-only). `_SUBSTRING_DENYLIST` = `{password, api_key, authorization, access_token, refresh_token, client_secret, private_key}` (substring). `content_type`/`content_length` false-positive fixed.
- `_outcome_from_status(status: str) -> str` helper added: `"cancelled"→"degraded"`, `2xx/3xx→"ok"`, all other (4xx/5xx/"0"/unknown)→`"error"`. Cyclo=3, LOC=8 — well within I13 caps.
- Tests: 40 new tests across `TestContentRedactorDenylistV547`, `TestOutcomeFromStatus`, `TestRequestLoggingMiddlewareI14`. Total test file: 66 tests.
- **v5.6 follow-ups RESOLVED:** `RequestLoggingMiddleware` non-conformance and `content_type`/`content_length` false-positive both closed in this PR.

**v5.4.8 update — request-log visibility fix:**
- Root cause: `CORE_LOG_LEVEL` defaults to `"warn"` → `configure_logging(level="WARNING")` set root + root handler + `yadgar` logger all to WARNING → `yadgar.requests` inherited WARNING → INFO records silently dropped. Env var `YADGAR_LOG_LEVEL` (from bug report) is NOT a valid Settings field; the correct var is `YADGAR_CORE_LOG_LEVEL`. Neither deployment file (`docker-compose.yml`, `yadgar.service`) set it → default "warn" was always active.
- Fix: `_configure_request_logger(formatter)` — dedicated `StreamHandler` at `INFO` attached directly to `yadgar.requests` with `propagate=False`. Request telemetry now flows regardless of root log level. Idempotent: existing handler updated, not stacked.
- Invariant: `yadgar.requests` MUST always have its own handler at `INFO` after `configure_logging()`. Root log level is irrelevant to request observability.
- Tests: `TestRequestLogVisibilityAtWarningLevel` (3 tests) — `level="WARNING"` root proves INFO still flows; suppression list excludes `yadgar.requests`; idempotency guard on re-configure.
- **Operator action required:** production deployment must set `YADGAR_CORE_LOG_LEVEL=info` in `yadgar.service` / `docker-compose.yml` for full INFO coverage from other yadgar.* loggers. `yadgar.requests` is now always visible regardless.

**v5.5.1 update — dual-sink (stdout + rotating file), rate limiter shipped:**
- `RotatingJSONLFileHandler` (stdlib `RotatingFileHandler` subclass) installed alongside stdout StreamHandler by `configure_logging()`. Same `JSONLogFormatter` on both sinks — I14 schema preserved. Size-based rotation: `maxBytes=100_000_000`, `backupCount=5` → 500 MB cap per daemon. Core writes to `/data/logs/yadgar.log`; backend writes to `/data/logs/backend.log` (same `/data` bind mount). Worst-case disk: 500 MB × 2 daemons = 1 GB.
- `RateLimitFilter` (token-bucket per `(logger_name, level)`) ships as `LOG_RATE_LIMIT_ENABLED=True` default. Rate 10/s burst 50. Drops increment `yadgar_log_dropped_total{logger,level,reason}` counter. Summary line emitted at most once per minute on drop.
- Option A env resolution: backend reads `YADGAR_BACKEND_LOG_FILE_PATH` first, falls back to `YADGAR_LOG_FILE_PATH`, then default. Core reads `YADGAR_LOG_FILE_PATH` only. Single `_resolve_log_file_path(process)` helper — no separate Settings class.
- Graceful fallback (I3/I7): if log dir missing or unwritable, warns once to stdout and continues stdout-only. No crash. Explicit opt-out: `YADGAR_LOG_FILE_PATH=""`.
- Three new metrics: `yadgar_log_file_rotations_total{logger}` Counter, `yadgar_log_file_size_bytes{logger}` Gauge, `yadgar_log_dropped_total{logger,level,reason}` Counter. Registered on both core registry (`yadgar/metrics.py`) and backend registry (`yadgar/embed_service_metrics.py`).
- `configure_logging()` gains `process: Literal["core","backend"] = "core"` param. Backend lifespan passes `process="backend"`. Core `__main__.py` passes `process="core"`.
- New settings in `config.py`: `LOG_FILE_PATH`, `LOG_FILE_MAX_BYTES`, `LOG_FILE_BACKUP_COUNT`, `LOG_RATE_LIMIT_ENABLED`, `LOG_RATE_LIMIT_TOKENS_PER_SEC`, `LOG_RATE_LIMIT_BURST`.
- Tests: `yadgar/tests/test_log_rotation.py` — 15 tests covering JSONL write, rotation trigger, backup count, I14 schema, dual-sink, graceful fallback, opt-out, env override, idempotency, rotation counter, rate limiter burst, drop counter, summary line.

**v5.5.2 fix — backend log_* metric wiring:** `RotatingJSONLFileHandler` and `RateLimitFilter` previously imported `yadgar.metrics` unconditionally in `_ensure_metrics()`. Backend's isolated `CollectorRegistry` (in `embed_service_metrics.py`) was declared but never updated — metrics showed HELP/TYPE but zero time series. Fix: DI via `metrics_module=` kwarg (default `None` → `yadgar.metrics`). `_install_file_handler` and `_install_rate_limiter` now call `_resolve_metrics_module(process)` which returns `embed_service_metrics` for `process="backend"`. Backend log_* metrics now update correctly in the backend's own registry. 3 new tests added to `test_log_rotation.py` (`TestBackendMetricDI`) verifying cross-registry isolation.

### I15. Boundary-property fuzz tests (SCOPED)

Every input validator + parser + migration MUST have a Hypothesis property test covering pathological inputs (unicode surrogate pairs, empty, oversized, malformed JSON, SQL-injection-ish strings, race ordering for queue replay). Scope: parsers (SurrealQL builder, queue payload deserialization, hook payload parse), validators (memorize/recall/wiki_query inputs), migrations (#004–#007), queue+DLQ replay. Runs in CI; failure blocks merge.

### Deferred (codify only when violations surface)

- **I16 migration reversibility** — better as documented rollback procedure (restore-from-backup OR forward-fix script) verified by integration test.
- **I17 hooks ≤100ms** — number is a guess until Claude Code's actual hook timeout is confirmed.
- **I18 idempotent retry** — already implicit in queue+job_id; codifying adds no enforcement.

### Recast as PR-template checklist (NOT invariant)

- **I19 forward-context awareness** — every planning PR lists known upcoming requirements as considerations. Soft enough that an invariant adds no enforcement; PR template gets the outcome.

---

## Patterns Library

Validated mechanisms shipped in yadgar. Patterns differ from invariants: invariants describe WHAT must hold; patterns describe HOW a shipped mechanism works. Any plan adding similar capability MUST follow these patterns OR explicitly justify deviation in the PR description (per I10).

### CB-1. Circuit breaker on external dependencies

Shipped: v5.3.10 (N4) — `RemoteMLClient` `/rerank` endpoints.

**State machine:** per-endpoint `CLOSED → OPEN → HALF_OPEN`. Open after `YADGAR_CIRCUIT_BREAKER_FAILURE_THRESHOLD` consecutive failures (default 3). Stay open `YADGAR_CIRCUIT_BREAKER_OPEN_DURATION_SEC` (default 60s). Single probe on cooldown end → success closes, failure re-opens.

**Applies to:** HTTP/RPC to a slower-than-request-path service whose failure shouldn't propagate as latency to the caller. Current users: `/rerank/ce`, `/rerank/nli`, `/rerank/pair`. Future targets: LLM clients (Ollama), external embedding services, any backend endpoint added that isn't correctness-critical.

**Does NOT apply to:** SurrealDB queries (fast + correctness-critical — failure must propagate). Health probes (those ARE the probe).

**Caller contract:** when `score_X()` returns `None`, caller MUST degrade gracefully (skip stage, return pre-rerank order, never crash). See `yadgar/retrieval/_reranking_cross_encoder.py` + `_reranking_nli.py` for the canonical None-guard pattern.

**Code:** `yadgar/ml_client.py::_CircuitBreaker` + per-endpoint instances on `RemoteMLClient`.
**Tests:** `yadgar/tests/test_circuit_breaker.py` (18 tests, includes v5.4.2 + v5.5.3 gauge additions).
**Env:** `YADGAR_CIRCUIT_BREAKER_ENABLED` (default 1), `_FAILURE_THRESHOLD` (3), `_OPEN_DURATION_SEC` (60).

**Why this matters (don't break):** v5.3.9 `BindsTo → Wants` decoupled core from backend lifecycle. Without CB-1, core busy-loops retrying against a struggling backend (the v5.3.10 CPU incident). CB-1 is the architectural pair to the decouple — removing it re-introduces the CPU regression.

**Banned regressions:**
- Removing the breaker without equivalent fault-isolation (rate limiter, bulkhead, exponential backoff).
- Disabling per-endpoint isolation (one breaker for all endpoints — a slow CE would block NLI/pair).
- Bypassing the breaker in "retry harder" patches.

**v5.4.2 update — probe-specific timeout + exponential backoff:**

Root-cause analysis (2026-05-22): v5.3.10 stopped the rapid-retry tight loop but probes themselves were driving CPU. Every HALF_OPEN probe fired real PyTorch CE inference on a saturated model thread, waited the full `BACKEND_HTTP_TIMEOUT_SEC` (5s), then re-opened at the same 60s cooldown. Result: ~1 CPU spike/min indefinitely.

Fix 1a — probe-specific short timeout (`YADGAR_CIRCUIT_BREAKER_PROBE_TIMEOUT_SEC`, default 2.0s). When breaker is HALF_OPEN, probe call overrides httpx timeout to 2s instead of 5s. Implementation: `score_X` detects `is_half_open()` and passes `timeout=self._probe_timeout` to `client.post()`.

Fix 1b — exponential backoff on HALF_OPEN failure. `_CircuitBreaker` now tracks `consecutive_probe_failures`. Each failed probe doubles `_open_duration_sec` (cap: `YADGAR_CIRCUIT_BREAKER_MAX_OPEN_DURATION_SEC`, default 600s). Multiplier: `YADGAR_CIRCUIT_BREAKER_BACKOFF_FACTOR` (default 2.0). `record_success()` resets both counter and duration to base. Does NOT affect `consecutive_failures` (CLOSED-state threshold counter).

New env vars:
- `YADGAR_CIRCUIT_BREAKER_PROBE_TIMEOUT_SEC` (default 2.0) — probe HTTP read timeout
- `YADGAR_CIRCUIT_BREAKER_MAX_OPEN_DURATION_SEC` (default 600) — backoff ceiling
- `YADGAR_CIRCUIT_BREAKER_BACKOFF_FACTOR` (default 2.0) — per-probe cooldown multiplier

Backoff curve (base=60s): 60 → 120 → 240 → 480 → 600 (capped). After 5 consecutive probe failures, backend gets 10 minutes before next probe. Self-heals on success.

**CORRECTS prior verification claim:** v5.3.10 PR claimed CPU spin-up eliminated. Investigation 2026-05-22 shows rate-limited logging (≤1/min) ≠ elimination — probes still fired, still caused spikes. v5.4.2 is the actual fix.

**v5.5.3 update — inline state gauge:**

`yadgar_circuit_breaker_state{endpoint}` (already declared in `yadgar/metrics.py`) now updates inline on every state transition instead of polling. Four sites: `__init__` (→0 CLOSED), `_open()` (→2 OPEN), `is_open()` cooldown-expired branch (→1 HALF_OPEN), `record_success()` (→0 CLOSED). DI: `_CircuitBreaker` accepts `metrics_module=None` kwarg; if None, lazily imports `yadgar.metrics` on first transition. Label is `self._endpoint` (e.g. `/rerank/ce`). Removed the broken `_collect_circuit_breaker_states()` polling function (used `_cb_ce` attr name — never existed on `RemoteMLClient`). 5 new tests in `test_circuit_breaker.py`.

**v5.6.0 update — V1c viz daemon sidebar (SHIPPED):**

Aggregated daemon health panel surfaced in the viz UI. Two components:

1. **Backend aggregator (`yadgar/viz_daemon_health.py`):** background scraper (5s cadence, `# TODO V1d` env-configurable marker) fetches core metrics via `generate_latest()` + backend metrics via `httpx.AsyncClient` GET to `http://127.0.0.1:8001/metrics`. Parses both via `prometheus_client.parser.text_string_to_metric_families`. Caches into `_health_cache`. Exposes `GET /api/daemon-health` (always 200; `backend.unavailable=True` when backend unreachable). SSE channel extended: `daemon_health` event emitted every 5s from `_make_event_stream` in `server/http.py`.

2. **Frontend sidebar (`yadgar/static/index.html`):** 480px right drawer (`#dh-panel`) collapsible via toggle button in topbar. Two-column layout (Core left, Backend right): process (RSS/CPU/FDs/uptime), queue (depth/DLQ/lag-p95), log (size/rotations/dropped), circuit breakers (per-endpoint colored badge: green=CLOSED/amber=HALF_OPEN/red=OPEN), rerank (req ce/nli, err 503, inflight semaphore), models (loaded dot per model). DOM-only rendering — no innerHTML with unsandboxed content (W-SG compliance). SSE handler branch added for `daemon_health` event; `_dhFetchOnce()` REST fallback on panel open. Color palette: `--dh-ok #3fb950 / --dh-warn #e6a817 / --dh-degraded #f85149 / --dh-idle #8b949e`.

Decision: server-side scraping (not browser direct-scrape). Viz UI proxies `/api/*` through `viz_server.py` to daemon — no CORS. Backend unreachability is non-fatal (sidebar shows "backend unreachable" banner, core metrics still live). V1d (env-configurable cadence) deferred.

**No new Python deps.** Uses existing `httpx` + `prometheus_client.parser`. No new JS frameworks — vanilla DOM only per W-FD.

**v5.6.1 update — V1c bug fixes (SHIPPED 2026-05-22):**

Two bugs found in live verification:

1. **Backend URL** (`viz_daemon_health.py`): scraper used `http://127.0.0.1:8001/metrics` — resolves to self inside the core container. Fixed: `_get_backend_metrics_url()` reads `YADGAR_EMBED_URL` (set by nix container config to `http://yadgar-backend:8001`), strips trailing slash, appends `/metrics`. Explicit override via `YADGAR_BACKEND_METRICS_URL` env var (local dev). Hardcoded fallback `http://yadgar-backend:8001/metrics`.

2. **Core process metrics** (`viz_daemon_health.py`): `parse_core_metrics` was calling `_parse_process()` which reads standard `process_resident_memory_bytes` / `process_open_fds` names — names used by prometheus_client's default `ProcessCollector`. Core uses an isolated `CollectorRegistry` with custom gauges: `yadgar_process_rss_bytes`, `yadgar_process_open_fds`, `yadgar_process_cpu_percent`. Fix: new `_parse_core_process()` reads `yadgar_process_*` names. `uptime_s` is now `None` for core (no `process_start_time_seconds` in registry); `cpu_pct` is read directly from gauge (not computed as rate). `_parse_process()` retained unchanged for backend.

### Pattern slots (planned, not yet shipped)

- **CB-2** — bulkhead / connection-pool isolation. Trigger: if backend connection-pool exhaustion surfaces in v5.4 P11 metrics.
- **CB-3** — rate limiter on hook firing. Trigger: v5.3.10 root cause was hook volume driving rerank load; if hook traffic keeps stressing backend even with CB-1, add upstream throttle.
- **DOC-1** — branch-routing canonical-NULL pattern. Trigger: once W1 ships (`wiki_add` `branch_hint` arg), document the symmetric "branch=None means canonical" rule for `wiki_read` callers.

---

## Workflow integration

External plugins installed in the Claude Code harness that affect how yadgar work is performed. NOT invariants — tooling rules. Live here so planning checks pick them up in the same pass.

### W-RL. ralph-loop (`/ralph-loop`, `/cancel-ralph`)

**What:** iterative self-referential loop. `/ralph-loop "<task>" [--max-iterations N] [--completion-promise "<text>"]` runs the prompt repeatedly in the SAME session via a Stop hook that intercepts exit. Main thread sees its own previous file edits + git history each iteration.

**Hard constraint — orchestrator interaction:** Ralph re-feeds the **main thread**, not a subagent. The Orchestrator Mode HARD RULE (delegate ≥2 reads / investigation verbs / ≥3 files) still applies inside every iteration. Permitted shapes:

1. **Delegated body:** each iteration's substantive work IS an `Agent(...)` dispatch; main thread only synthesizes the agent's report and decides whether to continue. Investigation-shaped tasks MUST use this shape.
2. **Non-investigatory check:** iteration body is a one-shot tool call (single bash health check, single metric read, single test invocation) + a decision. Polling, soak-watch, retry-until-green.

**Forbidden shapes:**
- `/ralph-loop "audit X across the codebase"` — investigation in main thread. Use shape (1).
- `/ralph-loop "refactor module Y until tests pass"` — ≥2 file edits per iteration in main thread. Use shape (1) (delegate refactor; main thread runs tests).

**When to use in yadgar:**
- Soak validation: poll `/metrics` + decide whether to stop.
- Consolidation-cycle verification: check `memory_stats` after a write burst, iterate until stable.
- Deployment smoke loops: check daemon health post-`nix-update`.

**When NOT to use:**
- Anything resembling P1-style refactor work — planned bundle, not a loop.
- Multi-step investigations — single Agent dispatches, not iterations.

**Safety:** ALWAYS pass `--max-iterations` (cap: 20). ALWAYS pass `--completion-promise "<exact-phrase>"` matching the success criterion. State file: `.claude/.ralph-loop.local.md`.

### W-FD. frontend-design (skill, auto-fires on frontend prompts)

**What:** skill that injects design-quality guidance (typography, color, motion, composition) when the user asks to build web components / pages / UI. Auto-triggers from prompt content.

**Scope in yadgar:** ONLY `yadgar/viz_server.py` and `ui/` (the viz frontend). Yadgar is 95% backend Python — most "build a dashboard" requests refer to Grafana JSON (`docs/observability/`), which is config, NOT a frontend build. Skill should NOT fire for Grafana work.

**When to use:**
- Touching `ui/` components, pages, layout (viz graph, memory browser, controls).
- Designing new viz panels rendered by `viz_server.py`.

**When NOT to use:**
- Grafana dashboard JSON edits.
- Markdown / docs work with ASCII tables or diagrams.
- Server-rendered HTML in non-`ui/` paths.

**Interaction with orchestrator rule:** frontend change spanning ≥2 files → `Agent(subagent_type="general-purpose", model="sonnet")`. Skills load in subagents, so design guidance still applies.

**Follow-up (not done yet):** consider `mcp__yadgar__agent_prompt_save(pattern="dispatch-viz-ui", ...)` to inject a viz-specific prelude into subagent dispatches that touch `ui/`. Skip until first viz-UI work surfaces a concrete prompt pattern.

### W-SG. security-guidance (PreToolUse hook, passive)

**What:** PreToolUse hook (`security_reminder_hook.py`) runs before every Edit / Write / MultiEdit. Detects security-sensitive patterns (currently: `.github/workflows/*.yml` template-injection risks, plus XSS / unsafe-code patterns). Logs to `/tmp/security-warnings-log.txt`. **Non-blocking — informational warnings only.**

**Operational rule:** hook fires automatically — no opt-in. On warning:

1. Read warning text + linked guidance.
2. Apply safe pattern (e.g. `env: VAR: ${{ ... }}` then `run: echo "$VAR"` for GHA template injection).
3. Do NOT silence the hook. Do NOT bypass via raw Bash + heredoc to avoid Edit/Write tools.
4. If wrong for the specific case, document why in commit message — but still apply safe pattern unless impossible.

**Yadgar surfaces likely to trigger:**
- `.gitea/workflows/*.yml` (Forgejo CI — analogous to GHA, same injection risks).
- `entrypoint-backend.sh` and other shell scripts with env-var interpolation.
- Any new dockerfile `RUN` lines with `${VAR}` expansion of user-controlled inputs.

**No carve-out for orchestrator mode:** hook fires on main thread AND subagents (PreToolUse is per-tool, not per-context). Subagents must respect it identically.

---

## Current violations (snapshot v5.3.7, 2026-05-20 evening)

| Site | Invariant | Notes |
|---|---|---|
| `memorize.py:154` `embeddings.encode` | I1, I4 | sync ML in fallback request path |
| `memorize.py:229` `curator.curate_on_remember` | I1, I2 | inlined v5.1 (7c29a33) |
| `memorize.py:310` `pool.assign_memory` | I1, I2 | astrocyte inlined v5.1 |
| `memorize.py:321/331` `prospective.*` | I1, I2 | inlined v5.1 |
| `memorize.py:337` `engram.allocate` | I1, I2 | inlined v5.1 |
| `memorize.py:366` `thermo.apply_session_coherence` | I1 | DB write per request |
| `memorize.py:392` `retriever.recall` reinjection | I1, I2 | recall inside memorize |
| `conflict_resolver.py:149` `httpx.post` | I3, I4 | sync 30s timeout if env on |
| Drainer `_apply()` | I2, I6 | replays full tool, not lean inserts |
| Backend image 1.63 GB (was 6.78 GB pre-v5.4.2) | I11 | F0 achieved incidentally during v5.4.2 backend rebuild (newer ML wheels). P9 ratchet (`scripts/check_image_size.py`, cap 2.0 GB) prevents regression. |
| Backend embed_service OOM under /rerank load | I8 | **MITIGATED v5.4.2 F5-A** — concurrent-inference semaphore bounds rerank throughput; probes fast-fail 503 instead of queueing. Observability gap remains: no liveness/memory metrics yet (P11 N3 gauges pending). |
| No read-path metrics + no per-stage write metrics | I8, I12 | blocks all perf optimization — P11 prerequisite |
| 26+ high-complexity functions (anchor 116496) | I13 | full catalog via P12 |
| systemd `BindsTo=yadgar-backend.service` on `yadgar.service` | (cascade-failure) | NOT an invariant violation per se but operationally fatal; fix in v5.3.9 (decouple) |
| Default httpx timeouts on backend calls | (resilience) | unbounded → thread starvation during backend failure; v5.3.9 N1 (5s timeout) |
| ASGI graceful shutdown holds 30s | (resilience) | drainer + ML client retries block exit; v5.3.9 N2 (≤5s budget) |

---

## Candidate plans

Full v5.3.9 / v5.4 / v5.5 execution order + exit criteria in the `yadgar-roadmap-future-improvements` wiki and `docs/PLAN_V5_4_to_v7.md`.

### P1. Split memorize into thin-enqueue + heavy-drain + consolidation

- `_memorize_enqueue` (~50 LOC, request path)
- `_memorize_apply_lean` (~150 LOC, drainer)
- `_memorize_apply_consolidation` (deferred: curator, engram, astrocyte, reinjection, postmortem boost)

**Lands v5.5.** Informed by v5.4 P12 audit.

### P2. Deferred ops → ConsolidationScheduler

Fast-tier sub-cycle (5–15s) picks memories where `last_consolidated IS NONE`. Re-uses existing infra. **Lands v5.5.**

### P3. Wrap sync ML in `asyncio.to_thread`

Survey `grep -n "\.encode(" yadgar/ -r`. Every async-context call → `await asyncio.to_thread(model.encode, ...)`. **Lands v5.5** (data-driven via v5.4 P11).

### P4. C4 conflict resolver gate hoist

Check `YADGAR_CONFLICT_RESOLVER` at module import. **Lands v5.4.**

### P5. (folded into P11)

### P6. Drainer concurrency

`YADGAR_DRAINER_WORKERS` env. **Optional v5.5 bench.**

### P7. Reinjection becomes opt-in

`YADGAR_REINJECT_ON_WRITE` default OFF. **Lands v5.4.**

### P8. Idempotency markers for I6

`consolidation_state` field (NULL / drainer-done / consolidation-done). **Lands v5.5** (couples with P1/P2).

### P9. Image partitioning audit for I11

During v5.4 F0, every layer >100MB justified or moved. Add `docker history` check to release-readiness CI. **Lands v5.4.**

**Shipped v5.4.2:**

- **Script:** `scripts/check_image_size.py` — CLI checker using `podman history` (fallback: `docker history`). Parses size column (`1.36GB`, `119MB`, `19.5kB`) and sums layers.
- **Caps:** backend image ≤2.0 GB (`--max-size-gb 2.0`); core image ≤0.8 GB (auto-detected from image name substring). Override via `--max-size-gb`.
- **Layer warnings:** layers >500 MB (`--warn-layer-mb 500`) emit a warning to stdout; exit 0 (warning only). Total over cap → exit 1 + message to stderr.
- **Hook stage:** `stages: [manual]` — does NOT run on every commit. Image must exist locally. Invoke after `podman build`:
  ```
  pre-commit run check-image-size-backend --hook-stage manual
  pre-commit run check-image-size-core --hook-stage manual
  ```
- **Trigger files:** `Dockerfile.backend`, `Dockerfile`, `uv.lock`, `pyproject.toml` (build inputs that affect size).
- **Tests:** `yadgar/tests/test_check_image_size.py` — 30 tests; `subprocess.run` mocked, no real container calls.
- **I11 cross-reference:** ratchet preserves the 6.78 GB → 1.63 GB improvement achieved incidentally in v5.4.2 backend rebuild. Any future dep bump that pushes backend past 2 GB will be caught before release.

### P10. (folded into P11)

### P11. Observability v1 — UNIFIED metrics framework

Subsumes P5 + P10 + N3. Single bundle. **FIRST PR in v5.4** — prerequisite per I12.

**Write path:** queue_depth, dlq_size, drainer_lag_ms, drain_cycle_duration_ms, drain_stage_ms{stage}, writegate_outcome{outcome}.

**Read path:** recall_duration_ms, recall_result_count, recall_stage_ms{stage} for embed_query/bm25/hnsw/ppr/spreading_activation/cross_encoder/nli/contextual_prefix/rerank_final. Same shape for wiki_query.

**Embedding:** encode_duration_ms{model}, encode_queue_depth, encode_cache_hit_rate.

**KG / curator / engram:** entity_extract_duration_ms, curator_duration_ms + curator_merge_outcome{merged/linked/noop}, engram_allocate_duration_ms, astrocyte_assign_duration_ms.

**LLM (C4):** llm_call_duration_ms{provider,model,purpose}, llm_decision{outcome}.

**MCP + auth:** mcp_request_duration_ms{tool}, mcp_auth_check_duration_ms, mcp_request_count{tool,status}.

**Database:** surrealdb_query_duration_ms{op}, surrealdb_connection_pool_wait_ms, surrealdb_pool_active.

**Process:** process_rss_bytes, _cpu_percent, _open_fds, python_gc_duration_ms{generation}.

**Subagents:** subagent_dispatch_count{agent_type}, subagent_capture_rate.

**Viz:** viz_api_graph_duration_ms, viz_sse_clients, viz_dbsize_sample_duration_ms.

**N3 backend liveness (new, crash-driven):** `yadgar_backend_reachable{endpoint=ce/nli/pair/dbsize/storage}` (gauge); `yadgar_backend_memory_pressure` if exposed by backend.

Ships with: Grafana dashboard JSON + alert rules YAML in `docs/observability/`. Decorator helper `yadgar/observability/timing.py`. Backward-compatible.

### P12. Complexity audit — one-time catalog (PRE-P1)

NOT auto-decompose. Output `docs/complexity-audit.md` table with file:line + cyclomatic/LOC/params/nesting + hard/soft violation + decomposition risk per I5 + proposed action.

Risk-tiered: LOW → v5.5/v5.6 small PRs (~5 funcs/PR with test parity); MEDIUM → per-PR explicit topology proof per I5; HIGH → P11-gated; cohesion → one-line noqa annotation.

**Lands v5.4** (PRE-P1 means before memorize split in v5.5).

### V1. Viz daemon health panel — both daemons surfaced (added 2026-05-21 evening)

Post-v5.3.9 `BindsTo → Wants` decouple, core + backend run as independent daemons. Viz currently shows neither. Add a sidebar / overlay in `yadgar/viz_server.py` UI that polls `/metrics` for both and renders per-daemon health.

**Per-daemon (core + backend):**
- `process_rss_bytes`, `process_cpu_percent`, `process_open_fds`
- Uptime (compute from `process_start_time_seconds` if exposed, else from first-seen)
- Recent restart indicator (systemd `ActiveEnterTimestamp` via journal probe, OR detect from uptime reset)
- `python_gc_duration_ms{generation}` p95

**Core-only:**
- `queue_depth`, `dlq_size`, `drainer_lag_ms` p95, `drain_cycle_duration_ms` p95
- `encode_queue_depth` (if `asyncio.to_thread` queue ships per P3)
- `mcp_request_duration_ms{tool}` top-5 slowest tools

**Backend-only:**
- Rerank queue depth (if exposed — sub-task: backend may not expose `/metrics` yet; gate V1 on backend-metrics-endpoint sub-task)
- Model-loaded state per reranker (CE / NLI / pair)
- GPU memory if applicable
- `embed_service_status` (memory pressure post-F5 investigation)

**Cross-daemon (already in P11):**
- `backend_reachable{endpoint=ce/nli/pair/dbsize/storage}` (N3 gauge)
- Per-endpoint circuit breaker state (CLOSED / OPEN / HALF_OPEN) — CB-1 currently surfaces only via logs; need a gauge
- Recent rerank failure count (last 1m, 5m, 15m)

**Sub-tasks:**
- ~~**V1a.** Backend `/metrics` endpoint — add `prometheus_client` to backend image + expose port. Required prerequisite.~~ **SHIPPED v5.5.0.** `yadgar/embed_service_metrics.py` + GET `/metrics` on embed_service app. F5-A semaphore counters + model gauges + process metrics. Unauthenticated (matches core pattern). Backend bumped 5.0.3 → 5.1.0.
- ~~**V1b.** Circuit breaker state gauge — extend CB-1 to emit `yadgar_circuit_breaker_state{endpoint}` (0=CLOSED / 1=HALF_OPEN / 2=OPEN).~~ **SHIPPED v5.5.3.** Inline updates from `_CircuitBreaker` transitions (`__init__`/`_open`/`is_open` cooldown branch/`record_success`). Removed pre-existing broken `_collect_circuit_breaker_states()` polling (looked for `_cb_ce`/`_cb_nli`/`_cb_pair` attrs that never existed).
- **V1c.** Viz daemon panel UI — sidebar component in `yadgar/viz_server.py` (or `ui/` if frontend split lands). SSE-driven (reuse existing viz SSE channel, see `viz_sse_clients` metric).
- **V1d.** Refresh cadence: 5s default. Configurable via `YADGAR_VIZ_HEALTH_REFRESH_SEC`.

**V1b/V1c/V1d land v5.5.x** (V1a prerequisite now met). Touches both daemons (image change in core for SSE channel addition). Frontend changes invoke W-FD skill per Workflow integration section.

**Why:** P11 ships metrics to Grafana but Grafana is ops-side. Viz is the in-session UX — when a user is browsing memory graph and something feels slow, daemon health belongs ONE PANE AWAY, not "open Grafana, find the right dashboard". Same principle as P11 surfacing metrics through `memory_stats` MCP tool — multiple surfaces for the same data.

### Crash-driven items (added 2026-05-20 evening)

- **N1.** 5s HTTP timeouts on backend calls (ML client, drainer, dbsize, storage). **Lands v5.3.9** (urgent crash-prevention).
- **N2.** ASGI graceful shutdown ≤5s budget. **Lands v5.3.9.**
- **N3.** Backend liveness gauges — folded into P11.
- **N4.** ~~Circuit breaker on backend client~~ — shipped in v5.3.10 (see CB-1 in Patterns Library).
- **F0.** Backend image bloat 6.78GB → ≤1.6GB. **Lands v5.4.**
- **F1/F2.** Async embed load + pre-pull. **Lands v5.5.**
- **F3.** Blue-green backend swap. **RE-PRIORITIZED to v5.5 P1** — crash justified systemd refactor.
- **F5.** ~~Backend OOM root-cause INVESTIGATION REPORT in v5.4 (1-pager). FIX in v5.5.~~ **FIX SHIPPED v5.4.2 (F5-A concurrent-inference semaphore).** See "Shipped F5-A" below.
- **systemd cascade decouple.** `BindsTo=yadgar-backend.service` → `Wants=`. **Lands v5.3.9.** Lives in nix repo, NOT this repo. Document via MIGRATION_NOTES.md per HARD RULE.

### Shipped: F5-A concurrent-inference semaphore (v5.4.2)

**Root cause confirmed (2026-05-22):** `embed_service` at 158% CPU, 37 consecutive `/rerank/ce` timeouts. Every HALF_OPEN probe fired real PyTorch CE inference on a saturated model thread with no concurrency control — unlimited concurrent inference requests queued behind each other. Probes couldn't fast-fail.

**Fix:** per-mode `asyncio.Semaphore(N)` in `yadgar/embed_service.py`, N=1 default. `/rerank` handler acquires semaphore with `asyncio.wait_for(sem.acquire(), timeout=RERANK_SEMAPHORE_ACQUIRE_TIMEOUT_SEC)`. On timeout → 503 immediately. Circuit-breaker probe then fast-fails as timeout/5xx → re-OPEN without burning CPU on doomed inference.

**Env:** `YADGAR_RERANK_MAX_CONCURRENCY` (default 1), `YADGAR_RERANK_SEMAPHORE_ACQUIRE_TIMEOUT_SEC` (default 2.0).
**Tests:** `yadgar/tests/test_embed_service_semaphore.py` (4 tests).
**Backend bump:** `server.json` `backend_version` 5.0.2 → 5.0.3.

**If saturation persists post-F5-A:** consider F5-B (lazy-load/idle-eviction, ~80 LOC) and/or F5-C (cgroup bump `--cpus 2 → 4`, nix repo change, document in `MIGRATION_NOTES.md`).

---

## Decision log

- 2026-05-20: created. Triggered by write-speed regression during v5.3.7 soak. No invariants overridden yet.
- 2026-05-20: I11 added (image partitioning).
- 2026-05-20: I12 added (measure before optimize).
- 2026-05-20: P11 added (Observability v1) unifying P5 + P10; FIRST in v5.4 per I12.
- 2026-05-20: I13 added (bounded complexity) + P12 (complexity audit); P12 ordered PRE-P1.
- 2026-05-20: I14 added (structured logging, scoped — trace_id propagation moved to v5.5 separate P-item).
- 2026-05-20: I15 added (boundary-property fuzz tests, scoped).
- 2026-05-22: I14 implementation shipped (v5.4.2) — JSONLogFormatter + ContentRedactor + YADGAR_LOG_FORMAT env var + 4 integration sites converted (circuit_breaker, embed_service/rerank, drainer drain_cycle, memorize enqueue-fallback). Default log format changed human→json. Ratchet active; full conformance target v5.6.
- 2026-05-20: I16/I17/I18 DEFERRED (codify when violations surface). I19 recast as PR-template checklist (not invariant).
- 2026-05-20 evening: backend OOM cascade. N1/N2 + systemd-cascade-fix MOVED to v5.3.9 hotfix. F3 RE-PRIORITIZED to v5.5 P1. F5 reframed as v5.4 report + v5.5 fix. F0 vs OOM separated. N4 circuit breaker proper-designed in v5.4. v5.3.8 → v5.3.9.
- 2026-05-20: cap numbers (15 cyclomatic / 150 LOC hard / 80 LOC soft) provisional. P12 audit data may trigger I13 review per I10 (if >20% violations).
- 2026-05-21: Patterns Library section added (introductory CB-1 for circuit breaker shipped v5.3.10). Patterns ≠ invariants; both must be checked by future planning.
- 2026-05-21 evening: Workflow integration section added for `ralph-loop`, `frontend-design`, `security-guidance` plugins installed 2026-05-21. Not invariants — workflow tooling. Section scoped per-plugin with explicit orchestrator-rule carve-out for ralph-loop.
- 2026-05-21 late evening: V1 viz daemon health panel added — surfaces both core + backend daemon stats (RSS/CPU/FDs + queue/breaker/model-load) in viz UI. Targets v5.5. Sub-tasks V1a (backend /metrics endpoint) + V1b (CB state gauge) + V1c (sidebar UI) + V1d (refresh cadence env).
- 2026-05-22: CB-1 probe-fixes + F5-A saturation fix shipped in v5.4.2. CORRECTS prior v5.3.10 verification claim (rate-limited logging ≠ elimination — probes still caused CPU spikes). Fix 1a (probe timeout 2s), Fix 1b (exponential backoff 60→600s), F5-A (semaphore N=1 per mode). Backend bump 5.0.2→5.0.3; core bump 5.4.1→5.4.2.
- 2026-05-22: P9 image size ratchet shipped (v5.4.2). F0 (6.78 GB → 1.63 GB) preserved via release-readiness check. Caps: core ≤800 MB, backend ≤2 GB. Script: `scripts/check_image_size.py`. Hook stage: manual (post-build gate, not per-commit).
- 2026-05-22: v5.5.3 V1b CB-1 state gauge shipped. `yadgar_circuit_breaker_state{endpoint}` now updated inline at all four state transition sites in `_CircuitBreaker`. Removes broken polling `_collect_circuit_breaker_states()`. Core 5.4.5 → 5.5.3; backend unchanged 5.0.3.
- 2026-05-22: v5.4.3 hotfix shipped — I14 framework-logger coverage extended to root logger (uvicorn, mcp, fastmcp, httpx all now emit JSON). I13 b27d218 grandfathering gap closed: 31 pre-existing C901/PLR0913 violators added to pyproject.toml per-file-ignores; refactor target v5.4.4. Backend version unchanged (5.0.3).
- 2026-05-22: v5.4.7 I14 ratchet cleanup shipped — `RequestLoggingMiddleware` migrated to I14 schema (component/action/outcome/latency_ms/http_status); `ContentRedactor` denylist tightened (two-tier exact+substring, `content_type`/`content_length` false-positive fixed). Both v5.6 I14 follow-ups RESOLVED. Backend version unchanged (5.0.3). BREAKING: `duration_ms` renamed to `latency_ms` in request logs.
- 2026-05-22: v5.4.8 middleware visibility fix shipped — root cause: `CORE_LOG_LEVEL` defaults to "warn" → root handler at WARNING → `yadgar.requests` INFO records silently dropped. Fix: dedicated always-INFO handler on `yadgar.requests` (propagate=False) installed by `configure_logging()`. Secondary finding: `YADGAR_LOG_LEVEL` (used in bug report) is NOT a valid env var; correct var is `YADGAR_CORE_LOG_LEVEL`. Neither production deployment file sets it. Operator action: add `YADGAR_CORE_LOG_LEVEL=info` to `yadgar.service` / `docker-compose.yml` for full INFO on all `yadgar.*` loggers. Backend version unchanged (5.0.3).
- 2026-05-22: V1a backend /metrics endpoint shipped (v5.5.0). `yadgar/embed_service_metrics.py` new module with isolated CollectorRegistry. GET /metrics on embed_service app — unauthenticated (matches core pattern in yadgar/server/http.py §15; Prometheus scrapers can't carry bearer tokens). F5-A semaphore observability: `yadgar_embed_rerank_requests_total{mode}`, `yadgar_embed_rerank_503_total{mode}`, `yadgar_embed_rerank_duration_seconds{mode}`, `yadgar_embed_rerank_semaphore_held{mode}`. Model state: `yadgar_embed_model_loaded{model}`. Process: ProcessCollector + PlatformCollector. Always-on (no opt-in gate — I3 opt-in considered and rejected; no opt-in needed for a low-overhead, data-free endpoint). Backend bumped 5.0.3 → 5.1.0; core bumped 5.4.8 → 5.5.0. V1b/V1c/V1d unblocked.
- 2026-05-22: v5.5.1 log rotation plan drafted (`docs/PLAN_v5_5_1_log_management.md`) — dual-sink (stdout+rotating file), 500 MB default cap, I3/I12/I13/I14 compliant. Awaiting human answers to 4 open questions before implementation dispatch.
- 2026-05-22: v5.5.1 log rotation + rate limiter shipped. Dual-sink (stdout+rotating file), 500 MB cap/daemon, `RateLimitFilter` token-bucket at 10/s burst 50 (default ON), Option A env resolution (`YADGAR_BACKEND_LOG_*` override), 3 new metrics (rotations_total/file_size_bytes/dropped_total), 15 tests. Core 5.5.0→5.5.1; backend 5.1.0→5.1.1. Operator pre-deploy: `mkdir -p ~/.yadgar/logs`.
- 2026-05-22: v5.5.2 shipped — backend log_* metric wiring fix. `_ensure_metrics()` in `RotatingJSONLFileHandler` and `RateLimitFilter` hardcoded `yadgar.metrics` import; backend's isolated `embed_service_metrics` registry was never updated. Fixed via DI (`metrics_module=` kwarg) + `_resolve_metrics_module(process)` helper in install path. Core 5.5.1→5.5.2; backend 5.1.1→5.1.2.

---

## Cross-references

- Wiki mirror: `yadgar-architectural-invariants`.
- Locked trajectory: `docs/PLAN_V5_4_to_v7.md`.
- Roadmap wiki: `yadgar-roadmap-future-improvements`.
- Original write-flow spec wiki: `yadgar-write-pipeline-surprise-gated`.
- Stabilize-strategy (frozen) wiki: `yadgar-v5-stabilize-strategy-tldr-gap-analysis`.
- Soak observation memories (3 entries for 2026-05-20).
- Regression commits: `7c29a33` (v5.1 module decomp), `263bfa3` (v5.3.4 C4 conflict resolver inline).
- Crash forensics: journalctl 2026-05-20 19:57:53 (backend OOMKill) → 20:01:14 (manual restart).
