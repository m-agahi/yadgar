# Yadgar plan roadmap

Single source of truth for **open** plans. Shipped/dead plans live in
`archive/` (kept for history; do not edit).

## Convention (read before adding a plan)

- **Slug-named, not version-named.** Plans live at `docs/plans/<slug>.md`. The
  filename is stable identity; it never gets renamed for reprioritization.
- **Version assigned at ship, not in the filename.** A plan gets its `vX.Y.Z`
  only when it ships — recorded in `docs/CHANGELOG.md` + the git tag, and as the
  `target`/`shipped` field here. (The old *PLAN_V5_NN_TOPIC.md* scheme caused
  constant renumbering drift — that's why hundreds of docs now sit in
  `archive/`: 209 as of 2026-08-01. That count only grows; don't hand-maintain
  it in prose.)
- **Register every new plan in this file.** If it's not here, it's not tracked.
- **On ship:** move the plan doc to `archive/`, mark it shipped in CHANGELOG,
  AND update this file's link to the `archive/` path in the *same* commit — a
  link still pointing at `docs/plans/<slug>.md` after the file moved is a dead
  reference. `scripts/check_roadmap_links.py` (pre-commit + CI, Car 0026) now
  catches this class of drift: 11 dead references (9 stale root-relative links
  to since-archived plans, one link to a plan renamed before it ever shipped,
  one backticked path orphaned by a docs move) plus 16 backtick bare-filename
  mentions missing their `archive/` prefix survived undetected for 2+ weeks
  before this guard existed.

## Latest shipped

*(Curated highlights, not an exhaustive per-version log — see below for the
gap. Full per-version detail lives in `docs/CHANGELOG.md`.)*

- **v5.171 (in flight)** — bug-fix train off `feat/v5.171-bug-train`, not yet
  merged to master. Backend DB-mount convergence (Car 0100, core 5.170.1) +
  vacuum container side-build (Car 0092, core 5.170.9) + runtime-agnostic
  systemd readiness (Car 0105, core 5.170.12) have shipped on the branch and
  archived —
  [converge-backend-db-mount-2026-08-01](archive/converge-backend-db-mount-2026-08-01.md),
  [vacuum-container-side-build-2026-08-01](archive/vacuum-container-side-build-2026-08-01.md),
  [runtime-agnostic-systemd-readiness-2026-08-01](archive/runtime-agnostic-systemd-readiness-2026-08-01.md).
- **v5.170.0** (#20, 6 cars) — install-generated backend units now forward
  `YADGAR_MCP_AUTH_TOKEN` into the container (every `/admin/*` call 503'd on a
  fresh install otherwise, ADR-0180); vacuum wedge fixed — `surreal` binary
  preflight + `.pre-vacuum-*` abort-path pruning (Bug 1 of
  [fix-vacuum-reclaim-and-core-stability-2026-07-29](fix-vacuum-reclaim-and-core-stability-2026-07-29.md));
  host CLI OTLP import-tax fix (`yadgar restore`/`yadgar drain` 8.2s→0.20s via
  a `core/forward.py` leaf-module split); three `/health`→`/health/live`
  call-site corrections (ADR-0019 follow-up + new
  `check_health_endpoint_semantics.py` lint); PyYAML-undeclared-dependency
  fix; vacuum export-scratch unbounded-growth fix.
- **v5.169.0** (#15, 13 cars) — install-path + runtime train: vacuum
  rollback, podman daemon crash, dead guards. See `docs/CHANGELOG.md` for
  per-car detail (not restated here).
- **v5.166.0 + v5.166.1** — OpenCode hook port train, 10 cars + 4 follow-up
  commits ([opencode-hook-port-train-2026-07-26](opencode-hook-port-train-2026-07-26.md),
  ADR-0168 locks 6 design decisions). 4-of-5 functional + 1-of-5 non-blocking
  hook layer shipped. F4–F7 follow-ups done in v5.166.1; F1–F3 deferred
  (env-gated) — see
  [followup-opencode-port-2026-07-26](followup-opencode-port-2026-07-26.md).

**Known gap (reported, not fixed by this doc):** `docs/CHANGELOG.md` has not
cut a version-numbered `## [x.y.z]` section since `[5.106.0]` — every shipped
version from 5.107.0 through the current 5.170.0 sits undifferentiated inside
`[Unreleased]`. That's a CHANGELOG-hygiene defect and a separate car; it's why
the highlights above were hand-picked from commit history + individual plan
docs rather than from a clean per-version CHANGELOG cut.

- **v5.117.0 / backend 5.30** (#169/#170/#171) — Reorg R2a/R2b/R3: composition-root restructure (zero `_shared→core`), dynamic `module.qualname` span naming, write-path relocation (compute cluster + R5 writes → backend `/admin`). Task #17 folder-split complete. Tests mirrored to backend; CI regrouped. ADR-0062 codified modular-layer-coherence + forward-only standing directive. MCP tool split (#43) also landed.
- **v5.104** (#156) — test-speed train (ADR-0036): module-scope per-test `storage` StorageEngine (~5.3× setup) + batched surreal wipe (~18× teardown) + spreading-activation N+1 batch (recall ~5s → tens-ms) → CI shards ~2× faster. Corrected ADR-0027's schema-init premise.
- **v5.100–v5.102** (#148/#150/#152) — observability: fine-grained OTEL spans (v5.100), tri-signal P0 (v5.101 — `@observe` + I33 lint + histogram p95 fix + core→backend traceparent; ADR-0034), recall trace-gap spans + batched heat-writes (v5.102). Recall FULLY accounted via MCP-tool traces: CE ~90% _(corrected to ~25% cold by ADR-0105 (#192); Ettin shipped Train 4)_, fusion pass quality-load-bearing (ADR-0035). CI: never `OTEL_SDK_DISABLED` (ADR-0037).
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

> **Recall is DONE (don't re-plan it).** `recall(query, directory=…)` already fetches **everything** — memory + wiki merged into one ranked list (default `type="all"`). `UNIFIED_RECALL_ENABLED` is `True` in prod; `type=memory|wiki` narrows; `mode="landscape"` is the optional slow cross-domain mode. Steps 0–2 shipped v5.78; the 3–5 test-first redo shipped v5.79; default flip v5.80. Behaviour-locked: BC-U1..U8, BC-G11, BC-AC3a all ✅ in `docs/contracts/BEHAVIOR_CONTRACT.md` with live-DB e2e. The old `unified-scoped-recall*` plan docs are archived.
- **v5.78** (`09138c3`) — Wave-2: recall-rebuild 0–2 + fresh-memory tools + repo-wiki-native.
- **v5.76** (`f28faf5`) — Wave-1: dead-config #41 + viz #33 (viz-data-fidelity) + e2e/cogmap.

## Open plans (by priority)

### Active / next
| Plan | Theme | Status | Notes |
|------|-------|--------|-------|
| [fix-vacuum-reclaim-and-core-stability-2026-07-29](fix-vacuum-reclaim-and-core-stability-2026-07-29.md) | vacuum / stability (#45, #27) | **PARTIALLY SHIPPED (v5.169.0, #15) — NOT archived, per ADR-0081** | Bug 1 (reclaim never persisted) + Bug 2a (aborts left core stopped): SHIPPED. Bug 2b (core SIGKILL during consolidation): NOT BUILT, recommend closing — 61 core startups vs 60 signal handlers, 0 unpaired across 15 days of `yadgar.log`; doesn't reproduce. |
| [fix-code-graph-digest-budget-and-orphans-2026-07-29](fix-code-graph-digest-budget-and-orphans-2026-07-29.md) | code_graph (#87) | **PARTIALLY SHIPPED (v5.169.0, #15) — NOT archived, per ADR-0081** | Car 1 (stale-marker truncation): SHIPPED. Car 2 (orphan index-cache prune, 746 MB / 37 files at `~/.cache/yadgar/code_graph`, plus a wrong path recorded in `CAPABILITY_REGISTRY.md:1935`): NOT BUILT, tracked as task:0087. |
| [converge-backend-db-mount-2026-08-01](archive/converge-backend-db-mount-2026-08-01.md) | install / vacuum (#0100) | **SHIPPED (core 5.170.1, `98097cf6`, v5.171 train)** | Three install paths mounted the backend DB three different ways; converged on the host bind mount all three now use. Unblocked #0092 (vacuum container side-build). |
| [vacuum-container-side-build-2026-08-01](archive/vacuum-container-side-build-2026-08-01.md) | vacuum (#0092) | **SHIPPED (core 5.170.9, `d2f93442`, v5.171 train)** | Runs the vacuum side-build in a one-shot backend container so container installs stop skipping vacuum. Plan §6 (neither `yadgar-vacuum.service.in` nor `flake.nix` sets PATH for the vacuum unit — which decides whether a host takes the host-binary or the container branch) deliberately left open, tracked as its own task. |
| [runtime-agnostic-systemd-readiness-2026-08-01](archive/runtime-agnostic-systemd-readiness-2026-08-01.md) | install / systemd (#0105) | **SHIPPED (core 5.170.12, `01bb4c11`, v5.171 train)** | Closed the docker half of the install path: generated units used `Type=notify` with no `READY=1` source, and docker has no sd_notify proxy at all. Plan §6 stated residuals: residual (1) (podman arm's missing `TimeoutStartSec`) discharged by Car 0106 (core 5.170.13); residual (2) (`yadgar-backend.service.in` is `Type=simple`, so it gives no readiness guarantee) still open. |
| [recall-scoring-c4-2026-07-18](recall-scoring-c4-2026-07-18.md) | recall / scoring (#62) | **DECISIONS LOCKED — C4.0/C4.1/C4.3 BUILT (v5.151.0); 1b-fix + S3 DEFERRED** | C4 recall-scoring scoping plan; tie-order + abbreviation-miss cars landed, corpus-side SNR (S3) and the 1b-fix remain deferred. |
| [tdd-hardening-pipeline-2026-07-18](archive/tdd-hardening-pipeline-2026-07-18.md) | dev-process / quality | **codified v5.150.0 (#213)** | 5-phase pipeline (RED-VERIFY → adversarial critic → green → mutation+fuzz → gates) codified into `implement-tdd` agent-prompt + weekly mutation-sweep workflow. |
| [db-audit-fix](db-audit-fix.md) | data-integrity | **DEFERRED INDEFINITELY** | Re-audited 2026-07-16: store healthy. Residuals: ~5 dangling edge rows + unverifiable `last_decay_at` — not worth work. |

### In-flight migrations / investigations
| Plan | Theme | Status | Notes |
|------|-------|--------|-------|
| [investigation-migration-script-system-2026-07-26](investigation-migration-script-system-2026-07-26.md) | db-migrations (#45) | **undecided — review-only** | Survey of the DB migration-script system now that yadgar has multiple users; design proposed, no commitments. Sister to `surrealmigrate-fork-2026-07-26` and `settings-to-db-config-migration-2026-07-24`. |
| [surrealmigrate-fork-2026-07-26](surrealmigrate-fork-2026-07-26.md) | db-migrations | **undecided — review-only** | Proposal to fork `surrealdb/surrealkit` for LLM-authoring/Python/embedded-mode work while upstreaming small Flyway-parity gaps. No commitments. |
| [settings-to-db-config-migration-2026-07-24](settings-to-db-config-migration-2026-07-24.md) | config / runtime_config (#35) | **DRAFT / SKELETON — investigation in flight** | Survey of `Settings`' ~335 knobs for which are migratable to the DB-backed `runtime_config` store (ADR-0163). Fill the inventory when the #35 survey lands. |
| [task-table-refactor-2026-07-29](task-table-refactor-2026-07-29.md) | data-model (#0047, #0080) | **AUDITED (build-with-changes) — not started** | `_LedgerMixin` spine: dedicated tables for `task`, `adr`, `agent_prompt`. ADR-0182 (supersedes ADR-0181). Audit findings folded into the doc; related to #0095–#0098. |
| [drift-axis-sweep-2026-06-30](drift-axis-sweep-2026-06-30.md) | dev-process | **scoping (plan-only; no code in this doc)** | Multi-axis drift ratchet — config-knob axis already hard-ratcheted (I25 + phantom-doc guard); other axes (this doc) not yet scoped into a build. |
| [harness-task-seed-inbound-2026-07-17](harness-task-seed-inbound-2026-07-17.md) | harness task-list / hooks | **PROPOSED — decision required before build** | Inbound leg (yadgar → Claude Code harness `TaskList`) to complement the outbound leg (`wiki_write_task_list`), which already works. |
| [fix-agent-prompt-scoping-drift-2026-07-30](fix-agent-prompt-scoping-drift-2026-07-30.md) | wiki / scoping (#0093) | **planned, not started** | One-time data cleanup for 9 legacy-scoped wiki rows (8 agent-prompt pages + `model-tier-dispatch`). The write-time enforcement that prevents new drift already shipped 2026-07-22 — this plan is cleanup of pre-existing rows only. |

*(`wiki-restamp-migration` completed 2026-06-23 — global wiki count 616 → 5 (legit cross-project remainder); final 3 stragglers cleared via the v5.81 all-rows `wiki_set_metadata`. Archived.)*

### Infra / ops
| Plan | Theme | Status | Notes |
|------|-------|--------|-------|
| [cpu-burst-rootcause-and-embedding-scan-fix](cpu-burst-rootcause-and-embedding-scan-fix.md) | perf | **Part 2 buildable; Part 1 deferred** | Part 2 = kill `SELECT *` consolidation scans (projection + server-side decay + sample-then-fetch + incremental linking). Part 1 = fan-burst deferred (task #26: likely non-yadgar / observer-effect, unresolved). See also ADR-0005 (OPEN). improvement-train A1 / #30–33. |
| [plt-observability-polish-2026-07-13](plt-observability-polish-2026-07-13.md) | observability / Grafana (#23) | **AUDITED — REVISE, build-conditional** | Grafana dashboards + queue visibility/alerting; 90% sound (queue/DLQ/cache/consolidation rows + 3-of-4 alerts ship today). Blocked on user-decision A: the plan expected a P-SB dependency file at the literal path *docs/plans/psb-observability-2026-07-13.md*, which was never created under that name — the actual P-SB work shipped as [psb-span-budget-hot-loop-2026-07-14](archive/psb-span-budget-hot-loop-2026-07-14.md) (v5.138.0). Blocker likely resolved in substance; this plan hasn't been re-audited to confirm. |
| [viz-trace-replay-2026-07-09](viz-trace-replay-2026-07-09.md) | viz | **Phases 0–2 SHIPPED (core 5.145.0); Phase 3 OPEN** | Traces tab — live trace-replay mesh + oscilloscope design language. Phase 3 (SSE `trace_complete` + live metrics) deferred, see task backlog. |
| [precompact-async-global-hooks-2026-07-22](precompact-async-global-hooks-2026-07-22.md) | install / hooks (#103) | **plan header says PR-1 IN PROGRESS / PR-2 PLANNED — stale; git history shows both merged** | PR-1 (async PreCompact drain hook, unblock `/compact`) shipped as #226. PR-2 (global-authoritative install, ADR-0161) shipped as commit `82ec0419`. Plan doc itself was never updated to reflect this — functionally closed, formally still open. |
| [multi-client-harness-hooks-train-2026-07-20](multi-client-harness-hooks-train-2026-07-20.md) | install / hooks (#58, #61) | **PARTIAL SHIP (v5.158.0)** | Shared hook-emitter seam + HookCapability matrix + CLI + Cursor Car B + capture-loop/anchor-audit folds shipped. OpenCode (#58) shipped separately as its own train (v5.166.0/.1, see Latest shipped). Codex/Cline/Kiro/Windsurf/Amp (#61) deferred to their own per-client plans (below). Train not archived: deferred cars remain outstanding. |
| [followup-opencode-port-2026-07-26](followup-opencode-port-2026-07-26.md) | opencode follow-ups (#57) | **IN-PROGRESS — F4–F7 done (v5.166.1); F1–F3 deferred (gated)** | [followup-f1-headless-e2e](followup-f1-headless-e2e.md): PROPOSED, queued (env infra). [followup-f2-stop-blocking](followup-f2-stop-blocking.md): WATCH-ONLY, depends on upstream `sst/opencode#16626`. [followup-f3-chat-message-wiring](followup-f3-chat-message-wiring.md): PROPOSED, blocked on F1. |

### Horizon — v6 / v7 (skeletons + indices, not ready to build)
| Plan | Theme | Notes |
|------|-------|-------|
| [v6-parallel-trains](v6-parallel-trains.md) | v6 index | **DEFERRED — v6 horizon (task #19)** | Index stale (T2/T3/T4 shipped); rebuild fresh when v6 resumes. |
| [PLAN_V6_QUALITY_FOUNDATION](PLAN_V6_QUALITY_FOUNDATION.md) | v6 north-star | Measurement harness → surprise-gate ON → enrichment/retrieval ablations → consolidation efficacy → LLM synthesis. |
| [v6-llm-curator](v6-llm-curator.md) | future | LLM curator cycle scaffold. |
| [v6-extract-on-ingest](v6-extract-on-ingest.md) | future | LLM extract-on-ingest (Adopt-7). |
| [v7-team-usability](v7-team-usability.md) | future | Team usability / multi-user architecture. |
| [saas-feasibility-skeleton-2026-07-13](saas-feasibility-skeleton-2026-07-13.md) | exploratory / business | **DEFERRED — v8+ horizon** | Open-core SaaS feasibility decision-menu. Not an impl spec. |
| [security-stack-skeleton-2026-07-13](security-stack-skeleton-2026-07-13.md) | exploratory / security | **DEFERRED — v7 (multi-user)** | HTTPS + auth + encryption layered menu. Decision-ready menu, nothing scheduled. |
| [port-codex](port-codex-2026-07-20.md) / [port-cline](port-cline-2026-07-20.md) / [port-kiro](port-kiro-2026-07-20.md) / [port-windsurf](port-windsurf-2026-07-20.md) / [port-amp](port-amp-2026-07-20.md) | portability / clients (#57) | **PLANNED (deferred), each** | Per-client hook-port plans, each supersedes [port-clients-survey-2026-07-18](archive/port-clients-survey-2026-07-18.md) §Task #57-<letter>. Emitter target for all five: `yadgar/core/install/clients/hooks_render.py` (`_emit_stub` replacement). |

## Tasks minted from 2026-07-16 triage

| Task | Origin |
|------|--------|
| #30 | agent-brain demoted: incidents-ledger + staleness nags |
| #32 | perf-loadtest re-scoped (ADR-0129; histogram-delta canonical) |
| #33 | stamina adoption (build-vs-buy decided = ADR-0103 keep custom) |
| #9-C4 | wiki-kb SNR noise remnant folded in |
| #19 | v6 horizon — parallel-trains index to rebuild when v6 resumes |

## Unfiled (no plan doc yet — candidates)
- **consolidate `light` latency** — `light` mode took ~5.7 min live (MCP client timed out though server finished). Likely first-run `last_decay_at` backfill + episode/CLS/causal phases at scale. Needs a perf plan if it persists.
- **wiki AWS-inventory archive** — ~1547 inventory-tier wiki pages flagged in the v5.58 wiki audit (source_count=0, orphaned). Cleanup decision parked.

## Recently closed (see `archive/` — not restated in Open plans above)

- `en2a-comet-fpa-v5.82` + `comet-retire-dormant` — **SHIPPED → archived — ADR-0004 RETIRE.** Ablation concluded un-FPA'd COMET net-negative recall (−4.2pt) at ~17h/10-core → retired to dormant. Verdict report `benchmarks/reports/en2a_comet_ablation_2026-06-24.md`.
- `fix-claude-code-mcp-auth-token-missing-2026-07-28` (#71) + `fix-systemd-generate-missing-queue-base-2026-07-28` (#72) — **SHIPPED**, both in "fix: 2026-07-28 fresh-VM QA bugs (auth token, queue-base) + hook-bypass guard (#13)".
- `hook-install-hygiene-2026-07-13` (Car #64) — **SHIPPED v5.136.0 (#199)**, folded into the startup hook-install poison guard bug-train; archived without an explicit ARCHIVED banner (own header still says "DRAFT — awaiting audit" — a docs-hygiene gap in that specific file, not this roadmap).
- `task-list-mirror-2026-07-14` — **SHIPPED**, Stop-hook / task-list train — 6 cars (v5.136.1 → v5.139.1, #200).
- `ci-velocity-train-2026-07-03` — **ARCHIVED 2026-07-14, all legs resolved.** #84 test-speed SHIPPED v5.104; #83 backend-bump gate MERGED; #79 load-test contract re-scoped as its own plan (`perf-loadtest-contract-2026-06-30`, `perf-loadtest-remaining-deferred-2026-07-14` — both also archived, ADR-0129).
- `full-observability-standard-2026-07-03` — **ARCHIVED, STANDARD COMPLETE.** Tri-signal `@observe` rollout P0–P6 shipped; I33 coverage lint 1564 MISSING → 0, GLOBAL HARD-FAIL. Sole extracted remainder (P-SB) also shipped: `psb-span-budget-hot-loop-2026-07-14`, v5.138.0.
- `recall-content-integrity-flake` — **ARCHIVED — VERDICT: OBVIATED.** Root cause was a cross-test SurrealDB pollution bug, structurally fixed by an unrelated commit before the flaky test was even un-skipped. *(ROADMAP previously carried this as still-open — "scoring diagnosis open" — which was stale; the plan's own archived banner already recorded the obviated verdict.)*
- `ce-perf-options`, `anchor-signal-gap`, `comet-dormant-startup-warning`, `roadmap-freshness`, `viz-config-control-panel` — all **SHIPPED/CLOSED/SUPERSEDED → archived**; see `archive/` for each doc's own closure note.
- `obs-velocity-completion-2026-07-04` — **undecided, not collapsed here.** Its own header says "executable spine... stays live", but both docs it cites as authoritative (`full-observability-standard-2026-07-03`, `ci-velocity-train-2026-07-03`) are now themselves archived/shipped, and #29's remaining load-test leg is also closed (above). Strong circumstantial evidence this is closeable, but it has not been re-audited since 2026-07-13 to confirm — flagging rather than declaring done.

## Archive
`archive/` holds 209 shipped/dead plan docs as of 2026-08-01 (grows
monotonically — don't hand-maintain this count). Reference only — do not edit.

Verification sweep (archived 2026-07-09 — evidence-based, ADR-0081):
- `archive/adr-capture-system.md` — SHIPPED: all phases #121 (P0) + #124 (P1–P3/B1–B4/C1) + #177 (B5).
- `archive/data-dir-hygiene-2026-07-09.md` — SHIPPED: #175 (db36e1e5, ADR-0076).
- `archive/daemon-hang-rca-and-recovery-2026-06-30.md` — SHIPPED: P0 e783510 (nix) + P1 v5.90.0 to_thread.
- `archive/daemon-offload-A-2026-06-30.md` — SHIPPED: v5.90.0 (#134, 2febcedb), default-OFF.
- `archive/daemon-offload-A-BUILD-NOTES.md` — SHIPPED: companion to v5.90.0.
- `archive/recall-pipeline-to-backend-2026-07-04.md` — SHIPPED: T1 #162 (8ae9e52c).
- `archive/recall-forward-only-2026-07-05.md` — SHIPPED: T1.5 #163 (219dd61f), flag deleted.
- `archive/viz-config-control-panel.md` — SUPERSEDED by `archive/settings-panel-redesign-2026-06-29.md` (shipped #66, archived 2026-07-13).
- `archive/wiki-repo-builtin.md` — SUPERSEDED by `archive/wiki-repo-full-buildout-2026-06-29.md` (shipped #125/#34, archived 2026-07-13).

Caching train (archived 2026-07-09 — shipped #164/#165, Car 3 killed ADR-0071):
`archive/backend-caching-train-2026-07-06.md`, `archive/caching-train-build-2026-07-05.md`,
`archive/caching-opportunities-2026-07-05.md`, `archive/unified-cache-2026-07-05.md`,
`archive/cache-refactor-2026-07-01.md`.

Docs-reorg (archived 2026-07-14 — shipped, `feat/docs-reorg`, #65):
`archive/docs-cleanup-taxonomy-2026-07-13.md` — 5-car taxonomy reorg: contracts→`docs/contracts/`
(+ 20 HARD pins), `reference/`+`reports/`+`testing/`+`benchmarks/` dirs, kebab/dated
renames, roadmap dedup (wiki-mirror file deleted, `roadmap/v*.md`→`roadmap/archive/`),
benchmarks fold, diagram `out/` untrack+gitignore (#68 regenerates), `docs/README.md` index.
