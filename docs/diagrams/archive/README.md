# Diagram Archive

Old diagram generations, grouped by capture/render date. Live diagrams stay in `docs/diagrams/out/` (current) and `docs/diagrams/specs/` (current specs).

## Directory layout

```
archive/
├── 2026-07-04/    recall cold-trace (OTel 242cc546) + proposed-optimized mock
├── 2026-07-06/    YAML-spec era: forward-only warm, recall/write/cache path diagrams + superseded recall-warm-cache-{hit,miss} + write undated outputs
└── 2026-07-07/    MCP tool trace sweep (pre-R3) — 3 stale mcp-traces SVGs + sweep doc
```

## Retention policy

Archived specs and outputs are kept (not deleted) because plan docs reference them as historical baselines (e.g. `recall-cold-trace-2026-07-04.yaml` cited in `docs/plans/recall-3-train-overhaul-2026-07-04.md`). Path references in plan docs are updated to point here.

## Current live diagrams (docs/diagrams/out/)

- `*-trace-2026-07-09.*` — MCP tool simplified two-lane traces (R3, core 5.117 / backend 5.30)
- `docs/diagrams/mcp-traces/*.svg` — detailed per-tool span-tree SVGs (07-09 sweep, 32 tools)
- `docs/diagrams/specs/` — current YAML-driven specs (write, recall-cold-trace-07-04 + recall-proposed-07-04 stay as historical baselines per plan references)
