# Obs + Velocity Completion Plan — close #8 (observability standard) + #29 (CI/velocity train)

**Date:** 2026-07-04
**Status:** executable spine. References — does NOT restate — the three prior design docs:
- `docs/plans/full-observability-standard-2026-07-03.md` (the STANDARD: `@observe`, tiers, exemption categories, I33 lint, P0–P5 rollout, cardinality/no-slowness math). **Authoritative for PART A design.**
- `docs/plans/ci-velocity-train-2026-07-03.md` (P1–P4; #83 is its P2, #79 its P4).
- `docs/plans/perf-loadtest-contract-2026-06-30.md` (#79 workload contract, thresholds, snapshot fixture, CI wiring). **Authoritative for PART B load-test design.**

This doc adds the closure layer the three lack: (1) triage against the **live** coverage count, (2) integration of PART A/B/C into one PR sequence, (3) a PR-batch / CI-minute strategy, (4) an adversarial AUDIT, (5) DONE criteria that literally close #8 and #29.

---

## 0. TL;DR — recommendation up front

1. **Obs-first, per the user directive.** Implement PART A (obs rollout) waves P1→P5 in order; fold PART C small-fixes into wave-adjacent PRs to avoid dedicated CI runs; land PART B (#83 gate, #79 harness) around P3 (#83 must precede the backend obs PR).
2. **"Every function" is NOT literally instrumented — and that is the correct reading of the directive.** The directive is satisfied by *classification*: every in-scope function carries a span source OR a categorized exemption. ~55% of functions (trivial getters, hot-inner, private helpers) get an exemption *with a documented category = the reason the user asked for*. Full per-function span+metric+log is rejected on cardinality (~19.5k→~6.5k series), noise (~40→~3 log-lines/recall), and churn grounds. See §5 AUDIT.
3. **Runtime overhead is a non-issue — measured, not assumed.** `@observe` A/B-measured at **+8ms (+4.2%), off-thread** on recall (ADR-0035). The hot path uses `stage`+`hot` tiers = zero per-item metric/log. Perf is NOT a blocker; diff-churn + span-noise + maintenance are the real costs, mitigated by leaning on exemptions.
4. **CI minutes:** the plan-doc PR, the #83 CI-gate PR, and every per-area **I33 `--hard` flip** all **skip test-CI** (`docs/**` / `.forgejo/**` in `paths-ignore`). The expensive PRs are the **source-instrumentation** area PRs — each merge to master that bumps the version = one multi-arch (amd64+arm64) image build. Backend obs = the one **backend** image rebuild; batch ALL backend instrumentation into a single PR to pay it once.
5. **Recommended FIRST phase to implement: PART A wave P1 (recall read path).** Rationale in §7.

---

## 1. Live current state (VERIFIED 2026-07-03, observed-state-wins)

The task brief's "534 functions allowlisted" is **stale/incorrect**. Ground truth from `python3 scripts/check_observe_coverage.py --warn`:

| Metric | Value | Source |
|---|---|---|
| **MISSING** (in-scope, no span source, not exempt, not allowlisted) | **1564** | live script run |
| **Allowlist entries** (`.observe-allowlist.json`) | **2** (both `hot-loop`: `fusion:_FusionMixin._normalize_signal`, `scoring:_normalize_fts_hits`) | file |
| Auto-exempt (dunder/property/trivial) | remainder of ~1600 in-scope | lint classification |
| Backend files instrumented | **1 of 5 files** (`ml_client.py` via `_rpc_span`; NOT 2/5 as briefed). ~4 backend functions span-sourced (`_rpc_span`×3 + `@trace_span` embed) — file vs function denominators differ, both fine for P3 scope | grep |
| Hooks instrumented | **0 of 15** — and hooks ARE in-scope (lint scans all `yadgar/**` except `tests/`) | grep + script `_iter_py_files` |
| MCP subsystem | **no `yadgar/mcp/` dir exists** — the "MCP surface" = `@_tool`-decorated fns under `yadgar/server/tools/*` (already span-sourced) | fs |
| I33 lint mode | `--warn` (exit 0) in CI `invariant-checks` + pre-commit; allowlist-integrity always hard | `ci-pr.yaml:125`, `.pre-commit-config.yaml:70` |

> **Reconciliation note.** The brief conflated three distinct buckets. The lint produces `SATISFIED / EXEMPT_{dunder,property,trivial,allowlist} / MISSING`. "534" matches none of the live numbers; the day-one I33 baseline was **1555 MISSING** (invariants doc), now **1564** (drift since). The triage below targets **MISSING**, not a phantom 534-entry allowlist. The allowlist is an intentionally tiny seed — the rollout waves populate real `hot-loop`/`generated` exemptions; auto-exempt trivial/dunder/property need **no** entry.

Rough in-scope `def` counts per subsystem (grep, includes to-be-exempted trivials):

| Subsystem | ~defs | files | Obs value |
|---|---|---|---|
| `server/` (incl `tools/`) | ~464 | 43 | HIGH (tool boundaries, RED) |
| `storage/` | ~317 | 23 | MED (write-path stages) |
| `retrieval/` | ~163 | 39 | **HIGHEST** (recall hot path) |
| `backend/` | ~77 | 5 | HIGH (inference RED + rerank/embed stages) |
| `consolidation/` | ~67 | 7 | MED (drainer/consolidation stages) |
| `hooks/` | ~60 | 15 | LOW (short-lived; flush caveat — §5) |
| `observability/` | ~23 | 3 | self (mostly framework) |
| root `yadgar/*.py` (49 files: engram, predictive_coding, knowledge_graph, embeddings, viz_*, wiki*, astrocyte_pool …) | large | 49 | MIXED |
| `causal_discovery/ cls_store/ curation/ enrichment/ export/ file_queue/ metacognition/ repo_wiki/ sleep_compute/ security/ seed/ update/ vacuum/ cli/` | tail | many | mostly LOW → exempt-heavy |

**Current versions:** core `5.104.0` (`pyproject.toml` + `server.json.version`); `server.json.backend_version` `5.10.0`.

> **AST inventory cross-check** (from the obs-standard doc §1): ~**1,626** in-scope functions total (~723 public / ~903 private); only ~**119 (~7%)** carry a span today (skewed: storage 57, server 32, `@_tool` tools 83). ~114 metric write call-sites, ~466 log call-sites. So the rollout is ~7%→classified-100%, but INSTRUMENT (span+metric+log added) lands on the HIGH/MED-value ~45%, not all 1,626 — see §5.
>
> **Naming caveat:** the obs-standard doc (2026-07-03, pre-merge) calls the lint invariant **`I34`** in its text; the SHIPPED invariant is **`I33`** (ID reconciled at merge — I28/I32 were script-only, I34 would skip a slot). This doc + `docs/ARCHITECTURE_INVARIANTS.md` use **I33**. Same lint, same script (`scripts/check_observe_coverage.py`).

---

## 2. PART A — Observability full rollout (#8, ADR-0034). PRIORITY. Implement first.

**Design authority = `full-observability-standard-2026-07-03.md`.** That doc's §5 already defines P0–P5 with per-area scope, files, versions, exit criteria. P0 shipped (v5.101, PR #150). This section carries the **remaining** waves P1–P5, mapped to the live 1564 and to concrete PRs, and folds in the triage the user asked for.

### 2.1 Triage rule — INSTRUMENT vs KEEP-EXEMPT-WITH-REASON

Applied per-wave (never bulk-add). For each in-scope MISSING function, classify:

**(a) INSTRUMENT** — gets `@observe(tier=…)`:
- **`boundary`** — public entrypoint / tool / API handler / cross-subsystem call. Full RED (span + `yadgar_observe_requests_total` + `_request_duration_seconds` + INFO/ERROR log).
- **`stage`** — a named pipeline stage (an already-extracted method in recall/write/consolidation). span + shared `yadgar_observe_stage_duration_seconds{stage}` + ERROR-on-raise log.
- **`hot`** — span/attribute only, NO per-call metric/log. For fns on a hot inner path where a boundary/stage would bloat but a diagnostic span is still wanted.

**(b) KEEP-EXEMPT-WITH-REASON** — categorized, NOT instrumented. Categories (source: obs-standard §3.5, encoded in lint + `.observe-allowlist.json`):

| Category | Definition | Declared via | Needs allowlist entry? |
|---|---|---|---|
| `trivial` | ≤3 statements, no branch/loop/IO/raise (pure getter/formatter) | auto-detected by lint | **no** |
| `property` | `@property`/`@cached_property`/descriptor | auto-detected | **no** |
| `dunder` | `__init__`, `__repr__`, `__eq__`, … | auto-detected | **no** |
| `hot-loop` | inner fn called per-item in a hot loop (per-call signal = bloat) | allowlist + ≥40-char rationale | yes |
| `generated` | codegen / migration boilerplate | allowlist path-glob | yes |
| `framework-instrumented` | span from `@_tool`/`_rpc_span`/httpx/FastAPI | auto-detected (sentinel) | no |
| `test` | under `tests/` | path-excluded | n/a |

**The category IS the "documented reason" the directive demands.** Auto-exempt (trivial/property/dunder/framework) cover the bulk with zero file churn; only `hot-loop`/`generated` need an explicit ≥40-char rationale entry (integrity always hard-checked). This is the user's "exemptions with reason, documented" — satisfied structurally.

**Tune the `trivial` threshold in P0 warn-mode BEFORE any hard flip** (obs-standard §6 open item): inspect the auto-exempt list; if `≤3 statements` over- or under-exempts, adjust before flipping an area to `--hard` (monotonic, so a wrong flip is expensive to walk back).

### 2.2 Waves → PRs (one area per PR; per-area I33 `--hard` flip = the ratchet)

Each wave: instrument in-scope MISSING → add needed `hot-loop`/`generated` allowlist entries → run `check_observe_coverage.py --area <name>` until 0 MISSING → flip that area's CI invocation to `--hard` (add `--area <name>` without `--warn`) → measure recall warm-floor A/B on the same deploy (obs-standard §6; control for ADR-0033).

| Wave | Area (`--area` substring) | Scope summary | Version bump | CI cost | Effort | Closes toward |
|---|---|---|---|---|---|---|
| **P1** | `retrieval` (+ `server/tools/recall`) | `boundary` on recall tool (already `@_tool`→metric+log only); `stage` on scoring/fusion/reranking extracted methods; `hot` allowlist for per-candidate inner fns | core minor (5.104→5.105) | core image rebuild | **M — ~1.5–2 days** (~163 fns, but ~8 already spanned; hottest path = careful warm-floor A/B; sets the tiering pattern all later waves copy) | #8 |
| **P2** | write path: `_memorize_phases`, `predictive_coding`, `file_queue`, `consolidation` | `stage` on memorize phases, WriteGate, drainer apply, consolidation phases | core minor | core image rebuild | **M — ~1.5–2 days** (~130 fns across 4 subsystems; pattern reused from P1; write-path perf check) | #8 |
| **P3** | `backend` | `boundary` RED on FastAPI endpoints (mostly framework-instrumented → metric+log only); `stage` on rerank/embed internals; keep `_rpc_span` | **backend_version 5.10→5.11 + backend image rebuild** | **backend image rebuild (batch ALL backend here → pay once)** | **S–M — ~1 day** (only 5 files/~77 fns, endpoints already framework-spanned; cost is the rebuild + backend RED dashboard verify, not code volume) | #8 |
| **P4** | `server/tools` + `server/_app` | `@_tool` already spans all tools → lint counts them; add metric+log-only `@observe` where a tool needs RED beyond pool metrics; allowlist private helpers | core minor | core image rebuild | **S — ~0.5–1 day** (~245 tool fns but 83 already `@_tool`-spanned → lint counts them; mostly allowlisting private helpers as trivial/hot) | #8 |
| **P5** | `hooks` + root `yadgar/*.py` + `storage` + `repo_wiki` + long tail → **global `--hard`** | remaining functions classified/allowlisted; hooks (see §5 flush caveat); flip lint to GLOBAL `--hard` (drop `--area`, drop `--warn`) | core minor | core image rebuild | **L — ~3–4 days** (the big sweep: ~600 root-module + ~310 storage + hooks-with-flush + 49 root files + long tail; most are exempt-classification, but volume + global flip risk dominates) | **#8 CLOSED** |

**Ordering rationale (obs-standard §5, endorsed):** P1 first = hottest path proves the tiering holds the warm-floor. P3 (backend) isolated behind a version bump/rebuild. P5 global hard-fail last, once every area is individually green (monotonic ratchet).

### 2.3 Signals = all three, by construction
Confirmed from `yadgar/observability/observe.py`: `@observe` composes `@trace_span` (span, I24-family) + shared bounded Prometheus families (I23 — writers-by-construction) + I14 JSON logger. So **instrumenting a function = span + metric + log in one decorator**, tier-gated (`hot` = span only by design). No separate metrics/logs pass needed. Double-instrumentation guard: a fn already carrying `@trace_span`/`@_tool`/`_rpc_span` runs `@observe` in metric+log-only mode (exactly one span).

### 2.4 Backend / hooks / mcp — the user's explicit call-outs
- **Backend (1/5 today):** all four uninstrumented files (`cache.py`, `embed_service.py`, `embed_service_metrics.py`, `__init__.py`) instrumented in **P3**, one PR, one backend rebuild. Endpoints are FastAPI-instrumented (span present) → mostly metric+log-only.
- **Hooks (0/15):** covered in **P5**. **CAVEAT (§5):** hook scripts are short-lived processes; async `BatchSpanProcessor` won't flush before exit → spans lost. Hooks need a **force-flush-on-exit** (`tracer_provider.force_flush()` in a `finally`/atexit) or the instrumentation is cosmetic. Most hook internals are `trivial`-exempt; instrument only the hook *entrypoint* + add the flush.
- **MCP:** no `yadgar/mcp/` dir. The MCP tool surface = `@_tool` fns under `server/tools/*`, already span-sourced (P4 counts them; RED only where a tool needs it).

---

## 3. PART B — Velocity train remainder (#29 → closed)

Design authority: `ci-velocity-train-2026-07-03.md` (#83 = its P2) + `perf-loadtest-contract-2026-06-30.md` (#79).

**Effort:** #83 = **S, ~0.5 day** (CI-only, no source change, logic already exists — reuse `check_backend_bump.py` constants + a red-then-green test PR). #79 = **M–L, ~3.5–4.5 days** (per loadtest doc: ~2–3 days record-only harness + snapshot pin + report writer + `perf.yaml`; +1 day baseline-diff + snapshot/model guards; +0.5 day Phase-2 gates once baseline stable).

### 3.1 #83 — backend-version-bump CI gate (closes the PR#144 miss)
- **What:** `check_backend_bump.py` exists as **pre-commit only** — never runs in CI (verified: no CI job invokes it). Promote its logic into a CI step in `ci-pr.yaml`'s `verify-version-bump` job, comparing `origin/master...HEAD` (not `--cached`). Import the module's `BACKEND_BUILD_INPUTS`/`BACKEND_BUILD_DIRS` constants in-CI to avoid drift. FAIL a PR that touches `yadgar/backend/**` / `Dockerfile.backend` / `entrypoint-backend.sh` without a `server.json.backend_version` bump (allow a `no-backend-release` label escape). Also add `check_versions.py` as a CI step (4-pin drift guard, same skip-risk).
- **Exact YAML in** `ci-velocity-train-2026-07-03.md` §P2.
- **Files:** `.forgejo/workflows/ci-pr.yaml`. **No source change.**
- **CI cost: SKIPS test-CI** (`.forgejo/**` in `paths-ignore`). Cheap.
- **SEQUENCING: land #83 BEFORE PART A wave P3.** Every backend obs PR must be forced to bump `backend_version`; the gate makes that un-bypassable. This is a real argument to interleave #83 early even under obs-first — put it right before P3.

### 3.2 #79 — load-test contract + per-PR regression harness
- **Build per `perf-loadtest-contract-2026-06-30.md` unchanged.** Realistic scope: **record-only first** (earn a baseline + noise band before gating — anchored lesson mem 518987: contention produced 14–47 *false* regressions).
- **Harness:** direct-HTTP threads via `_Daemon`/`_call_tool` (NOT batched MCP calls — MCP recall serializes, which is exactly why Phase B goes direct-HTTP). Workload contract (§2.1): W0 warm-up 10×; A 100× sequential recall (p50/p95/p99); **B 50× recall @ 8-concurrent** (the offload/backpressure regime); C 30× wiki_query; D 20× memorize→drain; E `/health/live` continuous during B. Fixed committed `.jsonl` query list. N=5 runs, medians.
- **Metrics:** recall p50/p95/p99 (seq + concurrent), Phase-B throughput, `/health/live` p99 under load, drainer throughput, error rate.
- **Ties to CE work:** recall latency is CE-dominated (~90%, ADR-0035). The harness's recall-p95 + a **CE-span budget** (Tempo `backend.rerank.ce` span duration baseline) is the regression signal for the CE Lever work (#13/#28). Record CE-span p95 alongside recall p95.
- **Thresholds (Phase 2, after baseline):** recall-seq p95 < baseline×1.15; `/health/live` p99 < 8s @ 8-concurrent; 0 crash-loops; error-rate < 0.5%; regression = metric > baseline + max(15%, noise-band) across median of N.
- **Snapshot:** ONE frozen quiesced pin from the nightly backup (`YADGAR_PERF_SNAPSHOT_DIR`), NOT committed (data). Committed query list IS (tiny prompts).
- **CI wiring:** `make perf` + `.forgejo/workflows/perf.yaml` as `workflow_dispatch` (**non-gating, opt-in, NOT every-PR** — runner noise). Mirrors `eval.yaml`. **SKIPS test-CI** (`.forgejo/**`).
- **Files:** new `.forgejo/workflows/perf.yaml`, `Makefile` (`perf` target), a `benchmarks/` harness script, `docs/benchmarks/` report output.

---

## 4. PART C — Small fixes from prior CI warnings (#37)

Batch into a single PR OR fold each into a wave-adjacent source PR to avoid a dedicated CI run. These are the *actionable* items; the rest of #37 is explicitly **deferred**. **Effort: XS, ~0.5 day total** (one-line `utcnow` edit + one grep-verification of the lifespan item).

1. **`datetime.utcnow()` → `datetime.now(datetime.UTC)`** in `yadgar/hooks/session-end-capture.py:272` (Python 3.14 removes `utcnow()`). **VERIFIED** — line 272: `"ended_at": datetime.datetime.utcnow().isoformat() + "Z"`. Fix: `datetime.datetime.now(datetime.UTC).isoformat()` (drop the manual `+ "Z"` — `now(UTC)` yields `+00:00`; or keep `.replace(tzinfo=None)` + "Z" for byte-identical output — choose the tz-aware form). This file is a **hook** → touches `yadgar/hooks/**` → triggers test-CI. **Natural fold: bundle with PART A wave P5 (hooks)** — same file area, one CI run.
2. **async-generator lifespan → `@contextlib.asynccontextmanager`.** **STATUS: LIKELY ALREADY DONE / MIS-SCOPED.** Verified: the ONLY `lifespan` definition in the codebase is `yadgar/backend/embed_service.py:414`, already decorated `@asynccontextmanager` (line 414, `app = FastAPI(..., lifespan=lifespan)` at 521). No bare async-gen lifespan exists in core `server/`. **ACTION: before writing any fix, re-run the failing-warning capture** to confirm the source; if it's a third-party (Starlette/uvicorn) deprecation, it belongs in the DEFERRED bucket, not #37 actionable. Do NOT "fix" the already-correct backend lifespan.

**DEFERRED (documented, out of scope for closing #29):** third-party `httpx`→`httpx2`, `websockets`/`uvicorn` deprecations, npm/viz `npm audit` vulns. These are dependency-migration efforts, not warnings-cleanup; track separately.

---

## 5. AUDIT — adversarial, brutal

**Is instrumenting 1564 functions worth the diff churn + runtime overhead?**

**No — and the standard already rejects literal per-function instrumentation.** The honest breakdown:

- **Runtime overhead: NOT a real risk.** A/B-measured `@observe` cost on recall = **+8ms (+4.2%), off-thread** (ADR-0035); instrumentation was *exonerated* as a latency cause. Spans export async (BatchSpanProcessor, off event loop); metric `.observe()`/`.inc()` are cheap; I14 log is off-thread (C2 queue). The hot path (recall/encode) is `stage`+`hot` tiers = **zero per-item metric/log**. The task brief's hypothesis ("@observe perf cost on hot paths like recall/encode") is **empirically dead** — do not treat it as a blocker. The `hot` tier exists precisely to keep per-candidate inner loops at span-attribute-only.
- **The REAL cost is diff churn + span-noise + maintenance drag** — not CPU. 1564 decorators across ~200 files is a large, low-signal diff; span/log noise from over-instrumenting trivial helpers degrades the *signal* the standard is meant to add. Mitigation = lean HARD on exemptions: ~55% of functions SHOULD be `trivial`/`property`/`dunder`/`hot`/`framework`-exempt. Auto-exempt (no allowlist entry) carries the bulk with zero churn.
- **Value ranking (where observability actually pays):**
  - **HIGH:** recall/retrieval hot path (P1), backend inference RED + rerank/embed stages (P3), server tool boundaries (P4), write/consolidation drainer stages (P2). These are where a Tempo trace or a RED panel answers a real ops question.
  - **LOW → exempt-heavy:** viz/wiki formatters, pure transforms, CLI glue, seed/migration/codegen, causal_discovery/metacognition tails, trivial getters. Instrumenting these adds noise, not insight. `hot`/`exempt` tier = correct.
  - **hooks (P5): LOWEST value + a real trap.** Short-lived processes; without a **force-flush-on-exit**, async spans never export → cosmetic instrumentation. Instrument only the hook entrypoint + add the flush; exempt the rest as `trivial`. Do not spend effort here.

**Is the per-area I33 `--hard` flip safe?** Yes, with two guards: (1) the ratchet is **monotonic** — flip an area to `--hard` only when it reads 0 MISSING under `--area`, and never flip back (banned regression). A wrong flip blocks all subsequent PRs touching that area until fixed. (2) The area substring is a naive `path contains` match (`_iter_py_files`) — a badly-chosen `--area` string can over-match (e.g. `--area server` catches `observability`? no, but `--area re` would catch `retrieval`+`repo_wiki`+`security`). **Use full path segments** (`--area retrieval`, `--area backend`), and verify the file set with `--list-all` before flipping.

**Sequencing risks:**
- **#83 must land before P3** (backend obs) or a backend obs PR can merge without a `backend_version` bump → phantom-image gap (the #144 failure mode). Interleave #83 as the last step before P3.
- **Backend rebuild is a discrete cost** — batch ALL backend instrumentation into P3's single PR; do NOT split backend obs across PRs (multiple backend rebuilds).
- **ADR-0033 (recall slowdown) is OPEN** — the warm-floor overhead gate must A/B on the *same deploy* (before vs after within one image/config), never against a historical 1.6s that may be contaminated. Do not claim "no regression" from a cross-deploy comparison.
- **`trivial` threshold tuning must precede the first hard flip** — inspect auto-exempt output in warn-mode first.

**Verdict:** proceed, but reframe the goal from "instrument 1564" to "**classify 1564; instrument the ~45% that are HIGH/MED-value boundaries+stages; exempt the rest with a category**." That reframing IS the architectural judgment the directive ("unless you create a reason") delegated. Do not gold-plate the low-value tails.

---

## 6. PR-batch / CI-minute strategy

**CI cost rules (verified):**
- **test-CI (`ci-pr.yaml`) SKIPS** only when EVERY changed path is under `benchmarks/**` / `docs/**` / `.forgejo/**`. Mixed PRs run. **ROOT files (README, `pyproject.toml`, `server.json`) are NOT ignored → they DO trigger test-CI.**
- **Release build (`ci-release.yaml`, post-merge to master)** fires when `pyproject` version ≠ latest tag. **Core image** rebuilds when a `yadgar/` file NOT under `yadgar/backend/` changes (or core version bump). **Backend image** rebuilds when `yadgar/backend/**`/`Dockerfile.backend`/`entrypoint-backend.sh` changes AND `backend_version` bumped (registry-existence fallback). Multi-arch amd64+arm64.

| PR | test-CI? | Release image build? | Notes |
|---|---|---|---|
| **This plan doc** | **SKIP** (`docs/**`) | none | free |
| **#83 CI gate** (`.forgejo/`) | **SKIP** (`.forgejo/**`) | none | free — land early |
| **#79 perf harness** (`.forgejo/perf.yaml` + `benchmarks/` + `Makefile`) | Makefile change touches root → **runs test-CI**; keep harness under `benchmarks/**` + a minimal Makefile hunk | none | mostly cheap |
| Each **per-area I33 `--hard` flip** (`.forgejo/ci-pr.yaml` arg change only) | **SKIP** (`.forgejo/**`) | none | free — separate the flip from the source PR OR combine (see below) |
| **P1/P2/P4/P5 source PRs** (`yadgar/**` core) | runs test-CI | **1 core image build each** on merge (version bump) | the expensive ones |
| **P3 backend PR** (`yadgar/backend/**`) | runs test-CI | **1 backend image build** (backend_version bump) | batch ALL backend here |
| **PART C `utcnow` fix** (`yadgar/hooks/`) | runs test-CI | core build | **fold into P5** (same hooks area) → 0 extra runs |

**Batching decisions:**
- **Fold the `--hard` flip into its own source PR's follow-up?** Two options: (a) flip in the SAME PR that reaches 0 MISSING (one CI run, but the flip + instrumentation land atomically — cleaner ratchet, but a test failure blocks both); (b) flip in a separate `.forgejo/`-only PR right after (free CI, but a two-step merge). **Recommend (a)** — atomic, and the source PR already runs test-CI so the flip adds nothing. The `.forgejo/`-only separate-flip option is the fallback if a source PR is large/risky.
- **Batch areas to cut rebuilds?** Each core source PR = one core rebuild regardless of size, so merging P1+P2 into one PR saves a rebuild BUT: bigger PR = harder review + wider blast radius for the monotonic area-flip (a bad flip blocks more). **Recommend keeping P1–P5 as separate PRs** (one area = one reviewable diff = one safe flip); the rebuild-per-PR cost is acceptable and the review/ratchet safety dominates. The ONE batching that IS worth it: **all backend instrumentation in a single P3 PR** (backend rebuild is the expensive, discrete one).
- **Net CI-minute cost:** ~5 core image builds (P1,P2,P4,P5 + any) + 1 backend build (P3) + ~5 test-CI runs on the source PRs. The doc/gate/flip/perf-config PRs are ~free. `utcnow` folded into P5.

---

## 7. Recommended FIRST phase to implement

**PART A wave P1 — recall read path (`retrieval` + `server/tools/recall`).**

Rationale:
1. **Obs-first, per the user directive** — PART A is the priority; P1 is the first rollout wave (P0 already shipped).
2. **Highest observability value** — recall is the hottest, most-diagnosed path (CE 90% of latency, ADR-0035); a `stage`-tiered trace here is where a Tempo waterfall answers real questions.
3. **De-risks everything downstream** — proving the `stage`+`hot` tiering holds the warm-floor on the hottest path validates the no-slowness claim before backend/write/global waves. If tiering can't hold recall, better to learn it in P1 than P5.
4. **No backend rebuild, no #83 dependency** — pure core, lands immediately; #83 can land in parallel (free, `.forgejo/`-only) ahead of P3.
5. **Tunes the `trivial` threshold** on a well-understood area before any hard flip elsewhere.

**Immediately parallel-able (free CI):** land **#83 CI gate** (`.forgejo/`-only, unblocks P3) and **this plan doc** first — neither touches source, both skip test-CI.

---

## 8. DONE criteria — what closes #8 and #29

### #8 (observability standard, ADR-0034) CLOSED when:
- [ ] P1–P5 all merged; each area reads **0 MISSING** under `check_observe_coverage.py --area <name>`.
- [ ] `check_observe_coverage.py` runs **global `--hard`** (no `--warn`, no `--area`) in `ci-pr.yaml invariant-checks` + pre-commit — exit 1 on any new MISSING.
- [ ] `.observe-allowlist.json` contains ONLY documented exemptions (each `hot-loop`/`generated` entry has a valid category + ≥40-char rationale; integrity green).
- [ ] Backend (P3): all 5 files classified; `backend_version` bumped; backend RED dashboards live.
- [ ] Hooks (P5): entrypoints instrumented **with force-flush-on-exit**; rest categorized.
- [ ] Every wave's recall warm-floor A/B (same deploy) within budget (no regression attributable to `@observe`).
- [ ] I33 invariant text in `docs/ARCHITECTURE_INVARIANTS.md` updated from "warn-mode / per-area" to "global hard-fail".

### #29 (CI/velocity train) CLOSED when:
- [ ] **#83:** `ci-pr.yaml` fails a PR touching `yadgar/backend/**` without a `backend_version` bump (verified by a red-then-green test PR); `check_versions.py` runs in CI.
- [ ] **#79:** `make perf` + `.forgejo/workflows/perf.yaml` (`workflow_dispatch`) exist; ≥3 record-only baseline runs committed to `docs/benchmarks/`; noise band established; Phase-2 gate thresholds set from the baseline (not guessed).
- [ ] PART C actionable items resolved: `utcnow` fixed (verified); async-gen lifespan item resolved OR reclassified deferred with evidence.

---

## Related
- `docs/plans/full-observability-standard-2026-07-03.md` — PART A design authority (tiers, exemptions, P0–P5, cardinality/no-slowness math).
- `docs/plans/ci-velocity-train-2026-07-03.md` — #83 (P2) + #79 (P4) design.
- `docs/plans/perf-loadtest-contract-2026-06-30.md` — #79 workload contract, thresholds, snapshot, CI wiring.
- `docs/ARCHITECTURE_INVARIANTS.md` — I33 (tri-signal), I14 (logging), I23 (metric writers), I24 (span on HTTP handlers).
- `wiki:yadgar-adr-log` — ADR-0034 (obs standard), ADR-0035 (recall accounted, `@observe` +8ms exonerated), ADR-0037 (CI never OTEL_SDK_DISABLED).
- `wiki:yadgar-open-tasks-live-tasklist-mirror` — task IDs #8/#29/#83/#79/#37.
