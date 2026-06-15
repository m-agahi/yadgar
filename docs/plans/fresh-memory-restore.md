# PLAN — v5.65: Fresh memory access UX (SKELETON)

**Status:** SKELETON drafted 2026-06-04. Origin: live UX failure 2026-06-04 — user memorized v5.64 wiki surgical edit idea (memory id 523183, heat=1, importance=1, is_protected=true) at 12:47. Post-`/clear` at 12:55 (~5 minutes later), `restore` returned hot/anchored sets that did NOT surface the just-saved memory. Subsequent `recall` blocked by Anthropic auto-mode classifier 503. User had to drive Claude through 6+ tool calls to recover the content. Defeats yadgar core promise: write once, recall easily.

**Not blocking.** Adjacent to v5.63 (wiki corpus maintenance) + v5.64 (wiki surgical edit primitives).

## Problem

Three failure modes compound:

1. **Restore output is heat/anchor-ranked, not time-ranked.** Hot memories favor stability + importance + access history. A memorize within the last hour can lose to a 2-week-old _anchor with high cumulative access. No deterministic "show me what I just saved" path through `restore`.

2. **`recall` depends on Anthropic auto-mode classifier.** Observed 2026-06-04 12:55 — classifier returned 503 (`claude-opus-4-7 is temporarily unavailable, so auto mode cannot determine the safety of mcp__yadgar__recall right now`). Tool was completely unusable for ~5+ minutes. Wiki tools (`wiki_query`, `wiki_list`, `wiki_drafts`, `memory_get`, `memory_stats`) worked the entire time. The classifier hits only on `recall` (and possibly `consolidate_now`/`reembed_all` mutation tools).

3. **`memorize` returns minimal feedback.** No printed memory id, no slug, no anchor-or-not summary. User has no handle to retrieve the exact write.

## Scope

Three deliverables. All independently shippable.

### Deliverable 1 — `recent_memories(limit, since, directory)` MCP tool

Deterministic time-ranked listing of recent writes. No similarity, no heat, no classifier.

Contract:
- `limit` (default 10, max 100)
- `since` (default `24h`, accepted: ISO datetime OR duration string `5m`, `1h`, `7d`)
- `directory` (default caller cwd; `"global"` for cross-project; explicit absolute path otherwise)
- Returns ordered by `created_at DESC`
- Returns: id, created_at, content (truncated to 300 chars), tags, store_type, heat, is_protected, slot_index
- Optionally include anchored-only flag for filtering

Use cases:
- "What did I save in this session?"
- "What did I memorize in the last hour?"
- Post-`/clear` recovery without classifier dependency
- Audit recent writes during debugging of write-gate behavior

Effort: ~30 LOC server-side + 1 storage query + 4-5 tests.

### Deliverable 2 — `restore` includes "Recent writes" section

Augment `restore` formatted output with a separate section listing the last 10 memorize/anchor writes in the last 24h for the requested directory. Ordered by `created_at DESC`, NOT by heat.

Renders alongside (not replacing) Hot Memories. Preserves heat-ranked ranking for the bulk of context; adds deterministic freshness floor.

Implementation: reuse Deliverable 1's storage query. Add formatted block:

```markdown
## Recent Writes (last 24h)
- [2026-06-04T12:47] id=523183 (anchor) — v5.64 SLOT IDEA — wiki surgical edit primitives. Surfaced by user...
- [2026-06-04T11:52] id=523180 — Session activity batch...
```

Effort: ~20 LOC in `restore` formatter + 2 tests.

### Deliverable 3 — `memorize` returns id + slug-like hint

Currently `memorize` likely returns success bool or minimal dict. Extend to return:

```json
{
  "ok": true,
  "memory_id": 523183,
  "heat": 1.0,
  "is_protected": true,
  "anchored": false,
  "tags": ["yadgar", "v5.64", "..."],
  "created_at": "2026-06-04T12:47:30Z"
}
```

Lets the caller (Claude main thread, scripts, future MCP clients) record the id immediately. Subsequent `memory_get(memory_id)` is then a guaranteed lookup independent of classifier or heat ranking.

Effort: ~10 LOC return-shape change + adjust tests.

## Deferred / out of scope

- **Classifier 503 workaround inside yadgar** — classifier check happens at Anthropic tool-permission layer, OUTSIDE yadgar's process. Can't fix from server side. Mitigation = ship Deliverable 1 (`recent_memories` is unlikely to trip the same classifier gate since it's pure read-by-id, no semantic surface).
- **Heat-ranked Hot Memories overhaul** — separate concern. Don't touch ranking; just add freshness section.
- **UI/visualizer recent-writes panel** — v5.50 viz overhaul.
- **CLI `yadgar recent`** — v5.62 CLI plan.

## Acceptance

- `recent_memories(limit=5)` returns 5 most recent writes for caller's directory, ordered by `created_at DESC`
- `restore` output includes "Recent Writes" section with last 10 writes in last 24h
- `memorize` return value includes memory_id field; existing callers tolerate addition (backward-compatible field addition)
- Post-`/clear` workflow: user memorizes → `/clear` → `restore` → fresh content visible in "Recent Writes" section without recall round-trip
- Classifier 503 sim: `recent_memories` callable when `recall` is blocked

## Cross-references

- `docs/PLAN_V5_63_WIKI_CORPUS_MAINTENANCE_TOOLS.md` — metadata maintenance tools (orthogonal)
- v5.64 SLOT IDEA (memory id 523183) — wiki surgical edit primitives (orthogonal; same UX failure surfaced both)

## Defer rationale

Yadgar functional state is correct — data is durably stored, retrievable via memory_get(id). UX gap only. Schedule after v5.42.x cleanup cycle + adjacent to v5.63/v5.64 wiki-edit work.
