# PLAN — observability fixes: health truthfulness + OTLP resilience

Status: **TRAIN — "observability-train" (proposed v5.83; renumber at ship). PLANNED 2026-06-24.** Keep OTLP + Prometheus metrics ENABLED. Triggered by: core flagged `unhealthy` while actually serving + a 14h OTLP retry-flood (collector absent).

theme: observability / health / tracing
priority: C1 (P0) has a HIGH-severity masking bug; rest MEDIUM/LOW.

## Train cars (sequenced)
- **C1 = task #15 (P0):** `/health` masking → 503-on-degraded + anti-flap. HIGH. Ship first; standalone.
- **C2 = task #16 (P1–P3):** health handler concurrent-probe + outer timeout; OTLP span-logging off the event-loop (QueueHandler); OTLP circuit-breaker + rate-limited logging; consume/remove dead `OTLP_INSECURE`. After C1.
- **C3 = task #17 (P4):** add otel/Tempo collector at `:4318` (nix) + healthcheck param tune. Nix/ops; last (or parallel).
Sequence: C1 → C2 → C3. C1 is independently shippable; C3 is the only one that touches nix.

## Root causes (verified, file:line)
- **Health is LIVE-probe, not cached** (`server/http.py:246`; fresh `httpx.AsyncClient(timeout=2.0)`, sequential db+embed). No stale-cache bug. (`_health_cache` at `viz_daemon_health.py:59` is a DIFFERENT endpoint.)
- **HIGH — masking false-negative:** handler returns `JSONResponse(200)` even when `status=="degraded"` (`http.py ~290-298`); container check is `curl -f` (fails only ≥400) → a real db/embed outage is reported **healthy**. Healthcheck structurally cannot detect a dependency outage.
- **Health "staleness" = LAG, not stale data:** podman flips `unhealthy` only after ~60-90s sustained failures (30s interval × 3 retries), clears on one pass (no latch). The observed "unhealthy then instantly fine" = a ~1.5-min stall episode that self-cleared; flag lagged ~90s. Handler can stall up to ~4s (two serial 2s probes, no outer timeout, `http.py ~268-288`) — under the 5s `--health-timeout` but zero margin.
- **OTLP flood (MEDIUM):** `OTLPSpanExporter`+`BatchSpanProcessor` (`tracing.py:280-317`) export OFF the event-loop (doesn't block requests), BUT no app-level circuit-breaker + no rate-limit on retry logging → logs every failed batch on the SDK's 5s cadence forever. `OTLP_TIMEOUT_SEC=3` fast-fails each attempt. `OTLP_INSECURE` (`config.py:115`) is a declared **no-op** (never consumed).
- **No collector exists:** `:4318`/Tempo/otel-collector absent from `~/git/nix` + `~/git/yadgar` compose. `host.containers.internal:4318` is hardcoded prod endpoint with nothing listening.
- **Plausible A↔B link (HYPOTHESIS):** `_emit_span_log` runs a synchronous stdlib `logger.info` per span on the span-ending (event-loop) thread (`tracing.py:235-242`); shares the logging-handler lock with the OTLP-worker flood → stdout backpressure could stall the event loop → the ~90s health stall. Confirm by correlating flood-burst vs healthcheck-failure timestamps.
- **Ruled out:** CPU throttle (cgroup `cpu.stat`: 17ms throttled over 15h); event-loop blocking by export (off-thread); metrics coupling (`/metrics` uses a private registry, `metrics.py:46` — independent, safe).

## Fixes (OTLP + metrics stay ON)

### P0 — health masking (HIGH, do first) — code `server/http.py`
- Return **HTTP 503 when `status=="degraded"`** so `curl -f` actually fails on a real db/embed outage. **Anti-flap:** require N consecutive degraded probes (or lean on the 3-retry healthcheck) so transient blips don't flap. Closes the dangerous outage-masking. TDD.

### P1 — health handler robustness — code `server/http.py`
- Probe db + embed **concurrently** (`asyncio.gather`, ~2s not 4s) + wrap handler in `asyncio.wait_for(~3s)` outer bound. Removes the tight-margin-vs-5s stall risk.

### P2 — OTLP span-logging off the event-loop — code `tracing.py`
- Route `_emit_span_log`'s `logger.info` through `logging.handlers.QueueHandler` + `QueueListener` so per-span logging never blocks the event-loop thread. Kills the plausible stall link.

### P3 — OTLP exporter resilience — code `tracing.py`
- App-level **circuit-breaker**: after K consecutive export failures, back off to a long re-probe interval (e.g. 60s) instead of hammering every 5s; **rate-limit failure logging** (once per window, not per batch); graceful degrade (buffer/drop silently) when collector absent. Keep OTLP ON.
- Consume `OTLP_INSECURE` in `_build_otlp_exporter()` or remove the dead knob.

### P4 — healthcheck params + collector — config (nix) + ops
- `modules/home/yadgar.nix`: with P1 bounding the handler, 5s timeout is OK; optionally raise `--health-timeout` margin / lower `--health-interval` for faster lag-recovery.
- **Run a collector at `:4318`** (Tempo/otel-collector — add to nix) so OTLP actually lands somewhere (user wants tracing). With P3 in place this is non-urgent but needed for OTLP to be USEFUL.

## Severity / order
1. **P0 masking — HIGH** (can hide a real outage indefinitely). Ship first.
2. P2+P3 OTLP — MEDIUM (spam + wasted cycles + the stall hypothesis).
3. P1 handler robustness — MEDIUM.
4. P4 params/collector — LOW/ops.

## Related
`server/http.py:246`, `tracing.py:160/227/235/280-317`, `config.py:112-118`, `metrics.py:46`, `modules/home/yadgar.nix` (healthcheck). [[yadgar-architectural-invariants]] (I14 logging, observability). [[yadgar-roadmap-future-improvements]].
