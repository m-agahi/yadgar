<p align="center">
  <img src="yadgar/static/yadgar.svg" alt="yadgar logo" width="200">
</p>

<div align="center">

# Yadgar

[![CI](https://codeberg.org/maxagahi/yadgar/actions/workflows/ci.yaml/badge.svg?branch=master)](https://codeberg.org/maxagahi/yadgar/actions?workflow=ci.yaml)
[![Version](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fcodeberg.org%2Fapi%2Fv1%2Frepos%2Fmaxagahi%2Fyadgar%2Ftags&query=%24%5B0%5D.name&label=version&color=blue)](https://codeberg.org/maxagahi/yadgar/tags)
[![Python](https://img.shields.io/badge/python-3.14%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)

</div>

[Changelog](docs/CHANGELOG.md) · [Roadmap](#roadmap) · [Architecture](docs/architecture.md)

*Yadgar* (یادگار) is Persian for "memento, keepsake." It's a persistent memory engine for Claude Code: tell it what matters, and it survives across sessions — decaying what you stop touching, promoting what repeats, filtering recall to the git branch you're on, and pairing every memory with a curated wiki that searches through the same pipeline.

## Features

- **Persistent memory** — survives Claude sessions; heat-based decay drops unused items, surprise gating drops duplicates on arrival.
- **Branch-aware recall** — 1.5× boost on current-branch matches; wiki pages resolve in branch precedence so canonical content stays reachable from feature work.
- **Curated wiki paired with memory** — one ranking pipeline searches both stores in a single query.
- **Wiki Bookmarks** — pinned wiki pages in the viz UI (`/static/bookmarks.html`); 4 MCP tools (`bookmark_add`, `bookmark_remove`, `bookmark_list`, `bookmark_reorder`); drag-to-reorder, keyboard nav, markdown rendering with syntax highlighting.
- **Write-time contradiction detection** — lightweight heuristic (negation + action-divergence) fires on every write; contradicting memories flagged and confidence-decayed without blocking the write.
- **Scope-aware anchor surfacing** — `restore()` serves a two-bucket blend (global + project anchors) so high-anchor projects don't crowd out cross-cutting knowledge.
- **Cross-project anchor dedup** — `audit_anchors()` surfaces cosine ≥ 0.95 pairs across different `directory_context` values; PD-23 `migration_grace` expiry handler ships alongside.
- **Wiki sync with repo** — drift detection, master-only regen dispatch, cleanup of merged-branch wikis; pairs with the `repo-wiki` skill shipped via [ccpm](https://codeberg.org/maxagahi/ccpm).
- **Nightly consolidation** — heat decay, episodic→semantic promotion via Complementary Learning Systems, dream insights, causal-DAG discovery, duplicate merging.
- **Hippocampal-replay session bootstrap** — top anchors, hot memories, and active-work pointer auto-injected when Claude opens a session.
- **Auto-capture hook** — records tool usage with per-source token-bucket rate limit and sanitization (ANSI, control chars, bidi-override stripped).
- **db-lockdown 2026 hook schema** — PreToolUse hook ships as `yadgar/hooks/db-lockdown-check.py` (installed globally by `install_hooks`); emits `hookEventName` field required by Claude Code 2026 schema.
- **Stop-hook signal-eval prompt** — fires every 25 messages, asking the running session to evaluate stale-wiki and active-work signals.
- **Async write queue** — retry/backoff, dead-letter for permanent failures, schema-version validation on drain, DLQ inspection tools.
- **Bearer-token MCP auth** — default-deny CORS, timing-safe token compare, always-on secret patterns blocking AWS/GCP/Stripe/Slack/OpenAI/Anthropic keys, JWT, PATs, private keys, DB URIs. Context-aware allowlist (`~/.yadgar/secret-gate-allowlist.yaml`) for known-good fixtures.
- **Knowledge-graph viz** — `yadgar viz` serves a Three.js graph of memories, entities, and relationships at `http://localhost:42069` with live filtering by tag, age, and store type. All 35 viz constants configurable via `config.yaml` without redeploy.
- **Prometheus `/metrics`** — structured JSON logs, per-phase consolidation duration markers, CRITICAL alert on phase exceeding `PHASE_DURATION_WARN_MS`, loopback-only by default.
- **Benchmark infra** — Phase 1 LongMemEval retrieval harness with reproducibility metadata (commit SHA, dataset SHA256, model names); Phase 2 QA + publication in v5.26.0.
- **Idempotent transactional migrations** — backfills safe to re-run, failures roll back cleanly.

v5 ships bearer-token auth, branch-tagged retrieval, layered session bootstrap, write-time contradiction detection, scope-aware anchor surfacing, cross-project anchor dedup, Wiki Bookmarks, db-lockdown 2026 hook schema, and benchmark Phase 1 infra. See [Roadmap](#roadmap) for v6 (nightly LLM curator) and v7 (real-time synthesis).

---

## Yadgar in production

Personal deployment — first commit 2026-04-20, ~41 days runtime as of 2026-05-31.

### Scale

- **~2.7k memories** curated (episodic + semantic + archived)
- **~1.9k wiki pages** crystallized from working sessions
- **~1.1k knowledge graph entities** with auto-discovered relationships
- **~2.5k tool invocations** logged and replayable
- **~80 MB persistent state** post-vacuum (surrealkv vlog reclaim: `vacuum_now()` recovered 91% of 962 MB on most recent run)

### Latency

*p50 = half of calls finish at or below this. p95 = 95th percentile (only 5% are slower). Measured from distributed tracing spans on real traffic.*

- Hook-driven recall: **p50 ~2.3s, p95 ~2.7s** (down 20× from 4.7s/53s pre-v5.6.6 fix)
- Vector search: **p50 21ms, p95 58ms**
- Embedding RPC: **p50 2ms, p95 50ms**

### Continuity

- Checkpoint + `restore(directory="…")` to resume after `/clear` or `/compact`
- Cross-session anchors surface critical decisions automatically
- Wiki retrieval returns curated knowledge from prior sessions on the same domain
- Continuous nightly consolidation: dream replay, CLS episodic→semantic promotion, causal discovery

### Observability

- Distributed tracing (OpenTelemetry) across both core + backend processes
- Every memory operation produces a span with W3C `traceparent` propagation
- 17 rapid releases (v5.4.2 → v5.6.6) shipped in 24 hours with zero data loss; 17 further versions (v5.10.4 → v5.25.0) shipped 2026-05-30 to 2026-05-31

---

## For AI agents setting up Yadgar

> Canonical setup spec. Read this section; skip the rest of the README.

**Pick a mode:**

| Mode | Auth | When |
|---|---|---|
| `stdio` | none | Single Claude session. No daemon. |
| `http` | bearer | Multi-session. Docker required (daemon runs SurrealDB + embed service in containers). |
| `docker-manual` | bearer | Operator-managed two-container deploy without `yadgar daemon`. |

**Mode: stdio — minimum viable.**

Prereq: `pip install yadgar` (Python 3.14+ on host).

Add to `~/.claude.json`:

```json
{
  "mcpServers": {
    "yadgar": {
      "command": "yadgar",
      "args": []
    }
  }
}
```

Restart Claude Code. Done.

**Mode: http (recommended) — three commands.**

Prereq: Docker installed. Python 3.14+ only needed for `pip install yadgar` (no host Python at all if using `docker-manual` below).

```bash
pip install yadgar
yadgar setup        # generates ~/.yadgar/secrets.env (chmod 600) with random token + DB pass
set -a && . ~/.yadgar/secrets.env && set +a
yadgar daemon start
yadgar daemon configure-mcp   # writes ~/.claude.json with Authorization: Bearer header
```

Restart Claude Code. `configure-mcp` reads `$YADGAR_MCP_AUTH_TOKEN` from env at the time it runs; if unset, the header is omitted and `/mcp` returns 401.

**Mode: docker-manual** — see [Docker](#docker) below.

**Required env vars (http + docker modes):**

| Var | Required when | Source |
|---|---|---|
| `YADGAR_MCP_AUTH_TOKEN` | `YADGAR_REQUIRE_AUTH=1` (v5 default) | `~/.yadgar/secrets.env` via `yadgar setup` |
| `SURREAL_USER` / `SURREAL_PASS` | backend container starts | Same |
| `YADGAR_DB_URL` / `YADGAR_EMBED_URL` | core container starts | Defaults in `yadgar daemon`; explicit in `docker-manual` |

**Verify the install:**

```bash
yadgar daemon status        # http mode
yadgar stats                # any mode — prints memory counts
```

---

## Install

```bash
pip install yadgar
```

Needs Python 3.14+ on the host. For zero host Python see [Docker](#docker).

## Quick setup

`yadgar setup` does three things:

1. Checks Docker (warns if missing — `stdio` mode still works without it).
2. Writes `~/.yadgar/config.yaml` with defaults if absent.
3. Generates random `YADGAR_MCP_AUTH_TOKEN` + `SURREAL_PASS` + `YADGAR_RW_PASS` + `YADGAR_RO_PASS` into `~/.yadgar/secrets.env` (chmod 600) if the file doesn't already exist.

Then source the env file and start the daemon:

```bash
set -a && . ~/.yadgar/secrets.env && set +a
yadgar daemon start
yadgar daemon configure-mcp
```

## Docker

Two containers. Backend = SurrealDB + embed service; core = MCP server.

```bash
docker network create yadgar-net

docker run -d --name yadgar-backend --network yadgar-net \
  -v yadgar-db-data:/data \
  -e SURREAL_USER=$SURREAL_USER \
  -e SURREAL_PASS=$SURREAL_PASS \
  openfantasy/yadgar-backend:5.4.0

docker run -d --name yadgar --network yadgar-net \
  -v yadgar-data:/data \
  -p 127.0.0.1:8765:8765 \
  -e YADGAR_DB_URL=http://yadgar-backend:8000 \
  -e YADGAR_EMBED_URL=http://yadgar-backend:8001 \
  -e YADGAR_MCP_AUTH_TOKEN=$YADGAR_MCP_AUTH_TOKEN \
  openfantasy/yadgar:5.25.0
```

Containers bundle Python 3.14 — no host Python required.

<details><summary><b>Auto-start on login (systemd user units)</b></summary>

```bash
sudo mkdir -p /etc/yadgar
sudo cp ~/.yadgar/secrets.env /etc/yadgar/secrets.env   # or generate fresh
sudo chmod 600 /etc/yadgar/secrets.env

yadgar daemon install-service
systemctl --user enable --now yadgar-db.service yadgar.service
```

Generated units include `EnvironmentFile=/etc/yadgar/secrets.env`. See [MIGRATION_NOTES.md](MIGRATION_NOTES.md).

</details>

## Tools

⚡ = `power=True` (gated in minimal MCP profile).

<details><summary><b>Memory</b> — 10 tools</summary>

| Tool | Power | Purpose |
|---|:---:|---|
| `memorize(content, context, tags)` | | Store memory; auto-captures branch + surprise gate |
| `recall(query, max_results)` | | Branch-aware semantic + keyword + graph search |
| `memory_get(id)` | | Fetch by integer ID; strips embedding bytes |
| `memory_update(id, fields)` | ⚡ | Patch `content` / `tags` / `is_protected` / `is_stale` |
| `forget(id)` | ⚡ | Hard delete |
| `anchor(content, context, reason)` | ⚡ | Protected memory; never decays |
| `checkpoint(directory, ...)` | ⚡ | Snapshot pre-compaction |
| `restore(directory)` | | Reconstruct post-compaction |
| `memory_stats()` | | Health + counts |
| `check_invariants(repair)` | ⚡ | Validate + auto-repair schema |

</details>

<details><summary><b>Wiki</b> — 11 tools</summary>

| Tool | Power | Purpose |
|---|:---:|---|
| `wiki_add(slug, title, content, tags)` | ⚡ | Create draft |
| `wiki_query(query, tags)` | | Search pages |
| `wiki_read(slug)` | | Resolve: current branch → default → unscoped |
| `wiki_get(id)` | | Fetch by integer ID |
| `wiki_update(id, fields)` | ⚡ | Patch `content` / `tags` / `category` / `confidence` |
| `wiki_approve(slug)` | ⚡ | Promote draft |
| `wiki_discard(slug)` | ⚡ | Drop draft |
| `wiki_list(category, slug_prefix, limit)` | | Paginated listing |
| `wiki_lint(slug)` | ⚡ | Validate structure |
| `wiki_refresh_stale(directory)` | ⚡ | Dispatch regen of stale wikis (master-only) |
| `wiki_cleanup_merged_branches(directory, dry_run)` | ⚡ | Remove wikis on merged branches |

</details>

<details><summary><b>Bookmarks</b> — 4 tools (v5.23.0)</summary>

| Tool | Power | Purpose |
|---|:---:|---|
| `bookmark_add(slug, label_override)` | ⚡ | Pin a wiki page; idempotent upsert |
| `bookmark_remove(slug)` | ⚡ | Unpin |
| `bookmark_list()` | | List pinned pages in position order |
| `bookmark_reorder(slug, new_position)` | ⚡ | Move pin; dense integer shift |

</details>

<details><summary><b>Project state</b> — 3 tools</summary>

| Tool | Power | Purpose |
|---|:---:|---|
| `project_brief(directory, mode)` | | Catalog (~500 tok) or full (~1050 tok) bootstrap |
| `bootstrap_project(directory, content)` | ⚡ | Set `_project_init` (2000-char cap) |
| `update_active_work(directory, content)` | ⚡ | Atomic replace of `_active_work` |

</details>

<details><summary><b>Ops</b> — 8 tools</summary>

| Tool | Power | Purpose |
|---|:---:|---|
| `install_hooks(scope)` | ⚡ | Wire Claude Code hooks; inject bearer token |
| `sync_instructions()` | ⚡ | Refresh CLAUDE.md from rules engine |
| `vacuum_now(force)` | ⚡ | SurrealKV compaction |
| `add_rule(rule_type, scope, condition, action)` | ⚡ | Add retrieval / write policy rule |
| `get_rules(directory, rule_type)` | | List rules |
| `consolidate_now()` | ⚡ | Force consolidation cycle |
| `seed_project(directory)` | ⚡ | Bootstrap from README + top-level docs |
| `remember(thought)` | | Quick thought → action stream |

</details>

`get_project_context()` is a deprecated alias of `project_brief(mode="catalog")`.

## Configuration

```bash
yadgar config init        # write ~/.yadgar/config.yaml
yadgar config set retrieval_profile fast
```

Priority: env vars (`YADGAR_*`) > `~/.yadgar/config.yaml` > defaults.

Key v5 vars (full reference in [docs/configuration.md](docs/configuration.md)):

| Var | Default | Purpose |
|---|---|---|
| `YADGAR_REQUIRE_AUTH` | `1` | Bearer auth on `/api/*` `/hooks/*` `/mcp`. Set `0` only during initial rollout. |
| `YADGAR_MCP_AUTH_TOKEN` | (required) | Bearer token. `yadgar setup` generates one. |
| `YADGAR_DB_PASS` | (required) | SurrealDB password. No `root:root` fallback. |
| `YADGAR_HOST` | `127.0.0.1` | Bind interface. Loopback by default. |
| `YADGAR_ALLOWED_ORIGINS` | loopback | CORS allowlist. |
| `YADGAR_METRICS_ENABLED` | `1` | Expose Prometheus `/metrics` (loopback, unauthenticated). |
| `YADGAR_LOG_FORMAT` | `human` | Set `json` for structured logs. |
| `YADGAR_MODEL_IDLE_EVICTION_SECONDS` | `0` | See below. |

### Vacuum trigger precedence (`VACUUM_AUTO_*`)

From v5.7.0, three trigger paths exist and fire in the following order of priority:

| Priority | Trigger | Knob |
|---|---|---|
| 1 — primary | Nightly cron at 19:00 UTC (`yadgar-vacuum.timer`, ships in PR-1a/1b). Runs unconditionally. | — (always on) |
| 2 — backstop | Threshold path in `ConsolidationScheduler._maybe_auto_vacuum()`. Fires only when DB size exceeds `VACUUM_AUTO_THRESHOLD_BYTES` and local time is within [`VACUUM_AUTO_WINDOW_START`, `VACUUM_AUTO_WINDOW_END`). Role: catch runaway growth between cron cycles. | `YADGAR_VACUUM_AUTO_ENABLED`, `YADGAR_VACUUM_AUTO_THRESHOLD_BYTES` |
| 3 — manual | `vacuum_now()` MCP tool. Writes a trigger file; host systemd path-watch unit starts `yadgar-vacuum.service`. | `YADGAR_VACUUM_TRIGGER_PATH` |

`VACUUM_AUTO_THRESHOLD_BYTES` (default 2 GiB) is an **emergency backstop**, not the primary vacuum driver. The per-day cooldown in `_maybe_auto_vacuum()` prevents double-fires when both cron and the threshold would fire on the same day.

### Model idle eviction (`YADGAR_MODEL_IDLE_EVICTION_SECONDS`)

Controls whether heavy ML models (CE / NLI / pair) are unloaded from RAM after a period of inactivity.

| Value | Behaviour | Recommended for |
|---|---|---|
| `0` (default) | Models stay loaded for the container lifetime. No eviction. | 16+ GB hosts, production backends with steady traffic |
| Positive integer (e.g. `300`) | Evict heavy models after that many idle seconds. Frees ~500 MB RSS. | ≤ 8 GB memory-tight hosts |

**Trade-off:** eviction saves RAM but the next request after a cold unload pays a reload latency of roughly 1–3 s for `cross-encoder/nli-deberta-v3-small` on CPU. The reload also produces a brief CPU spike that is now attributed via the `yadgar_embed_model_load_duration_seconds{model}` histogram and a `model.load` OTel span — operators can correlate the spike to the reload event rather than treating it as unexplained noise.

> **Tip:** `HEAVY_RERANK_ENABLED=false` already disables NLI on the hook hot-path. Eviction is the additional RAM-management knob for hosts that cannot afford 500 MB resident after the first request.

Eviction telemetry (v5.6.7 PR-G):
- `yadgar_embed_model_unload_total{model}` — counter, increments once per idle eviction.
- `yadgar_embed_model_load_duration_seconds{model}` — histogram, wall-clock duration of each cold model load.
- `model.unload` OTel span — emitted on each eviction call that actually unloads handles.
- `model.load` OTel span with `cold_load=true` — emitted on each first-construction of a model handle.

## CLI

```
yadgar                              # MCP server (stdio)
yadgar --transport streamable-http --port 8765
yadgar daemon start|stop|restart|status
yadgar daemon configure-mcp         # write ~/.claude.json with bearer header
yadgar daemon install-service       # systemd user units + EnvironmentFile
yadgar setup                        # generate ~/.yadgar/secrets.env + config.yaml
yadgar stats [--project /path]
yadgar vacuum
yadgar seed <directory>
yadgar viz                          # graph at http://localhost:42069
yadgar rules add|export|import
yadgar config init|list|get|set|edit
```

## Architecture

<details><summary><b>Memory lifecycle</b></summary>

Memories carry a `heat` value in [0, 1] decaying exponentially over time. Access boosts heat; lack of access decays it. Daily 18:30 UTC consolidation runs phases: `apply_decay → process_episodes → merge_duplicates → link_similar → detect_causality → memify → cls_consolidation`. Recurring episodic patterns promote to semantic via Complementary Learning Systems. Dream insights cap at 21 days; auto-abstracted memories at 30 days if unaccessed.

Full pipeline: [docs/memory-lifecycle.md](docs/memory-lifecycle.md).

</details>

<details><summary><b>Retrieval pipeline</b></summary>

`recall()` orchestrates eight pipeline stages: FTS + KNN vector + PPR + spreading + temporal → WRRF fusion → cross-encoder rerank → NLI → MMR diversity → adversarial detection → rules engine.

Branch filter applies `branch IN (current, default, NULL)` post-fetch; current-branch matches get a 1.5× score boost. `wiki_read(slug)` resolves current → default → unscoped in that order.

Behavior pinned by characterization tests so refactors can't drift. Full spec: [docs/retrieval.md](docs/retrieval.md).

</details>

<details><summary><b>Security</b></summary>

Bearer-token middleware on `/api/*` `/hooks/*` `/mcp`. `/health` and `/metrics` exempt on loopback. Timing-safe compare via `hmac.compare_digest`. Default-deny CORS.

`install_hooks` ships as a real Python script with `shlex.quote`'d path — no shell-injection vector. Auto-capture sanitizer strips ANSI escapes, control chars, and Unicode bidi-override before action-log insert.

Always-on secret patterns block AWS, GCP, Stripe, Slack, OpenAI, Anthropic keys, JWT, GitHub PATs, private keys, DB URIs. Cannot be disabled.

Full notes: [docs/architecture.md](docs/architecture.md).

</details>

## Subagent Integration

Yadgar v5.3.0+ ships a `SubagentStop` hook that automatically captures memory
findings from Claude Code subagents. When a subagent completes, its final report
is scanned for a `## Yadgar findings` section; each bullet is persisted as a memory
with `provenance_agent` set to the agent type.

To opt your subagents into the Yadgar protocol, paste the contents of
[`docs/CLAUDE_SUBAGENT_CONTRACT.md`](docs/CLAUDE_SUBAGENT_CONTRACT.md) into your
`~/.claude/CLAUDE.md`. Then run:

```sh
yadgar install-hooks --scope global
```

This registers the `SubagentStop` hook in `~/.claude/settings.json` (append-if-absent
— existing hooks are preserved). The contract is opt-in: Yadgar works without it,
subagents just won't auto-write their findings.

## Documentation

- [Architecture](docs/architecture.md) — component map, branch-aware retrieval, security, observability
- [Memory lifecycle](docs/memory-lifecycle.md) — heat, archiving, pruning, project-state memories
- [Retrieval](docs/retrieval.md) — fusion, rerank, branch filter, pipeline stages
- [Configuration](docs/configuration.md) — every setting
- [Release runbook](docs/RELEASE.md) — version bump → tag → nix
- [Migration notes](MIGRATION_NOTES.md) — operator steps for breaking changes
- [Claude workflow](docs/claude-workflow.md) — long-lived feature-branch workflow
- [Subagent contract](docs/CLAUDE_SUBAGENT_CONTRACT.md) — paste snippet + SubagentStop hook setup

## Roadmap

- **v6 — Nightly LLM curator.** A local agent (Ollama, deepseek-r1 + qwen3:8b two-tier routing) runs every night to detect staleness, annotate contradictions, find semantic correlations beyond co-occurrence, propose merges and forgets, and dedupe wiki pages. Two-phase consolidation: tier 1 (existing) plus tier 2 (LLM, skips if Ollama offline). Plan: [docs/roadmap/v6.md](docs/roadmap/v6.md).
- **v7 — Real-time synthesis.** `recall(synthesize=True)` and `wiki_query(synthesize=True)` append a synthesized answer alongside raw records. New `ask()` tool returns synthesis-only output for conversational callers. Depends on a sub-10s local synthesis model. Plan: [docs/roadmap/v7.md](docs/roadmap/v7.md).
- **v5.x pipeline.** v5.26.0 benchmark QA + publication (LongMemEval results); v5.27.0 DuckDB analytics export; v5.29.0 bi-temporal edges; v5.31.0 recall pipeline plugin architecture (A/B-testable stages); v5.33.0 in-context memory blocks; v5.35.0 JS/TS SDK. See `docs/roadmap/` for full plans.

## Contributing

Every change to `yadgar/**` must update `README.md` and `docs/` in the same PR. Conventional Commits format. No `Co-Authored-By:` trailers.

## Related projects

- **[ccpm](https://codeberg.org/maxagahi/ccpm)** — Claude Code Plugin Marketplace. Ships `code-review`, `confluence-rfc`, `git-flow`, `repo-wiki`, `tf-naming-check`, and `update-jira` plugins that compose with yadgar.

## Tribute

Inspired by [Zikkaron](https://github.com/amanhij/Zikkaron) by [@amanhij](https://github.com/amanhij). Different architecture, same north star.

## License

Apache 2.0. See [LICENSE](LICENSE).

### Third-party licenses (key dependencies)

- **SurrealDB** (default storage backend) — [Business Source License 1.1](https://github.com/surrealdb/surrealdb/blob/main/LICENSE) (BSL). The Additional Use Grant explicitly permits embedded use; yadgar bundles + operates SurrealDB as a single-tenant store per-deployment, which falls under that grant. **Non-compliant trigger:** offering a hosted multi-tenant managed yadgar service that exposes the SurrealDB API directly to third-party customers. If that ever becomes a goal, get a commercial SurrealDB license OR migrate to Postgres+pgvector (see `docs/competitor-audit-2026-05-30.md`).
- **`surrealdb` Python SDK** — Apache 2.0 (separately licensed from the server).
- **embed/rerank models** — `sentence-transformers/all-MiniLM-L6-v2`, `cross-encoder/ms-marco-MiniLM-L-6-v2`, `cross-encoder/nli-deberta-v3-small`: Apache 2.0 model weights via Hugging Face.
- **Benchmarks** (`benchmarks/`): scripts Apache 2.0 (yadgar code), but the datasets themselves carry their own licenses — **LoCoMo is CC BY-NC 4.0 (non-commercial only)**; LongMemEval MIT. See `benchmarks/README.md` for full citations + commercial-use guidance.

Full per-dependency audit: `docs/LICENSE_COMPLIANCE_AUDIT_2026-05-30.md`.
