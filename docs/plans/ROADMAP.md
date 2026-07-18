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

> **Recall is DONE (don't re-plan it).** `recall(query, directory=…)` already fetches **everything** — memory + wiki merged into one ranked list (default `type="all"`). `UNIFIED_RECALL_ENABLED` is `True` in prod; `type=memory|wiki` narrows; `mode="landscape"` is the optional slow cross-domain mode. Steps 0–2 shipped v5.78; the 3–5 test-first redo shipped v5.79; default flip v5.80. Behaviour-locked: BC-U1..U8, BC-G11, BC-AC3a all ✅ in `BEHAVIOR_CONTRACT.md` with live-DB e2e. The old `unified-scoped-recall*` plan docs are archived.
- **v5.78** (`09138c3`) — Wave-2: recall-rebuild 0–2 + fresh-memory tools + repo-wiki-native.
- **v5.76** (`f28faf5`) — Wave-1: dead-config #41 + viz #33 (viz-data-fidelity) + e2e/cogmap.

## Open plans (by priority)

### Active / next
| Plan | Theme | Status | Notes |
|------|-------|--------|-------|
| [en2a-comet-fpa-v5.82](archive/en2a-comet-fpa-v5.82.md) + [comet-retire-dormant](archive/comet-retire-dormant.md) | enrichment / eval | **SHIPPED → archived — ADR-0004 RETIRE (in CHANGELOG `[Unreleased]`)** | Ablation concluded: un-FPA'd COMET net-negative recall (−4.2pt) at ~17h/10-core → retired to dormant (flag default True→False), BC-EN2b warning shipped. NOT improvement-train #29. Both plan docs archived 2026-06-25; verdict report `benchmarks/reports/en2a_comet_ablation_2026-06-24.md`. |
| [db-audit-fix](db-audit-fix.md) | data-integrity | **DEFERRED INDEFINITELY** | Re-audited 2026-07-16: store healthy. Residuals: ~5 dangling edge rows + unverifiable last_decay_at — not worth work. |
| [tdd-hardening-pipeline-2026-07-18](tdd-hardening-pipeline-2026-07-18.md) | dev-process / quality | **codified v5.150.0 (#213)** | 5-phase pipeline (RED-VERIFY → adversarial critic → green → mutation+fuzz → gates) codified into `implement-tdd` agent-prompt + weekly mutation-sweep workflow. |
| [recall-scoring-c4-2026-07-18](recall-scoring-c4-2026-07-18.md) | recall / scoring (#62) | **PROPOSED — awaiting user decisions** | C4 recall-scoring scoping plan. 1a tie-order = fusion-tiebreak plan; 1b abbreviation-miss + corpus-side SNR. Depends on user decisions before build. |
| [hook-install-hygiene-2026-07-13](hook-install-hygiene-2026-07-13.md) | install / hooks (#64) | **DRAFT — awaiting audit** | FIXES-train Car #64. Hook-install hygiene: `install_hooks_lib.py` HOME-guard + orphan-sweep (content-hash predicate, not sibling-exists). Core-only. |
| [task-list-mirror-2026-07-14](task-list-mirror-2026-07-14.md) | harness task-list / hooks | **DRAFT — awaiting audit** | Persist harness `TaskCreate` list across instances. FLAT per-project wiki `{project}-task-list`, read-before-write, instruction-driven (AUTO/INJECT/session-sectioning rejected). Stop-prompt 4-case state machine: reconcile own list → `wiki_read` → {create / skip / catch-up-sync / merge-full-rewrite} with per-task context pointers. Catch-up guard: skip `completed` + 14d age gate. Schema: `page_type="task_list"` (yaml, tier b) + one `## task:<id>` section/task (ADR-style, surgical-editable via `wiki_append_section(replace_section)`); status enum `{pending,in_progress,completed}` (verified harness output). Session-context endpoint injects restore line only if page exists (server-side pre-check, main-only, proven). No `project_brief` change + no new tool. Wiki not block (fidelity). |

### In-flight migrations / investigations
| Plan | Theme | Status | Notes |
|------|-------|--------|-------|

*(`wiki-restamp-migration` completed 2026-06-23 — global wiki count 616 → 5 (legit cross-project remainder); final 3 stragglers cleared via the v5.81 all-rows `wiki_set_metadata`. Archived.)*

### Infra / ops
| Plan | Theme | Status | Notes |
|------|-------|--------|-------|
| [ce-perf-options](archive/ce-perf-options.md) | infra-ops | **SHIPPED — Ettin-32m (Train 4); plan archived** | CE was quoted at ~90% of recall latency (ADR-0035) — _corrected to ~25% cold by ADR-0105 (#192)._ Option B (onnx-int8) rejected (2× slower, ADR-0043/ADR-0067). The actual win came from swapping the CE model: Ettin-32m shipped Train 4 (~4.7× faster per-pass, 2.44× end-to-end; #193). improvement-train A2 / #4. |
| [full-observability-standard-2026-07-03](full-observability-standard-2026-07-03.md) | observability | **P0 SHIPPED v5.101; per-area rollout remains** | Tri-signal standard (span+metric+log per function). P0 shipped v5.101 (`@observe` + I33 lint warn-mode + histogram fix + traceparent; ADR-0034). Remaining: per-area rollout (flip I33 lint to hard-fail per area) + backend fine-spans. |
| [cpu-burst-rootcause-and-embedding-scan-fix](cpu-burst-rootcause-and-embedding-scan-fix.md) | perf | **Part 2 buildable; Part 1 OPEN (host-side)** | Part 2 = kill `SELECT *` consolidation scans (projection + server-side decay + sample-then-fetch + incremental linking). Part 1 = fan-burst still open (observer-effect / surrealkv suspect, hand to user). improvement-train A1 / #30–33. |
| [anchor-signal-gap](archive/anchor-signal-gap.md) | signals / anchors | **SHIPPED → archived #177 (a4390c4e)** | project_brief over-signals fix + per-directory scope shipped. improvement-train B5 / #20. Archived 2026-07-09. |
| [comet-dormant-startup-warning](archive/comet-dormant-startup-warning.md) | observability | **CLOSED / ARCHIVED** | #25: warning IS reached on streamable-http (refutes ticket premise); residual = server-log vs client-visible. improvement-train C3. |
| [recall-content-integrity-flake](recall-content-integrity-flake.md) | recall / test | **test unskipped (#124); scoring diagnosis open** | #21: skip removed (#124, 6e1629cb); it's a ranking miss (PAT vs "personal access token"). Diagnose per-signal scores → choose fix (tie-break vs test-realism vs re-weight). Don't overfit recall. improvement-train C4. |
| [roadmap-freshness](archive/roadmap-freshness.md) | infra-ops | **DEFERRED indefinitely / ARCHIVED** | Roadmap-staleness signal; deferred on an async-write-queue design problem; partially mitigated v5.41.4. |
| [viz-config-control-panel](archive/viz-config-control-panel.md) | viz / config / ops | **SUPERSEDED → archived** | Superseded by [settings-panel-redesign-2026-06-29](archive/settings-panel-redesign-2026-06-29.md) (shipped #66 v5.89.0, archived 2026-07-13). Archived 2026-07-09. |
| [ci-velocity-train-2026-07-03](ci-velocity-train-2026-07-03.md) | test-perf / ci | **test-speed leg SHIPPED v5.104; #83/#79 remain** | Unified velocity plan. #84 test-speed leg SHIPPED v5.104 (PR #156, ADR-0036 — module-scope `storage` fixture + batched wipe → CI shards ~2×; corrected ADR-0027's schema-init premise). Feeder [test-suite-speedup-2026-07-01](archive/test-suite-speedup-2026-07-01.md) archived. Remaining: #83 backend-bump CI gate + conditional image builds, #79 load-test contract. |

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
| [port-clients-survey-2026-07-18](port-clients-survey-2026-07-18.md) | portability / clients (#57) | **PROPOSED** | Compatibility matrix: 9 agentic clients × 4 integration surfaces (MCP, rules-file, hooks, task-system). HIGH-value targets: Codex CLI, Cursor, Cline, Windsurf, Kiro, Amp. Per-client hook/MCP API claims need verification (task #59) before build (#61). See also plan #56 (OpenCode, separate). |
| [port-opencode-2026-07-18](port-opencode-2026-07-18.md) | portability / clients (#56) | **PROPOSED** | OpenCode harness port. MCP already wired (opencode.json remote). 15 lifecycle hooks need TS-plugin shims; task-list inbound deferred (no persistent todo store). Spike-first, Cars A–E (task #58). |

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

## Archive
`archive/` holds ~117 shipped/dead plan docs (v5.2 → v5.120). Reference only — do not edit.

Verification sweep (archived 2026-07-09 — evidence-based, ADR-0081):
- `adr-capture-system.md` — SHIPPED: all phases #121 (P0) + #124 (P1–P3/B1–B4/C1) + #177 (B5).
- `data-dir-hygiene-2026-07-09.md` — SHIPPED: #175 (db36e1e5, ADR-0076).
- `daemon-hang-rca-and-recovery-2026-06-30.md` — SHIPPED: P0 e783510 (nix) + P1 v5.90.0 to_thread.
- `daemon-offload-A-2026-06-30.md` — SHIPPED: v5.90.0 (#134, 2febcedb), default-OFF.
- `daemon-offload-A-BUILD-NOTES.md` — SHIPPED: companion to v5.90.0.
- `recall-pipeline-to-backend-2026-07-04.md` — SHIPPED: T1 #162 (8ae9e52c).
- `recall-forward-only-2026-07-05.md` — SHIPPED: T1.5 #163 (219dd61f), flag deleted.
- `viz-config-control-panel.md` — SUPERSEDED by `archive/settings-panel-redesign-2026-06-29.md` (shipped #66, archived 2026-07-13).
- `wiki-repo-builtin.md` — SUPERSEDED by `archive/wiki-repo-full-buildout-2026-06-29.md` (shipped #125/#34, archived 2026-07-13).

Caching train (archived 2026-07-09 — shipped #164/#165, Car 3 killed ADR-0071):
`backend-caching-train-2026-07-06.md`, `caching-train-build-2026-07-05.md`,
`caching-opportunities-2026-07-05.md`, `unified-cache-2026-07-05.md`,
`cache-refactor-2026-07-01.md`.

Docs-reorg (archived 2026-07-14 — shipped, `feat/docs-reorg`, #65):
`docs-cleanup-taxonomy-2026-07-13.md` — 5-car taxonomy reorg: contracts→`docs/contracts/`
(+ 20 HARD pins), `reference/`+`reports/`+`testing/`+`benchmarks/` dirs, kebab/dated
renames, roadmap dedup (wiki-mirror file deleted, `roadmap/v*.md`→`roadmap/archive/`),
benchmarks fold, diagram `out/` untrack+gitignore (#68 regenerates), `docs/README.md` index.
