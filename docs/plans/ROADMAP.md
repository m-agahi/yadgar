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

### Wiki / KB
| Plan | Theme | Status | Notes |
|------|-------|--------|-------|
| [wiki-edit-primitives](wiki-edit-primitives.md) | wiki-kb | skeleton | Wiki edit primitives + metadata maintenance (from v5.64 skeleton). |
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
