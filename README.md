<div align="center">

<img src="docs/assets/yadgar.svg" alt="yadgar logo" width="320">

[![Python](https://img.shields.io/badge/python-3.14%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)

</div>

[Changelog](docs/CHANGELOG.md) · [Benchmark](#benchmark) · [Architecture](#architecture) · [Roadmap](#roadmap) · [JS/TS SDK](docs/reference/sdk-js.md) · [Agents guide](AGENTS.md)

> **AI coding agents:** the operational guide lives in [`AGENTS.md`](AGENTS.md) — setup commands, dev environment, test runner, code style, PR rules, security gates. This README is the human overview.

> **Why this repo lives on GitHub (moved from Codeberg, 2026):** Codeberg's [updated Terms of Use](https://codeberg.org/Codeberg/org/commit/71149c7fc95ccfeae36109b5cddca339e4aa1473) — § 2 (1) 7, added in *"Proposal Assembly 2026: prohibit LLM-extrusions"* (2026-06-29) — now ask that people **not** host projects that *"mostly consist of code written by 'generative AI'-tools."* Yadgar is built with heavy AI-agent assistance and is candid about it, so it no longer fits Codeberg's hosting policy and has moved here. No hard feelings — Codeberg is an excellent home for human-authored free software; this is a policy-fit decision, nothing more.

**A persistent memory engine for MCP clients.** Tell it what matters and it survives across sessions — decaying what you stop touching, promoting what repeats, filtering recall to the git branch you're on, and pairing every memory with a curated wiki that searches through the same ranking pipeline.

> **Supported clients:** one shared streamable-HTTP daemon (`http://127.0.0.1:8765/mcp`) serves the memory and wiki MCP surface to all 9 supported clients: `claude-code`, `codex`, `gemini`, `cursor`, `cline`, `windsurf`, `kiro`, `amp`, `opencode`. Claude Code additionally gets the full harness integration (hooks, task-list mirror, CLAUDE.md sync). All other clients receive MCP registration and a rules file (AGENTS.md-equivalent).
>
> **Multi-client setup:** use `yadgar install` to wire any client. Register a single client by name, or probe and register all detected clients at once:
>
> ```bash
> yadgar install --client <name>           # e.g. --client opencode
> yadgar install --auto-detect             # detect + register all installed clients
> yadgar install --client <name> --print   # dry-run: emit JSON to stdout, no file writes
> ```
>
> `--print` is the nix/home-manager contract (task #67): it outputs the full config without touching the filesystem, suitable for declarative activation. Per-platform detail and flag reference: [`docs/reference/install.md`](docs/reference/install.md).

*Yadgar* (یادگار) is Persian for "memento, keepsake."

---

## What it is

Claude Code forgets everything when a session ends, when you `/clear`, or when context compacts. Yadgar is the layer that remembers. It runs as an [MCP](https://modelcontextprotocol.io/) server alongside Claude Code and gives it two durable stores:

1. **Memory** — episodic + semantic facts, each carrying a `heat` value that decays over time. Access reheats; disuse decays; recurring episodes get promoted to semantic memory by a nightly consolidation cycle modelled on how brains sleep.
2. **A curated wiki** — long-lived, versioned, branch-scoped pages: conventions, module purpose, past decisions, where subsystems live. Written deliberately, not captured incidentally.

A single `recall()` query searches **both stores at once**, fuses and re-ranks the results, weights them by heat, gates out decayed entries, and boosts matches on your current git branch. Critical context can be *anchored* so it never decays; whole working states can be *checkpointed* and *restored* across a `/compact`.

### Why

- **Survives sessions.** Heat decay drops what you stopped using; surprise gating drops duplicates on arrival; anchors pin what must never be lost.
- **Branch-aware.** Recall boosts current-branch matches; wiki pages resolve in branch precedence (current → default → unscoped) so canonical knowledge stays reachable from feature work.
- **One ranking pipeline for memory + wiki.** Curated knowledge and episodic memory come back in a single ranked result, not two separate searches.
- **Consolidates while you sleep.** A nightly brain cycle decays heat, promotes episodes to semantics, discovers causal links, forms associative links, and merges duplicates — without dropping the MCP connection.
- **Self-documenting.** Architecture Decision Records, a reusable agent-prompt library, a harness task-list mirror, and an interactive 3D knowledge-graph all live in the same store.

---

## Key features

### Memory
- **Heat-decay lifecycle** — every memory carries `heat ∈ [0,1]` decaying exponentially; access reheats, disuse decays, unused items eventually archive and purge.
- **Surprise-gated writes** — a similarity gate rejects near-duplicate writes on arrival (rejections land in the DLQ, inspectable via `dlq_inspect`).
- **Write-time contradiction detection** — a lightweight negation + action-divergence heuristic flags contradicting memories and decays their confidence, without blocking the write.
- **Anchors** — protected memories that never decay; `audit_anchors()` surfaces redundant, oversize, expired, or near-duplicate (cosine ≥ 0.95) anchors across projects.
- **Checkpoint / restore** — `checkpoint(directory)` snapshots your working state before compaction; `restore(directory)` reconstructs it afterward via a hippocampal-replay blend of checkpoint + anchors + hot memories + predicted context. Use `restore`, not `recall`, to resume after `/clear` or `/compact`.
- **Memory blocks** — Letta-style named, scoped, char-limited core-memory containers (`block_*`) for always-in-context working state.

### Curated wiki
- **Versioned pages** — full history (`wiki_history`, `wiki_diff`, `wiki_read_version`, `wiki_restore`); `wiki_add` commits directly (no draft step).
- **Branch-scoped resolution** — `wiki_read(slug)` resolves current branch → default branch → unscoped.
- **Surgical editing** — anchor-text and positional edit tools (`wiki_replace_text`, `wiki_insert_before/after`, `wiki_append_section`, `wiki_replace_markdown_block`, positional `wiki_*_at`) so pages mutate in place instead of full rewrites.
- **Auto-linking** — `wiki_autolink` inserts `[[slug]]` cross-references by matching page titles; validated so it never manufactures broken refs.
- **Sync helpers** — `wiki_refresh_stale`, `wiki_cleanup_merged_branches`, `wiki_coverage`, `wiki_lint`.
- **Code graph** (on by default, opt-out; successor to the retired repo-wiki generator) — `yadgar code-graph` shells out host-side to the [codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp) static binary (158-language tree-sitter, offline) to index the latest `origin/<default-branch>` and render a bounded architecture *digest* into an always-injected memory block (recall-free). `code_graph.enabled` defaults to `true` in the DB-backed runtime-config store (no row needed); install the host binary with `yadgar setup --code-graph` (interactive `[y/N]` when neither `--code-graph`/`--no-code-graph` is passed) or `CODE_GRAPH_ENABLED=1 yadgar setup`. Opt a single repo out with `config_set("code_graph.enabled", false, scope="project", directory=<repo>)`, or disable globally with `config_set("code_graph.enabled", false, scope="global")` — a per-dir override always wins over global. See ADR-0162/0163.
- **Bookmarks** — pin wiki pages in the viz UI (`bookmark_*`); drag-to-reorder, dense-integer positions.

### Unified recall
- One `recall()` query retrieves **memory + wiki together**, fused through WRRF, cross-encoder reranked, NLI- and MMR-filtered, and run through the rules engine — heat-weighted, decay-gated, branch-scoped. Filter by `type=` (`"memory"`, `"wiki"`, or both). `wiki_query` remains for wiki-only search.

### Harness task-list mirror
- `wiki_write_task_list(project, content, directory)` persists the Claude Code harness task list (TaskCreate/TaskList) to the wiki store so it survives `/clear` and session exit. The stop-hook checkpoint step writes it out; the SessionStart restore-nudge re-injects open tasks. Written canonical (branch-NULL) so it resolves from any branch. A dedicated MCP tool — not a raw `wiki_add` — because the canonical-write path is structurally bounded to the `{project}-task-list` slug.

### Architecture Decision Records (ADR)
- `adr_add(...)` writes a per-ADR wiki page (`yadgar-adr-NNNN`) enforcing an 11-field schema with auto-incrementing IDs. Each ADR gets its own page indexed in a thin `yadgar-adr-index`; `recall()` IS the read path (pages are recall-visible and immortal — no decay). `adr_get(directory, adr_id)` fetches a single ADR; `adr_list(directory, status=)` reads the index with optional status filter. A stop-hook prompt captures decisions at session end. Yadgar dogfoods its own system with 138 real ADRs.

### Agent-prompt library
- Reusable subagent dispatch prompts, stored as tagged wiki pages (`agent-prompt-<pattern>`), versioned and improving over time. `agent_prompt_save(pattern, content, purpose)` upserts one page per pattern; `agent_dispatch_prelude(pattern)` builds a prelude to prepend to a subagent prompt — recall-first contract + mandatory `## Yadgar findings` footer. `seed_agent_prompts()` seeds starter patterns. Lookup is via tagged recall (`recall(type="wiki", tags=["agent-prompt"])`), kept out of normal recall noise.

### Knowledge-graph viz (galaxy)
- `yadgar viz` serves an interactive 3D **galaxy layout** of memories, wiki pages, entities, and relationships at **http://localhost:42069**. The galaxy arranges nodes spatially: loose low-heat memories form the outer halo, recurring semantic clusters spiral as arms, and core/anchor nodes form a central bulge. Heat encodes brightness. The force-directed / 2D engine is removed (ADR-0138) — galaxy is the sole renderer.
- Features: draggable node popups, Fit/Reset galaxy camera, filter by node type, heat slider, hover-neighborhood highlight, search, **traces replay** (replay a tool trace from Tempo), and an **in-browser config panel** (System → Config).
- **Precomputed server-side layout** (unconditional): the nightly/full cycle computes 3D node positions, signature-caches them, and `/api/graph` serves the precomputed coordinates. On a seed miss, the client runs a cold layout.
- The UI is organized into four menus: **Graph** · **Bookmarks** · **System** {Config, Health, Stats} · **Help** {Guide, Config Reference, About, Debug}.

### Read-only DB inspection (`db_inspect`)
- `db_inspect(query, params, limit)` executes a SurrealQL SELECT against a read-only viewer client (`YADGAR_RO_PASS`), gated by `YADGAR_DEBUG_APIS_ENABLED`. No writes possible from this surface (ADR-0132). Forwarded by core to the backend `/api/debug/read_query` endpoint.

### Nightly consolidation (the "brain cycle")
Runs nightly while the daemon stays up in maintenance mode (no MCP reconnect). Phases:
`apply_decay → process_episodes → merge_duplicates → link_similar → detect_causality → memify → cls_consolidation` plus a dream/sleep phase.
- **CLS promotion** — recurring episodic patterns promote to semantic memory (Complementary Learning Systems).
- **Causal discovery** — PC-algorithm causal-DAG inference over co-occurring memories.
- **Dream replay** — associative `co_occurrence` linking + insight generation; insights cap at 21 days if unaccessed.
- **Community detection** — graph clustering surfaced as clusters in the viz.
- Re-embeds stale memories; precomputes the galaxy graph layout.

### Config system
- [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) with precedence **env vars (`YADGAR_*`) > `~/.config/yadgar/config.yaml` > defaults**.
- **System → Config** is an in-browser editor (part of the viz UI): category-grouped knobs, alpha-sorted, hover tooltips, `ⓘ` deep-links to a Config Reference page, and SOURCE badges distinguishing env-var / config-file / default origins. Writes are bearer-auth gated.

### Security & observability
- **Bearer-token MCP auth** on `/api/*`, `/hooks/*`, `/mcp` — default-deny CORS, timing-safe compare, loopback-only by default. `/health` and `/metrics` exempt on loopback.
- **Always-on secret gate** blocks AWS / GCP / Stripe / Slack / OpenAI / Anthropic keys, JWTs, GitHub PATs, private keys, and DB URIs before they reach the store (cannot be disabled; context-aware allowlist for known-good fixtures).
- **Auto-capture sanitization** strips ANSI escapes, control chars, and Unicode bidi-override before action-log insert.
- **Prometheus `/metrics`** + OpenTelemetry distributed tracing across core + backend (core→backend W3C traceparent joins one trace in Tempo); structured JSON logs; per-phase consolidation duration markers. **Tri-signal standard** (v5.101, ADR-0034): every in-scope function emits span+metric+log via the `@observe` decorator, ratcheted by the I33 coverage lint.
- **Async write queue** (file-queue, ADR-0075) with retry/backoff, dead-letter for permanent failures, and DLQ inspection tools (`dlq_inspect`, `dlq_requeue`, `dlq_dismiss`).

---

## Architecture

```
┌──────────────┐        MCP (streamable-HTTP / stdio)         ┌──────────────────────────────┐
│  Claude Code │ ◄────────────────────────────────────────── ►│  yadgar/core (:8765)          │
│   + hooks    │  memorize / recall / wiki_* / checkpoint …   │  MCP server (thin router)     │
└──────────────┘                                               │  auth · rules · hooks · viz  │
                                                               └────────────┬─────────────────┘
                                                                            │ HTTP + file queue
                                                               ┌────────────▼─────────────────┐
                                                               │  yadgar/_shared              │
                                                               │  config · storage contracts  │
                                                               │  observability · security    │
                                                               └────────────┬─────────────────┘
                                                                            │ HTTP
                                                               ┌────────────▼─────────────────┐
                                                               │  yadgar/backend               │
                                                               │  SurrealDB store (:8000)      │
                                                               │  embed + rerank (:8001)       │
                                                               │  retrieval pipeline (POST /recall) │
                                                               │  consolidation compute       │
                                                               └──────────────────────────────┘
```

Three physical layers (ADR-0056 / ADR-0060 / ADR-0062):
- **yadgar/core** — the MCP server (FastAPI). Routes tool calls; forwards recall to backend via `POST /recall`; hosts the viz web UI routing layer. No heavy compute lives here.
- **yadgar/_shared** — contracts, config (pydantic-settings), storage client, observability (`@observe`), security gate. No direct MCP tool code.
- **yadgar/backend** — SurrealDB (`:8000`) + embed/rerank service (`:8001`). Owns the full retrieval pipeline, consolidation compute, and the async write drainer. ML models are baked into the backend image (ADR-0101).

**Write path (ADR-0075):** MCP tool → core → file queue (`YADGAR_QUEUE_BASE=/data/queue`) → backend drainer → SurrealDB. Writes are async by default; `wait=True` blocks until drainer commits.

**Recall path (ADR-0044):** `recall()` in core is a thin forwarder → backend `POST /recall` runs the full pipeline (FTS + KNN + PPR + spreading activation → WRRF fusion → Ettin-32m CE rerank → NLI → MMR → adversarial → rules). Branch filter (`branch IN (current, default, NULL)`) and current-branch boost applied post-fetch.

Deeper detail: [docs/reference/architecture.md](docs/reference/architecture.md) · [docs/reference/retrieval.md](docs/reference/retrieval.md) · [docs/reference/memory-lifecycle.md](docs/reference/memory-lifecycle.md).

**Layer docs (in-tree):** each layer root carries a `README.md` (subsystem map) and an `AGENTS.md` (placement laws for coding agents) — [`yadgar/_shared/`](yadgar/_shared/README.md) · [`yadgar/backend/`](yadgar/backend/README.md) · [`yadgar/core/`](yadgar/core/README.md).

---

## Install

Python 3.14+ on the host (or use the Docker / Compose path for zero host Python). All paths reach the same `yadgar setup` post-install step.

**pipx (recommended for isolated install):**
```bash
pipx install yadgar
yadgar setup
```

**Nix flake:**
```bash
nix profile install github:m-agahi/yadgar
yadgar setup
```

**Plain pip:**
```bash
pip install yadgar
yadgar setup
```

**Repo checkout (development):**
```bash
git clone https://github.com/m-agahi/yadgar.git
cd yadgar
make setup            # install + hooks + agents + units + seed anchors
```

`yadgar setup` writes `~/.config/yadgar/config.yaml`, generates `~/.config/yadgar/secrets.env` (chmod 600) with random `YADGAR_MCP_AUTH_TOKEN` + `SURREAL_PASS` + `YADGAR_RW_PASS` + `YADGAR_RO_PASS`, installs Claude Code hooks + subagent templates + rules, seeds anchors, and prepares systemd (Linux) or launchd (macOS) user units. Idempotent — re-run after upgrades.

Then start the daemon and register it with Claude Code:

```bash
set -a && . ~/.config/yadgar/secrets.env && set +a
yadgar daemon start
yadgar daemon configure-mcp   # writes ~/.claude.json with Authorization: Bearer header
```

Restart Claude Code, then verify:

```bash
yadgar daemon status
yadgar stats
```

Per-platform detail: [`docs/reference/install.md`](docs/reference/install.md).

### Stdio-only (no daemon, no Docker)

Single-session use. Skip `yadgar setup`. Add to `~/.claude.json`:

```json
{ "mcpServers": { "yadgar": { "command": "yadgar", "args": [] } } }
```

Restart Claude Code. No bearer auth; embed / rerank degrade gracefully without the backend. Note: the installed daemon uses streamable-HTTP transport by default; stdio mode is for single-session / no-Docker use.

### Docker Compose (recommended for production)

Two containers — backend (SurrealDB + embed/rerank service) and core (MCP server). Independent version tracks.

```bash
# Source secrets first
set -a && . ~/.config/yadgar/secrets.env && set +a

docker compose up -d
```

The compose file (`docker-compose.yml`) defines:
- **`yadgar-backend`** — `openfantasy/yadgar-backend:5.55.0`, SurrealDB on `:8000` + embed/rerank on `:8001`. Mounts `yadgar-db-data` (read-only) + `yadgar-queue-data` (file queue, `YADGAR_QUEUE_BASE=/queue-data`).
- **`yadgar-core`** — `openfantasy/yadgar:5.149.0`, MCP server on `:8765`. Mounts `yadgar-queue-data` at `/data` (shared file queue). Depends on backend healthcheck before starting.

The knowledge-graph viz UI is **not** part of the compose service. Run `yadgar viz` separately on the host to launch the viz server at `http://localhost:42069` — it reverse-proxies `/api/*` to the daemon at `:8765`.

Then register with Claude Code:
```bash
yadgar daemon configure-mcp
```

> Note: for the manual `docker run` path or systemd-based production deploy, see [`docs/reference/install.md`](docs/reference/install.md). The compose file above is the recommended development/production path.

### Updating

```bash
yadgar update --check   # probes PyPI; prints upgrade command; exit 0
```

For `pipx` the suggested command is `pipx upgrade yadgar`; then re-run `yadgar setup` (idempotent) to refresh hooks and units. An opt-in update orchestrator (`yadgar update --install`, default off via `update.install_enabled: false`) coordinates snapshot → image pull → graceful drain → restart → health-check → CLI upgrade, with automatic rollback (`yadgar update --rollback`). See `MIGRATION_NOTES.md`.

---

## Common commands

### Setup / install
```bash
yadgar setup                                  # first-run: config, secrets, hooks, units
yadgar install --client claude-code --hooks    # (re-)wire Claude Code hooks + bearer token (replaces yadgar install-hooks)
yadgar install-subagents                      # install subagent templates
yadgar daemon configure-mcp                   # write ~/.claude.json with streamable-HTTP + bearer header
yadgar seed <directory>                       # bootstrap memory for an existing project
```

### Daily use
```bash
yadgar daemon start|stop|status|logs|health   # manage the background daemon
yadgar viz                                     # launch knowledge-graph UI at http://localhost:42069
yadgar stats [--project /path]                 # memory counts + health
yadgar daemon restart                          # restart after config change
```

### Maintenance / backup
```bash
yadgar vacuum                         # compact the SurrealKV store
yadgar export duckdb --output snap.duckdb    # analytics snapshot (requires yadgar[analytics])
yadgar config set <key> <value>       # change a config knob (also editable in viz System→Config)
yadgar config list                    # show all config knobs with current values
yadgar update --check                 # check for new version on PyPI
yadgar update --install               # orchestrated upgrade (snapshot → pull → restart → verify)
```

### Debugging
```bash
yadgar daemon logs                    # tail daemon container logs
yadgar daemon health                  # check /health endpoint
yadgar --version                      # core + backend + daemon versions
yadgar config get YADGAR_LOG_FORMAT   # inspect a single knob
# MCP-level: dlq_inspect() · audit_anchors() · check_invariants() · db_inspect("SELECT ...")
```

---

## CLI

```
yadgar                              # MCP server (streamable-HTTP default); --transport {sse,streamable-http}
yadgar daemon start|stop|logs|health|status|restart
yadgar daemon configure-mcp         # write ~/.claude.json with bearer header + streamable-HTTP transport
yadgar daemon install-service       # install systemd / launchd unit
yadgar setup                        # first-run config + secrets + MCP snippet
yadgar stats [--project /path]      # memory statistics
yadgar viz                          # knowledge graph at http://localhost:42069
yadgar vacuum                       # compact the SurrealKV store
yadgar seed <directory>             # bootstrap memory for an existing project
yadgar code-graph index|query|refresh <repo>  # host-side multi-lang code-structure (on by default; MCP config_set code_graph.enabled=false to opt out)
yadgar rules export|import          # policy rules
yadgar config init|list|get|set     # configuration
yadgar update --check|--install|--rollback
yadgar export duckdb --output snap.duckdb   # analytics snapshot (pip install yadgar[analytics])
yadgar install --client <name> [--hooks] [--scope ...]   # wire MCP + rules + hooks (default-on) per client; --hooks explicit when scope is set
yadgar install-subagents                # install subagent templates (claude-code only)
yadgar restore <directory>          # (hook-internal) restore context post-compaction
yadgar capture                      # (hook-internal) lightweight action capture
yadgar drain                        # (hook-internal) flush file queue
yadgar context                      # (hook-internal) session-start context inject
```

---

## MCP tools

Yadgar exposes **~79 MCP tools** (excluding test-only stubs). ⚡ marks `power=True` tools (gated in the minimal MCP profile). A categorized selection — full list via the running server's tool catalog:

<details><summary><b>Memory</b></summary>

| Tool | Power | Purpose |
|---|:---:|---|
| `memorize(content, context, tags)` | | Store a memory; auto-captures branch + surprise gate |
| `recall(query, type, max_results)` | | Unified branch-aware search over memory **and** wiki |
| `recent_memories()` | | Newest-first listing, no classifier dependency |
| `memory_get(id)` | | Fetch by numeric ID |
| `memory_update(id, fields)` | ⚡ | Patch `content` / `tags` / flags |
| `forget(id)` | | Mark for deletion |
| `anchor(content, context, reason)` | ⚡ | Protected memory; never decays |
| `checkpoint(directory, …)` | ⚡ | Snapshot pre-compaction |
| `restore(directory)` | ⚡ | Reconstruct post-compaction (hippocampal replay) |
| `memory_stats()` | | Health + counts |
| `validate_memory()` | ⚡ | Check validity against current file state |

</details>

<details><summary><b>Wiki</b> (core + editing + maintenance)</summary>

Core: `wiki_add` · `wiki_read` ⚡ · `wiki_query` · `wiki_list` ⚡ · `wiki_get` · `wiki_update` ⚡ · `wiki_delete` ⚡ · `wiki_history` · `wiki_diff` · `wiki_read_version` · `wiki_restore` ⚡ · `wiki_check_duplicate` · `wiki_lint` ⚡

Editing: `wiki_append_section` ⚡ · `wiki_replace_text` ⚡ · `wiki_delete_text` ⚡ · `wiki_insert_before/after` ⚡ · `wiki_replace_at` / `wiki_delete_at` / `wiki_insert_at` ⚡ · `wiki_replace_markdown_block` ⚡ · `wiki_autolink` ⚡ · `wiki_set_metadata` ⚡

Maintenance: `wiki_coverage` · `wiki_refresh_stale` ⚡ · `wiki_cleanup_merged_branches` ⚡

**Task-list mirror:** `wiki_write_task_list(project, content, directory)` — canonical write bounded to `{project}-task-list` slug; used by stop-hook checkpoint (step 4).

> `wiki_add` commits directly — there is no draft/approve workflow. The draft-workflow tools (`wiki_drafts`, `wiki_approve`, `wiki_discard`) were removed in v5.157.0 (Fix #76); no production path ever produced drafts.

</details>

<details><summary><b>Bookmarks &amp; Blocks</b></summary>

Bookmarks: `bookmark_add` · `bookmark_remove` · `bookmark_list` · `bookmark_reorder`

Blocks (Letta-style core memory, all ⚡): `block_create` · `block_get` · `block_update` · `block_delete` · `block_list` · `block_replace` · `block_append`

</details>

<details><summary><b>Project state</b></summary>

| Tool | Power | Purpose |
|---|:---:|---|
| `project_brief(directory, mode)` | | Layered bootstrap — `signals` (<100 tok), `restore` (~800 tok), `catalog` (~500 tok), `full` (~1050 tok) |
| `bootstrap_project(directory, content)` | ⚡ | Set `_project_init` |
| `update_active_work(directory, content)` | ⚡ | Atomic replace of `_active_work` |
| `seed_project(directory)` | ⚡ | Bootstrap memory from README + top-level docs |
| `install_hooks(project_directory, scope)` | ⚡ | Wire Claude Code hooks (Car 7: now delegates to `yadgar install --client claude-code --hooks`); inject bearer token |

`get_project_context()` is a deprecated alias of `project_brief(mode="catalog")`.

</details>

<details><summary><b>ADR system</b></summary>

| Tool | Power | Purpose |
|---|:---:|---|
| `adr_add(directory, title, context, decision, …)` | ⚡ | Append 11-field ADR to per-project index; writes `{project}-adr-NNNN` wiki page |
| `adr_get(directory, adr_id)` | ⚡ | Fetch a single ADR page by ID (e.g. `"ADR-0042"`) |
| `adr_list(directory, status=)` | ⚡ | Read the index; optional status filter (`"open"`, `"accepted"`, etc.) |

</details>

<details><summary><b>Agent-prompt library</b></summary>

| Tool | Power | Purpose |
|---|:---:|---|
| `agent_dispatch_prelude(pattern, task_topic)` | | Build a subagent prompt prelude from the saved prompt library |
| `agent_prompt_save(pattern, content, purpose)` | ⚡ | Upsert a reusable dispatch pattern page |
| `seed_agent_prompts()` | ⚡ | Seed starter patterns into the library |

Patterns are wiki pages tagged `["agent-prompt"]`; lookup: `recall(type="wiki", tags=["agent-prompt"])`.

</details>

<details><summary><b>DB inspection</b></summary>

| Tool | Power | Purpose |
|---|:---:|---|
| `db_inspect(query, params, limit)` | | Read-only SurrealQL SELECT; gated by `YADGAR_DEBUG_APIS_ENABLED` (ADR-0132) |

</details>

<details><summary><b>Consolidation &amp; ops</b></summary>

`consolidate_now()` ⚡ · `vacuum_now()` ⚡ · `vacuum_checkpoints()` ⚡ · `reembed_all()` ⚡ · `check_invariants(repair)` ⚡ · `sync_instructions()` ⚡ · `archive_purge()` ⚡ · `add_rule(...)` ⚡ · `get_rules(...)` ⚡ · `audit_anchors()` ⚡

</details>

<details><summary><b>Dead-letter queue</b></summary>

`dlq_inspect(filter)` · `dlq_requeue(id)` ⚡ · `dlq_dismiss(id)` ⚡

</details>

A typed **JavaScript / TypeScript SDK** wraps the tool surface as async methods (Node.js, Vercel Edge, Cloudflare Workers, Deno) — see [`docs/reference/sdk-js.md`](docs/reference/sdk-js.md).

---

## Configuration

```bash
yadgar config init        # write ~/.config/yadgar/config.yaml
yadgar config set retrieval_profile fast
```

Priority: env vars (`YADGAR_*`) > `~/.config/yadgar/config.yaml` > defaults. Knobs are also editable in-browser via **System → Config** in the viz UI. Key vars (full reference in [docs/reference/configuration.md](docs/reference/configuration.md)):

| Var | Default | Purpose |
|---|---|---|
| `YADGAR_REQUIRE_AUTH` | `1` | Bearer auth on `/api/*` `/hooks/*` `/mcp`. Set `0` only during initial rollout. |
| `YADGAR_MCP_AUTH_TOKEN` | (required) | Bearer token. `yadgar setup` generates one. |
| `YADGAR_DB_PASS` | (required) | SurrealDB password. No `root:root` fallback. |
| `YADGAR_HOST` | `127.0.0.1` | Bind interface. Loopback by default. |
| `YADGAR_ALLOWED_ORIGINS` | loopback | CORS allowlist. |
| `YADGAR_METRICS_ENABLED` | `1` | Expose Prometheus `/metrics` (loopback, unauthenticated). |
| `YADGAR_LOG_FORMAT` | `human` | Set `json` for structured logs. |
| `YADGAR_VIZ_MAX_MEMORIES` / `_WIKI` / `_ENTITIES` | `0` (unlimited) | Galaxy node caps (`0`/`-1` = unlimited). |
| `YADGAR_MODEL_IDLE_EVICTION_SECONDS` | `0` | Unload heavy ML models after idle seconds (`0` = stay loaded). |
| `GTE_RERANKER_MODEL` | `cross-encoder/ettin-reranker-32m-v1` | Primary CE reranker (Train 4; ADR-0104). Rollback: `Alibaba-NLP/gte-reranker-modernbert-base`. |
| `YADGAR_DEBUG_APIS_ENABLED` | `0` | Enable `/api/logs/*` + `db_inspect` surface. |

---

## Subagent integration

Yadgar ships a `SubagentStop` hook that captures memory findings from Claude Code subagents: when a subagent completes, its final report is scanned for a `## Yadgar findings` section and each bullet is persisted with `provenance_agent` set to the agent type.

To opt your subagents into the protocol, paste [`docs/reference/claude-subagent-contract.md`](docs/reference/claude-subagent-contract.md) into your `~/.claude/CLAUDE.md`, then run `yadgar install --client claude-code --hooks --scope global`. The contract is opt-in — Yadgar works without it.

The **agent-prompt library** (`agent_prompt_save` / `agent_dispatch_prelude`) gives subagent dispatch prompts a versioned home: save a good prompt once, improve it over time, and every dispatch pulls the latest version automatically. `agent_dispatch_prelude(pattern, task_topic)` builds a compliant prelude (recall-first contract + `## Yadgar findings` footer) from the stored pattern.

---

## Benchmark

Yadgar is evaluated on [**LongMemEval**](https://arxiv.org/abs/2410.10813) (ICLR 2025), the standard academic benchmark for long-term conversational memory. The `longmemeval_s` variant runs 500 questions across 6 categories against ~50 sessions of synthetic history per query, scored on **Phase 1 retrieval** (does the memory layer surface gold-context sessions in top-k?) and **Phase 2 QA accuracy** (does the reader produce the gold answer, judged by an LLM grader?).

**Headline (v5.26.0, full 500q, Sonnet 4.6 reader + judge — the most recent full run; subsequent versions targeted infrastructure, viz, wiki, and recall-unification rather than retrieval quality):**

| System | Reader | LongMemEval-s QA | vs yadgar |
|---|---|---|---|
| **yadgar v5.26.0** | **Sonnet 4.6** | **69.4%** (347/500) | — |
| mem0 V3 | GPT-4o | 94.4% | +25.0 pp |
| Zep / Graphiti | GPT-4o | 63.8% | **−5.6 pp** |

Yadgar beats Zep by 5.6 pp on the same 500-question sample. mem0 leads by 25 pp via LLM-extract-on-ingest. **Phase 1 retrieval:** MRR 0.928, Recall@10 0.906, NDCG@10 0.863 — the memory layer surfaces gold context for ~91% of queries, so the remaining QA gap is mostly reader synthesis, not retrieval.

Full methodology and per-type breakdown: [`docs/benchmark-results/BENCHMARK_RESULTS.md`](docs/benchmark-results/BENCHMARK_RESULTS.md).

### Recall speed

**Ettin-32m @ --cpus 3 (ADR-0106 standing config), warm steady-state p50 ~2.6s / mean ~2.3s (n=30, controlled 2026-07-15).**

The cross-encoder reranker swap (GTE-ModernBERT → Ettin-32m, v5.132.0/5.43.0) delivered a measured **2.44× end-to-end recall speedup** on equal hardware (same-image, same-CPU A/B, histogram-delta method per ADR-0098). The 3-CPU standing config (ADR-0106) adds parallel batch scoring via `gather_budget=2` (~24% further reduction vs 2-CPU). CE is ~25% of the cold recall wall (ADR-0105) — the dominant gains come from the model swap.

Controlled re-measurement 2026-07-15 (v5.143.0/5.50.0, n=30 per regime, CE-miss gate PASS): warm steady-state mean **2,332ms** / p50 **2,644ms** / p95 **3,064ms**; cold (post-restart) mean **2,594ms** / p50 **2,682ms** / p95 **3,148ms**.

Full consolidated latency history, measurement protocol, comparability caveats, and per-milestone verdicts: [`docs/benchmark-results/RECALL_SPEED.md`](docs/benchmark-results/RECALL_SPEED.md).

---

## Roadmap

### Shipped highlights (v5.78 → current)
- **Unified recall** (v5.78–v5.81) — memory + wiki merged into one ranked, heat-weighted, branch-scoped result; now the default.
- **ADR tooling** (`adr_add` / `adr_get` / `adr_list`, v5.85+) + capture-first Stop-hook prompt. Per-ADR wiki pages with thin index; `recall()` is the read path. 138 real ADRs dogfooded.
- **Agent-prompt library** (v5.85, ADR-0007) — wiki-backed, tagged-recall lookup; `agent_dispatch_prelude` builds compliant subagent preludes.
- **Harness task-list mirror** — `wiki_write_task_list` (stop-hook out) + session-start restore-nudge (in); persists the Claude Code task list across `/clear`.
- **Galaxy viz** (post-#52, ADR-0134/0138) — galaxy is the sole renderer (force-directed/2D engine removed); galaxy layout (loose/core/arms), traces replay, in-browser config panel.
- **Read-only DB inspection** (`db_inspect`, ADR-0132) — gated SurrealQL SELECT surface.
- **Wiki autolink + repo-wiki store-bridge** (v5.85).
- **Viz overhaul + precomputed server-side layout + System → Config editor** (v5.86–v5.88).
- **Core/_shared/backend layer split** (ADR-0056/0060/0062/0063) — import-linter-enforced three-layer architecture; forward-only recall (ADR-0044) with full pipeline in backend.
- **Recall perf + accounting** (v5.97–v5.104) — WRRF N+1 batches, spreading-activation N+1 batched; CE is ~25% of cold recall wall (ADR-0105 corrected).
- **Ettin-32m CE reranker** (Train 4, ADR-0104) — 2.44× end-to-end recall speedup; 3-CPU standing config (ADR-0106).
- **Tri-signal observability standard** (v5.100–v5.101, ADR-0034) — span+metric+log per function, I33 coverage lint, core→backend W3C traceparent, OTLP → Tempo.
- **Test-speed train** (v5.104, ADR-0036) — CI shards ~2× faster.
- **File-queue write path** (ADR-0075) — async write queue on shared volume; both containers share `yadgar-queue-data`.
- **Deps modernization** (ADR-0100) — transformers 5.x; optimum-onnx removed.
- **Module standardization** (ADR-0128/0130) — major subsystems promoted to package dirs; import-linter enforced; perf-neutral (ADR-0129).

### Open horizon
- **v6 — Nightly LLM curator.** A local agent (Ollama; two-tier deepseek-r1 + qwen3:8b) runs each night to detect staleness, annotate contradictions, find semantic correlations beyond co-occurrence, propose merges/forgets, and dedupe wiki pages.
- **v7 — Real-time synthesis.** `recall(synthesize=True)` / `ask()` tool; depends on a sub-10 s local synthesis model.
- **Open work:** improvement-train A1+C4 · full-observability per-area rollout (#I33) · ci-velocity remaining (#83/#79) · cpu-burst Part 2 · task-routing fix · fusion tiebreak determinism · obs velocity completion.

Full history: [CHANGELOG.md](docs/CHANGELOG.md).

---

## Documentation

- [AGENTS.md](AGENTS.md) — operational guide for AI coding agents (setup, dev env, tests, code style, PR rules)
- [Architecture](docs/reference/architecture.md) — component map, retrieval, security, observability
- [Memory lifecycle](docs/reference/memory-lifecycle.md) — heat, archiving, pruning, consolidation phases
- [Retrieval](docs/reference/retrieval.md) — fusion, rerank, branch filter, pipeline stages
- [Configuration](docs/reference/configuration.md) — configuration reference (env vars, precedence, key knobs)
- [Install](docs/reference/install.md) — per-platform setup + the `yadgar setup --doctor` probe
- [JS/TS SDK](docs/reference/sdk-js.md) — typed client for the MCP tool surface
- [Release runbook](docs/reference/release.md) · [Migration notes](MIGRATION_NOTES.md) · [Subagent contract](docs/reference/claude-subagent-contract.md)

---

## Contributing

Every change to `yadgar/**` must update `README.md` and `docs/` in the same PR. Conventional Commits format. No `Co-Authored-By:` trailers. AI coding agents working on this repo: read [`AGENTS.md`](AGENTS.md) for the full dev / test / PR contract.

## Related projects

- **[ccpm](https://codeberg.org/maxagahi/ccpm)** — Claude Code Plugin Marketplace. Ships `code-review`, `confluence-rfc`, `git-flow`, `repo-wiki`, `tf-naming-check`, and `update-jira` plugins that compose with yadgar.

## Tribute

Inspired by [Zikkaron](https://github.com/amanhij/Zikkaron) by [@amanhij](https://github.com/amanhij). Different architecture, same north star.

Yadgar stands on a great deal of open-source work — models, databases, frameworks, and tools. Full credits and acknowledgments (including [Tom Aarsen](https://github.com/tomaarsen), whose sentence-transformers + Ettin reranker underpin our retrieval): **[docs/reference/tributes.md](docs/reference/tributes.md)**.

## License

Apache 2.0. See [LICENSE](LICENSE).

### Third-party licenses (key dependencies)

- **SurrealDB** (default storage backend) — [Business Source License 1.1](https://github.com/surrealdb/surrealdb/blob/main/LICENSE) (BSL). The Additional Use Grant permits embedded use; yadgar bundles + operates SurrealDB as a single-tenant per-deployment store, which falls under that grant. **Non-compliant trigger:** offering a hosted multi-tenant managed yadgar service exposing the SurrealDB API directly to third-party customers — get a commercial SurrealDB license or migrate to Postgres + pgvector if that becomes a goal.
- **`surrealdb` Python SDK** — Apache 2.0 (separately licensed from the server).
- **embed / rerank models** — `sentence-transformers/all-MiniLM-L6-v2`, `cross-encoder/ettin-reranker-32m-v1` (CE primary), `cross-encoder/ettin-reranker-68m-v1` (fallback), `Alibaba-NLP/gte-reranker-modernbert-base` (rollback), `cross-encoder/ms-marco-MiniLM-L-6-v2`, `cross-encoder/nli-deberta-v3-small`: Apache 2.0 weights via Hugging Face.
- **Benchmarks** (`benchmarks/`): scripts Apache 2.0, but datasets carry their own licenses — **LoCoMo is CC BY-NC 4.0 (non-commercial only)**; LongMemEval MIT. See `benchmarks/README.md`.

Full per-dependency audit: `docs/reports/audits/license-compliance-audit-2026-05-30.md`.
