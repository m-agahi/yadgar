# Yadgar plan roadmap

Single source of truth for **open** plans. Shipped/dead plans live in
`archive/` (kept for history; do not edit).

## Convention (read before adding a plan)

- **Slug-named, not version-named.** Plans live at `docs/plans/<slug>.md`. The
  filename is stable identity; it never gets renamed for reprioritization.
- **Version assigned at ship, not in the filename.** A plan gets its `vX.Y.Z`
  only when it ships — recorded in `docs/CHANGELOG.md` + the git tag, and as the
  `target`/`shipped` field here. (The old `PLAN_V5_NN_TOPIC.md` scheme caused
  constant renumbering drift — that's why ~85 docs sit in `archive/`.)
- **Register every new plan in this file.** If it's not here, it's not tracked.
- **On ship:** move the plan doc to `archive/` and mark it shipped in CHANGELOG.

## Open plans (by priority)

### Active / next
| Plan | Theme | Status | Notes |
|------|-------|--------|-------|
| [viz-data-fidelity](viz-data-fidelity.md) | viz | scoping (F1-F5) | **Next.** Viz must reflect DB reality — F2 stale-heat (SSE write-time freeze) is the worst; F1 connection miscount, F3 typed-id, F4 dropped edges, F5 fidelity test. |
| [db-audit-fix](db-audit-fix.md) | data-integrity | **skeleton — discuss first** | Audit + fix live-store issues (legacy `last_decay_at`, 6-week-dead aftermath, entity heat, wiki↔memory link, archive tier, orphans). User has thoughts to bring before scoping. |

### Wiki / KB + retrieval — the "truly useful" train (sequenced 2026-06-15)
Investigation: `[[wiki-kb-usefulness-snr]]` (recall is 37.5% noise from within yadgar; `directory=` is a no-op; mis-stamp sinks `system`/`global`). Decision D1: wiki↔memory linkage DROPPED (unused field). Order:
| # | Plan | Theme | Status | Notes |
|---|------|-------|--------|-------|
| 1 | [wiki-edit-primitives](wiki-edit-primitives.md) | wiki-kb | **GRINDING (v5.61)** | Edit/maintenance tools (set_metadata, anchor-text, structural, positional). Cleanup foundation; `wiki_set_metadata` is the re-stamp tool. |
| 2 | [recall-scoping-restamp](recall-scoping-restamp.md) | retrieval | planned (next) | Immediate noise cut: fix write-time dir defaults, tighten recall filter (`directory=` no-op + quality floor + dedup), re-stamp ~612 mis-stamped wikis + memory rows. Lower-risk, existing design. |
| 3 | [unified-scoped-recall](unified-scoped-recall.md) | retrieval (arch) | planned (after) | **Centerpiece.** One `recall(type=all\|memory\|wiki\|…)` — fan-out to source providers, single DB-level DirectoryFilter, one cross-encoder rerank. `wiki_query`→deprecated alias. Absorbs #2's scoping core. |
| [fresh-memory-restore](fresh-memory-restore.md) | wiki-kb / retrieval | skeleton | Fresh-memory-access UX — recently-written memory not visible without reload (from v5.65). Overlaps viz-data-fidelity F2 (staleness). |

### Infra / ops
| Plan | Theme | Status | Notes |
|------|-------|--------|-------|
| [ce-perf-options](ce-perf-options.md) | infra-ops | partial / undecided | Cross-encoder perf menu; options A/C folded earlier, option B (int8) never shipped. Close or pick B. |
| [roadmap-freshness](roadmap-freshness.md) | infra-ops | deferred (design open) | Roadmap-staleness signal; deferred on an async-write-queue design problem. |

### Future vision (skeletons — not ready to build)
| Plan | Theme | Notes |
|------|-------|-------|
| [agent-prompt-passive-library](agent-prompt-passive-library.md) | future | Agent-prompt passive library (Tier-1 MVP). |
| [v6-llm-curator](v6-llm-curator.md) | future | LLM curator cycle scaffold. |
| [v6-extract-on-ingest](v6-extract-on-ingest.md) | future | LLM extract-on-ingest (Adopt-7). |
| [v7-team-usability](v7-team-usability.md) | future | Team usability / multi-user architecture. |

## Unfiled (no plan doc yet — candidates)
- **consolidate `light` latency** — `light` mode took ~5.7 min live (MCP client timed out though server finished). Likely first-run `last_decay_at` backfill + episode/CLS/causal phases at scale. Needs a perf plan if it persists.
- **wiki AWS-inventory archive** — ~1547 inventory-tier wiki pages flagged in the v5.58 wiki audit (source_count=0, orphaned). Cleanup decision parked.

## Archive
`archive/` holds 85 shipped/dead plan docs (v5.2 → v5.59). Reference only.
