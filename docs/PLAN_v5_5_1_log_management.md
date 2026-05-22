# PLAN v5.5.1 — Log Rotation + Disk-Fill Protection

Status: **DRAFT — awaiting answers to §12 open questions before implementation.**
Version target: v5.5.1
Author: architecture session 2026-05-22
Related: `docs/ARCHITECTURE_INVARIANTS.md` (I3, I12, I13, I14), `docs/observability/`

---

## 1. Goals & Non-Goals

### Goals

- **Bound disk footprint.** Worst-case max bytes on disk is known at deploy time, not left to chance.
- **Preserve `journalctl --user -u yadgar` workflow.** Stdout path stays intact; operators keep live-tail.
- **Enable separate retention policies per severity.** INFO vs WARNING can rotate independently.
- **Sane defaults, out-of-box.** File logging enabled by default wherever the log directory is writable; no operator config required to get protection.

### Non-Goals

- Log shipping to remote sinks (Loki, Splunk, Grafana Cloud) — separate v5.x feature.
- Log search / indexing (ELK, Loki LogQL) — out of scope.
- Log encryption at rest.
- Multi-host aggregation.

---

## 2. Constraints

All implementation work for v5.5.1 must satisfy these invariants (from `ARCHITECTURE_INVARIANTS.md`).

| Invariant | Impact on log management |
|-----------|--------------------------|
| **I3** — opt-in features short-circuit before expensive setup | File-logging gate = single env-var read at import time (`os.getenv("YADGAR_LOG_FILE_PATH", default)`). O(1). If empty string → skip handler install entirely. No filesystem stat, no open(), no thread spawn. |
| **I12** — measure before optimize | Cap defaults derived from observed journal volume (§4). Any future tuning requires before/after metrics. |
| **I13** — bounded complexity | New `RotatingFileHandler` setup code: cyclomatic ≤10 soft / ≤15 hard; LOC ≤80 soft / ≤150 hard. Helper functions for handler construction keep setup out of `configure_logging()` main body. |
| **I14** — structured logging contract | Rotation must preserve one-JSON-record-per-line. `RotatingFileHandler` in Python stdlib rotates at file-level with an atomic `os.rename`-style doRollover — no partial record writes during rotation (stdlib guarantee). |

**I3 / default-on reconciliation:** "defaults that work out-of-box" and "O(1) gate" are compatible. The gate check (`YADGAR_LOG_FILE_PATH`) is an env-var read — constant time regardless of value. On by default means the default value of `YADGAR_LOG_FILE_PATH` resolves to a real path (`/data/logs/yadgar.log`). Off = explicitly empty string. No contradiction.

---

## 3. Proposal: Dual-Sink Architecture

### Sink A — stdout → journald (current behavior, unchanged)

- Yadgar process writes to stdout. Container runtime (`podman run --log-driver journald`) ingests into host journald.
- Operator workflow `journalctl --user -u yadgar` continues to work.
- Host journald caps are the **operator's responsibility** (see §8 / MIGRATION_NOTES recommendation).
- v5.5.1 adds no code here. Sink A is already shipping.

### Sink B — rotating JSON file inside container data volume (new in v5.5.1)

Python stdlib `logging.handlers.RotatingFileHandler` writing JSONL to a configurable path.

**Defaults:**

| Parameter | Default | Env var to override |
|-----------|---------|---------------------|
| File path | `/data/logs/yadgar.log` | `YADGAR_LOG_FILE_PATH` |
| Max file size | 100 MB | `YADGAR_LOG_FILE_MAX_BYTES` |
| Backup count | 5 | `YADGAR_LOG_FILE_BACKUP_COUNT` |
| Disabled | No (on by default if dir writable) | Set `YADGAR_LOG_FILE_PATH=""` to opt-out |

Total worst-case disk: **500 MB** (100 MB × 5 files). Justified in §4.

**Behavior:**

- `RotatingFileHandler` is installed during `configure_logging()` alongside the existing stdout handler. Both emit simultaneously.
- Same `JSONLogFormatter` instance used for both sinks — I14 schema preserved.
- If path directory does not exist or is not writable: warn once to stdout, skip handler install, continue stdout-only. No crash, no silent failure.
- Handler is idempotent (same guard as existing stdout handler — check `handler.__class__.__name__` before adding).

**Backend (yadgar-backend):**

Same dual-sink pattern. Env vars use the same names by default, but backend has option of separate prefix (open question — see §12). Backend's `configure_logging()` call in `embed_service.lifespan` gains the same Sink B setup.

---

### Alternative Analysis

**Alt 1: File-only, no journald**

Operator loses `journalctl --user -u yadgar` live-tail. Violates goal 2. Rejected.

**Alt 2: journald-only with per-logger rate limiting**

Rate limiting smoothes peaks but does not bound total disk. A sustained INFO flood at 1 req/sec × 300 B runs at ~26 MB/day even if rate-limited to 10/sec — disk still grows unbounded over weeks. Rejected as primary strategy; rate limiting is useful as optional add-on (§5), not as replacement for rotation.

**Alt 3: `TimedRotatingFileHandler` (daily) vs `RotatingFileHandler` (size)**

- Timed: rotates at midnight regardless of size. If a deployment has a spike day followed by quiet weeks, some files are huge (disk bound violated) and some are tiny (rotation slots wasted).
- Size: rotates predictably — worst-case disk is always `max_bytes × (backup_count + 1)`. Mathematical guarantee.
- Recommendation: **size-based**. Predictable disk bound is the primary goal; time-based rotation is a secondary human convenience. If human readability matters, the timestamp in each JSON record lets operators filter by time post-hoc.

**Alt 4: Async queue + worker thread for log writes**

Benefit: log writes don't block caller; backpressure can be applied.
Risk: queue drains in-flight at process exit → last N log records lost on crash (exactly when you most need them). This violates the spirit of I7 (queue is the durability boundary) applied to logs.
Decision: **defer to v5.6** when trace-id propagation and async log pipeline design is more mature. v5.5.1 uses synchronous `RotatingFileHandler` (stdlib, battle-tested, no lost-on-crash risk).

---

## 4. Estimated Log Volume

### Observed state

- User journald: **1.3 GB**
- System journals: 1.4 GB
- Accumulation time period: **unknown** (not recorded; gap acknowledged)

### Bracketed daily rate

| If journals accumulated over | Implied daily rate |
|-----------------------------|--------------------|
| 7 days | ~186 MB / day |
| 30 days | ~43 MB / day |
| 60 days | ~22 MB / day |
| 90 days | ~14 MB / day |

### Bottom-up sanity check

Hook traffic (observed): 36 `/hooks/auto-capture` per hour = 864 / day.

```
864 req/day × 300 B/line = ~260 KB/day from hook traffic alone
```

That is orders of magnitude below 22 MB/day (90-day scenario). The discrepancy indicates the 1.3 GB is dominated by one or more of:
- ASGI lifecycle spam (startup/shutdown events repeated across container restarts)
- Uvicorn access log lines (before `uvicorn.access` was capped to WARNING in v5.4.3)
- DEBUG-level output active for some period
- System journal scope includes unrelated services

**Conclusion:** raw hook traffic is negligible. True steady-state INFO volume likely falls in the 20–50 MB/day range (30–60 day scenario) under normal operation.

### Cap default justification

Worst-case scenario: DEBUG enabled + restart storm + high tool-call rate.
DEBUG can 10× INFO volume → assume 500 MB/day peak.
A 500 MB cap covers ~1 day of worst-case DEBUG.
Under normal INFO-only operation (20–50 MB/day), 500 MB = 10–25 days of coverage before oldest backup rotates out. That is a generous retention window for recent-incident debugging.

**Default 500 MB total (5 × 100 MB) is justified.**

### Monthly / yearly projection (INFO-only normal operation)

| Period | Volume |
|--------|--------|
| Monthly | ~650 MB – 1.5 GB (bracketed from daily rate) |
| Yearly | ~8 GB – 18 GB |

Without rotation: disk fills in weeks under peak load. With rotation: disk stays at ≤500 MB regardless of age.

---

## 5. Rate Limiting (Optional Add-On)

A per-logger rate limiter suppresses floods without losing all records. Implementation: `logging.Filter` subclass using token-bucket algorithm.

**Candidate targets:**

- `yadgar.requests` (RequestLoggingMiddleware) — high volume, low signal per line. At 864/day normal, at 8,640/day spike (10×). Rate-limit to 1 log/sec bursting to 10 — sheds ~90% of spike traffic.
- `yadgar.circuit_breaker` (WARNING repeated open events) — when a dependency is down, circuit-breaker WARNING fires on every rejected call. Rate-limit to 1/min per endpoint.

**Design:**

```python
class TokenBucketFilter(logging.Filter):
    """Env-gated; no-op if YADGAR_RATE_LIMIT_LOGS="" """
    def __init__(self, rate: float, burst: int, counter_metric: Counter):
        ...
    def filter(self, record: logging.LogRecord) -> bool:
        # refill tokens, check bucket, increment yadgar_log_dropped_total on drop
        ...
```

**Gate:** `YADGAR_RATE_LIMIT_LOGS=1` enables; default off (empty = disabled). O(1) check at import (I3 compliant).

**Open question:** ship in v5.5.1 or defer to v5.5.2? See §12.

---

## 6. Backend Log Management

`yadgar-backend` (embed service) runs its own uvicorn instance. Log sources:
- uvicorn startup / shutdown
- `/rerank` and `/embed` request logs (via ASGI middleware in backend)
- Circuit breaker state changes
- ML model load events (high-volume during cold start)

**Approach:** same dual-sink design. Backend's `configure_logging()` call (in `embed_service.lifespan`) gains Sink B setup with backend-specific defaults.

**Env vars (preferred option — see §12 open question):**

Option A (shared prefix, different default path):
- `YADGAR_LOG_FILE_PATH` → `/data/logs/yadgar.log` (core)
- `YADGAR_BACKEND_LOG_FILE_PATH` → `/data/logs/backend.log` (backend-specific)
- `YADGAR_BACKEND_LOG_FILE_MAX_BYTES`, `YADGAR_BACKEND_LOG_FILE_BACKUP_COUNT`

Option B (unified prefix, single file):
- Both core and backend write to same `YADGAR_LOG_FILE_PATH` — simpler but interleaves two processes in one file, complicating filtering.

Recommendation: **Option A** (separate paths, backend prefix). Keeps log streams separated; easier to grep, easier to rotate independently if backend is noisier. Decision is open — see §12.

---

## 7. Migration & Backward Compatibility

- v5.5.1 **adds** Sink B handler. Removes nothing.
- Existing journalctl workflow: **unchanged**.
- If `/data/logs/` directory does not exist: warn once to stdout (`logging.warning("log file handler skipped: /data/logs/ not writable")`), continue stdout-only. Graceful fallback, no crash.
- Operator opt-out: `YADGAR_LOG_FILE_PATH=""` — handler install is skipped entirely at O(1) (I3).
- No schema change (I14 preserved — same JSONLogFormatter on both sinks).
- No behavior change for operators who don't set new env vars (default path activates only if `/data/logs/` already exists and is writable via bind mount).

**Recommended journald caps (MIGRATION_NOTES content):**

Operators should add to `~/.config/systemd/user/yadgar.service` or host's `/etc/systemd/journald.conf`:

```ini
[Journal]
SystemMaxUse=500M
RuntimeMaxUse=200M
```

Or per-unit (if supported):
```
PODMAN_SYSTEMD_UNIT=yadgar.service
```

Document in `MIGRATION_NOTES.md` as operator recommendation, not hard requirement.

---

## 8. Nix Integration

**Current bind mount:** `podman run … -v ${homeDir}/.yadgar:/data`

This means `/data/logs/` inside the container maps to `~/.yadgar/logs/` on the host. The directory needs to exist and be writable.

**Required nix change:** ensure `~/.yadgar/logs/` is created at activation.

```nix
# In yadgar.nix service activation or systemd tmpfiles
systemd.user.tmpfiles.rules = [
  "d %h/.yadgar/logs 0750 - - -"
];
```

**Alt: Separate volume for logs**

`-v ${homeDir}/.yadgar-logs:/logs` with `YADGAR_LOG_FILE_PATH=/logs/yadgar.log`.

| Approach | Pros | Cons |
|----------|------|------|
| Shared `/data/logs/` | No new bind mount; already configured | Logs and data co-locate; one `rm -rf` clears both |
| Separate `/logs/` volume | Logs isolated; can manage independently | Two bind mounts in nix; more configuration |

Recommendation: **shared `/data/logs/`** for v5.5.1. Simpler, fewer changes. Separate volume is a valid v5.6 upgrade if operators want independent lifecycle.

**MIGRATION_NOTES action:** document the `systemd.user.tmpfiles.rules` addition for `~/.yadgar/logs/`.

---

## 9. Observability of the Log System Itself

Three new metrics exposed via core and backend `/metrics` endpoints (V1a foundation in place as of v5.5.0).

| Metric | Type | Labels | Notes |
|--------|------|--------|-------|
| `yadgar_log_file_rotations_total` | Counter | `logger` | Incremented each time `RotatingFileHandler.doRollover()` is called. Hooked via `logging.Handler` subclass that overrides `doRollover`. |
| `yadgar_log_file_size_bytes` | Gauge | `logger` | Current active log file size. Polled on each emit (cheap: `os.path.getsize` — cached between emits, updated at most once per second to avoid syscall-per-record). |
| `yadgar_log_dropped_total` | Counter | `reason` | Incremented per rate-limit drop (§5). Only registered if rate limiter is enabled. |

**Alert suggestion (document in `docs/observability/`):**

- `yadgar_log_file_rotations_total` rate > 10/hour → log volume spike, investigate root cause.
- `yadgar_log_file_size_bytes` near `YADGAR_LOG_FILE_MAX_BYTES` continuously → backup_count too low or max_bytes too small.

---

## 10. Tests Required for v5.5.1

Test file: `yadgar/tests/test_log_rotation.py`

| Test | What it proves |
|------|----------------|
| `test_rotating_file_handler_writes_jsonl` | Each emitted record is valid JSON on its own line (no partial writes, no concatenation) |
| `test_rotation_triggers_at_configured_size` | After emitting records totalling > max_bytes, `yadgar.log.1` exists; active file size < max_bytes |
| `test_backup_count_respected` | After N+2 rotations, only `backup_count` backup files remain on disk |
| `test_dual_sink_coexistence` | With both stdout and file handlers installed, one emit → one record in each sink; no duplication count mismatch |
| `test_graceful_fallback_on_unwritable_path` | `YADGAR_LOG_FILE_PATH=/nonexistent/path/yadgar.log` → configure_logging() does not raise; stdout handler still active; one warning emitted to stdout |
| `test_opt_out_empty_path` | `YADGAR_LOG_FILE_PATH=""` → file handler not installed at all (O(1) I3 compliance) |
| `test_json_schema_preserved_in_file` | Records in file include required I14 fields: `ts`, `level`, `component`, `action`, `outcome` |
| `test_rotation_counter_metric_incremented` | `yadgar_log_file_rotations_total` increases by 1 per doRollover call |
| `test_rate_limiter_drops_at_expected_rate` (conditional) | If rate limiter enabled: emit 100 records/sec → only rate × window records pass filter; `yadgar_log_dropped_total` reflects drops |
| `test_idempotent_handler_install` | Calling `configure_logging()` twice does not stack duplicate file handlers |

Note: Python's `RotatingFileHandler` has stdlib-level thread-safety for concurrent emits. No need to test that; test that the handler can be installed alongside stdlib handler concurrency (existing stdlib coverage sufficient).

---

## 11. Out of Scope for v5.5.1

- Log shipping (Loki/Splunk/Grafana Cloud)
- Structured log search / indexing
- Log encryption at rest
- Multi-host aggregation
- Async log write pipeline with queue + worker (deferred to v5.6 — see §3 Alt 4)
- Per-user or per-session log partitioning
- Log compression (`.gz` rotation — stdlib `RotatingFileHandler` supports this; deferred since it adds complexity without changing disk bound)

---

## 12. Open Questions for Human Decision

These four points block implementation design. Answers needed before v5.5.1 implementation dispatch.

**Q1: Default cap size — 500 MB total (5 × 100 MB) — acceptable?**

Justification: covers ~10–25 days of normal INFO-level traffic; covers ~1 day of worst-case DEBUG. Trade-off: if `/data/` mount is constrained (small homeDir partition), 500 MB may be too large. Alternative: 3 × 50 MB = 150 MB total (7–12 days INFO retention, less generous).

**Q2: Rate limiter — ship in v5.5.1 or defer to v5.5.2?**

In scope adds ~100 LOC + 1 test class. Benefit: immediate flood protection. Risk: adds complexity to an already feature-heavy v5.5.1. Deferring keeps v5.5.1 focused on rotation only.

**Q3: Backend env var prefix — shared `YADGAR_LOG_*` with a `YADGAR_BACKEND_LOG_*` override, or fully separate prefixes?**

Option A (recommended): `YADGAR_LOG_FILE_PATH` for core; `YADGAR_BACKEND_LOG_FILE_PATH` for backend. Clean separation.
Option B: both use same `YADGAR_LOG_FILE_PATH` pointing to same file (simpler, interleaved output).
Option C: fully separate prefixes for every backend log setting (`YADGAR_BACKEND_LOG_FILE_MAX_BYTES`, etc.) — most explicit, most env-var sprawl.

**Q4: Log volume accumulation period — can you retrieve journald start date?**

`journalctl --user --list-boots` or `journalctl --user -u yadgar --since "60 days ago" | wc -l` would confirm whether 1.3 GB is 7 days or 90 days of traffic. This informs whether 500 MB is overly generous or dangerously thin. If unknown, implementation proceeds with the bracketed estimate and the default can be adjusted post-deployment.

---

## 13. Estimated Effort

**Scope: v5.5.1 implementation phase**

| Component | Estimate | Notes |
|-----------|----------|-------|
| `yadgar/log_config.py` — add file handler setup | ~60–80 LOC | `_configure_file_handler()` helper + env-var read + graceful fallback |
| `yadgar/log_config.py` — metric hooks (rotation counter, size gauge) | ~40–60 LOC | Subclass `RotatingFileHandler`, override `doRollover`, register prometheus counter |
| `yadgar/embed_service.py` — wire backend Sink B | ~15–25 LOC | Call extended `configure_logging()` with backend path default |
| `yadgar/tests/test_log_rotation.py` | ~150–200 LOC | 10 test cases (§10) |
| `nix/` — `tmpfiles.rules` for `~/.yadgar/logs/` | ~3–5 LOC | Dir creation at activation |
| `MIGRATION_NOTES.md` updates | ~20 LOC | Journald cap recommendation + bind-mount note |
| Rate limiter (if Q2 = ship v5.5.1) | +80–100 LOC + 1 test class | Optional; scoped separately |

**Total (without rate limiter):** ~270–370 LOC + nix. Well within I13 file caps if spread across 2–3 files.

**Agent sizing:** single `cavecrew-builder` dispatch for `log_config.py` + `embed_service.py` changes; separate dispatch for test file; separate dispatch for nix change. Rate limiter if approved = fourth dispatch.

---

## Appendix: Candidate Log Paths

| Context | Default path inside container | Host path (via bind mount) |
|---------|------------------------------|---------------------------|
| Core | `/data/logs/yadgar.log` | `~/.yadgar/logs/yadgar.log` |
| Backend | `/data/logs/backend.log` | `~/.yadgar/logs/backend.log` |

Both paths rooted in the same `/data` bind mount. Rotation files: `yadgar.log.1` … `yadgar.log.5`.
