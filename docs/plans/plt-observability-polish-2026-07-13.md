# PLT Observability Polish — Grafana Dashboards + Queue Visibility/Alerting

**Task:** #23 — "PLT observability polish: Grafana dashboards + queue visibility/alerting"
**Date:** 2026-07-13
**Status:** AUDITED (2026-07-13) — REVISE, build-conditional. See `## AUDIT (2026-07-13)` at end. Blocking user-decision A (P-SB dependency file absent).
**Author:** dispatched planning agent (source-verified against metrics.py / observe.py / pipeline.py)

---

## BLUF

Yadgar already ships an in-repo Grafana dashboard + alert set (`docs/observability/{dashboard.json,alerts.yaml}`, "Yadgar Observability v1", uid `yadgar-v1`, 20 panels / 6 rows, schema 36). **This car is a POLISH/EXTENSION of v1, not a greenfield build.** It:

1. Repoints the stale recall-stage panel from the effectively-dead `yadgar_recall_stage_ms{stage}` (legacy) onto the P-SB-restored `yadgar_observe_stage_duration_seconds{stage}` — per-stage recall latency (vector / fts / ppr / fusion / rerank / ce).
2. Adds a dedicated **queue/DLQ/drainer** row (depths, DLQ, drainer throughput, rejection taxonomy).
3. Adds a **cache** row (hit/miss ratio, evictions, size per named cache).
4. Adds a **consolidation-cycle health** row (phase durations, loop heartbeat freshness, loop errors).
5. Adds four operator alerts: DLQ-nonempty, queue-backlog, drainer-stalled, unclean-DB-stop.

Delivery: dashboards-as-code, shipped in-repo under `docs/observability/` (already the convention here) — consistent with ADR-0101 self-sufficiency. **No terraform, no code changes, no live Grafana stand-up.** Version impact: config/infra only (`.json` + `.yaml`) → TDD-scope exemption applies.

**Hard dependency:** the recall-latency panels + one alert consume `yadgar_observe_stage_duration_seconds`, which is **currently dead** and only becomes live after the P-SB Recall-Observability Car (`docs/plans/psb-observability-2026-07-13.md`) lands. This plan is designed to be MERGED-AND-DORMANT before P-SB, or gated behind it. See dependency section.

---

## Existing observability infra (verified)

### In-repo (this repo, `/home/max/git/yadgar`)

| Path | Contents | Role |
|---|---|---|
| `docs/observability/dashboard.json` | "Yadgar Observability v1", uid `yadgar-v1`, 20 panels, 6 rows, schema 36, 30s refresh | The shipped dashboard (dashboards-as-code) |
| `docs/observability/alerts.yaml` | 5 Prometheus alert rules (drainer lag, DLQ>10, recall slow, backend unreachable, CB stuck) | The shipped alert set |
| `yadgar/_shared/observability/metrics.py` | ~90 core metric families + core registry (`_registry`) + `/metrics` handler | Core metric surface (:8765) |
| `yadgar/_shared/observability/observe.py` | `@observe(tier=)` tri-signal decorator + 4 shared `yadgar_observe_*` families | Standard instrumentation mechanism |
| `yadgar/backend/embed_service/embed_service_metrics.py` | 8 backend families on an **isolated** `CollectorRegistry()` + `/metrics` handler | Backend metric surface (:8001) |
| `yadgar/backend/retrieval/pipeline.py` | recall plugin pipeline; `yadgar_recall_stage_duration_seconds{stage,profile}` emit (dead path) | Bespoke recall-stage histogram |

### External (NOT in this repo)

The PLT stack (Prometheus/Grafana/Tempo/Loki/Alloy) runs as **NixOS systemd services on host `nixos-quinyx`**, provisioned via the separate `nix` repo under `modules/observability/`. Ports: Prometheus :9090, Grafana :3000, Tempo :3200/:4318, Loki :3100.

- **Dashboard provisioning is file-drop:** Grafana watches `dotfiles/observability/dashboards/*.json` (nix repo, `grafana.nix`, 30s poll). Alert rules load from `dotfiles/observability/alerts/*.yaml`. **No nix edit per dashboard** — dropping a file suffices.
- **Two Prometheus scrape jobs:** `yadgar-core` → `127.0.0.1:8765/metrics`, `yadgar-backend` → `127.0.0.1:8001/metrics`. Both unauthenticated, loopback-only.
- **docker-compose (`docker-compose.yml`)** is dev-only and defines ONLY `core` (:8765) + `backend` (:8001). No Grafana/Prometheus/Tempo/Loki service in compose.

### `/metrics` endpoint wiring (verified)

| Port | Process | Handler | Registry | On-scrape refresh |
|---|---|---|---|---|
| :8765 | core (MCP server + recall daemon) | `yadgar/core/server/http.py:640` → `metrics.py:1178 metrics_handler` | core `_registry` | queue depths, process, data-quality, pool stats |
| :8001 | backend (embed service + queue drainer) | `yadgar/backend/embed_service/embed_service.py:813` → `embed_service_metrics.py:492` | isolated `CollectorRegistry()` | none |

**Registry split is the load-bearing fact for every panel:** the two processes have SEPARATE registries. A metric registered on the core `_registry` is served on :8765 and is INVISIBLE on :8001 unless a bridge collector re-exports it. P-SB adds exactly such a bridge for the `yadgar_observe_*` families.

### Verified live metric families this plan consumes

All names below are source-verified (`metrics.py` / `embed_service_metrics.py`) and live TODAY (except the two flagged `[P-SB]`).

Queue / drainer / DLQ (all on :8765 core, refreshed on scrape):
- `yadgar_queue_depth{queue}` — Gauge; `queue ∈ {queue, archive, dlq}` (metrics.py:52)
- `yadgar_dlq_size` — Gauge (metrics.py:177)
- `yadgar_dlq_rejection_count` — Gauge; DLQ entries carrying a rejection_reason (metrics.py:227)
- `yadgar_drainer_lag_ms` — Histogram; enqueue→drain-start (metrics.py:183)
- `yadgar_drain_cycle_duration_ms` — Histogram; full cycle (metrics.py:190)
- `yadgar_drain_stage_ms{stage}` — Histogram; per drain stage (metrics.py:197)
- `yadgar_wiki_add_rejected_total{reason}` — Counter; similarity-gate rejects (metrics.py:216)
- `yadgar_writegate_outcome{outcome}` — Counter (metrics.py:205)

Cache (all on :8765 core):
- `yadgar_cache_hit_total{cache}` / `yadgar_cache_miss_total{cache}` (metrics.py:108/115)
- `yadgar_cache_evictions_total{cache}` (metrics.py:127)
- `yadgar_cache_size_entries{cache}` — Gauge (metrics.py:134)
- `yadgar_embedding_cache_hits_total` / `yadgar_embedding_cache_misses_total` — legacy unlabelled (metrics.py:77/83)

Consolidation (all on :8765 core):
- `yadgar_consolidation_duration_seconds{phase}` — Histogram (metrics.py:68)
- `yadgar_action_batch_size` — Histogram (metrics.py:90)
- `yadgar_loop_last_run_unix_timestamp{loop}` — Gauge; heartbeat (metrics.py:777)
- `yadgar_loop_errors_total{loop,error_type}` — Counter (metrics.py:785)

Recall latency (per-stage) — **[P-SB dependency]**:
- `yadgar_observe_stage_duration_seconds{stage}` — Histogram (observe.py:97). **Dead today** (circular-import kills registration). After P-SB: live on **:8765 AND :8001** (bridge). Stage label values are DOTTED — see dependency section.
- `yadgar_recall_duration_ms` — Histogram; total recall (metrics.py:259). **Live today** — the total-latency stat panel already uses it.

DB / backend liveness (already on the v1 dashboard, retained):
- `yadgar_backend_reachable{endpoint}`, `yadgar_circuit_breaker_state{endpoint}`, `yadgar_surrealdb_query_duration_ms{op}`, `yadgar_process_rss_bytes`, `yadgar_process_cpu_percent`.

Backend (:8001, isolated registry): `yadgar_embed_rerank_duration_seconds{mode}`, `yadgar_embed_rerank_503_total{mode}`, `yadgar_embed_model_loaded{model}`, `yadgar_embed_queue_drainer_running`.

---

## Dependency on obs-quickwins (P-SB) fix

The "obs-quickwins train" in the task brief == the **P-SB Recall-Observability Car** (`docs/plans/psb-observability-2026-07-13.md`, status: AUDITED, build-GO, NOT YET BUILT as of 2026-07-13).

**What is broken today (verified):** the ENTIRE `@observe` metric arm (boundary RED + stage histograms) is silently dead in every process — a circular import at `observe.py:56-64` is swallowed by a bare `except Exception`, so `_PROM_AVAILABLE=False` and the four `yadgar_observe_*` families never register. Confirmed: zero `yadgar_observe_*` samples on :8765 AND :8001.

**What P-SB restores:**
1. P0 — breaks the circular import (leaf `registry.py`) so the `yadgar_observe_*` families register → live on **:8765** (core process @observe emissions).
2. Backend **bridge collector** — a read-only collector on :8001 that yields the shared-registry `yadgar_observe_*` samples at scrape time → recall stages (which run backend-side) become visible on **:8001**, no Tempo needed.
3. Two hot→stage tier promotions (`retrieval.vector.encode_query`, `retrieval.ce.score_ce_cached`) so encode + CE-wall surface as stage histograms.

**Stage label VALUES after P-SB (dotted — use these exact strings in PromQL, NOT `fts/vector/ppr/...`):**
`retrieval.fts`, `retrieval.vector`, `retrieval.vector.encode_query`, `retrieval.ppr`, `retrieval.spreading`, `retrieval.temporal`, `retrieval.fusion`, `retrieval.build_results`, `retrieval.rerank`, `retrieval.cross_encoder_rerank`, `retrieval.ce.score_ce_cached`.

**Correction to task brief (observed-state-wins):** the brief said the restored metric surfaces on :8001 only. Reality: it surfaces on **BOTH** ports post-P-SB (:8765 core-process spans, :8001 backend recall stages via bridge). For the recall-latency panels the correct scrape job is `yadgar-backend` (:8001) because recall executes backend-side. The brief's `vector/fts/ppr/fusion/rerank/ce` shorthand maps to the dotted values above.

**Sequencing options (pick one at audit):**
- **(A) Merge-dormant (recommended):** ship this whole dashboard now. Recall-latency panels render "No data" until P-SB lands, then light up automatically — zero rework. Queue/cache/consolidation rows are live immediately (they consume already-live metrics). Simplest, lowest coupling.
- **(B) Gate behind P-SB:** hold merge until P-SB is on master. Cleaner demo, but blocks 3 of 4 new rows that need no dependency.

Recommendation: **(A)**. Only the recall-stage panels + the `RecallStageStalled` alert (if added) touch the dependency; everything else is independent.

---

## Dashboard designs (panel → metric family → PromQL)

**Target file:** extend `docs/observability/dashboard.json` in place (bump to "Yadgar Observability v2", keep uid `yadgar-v1` so the existing Grafana bookmark survives; bump `schemaVersion` only if regenerated by Grafana export). `datasource.uid = "prometheus"` on every target (matches the provisioned DS — see 2026-05-23 UID-mismatch incident). Scrape job selector via `{job="yadgar-core"}` / `{job="yadgar-backend"}` where port matters.

### (a) Recall latency — per-stage breakdown [P-SB dependency] — REPOINT existing Row 3 + extend

Existing Row 3 "Recall stage durations p95" queries the dead legacy `yadgar_recall_stage_ms`. **Repoint** onto the P-SB metric.

| Panel | Metric family | PromQL |
|---|---|---|
| Recall total p95 (ms) [keep] | `yadgar_recall_duration_ms` | `histogram_quantile(0.95, rate(yadgar_recall_duration_ms_bucket[5m]))` |
| Recall stage p95 by stage (s) [repoint] | `yadgar_observe_stage_duration_seconds{stage}` | `histogram_quantile(0.95, sum by (stage, le) (rate(yadgar_observe_stage_duration_seconds_bucket[5m])))` |
| Recall stage p50 by stage (s) [new] | same | `histogram_quantile(0.50, sum by (stage, le) (rate(yadgar_observe_stage_duration_seconds_bucket[5m])))` |
| CE-wall stage mean (ms) [new] | same, `stage="retrieval.ce.score_ce_cached"` | `1000 * (sum without(job) (rate(yadgar_observe_stage_duration_seconds_sum{stage="retrieval.ce.score_ce_cached"}[5m])) / sum without(job) (rate(yadgar_observe_stage_duration_seconds_count{stage="retrieval.ce.score_ce_cached"}[5m])))` |
| Backend rerank p95 by mode (s) [new] | `yadgar_embed_rerank_duration_seconds{mode}` | `histogram_quantile(0.95, sum by (mode, le) (rate(yadgar_embed_rerank_duration_seconds_bucket{job="yadgar-backend"}[5m])))` |

Notes:
- `stage="retrieval.vector.encode_query"` isolates embedding-encode latency inside the recall path (distinct from batch `yadgar_encode_duration_ms`).
- **No `job` filter on the `yadgar_observe_stage_duration_seconds` panels — intentional.** :8765 (core process) and :8001 (backend process, via P-SB bridge) have disjoint registries; a given `retrieval.*` stage label gets real samples in at most ONE process, so aggregating across both jobs never double-counts and the panel lights up regardless of which process actually executes recall. This neutralizes Risk #2 (a `job=` pin on not-yet-built P-SB code = silent "No data" forever if the process guess is wrong). Keep the `job="yadgar-backend"` pin ONLY where the port is certain and isolated: the backend rerank family `yadgar_embed_rerank_*`.

### (b) Queue visibility — NEW ROW "Queue / DLQ / drainer"

| Panel | Metric family | PromQL |
|---|---|---|
| Queue depth (waiting) [stat] | `yadgar_queue_depth{queue="queue"}` | `yadgar_queue_depth{queue="queue"}` |
| Archive depth [stat] | `yadgar_queue_depth{queue="archive"}` | `yadgar_queue_depth{queue="archive"}` |
| DLQ size [stat, thresholds 1/10] | `yadgar_dlq_size` | `yadgar_dlq_size` |
| DLQ rejection-taxonomy count [stat] | `yadgar_dlq_rejection_count` | `yadgar_dlq_rejection_count` |
| Drainer throughput (records/s) [timeseries] | `yadgar_drain_cycle_duration_ms` (count) | `rate(yadgar_drain_cycle_duration_ms_count[5m])` |
| Drainer lag p95 (ms) [timeseries] | `yadgar_drainer_lag_ms` | `histogram_quantile(0.95, rate(yadgar_drainer_lag_ms_bucket[5m]))` |
| Drain stage p95 by stage (ms) [timeseries, keep from Row 2] | `yadgar_drain_stage_ms{stage}` | `histogram_quantile(0.95, sum by (stage, le) (rate(yadgar_drain_stage_ms_bucket[5m])))` |
| Wiki-add rejections/s by reason [timeseries] | `yadgar_wiki_add_rejected_total{reason}` | `sum by (reason) (rate(yadgar_wiki_add_rejected_total[5m]))` |

### (c) Cache hit/miss/evictions — NEW ROW "Cache"

| Panel | Metric family | PromQL |
|---|---|---|
| Cache hit ratio by cache [timeseries, 0–1] | `yadgar_cache_hit_total{cache}` / `yadgar_cache_miss_total{cache}` | `sum by (cache)(rate(yadgar_cache_hit_total[5m])) / clamp_min(sum by (cache)(rate(yadgar_cache_hit_total[5m])) + sum by (cache)(rate(yadgar_cache_miss_total[5m])), 1)` |
| Evictions/s by cache [timeseries] | `yadgar_cache_evictions_total{cache}` | `sum by (cache)(rate(yadgar_cache_evictions_total[5m]))` |
| Cache size (entries) by cache [timeseries] | `yadgar_cache_size_entries{cache}` | `yadgar_cache_size_entries` |
| Embedding-cache hit ratio (legacy) [stat] | `yadgar_embedding_cache_hits_total` / `..._misses_total` | `rate(yadgar_embedding_cache_hits_total[5m]) / clamp_min(rate(yadgar_embedding_cache_hits_total[5m]) + rate(yadgar_embedding_cache_misses_total[5m]), 1)` |

### (d) Consolidation-cycle health — NEW ROW "Consolidation"

| Panel | Metric family | PromQL |
|---|---|---|
| Consolidation phase p95 (s) by phase [timeseries] | `yadgar_consolidation_duration_seconds{phase}` | `histogram_quantile(0.95, sum by (phase, le) (rate(yadgar_consolidation_duration_seconds_bucket[5m])))` |
| Consolidation cycles/s [stat] | `yadgar_consolidation_duration_seconds` (count) | `sum(rate(yadgar_consolidation_duration_seconds_count[15m]))` |
| Action batch size p95 [stat] | `yadgar_action_batch_size` | `histogram_quantile(0.95, rate(yadgar_action_batch_size_bucket[15m]))` |
| Loop heartbeat age (s) by loop [timeseries, thresholds] | `yadgar_loop_last_run_unix_timestamp{loop}` | `time() - yadgar_loop_last_run_unix_timestamp` |
| Loop errors/s by loop [timeseries] | `yadgar_loop_errors_total{loop,error_type}` | `sum by (loop)(rate(yadgar_loop_errors_total[5m]))` |

---

## Alert rules (condition → metric → threshold)

**Target file:** extend `docs/observability/alerts.yaml` (same `groups[0].name: yadgar`). Existing 5 rules retained. Four new rules:

| Alert | Condition | Metric | Expr | for | severity |
|---|---|---|---|---|---|
| `YadgarDlqNonEmpty` | ANY DLQ entry (tighter than existing >10 warning) | `yadgar_dlq_size` | `yadgar_dlq_size > 0` | 10m | warning |
| `YadgarQueueBacklog` | write queue backing up | `yadgar_queue_depth{queue="queue"}` | `yadgar_queue_depth{queue="queue"} > 500` | 10m | warning |
| `YadgarDrainerStalled` | drainer loop not heartbeating — no cycle completed recently while queue non-empty | `yadgar_drain_cycle_duration_ms_count` + `yadgar_queue_depth` | `(rate(yadgar_drain_cycle_duration_ms_count[10m]) == 0) and (yadgar_queue_depth{queue="queue"} > 0)` | 10m | critical |
| `YadgarUncleanDbStop` | DB integrity / null-embedding invariant breach (proxy for unclean stop) | `yadgar_data_quality_null_embedding_count` | `yadgar_data_quality_null_embedding_count > 0` | 15m | critical |

Notes:
- **Drainer-stalled** is expressed via cycle-count rate rather than a bespoke "stalled" gauge (none exists). Guarded by `queue_depth > 0` so an idle-but-empty drainer does not false-alarm. Backend-side alternative signal if scoped to :8001: `yadgar_embed_queue_drainer_running == 0`.
- **Unclean-DB-stop** has no direct metric; `yadgar_data_quality_null_embedding_count` (metrics.py:891, a hard invariant that should always be 0) is the closest structural-corruption proxy. If a dedicated startup-cleanliness gauge is desired, that is a code change → OUT of scope for this car (flag as open question / follow-up).
- **DLQ-nonempty** intentionally overlaps the existing `YadgarDlqGrowing` (>10) — different threshold/severity intent (any-stuck vs backlog). Keep both or fold; audit decision.
- A `YadgarRecallStageStalled` alert on `yadgar_observe_stage_duration_seconds` is DEFERRED to P-SB (dependency) — do not add until the metric is live, else it fires "no data" perpetually.

---

## Delivery mechanism (as-code)

**Recommendation: dashboards + alerts as-code, shipped in-repo.** This is already the established pattern here (`docs/observability/dashboard.json` + `alerts.yaml` exist and are the source of truth). ADR-0101 (self-sufficiency: no external daemons; in-repo + two-container sufficiency) supports keeping the operator-facing dashboard definitions inside the yadgar repo rather than only in the external nix/dotfiles repo.

- **Edit** `docs/observability/dashboard.json` in place (title → v2, keep uid `yadgar-v1`). Add 3 rows (queue, cache, consolidation), repoint Row 3 recall-stage target, add CE-wall + backend-rerank panels.
- **Edit** `docs/observability/alerts.yaml` — append 4 rules.
- **Deployment (operator step, NOT performed by this car):** the file-drop into `dotfiles/observability/dashboards/` + `dotfiles/observability/alerts/` on `nixos-quinyx` happens in the external nix repo (`grafana.nix` 30s poll auto-loads; no nix rebuild needed for the dashboard JSON, though alert-rule reload may need a Prometheus reload). Document the exact copy/reload commands in `MIGRATION_NOTES.md` for the user to run — per the No-Apply / No-Terraform hard rules, this car does not push to the host.
- **Provenance note:** the repo copy under `docs/observability/` is canonical; the dotfiles copy is a deployment artifact. Keep them in sync (a follow-up could add a CI check or a `make sync-dashboards` — OUT of scope here, flag as open question).

---

## Acceptance criteria

**Manual (operator, post-deploy):**
- [ ] `docs/observability/dashboard.json` parses as valid JSON; loads in Grafana without datasource errors (all targets resolve `datasource.uid=prometheus`).
- [ ] Queue row: DLQ size, queue depth, drainer throughput, drain-stage p95 render with live data on a running instance.
- [ ] Cache row: hit-ratio panels render 0–1 bounded; no divide-by-zero (clamp_min present).
- [ ] Consolidation row: loop heartbeat-age panel shows small values (< cycle interval) on a healthy instance.
- [ ] Recall-stage panels render "No data" pre-P-SB and populate post-P-SB (documents the dependency visibly rather than silently).
- [ ] `alerts.yaml` passes `promtool check rules docs/observability/alerts.yaml` (operator runs; not this car).
- [ ] Existing `yadgar-v1` bookmark still resolves (uid unchanged).

**E2E / automated (in-repo, no live Grafana):**
- [ ] A JSON-validity + PromQL-lint test over `docs/observability/dashboard.json` (parse + assert every `targets[].expr` references a metric name that exists in `metrics.py` / `embed_service_metrics.py` / `observe.py` — reuse or extend any existing dashboard-lint test). This is the guard that catches the 2026-05-23 "panel queries non-existent metric" class of bug.
- [ ] `promtool check rules` on `alerts.yaml` in CI if a promtool step exists; else assert YAML validity + that each alert `expr` metric name is registered.
- [ ] Cross-check: every metric name used by a new panel/alert appears in the verified-live table above (or is flagged `[P-SB]`).

---

## Risks

1. **P-SB slips / changes stage names.** If P-SB renames stages or changes the port mapping, the recall-stage panels + CE-wall PromQL break. Mitigation: merge-dormant (option A) isolates the blast radius to those specific panels; the dotted stage names are quoted from the P-SB plan (authoritative but not-yet-merged — re-verify at build).
2. **Registry-split confusion [NEUTRALIZED].** `yadgar_observe_stage_duration_seconds` lives on the core registry but recall runs backend-side; only the P-SB bridge makes it visible on :8001. A `{job=...}` pin on this not-yet-built path would yield silent "No data" forever if the process guess is wrong. Mitigation (applied): recall-stage PromQL carries NO `job` filter — the two jobs have disjoint registries so cross-job aggregation is safe and process-agnostic. The `job=` pin is retained only for the genuinely-isolated backend rerank family. Residual risk ≈ 0.
3. **Dashboard drift (two copies).** Repo `docs/observability/` vs deployed `dotfiles/observability/`. Manual sync can drift (this already bit the 2026-05-23 session — skeleton drifted from real metric names). Mitigation: the JSON-lint test pins panel exprs to real metric names; recommend a sync check as follow-up.
4. **Alert false-positives.** `YadgarQueueBacklog > 500` and `YadgarDrainerStalled` thresholds are estimates without a baseline. Mitigation: `for: 10m` dampening; tune against `v5.4-baseline.json` / observed steady-state before promoting severity.
5. **Unclean-DB-stop proxy is indirect.** `null_embedding_count` catches corruption, not every unclean shutdown. A true "unclean stop" signal needs code (startup marker). Accepted as proxy; real signal is a follow-up code car.
6. **schemaVersion bump on Grafana round-trip.** If someone edits in the Grafana UI and re-exports, schemaVersion may jump and reorder JSON, producing a noisy diff. Mitigation: edit the JSON by hand; document "do not round-trip through the UI" in the file header comment / MIGRATION_NOTES.

---

## Scope

**IN:**
- Extend `docs/observability/dashboard.json`: repoint recall-stage panel; add queue/cache/consolidation rows; add CE-wall + backend-rerank recall panels.
- Extend `docs/observability/alerts.yaml`: 4 new alerts (DLQ-nonempty, queue-backlog, drainer-stalled, unclean-DB-stop proxy).
- In-repo JSON/YAML validity + metric-name-existence test (if not already present).
- `MIGRATION_NOTES.md` with operator deploy/reload commands.

**OUT:**
- Any yadgar application code change (new metrics, a real "unclean stop" gauge, the dashboard-sync CI check) — separate cars.
- The P-SB fix itself (`psb-observability-2026-07-13.md`) — its own car; this car depends on it.
- Standing up / modifying the external PLT stack (nix repo, `modules/observability/`, Grafana/Prometheus/Tempo config).
- Terraform, container exec, SurrealDB writes, live Grafana deploy — all forbidden by hard rules.
- Tempo/trace-based RED panels (span-metrics generator is OFF on the host — separate enablement).
- Loki log panels (Loki ingestion is an open host-side issue per 2026-05-23 investigation).

---

## Open questions

1. **Sequencing:** merge-dormant (A) or gate-behind-P-SB (B)? (Plan recommends A.)
2. **Dashboard identity:** keep uid `yadgar-v1` + retitle "v2" (bookmark survives), or new uid `yadgar-v2`? (Plan recommends keep uid, retitle.)
3. **Repo↔dotfiles sync:** add a CI sync check / `make sync-dashboards` now, or defer? (Plan: defer, flag as follow-up.)
4. **DLQ alerts overlap:** keep both `YadgarDlqGrowing`(>10) and new `YadgarDlqNonEmpty`(>0), or consolidate?
5. **Unclean-DB-stop:** accept the `null_embedding_count` proxy, or scope a real startup-cleanliness gauge as a prerequisite code car?
6. **Backend drainer signal:** should `YadgarDrainerStalled` also/instead use `yadgar_embed_queue_drainer_running==0` (backend-native, :8001) rather than the core cycle-count proxy?
7. **Existing dashboard-lint test:** does one already exist to extend, or does this car create it? (Needs a quick repo check at build time.)

---

## Version impact

**Config/infra only** — the deliverables are `.json` (Grafana dashboard) + `.yaml` (Prometheus alert rules) + `MIGRATION_NOTES.md`. No `.py` change.

Per the Test-Driven hard rule's scope clause ("application/library code only — not config, infra (.nix, .tf, .yaml), or one-line edits"), the dashboard JSON and alert YAML are **exempt from the failing-test-first requirement**. The recommended JSON/YAML validity + metric-name-existence test is a *safety net for the config*, not a red-green-refactor gate — it may be written after the config, or reuse an existing lint harness. No yadgar version bump is required (no shipped code changes); this rides as a docs/observability edit. If a lint test is added under `tests/`, that test file IS code and follows normal conventions, but it tests config, so its own "failing first" is trivially the parse/assert.

---

## AUDIT (2026-07-13)

Method: source-grep + fresh Python import of `observe.py` + **live curl of both `/metrics` endpoints** (read-only triage, sanctioned by CLAUDE.md). Live scrape is the load-bearing evidence — it converts the plan's abstract registry-split topology into fact.

### Per-claim verdict table

| # | Claim (plan) | Verdict | Evidence (file:line / scrape) |
|---|---|---|---|
| 1 | `docs/observability/dashboard.json` = "Yadgar Observability v1", uid `yadgar-v1`, 20 panels, 6 rows, schema 36 | **VERIFIED** | file parses: title/uid/schemaVersion/20 panels/6 rows all match exactly |
| 2 | `docs/observability/alerts.yaml` = 5 rules (drainer lag, DLQ>10, recall slow, backend unreachable, CB stuck) | **VERIFIED** | alerts.yaml: `YadgarDrainerLagHigh`, `YadgarDlqGrowing`(>10), `YadgarRecallSlow`, `YadgarBackendUnreachable`, `YadgarCircuitBreakerStuck` |
| 3 | This is v1→v2 EXTENSION, not greenfield | **VERIFIED** | both files exist and are the source of truth; plan edits in place |
| 4 | Row 3 "Recall stage durations p95" queries `yadgar_recall_stage_ms{stage}` | **VERIFIED** | dashboard.json Row 3 target: `histogram_quantile(0.95, sum by (stage, le) (rate(yadgar_recall_stage_ms_bucket[5m])))` |
| 5 | `yadgar_recall_stage_ms` is "effectively-dead legacy" | **VERIFIED (with correction)** | Registered on core `_registry` (metrics.py:294) AND actively `.observe()`'d (scoring.py:19, reranking.py:35) — so "dead" is FALSE at the code/registration layer. But **dead-on-scrape is TRUE and that is what matters**: :8765 serves only HELP/TYPE headers, **zero `_count`/buckets**, despite 76 recalls having run (`yadgar_recall_duration_ms_count 76.0`). :8001 serves **zero** `recall_stage_ms` lines. Root cause = registry-split: emitted from `backend/retrieval/*` (backend process) but registered on core `_registry` object → lands in a registry served by neither port. Repoint is justified; the *reason* is registry-split, not "legacy/unused". Fix the plan's wording (§0.1 BLUF, §a) to say "dead-on-scrape via registry-split" not "legacy". |
| 6 | `yadgar_observe_stage_duration_seconds` is dead today (circular import) | **VERIFIED** | Empirical import: `_PROM_AVAILABLE=False`, `_STAGE_DURATION is None`, family ABSENT from `_registry`. Live scrape: zero `observe_stage_duration` lines on :8765 AND :8001. Swallowing `except Exception` at observe.py:56-64 confirmed. |
| 7 | P-SB (`docs/plans/psb-observability-2026-07-13.md`) is the unblocker; status AUDITED/build-GO | **WRONG (file absent)** | `docs/plans/psb-observability-2026-07-13.md` **does not exist**. No file under `docs/plans/` matches the circular-import-fix / bridge-collector work by any name (`obs-velocity-completion`, `full-observability-standard`, `hook-recall-cache-track-a` checked — none is the P-SB fix). The dependency is currently **unbuildable / untracked**. See User-decision A. |
| 8 | Queue/DLQ/drainer families live on :8765 | **VERIFIED** | scrape :8765: `yadgar_queue_depth{queue=...}`, `yadgar_dlq_size 0.0`, `yadgar_dlq_rejection_count 0.0`, `yadgar_drain_cycle_duration_ms_*` all present. |
| 9 | `yadgar_dlq_rejection_count` registered (metrics.py:227) | **VERIFIED** | metrics.py:227 Gauge on `_registry`; live on :8765. |
| 10 | Cache families (`cache_hit_total`/`miss`/`evictions`/`size_entries`) + legacy embedding-cache | **VERIFIED** | metrics.py:108/115/127/134/77/83; cache/consolidation/loop family lines present on :8765 (35 matches). |
| 11 | Consolidation families (`consolidation_duration_seconds`, `action_batch_size`, `loop_last_run_unix_timestamp`, `loop_errors_total`) | **VERIFIED** | metrics.py:68/90/777/785; present on :8765. |
| 12 | `yadgar_recall_duration_ms` live today (Row 3 keep) | **VERIFIED** | metrics.py:259; :8765 `_count 76.0`, `_sum 522750.7` — real samples. |
| 13 | Backend rerank `yadgar_embed_rerank_duration_seconds{mode}` on :8001 | **VERIFIED** | scrape :8001: present with `mode="ce"`/`mode="nli"` series. |
| 14 | `drain_cycle_duration_ms` (throughput panel + DrainerStalled alert) live on :8765 | **VERIFIED** | scrape :8765 present (count=0 idle, series exists). Drainer emits **core-side** → split is selective & coherent: drainer core, retrieval backend. Plan's "live today" label correct. |
| 15 | Unclean-DB-stop proxy = `yadgar_data_quality_null_embedding_count` (real gauge, should be 0) | **VERIFIED (honest proxy)** | metrics.py:885 (plan cites 891 — minor drift), Gauge on `_registry`; :8765 `0.0`. It IS a real gauge and IS a hard-invariant visibility metric. Plan honestly caveats it catches corruption, not shutdown-cleanliness. Accept as proxy; flagged as such. |
| 16 | Line citation `null_embedding_count` at metrics.py:891 | **STALE** | actual def at metrics.py:885 (6-line drift, harmless). |
| 17 | Line citation `dlq_rejection_count` at metrics.py:227 | **VERIFIED** | exact. |
| 18 | Metric files at `_shared/observability/metrics.py`, `.../observe.py`, `backend/embed_service/embed_service_metrics.py` | **VERIFIED** | real files (1202/…/503 lines). Note: `_shared/metrics.py` + `backend/embed_service_metrics.py` are now **PEP-562 shims** (T2 Car D1/D2, ADR-0084) — plan targets the real post-reorg paths correctly. |
| 19 | ADR-0101 (self-sufficiency) supports in-repo dashboards-as-code | **VERIFIED (exists)** | ADR-0101 present in ADR log (ADRs run to 0106). Citation is directionally sound; delivery-as-code matches the existing `docs/observability/` convention. |
| 20 | Registry-split: :8765 core `_registry` vs :8001 isolated `CollectorRegistry()`, disjoint | **VERIFIED** | metrics.py:47 `_registry = CollectorRegistry()`; embed_service_metrics.py:48 separate `_registry`. Confirmed empirically by claim #5. |
| 21 | Delivery: as-code in-repo, host-deploy via MIGRATION_NOTES, no push/apply | **VERIFIED / conformant** | No terraform, no code, no live stand-up. ADR-0101-conformant. Meets No-Apply / No-Terraform hard rules. |
| 22 | TDD exemption for `.json`/`.yaml` | **VERIFIED** | matches Test-Driven scope clause ("not config, infra"). Lint test as safety-net (not red-green gate) is the correct framing. |
| 23 | Recall-stage PromQL carries NO `job` filter (neutralizes Risk #2) | **VERIFIED sound** | disjoint registries → a `retrieval.*` stage label gets samples in ≤1 process, so cross-job aggregation never double-counts. Correct mitigation. |
| 24 | Merge-dormant (option A) makes recall row "temporarily" dead until P-SB | **WRONG (conditional)** | Only "temporary" IF P-SB gets built. With P-SB **absent** (claim #7), option A ships a **permanently** dead recall row — a silent regression from today's already-dead-but-labelled Row 3. Materially changes the recommendation. See User-decision A. |

### Verdict Status

**REVISE — build-conditional (do NOT flip to REJECT).** The plan is 90% sound: queue/DLQ/drainer, cache, and consolidation rows + 3 of 4 alerts consume metrics that are live-on-scrape TODAY (verified by curl) and can ship immediately with real value. The delivery mechanism, hard-rule conformance, registry-split reasoning, and no-`job`-filter mitigation are all correct.

Two defects block a clean GO:

1. **P-SB is a dangling reference (claim #7, WRONG).** The plan's entire recall-latency arm depends on a plan file that does not exist and whose *work* (break circular import + backend bridge collector) is not tracked anywhere in `docs/plans/`. Merge-dormant option A therefore ships a permanently-dead recall row, not a temporarily-dead one. **Resolve before build.**
2. **Headline wording overstates "legacy" (claim #5).** `recall_stage_ms` is not legacy/unused — it is actively emitted but dead-on-scrape due to registry-split. The repoint is still correct, but the *rationale* must be fixed so the reader (and the metric-name-existence lint) understands the real failure mode. A lint that only checks "name is registered somewhere" would PASS `recall_stage_ms` and MISS that it's served with no data — the lint must assert **served-on-a-scrape-job**, or at minimum flag register-core/emit-backend families.

Everything else: VERIFIED. STALE count: 1 (line-891 drift). WRONG count: 2 (claim #7 P-SB file absent; claim #24 dormancy is permanent-not-temporary given #7). No panel/alert points at a non-existent metric — every consumed family is registered; the only served-with-no-data family is `recall_stage_ms` (the one being repointed AWAY from) and `observe_stage_duration_seconds` (the P-SB target, knowingly dormant).

### User-decisions required

- **A (BLOCKING) — P-SB provenance.** `psb-observability-2026-07-13.md` is absent. Choose: **(A1)** author/locate the P-SB fix plan first and gate this car behind it (option B); **(A2)** proceed merge-dormant (option A) but explicitly accept the recall row renders "No data" **indefinitely** until the circular-import fix + backend bridge land under *some* tracked plan; or **(A3)** drop the recall-latency arm from this car entirely and ship only the 3 independent live rows + 3 independent alerts now (recall repoint becomes a follow-up bundled with the P-SB fix). Recommendation: **A3** — decouples the 90% that works today from the unbuilt dependency; cleanest blast radius.
- **B — headline wording.** Fix BLUF §0.1 and §a to state `recall_stage_ms` is *dead-on-scrape via registry-split* (emitted backend-side, registered core-side, served by neither port), not "legacy". Load-bearing for the lint design.
- **C — metric-name-existence lint scope.** The lint is worth adding, but "name exists in `metrics.py`" is insufficient — it would green-light `recall_stage_ms`. Scope it to assert the family is served on the scrape job the panel targets (or flag register-core/emit-backend split families). Otherwise the lint gives false assurance for exactly the bug class this plan exists to fix.
- **D — line-drift fix.** Update `null_embedding_count` citation 891 → 885 (trivial).
- **E — carried-over open questions (plan §Open questions 1,2,4,6) remain valid** and are genuine user-calls: uid keep-vs-new, DLQ alert overlap (`YadgarDlqGrowing>10` vs new `YadgarDlqNonEmpty>0`), backend-native drainer signal (`yadgar_embed_queue_drainer_running==0` on :8001) vs core cycle-count proxy. No blocker; author's recommendations are reasonable.
