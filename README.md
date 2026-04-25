# yadgar

Persistent memory engine for Claude Code. Memories decay by heat, consolidate during sleep cycles, and are gated on arrival by surprise — so only novel information gets stored.

## Install

```bash
pip install yadgar
yadgar setup
```

## Connect to Claude Code

Add to `~/.claude.json` (or `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "yadgar": {
      "command": "yadgar",
      "args": [],
      "env": {}
    }
  }
}
```

Or run as a persistent daemon (recommended):

```bash
yadgar daemon start
yadgar daemon configure-mcp   # switches Claude to HTTP transport
yadgar daemon install-service # auto-start on login (systemd)
```

## First use

```
memorize("We're using SurrealDB with surrealkv backend", "/home/user/myproject", ["db", "infra"])
recall("database setup")
get_project_context("/home/user/myproject")
```

## How it works

**Heat decay** — every memory has a heat score (0–1) that decays at 0.9995× per consolidation cycle. Cold memories fade; hot ones persist. Protected memories (`is_protected=True` or `_anchor` tag) never decay.

**Surprise-gated writes** — `memorize()` passes content through a write gate. If the content is too similar to what's already stored, it's rejected. Only novel information gets in.

**Sleep consolidation** — a background daemon runs periodic consolidation: heat decay, cluster detection, action-log processing, wiki draft proposals.

**Semantic retrieval** — `recall()` fuses vector similarity, BM25 full-text search, personalized PageRank (knowledge graph walk), and spreading activation, then re-ranks with a cross-encoder.

## Core tools

| Tool | Purpose |
|---|---|
| `memorize(content, context, tags)` | Store a memory. `context` must be the absolute directory path. |
| `recall(query)` | Semantic + keyword search across all memories. |
| `get_project_context(directory)` | Hot memories + wiki for a project directory. |
| `forget(memory_id)` | Delete a memory. |
| `checkpoint(directory, ...)` | Snapshot working state before context compaction. |
| `restore(directory)` | Reconstruct context after compaction. |
| `anchor(content, context, reason)` | Store a protected memory that never decays. |
| `wiki_add(title, content)` | Create a wiki page. |
| `wiki_query(query)` | Search wiki pages. |
| `memory_stats()` | System health and counts. |

## Configuration

```bash
yadgar config init        # write ~/.yadgar/config.yaml with all defaults
yadgar config list        # show current settings
yadgar config set retrieval_profile fast
```

Retrieval profiles: `fast` (vector + BM25, no reranking), `balanced` (+ PPR + cross-encoder, default), `full` (all signals).

## Secret protection

Always-on patterns block storage of AWS keys, private keys, JWT tokens, GitHub tokens, database connection strings, and generic credential patterns. These cannot be disabled.

User-defined rules:

```bash
# Block all memories from a directory
yadgar rules add write_block "directory_context matches /work/classified/*"

# Export/import rules
yadgar rules export > my_rules.yaml
yadgar rules import my_rules.yaml
```

## Docker

```bash
docker run -v ~/.yadgar:/data -p 8765:8765 yadgar
```

```json
{
  "mcpServers": {
    "yadgar": {
      "type": "streamable-http",
      "url": "http://localhost:8765/mcp"
    }
  }
}
```

## CLI reference

```
yadgar                          # start MCP server (stdio, default)
yadgar --transport streamable-http --port 8765
yadgar daemon start|stop|restart|status
yadgar daemon configure-mcp    # switch Claude to HTTP transport
yadgar daemon install-service  # systemd user service
yadgar stats                   # memory statistics
yadgar stats --project /path   # project-scoped stats
yadgar vacuum                  # compact SurrealKV commit log
yadgar seed <directory>        # bootstrap memory for existing project
yadgar viz                     # knowledge graph at http://localhost:42069
yadgar rules export|import
yadgar config init|list|get|set|edit
```

## FAQ

**How is this different from just using markdown files?**

Markdown files don't decay, don't rank by recency or relevance, don't detect duplicates, don't protect critical facts from being forgotten, and don't surface related context automatically. Yadgar behaves more like long-term memory than a note-taking tool — it forgets what you don't revisit and strengthens what you use.

**Why `memorize` instead of `remember`?**

`remember` is a common word that collides with natural language. `memorize` is specific enough that Claude won't accidentally call it when the user says "remember to do X."

**Does it work without the daemon?**

Yes — `yadgar` with no arguments runs as a stdio MCP server. The daemon is optional but recommended for persistent consolidation and lower per-session startup cost.

**Where is data stored?**

`~/.yadgar/surreal_db/` by default. Override with `YADGAR_DATA_DIR` or `--db-path`. In containers, mount `/data` as a volume.
