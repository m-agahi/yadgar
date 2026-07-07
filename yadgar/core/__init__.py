"""yadgar.core — the core process layer (folder split #17).

MCP server + tools, hooks, consolidation scheduler, daemon, CLI, core-side
read-tool cache, install/ops. Runs in the core container (thin: no ML models).

Layer rules (import-linter enforced): core may import ``yadgar._shared`` and
``yadgar.backend`` ONLY via the HTTP `/recall`+`/rerank` boundary — NOT backend
internals. core must NOT be imported by ``yadgar._shared`` or ``yadgar.backend``.
"""
