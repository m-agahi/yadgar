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

## Docker

```bash
docker run -d \
  -v yadgar-data:/data \
  -p 8765:8765 \
  --name yadgar \
  yadgar
```

Add to `~/.claude.json`:

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

Data persists in the `yadgar-data` volume. Backup: `docker run --rm -v yadgar-data:/data -v $(pwd):/backup alpine tar czf /backup/yadgar-backup.tar.gz /data`

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

## Configuration

```bash
yadgar config init        # write ~/.yadgar/config.yaml with all defaults
yadgar config list        # show current settings and sources
yadgar config set retrieval_profile fast
```

Settings can also be set via environment variables (`YADGAR_*`) or by editing `~/.yadgar/config.yaml` directly. Environment variables take priority over the file, which takes priority over defaults.

## Documentation

- [Architecture](docs/architecture.md) — component map, data flow, module responsibilities
- [Memory lifecycle](docs/memory-lifecycle.md) — heat decay, archiving, action stream, pruning
- [Retrieval pipeline](docs/retrieval.md) — multi-signal fusion, reranking, query routing
- [Configuration reference](docs/configuration.md) — all settings with defaults and env vars

## Secret protection

Always-on patterns block storage of AWS keys, private keys, JWT tokens, GitHub tokens, database connection strings, and generic credential patterns. These cannot be disabled.

User-defined rules:

```bash
yadgar rules add write_block "directory_context matches /work/classified/*"
yadgar rules export > my_rules.yaml
yadgar rules import my_rules.yaml
```
