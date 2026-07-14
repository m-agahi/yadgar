> ARCHIVED 2026-07-14 — tri-signal @observe STANDARD complete (P0–P6 shipped v5.101–v5.105, I33 lint 1564 MISSING → 0 GLOBAL hard-fail, backend fine-spans shipped, closes #8). Sole remaining phase P-SB (§5b, ADR-0074) EXTRACTED to docs/plans/psb-span-budget-hot-loop-2026-07-14.md (train feat/stophook-tasklist-train).

# Full-Observability Standard — span + metric + log on every function, tiered + enforced

**Status:** STANDARD COMPLETE — full tri-signal `@observe` rollout shipped across waves P0–P6 (v5.101 P0 scaffolding → v5.105 P1–P6, ADR-0034, closes #8). I33 coverage lint went **1564 MISSING → 0** and is now **GLOBAL HARD-FAIL** (`check_observe_coverage.py` runs with NO `--warn`/`--area` in both `.pre-commit-config.yaml` and `.forgejo/workflows/ci-pr.yaml`). The per-area-flip rollout table (old §5) is therefore OBSOLETE — every area is already at hard-fail. **Sole remaining work: Phase P-SB** (§5b) — I33 v2 span-budget refinement + hot-loop sweep (ADR-0074 ACCEPTED 2026-07-09), sequenced AFTER recall-3-train T3 (Ettin swap). Backend fine-spans already shipped in wave P3 (backend instrumented, `BACKEND_VERSION` 5.10.0→5.11.0 at v5.105; now 5.33.0) — NOT remaining work. **Refreshed:** 2026-07-09 (post folder-split reorg R2a/R2b + I33 hard-flip). **Original date:** 2026-07-03.
**2026-07-13 note (NOT archived):** the recall program is complete (T4 Ettin shipped, core 5.132.0 / backend 5.43.0). Phase P-SB (§5b) — its sole gate — is therefore now **unblocked and actionable**, so this plan stays live rather than being archived with the shipped post-T4 plans.
**Author:** agent (bot). **Branch:** `docs/full-observability-plan` (original); refresh on `master`.
**Directive (verbatim):** *"every function emits a trace span + a metric + a
structured log — unless there is a documented, categorized reason not to."*
**Scope:** yadgar-core, yadgar-backend, MCP tools, hooks. After this lands,
NOTHING is un-instrumented without an explicit, categorized exemption.

---

## TL;DR — the recommendation up front

1. **The literal directive is right in spirit, wrong if taken naively.** There are
   **~1,626 functions** in scope (see Inventory §1). A duration histogram on every
   one, at ~12 buckets each, is **~19,500 Prometheus series from a single metric
   type** — a cardinality bomb that will OOM the scrape and tell you nothing. A span
   per function in the recall hot loop blows the warm-recall floor
   (`wiki:yadgar-adr-log` ADR-0026/0030/0031; the floor has since moved — hot recall
   ~4.1s at the 2026-07-09 recall-3-train baseline, backend-CE-bound). A per-function
   INFO log is noise that drowns the signal. **So the deliverable is a STANDARD +
   EXEMPTION POLICY + ENFORCEMENT RATCHET — not a blind span+metric+log sweep.** (This
   thesis was vindicated: the tiered rollout shipped with NO measurable recall
   slowdown, v5.106 — see §3.6; and the ADR-0074 span-budget amendment §5b confirms
   the hot-loop danger was real — an un-budgeted sweep DID storm 42k spans/op.)

2. **The mechanism is one decorator, `@observe(tier=...)`, that composes the three
   existing signal paths** (`@trace_span` for the span, a bounded metrics helper,
   the I14 JSON logger) and emits **by TIER**. Four tiers (§3): `boundary` (full
   RED: span+metric+log), `stage` (span + one shared histogram, log-on-error),
   `hot` (attribute/count only — NO per-call span/metric/log), `exempt`
   (categorized, allowlisted). The tier IS the "documented reason" the directive
   demands.

3. **`@observe` must NOT double-instrument.** The MCP tool surface already gets
   `tool.<name>` spans via `@_tool`; `/rerank` gets `_rpc_span`, `/embed` gets
   `@trace_span("rpc.embed")`; httpx + FastAPI are auto-instrumented. The decorator
   detects an already-active span source and **suppresses its own span** (span
   requirement satisfied-by-existing). The coverage lint counts
   `@_tool`/`@trace_span`/`@observe` all as "span present."

4. **The task's headline P0 — "core→backend traceparent is NOT propagated" — is
   FALSE against observed code.** `opentelemetry-instrumentation-httpx>=0.51b0` IS a
   declared, resolved dependency (`pyproject.toml`, `uv.lock` → 0.63b1). At refresh
   time (post-reorg) `HTTPXClientInstrumentor().instrument()` runs inside
   `setup_tracing()` (`yadgar/_shared/tracing.py:521-533`, the `_instrument_httpx`
   helper called from `setup_tracing` at `:540`) — the v5.101 R2 hoist that closed
   the stdio/daemon hole is DONE (see §2.2); backend has `FastAPIInstrumentor`
   (`yadgar/backend/embed_service.py:736`); **both** backend calls are explicitly
   span-wrapped (`/rerank` → `_rpc_span` at `yadgar/backend/ml_client.py:785-800`;
   `/embed` → `@trace_span()` at `yadgar/_shared/remote_embeddings.py:58`).
   **Propagation is already wired** and the E2E verification + stdio/daemon hoist
   both shipped in P0. Later executors: do not trust the original "KNOWN GAP"
   framing — it was a task-prompt assumption, refuted by grep and since fully
   closed.

5. **The ratchet is the invariant lint `I33 / check_observe_coverage.py`** (shipped —
   the plan originally called it "I34"; the landed invariant number is **I33**) that
   AST-walks every function, classifies it (needs-`@observe` / auto-trivial /
   allowlisted-exempt), and FAILS if a non-exempt function lacks a span source AND is
   not in `.observe-allowlist.json`. Modeled 1:1 on the existing
   `check_trace_spans.py` (I24) + `.complexity-allowlist.json` (I30) machinery. It
   shipped warn-mode first (v5.101 P0) then flipped to **GLOBAL hard-fail** at v5.105
   once every area reached 0-MISSING. **Without this lint the standard is a one-time
   sweep that rots** — it is now closed and enforcing.

---

## 1. Inventory — the "list everything" step

> **HISTORICAL BASELINE — 2026-07-03, PRE-REORG.** The table below is the original
> pre-folder-split (`_shared/`+`core/`+`backend/`) inventory that motivated the
> standard. Do NOT treat its paths/counts as current. **Current authoritative
> coverage figure:** the I33 lint went **1564 MISSING → 0** and is GLOBAL hard-fail
> (CHANGELOG v5.105) — every in-scope function is now either instrumented or carries
> a governed exemption. Re-deriving the ~1,626 raw AST count post-reorg was
> deliberately skipped (task-scoped ≤15 min); the 0-MISSING lint state is the
> truthful current-state signal, not a fresh grep.

Counts are AST-derived (`ast.FunctionDef` + `AsyncFunctionDef`) and cross-checked
with ripgrep, over `/home/max/git/yadgar/yadgar/`. Instrumentation columns count
existing span sources (`@trace_span` / `with span(` / `start_as_current_span` /
`@_tool` / `_rpc_span`), metric write call-sites (`.observe(` / `.inc(` / `.set(` /
`.labels(`), and structured-log call-sites (`logger.{info,debug,warning,error}`).

| Area | Files | #Functions | Public | Private | Traced (spans) | Metric call-sites | Log call-sites |
|---|--:|--:|--:|--:|--:|--:|--:|
| Core top-level (`yadgar/*.py`) | 47 | 600 | 277 | 323 | 15 | 27 | 14 |
| Retrieval (`retrieval/`) | 35 | 160 | 49 | 111 | 8 | 6 | 15 |
| Consolidation (`consolidation/`) | 7 | 67 | 6 | 61 | 1 | 7 | 57 |
| Storage (`storage/`) | 23 | 310 | 214 | 96 | 57 | 7 | 1 |
| File queue (`file_queue/`) | 4 | 57 | 17 | 40 | 2 | 11 | 24 |
| Wiki (`repo_wiki/`) | 2 | 18 | 4 | 14 | 0 | 0 | 0 |
| Backend (`backend/`) | 4 | 77 | 34 | 43 | 4 (+`_rpc_span`×3) | 36 | 38 |
| Server http/app/offload | 3 | 92 | 38 | 54 | 32 | 20 | 188 |
| MCP tools (`server/tools/`) | 21 | 245 | 84 | 161 | 83 (`@_tool`) | 6 | — |
| **TOTAL** | **~146** | **~1,626** | **~723** | **~903** | **~119** | **~114** | **~466** |

**Reading the table:**
- **Span coverage is ~7% of functions** (119/1626) and heavily skewed: storage (57)
  and server (32) carry half; core-top-level has 15 (all in `tracing.py`/`metrics.py`
  themselves); `repo_wiki/` has zero. MCP tools get 83 boundary spans via `@_tool`
  (this is the *tool RPC boundary*, exactly the tier we want — no change needed).
- **Metrics: ~114 write call-sites against 104 declared metric objects** (83 core in
  `metrics.py`, 21 backend in `embed_service_metrics.py`). Many declared metrics have
  zero writers (stubs) — I23 (`check_metric_writers.py`) already gates this.
- **Logs: 466 call-sites, 69 files.** Concentrated in server (188) + consolidation
  (57) + backend (38) + file_queue (24). Storage/core-transform code is nearly
  silent (storage: 1). Lifecycle/IO code logs; data-transform code does not — a
  reasonable existing pattern the standard should preserve, not invert.

**Version anchors (CURRENT, refreshed 2026-07-09):** core `pyproject.toml` →
`5.120.1` (was 5.99.0 at plan authoring; 5.101 at P0, 5.105 at standard-complete);
backend `yadgar/__init__.py:21` → `BACKEND_VERSION = "5.33.0"` (was 5.10.0 at plan
authoring; bumped to 5.11.0 at P3/v5.105 when backend was instrumented, since
advanced to 5.33.0 by later trains). Backend-input changes require a
`BACKEND_VERSION` bump (gated by `check_backend_bump.py --ci`, #83).

---

## 2. Current-state analysis — the "analyse what's implemented" step

### 2.1 Tracing (`yadgar/_shared/tracing.py`)
Three span idioms (source: `wiki:Yadgar OTEL Tracing — Span Mechanism & Coverage`,
PR #148 v5.100; paths below refreshed to post-reorg `_shared/` layout):
- **`@trace_span(name=None, attributes=...)`** (`tracing.py:665`) — wraps sync/async,
  records exception + `status=ERROR` on raise, no-op identity decorator when OTel
  absent. Since R2b (v5.116) the name defaults to `module.qualname` (dynamic span
  naming, enforced by `check_dynamic_span_names.py`). **This is the codebase idiom —
  decorate extracted stage methods.**
- **`span("name", **attrs)`** inline CM (`tracing.py:748`, v5.100) — for genuinely
  inline blocks; no-ops to `nullcontext`.
- **`get_tracer(...).start_as_current_span`** — single-use generator CM (drainer).
  Enter EXACTLY once via `with`; double-enter → `RuntimeError`.

**Export is async and safe:** `setup_tracing` (`tracing.py:540`) registers
`LogSpanProcessor` (`:557`, always on) and `BatchSpanProcessor` (`:564`, opt-in via
`YADGAR_OTLP_ENDPOINT`) — off the event loop. `LogSpanProcessor` emits one I14 JSON
line per span, routed off the loop via a QueueHandler/QueueListener (C2 P2). Spans
add no blocking I/O.

**Context propagation across the offload boundary:** `@_tool()`
(`yadgar/core/server/_app.py:347`) wraps every tool in `@trace_span("tool.<name>")`
(`_app.py:380`). When `OFFLOAD_TOOLS=True`, `run_offloaded`
(`yadgar/_shared/runtime/offload.py`, called at `_app.py:474`) uses
`contextvars.copy_context()`, which captures the parent span — inner spans nest
correctly on the worker. No per-stage plumbing.

**v5.100 already added ~40 stage spans** across recall/write/consolidation/drainer/
wiki/checkpoint/storage. `recall()` and `_apply_rerank_pipeline` are I13-HARD-capped
— do NOT add nested `with span()` there; decorate the extracted stage methods.

### 2.2 Core→backend propagation — VERIFIED WIRED (task premise refuted; paths refreshed post-reorg)
| Fact | Evidence (current paths, 2026-07-09) |
|---|---|
| httpx auto-instrumentor is a real dep | `pyproject.toml`: `opentelemetry-instrumentation-httpx>=0.51b0`; `uv.lock` → 0.63b1 |
| It's activated inside `setup_tracing()` (R2 hoist — DONE) | `yadgar/_shared/tracing.py:521-533` (`_instrument_httpx` helper) called from `setup_tracing` (`:540`); `_app.py:38` comment confirms "activated INSIDE setup_tracing() (v5.101 R2)". Every entry mode (stdio/daemon incl.) now gets it. |
| RemoteMLClient uses a plain httpx.Client (auto-injected) | `yadgar/backend/ml_client.py:672` |
| `/rerank` has an explicit span | `_rpc_span("rpc.rerank.{ce,nli,pair}")` — `yadgar/backend/ml_client.py:785-800` |
| `/embed` has an explicit span | `@trace_span()` + `httpx.Client` — `yadgar/_shared/remote_embeddings.py:58` |
| Backend extracts incoming traceparent | `FastAPIInstrumentor` (`_FAI`) — `yadgar/backend/embed_service.py:736` |

**Residual gaps — BOTH CLOSED in P0 (v5.101):**
- **(R1) No end-to-end assertion.** → P0 added the traceparent E2E verification test.
- **(R2) stdio/daemon-only entry paths.** → P0 hoisted `HTTPXClientInstrumentor`
  instrumentation into `setup_tracing()` itself (`tracing.py:521-533`, the single
  choke-point), so every entry mode gets it. No longer a residual gap — retained
  here as the record of what was fixed.

### 2.3 Metrics (`yadgar/_shared/metrics.py` + `yadgar/backend/embed_service_metrics.py`)
- **83 core + 21 backend** Prometheus objects (Counter/Histogram/Gauge/Summary),
  **private registry** (independent of OTLP, safe — ADR-0001).
- Naming convention observed: `yadgar_<subsystem>_<measure>_<unit>` (e.g.
  `yadgar_recall_duration_seconds`, `yadgar_shadow_cache_*`, `yadgar_tool_pool_*`).
  Backend uses its own isolated registry.
- **I23 (`check_metric_writers.py`)** already gates "every declared metric has ≥1
  writer" (allowlist: `yadgar_subagent_capture_rate` = intentional zero).

### 2.4 Structured logs (I14, `yadgar/_shared/log_config.py`)
JSON logger, default in production. Fields: `ts`, `level`, `component`, `action`,
`outcome`, `event`, `latency_ms?`, `error?`, `traceback?`. `ContentRedactor` filter
(v5.4.7) strips secrets from `extra=`. **`trace_id` is already emitted per span line**
(LogSpanProcessor), so log↔trace correlation exists — the standard leans on it
instead of duplicating per-function logs.

**Post-rollout amendment (v5.106, ADR-0041):** the ENTIRE log-emission subsystem is
categorically `@observe`-EXEMPT (`framework-instrumented`). Under real OTLP, an
`@observe` on a log-emission fn opens a span → `LogSpanProcessor` emits a `span_end`
log line → re-enters the observed log path → per-log amplification flood (crash-looped
core+backend at v5.105). Fix: path-glob exempted `yadgar/_shared/log_config.py` +
`observe.py`/`timing.py` in `.observe-allowlist.json._exempt_globs`, and a
`_SpanEndFilter` on `LogRingHandler` drops `span_end` records (ADR-0041, 3rd
occurrence). The 4th ADR-0041 occurrence (`_ring_append`, `logs.py:59`) was fixed via
`@observe(span=False)` in #173 — precedent the P-SB span-budget phase (§5b) builds on.

### 2.5 OTLP export (live)
`BatchSpanProcessor` async → OTLP → **Tempo (PLT stack on nixos-quinyx)**; Grafana
Tempo dashboard live (ADR-0001, `wiki:yadgar-adr-log`). Metrics scraped from the
private registry endpoint.

### 2.6 Existing enforcement (the pattern to copy)
| Lint | File | Checks | Allowlist |
|---|---|---|---|
| I24 | `scripts/check_trace_spans.py` | `@trace_span` on public handlers in `server/http.py` only | `--allowlist fn1,fn2` |
| I23 | `scripts/check_metric_writers.py` | every declared metric has a writer | code allowlist |
| I30 | `scripts/check_complexity_allowlist.py` | HARD complexity violations gated + ≥40-char rationale + no-stale + ±20% drift | `.complexity-allowlist.json` (`{path,function,metric}` → `{rationale, metrics}`) |
| I32 | `scripts/check_capability_coverage.py` | Settings/tools/migrations/BC rows all have registry entries | `CAPABILITY_REGISTRY.md` status enum |

Wired in `.pre-commit-config.yaml` (local hooks) + `.forgejo/workflows/ci-pr.yaml`
(`invariant-checks` job). **`check_observe_coverage.py` (I33) shipped into both, same
shape — now runs with NO `--warn`/`--area` (global hard-fail, MISSING=0 enforced).**
`scripts/check_trace_spans.py` (I24) is now scoped to `yadgar/core/server/http.py`.

---

## 3. The STANDARD + method decision — the "decide best method and standard" step

### 3.1 The mechanism: one decorator, `@observe`
`@observe(...)` lives in `yadgar/_shared/observability/observe.py` (SHIPPED v5.101;
308 lines; keeps `tracing.py` single-purpose). It **composes**, not replaces, the
existing signal paths. The shipped signature added a `span: bool = True` param (see
§5b) beyond the design sketch below:

```python
def observe(
    *,
    tier: Literal["boundary", "stage", "hot"] = "stage",
    name: str | None = None,          # span/metric name; default = f"{module}.{qualname}"
    metric: str | None = None,        # bounded metric key (default = name); ignored for tier="hot"
    log_event: str | None = None,     # I14 `event`/`action`; boundary logs INFO, all log ERROR on raise
    attributes: dict | None = None,   # static span attributes (small ints/short strings only)
    exempt: str | None = None,        # if set → NO-OP passthrough; category recorded (see §3.5)
) -> Callable: ...
```

**Emission by tier:**

| Tier | Span | Metric | Log |
|---|---|---|---|
| `boundary` | full span (unless already span-sourced — §3.2) | RED: `..._requests_total{outcome}` counter + `..._duration_seconds` histogram, keyed by fn (bounded — §3.3) | INFO on entry-summary/exit with `latency_ms`; ERROR on raise |
| `stage` | span (unless already-sourced) | ONE shared `yadgar_stage_duration_seconds{stage="<name>"}` histogram (single metric family, bounded label) + shared `yadgar_stage_errors_total{stage}` | DEBUG entry/exit (gated); ERROR on raise |
| `hot` | NO span. Records `count`/size as an **attribute on the enclosing span**. | NONE per call. | NONE (ERROR only if it raises, via caller). |
| `exempt` | none | none | none |

**Rationale for composing over rewriting:** `@trace_span` already handles
sync/async + exception→`status=ERROR` + OTel-absent no-op correctly. `@observe`
delegates the span to it, adds the metric + log wrappers around it. Zero duplication
of the hard-won error/async logic.

### 3.2 Double-instrumentation guard (load-bearing)
`@observe` MUST NOT create a second span when one already exists for the function:
- If the function is already decorated with `@_tool` or `@trace_span`, OR called
  under an auto-instrumented boundary (httpx client span, FastAPI request span),
  the span requirement is **satisfied-by-existing**.
- Implementation: `@observe` checks at call time via
  `opentelemetry.trace.get_current_span()`; if a *non-recording*/absent span → it
  opens its own; if the function itself carries `@trace_span`/`@_tool` (detected by
  the lint statically, and by a sentinel attribute the decorators set) → `@observe`
  runs in **metric+log-only** mode.
- **Net rule for the whole tool surface:** `@_tool` stays the span source for the
  245 tool functions. `@observe(tier="boundary")` is redundant there and MUST NOT be
  stacked — the lint counts `@_tool` as satisfying coverage. Same for `/rerank`
  (`_rpc_span`) and `/embed` (`@trace_span`).

### 3.3 Metrics — the anti-cardinality design (the arithmetic)
- **Naive (rejected):** one duration histogram per function × ~12 buckets ×
  ~1,626 functions ≈ **~19,500 series** from one metric type. Add a call counter and
  error counter and it triples. This is the cardinality bomb; it is **explicitly
  rejected**.
- **Chosen surface:**
  - **RED at boundary tier ONLY.** Boundary set = **83 `@_tool` + ~38 public HTTP
    handlers ≈ ~120 entrypoints**. Per entrypoint: 1 duration histogram (name-keyed,
    NOT labelled by fn) + 1 requests counter with a **bounded `outcome` label**
    (`ok|error`). Series ≈ `120 × (12 buckets + 2 counter series) ≈ ~1,680`.
    Manageable, and it's the RED signal you actually dashboard.
  - **Stage tier → ONE histogram family** `yadgar_stage_duration_seconds` with a
    single **bounded `stage` label**. The label cardinality = number of *distinct
    stage names*, which is bounded by the count of `@observe(tier="stage")` sites
    (target ≤ ~200). Series ≈ `~200 × 12 ≈ ~2,400`, from ONE metric family, not
    one-per-function. Plus `yadgar_stage_errors_total{stage}` ≈ ~200.
  - **Hot tier → zero metrics.** Size/count recorded as span attributes only.
- **Total incremental series ceiling: ~6,500** (boundary ~1,680 + stage ~2,600 +
  headroom), vs ~19,500 for the naive floor of a single metric type. **The lint (§4)
  forbids introducing a new per-function metric object** — stage functions MUST use
  the shared family, enforced by rejecting new `Histogram(...)` definitions outside
  an allowlist.

### 3.4 Logs — anti-spam design
- **Boundary:** INFO one line on completion (`component`, `action=log_event`,
  `outcome`, `latency_ms`) + ERROR on raise. No per-call entry INFO.
- **Stage:** DEBUG entry/exit (off at prod `WARNING` default level, so zero prod
  volume) + ERROR on raise.
- **Hot:** nothing.
- **Correlation:** every line already carries `trace_id` (LogSpanProcessor) — do NOT
  duplicate call args into logs; point at the span attributes.
- **Volume math (one warm recall):** boundary lines = 1 (the `recall` tool) +
  1 backend `/rerank` + 1 `/embed` ≈ **3 INFO lines**. Stage DEBUG lines are
  suppressed at prod level → **0**. Error lines only on failure. So a clean recall
  emits ~3 app-log lines (plus the async span-log lines, already off the event loop).
  Contrast a naive per-function INFO: ~40+ lines/recall. **~13× reduction.**

### 3.5 Exemption categories — the "unless a reason" the directive demands
A function is exempt (no signals) ONLY for one of these **categorized** reasons.
Category is recorded either as `@observe(exempt="<category>")` (co-located) OR as a
`.observe-allowlist.json` entry (bulk/trivial). **The allowlist file is the single
source of truth the lint reads** (§4) — the decorator arg is a convenience that the
lint also honors via the sentinel attribute.

| Category | Definition | Declared via |
|---|---|---|
| `trivial` | ≤3 statements, no branches/loops, no I/O, no raise (pure getter/formatter) | auto-detected by lint (no entry needed) |
| `property` | `@property` / `@cached_property` / descriptor | auto-detected |
| `dunder` | `__init__`, `__repr__`, `__eq__`, … | auto-detected |
| `hot-loop` | inner fn called per-item in a hot loop (span/metric per call = bloat) | allowlist entry + rationale |
| `generated` | codegen / migration boilerplate | allowlist (path glob) |
| `test` | anything under `tests/` | path-excluded by lint |
| `framework-instrumented` | span comes from `@_tool`/`_rpc_span`/httpx/FastAPI | auto-detected (sentinel) |

**Rationale for allowlist-file as the singular source of truth** (over
decorator-only): mirrors the proven `.complexity-allowlist.json` (I30) /
`check_trace_spans.py --allowlist` pattern already in the repo; lets the lint verify
exemptions **without importing every trivial function** (pure AST, no runtime); makes
the exemption set diffable and reviewable in one place; and gives every non-obvious
exemption a mandatory ≥40-char `rationale` field (same as I30). The `exempt=` arg
stays legal for co-located clarity but is a mirror, not a second registry.

### 3.6 The no-slowness budget
- **Ceiling:** warm recall stays at its measured floor. **SETTLED (2026-07-09):** the
  `@observe` overhead question was independently resolved — **NO measurable slowdown**
  from the tri-signal rollout (fee2f129, "v5.106 recall perf — @observe overhead
  verdict = NO measurable slowdown", record-only loadtest in `benchmarks/`, #160). So
  the tiering held as designed.
- **CAVEAT (still open):** ADR-0033 (**status: open**) reports a live recall slowdown
  of 24–76s whose cause is *not isolated* (backend/deploy vs core) — a SEPARATE issue
  from `@observe` overhead. The absolute warm floor has since moved (recall-3-train
  2026-07-09 baseline: cold ~24.6s, CE 3-pass ~19s, **hot ~4.1s** per op — the
  backend CE cost, not observe). Use these current numbers, not the historical ~1.6s.
  ADR-0033 no longer blocks the observe question specifically; it remains open for the
  general recall-latency baseline (recall-3-train overhaul, `docs/plans/archive/recall-3-train-overhaul-2026-07-04.md`).
- **How the tiering protects it:** the recall hot path is `stage` + `hot` tiers.
  Stage spans are already-extracted methods (zero added nesting; v5.100 rules). Hot
  tier adds ZERO spans/metrics/logs per item — only a small integer attribute on the
  enclosing span. No per-item span, no per-item metric, no per-item log anywhere on
  the hot path. The only per-op cost is ~3 boundary emissions, all off the event
  loop.

---

## 4. Enforcement — `I33 / check_observe_coverage.py` (the ratchet) — SHIPPED, GLOBAL HARD

The invariant lint (called **I33**, not "I34" as the draft named it), modeled on
`check_trace_spans.py` (I24) + I30's allowlist discipline. It is what makes "nothing
missed" durable rather than a decaying sweep. **Status: SHIPPED and now GLOBAL
hard-fail** — the classification algorithm and allowlist schema below are as-built.

### 4.1 Classification algorithm (pseudocode — the crux, stated unambiguously)
```
for each *.py file under yadgar/ (excluding tests/, scripts/ if configured):
    tree = ast.parse(file)
    for node in every FunctionDef / AsyncFunctionDef (incl. nested & methods):
        fq = f"{module}:{qualname}"           # stable key

        # ── auto-exempt (no annotation, no allowlist entry required) ──
        if name is dunder:                              -> EXEMPT(dunder);    continue
        if has @property/@cached_property/descriptor:   -> EXEMPT(property);  continue
        if is_trivial(node):                            -> EXEMPT(trivial);   continue
            # is_trivial := (len(body_statements) <= 3)
            #            and (no If/For/While/With/Try nodes)
            #            and (no Raise, no await, no Call to I/O sinks*)
            #            *I/O sinks = httpx/surreal/open/subprocess/logger — conservative deny

        # ── framework-instrumented span source counts as "span present" ──
        if has_decorator(node, {"_tool","trace_span","observe"}) \
           or wrapped_by(node, {"_rpc_span"}):           -> SATISFIED;        continue

        # ── explicit exemption ──
        if fq in observe_allowlist:                      -> validate_entry(fq); continue
            # validate: rationale >= 40 chars; category in ENUM; not stale (fn still exists)

        # ── otherwise: MISSING ──
        record MISSING(fq, tier_guess)                   # tier_guess for the fixer's convenience
```
- **`is_trivial` is the only fuzzy boundary — pin it hard:** ≤3 statements AND no
  control-flow nodes AND no raise/await AND no call to a conservative I/O denylist.
  This is deliberately strict (favours *requiring* `@observe` over silently
  exempting) so the ratchet can't be gamed by "it's just a helper."
- **Exit code:** non-zero if any `MISSING` (in hard-fail mode) or any allowlist entry
  is stale / lacks rationale / has an invalid category (always hard, like I30).

### 4.2 Modes & rollout wiring — ROLLOUT COMPLETE (global hard-fail)
The mode flags still exist in the script, but the rollout they drove is DONE:
- **`--warn`**: prints MISSING, exits 0. Used at P0 (v5.101) to establish the
  baseline. No longer used in CI/pre-commit.
- **`--area <name>`**: restricts scan to a path substring. Was the per-area-flip
  mechanism during P1–P6; **no longer used** — the lint now runs whole-repo hard.
- **CURRENT wiring (v5.105+):** `check_observe_coverage.py` runs with NO flags in
  both `.pre-commit-config.yaml` (`check-observe-coverage`, `files:
  ^(yadgar/.*\.py|\.observe-allowlist\.json)$`) and `.forgejo/workflows/ci-pr.yaml`
  (`invariant-checks` job) → **default hard-fail, MISSING=0, whole codebase**
  (closes #8). The ratchet is monotonic and now fully closed.
- **`.observe-allowlist.json`** schema (mirrors `.complexity-allowlist.json`); per-fn
  entries are keyed `module:qualname` → `{category, rationale}`, e.g.:
  ```json
  { "_reranking_mmr:_cosine_sim": {
        "category": "hot-loop",
        "rationale": "per-candidate scorer; span/metric per call = 50+/op bloat" } }
  ```
  Plus an `_exempt_globs` section (ADR-0040 option B): path-globs for CATEGORICALLY
  non-observable dirs/files only (CLI glue, seed/export/migration codegen,
  logging/observe/timing framework, pure-render presentation). MIXED-logic files
  (viz_server.py, graph_api.py) were pulled OUT of the glob and their real fns
  per-fn instrumented/exempted, so a new fn there is not auto-invisible.
- **Anti-stale (like I30 invariant c):** every allowlist key must map to a currently
  existing function; a removed function → hard fail (forces cleanup).

### 4.3 Where it plugs in — AS SHIPPED
- `.pre-commit-config.yaml`: local hook `check-observe-coverage`, `files:
  ^(yadgar/.*\.py|\.observe-allowlist\.json)$`, NO `args` (hard). (The draft's
  `args: [--warn]` was the P0-only state.)
- `.forgejo/workflows/ci-pr.yaml`: in the `invariant-checks` job alongside
  I23/I24/I25/etc. — step "Check I33 — tri-signal observe-coverage (HARD; MISSING=0
  enforced, closes #8)".
- `scripts/check_observe_coverage.py` + its tests (`test_live_codebase` asserting the
  script runs at 0-MISSING, plus unit tests on `is_trivial` / allowlist validation).

---

## 5. Phased rollout — ALL SHIPPED (P0–P6, v5.101→v5.105)

> **HISTORICAL — the rollout below is COMPLETE.** Every phase landed by v5.105
> (ADR-0034, closes #8). Paths in the "Key files" column are the ORIGINAL pre-reorg
> paths; the code now lives under `yadgar/_shared/`, `yadgar/core/`, `yadgar/backend/`.
> The "Version" and "Exit criteria" columns are updated to reflect what actually
> shipped. The remaining work is **§5b Phase P-SB only**.

Each phase was its own PR + version bump. Backend-touching phases bumped
`BACKEND_VERSION` (gated by `check_backend_bump.py --ci`, #83). The CHANGELOG (v5.105)
records the whole rollout landing as waves P1–P6; the draft's P4/P5 split was folded
into that wave sequence.

| Phase | Scope | Status / shipped as |
|---|---|---|
| **P0 — Standard + ratchet + propagation-verify** | `@observe` module (`yadgar/_shared/observability/observe.py`); `check_observe_coverage.py` (warn-mode); hoist `HTTPXClientInstrumentor` into `setup_tracing()` (R2); traceparent E2E test (R1); histogram p95 fix | ✅ **SHIPPED v5.101** (#150). Lint warn-mode green; R1+R2 both closed. |
| **P1 — Recall read path** | boundary on `recall`/`RetrievalPipeline.run`; all 26 `@trace_span("retrieval.*")` → `@observe(tier="stage")`; `hot`/hot-loop exemptions allowlisted | ✅ **SHIPPED v5.105** (#159, ADR-0034). Retrieval area 0-MISSING. |
| **P2 — Write / consolidation / drainer** | `stage` on memorize phases, WriteGate, drainer apply, consolidation phases | ✅ **SHIPPED v5.105** (wave P2). |
| **P3 — Backend** | RED on FastAPI endpoints; `stage` on rerank/embed internals; `_rpc_span`/`rpc.embed` kept as span sources | ✅ **SHIPPED v5.105** — `BACKEND_VERSION` 5.10.0→5.11.0 + image rebuild (backend instrumented). NOT remaining work. |
| **P4 — MCP tool surface** | all 22 MCP tools; `@_tool` counted as span source; metric+log-only `@observe` where RED needed; private helpers allowlisted | ✅ **SHIPPED v5.105** (wave). |
| **P5/P6 — Hooks + core-top-level + storage + root-service sweep + GLOBAL hard-fail** | hook endpoints; remaining core/storage/wiki/server/cognitive residual classified/allowlisted; lint flipped to global `--hard` | ✅ **SHIPPED v5.105** — **1564 MISSING → 0; lint GLOBAL hard-fail; ratchet CLOSED.** |

**Post-rollout hotfix (v5.106):** the log-emission path was exempted from `@observe`
(span→log→span amplification flood under real OTLP crash-looped core+backend) — see
§2.4 amendment. `@observe` overhead verdict on recall: **NO measurable slowdown**
(fee2f129, benchmarks/, #160).

---

## 5b. Phase P-SB — I33 v2 span-budget refinement + hot-loop sweep (ADR-0074 ACCEPTED 2026-07-09) — THE SOLE REMAINING PHASE

**Why:** the I33 coverage ratchet over-applied spans to hot-loop micro-helpers —
`audit_anchors` emitted ~42k `_cosine_similarity` spans, recall 27–35k per-row
`_row_to_dict`/`_extract_id` spans per op → OTLP queue saturation → BOUNDARY SPANS
DROPPED (`tool.audit_anchors` unfindable in Tempo). ADR-0074 sets the policy; this
phase makes the lint enforce it. **Order is load-bearing: refine I33 FIRST, then
sweep** — a sweep without the lint counterpart is a one-time fix that rots (the
same argument §TL;DR-5 makes for I33 itself).

**Commit 1 — I33 v2 (lint refinement):**
1. `.observe-allowlist.json` gains a `_span_budget` section: `fq → {rationale}`
   meaning "this fn must NOT open a per-call span". Lint HARD-FAILS if a listed fn
   carries a span-opening decorator without `span=False`. Same governance as
   existing sections: ≥40-char rationale, stale-entry hard-fail.
2. Advisory channel (non-failing, like the ADR-0040 glob-audit report): a
   span-decorated fn called inside a `For`/`While` body in the same module →
   stdout warning. Catches NEW hot-loop spans before they storm.
3. ADR-0041 hard rule: span-opening decorators forbidden in the logging-handler
   module set (small explicit file list — `log_config.py`, LogSpanProcessor
   module).
4. Widen `span=False` / `tier="hot"` semantics + docstrings (`observe.py:240-246`
   currently scopes `span=False` to the explicit-inner-span nesting case
   only; the hot-loop budget case is a second legitimate reason; `tier="hot"`
   doc wording "span only" (module docstring `observe.py:13`) is muddled — fix to
   "attributes on enclosing span, NO per-call span").

**Commit 2 — sweep (under the refined lint):**
- Populate `_span_budget` with the storm offenders: `_cosine_similarity`,
  `_row_to_dict`, `_extract_id`, plus grep `tier="stage"` inside loops for others.
- Flip them to `@observe(span=False)` (metrics only — the _ring_append/ADR-0041
  treatment, precedent #173) or decorator-level aggregation (one span with
  count+total) where an aggregate is genuinely useful.
- Verify: re-run the trace sweep on a deploy; `tool.audit_anchors` and recall
  boundary spans present in Tempo; per-op span count for audit_anchors/recall
  drops from tens-of-thousands to tens.

**Sequencing (user 2026-07-09):** queued AFTER recall T3 (Ettin swap) completes.
Independent car — touches lint + allowlist + hot helpers only; no conflict with
hardening train or prelude worktree if slots free up earlier, but the queue
position is after-Ettin unless user pulls it forward.

## 6. Risks + open questions

- **Contested baseline (ADR-0033, OPEN) — but the observe-overhead question is now
  SETTLED.** The `@observe` rollout was measured to add **no measurable slowdown**
  (v5.106, `benchmarks/`, #160), so the standard's own overhead risk is retired. The
  ADR-0033 contested baseline persists for the GENERAL recall-latency question (live
  spike 24–76s, cause not isolated) and is being worked in the recall-3-train
  overhaul (2026-07-09 baseline: cold ~24.6s, CE 3-pass ~19s, hot ~4.1s). Any FUTURE
  overhead gate (e.g. for §5b) must still A/B on the same deploy, not compare against
  a historical floor. Measurement method = the recall-perf warm-floor checklist, ≥6
  warm runs, median, same box, backend fixed.
- **Cardinality (quantified §3.3).** Chosen surface ~6,500 incremental series vs
  ~19,500 naive floor. Risk: stage-label cardinality creeps if executors invent new
  stage names freely. Mitigation: lint rejects new `Histogram(...)` objects outside
  the allowlist; stage names must reuse the shared family.
- **Log volume (quantified §3.4).** ~3 INFO lines/recall vs ~40+ naive. Risk: someone
  sets prod log level to DEBUG and the stage entry/exit lines flood. Mitigation:
  document that stage logs are DEBUG-gated by design; the volume math assumes prod
  `WARNING`.
- **Backend image rebuild cost (P3).** One `BACKEND_VERSION` bump + rebuild. Batch
  all backend instrumentation into P3 to pay the rebuild once.
- **Double-instrumentation (§3.2).** If the guard is wrong, the entire 245-fn tool
  surface + rerank/embed get double spans. Mitigation: guard is both static (lint
  counts `@_tool`/`_rpc_span` as the span source, forbids stacking `@observe` span
  there) and runtime (`get_current_span()` check). A unit test asserts a
  `@_tool`+`@observe` fn emits exactly one span.
- **OPEN — is per-function truly worth it for hooks & trivial core helpers?**
  Brutal-honest answer: **no, not literally per-function** — the standard's value is
  at **boundaries (RED) + stages (span+shared-histogram)**. The `hot`/`exempt` tiers
  exist precisely because ~55% of functions (private helpers, trivial getters,
  hot-inner) should NOT carry their own signals. The directive "every function …
  unless a reason" is satisfied by the tier being the reason: **every function is
  classified; most stage/boundary get signals; the rest get a categorized
  exemption.** Full literal per-function span+metric+log is rejected on the
  cardinality + overhead + noise math above — that rejection IS the architect's job
  the directive asked for.
- **RESOLVED — `is_trivial` threshold (≤3 statements).** Tuned empirically during the
  P0→P6 warn-then-hard rollout; the landed `is_trivial` classifier + `_exempt_globs` +
  per-fn allowlist together drove MISSING to 0 with no known false-exemption
  complaints. The threshold is settled unless P-SB's hot-loop sweep surfaces a new
  edge.

---

## Related
- `wiki:Yadgar OTEL Tracing — Span Mechanism & Coverage` (span mechanism, no-slowness rules, I24)
- `wiki:yadgar-adr-log` — ADR-0001 (obs train, OTLP+metrics ON), ADR-0026/0030/0031
  (recall IO-bound, warm-floor), **ADR-0033 (OPEN — contested baseline, general
  recall latency)**, **ADR-0034 (the standard itself; implementation COMPLETE at
  v5.105)**, **ADR-0040** (I33 glob blind-spot → `_exempt_globs` option B),
  **ADR-0041** (span-in-log-path flood, 4 occurrences), **ADR-0074 (ACCEPTED
  2026-07-09 — span-budget policy, the basis for §5b P-SB)**
- `docs/reference/decisions.md` — P0-refuted-premise + deferrals recorded on merge
- Existing lints: `scripts/check_observe_coverage.py` (**I33**, global hard),
  `scripts/check_trace_spans.py` (I24), `check_metric_writers.py` (I23),
  `check_complexity_allowlist.py` (I30), `check_capability_coverage.py` (I32),
  `scripts/check_dynamic_span_names.py` (R2b — dynamic span names)
- Overhead method: recall-perf warm-floor checklist; `benchmarks/` record-only
  loadtest (#79); recall-3-train overhaul (`docs/plans/archive/recall-3-train-overhaul-2026-07-04.md`)
