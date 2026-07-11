# `yadgar/core/` — the router/session/ops layer

Ships in the thin `yadgar` image (1 CPU, no ML models). Core is the ROUTER:
MCP tool surface, HTTP endpoints, session state, response caches, and every
host-ops entrypoint that must work while containers are down.
See `AGENTS.md` here for the placement rules.

## Packages

| Package | What it is |
|---|---|
| `server/` | MCP server, tools, routes, middleware — the product surface |
| `cli/` | `yadgar …` CLI entrypoints (host-ops; DB writes forward) |
| `daemon/` | container orchestration, daemon threads, sd_notify, request draining |
| `viz/` | knowledge-graph viz HTTP server (compute forwards to backend, Car E3) |
| `graph/` | GraphAPI viz assembly + cached layout (Car E3 forwards the compute) |
| `cache/` | core-side response caches with container-aware budgets |
| `consolidation/` | core-side consolidation scheduler (compute runs in backend) |
| `install/`, `update/`, `bootstrap/`, `hooks/` | install/bootstrap + Claude Code hook scripts |
| `seed/`, `export/`, `repo_wiki/` | project seeding, DB export, repo-wiki generation |
| `backup/`, `vacuum/`, `_surreal_runner/`, `ops/` | host-ops: quiesced snapshots, SurrealKV vacuum, local surreal processes, service control |
| `staleness/` | file-watch staleness detector (writes relocate to backend, Car E1) |
| `auth_middleware/`, `sanitize/`, `sensitive_lock/`, `lifecycle/` | bearer auth, log sanitation, sensitive-op lock, process lifecycle |
| `systemd/`, `static/`, `install_assets/`, `scripts/` | unit files, viz static assets, bundled agents, hook script assets |

The flat `.py` files at this root are back-compat PEP-562 shims from the T2
layer-boundary moves — do not add new ones.

## How it connects

Core imports `_shared/` freely and reaches backend ONLY via HTTP endpoints /
the file-queue seam (import-linter contract 3 forbids direct imports).
