# MCP tool trace sweep — architecture + blind spots (2026-07-07)

Real span data for every yadgar MCP tool, cold + hot, captured from live Tempo
(yadgar 5.114.0). Method: fire each tool → `capture_trace.py tool.<name>` (finds
the call's trace, dumps the span tree) → `trace_to_boxes.py` (collapse to the
distinct stage tree, render rectangles). Per-tool diagrams: `mcp-traces/*.svg`.

Diagrams are collapsed by `(depth, span-name)`: one box = one stage, `Nms` =
summed time in that stage across the call, `xN` = span count collapsed into it.
Blue = `yadgar-core`, red = `yadgar-backend`. **The count is the finding** — a
stage box reading `x9000` is a per-item span explosion, not nine thousand steps.

## Headline: observability is COMPLETE. The blind spot is span EXPLOSION.

Every one of the ~30 tools emits a `tool.<name>` boundary span plus nested stage
spans — there are **no un-instrumented tools**. (An initial sweep reported "24
blind spots"; those were a *measurement* artifact — the `BatchSpanProcessor`
flushes on a ~5 s timer, so a fast tool's spans hadn't exported yet when the
capture retried at 4 s. Re-querying Tempo minutes later found every one. Lesson:
allow ≥1 batch-flush interval before capturing, or the absence is false.)

The genuine problem is the inverse: several tools emit **thousands of per-item
spans**, violating the "stage-granularity only, never a span per loop item" rule
([[yadgar-otel-tracing-span-mechanism-coverage]]) and saturating the OTLP export
queue (task #48).

## The consistent architecture (from `recall`, the richest trace)

```
POST /mcp                                     (core, 11.6s wall)
└─ offload.run_offloaded                      core worker thread (contextvars copy)
   └─ tool.recall                             the @_tool boundary span
      ├─ tools.project._detect_branch         cheap core-side prep
      └─ tools.recall._forward_to_backend
         └─ POST  ──[HTTP]──►  POST /recall    CORE→BACKEND boundary
            └─ backend.recall                 (backend)
               ├─ retrieval.provider.memory_candidates   9.0s
               │  └─ retrieval.recall
               │     └─ retrieval.rerank                 6.4s
               │        └─ retrieval.rerank.cross_encoder 5.2s  ◄── HOT PATH
               │           └─ ml_client.LocalMLClient._try_gte_reranker  5.2s
               └─ recall.fanout.fuse                     2.2s
      └─ tools.recall._apply_recall_session_side_effects  (post-return writes)
```

Backbone shared by all offloaded tools: **`POST /mcp` → `offload` thread →
`tool.<name>` → (core prep) → HTTP → `backend.<stage>`**. The core is a thin
MCP shell; the real work (retrieval, ML, storage) is over the HTTP boundary in
the backend. The GTE cross-encoder (5.2 s) is the dominant cost, as the standing
recall baseline records.

## Per-tool summary (span count / distinct stages / wall ms)

| Tool | spans | stages | ms | note |
|------|------:|------:|----:|------|
| `audit_anchors` (apply) | **46,297** | 42 | 4,298 | ⚠️ per-anchor span explosion — worst |
| `audit_anchors` (dry) | **46,186** | 60 | 4,204 | ⚠️ dry-run explodes too |
| `recall` | **27,133** | 222 | 11,601 | task #48; per-candidate spans |
| `memorize` (cold) | **6,618** | 225 | 6,804 | full drainer+embed chain |
| `anchor` | **4,267** | 230 | 1,638 | ⚠️ explodes for an anchor write |
| `check_invariants` | **3,458** | 63 | **39,920** | ⚠️ 40 s wall + heavy span tree |
| `restore` | **1,572** | 67 | 940 | + a NoneType bug (below) |
| `project_brief` (catalog) | **1,091** | 57 | 514 | catalog mode fans out |
| `reembed_all` | 994 | 47 | 6,830 | borderline |
| `wiki_add` | 167 | 115 | 219 | healthy: many stages, few dup spans |
| `wiki_read` | 173 | 74 | 422 | healthy |
| `checkpoint` | 86 | 39 | 123 | healthy |
| `anchor` (light call) | 61 | 44 | 129 | same tool, small input → no explosion |
| `block_create` | 47 | 33 | 111 | healthy |
| `recent_memories` | 42 | 20 | 51 | healthy |
| `memorize` (hot) | 35 | 31 | **6** | WriteGate near-dup reject fast-path |
| `wiki_list` | 31 | 19 | 117 | healthy |
| `add_rule` / `bookmark_add` | 30 | 26 | ~55 | healthy |
| `memory_get` | 23 | 19 | 27 | healthy |
| `block_list` | 21 | 17 | 110 | healthy |
| `project_brief` (light) | 18 | 14 | 5 | healthy |
| `wiki_query` | 17 | 13 | 6 | healthy |
| `get_rules` / `dlq_inspect` | 16 | 12 | ~6 | healthy |

"Healthy" = `spans ≈ stages` (each span is a real stage, no per-item duplication).
Explosion = `spans ≫ stages` (one stage entered thousands of times per call).

## Findings

1. **Span explosions (task #48) — the real blind spot.** `audit_anchors` (46 k),
   `recall` (27 k), `memorize`-cold (6.6 k), `anchor` (4.3 k on large input),
   `check_invariants` (3.5 k), `restore` (1.6 k), `project_brief`-catalog (1.1 k).
   These are per-item spans (per anchor, per candidate, per record). They flood
   the OTLP queue and make Tempo waterfalls unreadable. Fix per the tracing
   convention: collapse per-item spans to one stage span with an item-count
   attribute.

2. **`check_invariants` = 40 s wall.** Far slower than any other tool; only 63
   stages but 3,458 spans. Worth a dedicated look — it may be doing a full-table
   scan per invariant.

3. **`restore` bug.** One call raised
   `TypeError: '<' not supported between instances of 'NoneType' and 'int'`
   (a null field in a comparison, likely when a checkpoint field is missing); a
   later call captured a clean 1,572-span trace. Intermittent / null-guard bug in
   the restore handler.

4. **`consolidate_now` cannot run via MCP.** It exceeds the tool offload budget
   and times out — no `tool.consolidate_now` span is ever produced (the one true
   "no trace" result). Heavy maintenance cycles need a non-offload path or a
   longer budget if they are to be MCP-invokable.

5. **Cold vs hot.** Only `memorize` shows a dramatic split: cold 6,618 spans /
   6.8 s (drainer + embed), hot 35 spans / 6 ms (WriteGate rejects the near-dup
   before any work). `recall` cold==hot at the span level (no output cache yet —
   task #88). Most read tools are already sub-100 ms; there is little cold/hot
   delta to optimise there.

6. **Deployment observability gap.** The span→**log** path is suppressed in prod
   (v5.106 flood hotfix) and `YADGAR_OTLP_ENDPOINT` is set only via `config.yaml`
   — so spans reach Tempo but never the daemon logs. Trace inspection requires
   the Tempo API; there is no log-based fallback.

## Reusable tooling (added this sweep)

- `capture_trace.py <tool.name> out.json [since] [label]` — pull a tool's newest
  trace from Tempo as a flat rel-timed span JSON.
- `trace_to_boxes.py <capture.json> [out]` — collapse to the distinct stage tree,
  render rectangle DOT/SVG/PNG.

Both are standalone stdlib scripts (companions to `generate.py`), the capture +
render halves of task #82 (auto diagram-from-trace).

_Caveat: absolute ms are single-sample on a daemon ~15 min post-restart (not the
>30 min warm floor the recall-perf checklist wants). Structure + span counts are
robust; treat wall times as indicative, not baseline-grade._
