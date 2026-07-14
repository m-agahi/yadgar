# MCP tool trace sweep — R3 topology + regressions (2026-07-09)

Re-run of the 2026-07-07 sweep on the R3 release (**core 5.117.0 / backend
5.30.0** — write path + consolidation + admin compute forwarded core→backend).
Method identical: fire each tool → `capture_trace.py tool.<name>` →
`trace_to_boxes.py` → `mcp-traces/*.svg` (overwritten in place). Raw span JSONs
in `out/<tool>.json`. Daemon captured ~20–60 min post-restart; wall times are
single-sample, indicative not baseline-grade.

Skipped (baseline SVGs left unchanged): `adr_add` (would pollute the real ADR
log with a probe ADR id), `add_rule` (no rule-delete tool exists — the 07-07
sweep's probe rule id 4 `tag contains trace-sweep-temp` is STILL live in
`get_rules`, proving the pollution), `reembed_all` (forbidden-heavy on the
--cpus 1 daemon).

## Headline 1: R3 topology is live — three forward hops, ~1 ms overhead each

Tools now split into four families (backend span count from the trace):

| Family | Hop | Tools |
|--------|-----|-------|
| Backend-forwarded reads | `POST /recall` | `recall` |
| Backend-forwarded embed | `POST /embed` | `wiki_query`, `restore` (SR-predict embed) |
| Backend-forwarded admin | `POST /admin` | `block_create`, `bookmark_add`, `wiki_delete`, `update_active_work`, `check_invariants` |
| Core-only | none | all other reads (`wiki_list/read`, `memory_get`, `recent_memories`, `project_brief`, `get_rules`, `dlq_inspect`, `block_list`) + all queued writes (`memorize`, `anchor`, `checkpoint`, `wiki_add`) |

Forward-hop overhead (core client `POST` span minus backend server `POST /x`
span): **1.0–1.3 ms** across block_create / bookmark_add / wiki_delete /
update_active_work / recall (median ≈ 1.1 ms). The HTTP boundary is
effectively free; the compute dominates wherever it lands.

`audit_anchors` shows **zero backend spans** — its 42 k `_cosine_similarity`
explosion runs in **core** (but see Headline 3: its boundary spans were
dropped, so backend hops may be invisible).

## Headline 2: writes are now enqueue-only at the tool boundary — and the drainer is DEAD

R3 moved WriteGate/embed/store out of the MCP handler. `memorize` / `anchor` /
`checkpoint` / `wiki_add` now do validate → secret-gate → rules-policy →
`FileQueue.enqueue` and return `{queued: true}` in **5–8 ms** (baseline:
memorize-cold 6.8 s, anchor 1.6 s, checkpoint 123 ms). The I9 ≤5 ms budget is
finally visible at the tool boundary.

**CRITICAL REGRESSION: nothing drains the queue.** During the whole sweep
(~50 min) queue depth grew 1 → 9 (`yadgar_queue_depth{queue="queue"}`); the
oldest entry (04:56 UTC, pre-sweep) never applied; zero drainer/apply spans in
Tempo; zero drainer lifecycle lines in either container's logs since the
04:44 restart. Every queued write since the 5.117.0/5.30.0 deploy is sitting
in `~/.local/share/yadgar/queue/` unapplied — memorize/anchor/checkpoint/
wiki_add are all silently deferred-forever. Both containers DO mount the queue
dir (`~/.local/share/yadgar → /data` on core AND backend), so this is not the
MIGRATION_NOTES volume-mount miss — the drainer task itself is not starting in
either service. Consequences observed live:
- `wiki_add` probe page never committed (`wiki_read`/`wiki_delete` → not found).
- memorize-hot near-dup was NOT rejected (WriteGate now lives in the drainer →
  both cold and hot enqueue identically; the 07-07 "hot = 6 ms WriteGate
  reject" fast-path no longer exists at the tool boundary).
- The 8 sweep probe entries (2×checkpoint, 2×wiki_add `trace-probe-2026-07-09-…`,
  2×memorize, 2×anchor, all tagged `_trace_probe`) will apply whenever the
  drainer is fixed — they need `forget`/`wiki_delete` cleanup THEN.

## Headline 3: span explosions still saturate export — now dropping BOUNDARY spans

"Queue full, dropping Span." fired repeatedly (core log) during recall and
audit_anchors. New failure mode vs 07-07: for **both** `audit_anchors` calls
the `tool.audit_anchors` boundary span itself was dropped — the trace exists
(41.9 k spans) but is unfindable by `{name="tool.audit_anchors"}`; it had to be
captured by trace-id. Span explosion now degrades the observability of the
explosion itself. (Also: Tempo span-level search indexing lagged 60–90 s
behind trace-level availability throughout — capture retries must verify the
trace-id actually changed, not just that a trace was found.)

## Per-tool summary (span count / distinct stages / wall ms, Δ vs 2026-07-07)

| Tool | spans | stages | ms | 07-07 (spans/ms) | note |
|------|------:|------:|----:|------------------|------|
| `audit_anchors` (dry) | **41,954** | 35 | 3,514 | 46,186 / 4,204 | ⚠️ explosion persists (42 k × `_cosine_similarity`, in CORE); boundary span DROPPED |
| `audit_anchors` (apply) | **41,901** | 38 | 3,318 | 46,297 / 4,298 | ⚠️ same; actions list was empty |
| `recall` (cold) | **35,131** | 225 | 24,596 | 27,133 / 11,601 | ⚠️ explosion persists; CE 3-pass 19.0 s = 77% of wall (cold model) |
| `recall` (hot) | **26,895** | 206 | **4,068** | cold==hot in 07-07 | ✅ NEW: repeat query 6× faster — CE spans ~0 ms (CE cache #41/#164 now effective on repeat) |
| `check_invariants` (1st) | 2,046 | 71 | 33,785 | 3,458 / 39,920 | ⚠️ REGRESSION: backend `/admin` runs 33.8 s but core→backend client cuts off at **30 s** → MCP call now ALWAYS errors "timed out" |
| `check_invariants` (2nd) | 1,820 | 67 | 34,001 | — | same timeout; no warm speedup |
| `restore` | 1,608 | 67 | 944 | 1,572 / 940 | ⚠️ task #16 bug STILL errors (see below); per-row `_extract_id`/`_row_to_dict` ≈ 1.5 k spans |
| `project_brief` (catalog) | 1,103 | 60 | 536 | 1,091 / 514 | unchanged fan-out |
| `project_brief` (hot) | 24 | 17 | **6** | 18 / 5 (light) | ✅ epoch-cache HIT on catalog repeat |
| `memory_stats` | 184 | 47 | 722 | (svg only) | no cache; hot ≈ cold (735 ms) |
| `wiki_query` | 184 | 66 | 358 | 17 / 6 | now includes `POST /embed` hop + 9× `get_wiki_page`; 07-07 sample was a trivially-cached call |
| `update_active_work` | 61 | 51 | 174 | (svg only) | `POST /admin`, synchronous commit (returned new memory id) |
| `wiki_list` | 61 | 19 | 108 | 31 / 117 | healthy |
| `block_create` / `bookmark_add` | 57 | 42/36 | 130/135 | 47/111, 30/55 | now via `POST /admin` (+1 ms hop) |
| `wiki_add` (cold) | 50 | 36 | 78 | 167 / 219 | enqueue-only; 69 ms = `check_write_policy`; drainer half UNOBSERVABLE (dead) |
| `recent_memories` | 42 | 20 | 51 | 42 / 51 | unchanged; hot same |
| `memorize` (cold) | 35 | 31 | **7** | 6,618 / 6,804 | enqueue-only (validate→gate→enqueue); full chain moved to (dead) drainer |
| `memorize` (hot) | 35 | 31 | 5 | 35 / 6 | ⚠️ same spans as cold — WriteGate dup-reject no longer at boundary |
| `wiki_read` | 35 | 31 | 30 | 173 / 422 | ✅ much lighter |
| `checkpoint` (+hot) | 33 | 22 | 6/5 | 86 / 123 | enqueue-only |
| `anchor` (+hot) | 32 | 23 | 7/5 | 61–4,267 / 129–1,638 | enqueue-only; large-input explosion gone from boundary (moved to drainer) |
| `wiki_delete` | 32 | 27 | 38 | (svg only) | `POST /admin`; traced the not-found path (probe page never committed) |
| `wiki_add` (hot) | 26 | 21 | 5 | (svg only) | second enqueue, no dup check at boundary |
| `memory_get` | 23 | 19 | 28 | 23 / 27 | unchanged |
| `block_list` | 21 | 17 | 28 | 21 / 110 | healthy |
| `get_rules` / `dlq_inspect` | 16 | 12 | 5/8 | 16 / ~6 | unchanged |

## Findings

1. **Drainer not running (CRITICAL, new).** See Headline 2. All async writes
   since the R3 deploy are queued but never applied. Needs immediate triage —
   until fixed, memorize/anchor/checkpoint/wiki_add are data loss in slow
   motion (queue survives restart, but nothing consumes it).

2. **`check_invariants` unusable via MCP (new).** Baseline 39.9 s in-core call
   succeeded; post-R3 the compute runs in backend (33.8 s server-side) but the
   core→backend HTTP client timeout is 30 s → the tool errors every time while
   the backend finishes anyway (wasted 34 s of --cpus 1 CPU per attempt). Fix:
   raise the `/admin` client timeout for this tool, or make it async/job-based.

3. **`consolidate_now(mode="light")` still cannot run via MCP.** Returns
   `{"error": "timeout", "message": "tool consolidate_now exceeded the offload
   timeout"}`; zero spans emitted (same as 07-07 finding #4). The R3
   consolidation forward did not change the MCP-invokability story.

4. **`restore` bug (task #16) still present; now pinned.** Same
   `TypeError: '<' not supported between 'NoneType' and 'int'`. The 07-09
   trace + core log localize it: the ERROR outcome is on
   `yadgar._shared.restoration.CheckpointRestore._predict_memories`
   (SR cognitive-map prediction stage), after `_build_sr_query` → `POST /embed`
   returns. Look for a None-heat/None-score comparison in the predict ranking.

5. **Span explosions unchanged (task #48).** audit_anchors 42 k (per-anchor
   cosine in core), recall 27–35 k (per-candidate, backend), restore ~1.5 k
   (per-row `_extract_id`/`_row_to_dict`), project_brief-catalog 1.1 k. New
   consequence: boundary-span drops (Headline 3) — the collapse-to-stage-span
   fix is now observability-critical, not just cosmetic.

6. **Cold vs hot.** The story inverted since 07-07: `memorize` no longer has a
   hot fast-path at the boundary (both enqueue), while `recall` GAINED one
   (24.6 s → 4.1 s; CE spans vanish on repeat — cross-encoder cache hits) and
   `project_brief` catalog now cache-HITs (536 → 6 ms). `check_invariants` and
   `memory_stats` show no warm effect.

7. **No boundary-only blind spots.** Minimum trace = 16 spans (get_rules,
   dlq_inspect); every fired tool emitted a full stage tree. The one "no
   trace" tool remains `consolidate_now`. Caveat: audit_anchors' dropped
   boundary spans are a *selective* blind spot inside otherwise-rich traces.

8. **Instrumentation coverage post-R3 move is intact** (I33 concern): moved
   code emits spans under the new `yadgar._shared.*` / `yadgar.core.*` /
   `yadgar.backend.*` names; no formerly-instrumented stage went dark on the
   paths that still execute. The unobservable half is the drainer chain — not
   because instrumentation was lost, but because the drainer never runs.

## Sweep hygiene / cleanup state

- Cleaned during sweep: probe block (`trace_probe_2026_07_09`), probe bookmark,
  `_active_work` restored to pre-sweep content.
- **Pending user cleanup after drainer fix:** 8 queued probe entries will
  apply — `forget` the 2 memorize + 2 anchor probes (tag `_trace_probe`),
  `wiki_delete trace-probe-2026-07-09-wiki-probe-page` (2 queued adds), and
  re-`checkpoint` (2 probe checkpoints will supersede the PR-171 checkpoint).
- Leftover from the 07-07 sweep, still present: rule id 4
  (`tag contains trace-sweep-temp`, penalty soft rule) — no delete-rule tool
  exists; remove manually if unwanted.

_Capture lessons for the next sweep: Tempo span-name search lags trace
availability by 60–90 s (verify trace-id changed between cold/hot captures);
a boundary-span drop makes name search return exit 3 forever — fall back to
`{name=~".*<distinctive-inner-span>.*"}` + capture by trace-id._
