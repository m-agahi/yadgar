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

- **v5.81** (`091d958`) — viz-fidelity-v2 (BC-VZ-R1/2/3) + wiki BC-G10 + landscape recall (BC-AC3a) + MCP tool descriptions.
- **v5.80** (`22206d9`) — unified-recall default-flip + fan-out fusion regression fixes.
- **v5.79** (`52e81cf`) — unified-scoped-recall steps 0/3/4/5 (dormant flag).
- **v5.78** (`09138c3`) — Wave-2: recall-rebuild 0–2 + fresh-memory tools + repo-wiki-native.
- **v5.76** (`f28faf5`) — Wave-1: dead-config #41 + viz #33 (viz-data-fidelity) + e2e/cogmap.

## Open plans (by priority)

### Active / next
| Plan | Theme | Status | Notes |
|------|-------|--------|-------|
| [en2a-comet-fpa-v5.82](en2a-comet-fpa-v5.82.md) | enrichment / eval | **Next — v5.82** | COMET-FPA ablation: fix LongMemEval fidelity (enrichment-off baseline) → COMET-FPA exemption → ablation (2×8h runs) → flip-or-retire COMET. Prerequisite landed in v5.81 (`091d958`). |
| [unified-scoped-recall-v2-steps3-5](unified-scoped-recall-v2-steps3-5.md) | retrieval (arch) | **in-flight — redo** | **Centerpiece.** Steps 3–5 redo after failed first attempt (parked broken @ `feat/v6-t6-recall-345`). DB-level scope filter + cross-type fusion + `type=` param. Steps 0–2 shipped v5.78; `UNIFIED_RECALL_ENABLED` default-flipped v5.80. Parent design archived. |
| [db-audit-fix](db-audit-fix.md) | data-integrity | **skeleton — discuss first** | Audit + fix live-store issues (legacy `last_decay_at`, 6-week-dead aftermath, entity heat, wiki↔memory link, archive tier, orphans). User has thoughts to bring before scoping. |

### In-flight migrations / investigations
| Plan | Theme | Status | Notes |
|------|-------|--------|-------|
| [wiki-kb-usefulness-snr](wiki-kb-usefulness-snr.md) | wiki-kb | live decision log | Recall-SNR investigation (recall was 37.5% noise; `directory=` no-op; mis-stamp sinks). Feeds the recall train; D1: wiki↔memory linkage dropped (unused field). |

*(`wiki-restamp-migration` completed 2026-06-23 — global wiki count 616 → 5 (legit cross-project remainder); final 3 stragglers cleared via the v5.81 all-rows `wiki_set_metadata`. Archived.)*

### Infra / ops
| Plan | Theme | Status | Notes |
|------|-------|--------|-------|
| [ce-perf-options](ce-perf-options.md) | infra-ops | partial / undecided | Cross-encoder perf menu; options A/C folded earlier, option B (int8) never shipped. Close or pick B. |
| [roadmap-freshness](roadmap-freshness.md) | infra-ops | deferred (v5.99 — design open) | Roadmap-staleness signal; deferred on an async-write-queue design problem. |

### Horizon — v6 / v7 (skeletons + indices, not ready to build)
| Plan | Theme | Notes |
|------|-------|-------|
| [v6-parallel-trains](v6-parallel-trains.md) | v6 index | Execution index for remaining #82+ trains; sequencing/dependencies. |
| [PLAN_V6_QUALITY_FOUNDATION](PLAN_V6_QUALITY_FOUNDATION.md) | v6 north-star | Measurement harness → surprise-gate ON → enrichment/retrieval ablations → consolidation efficacy → LLM synthesis. |
| [agent-prompt-passive-library](agent-prompt-passive-library.md) | future | Agent-prompt passive library (Tier-1 MVP). |
| [v6-llm-curator](v6-llm-curator.md) | future | LLM curator cycle scaffold. |
| [v6-extract-on-ingest](v6-extract-on-ingest.md) | future | LLM extract-on-ingest (Adopt-7). |
| [v7-team-usability](v7-team-usability.md) | future | Team usability / multi-user architecture. |

## Unfiled (no plan doc yet — candidates)
- **consolidate `light` latency** — `light` mode took ~5.7 min live (MCP client timed out though server finished). Likely first-run `last_decay_at` backfill + episode/CLS/causal phases at scale. Needs a perf plan if it persists.
- **wiki AWS-inventory archive** — ~1547 inventory-tier wiki pages flagged in the v5.58 wiki audit (source_count=0, orphaned). Cleanup decision parked.

## Archive
`archive/` holds ~95 shipped/dead plan docs (v5.2 → v5.81). Reference only — do not edit.
