# PLAN — Unified scoped recall (one retrieval surface)

Status: **PLANNED 2026-06-15.** The "truly useful" centerpiece. Starts AFTER the
current train (wiki edit-primitives v5.61 + `[[recall-scoping-restamp]]`). User
decision: this is the next major work after this train lands.

theme: retrieval (architecture)
priority: high (the usefulness centerpiece)

## Vision

ONE recall tool returns the best knowledge regardless of where it lives —
memories, wikis, and future sources (skills, prompts, …) — ranked together,
scoped to the caller's project/branch.

```
recall(query, directory, branch, type=all|memory|wiki|skill|prompt|…, max_results)
  → fan out to SourceProviders [memory, wiki, (skill, prompt …later)]
       each returns scored candidates with native signals
  → ONE directory/branch scope filter   (DB-level DirectoryFilter — the heavy
                                          version of recall-scoping-restamp's
                                          quick filter; built once, here)
  → ONE cross-encoder rerank            (the equalizer across heterogeneous types)
  → typed, ranked results: [{type, id, title, content, score, directory, …}]
```

## Why (problem this solves)

1. **Fragmentation (the real bug).** Today recall (memory) + wiki_query (wiki)
   are separate tools/paths. A wiki can be the #1 best answer and never surface
   because the caller only called `recall`. Agents must remember to call both.
2. **Single scoping point.** Directory/branch enforced once, consistently —
   instead of two paths each separately broken (see recall-scoping-restamp).
3. **Extensibility.** New sources (skills/prompts) plug in behind one interface.

## Design

### Fan-out, not route
Do NOT guess "this query wants a wiki" and route. Fan out to ALL sources (or the
`type=`-filtered subset), pool candidates, rank together. Routing guesses;
fan-out-and-rank can't miss.

### Heterogeneous fusion — the hard part
Memories and wikis aren't comparable on one axis:
- memory value = heat + recency + surprise + PPR graph signal
- wiki value = durable curated knowledge, no heat decay
**Equalizer = the cross-encoder rerank** — scores query↔text relevance regardless
of source. Each provider contributes candidates with native signals (heat as a
prior for memory, freshness for wiki); the final ordering is relevance-first via
ONE rerank. Per-type signals feed in as priors, not as the final sort key.
- Open: fusion weighting (how much native-prior vs rerank); per-type candidate
  quotas before rerank (don't let one source starve another); cross-type dedup
  (a memory and the wiki derived from it).

### SourceProvider interface
```
class SourceProvider:
    type: str                       # "memory" | "wiki" | "skill" | …
    def candidates(query, scope) -> list[Candidate]   # native-scored
```
- `memory` provider = today's retriever pipeline (FTS+vector+PPR+temporal).
- `wiki` provider = today's wiki FTS+vector path.
- Build on the v5.31 plugin pipeline (stage architecture) — add a source-provider
  stage. NEEDS VERIFY: how shared is wiki vs memory retrieval infra today? The
  tagline claims wikis "search through the same pipeline" but `wiki_query` is a
  separate tool — determine actual sharing to size build cost. (First task.)

### Single scoping (folds in the restamp train's quick filter)
Promote directory to a real DB-level `DirectoryFilter` parallel to `BranchFilter`
(`storage/branch.py`), pushed into each provider's SurrealQL WHERE
(`directory_context IN ($caller_dir, 'global')` — `system` reclassified by then).
Hard pre-fetch exclusion, not post-crop. This is THE place the heavy filter is
built (recall-scoping-restamp deliberately leaves it here).

## Backward compatibility (user decision 2026-06-15)

- **`recall` BECOMES the unified tool** — gains `type` param, **default `all`**.
- `recall(type=wiki)` == old `wiki_query` → **deprecate `wiki_query`**: keep one
  release cycle as a thin alias delegating to `recall(type=wiki)`, emit deprecation
  note, migrate internal callers (hooks, skills, prelude, project_brief), then
  remove.
- `recall(type=memory)` == old memory-only behavior.
- **Contract change to flag:** `recall` default `type=all` means existing recall
  callers start receiving wikis too. Intended (surface best knowledge regardless
  of type) — document as the new default; memory-only callers pass `type=memory`.
- Keep per-type tuning knobs; unified means unified *ranking*, not collapsed
  *signals*. Type-scoped queries (`type=wiki`) remain first-class.

## Phasing
1. **Verify infra sharing** (wiki vs memory pipeline) — decides cost.
2. SourceProvider abstraction + memory/wiki providers over the v5.31 pipeline.
3. DB-level DirectoryFilter (single scoping point).
4. Cross-encoder fusion across providers + per-type quotas + cross-type dedup.
5. `recall` `type` param (default all); `wiki_query` → deprecated alias.
6. Migrate internal callers; document the default-all contract; later remove alias.

## Tests
- Unified recall returns memory AND wiki for a query answerable by either; the
  better one ranks first (cross-encoder).
- `type=wiki` == legacy wiki_query results; `type=memory` == legacy recall.
- Scoping: results obey caller directory/branch (DB-level filter).
- Fusion: a high-relevance wiki outranks a high-heat-but-irrelevant memory.

## Open questions (decide during build)
- Fusion weighting + per-type quotas.
- Cross-type dedup (memory ↔ its derived wiki).
- Default `max_results` split across types.
- Perf: fan-out + one rerank vs two separate paths (latency budget; the
  consolidate `light` 5.7min issue shows rerank/pipeline cost matters).

## Related
- `[[recall-scoping-restamp]]` — ships first; this folds in its scoping core
- `[[wiki-kb-usefulness-snr]]` — investigation + decisions
- `[[wiki-edit-primitives]]` — corpus-edit tooling
- v5.31 plugin pipeline (profiles fast/balanced/full) — the extension point
- Code: `retrieval/core.py`, `retrieval/stages/`, `server/tools/recall.py`, `server/tools/wiki.py`
