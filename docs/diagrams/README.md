# Yadgar subsystem diagrams

A small, **YAML-driven** flow-diagram generator. Each diagram is a data file in
[`specs/`](specs/); [`generate.py`](generate.py) renders it to SVG + PNG in
[`out/`](out/). **Adding a new diagram is a new YAML file — no code change.**

## Rendered diagrams

| Spec | What it shows |
|------|---------------|
| [`specs/core-backend-cache-overview-2026-07-06-1600.yaml`](specs/core-backend-cache-overview-2026-07-06-1600.yaml) | **PRIMARY OVERVIEW — CORE ↔ BACKEND cache + component, two columns (START HERE for the caching train).** Answers the question the two detail diagrams below left confusing: *do the caches move core→backend?* **They do NOT.** `rankdir=LR`: **LEFT column = CORE** (`yadgar/`, MCP tools + `recall()` thin forwarder + the SHIPPED unified `Cache` #164 + the `data_epoch` bus), **RIGHT column = BACKEND** (`yadgar/backend/`, `backend.recall` pipeline + the NEW unified `Cache` + ML + DB). Arrows = **who-calls-who**. There are **TWO separate unified caches, one per service — they never merge**, each drawn inside its own column with no edge between them. **ONLY TWO signals cross the boundary:** `recall()` **HTTP POST /recall** (solid, traceparent) and the structural **`data_epoch`** (async dashed, rides each recall request into the 3 DataEpoch namespace keys). Within CORE: read tools → core cache; writes + background cycles → `bump_epoch`. Within BACKEND: `backend.recall` branches to each namespace (`ce`/`embed` = ModelCkpt, `ckpt_sha` self-busts → ML model; `memory_doc`/`engram_slot`/`graph` = DataEpoch → DB). Legend: **SHIPPED** = core cache (#164) + backend `ce`/`embed` (exist today as loose LRUCache); **NEW** = the unified backend `Cache` **class itself** + the 3 data namespaces + the cross-service `data_epoch`. Pairs with the two detail views below (kept, not superseded — they carry the pipeline/structural depth this overview omits). |
| [`specs/recall-paths-all-2026-07-06-1024.yaml`](specs/recall-paths-all-2026-07-06-1024.yaml) | **CURRENT recall diagram — ALL PATHS, REAL warm-miss, core 5.111 / backend 5.16.** Forward-only pipeline (tool.recall → `_forward_to_backend` → POST /recall → backend.recall) with real Tempo per-stage ms (trace `17f93bcc`, PLAIN wall 19.3 s; CE 3 passes ≈ 15.2 s = 79%). Shows the `profile=fast` (1.5 s, no CE) and `mode=landscape` (2.2 s, astrocyte.consensus_retrieve) branches. DB side-effects run in **backend**, SESSION side-effects (SR transition) stay in **core**. All 4 variants share one trace_id core↔backend (traceparent verified). Supersedes `recall-forward-only-warm-2026-07-06.yaml`. |
| [`specs/write-paths-all-2026-07-06-1024.yaml`](specs/write-paths-all-2026-07-06-1024.yaml) | **NEW — all WRITE paths + epoch cache-bust.** memorize / anchor / checkpoint (surprise-gate → enqueue ≤5 ms I9 → async drainer replay) and wiki_add (commit → `_bump_wiki_epoch`). The KEY content: `_bump_epoch_for_context` (memorize/forget, per-dir) BUSTS `project_brief`; `_bump_wiki_epoch → bump_epoch(None)` (wiki writes, global) BUSTS wiki_read/wiki_query/prelude + project_brief. All 4 writes verified live; the project_brief epoch-bust was empirically observed (HIT→MISS on the same key). Drainer ms = estimates; flow + bust arrows verified. |
| [`specs/cache-layer-all-2026-07-06-1024.yaml`](specs/cache-layer-all-2026-07-06-1024.yaml) | **NEW — cache placement, hit/miss, invalidation (KEY diagram).** Every cache from the unified `Cache` class (`yadgar/cache.py`) + backend LRUCache, with REAL live metrics. Core epoch/TTL caches (project_brief, wiki_query/read, prelude) HIT on repeat and BUST on epoch-bump (observed). Backend CE/embed LRU caches do **NOT** accelerate a repeated recall — the recall CE path never consults the CE cache, so CE ran full on an identical repeat (repeat wall **20.5 s > 19.3 s** first). ADR-0030 lesson, still true post-#164. |
| [`specs/backend-cache-unified-2026-07-06-1230.yaml`](specs/backend-cache-unified-2026-07-06-1230.yaml) | **DETAIL VIEW (depth for the overview above) — unified backend `Cache` class, STRUCTURAL view.** The [backend caching train](../plans/backend-caching-train-2026-07-06.md) design: ONE `Cache` class (`yadgar/backend/cache.py`, mirroring core `yadgar/cache.py`) — one class, a `_REGISTRY` of 5 named namespaces, policy bound at construction. Namespaces: `ce` + `embed` **[MOVED-IN]** (ModelCkpt-keyed, `ckpt_sha` self-busts) and `memory_doc` + `engram_slot` + `graph` **[NEW]** (DataEpoch-keyed). The invalidation split is the point: CORE bumps a global structural `data_epoch` on **every** structural mutator — INTERACTIVE writes (memorize/forget/anchor/memory_update/wiki/entity/relationship) **and** BACKGROUND cycles (consolidate/nightly merge·dedup·create·delete, vacuum deletes, reembed embedding-change) — and passes it to the backend on each recall request (cross-service async arrow) → it lands in the 3 data-namespace keys, so stale keys naturally miss. A dedicated note calls out that **heat is NEVER cached**: heat/access are volatile (change every recall + decay cycle) → fetched fresh, `memory_doc` caches only the immutable projection, so pure heat-decay does NOT bump `data_epoch`. Legend distinguishes MOVED-IN vs NEW + ModelCkpt vs DataEpoch. Pairs with the pipeline view below. |
| [`specs/backend-cache-in-pipeline-2026-07-06-1230.yaml`](specs/backend-cache-in-pipeline-2026-07-06-1230.yaml) | **DETAIL VIEW (depth for the overview above) — the 5 namespaces IN the recall pipeline, FLOW view.** Companion to the structural view. The real warm cache-MISS PLAIN pipeline (trace `17f93bcc`, 19.3 s, CE 3-pass = 79%) with each cached stage arrowing to the namespace it consults: `encode_query`→`embed`, `spreading`+`ppr`→`graph` (est. 300–400 ms), `build_results`→`memory_doc` (150–200 ms), `engram_links`→`engram_slot` (100–150 ms). The `ce` namespace shows the **#41 within-request CE dedup**: `crossfuse` **POPULATES** `(query,text)` → `cross_encoder` **LOOKUP** of the overlapping pair **HITs** (same request/query/ckpt) → skips re-scoring; `multi_passage` is mostly disjoint (partial). `memory_doc` caches only the immutable projection (heat/access fetched fresh, never cached) and `data_epoch` is bumped by all structural mutators (interactive writes + background consolidate/vacuum/reembed). Numbers illustrative on a proposed design; stage ms are live-real. |
| ~~[`specs/recall-forward-only-warm-2026-07-06.yaml`](specs/recall-forward-only-warm-2026-07-06.yaml)~~ | **SUPERSEDED** by `recall-paths-all-2026-07-06-1024.yaml` (recall-only, no write/cache paths; captured on core 5.108 / backend 5.15 before the caching train). Kept for the `eeb62550` trace reference. |
| ~~[`specs/recall-warm-cache-miss.yaml`](specs/recall-warm-cache-miss.yaml)~~ | **SUPERSEDED** (architecture now wrong: drew the pipeline in *core*; forward-only moved it all to backend, and its "~1.6 s warm floor / CE ~2 ms" was a histogram undercount — see `docs/architecture.md:99`). Use `recall-forward-only-warm-2026-07-06.yaml`. |
| ~~[`specs/recall-warm-cache-hit.yaml`](specs/recall-warm-cache-hit.yaml)~~ | **SUPERSEDED** (same core/backend split is now wrong under forward-only). |
| [`specs/write.yaml`](specs/write.yaml) | `memorize` — fast-path enqueue + async drainer replay |
| [`specs/recall-cold-trace-2026-07-04.yaml`](specs/recall-cold-trace-2026-07-04.yaml) | **Real** cold recall — every pipeline stage with its `@rel_start` timestamp + duration, from OTel trace `242cc546` (total 26.1 s; cross-encoder runs 3× ≈ 20 s = the wall). NOTE: authored pre-forward-only — its ARCH NOTE that "the pipeline runs in CORE (#85 not done)" is now stale; forward-only relocated the pipeline to the backend (see the forward-only warm spec above). |
| [`specs/recall-proposed-optimized-2026-07-04.yaml`](specs/recall-proposed-optimized-2026-07-04.yaml) | **PROPOSED design-for-scaling** (NOT built — gated on post-Ettin measurement) — recall relocated all-backend (#85); **bounded parallelism** (`--cpus 2` = accessibility FLOOR not ceiling: parallel structure + thread budget ≈ncpu → 2 cores ~sequential, more cores fan out); **GIL-aware split** — ML stages (embed/KNN/CE) parallel-safe & degrade gracefully, Python/graph stages (fusion/PPR/spreading) serial-by-default; the **THREE CE passes stay SEPARATE + CONCURRENT** (memory cross_encoder ∥ optional multi_passage ∥ wiki crossfuse-CE — NOT merged, they scale with cores); memory∪wiki **late-union at finalize**; side-effects forked async off-path; CE targeted for **Ettin-32M/68M** swap (~2-6× cheaper) + keep-warm. All numbers PENDING MEASUREMENT |

Outputs: `out/<spec-name>.svg`, `.png`, and the intermediate `.dot`.

## Usage

```bash
# one spec -> out/recall-warm-cache-miss.{svg,png,dot}
python docs/diagrams/generate.py docs/diagrams/specs/recall-warm-cache-miss.yaml

# custom output stem (emits BOTH <stem>.svg and <stem>.png)
python docs/diagrams/generate.py docs/diagrams/specs/write.yaml -o /tmp/write

# render every spec in specs/
python docs/diagrams/generate.py --all
```

Run under the project venv (`uv run python docs/diagrams/generate.py --all`) or
standalone. There is **no new dependency**: the tool reads YAML via
`ruamel.yaml` (already a project dep), falling back to `PyYAML` if run outside
the venv, and renders by shelling out to the Graphviz `dot` binary.

### Requirements

- **Graphviz `dot`** on `PATH` — produces the SVG + PNG.
  If `dot` is missing, the tool still writes the `.dot` source **and** a
  `.mmd` Mermaid fallback, and prints a warning (no raster/vector image).
- A YAML reader (`ruamel.yaml` or `PyYAML`).

## Add a new diagram

1. Create `specs/<name>.yaml` (copy an existing one as a template).
2. Author `clusters`, `nodes`, `edges` from the **actual code flow**.
3. `python docs/diagrams/generate.py docs/diagrams/specs/<name>.yaml`
4. Commit the spec **and** the rendered `out/<name>.{svg,png}`.

That's it. No edit to `generate.py`.

## Schema reference

The schema is **forward-compatible**: unknown keys are ignored, and every field
has a sane default, so specs authored against a newer schema still render.

### Top-level

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `title` | str | spec filename | Diagram title (first line of the graph label). |
| `subtitle` | str | `""` | Second line of the label — good for provenance / source refs. |
| `rankdir` | `TB` \| `LR` | `TB` | Flow direction (top-bottom or left-right). |
| `total_ms` | number | *(computed)* | Explicit total-time annotation. If omitted, the sum of node `time_ms` is shown. |
| `show_total` | bool | `true` | Whether to render the `total ≈ …` annotation. |
| `clusters` | list | `[]` | Grouping boxes (e.g. process / service boundaries). |
| `nodes` | list | `[]` | Pipeline steps. |
| `edges` | list | `[]` | Directed connections between nodes. |

### `clusters[]`

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `id` | str | *(required)* | Referenced by `node.cluster`. |
| `label` | str | `id` | Box title. |
| `kind` | str | `id` | Style key: `core`, `backend`, `external`. Falls back to a neutral gray for unknown kinds; if omitted, the `id` is used as the kind (so `id: core` styles itself). |

### `nodes[]`

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `id` | str | *(required)* | Unique node id, referenced by edges. |
| `label` | str | `id` | Visible text. Use `\n` for a line break. |
| `cluster` | str | *(none)* | `id` of the cluster it belongs to. Unclustered nodes render at top level. |
| `time_ms` | number | *(none)* | Per-component timing; rendered as a second line under the label and summed into the total. |
| `type` | str | `compute` | Style key (see below). |

**Node `type` styles** (unknown types → plain rounded box):

| `type` | Shape / intent |
|--------|----------------|
| `compute` | rounded box — in-process CPU work |
| `io` | rounded box (tan) — DB / network IO |
| `model` | box (purple) — model inference (embed, cross-encoder) |
| `cache` | dashed rounded box (green) — served from cache |
| `store` | cylinder — a datastore write |
| `gate` | diamond — a decision / gate |
| `start` | circle — entry point |
| `end` | double circle — terminal result |

### `edges[]`

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `src` | str | *(required)* | Source node id. |
| `dst` | str | *(required)* | Destination node id. |
| `order` | int | *(none)* | Sort key for sequential flow; also shown as the edge label when no `label` is set. |
| `label` | str | `""` | Edge caption (overrides the auto `order` label). |
| `type` | str | `flow` | Style key: `flow` (solid), `skip` (gray dashed), `async` (green dashed). |

## Timings — provenance and how to update

The `time_ms` values in the shipped specs are **placeholders / estimates**, not
measured numbers. A few are sourced (the priors N+1 ~800/730 ms and the ≤5 ms
MCP-handler write budget come from ADR-0030 via
[`../plans/cache-refactor-2026-07-01.md`](../plans/cache-refactor-2026-07-01.md));
the rest are type-based estimates.

Real per-stage numbers come from a separate profiling task
(`../plans/recall-warm-profile-2026-07-02.md`). **The YAML is the single source
of truth**: when the measured numbers land, updating a diagram is a one-line
`time_ms:` edit per node followed by a re-render — no code change.

## What the recall cache diagrams actually show

There is **no query→ranked-output result cache today** — that is a *proposal*
(lever-(a)) in the cache-refactor plan, not shipped code. The only caches that
exist are **EMBED_CACHE** (query→embedding) and **CE_CACHE** (query,passage→
rerank score), both in the backend embed service. So the hit/miss diagrams share
the **same topology**; only the `embed` and `CE rerank` node timings differ.

The honest lesson (ADR-0030): even a full cache hit stays slow, because those
caches memoize the cheap *compute*, while the expensive *IO* — the priors N+1
(~1.5 s) and the per-query spreading-activation BFS — is uncached and runs
identically on both paths.
