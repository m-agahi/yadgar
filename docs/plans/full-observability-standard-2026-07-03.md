# Full-Observability Standard — span + metric + log on every function, tiered + enforced

**Status:** PLAN / DESIGN ONLY. No code changed. **Date:** 2026-07-03.
**Author:** agent (bot). **Branch:** `docs/full-observability-plan`.
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
   per function in the recall hot loop blows the ~1.6s warm-recall floor
   (`wiki:yadgar-adr-log` ADR-0026/0030/0031). A per-function INFO log is noise that
   drowns the signal. **So the deliverable is a STANDARD + EXEMPTION POLICY +
   ENFORCEMENT RATCHET — not a blind span+metric+log sweep.**

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
   declared, resolved dependency (`pyproject.toml`, `uv.lock` → 0.63b1);
   `HTTPXClientInstrumentor().instrument()` runs globally at core import
   (`server/_app.py:43`); backend has `FastAPIInstrumentor`
   (`backend/embed_service.py:539`); **both** backend calls are explicitly
   span-wrapped (`/rerank` → `_rpc_span` at `backend/ml_client.py:796-814`; `/embed`
   → `@trace_span("rpc.embed")` at `remote_embeddings.py:55`). **Propagation is
   already wired.** P0 is reframed (§5) from "fix the gap" to "add an end-to-end
   verification test + close the stdio/daemon-mode instrumentation hole + seed the
   coverage lint in warn-mode." Later executors: do not trust the original "KNOWN
   GAP" framing — it was a task-prompt assumption, refuted here by grep.

5. **The ratchet is a new I-invariant lint (`I34 / check_observe_coverage.py`)** that
   AST-walks every function, classifies it (needs-`@observe` / auto-trivial /
   allowlisted-exempt), and FAILS if a non-exempt function lacks a span source AND is
   not in `.observe-allowlist.json`. Modeled 1:1 on the existing
   `check_trace_spans.py` (I24) + `.complexity-allowlist.json` (I30) machinery.
   Warn-mode first, flipped to hard-fail per area as each reaches 100%. **Without
   this lint the standard is a one-time sweep that rots.**

---

## 1. Inventory — the "list everything" step

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

**Version anchors:** core `pyproject.toml:1` → `5.99.0`; backend
`yadgar/__init__.py:21` → `BACKEND_VERSION = "5.10.0"` (mirrored in `server.json`).
Backend-input changes require a `BACKEND_VERSION` bump (gated by
`check_backend_bump.py`, see #83).

---

## 2. Current-state analysis — the "analyse what's implemented" step

### 2.1 Tracing (`yadgar/tracing.py`)
Three span idioms (source: `wiki:Yadgar OTEL Tracing — Span Mechanism & Coverage`,
PR #148 v5.100):
- **`@trace_span("name", attributes=...)`** (`tracing.py:614`) — wraps sync/async,
  records exception + `status=ERROR` on raise, no-op identity decorator when OTel
  absent. **This is the codebase idiom — decorate extracted stage methods.**
- **`span("name", **attrs)`** inline CM (v5.100) — for genuinely inline blocks;
  no-ops to `nullcontext`.
- **`get_tracer(...).start_as_current_span`** — single-use generator CM (drainer).
  Enter EXACTLY once via `with`; double-enter → `RuntimeError`.

**Export is async and safe:** `setup_tracing` (`tracing.py:494`) registers
`BatchSpanProcessor` (`:516`, opt-in via `YADGAR_OTLP_ENDPOINT`) — off the event
loop. `LogSpanProcessor` (always on) emits one I14 JSON line per span, routed off
the loop via a QueueHandler/QueueListener (C2 P2). Spans add no blocking I/O.

**Context propagation across the offload boundary:** `@_tool()`
(`server/_app.py:373`) wraps every tool in `@trace_span("tool.<name>")`. When
`OFFLOAD_TOOLS=True`, `run_offloaded` (`server/_offload.py`) uses
`contextvars.copy_context()`, which captures the parent span — inner spans nest
correctly on the worker. No per-stage plumbing.

**v5.100 already added ~40 stage spans** across recall/write/consolidation/drainer/
wiki/checkpoint/storage. `recall()` and `_apply_rerank_pipeline` are I13-HARD-capped
— do NOT add nested `with span()` there; decorate the extracted stage methods.

### 2.2 Core→backend propagation — VERIFIED WIRED (task premise refuted)
| Fact | Evidence |
|---|---|
| httpx auto-instrumentor is a real dep | `pyproject.toml`: `opentelemetry-instrumentation-httpx>=0.51b0`; `uv.lock` → 0.63b1 |
| It's activated globally at core import | `server/_app.py:41-45` — `HTTPXClientInstrumentor().instrument()`, unconditional, try/except-guarded |
| RemoteMLClient uses a plain httpx.Client (auto-injected) | `backend/ml_client.py:684` |
| `/rerank` has an explicit span | `_rpc_span("rpc.rerank.{ce,nli,pair}")` — `ml_client.py:796-814` |
| `/embed` has an explicit span | `@trace_span("rpc.embed")` + `httpx.Client` — `remote_embeddings.py:36,55` |
| Backend extracts incoming traceparent | `FastAPIInstrumentor.instrument_app(app)` — `backend/embed_service.py:539` |

**Residual (real) gaps** — NOT the claimed disconnection:
- **(R1) No end-to-end assertion.** Nothing verifies traceparent actually crosses
  the wire and nests. A silent regression (dep drop, instrument() moved) would go
  unnoticed. → P0 adds a test.
- **(R2) stdio/daemon-only entry paths** that never import `server/_app.py` never
  call `HTTPXClientInstrumentor().instrument()` — so any backend HTTP from those
  paths roots a disconnected trace. → P0 hoists instrumentation into
  `setup_tracing()` itself (single choke-point) so every entry mode gets it.

### 2.3 Metrics (`yadgar/metrics.py` + `backend/embed_service_metrics.py`)
- **83 core + 21 backend** Prometheus objects (Counter/Histogram/Gauge/Summary),
  **private registry** (independent of OTLP, safe — ADR-0001).
- Naming convention observed: `yadgar_<subsystem>_<measure>_<unit>` (e.g.
  `yadgar_recall_duration_seconds`, `yadgar_shadow_cache_*`, `yadgar_tool_pool_*`).
  Backend uses its own isolated registry.
- **I23 (`check_metric_writers.py`)** already gates "every declared metric has ≥1
  writer" (allowlist: `yadgar_subagent_capture_rate` = intentional zero).

### 2.4 Structured logs (I14, `yadgar/log_config.py:1-49`)
JSON logger, default in production. Fields: `ts`, `level`, `component`, `action`,
`outcome`, `event`, `latency_ms?`, `error?`, `traceback?`. `ContentRedactor` filter
(v5.4.7) strips secrets from `extra=`. **`trace_id` is already emitted per span line**
(LogSpanProcessor), so log↔trace correlation exists — the standard leans on it
instead of duplicating per-function logs.

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
(`invariant-checks` job). **`check_observe_coverage.py` slots into both, same shape.**

---

## 3. The STANDARD + method decision — the "decide best method and standard" step

### 3.1 The mechanism: one decorator, `@observe`
Add `@observe(...)` to `yadgar/observe.py` (new module; keeps `tracing.py`
single-purpose). It **composes**, not replaces, the existing signal paths:

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
- **Ceiling:** warm recall stays at its measured floor (**~1.6s**, per
  `wiki:yadgar-adr-log` ADR-0026/0030/0031 and the recall-perf checklist).
  **CAVEAT:** ADR-0033 (**status: open**) reports a live recall slowdown of 24–76s
  whose cause is *not isolated* (backend/deploy vs core). **The baseline is currently
  contested** — the overhead-measurement method (§6) MUST control for ADR-0033, not
  assume a clean 1.6s.
- **How the tiering protects it:** the recall hot path is `stage` + `hot` tiers.
  Stage spans are already-extracted methods (zero added nesting; v5.100 rules). Hot
  tier adds ZERO spans/metrics/logs per item — only a small integer attribute on the
  enclosing span. No per-item span, no per-item metric, no per-item log anywhere on
  the hot path. The only per-op cost is ~3 boundary emissions, all off the event
  loop.

---

## 4. Enforcement — `I34 / check_observe_coverage.py` (the ratchet)

A new invariant lint, modeled on `check_trace_spans.py` (I24) + I30's allowlist
discipline. It is what makes "nothing missed" durable rather than a decaying sweep.

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

### 4.2 Modes & rollout wiring
- **`--warn`** (phase P0): prints MISSING, exits 0. Establishes the baseline count
  per area without blocking commits.
- **`--area <name> --hard`**: hard-fail only for a named area. As each rollout phase
  reaches 100%, flip that area to `--hard` in `.pre-commit-config.yaml` +
  `.forgejo/workflows/ci-pr.yaml` (`invariant-checks` job). Ratchet is monotonic:
  an area at `--hard` never regresses.
- **`.observe-allowlist.json`** schema (mirrors `.complexity-allowlist.json`):
  ```json
  { "yadgar.retrieval.scoring:_inner_score": {
        "category": "hot-loop",
        "rationale": "per-candidate scorer; span/metric per call = 50+/op bloat" } }
  ```
- **Anti-stale (like I30 invariant c):** every allowlist key must map to a currently
  existing function; a removed function → hard fail (forces cleanup).

### 4.3 Where it plugs in
- `.pre-commit-config.yaml`: new local hook `check-observe-coverage`, `files:
  ^yadgar/.*\.py$`, initially `args: [--warn]`, flipped per area.
- `.forgejo/workflows/ci-pr.yaml`: add to the `invariant-checks` job alongside I23/
  I24/I25/I29/I32.
- `scripts/check_observe_coverage.py` + `tests/.../test_check_observe_coverage.py`
  (a `test_live_codebase` test, warn-mode asserting the script *runs*, plus unit
  tests on `is_trivial` / allowlist validation).

---

## 5. Phased rollout

Each phase = its own PR + version bump. Backend-touching phases bump
`BACKEND_VERSION` (gated by `check_backend_bump.py`, #83) and force a backend image
rebuild. Overhead gate = recall-perf warm-floor before/after (§6), controlling for
ADR-0033.

| Phase | Scope | Key files | Version | Exit criteria |
|---|---|---|---|---|
| **P0 — Standard + ratchet + propagation-verify** | `@observe` module; `check_observe_coverage.py` in `--warn`; hoist `HTTPXClientInstrumentor` into `setup_tracing()` (fixes stdio/daemon-mode R2); add end-to-end traceparent test (R1) | `yadgar/observe.py`, `tracing.py`, `scripts/check_observe_coverage.py`, `tests/`, `.pre-commit-config.yaml`, `.forgejo/…` | core minor (5.100→5.101) | decorator + lint land; lint warn-mode green (runs, reports baseline); traceparent E2E test passes; no warm-floor regression |
| **P1 — Recall read path** | boundary on `recall` tool (already `@_tool` — metric+log only); `stage` on retrieval scoring/fusion/reranking extracted methods; `hot` exemptions allowlisted for per-candidate inner fns | `retrieval/scoring.py`, `retrieval/fusion.py`, `retrieval/reranking.py`, `server/tools/recall.py`, `.observe-allowlist.json` | core minor | retrieval area 100% classified; flip `--area retrieval --hard`; warm-floor within budget |
| **P2 — Write / consolidation / drainer** | `stage` on memorize phases, WriteGate, drainer apply, consolidation phases | `server/tools/_memorize_phases/*`, `predictive_coding.py`, `file_queue/apply.py`, `consolidation/*` | core minor | those areas 100%; flip `--hard`; write-path perf unregressed |
| **P3 — Backend** | `boundary` RED on FastAPI endpoints (mostly FastAPI-instrumented already → metric+log only); `stage` on rerank/embed internals; keep `_rpc_span`/`rpc.embed` as span sources | `backend/embed_service.py`, `backend/ml_client.py`, `backend/embed_service_metrics.py` | **BACKEND_VERSION bump** (5.10→5.11) + image rebuild | backend area 100%; flip `--hard`; backend RED dashboards live |
| **P4 — MCP tool surface** | `@_tool` already spans all 245 → lint counts them; add metric+log-only `@observe` where a tool needs RED beyond the pool metrics; allowlist private helpers as trivial/hot | `server/tools/*`, `server/_app.py`, `.observe-allowlist.json` | core minor | tools area 100%; flip `--hard` |
| **P5 — Hooks + core-top-level + storage sweep + global hard-fail** | hook endpoints in `server/http.py` (already I24-gated); remaining core/storage/wiki functions classified/allowlisted; flip lint to global `--hard` | `server/http.py`, `yadgar/*.py`, `storage/*`, `repo_wiki/*` | core minor | **whole codebase 100% classified; lint global `--hard`; ratchet closed** |

**Ordering rationale:** value/risk. P0 is pure scaffolding + the one real
propagation fix (low risk, unblocks measurement). P1 first among rollouts because
recall is the hottest path — proving the tiering holds the warm-floor there de-risks
everything else. Backend (P3) is isolated behind a version bump + rebuild, so it's
sequenced where its image cost is a discrete step. Global hard-fail (P5) last, once
every area is individually green.

---

## 6. Risks + open questions

- **Contested baseline (ADR-0033, OPEN).** Live recall spiked 24–76s, cause not
  isolated. **Every phase's overhead gate must A/B on the same deploy** (warm-floor
  before vs after within one image/config), not compare against a historical 1.6s
  that may already be contaminated. Measurement method = the recall-perf warm-floor
  checklist, ≥6 warm runs, median, same box, backend fixed.
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
- **OPEN — `is_trivial` threshold (≤3 statements) may over- or under-exempt.** Tune
  empirically in P0 warn-mode by inspecting the auto-exempt list before flipping any
  area to hard.

---

## Related
- `wiki:Yadgar OTEL Tracing — Span Mechanism & Coverage` (span mechanism, no-slowness rules, I24)
- `wiki:yadgar-adr-log` — ADR-0001 (obs train, OTLP+metrics ON), ADR-0026/0030/0031 (recall IO-bound, warm-floor), **ADR-0033 (OPEN — contested baseline)**
- `docs/DECISIONS.md` — record P0-refuted-premise + any deferrals here on merge
- Existing lints: `scripts/check_trace_spans.py` (I24), `check_metric_writers.py`
  (I23), `check_complexity_allowlist.py` (I30), `check_capability_coverage.py` (I32)
- Overhead method: recall-perf warm-floor checklist
