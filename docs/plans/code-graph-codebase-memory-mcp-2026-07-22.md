# Plan — `code_graph`: multi-language code-structure via codebase-memory-mcp

**Date:** 2026-07-22 (v2 — adversarial-audit fixes folded)
**Status:** DESIGN — GO after audit edits; ready to build
**ADR:** ADR-0162
**Supersedes (eventually):** repo_wiki (#83, ADR-0157/0158/0159) — decommissioned after code_graph proven (task #33)

## Problem

repo_wiki documents a repo's code structure, but yadgar's generator is single-language
(Python) + hash-based. Product goal: code-structure recall over the **user's arbitrary
multi-language repos** across sessions. Building an in-house multi-language AST/graph indexer
is waste — mature permissive tools exist. repo_wiki feature dev is HALTED (user, 2026-07-22);
code_graph is its successor.

## Decision (ADR-0162)

Shell out to [`codebase-memory-mcp`](https://github.com/DeusData/codebase-memory-mcp) (MIT,
static C binary, 158 langs tree-sitter, offline, Louvain community detection — no GPL
`leidenalg`). Yadgar owns a thin adapter converting **query-scoped** indexer output into a new
`code_graph` wiki page_type. Indexer SQLite stays authoritative; yadgar holds a derived,
refreshable projection.

### Locked choices (D/S)
D1a wiki pages (not raw entities). D2b query-scoped + configurable (page budget; default
persists nothing). D3 indexer = source of truth; yadgar = derived projection; incremental
refresh. D4a CLI shell-out. D5 codebase-memory-mcp (pilot-gated). S1 new `code_graph`
page_type (own policy; gate-aware). S2 `directory_context` = indexed repo path (cross-repo).
S3 explicit capped persist; artifacts persist so stale-refresh is incremental.

## ⚠ Execution locus (AUDIT BLOCKER — corrected)

The yadgar **MCP core daemon runs in a container** (`docker-compose.yml` core service: mounts
only `.:/app` + queue volume, `read_only: true`, no `$HOME`, no user repos). An MCP tool
**cannot** shell out to the host binary against `/home/max/quinyx/...` — neither the binary
nor the repos exist in the container. This is exactly what repo_wiki solved with a **host-side
CLI** (`yadgar repo-wiki`, `yadgar/core/cli/repo_wiki.py`) + a stop-hook nudge; the agent runs
the CLI and submits results via `wiki_add`.

**Therefore:**
- **Binary + indexer runs + `~/.cache/codebase-memory-mcp` are HOST-side only.** The binary
  NEVER enters the docker image.
- **`yadgar code-graph index|query|persist` CLI subcommands** (mirror `yadgar repo-wiki`) do
  the shell-out + conversion host-side.
- **MCP surface = `code_graph_persist` only** (writes the page via the existing wiki path;
  same transport repo_wiki uses). Index/query are CLI, agent-driven via Bash (skill/hook nudge).
- **Container-mode contract:** if a `code_graph_*` MCP tool is somehow invoked in the
  container transport, return a graceful "code_graph indexing runs host-side via
  `yadgar code-graph` — not available over this transport" error, never a stacktrace.

## Verified integration facts (2026-07-22, v0.9.0 binary — MEASURED)

- CLI form: `codebase-memory-mcp cli <tool> [json]`. Raw-JSON positional arg is **DEPRECATED**
  → runner uses flags / `--args-file <path>` / piped stdin. No `--raw`. Log line
  `level=info msg=mem.init…` on stderr — strip.
- Tools (14): index_repository, search_graph, query_graph, trace_path, get_code_snippet,
  get_graph_schema, get_architecture, search_code, list_projects, delete_project, index_status,
  detect_changes, manage_adr, ingest_traces.
- `query_graph` = **Cypher** (`MATCH … RETURN … LIMIT n`) → bounding lever. **Cap rows/bytes**
  on our side (precedent: `db_inspect` 500-row cap) — raw passthrough can blow context.
- Offline (vendored tree-sitter + bundled `nomic-embed-code`), no API key. Statically-linked
  ELF (~259 MB extracted). Assets carry `checksums.txt` (sha256).
- Storage: SQLite under `CBM_CACHE_DIR` (default `~/.cache/codebase-memory-mcp/`).
  `.codebase-memory/graph.db.zst` committed snapshot is opt-in (plain index writes nothing to tree).
- **No plugin/library API** — consumed as an MCP backend by 40+ client surfaces. Shell-out is
  the intended integration; nothing to fork. Scale proof: Linux kernel 28M LOC / 75k files ~3 min.

## Pilot findings (3 real Quinyx monorepo services)

| Service | Lang | nodes | edges | time | endpoints (`route_method`) |
|---|---|---|---|---|---|
| manage/validation | Java/Spring | 744 | 1930 | ~0.5 s | ✅ `POST /validate`, `/validate/by-group/{groupId}`, `/internal/v1/…` |
| core/pulse | PHP/Lumen | 859 | 1755 | ~0.5 s | ❌ 0 (routes in `src/routes/web.php`, not parsed) |
| tools/go-cloudbuilder | Go | 853 | 2194 | ~0.5 s | n/a (CLI tool) |

- **Page backbone = `get_architecture(aspects=[all])`** → pre-digested `hotspots` (fan_in),
  `layers` (api/internal/core/entry + reason), `clusters` (communities), `boundaries`,
  `packages`, `routes`, `file_tree`, counts. Build the page from THIS; Cypher only for drill-down.
- **Node labels:** Method/Function, Class/Struct/Interface, Field, Variable, File, Folder,
  Module/Package, Decorator, Route, Section, Branch, Project.
- **Edges:** CALLS, IMPORTS, INHERITS, DEFINES/_METHOD, USAGE, WRITES, THROWS, TESTS,
  DEPENDS_ON, HANDLES, DECORATES, CONFIGURES, CONTAINS_FILE/_FOLDER, SIMILAR_TO, SEMANTICALLY_RELATED.
- **`Method` props:** complexity, cognitive, loop_depth, signature, param_types, return_type,
  is_entry_point, is_test, decorators, **route_method/route_path**.
- **Endpoints from `Method.route_method` ONLY** (Java ✅). `Route` nodes = URL-literal noise
  (wiremock/LB hosts) — ignore everywhere. PHP/Go framework routes missing → optional supplement
  (below).
- **`list_projects` git metadata:** `canonical_root` (= whole monorepo), `worktree_root`,
  `branch`, `head_sha`, `base_sha`, `is_worktree`, nodes, edges, size_bytes.

## Containment (verified, load-bearing)

Runner ALWAYS exports `CBM_ALLOWED_ROOT=<repo_path>` per shell-out — a repo_path resolving
(after symlink/`..`) outside is refused. Measured: indexer computes git `canonical_root` = whole
monorepo even when pointed at a leaf; with `CBM_ALLOWED_ROOT` set it indexed ONLY the leaf (744
nodes) + wrote nothing into the tree. Never omit it. Bug #510: subpackage index misses
parent-inherited `.gitignore` (over-scans *within* subtree, never upward) → mitigate `.cbmignore`.

## Project identity + staleness

- **Project/page key = `canonical_root` + relative subdir** (NOT bare repo name) — two leaf
  services or two worktrees of one repo must not collide in the indexer cache or in page slugs.
- **Staleness signature = `head_sha` + node/edge counts** (from `list_projects`) — necessary,
  NOT sufficient: `head_sha` is commit-grained (dirty tree → false-fresh), counts can collide.
  **`detect_changes` (filesystem) is the freshness authority**; signature is the cheap first pass.

## Install: smart arch detection (Car A)

- os `uname -s` → linux/darwin; arch `uname -m` → x86_64→amd64, aarch64|arm64→arm64.
- asset `codebase-memory-mcp-${os}-${arch}-portable.tar.gz`; pin release tag; download
  `checksums.txt`; verify sha256; `chmod +x`. **Verify all 4 os×arch portable assets exist
  upstream before building Car A** (do not assume).
- nix: per-system `fetchurl` (flake-utils, home-manager module in `flake.nix`) — static ELF,
  no patchelf. **HOST-side only — binary never enters the docker image.**
- non-nix `yadgar` setup: same detect+download+verify (pin+checksum, NOT `curl | bash`).
- MIGRATION_NOTES entry for the user's nix repo (`modules/home/yadgar.nix`) — user applies.

## Implementation — cars

- **Car A — install plumbing** (sonnet). Smart arch-detect + checksum + pinned tag; flake.nix
  per-system fetch (host-side, NOT in image); non-nix `yadgar` setup path; MIGRATION_NOTES.
- **Car B — host CLI runner + config** (sonnet, TDD). `yadgar/core/code_graph/runner.py`
  subprocess wrapper (flags/`--args-file`/stdin, strip stderr log; mock `subprocess.run` for
  unit tests; guard live smoke with `shutil.which` — cite the test-suite conftest guard pattern,
  NOT a "diagram-generator" one). Always export `CBM_ALLOWED_ROOT`. `config.py` knobs: enable,
  per-repo scope, **page budget**, module-allowlist, cache dir. Binary-absent → friendly error
  contract, not stacktrace. Wire `yadgar code-graph index|query` CLI subcommands.
- **Car C — `code_graph` page_type + policy** (opus, TDD). Add ONE `POLICY_BY_TYPE` entry in
  `_shared/wiki/policy.py` (`recall_disposition="include"`, `storage_scope`, dir-scoped) +
  a parallel `code_graph_schema.py` (COPY the `repo_wiki_schema.py` shape — **never import from
  `core/repo_wiki/` or `repo_wiki_schema.py`**, so decommission can't break code_graph). Add
  `wiki_page_types.yaml` entry; fix stale `wiki_add` docstring page_type list (`wiki.py:418`).
  **Secret-gate fix (spec'd, bounded):** new policy axis `secret_gate: "full" | "high_precision_only"`;
  `code_graph` → `high_precision_only` drops ONLY the two keyword-gated broad heuristics
  (AWS-40 `[A-Za-z0-9/+]{40}`, generic-credential) while self-anchored high-precision rules
  (AKIA, PEM, ghp_, sk-ant-, sk_live_, glpat-, AIza, xox, JWT) run UNCONDITIONALLY. Thread
  page_type through `gate_or_reject` (`wiki.py:449` — page_type already in scope). I26 lint
  (`scripts/check_secret_gate.py`) stays green (asserts call-presence only). ADR-0162 gets a
  §0.6 spoof-analysis note: page_type is non-canonical, so relaxation must never skip real-secret
  rules — only heuristics. NOT a rabbit hole if kept to rule-tiering; no allowlist plumbing.
- **Car D — converter** (opus, TDD). indexer JSON → `code_graph` page markdown. Backbone =
  `get_architecture(all)`; Cypher drill-down (capped). Endpoints from `Method.route_method`;
  ignore `Route` nodes. Golden-file tests from pilot JSON fixtures (pin binary version in
  fixture provenance). Must pass Car C schema validation (hence C→D). Route supplement is
  OPTIONAL and **explicitly excluded from Car D exit criteria** (no scope creep).
- **Car E — persist MCP tool + budget** (sonnet, TDD). `code_graph_persist` MCP tool (NOT in
  alwaysLoad hot-7, ADR-0047). **Enforce page budget HERE** (repeated persist calls must not
  re-create the 432-page repo_wiki explosion), not converter-only. `directory_context` = indexed
  repo path. Container-mode graceful error.
- **Car F — integration + versions + docs** (opus). e2e (guarded live smoke) + no-churn proof +
  version bump + CAPABILITY_REGISTRY / tool-docs / BEHAVIOR_CONTRACT updates + ADR-0162 finalize.

**Optional (excluded from exit criteria):** per-framework route reader — Lumen/Laravel
`src/routes/web.php` `$router->METHOD('/path','Ctrl@method')` + group prefixes (~40 lines,
verified), Go `mux.HandleFunc`. Framework-detect (composer.json/go.mod) first; skip if none.

**Deferred (task #33, gated on code_graph proven live):** repo_wiki decommission.

## Dependencies

A ∥ C. B → D. (C, D) → E. F last. (C independent of B; but D needs C's schema + B's runner;
E needs both C and D.) Decommission after F + live pilot on ≥1 non-Python repo + yadgar itself.

## Risks / open

1. **Indexer quality unproven** (young v0.9.0). Pilot-gate Car F on real non-Python output;
   fallback = graphify or tree-sitter+networkx.
2. **Secret-gate** — Car C hard req; keep to rule-tiering, not allowlist plumbing.
3. **Scale** — budget at persist + query-scoped default + dir-scoped recall keep the store clean.
4. **Two persistence layers** — by design; page carries indexer signature, `detect_changes` authoritative.
5. **Upstream churn** — pin binary version; CLI flags already drifted (raw-JSON deprecated mid-pilot).
