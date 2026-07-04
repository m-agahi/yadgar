# Yadgar subsystem diagrams

A small, **YAML-driven** flow-diagram generator. Each diagram is a data file in
[`specs/`](specs/); [`generate.py`](generate.py) renders it to SVG + PNG in
[`out/`](out/). **Adding a new diagram is a new YAML file — no code change.**

## Rendered diagrams

| Spec | What it shows |
|------|---------------|
| [`specs/recall-warm-cache-miss.yaml`](specs/recall-warm-cache-miss.yaml) | Warm recall, both backend caches (EMBED_CACHE + CE_CACHE) **miss** |
| [`specs/recall-warm-cache-hit.yaml`](specs/recall-warm-cache-hit.yaml) | Same pipeline, both caches **hit** (embed + CE served from cache) |
| [`specs/write.yaml`](specs/write.yaml) | `memorize` — fast-path enqueue + async drainer replay |
| [`specs/recall-cold-trace-2026-07-04.yaml`](specs/recall-cold-trace-2026-07-04.yaml) | **Real** cold recall — every pipeline stage with its `@rel_start` timestamp + duration, from OTel trace `242cc546` (total 26.1 s; cross-encoder runs 3× ≈ 20 s = the wall) |

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
