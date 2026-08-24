# Yadgar CLI cheat-sheet

One-page operator reference for the **top 10 `yadgar` CLI verbs** — the verbs
you reach for in a working session. Run `yadgar --help` for the full subcommand
tree; this page is the short-list, not the manual.

Use it when: starting a session, recovering after a crash, onboarding a new
operator, or reminding yourself which subcommand owns a one-liner you've used
three times this month.

For the host-side MCP tools (memorize / recall / anchor / wiki_* / …) see
[`README.md` § MCP tools](../../README.md#mcp-tools). For installation,
configuration, and platform detail see [`install.md`](install.md) and
[`configuration.md`](configuration.md).

---

## Top 10 verbs

| # | Verb | What it does | Example |
|---|------|--------------|---------|
| 1 | `yadgar setup` | First-run: write config + secrets, register MCP, install code-graph binary. | `yadgar setup --no-code-graph` |
| 2 | `yadgar daemon <verb>` | Manage the background daemon: `start`, `stop`, `status`, `logs`, `health`, `restart`, `configure-mcp`, `install-service`. | `yadgar daemon status` |
| 3 | `yadgar viz` | Launch the knowledge-graph UI on http://localhost:42069 (reverse-proxies `/api/*` to the daemon). | `yadgar viz --port 42069` |
| 4 | `yadgar stats` | Memory counts, heat distribution, project totals. `--project <dir>` scopes to one project. | `yadgar stats --project .` |
| 5 | `yadgar seed <dir>` | Bootstrap memory for an existing project (reads README + top-level docs). | `yadgar seed ~/code/myapp` |
| 6 | `yadgar vacuum` | Compact the SurrealKV store via export → snapshot → swap → reimport. Daemon is briefly stopped. | `yadgar vacuum --yes` |
| 7 | `yadgar export duckdb` | Analytics snapshot to a DuckDB file (`pip install yadgar[analytics]` first). | `yadgar export duckdb --output snap.duckdb` |
| 8 | `yadgar config <verb>` | Manage configuration: `init`, `list`, `get <key>`, `set <key> <value>`. Also editable in the viz System → Config panel. | `yadgar config get YADGAR_LOG_FORMAT` |
| 9 | `yadgar update <verb>` | Check for, apply, or roll back a newer yadgar version. `--check` is read-only. | `yadgar update --check` |
| 10 | `yadgar install --client <name>` | Wire MCP registration + rules + hooks for an agentic client. `--hooks` re-wires Claude Code hooks; `--print` emits declarative JSON. | `yadgar install --client claude-code --hooks` |

---

## One-line reminders

- **Starting a session** — `yadgar daemon status` (is it up?) → `yadgar stats`
  (is it healthy?).
- **Wiring Claude Code** — `yadgar install --client claude-code --hooks`
  (idempotent; re-run after upgrades).
- **Recovery** — `yadgar daemon logs` for the daemon, `yadgar daemon restart`
  after a config change, `yadgar vacuum` when the DB gets heavy.
- **Read-only DB peek** — use the MCP tool `db_inspect("SELECT ...")`, not the
  CLI; the CLI has no host-side read path.

## See also

- **Full CLI tree** — `yadgar --help` (live), or
  [`README.md` § CLI](../../README.md#cli).
- **Hook-internal verbs** (`drain`, `capture`, `restore`, `context`) — invoked
  by Claude Code hooks, not by hand; see [`hooks.md`](hooks.md).
- **MCP tools** — the verb set agents actually call:
  [`README.md` § MCP tools](../../README.md#mcp-tools).
- **Agent-prompt library** — `yadgar code-graph` and the dispatch helpers
  live here: [`agent-prompts.md`](agent-prompts.md).
