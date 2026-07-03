# Yadgar plan roadmap

Single source of truth for **open** plans. Shipped/dead plans live in
`archive/` (kept for history; do not edit).

## Convention (read before adding a plan)

- **Slug-named, not version-named.** Plans live at `docs/plans/<slug>.md`. The
  filename is stable identity; it never gets renamed for reprioritization.
- **Version assigned at ship, not in the filename.** A plan gets its `vX.Y.Z`
  only when it ships — recorded in `docs/CHANGELOG.md` + the git tag, and as the
  `target`/`shipped` field here. (The old `PLAN_V5_NN_TOPIC.md` scheme caused
  constant renumbering drift — that's why ~95 docs sit in `archive/`.)
- **Register every new plan in this file.** If it's not here, it's not tracked.
- **On ship:** move the plan doc to `archive/` and mark it shipped in CHANGELOG.

## Latest shipped

- **v5.104** (#156) — test-speed train (ADR-0036): module-scope per-test `storage` StorageEngine (~5.3× setup) + batched surreal wipe (~18× teardown) + spreading-activation N+1 batch (recall ~5s → tens-ms) → CI shards ~2× faster. Corrected ADR-0027's schema-init premise.
- **v5.100–v5.102** (#148/#150/#152) — observability: fine-grained OTEL spans (v5.100), tri-signal P0 (v5.101 — `@observe` + I33 lint + histogram p95 fix + core→backend traceparent; ADR-0034), recall trace-gap spans + batched heat-writes (v5.102). Recall FULLY accounted via MCP-tool traces: CE ~90%, fusion pass quality-load-bearing (ADR-0035). CI: never `OTEL_SDK_DISABLED` (ADR-0037).
- **v5.97–v5.99** (#143/#144/#146) — recall perf: fusion N+1 + MMR fold-in (v5.97), GTE-ModernBERT rerank Lever-1 (v5.98), PPR + spreading-BFS N+1 (v5.99).
- **v5.87** (#128) — viz-UX overhaul (live-feedback): physics edge-release fix (hidden edges drop from the d3 force → nodes separate), slow-reload fix (warm-start positions from localStorage), semantic edge removed; menu IA 8→4 (Graph/Bookmarks/System/Help), About config-strip, cluster panel default-off + View toggle; config editor grouped-by-category + alpha + emptied misc catch-all + per-knob tooltip & Config Reference page. Deferred: config-panel P3/P4 (#60), Prometheus retention (#53), viz-triage remainder (#55).
- **v5.86** (#127) — viz regression train: CPU rAF-pause + idle-debounce, search exact-title + edge-dim fixes, honest legend, 3D render-path overhaul + interaction layer (hover/focus/badge/filters/hide-mode), data fidelity (resolved_by fix, mem↔wiki bridge, cluster member_count, dropped imports/calls); OT-C4 incremental similarity-linking + mandatory full-reconcile (default-off, ADR-0008); config editor un-gated (`GET /api/control/config`) + unified `set_config_value`; `adr_add` multi-line-field fix. Deferred to v5.87: config-panel P3/P4 (#60), Prometheus retention (#53).
- **v5.85.1** (`3773ce9`) — agent-prompt capture loop: stop-hook step + `project_brief` nudge (#126).
- **v5.85** (`426768c`) — int8-onnx CE backend + wiki auto-linking (`wiki_autolink` MCP tool) + repo-wiki store-bridge (#36) + agent-prompt library rework (ADR-0007) + viz /api/control extend.
- **v5.84** (`6e1629c`) — improvement train (#124): ADR-capture tooling + consolidation perf + bug fixes.
- **v5.83** (`2785d9c`) — obs-train: /health 503 contract + OTLP circuit breaker + ADR-capture prompt redesign (#121/#122).
- **v5.81** (`091d958`) — viz-fidelity-v2 (BC-VZ-R1/2/3) + wiki BC-G10 + landscape recall (BC-AC3a) + MCP tool descriptions.
- **v5.80** (`22206d9`) — unified-recall default-flip + fan-out fusion regression fixes.
- **v5.79** (`52e81cf`) — unified-scoped-recall steps 0/3/4/5 (test-first redo, real e2e).

> **Recall is DONE (don't re-plan it).** `recall(query, directory=…)` already fetches **everything** — memory + wiki merged into one ranked list (default `type="all"`). `UNIFIED_RECALL_ENABLED` is `True` in prod; `type=memory|wiki` narrows; `mode="landscape"` is the optional slow cross-domain mode. Steps 0–2 shipped v5.78; the 3–5 test-first redo shipped v5.79; default flip v5.80. Behaviour-locked: BC-U1..U8, BC-G11, BC-AC3a all ✅ in `BEHAVIOR_CONTRACT.md` with live-DB e2e. The old `unified-scoped-recall*` plan docs are archived.
- **v5.78** (`09138c3`) — Wave-2: recall-rebuild 0–2 + fresh-memory tools + repo-wiki-native.
- **v5.76** (`f28faf5`) — Wave-1: dead-config #41 + viz #33 (viz-data-fidelity) + e2e/cogmap.

## Open plans (by priority)

### Active / next
| Plan | Theme | Status | Notes |
|------|-------|--------|-------|
| [improvement-train](improvement-train.md) | umbrella (#29) | **NEW — 3 PRs (A perf / B adr / C bugs)** | Umbrella for issue #29: A=embedding-scan perf + int8 CE + scrape tune; B=ADR-capture follow-through (P0 shipped, P0.5/#19 + P1/P2/P3 open); C=bug fixes (#9 stale-gate, #10 convention, #25 COMET-warn likely-closed, #21 recall flake). Suggested order C→B→A. Several cars already DONE/CLOSED — see umbrella. |
| [en2a-comet-fpa-v5.82](archive/en2a-comet-fpa-v5.82.md) + [comet-retire-dormant](archive/comet-retire-dormant.md) | enrichment / eval | **SHIPPED → archived — ADR-0004 RETIRE (in CHANGELOG `[Unreleased]`)** | Ablation concluded: un-FPA'd COMET net-negative recall (−4.2pt) at ~17h/10-core → retired to dormant (flag default True→False), BC-EN2b warning shipped. NOT improvement-train #29. Both plan docs archived 2026-06-25; verdict report `benchmarks/reports/en2a_comet_ablation_2026-06-24.md`. |
| [adr-capture-system](adr-capture-system.md) | memory / decisions | **P0 SHIPPED (#121); P0.5/#19 + P1–P3 open** | ADR capture into Yadgar wiki (per-project page, source of truth). P0 (stop-hook capture-first + 11-field schema) shipped `eeaec40`. Open: #19 read-side branch_hint (P0.5), `adr_add`+`adr_due` (P1), project_brief surfacing (P2), models.py ADR shape (P3). Group B of improvement-train. |
| [db-audit-fix](db-audit-fix.md) | data-integrity | **skeleton — discuss first** | Audit + fix live-store issues (legacy `last_decay_at`, 6-week-dead aftermath, entity heat, wiki↔memory link, archive tier, orphans). User has thoughts to bring before scoping. |

### In-flight migrations / investigations
| Plan | Theme | Status | Notes |
|------|-------|--------|-------|
| [wiki-kb-usefulness-snr](wiki-kb-usefulness-snr.md) | wiki-kb | live decision log | Recall-SNR investigation (recall was 37.5% noise; `directory=` no-op; mis-stamp sinks). Feeds the recall train; D1: wiki↔memory linkage dropped (unused field). |

*(`wiki-restamp-migration` completed 2026-06-23 — global wiki count 616 → 5 (legit cross-project remainder); final 3 stragglers cleared via the v5.81 all-rows `wiki_set_metadata`. Archived.)*

### Infra / ops
| Plan | Theme | Status | Notes |
|------|-------|--------|-------|
| [ce-perf-options](ce-perf-options.md) | infra-ops | **#1 recall lever (ADR-0035) — option B chosen, buildable** | CE onnx-int8 (Lever-3, #13) is the top go-forward recall lever per ADR-0035 (CE = ~90% of recall latency; call-count is quality-load-bearing so speed CE, don't cut passes). Option B (int8/ONNX CE) picked 2026-06-25 → concrete §"Option B" added (load path `ml_client._try_st_cross_encoder`, gated, fp32 default). Blocked on a working onnxruntime backend image + backend_version bump + a LongMemEval quality gate. improvement-train A2 / #4. |
| [full-observability-standard-2026-07-03](full-observability-standard-2026-07-03.md) | observability | **P0 SHIPPED v5.101; per-area rollout remains** | Tri-signal standard (span+metric+log per function). P0 shipped v5.101 (`@observe` + I33 lint warn-mode + histogram fix + traceparent; ADR-0034). Remaining: per-area rollout (flip I33 lint to hard-fail per area) + backend fine-spans. |
| [cpu-burst-rootcause-and-embedding-scan-fix](cpu-burst-rootcause-and-embedding-scan-fix.md) | perf | **Part 2 buildable; Part 1 OPEN (host-side)** | Part 2 = kill `SELECT *` consolidation scans (projection + server-side decay + sample-then-fetch + incremental linking). Part 1 = fan-burst still open (observer-effect / surrealkv suspect, hand to user). improvement-train A1 / #30–33. |
| [process-exporter-scrape-interval](process-exporter-scrape-interval.md) | observability / nix | **nix-only — hand to user** | High-res Prometheus scrape 2s→5s (observer-effect diagnostic). No in-repo change; edit `~/git/nix/modules/observability/prometheus.nix`. improvement-train A3 / #34. |
| [anchor-signal-gap](anchor-signal-gap.md) | signals / anchors | **NEW — buildable** | project_brief over-signals `audit_anchors` (count>15 gate ignores actionable items) + phantom action names (`forget_expired_anchors` etc. aren't tools). improvement-train B5 / #20. |
| [comet-dormant-startup-warning](comet-dormant-startup-warning.md) | observability | **likely CLOSED — user decision** | #25: warning IS reached on streamable-http (refutes ticket premise); residual = server-log vs client-visible. improvement-train C3. |
| [recall-content-integrity-flake](recall-content-integrity-flake.md) | recall / test | **NEW — investigate** | #21: unquarantine `test_specific_detail_preserved`; it's a ranking miss (PAT vs "personal access token"), not content drop. Don't overfit recall. improvement-train C4. |
| [roadmap-freshness](roadmap-freshness.md) | infra-ops | deferred (v5.99 — design open) | Roadmap-staleness signal; deferred on an async-write-queue design problem. |
| [viz-config-control-panel](viz-config-control-panel.md) | viz / config / ops | **skeleton — discuss first** | Browser config control panel in the viz UI: view/edit all 299 settings (source/restart/destructive metadata) via extended `/admin/config` + `PATCH /admin/config/<key>` sanctioned writer; guarded restart + armed destructive confirm + audit. Motivated by the CLI `COLD_MEMORY_PURGE_ENABLED` flip + manual restart this session. Mockup: `viz-config-control-panel.mockup.html`. |
| [ci-velocity-train-2026-07-03](ci-velocity-train-2026-07-03.md) | test-perf / ci | **test-speed leg SHIPPED v5.104; #83/#79 remain** | Unified velocity plan. #84 test-speed leg SHIPPED v5.104 (PR #156, ADR-0036 — module-scope `storage` fixture + batched wipe → CI shards ~2×; corrected ADR-0027's schema-init premise). Feeder [test-suite-speedup-2026-07-01](archive/test-suite-speedup-2026-07-01.md) archived. Remaining: #83 backend-bump CI gate + conditional image builds, #79 load-test contract. |

### Horizon — v6 / v7 (skeletons + indices, not ready to build)
| Plan | Theme | Notes |
|------|-------|-------|
| [v6-parallel-trains](v6-parallel-trains.md) | v6 index | Execution index for remaining #82+ trains; sequencing/dependencies. |
| [PLAN_V6_QUALITY_FOUNDATION](PLAN_V6_QUALITY_FOUNDATION.md) | v6 north-star | Measurement harness → surprise-gate ON → enrichment/retrieval ablations → consolidation efficacy → LLM synthesis. |
| [v6-llm-curator](v6-llm-curator.md) | future | LLM curator cycle scaffold. |
| [v6-extract-on-ingest](v6-extract-on-ingest.md) | future | LLM extract-on-ingest (Adopt-7). |
| [v7-team-usability](v7-team-usability.md) | future | Team usability / multi-user architecture. |

## Unfiled (no plan doc yet — candidates)
- **consolidate `light` latency** — `light` mode took ~5.7 min live (MCP client timed out though server finished). Likely first-run `last_decay_at` backfill + episode/CLS/causal phases at scale. Needs a perf plan if it persists.
- **wiki AWS-inventory archive** — ~1547 inventory-tier wiki pages flagged in the v5.58 wiki audit (source_count=0, orphaned). Cleanup decision parked.

## Archive
`archive/` holds ~95 shipped/dead plan docs (v5.2 → v5.81). Reference only — do not edit.
