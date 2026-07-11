# `_shared/observability/` — tri-signal observability

Spans + metrics + logs (I33 ratchet: every non-exempt function needs a span
source; `scripts/check_observe_coverage.py`).

- `observe.py` — the `@observe` decorator (span+metric+log per call)
- `tracing.py` — OTLP tracing, `trace_span`, circuit-breaker exporter
- `metrics.py` — every Prometheus collector + `/metrics` handler (I23:
  each metric needs a writer; `check_metric_writers.py`)
- `log_config.py` — JSON logging, redaction, rotation, request middleware
  (categorically exempt from @observe — log-path re-entry floods, see
  observe-allowlist rationale)
- `exception_telemetry.py` — `record_exception` counter

Both layers import this; keep it dependency-light (no storage, no server).
