# Plan — `code_graph`: multi-language code-structure via codebase-memory-mcp

**Date:** 2026-07-22 (**v3** — usage-model pivot: injected digest, no recall pages)
**Status:** DESIGN — ready to build (Car A safe to start now)
**ADR:** ADR-0162 (+ two audit/pivot corrections appended)
**Supersedes (eventually):** repo_wiki (#83, ADR-0157/0158/0159) — decommission after code_graph proven (task #33)

## Problem

Code-structure recall over the **user's arbitrary multi-language repos** across sessions.
repo_wiki is single-language (Python) + hash-based, and its bulk pages created recall noise —
so it went recall-EXCLUDED + TOC. repo_wiki dev is HALTED (user, 2026-07-22); code_graph is the
successor. Building an in-house multi-lang indexer is waste — use a mature permissive tool.

## Decision (ADR-0162)

Shell out to [`codebase-memory-mcp`](https://github.com/DeusData/codebase-memory-mcp) (MIT,
static C binary, 158 langs tree-sitter, offline, Louvain — no GPL). **HOST-SIDE** `yadgar
code-graph` CLI (the MCP core daemon is a read-only container, cannot reach host repos).

### Usage model (user-chosen) — injected digest + live CLI, NO recall pages
repo_wiki proved recall-ranked code pages = noise. So code_graph does NOT write recall pages
and does NOT add a `code_graph` page_type / wiki_policy entry / persist tool. Instead:

- **Per-repo architecture DIGEST** (from `get_architecture(all)`: layers, hotspots,
  entry-points, endpoints) → stored in a **yadgar memory BLOCK** scoped to the repo dir.
  Blocks are **always-injected at SessionStart** (verified: `/hooks/session-context` prepends
  `list_blocks(directory=cwd)` to project_brief; also on post-compact restore) and are
  **recall-free** → zero recall noise. Bounded (≤8000 char hard cap; target ~2000).
- **Live drill-down** = `yadgar code-graph query <repo> "<cypher>"` — ephemeral, capped, not stored.

### Locked D/S
D1 digest (not raw entities, not recall pages). D2 query-scoped + configurable. D3 indexer
SQLite authoritative; block = derived, refreshable. D4 host CLI shell-out. D5
codebase-memory-mcp (pilot-gated). S2 block scoped to indexed repo dir. S3 explicit refresh,
bounded.

## ⚠ HARD CONSTRAINT — default-branch only

**Only index the latest `origin/<default-branch>`, NEVER the working tree.** A feature-branch /
WIP / dirty checkout gives useless data. Refresh flow:
1. Resolve default branch: `git symbolic-ref refs/remotes/origin/HEAD` (fallback: remote HEAD).
2. `git fetch origin <default>`.
3. Materialize `origin/<default>` into a **temp worktree** (`git worktree add --detach`) or
   `git archive | tar -x` to scratch.
4. Index the temp with `CBM_ALLOWED_ROOT=<temp>`.
5. **Digest keyed to the REAL repo's `canonical_root` + subdir** (not the temp path).
6. Cleanup temp worktree.
- Guard: no remote / offline / fetch fails → **skip refresh**, keep last digest marked
  `stale @ <sha>`. Never silently index WIP.
- Effect: digest is always master-canonical, stable while the user branch-switches.

## ⚠ Execution locus (container reality)

MCP core daemon runs in a read-only container (`docker-compose.yml`: mounts `.:/app` + queue
vol, no `$HOME`, no user repos). So the binary + indexer runs + `~/.cache/codebase-memory-mcp`
are **HOST-SIDE only**; the binary NEVER enters the docker image. All indexing/query/refresh is
the host-side `yadgar code-graph` CLI (mirrors the existing `yadgar repo-wiki` CLI + stop-hook
precedent). The block write happens Claude-in-the-loop (injected prompt → Claude runs CLI →
Claude writes the block via `block_update`), same shape as repo_wiki's `wiki_add` flow.

## Verified facts (2026-07-22, v0.9.0 binary — MEASURED)

- CLI: `codebase-memory-mcp cli <tool> [json]`; raw-JSON positional **deprecated** → use
  flags / `--args-file` / stdin. No `--raw`. `level=info msg=mem.init` on stderr — strip.
- Tools (14): index_repository, get_architecture, query_graph (**Cypher** — cap rows/bytes,
  db_inspect 500-row precedent), search_graph, trace_path, get_code_snippet, get_graph_schema,
  search_code, list_projects, delete_project, index_status, detect_changes, manage_adr, ingest_traces.
- Offline (vendored tree-sitter + bundled `nomic-embed-code`), no API key. Static ELF ~259 MB.
  Assets carry `checksums.txt` (sha256).
- `list_projects` git metadata: `canonical_root`, `worktree_root`, `branch`, `head_sha`,
  `base_sha`, `is_worktree`, nodes, edges, size_bytes.
- Containment: `CBM_ALLOWED_ROOT` refuses any repo_path resolving (symlink/`..`) outside it —
  verified confined a monorepo leaf index to the leaf, wrote nothing to the tree.
- No plugin/library API — consumed as an MCP backend by 40+ surfaces; shell-out is intended.

## Pilot (3 Quinyx services)

| Service | Lang | nodes | edges | time | endpoints |
|---|---|---|---|---|---|
| manage/validation | Java/Spring | 744 | 1930 | 0.5 s | ✅ `Method.route_method`: POST /validate, /by-group/{id}, /internal/v1/* |
| core/pulse | PHP/Lumen | 859 | 1755 | 0.5 s | ❌ (routes in `src/routes/web.php`, not parsed) |
| tools/go-cloudbuilder | Go | 853 | 2194 | 0.5 s | n/a (CLI tool) |

- **Digest source = `get_architecture(all)`** → `layers` (api/internal/core/entry + reason),
  `hotspots` (fan_in), `clusters`, `boundaries`, `packages`, `file_tree`, counts. Compact → fits a block.
- Endpoints from `Method.route_method` ONLY (Java ✅); `Route` nodes = URL-literal noise, ignore.
  PHP/Go framework routes missing → OPTIONAL supplement (Lumen `routes/web.php` ~40-line reader), out of exit criteria.

## Yadgar integration facts (verified 2026-07-22)

- **Block injection:** SessionStart `/hooks/session-context` (`http.py:1025`, `render_blocks_section`
  `1107`) prepends `list_blocks(scope=None, directory=cwd)` to project_brief; also post-compact
  restore (`checkpoint_restore.py:441`). Scope = **exact `str(directory)`** (`blocks.py:87,358`) →
  digest injects only when session cwd == indexed dir. Index at the granularity sessions run in
  (service dirs).
- **Block gating:** `block_create/update/replace/append` all call `gate_or_reject(content)`
  (`tools/blocks.py:75,158,…`) — SAME secret gate as wiki_add. → digest passes the gate; path/
  identifier FPs possible (#30 risk, reduced — digest is summary not raw code). Test + sanitize.
- **Size:** `MEMORY_BLOCK_DEFAULT_CHAR_LIMIT=2000`, `HARD=8000`, max 10 blocks/dir.
- **Stop-hook cadence:** `stop-memory-checkpoint.py` — repo_wiki refresh every
  `REPO_WIKI_REFRESH_STOP_INTERVAL=200` msgs, priority 2. Swap point = reason string L232 +
  template `templates/repo_wiki_refresh_prompt.md`. Scheduler wiring (`_MAINTENANCE_ITEMS`,
  is_due) reused. `yadgar repo-wiki` CLI (`core/cli/repo_wiki.py`) is the host-side precedent.

## Instruction mechanism (NO skill)

1. **Agent-prompt library** — add a code-graph usage nudge to planning/coding dispatch patterns
   (plan-executing-build, dispatch-fix-bug, …) via `agent_prompt_save`: "unfamiliar/multi-file
   repo → `yadgar code-graph query` for structure before blind grep." Rides #15/#16 rework.
2. **SessionStart / project_brief** — introduce tool availability + nudge, alongside the injected
   digest ("code_graph digest below; `yadgar code-graph query <repo>` for drill-down").

## Implementation — cars

- **Car A — install (host-side)** (sonnet). Smart arch-detect (`uname -s`/`-m` → asset), pinned
  tag, `checksums.txt` sha256 verify, chmod +x. yadgar `flake.nix` per-system `fetchurl` (static
  ELF, no patchelf) — **host-side only, NOT in the docker image**. Non-nix `yadgar` setup: same
  detect+verify (no `curl | bash`). MIGRATION_NOTES for user's `modules/home/yadgar.nix`.
  Verify all 4 os×arch portable assets exist upstream first.
- **Car B — `yadgar code-graph` CLI + runner + default-branch logic** (sonnet/opus, TDD).
  Subcommands `index|query|refresh`. Runner: subprocess (flags/`--args-file`/stdin, strip stderr;
  mock for unit tests, live smoke guarded by `shutil.which` — cite the test-suite conftest guard
  pattern). **Default-branch temp-worktree flow** (the HARD CONSTRAINT above). Always export
  `CBM_ALLOWED_ROOT`. `config.py`: enable, digest char budget, per-repo scope, cache dir, cadence
  interval. Binary-absent → friendly error, not stacktrace.
- **Car C — digest renderer + block write** (opus, TDD). `get_architecture(all)` → ≤~2000-char
  digest markdown (layers/hotspots/entry-points/endpoints, keyed canonical_root+subdir). Write via
  `block_update` (gated). Golden-file tests from pilot JSON. **Verify no secret-gate FP on
  paths/identifiers** (sanitize if needed) — reduced scope vs raw-code pages, but real. Staleness =
  `head_sha` + node/edge counts (necessary-not-sufficient); `detect_changes` = freshness authority.
- **Car D — hook cadence swap + project_brief nudge** (opus). Repurpose the priority-2 maintenance
  slot: new template `templates/code_graph_refresh_prompt.md` (parallel to repo_wiki's) instructing
  Claude to run `yadgar code-graph refresh` + write the block; point the reason/template at it,
  **behind an enable flag** so repo_wiki keeps running until code_graph is proven, then flip
  (transition, not hard cutover). Add the availability nudge to project_brief/SessionStart output.
- **Car E — agent-prompt nudges** (sonnet). `agent_prompt_save` on the planning/coding patterns
  (no skill file). Rides #15/#16.
- **Car F — integration + versions + docs** (opus). e2e (guarded live smoke on a real repo) +
  version bump + CAPABILITY_REGISTRY/BEHAVIOR_CONTRACT + ADR-0162 finalize. **Pilot-gate: prove the
  digest gets USED (recalled/read + acted on), not just generated.**

**Deferred (task #33):** repo_wiki decommission — remove its generator/CLI/hook slot + sunset 432
pages, AFTER code_graph proven on ≥1 non-Python repo + on yadgar itself.

## Dependencies

A ∥ (B → C → D). E after B (references the CLI). F last. Hook flip + decommission gated on
proven-live.

## Risks / open

1. **Digest actually used?** The core value bet (repo_wiki's pages weren't). Car F pilot-gate must
   measure use, not generation.
2. **Block secret-gate FP** on digest content (paths/identifiers) — Car C test + sanitize.
3. **Exact-dir injection** — digest injects only when cwd == indexed dir; index at service-dir
   granularity where sessions run. Monorepo-root sessions won't see a leaf digest (acceptable).
4. **Default-branch fetch cost/latency** on stop-hook — bounded (fetch + ~0.5s index); skip if offline.
5. **Indexer young (v0.9.0)** — pin version; fallback graphify. CLI flags already drifted.
6. **Hook transition** — run code_graph alongside repo_wiki behind a flag; don't kill repo_wiki refresh before code_graph proven.
