# Yadgar v4 Plan

## Points

### 1. Two-container split

Split into two containers:
- **Backend container**: SurrealDB + heavy compute (embedding models, consolidation, etc.) — changes infrequently, large image, slow to rebuild
- **Core container**: Yadgar MCP server, APIs, viz, and all application logic — changes frequently, lightweight image, fast to rebuild/redeploy

Goal: faster iteration on the frequently-changing parts without rebuilding the heavy image. Boundary defined in point 2.

### 2. Define the container boundary (embedding model placement)

**Decision: Option A** — embedding model lives in the **backend** container.

- Backend container: SurrealDB + sentence-transformers model + `/embed` HTTP endpoint
- Core container: calls `/embed` for all embedding needs; no heavy model; starts in ~2-3s
- This is the primary motivation for the split — the model load (~30-90s on warm cache, longer cold) is what makes the current single container slow to restart; isolating it means the core can restart instantly
- Consolidation logic lives in **core** — it orchestrates, calls `/embed`, writes to DB

### 3. DB container lockdown

- **Network isolation**: DB container only accepts connections from the core container (private Docker network, no host port exposure for SurrealDB)
- **No shell / exec access**: harden the DB container so `docker exec` into it cannot run `surreal` CLI commands against the live DB — prevent rogue Claude Code instances from destroying data during testing/debugging
- **Primary control — Claude Code hook**: a `PreToolUse` hook that blocks any `Bash` tool call matching `docker exec yadgar-db` — this is the only protection that actually works since Claude Code has Docker socket access (= root); the hook fires before execution and can reject with a clear message
- **Secondary — named Docker volume**: switch from bind mount (`~/.yadgar`) to a named volume so DB files are not accessible from the host filesystem; also eliminates the concurrent-access `.bak` corruption vector seen in v3
- **Tertiary — container hardening**: run DB container with `--read-only --security-opt no-new-privileges`, remove surreal binary after startup in entrypoint; defense-in-depth but not the primary control
- Note: the file mirror (point 5) becomes the only host-side copy of data once the bind mount is removed — acceptable since it's the recovery mechanism anyway

### 4. Async write queue in the MCP layer

- Write operations (`memorize`, `anchor`, `checkpoint`, action log captures, etc.) enqueue immediately and return success to Claude Code without waiting for the DB write to complete
- A background worker in the core container drains the queue and writes to the DB container
- Goal: MCP tools never hold Claude Code hostage waiting on DB I/O, embedding generation, or consolidation
- Queue implementation: file-based (see point 5) — write to `~/.yadgar/queue/` first, drain to DB async

### 5. File-based mirror in ~/.yadgar/ — unified with the write queue

- Every memory and wiki write is persisted to `~/.yadgar/` on the host **first** (sync, fast), then the background worker drains the file queue to DB
- This unifies points 4 and 5: the file mirror IS the durable queue — write to file → return success to Claude Code → flush to DB async
- If core container crashes mid-drain, nothing is lost — the file mirror is the recovery source
- DB is always the primary source of truth for reads; file mirror is write-through and the replay source for recovery
- Directory structure:
  - `~/.yadgar/queue/` — pending writes not yet confirmed by DB
  - `~/.yadgar/archive/` — writes confirmed by DB, kept forever for recovery
  - `~/.yadgar/wiki/` — wiki pages as `.md` files, written on every wiki change
- Worker moves files from `queue/` → `archive/` after successful DB write; on startup only `queue/` is processed — no re-import risk
- **Memory file format**: markdown with YAML frontmatter (human readable + machine replayable)
  ```markdown
  ---
  uuid: 7f3a1b2c-4d5e-6f7a-8b9c-0d1e2f3a4b5c
  context: /home/max/git/yadgar
  tags: [docker, mtree]
  created_at: 2026-04-29T20:00:00
  ---
  Memory content here...
  ```
- UUID is client-generated at write time — DB assigns its own ID on insert; file is a write intent, not a DB record
- **Wiki file format**: plain markdown, filename = slug of title

### 6. Upgrade SurrealDB to v3.x

- Bump from 2.6.5 → 3.0.5 (latest), fresh start — no data migration
- Rewrite all vector index definitions in `storage.py` from MTREE to HNSW syntax
- MTREE corruption class eliminated entirely
- The auto-recovery probe from the current fix PR can be adapted for HNSW or dropped (HNSW is significantly more stable)

### 7. Investigate better visualizations

- **Decision: react-force-graph (vasturiano)** — MIT, Three.js/WebGL, same engine Obsidian 3D Graph plugin uses
- Primary: `react-force-graph-3d` — "memory brain" 3D view, nodes colored by heat score (cool→hot scale), size by access frequency, edge particles for causal direction
- Secondary: `react-force-graph-2d` — flat analytical view, easier to read labels
- Algorithms: `graphology` + `graphology-communities-louvain` — in-browser community detection to cluster memories by entity/topic
- Hermes: no graph viz. Jarvis: no standalone viz. Obsidian 3D graph plugin uses vasturiano under the hood — steal the library, not the plugin
- Demos to review: https://vasturiano.github.io/3d-force-graph/ | https://vasturiano.github.io/force-graph/
- Alternative if more polished defaults needed: reagraph (Apache-2.0) — https://reagraph.dev/

### 8. Better stop hook

- Stop hook prompt (v4):
  > "Yadgar checkpoint: call `memorize()` once or twice for key decisions or learnings. If anything wiki-worthy was discussed (architecture, concepts, decisions), call `wiki_add()` too. Then look at your last message — if you asked a question or were mid-thought, repeat it so the conversation can continue naturally."
- Wiki generation is included but conditional — only when something genuinely wiki-worthy happened, not every session
- "Look at your last message" is explicit — model re-asks any open question so the flow isn't broken by the hook
- **Caveat**: best-effort only — hook won't fire on SIGKILL/crash/network drop; session-start restore is the reliable recovery path

### 9. Two systemd daemons

- `yadgar-db.service` — manages the SurrealDB + embedding backend container; starts first
- `yadgar.service` — manages the MCP/core container; `After=yadgar-db.service`, restarts independently on code updates
- Two installation paths:
  1. **Normal users**: `yadgar daemon install` command writes both systemd user unit files and enables/starts them — no external dependencies
  2. **Owner (Nix)**: Nix module in `maxagahi/nix` repo defines both services declaratively; authoritative for the owner's machine
- The two paths produce the same unit files — Nix is just the declarative generator for one of them

### 10. DB backup cron

- Full exports only — simple and sufficient for dev phase
- **`DEFINE TASK` does not exist in SurrealDB 3.x** — no built-in scheduler
- Implementation: background loop in `entrypoint.sh` using `curl` to hit SurrealDB's `/export` HTTP endpoint; runs inside the DB container, no new container or systemd timer needed
  ```bash
  # in entrypoint.sh, before exec
  (while true; do
    sleep 21600  # every 6 hours
    curl -sf -u "${SURREAL_USER}:${SURREAL_PASS}" \
      -H "Surreal-NS: yadgar" -H "Surreal-DB: yadgar" \
      -H "Accept: application/json" \
      -o "/data/backup_$(date +%Y%m%d_%H%M%S).surql" \
      http://127.0.0.1:8000/export
    find /data -name 'backup_*.surql' -mtime +7 -delete  # keep 7 days
  done) &
  ```
- Backup files go to the DB data volume (`/data`) — accessible via `docker cp` or file mirror if needed

### 11. Compose-based orchestration

- `docker-compose.yml` for **dev only** — handles private network, health checks, dependency ordering, named volumes, source mounts
- Production uses plain `docker run` via systemd (normal users) or Nix module (owner)
- `daemon.py` keeps managing production lifecycle; dev workflow uses `docker compose up` directly

### 12. Schema migration as first-class code

- The SurrealDB 2→3 migration (MTREE→HNSW) must be a versioned migration script that runs automatically on startup, not a one-time manual step
- v4 needs a migration layer so future schema changes don't require manual recovery procedures
- Migration state tracked in the DB itself (e.g. a `schema_version` table); on startup, run any pending migrations in order

### 13. Health monitoring between containers

- Data safety on DB unavailability is covered by the file queue (point 5) — health monitoring is purely about visibility
- Alerting: systemd journal only (WARNING level, visible via `systemctl status`)
- **Backend `/health`**: returns 200 only when SurrealDB is up AND embedding model is loaded — true readiness signal
- **Core `/health`**: returns 200 when core is up; response body includes DB reachability status so degraded state is visible at a glance
- Both endpoints used by systemd readiness checks and `yadgar daemon status` CLI

### 14. Consolidation placement

- Consolidation logic lives in the **core container** — it orchestrates writes to DB and calls the embedding endpoint
- It does not belong in the DB container (which should be stateless compute + storage only)
- With the async write queue, consolidation cycles become a background drain + enrichment pass, not a write-pressure spike against the DB

### 15. Full test coverage via HTTP transport

- **Current problem**: tests run in embedded mode (`surrealkv`, no `YADGAR_DB_URL`); production runs HTTP mode — different code paths, HTTP bugs go untested entirely
- **Goal**: tests run via the same HTTP transport as production by default; embedded mode remains as CI fallback when `surreal` binary is absent
- Full plan already documented in `PLAN_TESTS_HTTP.md` — 4 mechanical steps:
  1. Replace 23 `storage._db.query()` calls with `storage._q()` across 7 test files
  2. Rewrite `conftest.py` isolation fixture — per-test SurrealDB namespace via httpx header patch
  3. Add session-scoped `surreal_server` fixture that starts a real SurrealDB process for the test session
  4. Add `surreal` binary download step to CI workflows
- No new Python deps required; test logic unchanged
