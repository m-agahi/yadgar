# `core/server/` — MCP server + tools

The product surface: every MCP tool, HTTP route, and middleware.

- `_app.py` — FastMCP app assembly + instrumentation
- `tools/` — one module per tool family (memorize, recall, wiki, blocks,
  admin_*, project, adr, …). Write tools MUST call `gate_or_reject` (I26)
  and go through the file-queue seam — no raw DB writes (ADR-0078).
- `routes/` — control/viz/config HTTP routes
- `http.py` — hook endpoints + SSE (2 raw writes forward in Car E1)

New tool checklist: `@_tool` decorator (span), secret gate on writes,
CAPABILITY_REGISTRY entry (I32 lint), BEHAVIOR_CONTRACT statement if
user-visible behavior.
