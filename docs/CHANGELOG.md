# Changelog

All notable changes to Yadgar are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning is [SemVer](https://semver.org/).

> Snapshots from v5.0.1 onward are captured from `yadgar stats` at release time.
> Earlier versions have no per-release snapshot (the practice started 2026-05-16).

## [Unreleased]

## [5.41.5] - 2026-06-02

Hotfix: move v5.39 similarity gate from MCP handler to drainer. Handler p50: 27ms → <1ms (I9 budget ≤5ms restored). **Breaking:** `wait=False` callers get `{queued: true, similarity_check: "deferred"}` instead of sync candidate list; use `wait=True` for sync rejection.

### Fixed
- **I9 budget violation** (`yadgar/server/tools/wiki.py`, `yadgar/file_queue/`):
  `wiki_add(wait=False)` handler p50 was 27ms (5.4× over 5ms I9 budget). Root
  cause: `find_similar_wiki_pages()` (embed + KNN) ran on the MCP request thread.
  Phase 0 profiling confirmed similarity gate = 102% of e2e handler time.
  Fix: gate moved to `QueueDrainer._apply_with_stage_metrics()` as a pre-apply
  stage (`_sim_gate_for_drainer()`). Handler now: secret-gate + slug-gen + enqueue
  = p50 < 1ms.

### Changed
- **Similarity gate timing (v5.39 contract change)**: gate now runs in drainer,
  not on request thread. `wait=False` path no longer returns sync rejection dict.
  `wait=True` path still returns rejection synchronously (DP-B preserved).
- **`wait=False` response shape** (BREAKING): adds `similarity_check: "deferred"`
  field. Callers checking for `{stored: False, reason: "duplicate_detected"}` on
  the async path must switch to `wait=True`. See MIGRATION_NOTES §v5.41.5.
- **Drainer extends pre-apply stage**: new `_sim_gate_for_drainer()` method in
  `_DLQMixin`. Force, replace_slug, append bypass conditions carried through
  enqueue payload and respected in drainer.

### Added
- **`yadgar_wiki_add_rejected_total{reason}`** Prometheus counter (I23): emitted
  by `_sim_gate_for_drainer()` on hard-mode rejection.
- **`FileQueue.get_job_result(job_id)`**: returns drainer-stored rejection payload
  for `wait=True` callers. `_signal_complete_with_result()` stores it.
- **`docs/V5_41_5_PROFILING_REPORT.md`**: Phase 0 per-substep profiling report.
- **8 new tests** (`yadgar/tests/test_wiki_sim_gate_drainer.py`):
  deferred-check response shape, wait=True sync rejection, force/replace_slug/append
  bypass in drainer, rejection metric increment.
- **7 updated tests** (`yadgar/tests/test_wiki_similarity_gate.py`):
  `TestWikiAddSimilarityGate` now tests `_sim_gate_for_drainer()` directly.
- **Perf test** (`yadgar/tests/test_wiki_mcp_handler_perf.py`):
  `xfail` marker removed — test now passes GREEN. Baseline updated to <1ms.

### Technical
- `wait_for_job()` defers cleanup to caller (no longer calls `_cleanup_job`
  internally) so result payload survives the wait call.
- `.complexity-baseline.json` updated for moved/modified functions.
- Plan: `docs/PLAN_V5_41_5_HANDLER_I9_FIX.md`; DPs A–E resolved.

## [5.41.4] - 2026-06-02

Tiny patch: roadmap-update-lag signal + `wiki_append_section` convention for ship entries.

### Added
- **`roadmap_update_lag_hours` signal** (`yadgar/server/tools/project.py`):
  `project_brief(mode="signals")` now returns `roadmap_update_lag_hours: float` —
  hours between roadmap wiki `updated_at` and master HEAD committer timestamp.
  `0` = roadmap is fresh. `-1.0` = roadmap wiki page not found (sentinel).
- **`update_roadmap` recommended action**: emitted when lag > 0 and a ship is
  detected. Ship detection: PRIMARY = `pyproject.toml` version changed between
  roadmap's `updated_at` and HEAD; FALLBACK = commit message matches
  `^merge: v\d+\.\d+\.\d+` or `chore: bump version`. Handles squash-merge commits
  that lack the `merge:` prefix.
- **`docs/WORKFLOW_ROADMAP_UPDATE.md`**: template and rationale for using
  `wiki_append_section` for routine ship entries instead of full RMW.
- **Roadmap wiki updated**: `wiki_append_section` dogfooded — new convention bullet
  appended to "Workflow rules (anchored)" section (version 3).
- **7 tests** (`yadgar/tests/test_roadmap_update_signal.py`):
  lag-positive, lag-zero, action-fires-on-ship, action-skips-non-ship,
  feature-branch-uses-master-head, wiki-not-found-sentinel, squash-merge-no-prefix.

### Technical
- New helpers: `_get_master_head_info`, `_get_pyproject_version_at_ts`,
  `_detect_ship`, `_compute_roadmap_signal`, `_apply_roadmap_signal`.
  Complexity-capped: each function ≤ cyclo 10, `_project_brief_signals` ≤ 15.
- `.complexity-baseline.json` updated for new helpers + `project.py` LOC growth.
- Ship-detection uses committer date (`%ct`) not author date — robust to rebases.

## [5.41.1] - 2026-06-02

Hotfix: wiki versioning transactional atomicity. Closes the silent version-hole
bug shipped in v5.41.0.

### Fixed
- **Wiki version chain atomicity** (`yadgar/storage/wiki.py`): `insert_wiki_page`
  and `update_wiki_page` now wrap the wiki_page mutation and wiki_page_version
  INSERT in a single `BEGIN TRANSACTION … COMMIT TRANSACTION` compound statement.
  In v5.41.0 the version INSERT was wrapped in `try/except`; a failure silently
  left the wiki_page mutated without a version row, creating holes in the history
  chain and breaking `wiki_restore`. Now either both rows land or both roll back.
- **Failure surface clarified**: version INSERT errors now propagate to the caller
  instead of being swallowed. `wiki_add` and `wiki_append_section` return `{"error":
  "…"}` on version failure. See `MIGRATION_NOTES.md §v5.41.1` for caller impact.

### Added
- **5 atomicity regression tests** (`yadgar/tests/test_wiki_versioning_atomicity.py`):
  insert rollback, update rollback, version chain preservation on rollback, sequential
  update serialization, happy-path baseline. Failure injection via `_q` compound-txn
  patch. Perf test: p50 ≤ baseline×1.5 (embedded SurrealKV baseline ~80-100ms; plan's
  5ms I9 figure was incorrect for this path).

### Technical
- Txn pattern: single `_q("BEGIN TRANSACTION; …; COMMIT TRANSACTION")` call per
  `upsert_project_init` precedent. IDs reserved via `_next_id` outside the txn
  (non-transactional counter; safe in single-writer embedded mode).
- Audit result: `wiki_restore`, `wiki_append_section`, and all other write paths
  reviewed — no other try/except masking found on version writes.

## [5.37.0] - 2026-06-01

Three-layer viz integration testing infrastructure. Directly addresses the v5.10.7–v5.10.9 saga
where five sequential patches failed to catch the actual bug (orphan edge endpoints crashing
force-graph.min.js). Now a single pytest catches that class of failure in CI.

### Added
- **Layer 1 — API contract integrity** (`yadgar/tests/test_graph_api_contract.py`): 18 tests on
  the `/api/graph` HTTP wire format. Asserts no orphan edge endpoints, required node/edge fields,
  node type values, stats shape. Uses `starlette.testclient.TestClient` + `BearerAuthMiddleware`
  against a seeded in-process test daemon. Meta-test confirms the orphan-edge check actually
  catches injected orphans.
- **Layer 2 — Playwright headless smoke** (`yadgar/tests/integration/viz/`): 10 tests. Spawns
  real uvicorn daemon + `viz_server._ThreadingHTTPServer` on ephemeral ports; Playwright loads
  the full `index.html`, waits for graph render, asserts DOM elements (`#canvas-wrap`, `#stats-btn`),
  no JS console errors, `allNodes` array defined, `/api/graph` request observed. System Chromium
  detection (NixOS-safe via `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` env or `shutil.which("chromium")`).
- **Layer 3 — JS unit tests** (`yadgar/static/viz_helpers.js` + `yadgar/static/viz_helpers.test.js`):
  Pure JS helpers extracted from `index.html` into an ES module. 28 Vitest tests covering
  `_fmtBytes`, `_fmtUptime`, `esc`, `_linkWidth`, and `findOrphanEdgeEndpoints` (the algebraic
  check that would have caught v5.10.9 immediately).
- **Layer 4 — CI integration** (`.forgejo/workflows/ci.yaml`): new `viz-tests` job runs all
  three layers on every PR and version tag. Installs system Chromium to avoid 200 MB bundled
  Playwright download. Layer 2 uses `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium`.
- **`viz-tests/` directory** at repo root: `package.json` + `vitest.config.js` for Vitest.
  `vitest.config.js` targets `../yadgar/static/**/*.test.js`.
- **`playwright>=1.40` + `pytest-playwright>=0.4`** added to `[project.optional-dependencies.test]`.
- **`docs/VIZ_TESTING.md`** — how-to doc covering all three layers, local dev setup, failure
  interpretation, and CI architecture.

## [5.26.0] - 2026-06-01

Published full 500q Sonnet 4.6 LongMemEval-s benchmark. Closes Adopt-1. Supersedes Haiku 96q pilot.

### Added
- **LongMemEval full 500q Sonnet 4.6 results** — `claude-sonnet-4-6` reader + judge, 500 questions (natural distribution), 470 min wall-clock via `claude -p` Max quota path (zero cash spend). **Phase 2 QA accuracy: 69.4% (347/500)** — beats Zep 63.8% (GPT-4o, 500q) by 5.6pp; apples-to-apples on sample size. Per-type: single-session-assistant 96.4%, single-session-user 92.9%, knowledge-update 75.6%, temporal-reasoning 63.9%, multi-session 55.6%, single-session-preference 33.3%. Abstention 80.0% (24/30). Full numbers + per-type breakdown in `docs/BENCHMARK_RESULTS.md`.
- **`--model` flag + `--resume` flag** — `benchmarks/run_longmemeval.py` now accepts `--model` (explicit model routing, deterministic reproducibility block) and `--resume` (per-question JSONL append with deduplication — enables incremental runs across quota windows without recompute).
- **Per-question JSONL incremental save** — `benchmarks/results/longmemeval_v5.26.0_s_full_hypotheses.jsonl` (500 lines). Survives process restart.
- **Aggregate + monitor scripts** — `scripts/aggregate_sonnet_results.py` (JSONL → final JSON + per-type table) and `scripts/monitor_sonnet_run.sh` (live progress monitoring).
- **favicon extended to graph.html** — `yadgar/templates/graph.html` now has `<link rel="icon" type="image/svg+xml" href="/favicon.svg">`. Original SVG archived in `docs/assets/`.
- **`call_claude_pipe` model routing** — passes `--model` from `ANTHROPIC_MODEL` env var explicitly so model identity is deterministic and recorded in reproducibility block.
- **`build_reproducibility_dict` model recording** — `reader_llm` and `judge_llm` fields populated from `ANTHROPIC_MODEL` at run time.
- **D2/D3 DEFER** — Sonnet run had NLI ON and `WRRF_PPR_WEIGHT=0.0` (single arm, no A/B). D2 (NLI on/off) and D3 (causal graph signals) remain DEFER pending explicit A/B runs. Plans: `docs/PLAN_V5_25_X_D2_NLI_AB.md`, `docs/PLAN_V5_25_X_D3_PC_AB.md`.
- **3 new tests** — `test_call_claude_pipe_passes_model_flag_when_anthropic_model_set`, `test_call_claude_pipe_no_model_flag_when_anthropic_model_unset`, `test_build_reproducibility_dict_llm_from_anthropic_model` (in `test_benchmark_phase1.py`).

## [5.25.6] - 2026-05-31

README cosmetic patch: HTML table white-bg wrapper for transparent PNG hero.

### Fixed
- **README hero background:** wrapped `<img>` in `<table bgcolor="white" cellpadding="40" cellspacing="0" border="0">` so the transparent-bg `yadgar.png` renders with a clean white surround on dark-mode viewers (Codeberg, GitHub). Inline `style` attribute is stripped by markdown sanitizers; legacy HTML4 `bgcolor` on table cells is preserved by all common renderers.
- **Redundant H1 removed:** deleted `# Yadgar` heading — the logo image contains the wordmark.
- **Display size:** bumped hero width 200 → 320 for better readability at typical render widths.

## [5.25.5] - 2026-05-31

SVG residue cleanup hit complexity wall (overlapping paths); pivoted to PNG with chroma-threshold transparency processing.

### Fixed
- **README hero image:** replaced `yadgar.svg` (residue) with `yadgar.png` (Pillow chroma-threshold cleaned, 531KB, 1.20% pixels made transparent).
- **yadgar.svg removed:** stale asset with near-white residue deleted from `yadgar/static/`.
- **favicon.svg unchanged:** separate clean asset; favicon links in `index.html` and `bookmarks.html` untouched.

## [5.25.4] - 2026-05-31

User-provided SVG logo wired into README hero and favicon links added to viz pages.

### Added
- **README hero image:** `yadgar/static/yadgar.svg` displayed at top of README (200px, centered).
- **Favicon — index.html:** `<link rel="icon" type="image/svg+xml" href="/favicon.svg">` in `<head>`.
- **Favicon — bookmarks.html:** same favicon link.
- **SVG assets committed:** `yadgar/static/yadgar.svg` (logo) and `yadgar/static/favicon.svg` (tab favicon).

Multi-size favicon set (16/32/48/96/180/192/512 PNG), apple-touch-icon, OG image, Info-tab branding, and tab-nav header logo deferred to v5.50 viz overhaul.

## [5.25.3] - 2026-05-31

Fast profile follow-up to v5.25.2 CPU burst hotfix.

### Fixed
- **instructions_loaded CPU burst (session_start/compact):** `hook_instructions_loaded` in `yadgar/server/http.py` called `retriever.recall()` without `profile="fast"`, triggering the full CE/NLI/MP rerank pipeline on every session_start + compact event. Highest-frequency burst path missed by v5.25.2. Fix: add `profile="fast"` (pattern matches siblings `hook_prompt_recall` + `hook_subagent_start`).
- **viz_search CPU burst (user-initiated search):** `api_viz_search` in `yadgar/server/http.py` called `retriever.recall()` without `profile="fast"`. Lower frequency than hooks but same rerank pipeline cost. Fix: same 1-line addition.

## [5.25.2] - 2026-05-31

CPU burst hotfix. Two root causes confirmed (HIGH confidence, 2-pass investigation).

### Fixed
- **subagent_start CPU burst (2.5-10s/dispatch):** `hook_subagent_start` in `yadgar/server/http.py` called `retriever.recall()` without `profile="fast"`, triggering the full CE/NLI/MP rerank pipeline on every subagent dispatch. Sibling `hook_prompt_recall` (~line 524) already used `profile="fast"` with comment warning about 8-46s bursts. Same fix applied to `hook_subagent_start` (was line 1043).
- **Consolidation daemon poison-pill loop:** `_process_action_log()` in `yadgar/consolidation/cleanup.py` hit `SecretLeakBlocked` from `insert_memory()` every cycle on a poisoned action-log group. Only 1 of N expected cycles completed in 5h10min. Fix: catch `SecretLeakBlocked` narrowly around `insert_memory()`, log WARNING, quarantine group IDs to `~/.yadgar/quarantine/action_log_poison.jsonl` (best-effort), fall through to `mark_actions_processed()` so the poisoned group never re-queues. Adds `actions_quarantined` counter to stats.

## [5.25.1] - 2026-05-31

## [5.25.0] - 2026-05-31

Benchmark infrastructure + Phase 1 retrieval-only scaffolding. Zero API spend.

Split from original v5.25.0 plan (2026-05-30): infrastructure + Phase 1 ship first (this release);
Phase 2 QA + publication ships in v5.26.0 after Phase 1 gate passes.

### Added
- **LongMemEval dataset download + sha256 pin:** `download_dataset()` in `benchmarks/run_longmemeval.py` downloads `longmemeval_s_cleaned.json` from HuggingFace. Pin constant `LONGMEMEVAL_S_SHA256` set to `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442` (verified 2026-05-31). Mismatch on re-download prints a warning (non-blocking).
- **Reproducibility metadata in output JSON:** `run_benchmark()` now writes a `reproducibility` dict to every output JSON. Fields: `yadgar_commit` (git HEAD SHA), `dataset_sha256`, `embedding_model`, `reader_llm` (null for Phase 1), `judge_llm` (null for Phase 1), `python_version`, `run_date_utc`. Helper functions: `compute_dataset_sha256`, `get_yadgar_commit`, `get_claude_version`, `build_reproducibility_dict`.
- **`docs/BENCHMARK_LICENSE.md`:** license status and required citations for LongMemEval (MIT, GREEN) and LoCoMo (CC BY-NC 4.0, YELLOW, deferred). LongMemEval citation: Wu et al., ICLR 2025, arXiv:2410.10813.
- **`docs/BENCHMARK_RESULTS.md`:** v0 draft. Phase 1 retrieval metrics table (PENDING deployment run), Phase 2 QA placeholder, comparison table (mem0 94.4, Zep 63.8, Yadgar PENDING), reproducibility block, Phase 1 gate condition, exact reproduction command.
- **`benchmarks/README.md`:** fixed LongMemEval citation URL (was `mtvu/LongMemEval`, correct is `xiaowu0162/longmemeval-cleaned`); added full Wu et al. citation per MIT attribution clause.
- **`benchmarks/results/longmemeval_v5.25.0_s_retrieval.json`:** scaffold result file. Full numbers PENDING deployment run (embedded SurrealDB path does not support FULLTEXT ANALYZER — pre-existing; full run requires live SurrealDB).
- **`yadgar/tests/test_benchmark_phase1.py`:** 12 tests. Covers: `compute_dataset_sha256` determinism, `get_yadgar_commit` and `get_claude_version` success + fail-soft paths, `build_reproducibility_dict` required fields + placeholder values, `LONGMEMEVAL_S_SHA256` format, `--retrieval-only` flag suppresses `call_claude_pipe`, `run_benchmark()` output includes `reproducibility` key. No ML pipeline invoked (all heavy fixtures mocked). TDD red-first.
- **`docs/benchmarks-current.md`:** updated status block (v5.25.0 infra shipped, run pending); per-release table extended with v5.25.0 and v5.26.0 rows.

### Plan
- v5.25.0: `docs/PLAN_V5_25_0_BENCHMARK_PUBLICATION.md` (revised, infra + Phase 1 only)
- Next: v5.26.0 Phase 2 QA + citation-ready number

## [5.24.2] - 2026-05-30

Second hotfix for bookmarks renderer introduced at v5.24.0.

### Fixed
- **bookmarks renderer round-trip crash:** v5.24.1 extracted `token.text` from the marked v15 token object correctly but then called `_origText(replaced)` — passing the HTML string back to v15's default `text` renderer, which does `'tokens' in arg` internally, throwing "Cannot use 'in' operator to search for 'tokens' in `<string>`" on any wiki page with inline text. Fixed: drop `_origText` delegation; return replaced string directly. DOMPurify downstream handles XSS.

## [5.24.1] - 2026-05-30

Hotfix for two production bugs introduced at v5.24.0 ship.

### Fixed
- **Bug 1 (bookmarks renderer):** `marked` v15 passes a token object to `renderer.text()`, not a raw string. `bookmarks.js` called `.replace()` on the token object → `text.replace is not a function`. Fixed: extract `token.text` string before `.replace()`; added `typeof content !== "string"` guard in `_renderMarkdown`.
- **Bug 2 (slug drift):** `_slugify` did not unescape HTML entities before slug generation, causing titles containing `&amp;` to produce `yadgar-roadmap-amp-*` slugs instead of canonical `yadgar-roadmap-*`. Fixed: `html.unescape(title)` at top of `_slugify` in `yadgar/wiki.py`.

## [5.24.0] - 2026-05-30

Wiki Bookmarks frontend: `bookmarks.html` + `bookmarks.css` + `bookmarks.js` + vendored libs. Completes the Wiki Bookmarks feature started in v5.23.0 backend.
v5.24.0 is a deliberate one-time even slot (frontend to match v5.23.0 backend; skip-1 convention resumes at v5.25.0).

### Added
- `yadgar/static/bookmarks.html` — bookmarks page: left sidebar (pinned list, drag-to-reorder, per-row refresh, remove), right pane (markdown rendering), queue-depth badge in nav, `+ Add` button.
- `yadgar/static/bookmarks.css` — dark theme matching `index.html` (`#0d1117`/`#161b22`/`#58a6ff` palette).
- `yadgar/static/bookmarks.js` — fetch logic against `/api/bookmarks`, `/api/wiki/read`, `/api/wiki/search`, `/api/wiki/list`, `/api/stats`. Markdown render via `marked` + `highlight.js` + `DOMPurify`. Drag-and-drop reorder. Add bookmark modal with slug autocomplete + semantic search modes. `j`/`k` keyboard nav. `r` per-row refresh. `Escape` to close modal.
- `yadgar/static/lib/marked.min.js` — marked 15.0.12 vendored (CommonMark + GFM tables/strikethrough/task lists).
- `yadgar/static/lib/highlight.min.js` — highlight.js 11.11.1 vendored (@highlightjs/cdn-assets).
- `yadgar/static/lib/dompurify.min.js` — DOMPurify 3.2.6 vendored (XSS guard on rendered markdown).
- `yadgar/static/lib/github-dark.css` — highlight.js GitHub-dark theme vendored.
- `yadgar/static/index.html` — `📑 Bookmarks` nav link added to top bar.
- `yadgar/viz_server.py` — `_mime_type()` helper + `do_GET` updated to serve any static file by path (path-traversal guard via `Path.resolve()`); falls back to `index.html` for unknown paths.
- `yadgar/server/http.py` — `GET /static/bookmarks.html` route on daemon (port 8765).
- `yadgar/tests/test_viz_bookmarks_static.py` — 71 static-asset tests: file presence, HTML structure, CSS selectors, JS functions, vendored lib sizes, viz_server MIME types + static file serving + path-traversal guard + SPA fallback.

### Deferred (PD-27)
- Playwright browser tests: deferred per plan note. Manual smoke test steps in MIGRATION_NOTES.

## [5.23.0] - 2026-05-30

Wiki Bookmarks backend: storage layer + 4 MCP tools + HTTP proxy routes. Frontend UI (bookmarks.html) ships in v5.24.0.
v5.22.0 slot reserved for hotfix per skip-1 convention (odd-only sequential features).

### Added
- `wiki_bookmark` SurrealDB table: slug (UNIQUE), label_override, position (dense 0-based int), added_at. Schema migration `009_wiki_bookmark_table`.
- `yadgar/storage/bookmarks.py` — `_BookmarksMixin` with `add_bookmark`, `remove_bookmark`, `get_bookmark`, `list_bookmarks`, `reorder_bookmark`. Idempotent add (upsert on slug); dense-integer position shift on reorder/remove.
- `yadgar/server/tools/bookmarks.py` — 4 MCP tools: `bookmark_add`, `bookmark_remove`, `bookmark_list`, `bookmark_reorder`. All registered via `@_tool()` pattern.
- HTTP routes on daemon (port 8765): `GET/POST /api/bookmarks`, `DELETE /api/bookmarks/{slug}`, `PUT /api/bookmarks/{slug}/position`, `GET /api/wiki/search`, `GET /api/wiki/list`. Cache-Control: no-store on wiki read routes.
- `viz_server.py` `do_DELETE` + `do_PUT` methods so browser-side proxy forwards all bookmark HTTP verbs.
- `yadgar/tests/test_bookmarks.py` (34 tests): storage CRUD + MCP tool unit tests. TDD red-first.
- `yadgar/tests/test_api_bookmarks.py` (14 tests): HTTP route registration + proxy + e2e MCP tests.

### Internal
- `_BookmarksMixin` added to `StorageEngine` inheritance chain.
- `wiki_bookmark` added to `_WIPE_TABLES` in test conftest for per-test isolation.

### Deferred (v5.24.0)
- Frontend: `bookmarks.html`, `bookmarks.css`, `bookmarks.js`, nav link in `index.html`
- Vendored libs: `marked.min.js`, `highlight.min.js`, `dompurify.min.js`
- Playwright browser tests (PD-27)

See MIGRATION_NOTES.md — v5.23.0

## [5.22.0] - 2026-05-30

Hotfix slot — reserved per skip-1 convention (odd-only sequential features). No release shipped.

## [5.21.0] - 2026-05-30

Cross-project anchor redundancy detection and PD-23 migration_grace graceful expiry handler.
Deadline driver: first pre-v5.8 backfilled anchors expire 2026-08-26.

### Added
- `audit_anchors()` returns `cross_project_redundancy_candidates` key: cosine >= 0.95 + content_length_ratio > 0.85 pairs across different `directory_context` values. AUDIT-GATED ONLY — never auto-mutates.
- `project_brief(mode="signals")` surfaces `cross_project_redundancy_candidates` (omitted when empty; capped at 3 for token budget).
- `verify_grace_expired_anchor` recommendation type in `audit_anchors()` actions: surfaces `migration_grace=True` rows past `valid_until` as user-gated review items. Always `skipped=True` — never auto-applied.
- New env knob `ANCHOR_CROSS_PROJECT_COSINE` (default 0.95): minimum cosine for cross-project dedup. Registered three-way (Settings, config_registry, config_yaml `anchor_hygiene` section).

### Internal
- New test file `test_cross_project_audit.py` (16 tests): detection, filtering, shape, primary selection, never-auto-mutate guard.
- `TestMigrationGraceHandler` class added to `test_audit_anchors.py` (7 tests): PD-23 handler.

See MIGRATION_NOTES.md — v5.21.0

## [5.20.0] - 2026-05-30

Hotfix: db-lockdown PreToolUse hook migrated from project-local `hook_runner.py` dispatcher
to a standalone `yadgar/hooks/db-lockdown-check.py` script that ships with the package and
is deployed by `install_hooks`. Fixes recurring "hookSpecificOutput is missing required field
'hookEventName'" PreToolUse validation errors caused by the old handler's non-compliant
JSON output.

### Fixed
- PreToolUse Bash hook now emits `hookEventName: "PreToolUse"` on all paths (allow, deny, fail-soft), satisfying the Claude Code 2026 hook schema.
- Old `hook_runner.py db-lockdown-check` wiring referenced a gitignored local file; in-session fixes were lost on every context reset. Now ships as `yadgar/hooks/db-lockdown-check.py`, installed globally as `~/.claude/hooks/yadgar-db-lockdown-check.py` by `install_hooks`.

### Changed
- `install_hooks_lib.py` `PreToolUse` entry uses direct-command pattern (`python3 "<dst>"`) instead of dispatcher pattern, matching `SubagentStop`, `InstructionsLoaded`, and `SubagentStart` hooks.
- `yadgar/scripts/hook_runner.py`: removed `hook_db_lockdown_check()` and `"db-lockdown-check"` from `_HOOKS`.

### Internal
- 7 new tests in `test_db_lockdown_hook.py` (subprocess-level, tests real entry point).
- 1 new test in `test_server.py`: `test_install_hooks_pretooluse_direct_command_not_dispatcher`.

See MIGRATION_NOTES.md — v5.20.0

## [5.19.0] - 2026-05-30

Scope-aware anchor surfacing in `HippocampalReplay.restore()`. Projects with 20+
anchors no longer crowd out global anchors from the restore payload.

### Fixed
- `restore()` called `get_anchored_memories(limit=20)` — a flat unscoped query. Projects with many anchors silently dropped global anchors. Now uses `get_anchored_memories_scoped(directory, limit)`: global bucket first, then project bucket, merged with dedup and heat ordering.

### Added
- `StorageEngine.get_anchored_memories_scoped(directory, limit)` — two-query scope split with hard safety cap 50 per scope, expired anchor exclusion, heat DESC ordering within scope.

### Internal
- 12 new tests in `test_anchor_surfacing.py`.

See MIGRATION_NOTES.md — v5.19.0

## [5.17.0] - 2026-05-30

Write-time contradiction detection wired default-on. Contradicting memories
no longer wait for the nightly consolidation pass — the lightweight detector
fires on every write where similar memories (cosine ≥ 0.6) exist.

### Added
- `curate_on_remember()` calls `detect_contradictions()` before merge/link/create. Env-gated via `YADGAR_WRITE_TIME_CONTRADICTION` (default `on`); fail-soft so detector errors never block writes.
- `yadgar_write_time_contradiction_total{reason}` counter (`negation_mismatch` | `action_divergence`).

### Fixed
- `confidence` added to `_MEMORY_UPDATABLE_FIELDS` — `update_memory_fields(id, confidence=...)` was a silent no-op; detector's confidence-decay side effect was dead code.

### Internal
- 7 new tests in `test_write_time_contradiction.py`.

See MIGRATION_NOTES.md — v5.17.0

## [5.15.0] - 2026-05-30

Two independent D-items: per-phase CPU burst alerting (D1) and secret-gate
tag plumbing through production write tools (D4/Part B).

### Added
- `PHASE_DURATION_WARN_MS` (default 60 000 ms): all 7 `_consolidation_cycle` phases now emit CRITICAL log when they exceed the threshold. Configurable via env or `config.yaml`.
- `yadgar/security/allowlist.py` tag plumbing: `memorize`, `wiki_add`, and `anchor` now pass `tags=` through `gate_or_reject()`. Allowlist entries configured in v5.13.0 now fire on real tool calls.

### Fixed
- Secret-gate allowlist was dormant on production writes — callers forwarded no `tags=` so allowlist entries never matched. All three write tools corrected.

### Internal
- 4 new CPU-burst tests; 5 new plumbing tests; `test_memorize_reinject_gate.py` updated to patch `gate_or_reject` (was `check_secrets`).

See MIGRATION_NOTES.md — v5.15.0

## [5.13.1] - 2026-05-30

Integration test backend pin fix — conftest was hard-coded to `5.0.3` while
production runs `5.4.0`.

### Fixed
- `yadgar/tests/integration/conftest.py` now reads `backend_version` from `server.json` at collection time; `pytest.skip` on read/parse failure.

### Internal
- 3 new tests: version-reads-server-json, skip-on-missing-file, regression gate for the `5.0.3` literal.

See MIGRATION_NOTES.md — v5.13.1

## [5.13.0] - 2026-05-30

Secret-gate context-awareness: user-managed YAML allowlist with JSONL audit
trail lets known-good content (test fixtures, plan docs) bypass pattern
detection without weakening strictness.

### Added
- `yadgar/security/allowlist.py` — `AllowlistEntry`, `is_allowlisted()`, `_write_audit()`. Allowlist loaded from `~/.yadgar/secret-gate-allowlist.yaml`; schema-version validated; errors loudly.
- `gate_or_reject()` extended with `tags=` and `source=` kwargs; calls `is_allowlisted()` before pattern scan.
- I28 pre-commit invariant (`scripts/check_allowlist_audit.py`).

### Internal
- 11 new tests (`test_allowlist.py`); fixture YAML covering known v5.10.2 false-positive cases.
- **Known gap at release:** no production write tool forwarded `tags=` to `gate_or_reject()` — allowlist loaded but never matched. Closed in v5.15.0.

See MIGRATION_NOTES.md — v5.13.0

## [5.11.0] - 2026-05-30

All 35 hardcoded viz constants (node size, edge width, physics, layout,
search colors) are now overridable via `config.yaml` — no redeploy needed
to tweak the graph.

### Added
- 35 `VIZ_*` Settings fields with v5.10.11 hardcoded values as defaults.
- `GET /api/viz/config` endpoint returning nested JSON; auto-protected by bearer auth.
- `loadVizConfig()` in frontend: fetches config at graph load, deep-merges over defaults.
- `config_yaml.py` + `config_registry.py` updated (I25 three-way sync).

### Changed
- All viz constants in `index.html` replaced with `YADGAR_VIZ_CONFIG.*` references.

See MIGRATION_NOTES.md — v5.11.0

## [5.10.4 → 5.10.11] - 2026-05-30 — Viz saga

Eight rapid patches fixing a cascade of force-graph rendering bugs discovered
after the v5.10.3 deploy. Root cause (v5.10.9): causal edges reference
`entity:*` node IDs that are never included in `get_full_graph()` — one
orphan edge crashed the force-graph library synchronously, stalling the
entire simulation. All v5.10.7–v5.10.8 attempts targeted downstream effects.

### Fixed
- **v5.10.4** `consolidate_now()` now accepts `mode='light'` (default, consolidation only) or `mode='full'` (+ sleep cycle + anchor pass). Previous no-arg behavior ran the full sleep cycle every time. Also: `hook_runner.py` PreToolUse schema updated to current Claude Code format.
- **v5.10.5** Two nightly-cycle URL bugs: vacuum and nightly-cycle entry-points used hard-coded `:8080` fallback; now read `YADGAR_DB_URL`. Also: `create_snapshot()` stamps snapshot mtime to prevent prune-on-create race.
- **v5.10.6** Session-end sentinel markers: new `session-end-capture.py` hook writes `~/.yadgar/session-ends/` on session exit; `project_brief(mode="signals")` surfaces `extract_last_session_findings` on next startup. Requires `install_hooks` re-run.
- **v5.10.7** Viz UX: 3D heat coloring, wiki/memory shape distinction (octahedra vs spheres), semantic search in 3D mode, stats overlay auto-refresh every 5 s.
- **v5.10.7.1** Bundled hotfix: sentinel slash-command noise filter (`SKIP_TAGS` frozenset covering 7 tag types) + 3D viz node material changed `MeshLambertMaterial` → `MeshBasicMaterial` (Lambert requires scene lights ForceGraph3D does not provide).
- **v5.10.7.2** 3D viz `transparent` flag: `MeshBasicMaterial({transparent: true})` at opacity 1.0 caused triangle-sort artifacts ("shard" rendering). Fixed to `transparent: !!node.__dimmed`.
- **v5.10.7.3** Reverted all custom `_makeNodeThreeObject` geometry (three attempts failed). ForceGraph3D default spheres restored; heat coloring retained via `.nodeColor()`.
- **v5.10.8** Physics hang: `onEngineStop` was firing with 0 ticks and pinning all nodes at origin. Tick-count guard (≥50 ticks before pin). Mesh leak: `resetLayout` double-data cleared by removing the empty-then-restore pattern.
- **v5.10.9** Root cause fix: `graph_api.py` now filters orphan edges (source or target absent from node set) before returning graph data. Frontend mirrors the filter. Counter `yadgar_graph_api_orphan_edges_dropped_total` added.
- **v5.10.10** 3D node size doubled (`nodeRelSize(8)`, default was 4). Auto-zoom-fit on engine tick 80 in both 2D and 3D modes.
- **v5.10.11** 3D-only: edge thickness ×1.5, link repulsion distance ×1.2 (30→36).

See MIGRATION_NOTES.md — v5.10.4 through v5.10.11

## [5.10.3] - 2026-05-29

`scan_db_for_secrets.py` end-to-end fix.

### Fixed
- OTLP hang on exit suppressed via `YADGAR_OTLP_ENDPOINT=""` env before import.
- `--limit N` scan now orders `DESC` (newest rows first) so recent leaks aren't missed.

See MIGRATION_NOTES.md — v5.10.3

## [5.10.2] - 2026-05-29

Secret-gate architecture (I26) + memorize/anchor parity + nightly-cycle hotfix.

### Added
- Two-layer secret defence: API boundary (`gate_or_reject()`) + storage level (`check_secrets()` inside `insert_memory()`). `SecretLeakBlocked` classified permanent in DLQ.
- `YADGAR_SECRET_GATE_DISABLED=1` kill switch (Layer 1 only; logs WARNING on every boot).
- Pattern thresholds tightened (GitHub PAT, Anthropic, OpenAI all cut to `{20,}`).
- I26 pre-commit lint (`scripts/check_secret_gate.py`).
- `memorize(is_protected=True)` now behaves identically to `anchor()`: injects `_anchor` tag and `anchor:{reason}` tag, defaults tier to `"conditional"`.

### Fixed
- `surrealdb` promoted from dev-dep to main dep; `pip install yadgar` no longer `ImportError`.
- vacuum `:8080` hard-coded URL replaced with `YADGAR_DB_URL` env read.

See MIGRATION_NOTES.md — v5.10.2

## [5.10.1] - 2026-05-29

`_active_work` and checkpoint soft-warning tier + optional watchdog timer.

### Added
- Two new `project_brief(mode="signals")` action types: `consider_refresh_active_work` and `consider_refresh_checkpoint` (soft warn before hard stale threshold). Both include `suggested_call` copy-paste field.
- Active-work directory registry (`~/.yadgar/active-work-tracked/`); optional watchdog systemd-user timer units in `scripts/systemd-user/`.
- `YADGAR_ACTIVE_WORK_WARN_HOURS`, `YADGAR_CHECKPOINT_WARN_HOURS`, `YADGAR_AUTO_REFRESH_ACTIVE_WORK` knobs (three-way registered).

### Changed
- `signals` mode token budget raised 100 → 350 (configurable via `YADGAR_SIGNALS_TOKEN_BUDGET_SOFT`).

See MIGRATION_NOTES.md — v5.10.1

## [5.10.0] - 2026-05-29

Test harness hardening: orphan SurrealDB process reap, deterministic xdist
port allocation, and multi-agent session isolation.

### Added
- `yadgar/tests/_surreal_helpers.py`: centralized `spawn_surreal()` with `atexit` + `pytest_sessionfinish` cleanup.
- Deterministic xdist ports: `YADGAR_TEST_PORT_BASE + worker_index * 100 + n` (default base 12000).
- `YADGAR_TEST_NAMESPACE` env redirects `TMPDIR` for concurrent agent sessions.
- Optional `yadgar-test-orphan-cleanup.timer` (user-managed systemd-user unit).

### Changed
- Default pytest timeout raised 120 s → 300 s (via `[tool.pytest.ini_options]`); per-test `@pytest.mark.timeout` now works.

See MIGRATION_NOTES.md — v5.10.0

## [5.9.0] - 2026-05-28

Anchor audit tooling: `audit_anchors()` MCP tool + automatic anchor pass
inside `consolidate_now()`.

### Added
- `audit_anchors(directory, dry_run=True)` MCP tool: scans anchors for expired rows, redundant pairs (cosine ≥ 0.92), and promote-to-wiki candidates. Returns draft `wiki_add` payloads; never auto-promotes.
- Anchor pass runs at end of `consolidate_now()` (gate: `ANCHOR_AUDIT_CONSOLIDATION_ENABLED`, default `true`). Writes `_audit_anchors` sentinel memory (latest-wins per directory).
- `recommended_actions` items now include `suggested_call` copy-paste field (v5.8 items lacked it).
- 3 new config knobs: `ANCHOR_AUDIT_CONSOLIDATION_ENABLED`, `ANCHOR_AUDIT_MAX_ACTIONS_PER_RUN` (20), `ANCHOR_AUDIT_HISTORY_RETENTION_DAYS` (30).

See MIGRATION_NOTES.md — v5.9.0

## [5.8.0] - 2026-05-28

Anchor hygiene foundation: tier expiry, TTL fields, and signals-mode
candidate detection.

### Added
- `tier` (`semantic_immortal` | `conditional` | `ephemeral`), `valid_until`, `ttl_days` params on `memorize()` and `anchor()`. `tier="conditional"` is the new default for anchors (90-day TTL).
- `valid_until < now()` rows excluded from `restore()`, hot ranking, and `project_brief(restore)`.
- `signals` mode: `anchor_count_project`, `anchor_redundancy_candidates`, `anchor_promote_candidates` fields. Four new `recommended_actions` types.
- Schema migration `migration_008`: adds `tier`, `valid_until`, `migration_grace` columns; backfills pre-v5.8 anchors with `tier="conditional"`, `migration_grace=True`.
- 7 new config knobs (three-way registered per I25).

See MIGRATION_NOTES.md — v5.8.0

## [5.7.12] - 2026-05-27

`project_brief()` two-audience split: `signals` mode for stop hooks and
`restore` mode for post-`/clear` rehydration.

### Added
- `signals` mode: binary flags + age numerics + `recommended_actions` (stale checkpoint, stale active-work, bootstrap-project). Budget <100 tokens.
- `restore` mode: `top_anchors` (scope-tagged) + `hot_memories` + checkpoint + key wiki pages. Budget <800 tokens.
- 3 new config knobs: `YADGAR_ACTIVE_WORK_STALE_HOURS` (24 h), `YADGAR_CHECKPOINT_STALE_HOURS` (24 h), `YADGAR_PROJECT_BRIEF_MAX_ANCHORS` (12).

### Fixed
- `hot_memories` now excludes anchor-tagged entries in all modes (were appearing at top due to `heat=1.0`).

### Internal
- 38 new tests (`test_project_brief_modes.py`).

See MIGRATION_NOTES.md — v5.7.12

## [5.7.11] - 2026-05-27

5 OTLP + dbsize knobs migrated from env-only to yaml-overridable Settings
fields; dead `YADGAR_LOG_LEVEL` env declaration removed. Backend 5.3.0 → 5.3.1.

### Changed
- `OTLP_ENDPOINT`, `OTLP_HEADERS`, `OTLP_TIMEOUT_SEC`, `OTLP_INSECURE`, `DBSIZE_CACHE_TTL_SEC` now read from Settings (yaml or env); `yadgar.tracing` + `embed_service` refactored accordingly.
- `YADGAR_LOG_LEVEL` removed from `config_registry.py` (was declared but never read).

See MIGRATION_NOTES.md — v5.7.11

## [5.7.10] - 2026-05-27

Container yaml-loading fix, I25 three-way-sync invariant, and nix `-e` flag
cleanup.

### Added
- `YADGAR_CONFIG_FILE` env override in `get_config_path()`; containers now actually read `~/.yadgar/config.yaml` via bind mount.
- I25 invariant: `test_config_three_way_sync.py` enforces every `Settings` field is either triple-registered or allowlisted as env-only. Wired into pre-commit + CI.

### Changed
- 4 operational knobs (`HOST`, `PORT`, `WIKI_SLUG_PREFIX`, `CORE_LOG_LEVEL`) moved from nix `-e` flags into `config.yaml`; yadgar core ExecStart reduced from 12 to 8 `-e` flags.

See MIGRATION_NOTES.md — v5.7.10

## [5.7.9] - 2026-05-27

`SessionStart` hook response now branches on `source` field.

### Changed
- `compact` source suppressed (post-compact-rehydrate hook already handles restore); other sources (`startup`, `resume`, `clear`, missing) emit tailored copy. Eliminates duplicate restore hint on compact.

See MIGRATION_NOTES.md — v5.7.9

## [5.7.8] - 2026-05-27

`/mcp` trace_id wiring fix.

### Fixed
- `POST /mcp` log lines lacked `trace_id`: new `MCPTraceSpanMiddleware` inserted above `RequestLoggingMiddleware` so the outer span is live when the finally block reads it.

See MIGRATION_NOTES.md — v5.7.8

## [5.7.7] - 2026-05-27

`VIZ_HEALTH_REFRESH_SEC` env knob for viz daemon scrape interval (was hardcoded 5 s).

### Added
- `YADGAR_VIZ_HEALTH_REFRESH_SEC` Settings field (default 5.0). Live-reloaded per iteration — no daemon restart needed.

See MIGRATION_NOTES.md — v5.7.7

## [5.7.6] - 2026-05-27

OTLP/HTTP span exporter to Tempo.

### Added
- When `YADGAR_OTLP_ENDPOINT` is set, spans export via OTLP/HTTP alongside the existing `LogSpanProcessor`. 4 new knobs: `OTLP_ENDPOINT`, `OTLP_HEADERS`, `OTLP_TIMEOUT_SEC`, `OTLP_INSECURE`.
- New dep: `opentelemetry-exporter-otlp-proto-http>=1.30,<2`.

See MIGRATION_NOTES.md — v5.7.6

## [5.7.5] - 2026-05-27

I24 `@trace_span` AST lint invariant.

### Added
- `scripts/check_trace_spans.py` (stdlib AST): enforces all public HTTP handlers in `server/http.py` carry `@trace_span`. Wired into pre-commit + CI.
- 13 `@trace_span` decorators added to previously un-spanned handlers.

See MIGRATION_NOTES.md — v5.7.5

## [5.7.4] - 2026-05-27

Hook observability: `@trace_span` + duration histogram + failure counter on
`/hooks/auto-capture` and `/hooks/prompt-recall` (the two highest-traffic handlers).

### Added
- `hook_auto_capture` and `hook_prompt_recall` gain full `_hook_observe` + `_hook_observe_response` envelope matching the PR-K pattern.

See MIGRATION_NOTES.md — v5.7.4

## [5.7.3] - 2026-05-27

Remove duplicate `yadgar_db_query_duration_seconds` metric.

### Internal
- Duplicate declaration and write site removed; only `yadgar_surrealdb_query_duration_ms{op}` remains.

See MIGRATION_NOTES.md — v5.7.3

## [5.7.2] - 2026-05-27

`CROSS_ENCODER_TOP_K` default cut 20 → 10 to halve CE rerank latency.

### Changed
- CE candidate count halved at default; recall quality impact expected minimal at corpus sizes tested. Override via `YADGAR_CROSS_ENCODER_TOP_K=20`.

See MIGRATION_NOTES.md — v5.7.2

## [5.7.1] - 2026-05-27

Consolidation `systemctl` container fix — auto-vacuum trigger now works
inside containers.

### Fixed
- `_maybe_auto_vacuum` pre-check called `systemctl --user is-active` which raised `FileNotFoundError` in containers and returned early, disabling the threshold backstop. Pre-check removed; trigger-file pattern from v5.7.0 PR-4 is sufficient.

See MIGRATION_NOTES.md — v5.7.1

## [5.7.0] - 2026-05-26

Nightly cycle redesign: daemon's 30-minute consolidation trigger removed;
replaced by a single `yadgar-nightly-cycle` systemd timer at 19:00 UTC
running `backup → consolidation → vacuum → backup`.

### Added
- `yadgar/scripts/nightly_cycle.py` console script (`yadgar-nightly-cycle` entry point).
- `yadgar/backup.py` `create_snapshot()` + `prune_snapshots()` helpers.
- Trigger-file pattern for `vacuum_now()` MCP tool: writes atomic file at `YADGAR_VACUUM_TRIGGER_PATH`; host-side systemd path-watch unit starts vacuum service. Eliminates container→host systemctl call.
- `VACUUM_AUTO_THRESHOLD_BYTES` documented as emergency backstop only.

### Changed
- Consolidation fires only at nightly cycle (19:00 UTC) or explicit `consolidate_now()` call.

### Internal
- Backup snapshot round-trip integrity tests.

See MIGRATION_NOTES.md — v5.7.0

## [5.6.7] - 2026-05-25

File logging made opt-in via `YADGAR_LOG_DIR`; enables Grafana Alloy log
shipping without privilege escalation.

### Changed
- File logging now requires `YADGAR_LOG_DIR` (or per-file env vars) to be set; containers default to `/data/logs` via entrypoint. Bare-metal installs are stdout-only by default.

See MIGRATION_NOTES.md — v5.6.7

## [5.6.1] - 2026-05-22

Viz daemon health endpoint bug fixes.

### Fixed
- Backend metrics URL now resolved via `YADGAR_EMBED_URL` / `YADGAR_BACKEND_METRICS_URL` (was hard-coded).
- `parse_core_metrics` reads correct per-registry metrics (`yadgar_process_rss_bytes`, `_open_fds`, `_cpu_percent`).

See MIGRATION_NOTES.md — v5.6.1

## [5.6.0] - 2026-05-22

Viz daemon health sidebar (V1c): live core + backend stats overlay in the
graph UI.

### Added
- `yadgar/viz_daemon_health.py`: background scraper polling core + backend `/metrics` every 5 s; `/api/daemon-health` endpoint.
- SSE `daemon_health` event emitted every 5 s from `_make_event_stream`.
- Collapsible "Daemons" sidebar panel in `index.html` showing process, queue, log, CB, and rerank metrics.

See MIGRATION_NOTES.md — v5.6.0

## [5.5.3] - 2026-05-22

Circuit-breaker state gauge fix.

### Fixed
- `yadgar_circuit_breaker_state{endpoint}` gauge was emitting nothing — polling function looked for attributes that never existed. Replaced with inline update on every CB state transition.

See MIGRATION_NOTES.md — v5.5.3

## [5.5.2] - 2026-05-22

Backend log metrics wiring fix (backend 5.1.1 → 5.1.2).

### Fixed
- `yadgar_log_file_size_bytes`, `yadgar_log_file_rotations_total`, `yadgar_log_dropped_total` now update correctly in the backend's own Prometheus registry.

See MIGRATION_NOTES.md — v5.5.2

## [5.5.1] - 2026-05-22

Dual-sink log rotation + token-bucket rate limiter.

### Added
- `RotatingJSONLFileHandler` (Sink B): 100 MB × 5 backups per daemon. Core: `/data/logs/yadgar.log`; backend: `/data/logs/backend.log`.
- Token-bucket log rate limiter: 10 records/s burst 50 per (logger, level) bucket. `YADGAR_LOG_RATE_LIMIT_ENABLED` kill switch.
- 3 new metrics: `yadgar_log_file_rotations_total`, `yadgar_log_file_size_bytes`, `yadgar_log_dropped_total`.

### Changed
- Backend 5.1.0 → 5.1.1.

### Operator action
- `mkdir -p ~/.yadgar/logs` required before deploy; absent dir → graceful stdout-only fallback.

See MIGRATION_NOTES.md — v5.5.1

## [5.5.0] - 2026-05-22

Backend `/metrics` endpoint (V1a): Prometheus metrics from `yadgar-backend`
container.

### Added
- `yadgar/embed_service_metrics.py`: rerank request/503/duration/semaphore metrics per `{mode}` + model-loaded gauge + process metrics. Exposed at `GET /metrics` (unauthenticated, loopback-only port 8001).

### Changed
- Backend 5.0.3 → 5.1.0.

See MIGRATION_NOTES.md — v5.5.0

## [5.4.8] - 2026-05-22

Request log visibility fix — `yadgar.requests` INFO lines were silently
dropped at WARNING root level.

### Fixed
- `configure_logging()` installs a dedicated always-INFO `StreamHandler` on `yadgar.requests` (`propagate=False`).
- **Note:** `YADGAR_LOG_LEVEL` is not a valid env var; correct var is `YADGAR_CORE_LOG_LEVEL`.

See MIGRATION_NOTES.md — v5.4.8

## [5.4.7] - 2026-05-22

I14 request-log schema ratchet.

### Changed
- `RequestLoggingMiddleware`: `duration_ms` renamed to `latency_ms` (**BREAKING** — update Loki/Grafana queries); `status` renamed to `http_status`; `component`, `action`, `outcome` fields added.
- `ContentRedactor` denylist tightened: `content_type`/`content_length` no longer falsely redacted.

See MIGRATION_NOTES.md — v5.4.7

## [5.4.6] - 2026-05-22

LOW-risk complexity refactor: 4 functions decomposed via dataclass parameter
objects (P12 audit).

### Internal
- `insert_typed_relationship`, `insert_new_memory`, `create_checkpoint`, `cmd_config` refactored. `.complexity-baseline.json` regenerated.

See MIGRATION_NOTES.md — v5.4.6

## [5.4.3] - 2026-05-22

I14 framework-logger coverage: all framework loggers (uvicorn, mcp, fastmcp,
httpx, starlette) now emit I14-conformant JSON.

### Changed
- `configure_logging()` uses root-logger approach to cover all framework loggers.
- 31 pre-existing C901/PLR0913 ruff violations grandfathered in per-file-ignores.
- New `YADGAR_LOG_FORMAT=human` env for local dev.

See MIGRATION_NOTES.md — v5.4.3

## [5.4.2] - 2026-05-22

Circuit-breaker probe hardening (CB-1 backoff + F5-A semaphore) + I14
structured logging default-on.

### Added
- CB-1 probe: exponential backoff up to `YADGAR_CIRCUIT_BREAKER_MAX_OPEN_DURATION_SEC` (600 s). Probe timeout configurable via `YADGAR_CIRCUIT_BREAKER_PROBE_TIMEOUT_SEC` (2 s).
- Backend rerank semaphore `YADGAR_RERANK_MAX_CONCURRENCY` (default 1): concurrent rerank requests beyond the cap receive HTTP 503 immediately instead of queueing indefinitely.
- Image size ratchet (`scripts/check_image_size.py`): backend ≤2.0 GB, core ≤0.8 GB.

### Changed
- **Breaking:** default log format changed `human` → `json` (I14 structured). Old fields `timestamp`→`ts`, `message`→`event`. Set `YADGAR_LOG_FORMAT=text` to restore human-readable output.
- Backend 5.0.2 → 5.0.3.

See MIGRATION_NOTES.md — v5.4.2

## [5.4.1] - 2026-05-21

P11 Observability v1 — unified metrics framework. Per invariant I12 (measure before optimize), this is the prerequisite for all further v5.4.x perf work (P12 audit, F0 image bloat, F5 OOM report, I14 logging, eventual memorize split in v5.5).

### Added
- **`yadgar/observability/`** subpackage with `stage_timer` + `request_timer` decorators. Backward-compatible: no-op when `prometheus_client` is absent.
- **37 metric families declared** at `/metrics` (Prometheus format) covering write path (queue depth, drainer lag, drain stages, writegate outcome), read path (recall / wiki_query duration + per-stage histograms), embedding, KG / curator / engram / astrocyte, LLM C4 calls, MCP transport + auth, SurrealDB queries + pool, process (RSS, CPU, FDs, GC), subagents, viz, backend liveness, **and `yadgar_circuit_breaker_state{endpoint}` reading directly from CB-1 (Pattern Library).**
- **Grafana dashboard** at `docs/observability/dashboard.json` (UID `yadgar-v1`, 6 rows).
- **Alert rules** at `docs/observability/alerts.yaml` — 5 starter rules: `YadgarDrainerLagHigh`, `YadgarDlqGrowing`, `YadgarRecallSlow`, `YadgarBackendUnreachable`, `YadgarCircuitBreakerStuck`.
- **`memory_stats` MCP tool extended** with a `metrics` block surfacing `queue_depth`, `drainer_lag_p95_ms`, `recall_p95_ms`, `circuit_breaker_states`.

### Internal
- 6 new observability tests (decorator emit, no-op fallback, /metrics endpoint format, breaker-state metric, memory_stats surfacing).
- Observe overhead measured: p50 0.67µs, p95 0.70µs per `Histogram.observe` — within I9 budget.

### Deferred (registered but not yet populated, await touching the underlying code)

These metric families are declared + exported at `/metrics` but populate as empty / zero until their underlying functions are instrumented. Per invariant I5 (decomposition preserves topology), they wait for the targeted P-items rather than risky in-place rewrites:

- `yadgar_recall_stage_ms{stage}` 9 sub-stages — await v5.5 P1 memorize split / P3 asyncio.to_thread wrap.
- `yadgar_wiki_query_stage_ms{stage}` 9 sub-stages — same.
- `yadgar_encode_duration_ms{model}` actual observations — await P3.
- `yadgar_surrealdb_query_duration_ms{op}` — needs storage-layer wrapper, separate v5.4.x PR.
- `yadgar_mcp_auth_check_duration_ms` — middleware-level, separate v5.4.x PR.
- All KG / curator / engram / LLM / subagent / viz observations — declared, populate as their respective modules get touched.

This is the v1 framework. Subsequent v5.4.x ships populate observation sites as code paths are touched.

## [5.4.0] - 2026-05-21

First v5.4 minor — three quick wins (B bundle per locked trajectory). Single tag-able release before P11 Observability v1 starts. P11 + heavier items ship as later v5.4.x patches.

### Added
- **W1.** `wiki_add` accepts `branch_hint: str | None` arg (mirrors `memorize` per v5.1.9). New resolution: explicit `branch` wins → `branch_hint` next → both omitted → `branch IS NULL` (canonical slot). Removes the `_detect_branch(os.getcwd())` fallback that always returned the daemon's CWD branch (`master`) regardless of caller. Fixes the long-standing meridian-style wiki-routing bug where uploads from non-daemon projects landed unsearchable.
- **P7.** `YADGAR_REINJECT_ON_WRITE` env (default `0` / OFF). When OFF, the write-time reinjection block in `memorize` is skipped entirely (saves ~50ms p50 per write). When ON, prior behavior preserved.

### Changed
- **P4.** Conflict-resolver env gate (`YADGAR_CONFLICT_RESOLVER`) hoisted to module-import time per invariant I3. When OFF, no `httpx.Client` constructed, no Ollama URL resolved, no module-level deps imported. Flag state frozen at import (I3 contract). When ON, client is built lazily on first call.

### Internal
- 12 new tests across 3 files (P4: 4, P7: 4, W1: 4) — all pass.
- Patterns Library entry CB-1 (circuit breaker) from v5.3.10 carries forward — no changes to ml_client.py in this minor.

## [5.3.10] - 2026-05-21

Hotfix bundle on top of v5.3.9. Two surgical fixes after v5.3.9 deploy surfaced a CPU busy-loop and a viz regression.

### Added
- **N4 circuit breaker on `RemoteMLClient`** — per-endpoint state machine (`ce`, `nli`, `pair`) with `CLOSED → OPEN → HALF_OPEN` transitions. After `YADGAR_CIRCUIT_BREAKER_FAILURE_THRESHOLD` (default 3) consecutive timeouts/errors on a `/rerank/<endpoint>`, the breaker OPENs and short-circuits subsequent calls to `None` for `YADGAR_CIRCUIT_BREAKER_OPEN_DURATION_SEC` (default 60s). Per-endpoint isolation so a slow CE doesn't disable NLI/pair. Gated by `YADGAR_CIRCUIT_BREAKER_ENABLED` (default 1). Forward-ported from v5.4 scope. Establishes Pattern CB-1 in `docs/ARCHITECTURE_INVARIANTS.md`.
- Disconnected-cluster sidebar nav in viz UI — BFS flood-fill identifies connected components of size ≥3 with no edge to the main cluster, lists them in a collapsible left panel with inferred labels, click flies the camera to the cluster centroid. Works in 2D + 3D. No DB changes.
- Zoom-to-fit-all on viz initial load (defensive) — surfaces periphery wikis on first render.

### Fixed
- **CPU fan continuous spinning post-v5.3.9 deploy** — v5.3.9 `BindsTo → Wants` removed the cascade-kill safety valve. Backend `/rerank` load spikes caused core to busy-loop retrying against a struggling backend instead of dying with it. N4 breaker breaks the loop: 3 consecutive failures → breaker opens → skip rerank 60s → backend recovers headroom. Recall degrades gracefully to BM25+HNSW results when breaker is open.
- **Meridian wiki pages invisible in viz** — 127 pages uploaded 2026-05-20 evening landed in DB but had zero edges to the main cluster. Force-directed layout ejected them to the periphery, invisible at default zoom. Sidebar nav + zoom-to-fit-all now surface them.

### Internal
- New env vars: `YADGAR_CIRCUIT_BREAKER_ENABLED` (1), `YADGAR_CIRCUIT_BREAKER_FAILURE_THRESHOLD` (3), `YADGAR_CIRCUIT_BREAKER_OPEN_DURATION_SEC` (60).
- 7 new circuit-breaker tests + 2 updated existing tests (zero-return → None semantics).

## [5.3.9] - 2026-05-20

Crash hotfix. Soak day 2026-05-20 surfaced a backend OOM cascade that took core down twice. v5.3.9 hardens the request path and durability boundary against backend transient failures, plus catches up on pre-existing operational debt.

### Added
- `YADGAR_BACKEND_HTTP_TIMEOUT_SEC` (default 5s) bounds all operational backend HTTP calls (ML rerank, dbsize, storage). (N1)
- `YADGAR_BACKEND_IMPORT_TIMEOUT_SEC` (default 300s) for vacuum `/import` and `/export`. (N1)
- `YADGAR_MIGRATION_HTTP_TIMEOUT_SEC` (default 30s) for `StorageEngine` schema setup calls. Separate from operational timeout to absorb migration-lock contention. (N1-fixup)
- `YADGAR_ASGI_SHUTDOWN_TIMEOUT_SEC` (default 5s) caps Uvicorn graceful shutdown — core no longer hangs 30s on stuck in-flight requests during backend-induced cascade. (N2)
- `docs/ARCHITECTURE_INVARIANTS.md` — codifies invariants I1–I15 + candidate plans P1–P12. Mirrored in wiki `yadgar-architectural-invariants`.
- `docs/PLAN_V5_4_to_v7.md` — locked v5.3.9 → v7 trajectory (advisor-audited).
- CHANGELOG backfilled for v5.1.5 through v5.3.7 (12 versions previously missing).

### Fixed
- **SubagentStop wallpaper** — lenient parser accepts heading variants (`## Yadgar Findings`, `## findings (Yadgar)`, `## Yadgar findings [agent:X]`, etc.) and the `agent_dispatch_prelude` contract now mandates the `## Yadgar findings` template at the end of every subagent message. Pre-fix capture rate was 1.6% (14/851).
- **N1** Backend HTTP timeouts bounded — see Added. Prevents thread starvation when backend goes unreachable.
- **N1-fixup** Separate migration HTTP timeout (see Added). Restores xdist test stability — `test_rules_engine_redos.py` was 9/10 fail under `-n 4`, now 10/10 pass. Also `func_only=True` on `@pytest.mark.timeout` for those tests so the 5s budget covers the regex check, not fixture setup.
- **N2** ASGI graceful shutdown ≤5s budget — see Added.
- SRI integrity hashes added to two unpinned CDN scripts in `yadgar/static/index.html` (three.js + 3d-force-graph).

### Operator action (host-side, manual)

- **systemd cascade decouple** — `BindsTo=yadgar-backend.service` on `yadgar.service` is the cascade-failure root cause from the 2026-05-20 backend OOM. Edit `~/git/nix/modules/home/yadgar.nix` to replace `BindsTo` + `Requires` with `Wants=yadgar-backend.service`. Verification command in `MIGRATION_NOTES.md`.
- **DLQ flush** — 16 stale `wiki_add` entries from 2026-05-18 with `schema_version_too_old`. Drop after deploy.

## [5.3.7] - 2026-05-20

### Added
- Semantic search box in viz UI — `/api/viz/search` endpoint + frontend search/clear controls with result pinning and pan-to-match. (V1)
- 2D/3D mode toggle in viz UI — localStorage-persisted, default 3D. (V4)

### Fixed
- Wiki node click now loads full content panel via `/api/wiki/read`. (V2)
- `db_size_mb` in server mode now returns the real value instead of zeros. (V5)
- Viz proxy read timeout raised to 60 s, fixing `/api/graph` 502 errors on large graphs. (V6)
- Click handler audit: universal fallback handler covers `memory`, `entity`, and future node types. (V3)

## [5.3.6] - 2026-05-20

### Added
- `FileChanged` hook: mirrors team-inbox writes and auto-memorizes `PLAN_*.md` files on save. (M1, Q4)
- `agent_dispatch_prelude` MCP tool — structured subagent dispatch helper with context injection. (M2)

## [5.3.5] - 2026-05-20

### Added
- `wiki_coverage()` MCP tool — reports wiki coverage percentage per module. (Q3)
- Token-estimate and cache-hit/miss counters in metrics. (Q1)
- Postmortem/incident tag score boost on action-verb queries. (Q2)

### Fixed
- `lru_cache` bypass in `main()` H-7 check; fixes `test_startup_fails_with_require_auth`. (Q5a)

## [5.3.4] - 2026-05-20

### Added
- Bi-temporal fact windows: `valid_from`/`valid_until` on KG edges — Zep parity. (C1, migration #007)
- LLM conflict-resolution ops on write: detect and resolve contradictory memories via LLM — Mem0 parity. (C4)

## [5.3.3] - 2026-05-20

### Added
- Citation tracing: KG edge provenance via `source_memory_id` — Zep parity. (C3, migration #006)
- Recall-frequency-modulated decay: memories recalled often decay slower — MemoryBank parity. (C2)

### Fixed
- `source_memory_id` write path added to `insert_typed_relationship`. (C3 bug)

## [5.3.2] - 2026-05-20

### Added
- `/hooks/instructions-loaded` and `/hooks/subagent-start` endpoints with `install-hooks` registration. (H1, SS)
- `HOOKS.md` documentation with ready-to-paste `settings.json` snippets.

## [5.3.1] - 2026-05-20

### Fixed
- `@_tool()` decorators missing from `agent_prompt_get`/`agent_prompt_save` — tools were not exposed via MCP after v5.3.0.

## [5.3.0] - 2026-05-20

### Added
- `provenance_agent` argument to `memorize` — tracks which subagent wrote a memory. (A1, migration #005)
- `SubagentStop` hook script, endpoint, and `install-hooks` extension. (A3)
- Agent-prompt versioning tools (`agent_prompt_get`/`agent_prompt_save`) with docs. (A4)
- `CLAUDE_SUBAGENT_CONTRACT.md` template and README subagent section. (A2)

### Fixed
- `provenance_agent` forwarded through file-queue drainer replay path.

## [5.2.0] - 2026-05-19

### Fixed
- SurrealQL injection: parameterized all raw string interpolations in `storage/ops.py`. (S1, H-4/H-5)
- `config.yaml` written with `0o600` permissions. (S2, H-9)
- ReDoS sandboxing: regex operations wrapped with timeout via `regex` library. (S3, H-6)
- Vacuum integration test: bootstrap-race wait applied to import-failure path. (S4)

### Docs
- `ARCHITECTURE_INVARIANTS.md` corrected module table, branch-boost description, and 2-container model. (S5)

## [5.1.9] - 2026-05-19

### Added
- Host-side branch hint injected into `SessionStart` hook context; eliminates branch-detection latency on session open.

## [5.1.8] - 2026-05-19

### Added
- `project_brief` anchor scope split, catalog enrichment with `_active_work` blending, branch fallback, and renderer restructure. (F1–F5)
- Docker image bloat fixed: added `.venv-test`, `.venv*`, `.claude/worktrees`, `result` to `.dockerignore`. (F7)

### Removed
- `get_project_context` deprecated alias removed (deprecated in v5.0.0; window long-expired). Use `project_brief(directory, mode='catalog')` instead. (F9)

## [5.1.7] - 2026-05-19

### Fixed
- Vacuum `check_invariants` POST now passes bearer token; previously exited with code 2 (false-failure) on every vacuum run.

## [5.1.6] - 2026-05-19

### Added
- `check-backend-bump` pre-commit hook: rejects commits that touch backend build inputs without bumping `backend_version`.

### Fixed
- `viz_server` broken `settings` import replaced with `get_settings()`. (B1)
- Vacuum health-check timeout raised from 60 s to 180 s to accommodate cold embedding-model warmup. (B2)
- `backend_version` bumped 5.0.1 → 5.0.2. (B3)

## [5.1.5] - 2026-05-19

### Fixed
- Vacuum re-DEFINE of `yadgar-rw`/`yadgar-ro` now actually executes post-import (v5.1.4 fix was merged but the re-DEFINE call was missing from the impl path).
- Viz bearer proxy: viz_server forwards `Authorization` header to the MCP daemon, fixing 401s when `YADGAR_MCP_AUTH_TOKEN` is set.

## [5.1.4] - 2026-05-18
> Vacuum properly recovers yadgar-rw / yadgar-ro after `/import`.

### Fixed
- **B1 `/import` wipes ROOT-level user definitions.** v5.1.3 V6 stripped `DEFINE USER` from the export based on a wrong hypothesis — online research confirmed SurrealDB exports redact users by default (surrealist#630), so the strip was a no-op. Empirically, after `/import` runs, `yadgar-rw` and `yadgar-ro` are gone. The `root` user survives only because the SurrealDB server re-bootstraps it from `SURREAL_USER/PASS` env on every start. DDL-defined non-root users are not durable across `/import` regardless of payload content.
  **Fix:** `cmd_vacuum_impl` now calls `_redefine_users_post_import(backend_url)` after `/import` returns 200, BEFORE starting yadgar core. Issues `DEFINE USER … ON ROOT PASSWORD … ROLES OWNER/VIEWER;` via root `/sql` with the JSON `vars` map pattern (mirroring `entrypoint-backend.sh`, avoiding SurrealQL parsing `yadgar-rw` as subtraction). Uses `YADGAR_RW_USER/PASS` + `YADGAR_RO_USER/PASS` from env; raises `RuntimeError` if either password is missing.

### Changed
- **B2 integration test hardened.** `test_vacuum_e2e_happy_path` now polls `_wait_for_yadgar_rw_auth` BEFORE vacuum (fails loudly if backend bootstrap incomplete) and UNCONDITIONALLY asserts post-vacuum auth (no `if rw_pre_ok:` conditional-skip). This is what should have caught v5.1.3 V6 broken before deploy.
- **B3 strip docstring** clarifies the strip step is defensive (in case a future SurrealDB version exports users); the real recovery mechanism is the B1 re-DEFINE.

### Known follow-ups (v5.x backlog)
- Vacuum health-check timeout (60s) for yadgar core restart — embedding-model warmup on cold starts can exceed it. V5 (v5.1.3) makes the resulting `WARNING` message accurate but real slow-starts would still surface as failures. Consider 180s timeout OR `/health` returning 200 before embedding warmup completes.

### Companion nix changes
None required. Image rebuild + `yadger_core_version` 5.1.3 → 5.1.4 bump only.

## [5.1.3] - 2026-05-18
> Vacuum hardening — fixes two bugs surfaced during v5.1.2 deploy.

### Fixed
- **V5 `_wait_for_yadgar_health` polls `/healthz`** — yadgar exposes `/health` (no z). The 60s poll never returned 200, vacuum exited with `WARNING: yadgar did not become healthy. Bloated dir retained: ...` and `status=2/INVALIDARGUMENT` even on successful runs (phases 1-3 completed). One-char rename in `yadgar/vacuum/__init__.py:114`. Regression test `TestWaitForYadgarHealth::test_polls_health_not_healthz` pins the URL.
- **V6 `/import` wipes `DEFINE USER` statements**. v5.1.2 vacuum saved 92% on first deploy (1161 MB → 92 MB) but yadgar core then crashloop-401'd because the import payload included `DEFINE USER yadgar-rw ON ROOT PASSWORD '<old_hash>' ROLES OWNER;` from the source DB — overwriting the freshly-bootstrapped users from `yadgar-backend`'s entrypoint (whose hashes matched `secrets.env`). Operator recovery required curling `DEFINE USER yadgar-rw … ROLES OWNER;` as root.
  **Fix:** extend `yadgar/vacuum/strip.py` with `strip_export_for_vacuum()` that also strips `DEFINE USER … ON {ROOT,NS,DB}`, `DEFINE ACCESS …`, `REMOVE USER …` at SQL-statement granularity (start-of-line anchored, must not eat memory content mentioning "DEFINE USER" in body text). `strip_action_log` retained as back-compat alias.
- **vacuum/_vacuum_export action_log strip regex** was over-greedy — replaced `\Z` terminator with blank-line terminator so the stripped section doesn't accidentally swallow subsequent statements.

### Changed
- Test infrastructure: `yadgar/tests/integration/test_vacuum_e2e.py` extended with post-vacuum yadgar-rw auth assertion (regression-prevention for V6). Fires when the pre-vacuum probe succeeds. **Caveat:** the upstream `openfantasy/yadgar-backend:5.0.1` entrypoint has a bootstrap race that occasionally leaves `yadgar-rw` un-auth-ready before the test exercises it — the assertion conditionally skips in that path. Tracked as v5.2 fixture-hardening (poll for user-ready before vacuum, fail loudly if absent).

### Known follow-ups (v5.x backlog)
- Vacuum health-check timeout for yadgar core restart is 60s. Embedding-model warmup on cold starts can exceed that. V5 makes the resulting message accurate (no longer a `/healthz` 404), but real slow-starts would surface as actual failures. Consider bumping the timeout to 180s or making `/health` return 200 as soon as the HTTP server binds (before embedding warmup completes).
- Integration test conditional-skip on yadgar-rw bootstrap race — fixture should wait for user-ready, not gate the assertion on a probe result.

### Companion nix changes
None required. Image rebuild + `yadger_core_version` 5.1.2 → 5.1.3 bump only.

## [5.1.2] - 2026-05-18
> Vacuum proper-fix + install_hooks containerization + end-to-end integration test.

### Fixed
- **V1 vacuum admin creds.** `yadgar/vacuum/_build_http_client` and `_surreal_headers` now read `SURREAL_USER`/`SURREAL_PASS` first (root, for SurrealDB IAM admin endpoints), fall back to `YADGAR_DB_USER`/`YADGAR_DB_PASS`, then `root`/`root` default. The previous v5.1.0 + v5.1.1 behavior used the yadgar-rw role for `/import` which got HTTP 403 "Not enough permissions". Removes the need for the nix bash-wrapper workaround on `yadgar-vacuum.service` ExecStart.
- **V2 vacuum fail-safe phase ordering.** Phase 2 no longer renames `surreal_db` → `.bloated-<ts>` before `/import` succeeds. New order: snapshot (copy), stop backend, rename to `.bloated-<ts>.tmp`, restart backend, POST `/import`. On 200: rename `.tmp` → final `.bloated-<ts>`. On non-200: restore original via `shutil.rmtree(surreal_db) + rename .tmp → surreal_db`, restart backend, exit non-zero. The previous v5.1.0 + v5.1.1 path renamed eagerly — when `/import` failed (as it did on every v5.1.1 deploy with the 403), the live DB was the fresh-empty one and yadgar required manual operator rollback.
- **vacuum/_restore_db EEXIST.** When SurrealDB pre-creates `surreal_db/` before `/import` fails, the restore rename hit `EEXIST`. Fixed by `shutil.rmtree(db_path)` before rename. Caught by V3.
- **vacuum/_import namespace bootstrap.** `/import` was silently returning HTTP 200 but importing nothing because the `yadgar` namespace did not exist on the fresh DB. Vacuum now bootstraps ns/db via `/sql` before calling `/import`. Caught by V3.

### Changed
- **V4 `vacuum_now()` MCP tool returns actionable structured response** when no service manager is reachable (the normal case when called from inside the daemon container). New fields: `skipped_reason="requires_host_systemctl"`, `host_command="systemctl --user start yadgar-vacuum.service"`, `fallback_host_command="yadgar vacuum --service-mode=systemd --yes"`, `detail` explaining the host-vs-container split. Preserves the legacy `shell_command` field for backward compat.
- **H1 `install_hooks` containerization.** Tool now detects container mode (`YADGAR_IN_CONTAINER=1` env — explicit opt-in) and refuses with a structured response pointing to the host-side path, instead of silently writing to the container's `/root/.claude/settings.json` (the long-running deployment bug for non-nix users where hook scripts ended up with no bearer token → 401 → `Hook JSON output validation failed`). New `yadgar install-hooks` CLI subcommand (host-side via pipx) writes to the invoking user's `$HOME/.claude/settings.json`. Shared install logic lives in `yadgar/install_hooks_lib.py`. The `/.dockerenv` probe was dropped after CI regression — Forgejo Actions runner containers have that file present, causing 4 test_server.py tests to receive `refused` instead of `installed`. The nix module and docker-compose set `YADGAR_IN_CONTAINER=1` explicitly on the yadgar core service ExecStart; CI must not set it.

### Added
- **V3 end-to-end vacuum integration test.** `yadgar/tests/integration/test_vacuum_e2e.py` spawns a real `yadgar-backend` container, populates ~100 memories, runs `yadgar vacuum` as a host subprocess, asserts `before_bytes > after_bytes` + DB intact. Second test forces `/import` to 403 via read-only creds and asserts V2's restore-on-failure path keeps the original DB usable. New pytest marker `integration` (opt-in; default test run skips via `addopts='-m "not integration"'`). Caught the two additional bugs landed in V2 (EEXIST + namespace bootstrap).

### Companion nix changes (out-of-repo, already on master ~/git/nix)
- `b7eb004` `--db-path /data/surreal_db` workaround on yadgar-vacuum.service — REVERTABLE after v5.1.2 image deploys (env-default in V1 covers it).
- `1f2f1a6` `--backend-url http://yadgar-backend:8000` workaround — REVERTABLE after v5.1.2.
- v5.1.1 bash-wrapper mapping `YADGAR_DB_USER=$SURREAL_USER` on the vacuum ExecStart — REVERTABLE after v5.1.2.
- Keep: `BindsTo=yadgar-backend.service` (`2773e4c`), backend `-p 127.0.0.1:8000:8000` exposure, host pipx vacuum invocation (`f45e7ec`), backend `-e YADGAR_MCP_AUTH_TOKEN` passthrough (`61bf5f5`), `claude-code.nix` token injection (`16dd962`).

## [5.1.1] - 2026-05-17
> Hotfix follow-up to v5.1.0 — ops bugs surfaced during deploy.

### Fixed
- **viz_server bind interface.** `yadgar/server/lifecycle.py` auto-started the viz thread without passing `host=`, so viz_server defaulted to container `127.0.0.1`. Host docker port mapping `-p 127.0.0.1:42069:42069` then couldn't forward — viz UI unreachable. Fix: pass `settings.HOST` (already `0.0.0.0` in container via `YADGAR_HOST` env var). Security default `127.0.0.1` preserved for non-container runs.
- **vacuum CLI env-aware defaults.** `cli/vacuum.py` `--db-path` defaulted to `~/.yadgar/surreal_db` (= `/home/yadgar/.yadgar/surreal_db` inside container) and `--backend-url` defaulted to `http://127.0.0.1:8080` (wrong port; backend is on 8000). Fix: argparse defaults now read `YADGAR_DATA_DIR` (+ `/surreal_db`) and `YADGAR_DB_URL` — matching the pattern `YADGAR_DB_USER/PASS` already follow. Removes the need for the nix module to repeat those flags on the systemd ExecStart.
- **vacuum export missing SurrealDB namespace + auth.** `vacuum/phases._vacuum_export` issued a bare `httpx.get(/export)` without `surreal-ns`/`surreal-db` headers or basic-auth. SurrealDB v2+ rejects with HTTP 400 "Specify a namespace". Fix: use the existing `_build_http_client(backend_url)` from `vacuum/__init__.py` which sets the headers and auth. Drops unused `import httpx` in `phases.py`.

### Known follow-ups (v5.2)
- **Integration test gap for vacuum** (P1). No end-to-end test exercises vacuum against a live yadgar-backend. All three bugs above were silent for v5.0 + v5.1 because unit tests mocked the HTTP/CLI layer. A `pytest -m integration` test that spins up the backend container, runs `yadgar vacuum --service-mode=manual`, and asserts `before_bytes > after_bytes` would have caught every one of them.
- **`install_hooks` MCP tool is broken for containerized deployments** (P1). The tool runs inside the yadgar docker container and writes `/root/.claude/settings.json` (container's `$HOME`) instead of the host's `~/.claude/settings.json`. Result for non-nix users: hooks never get the bearer token; PostToolUse/UserPromptSubmit hook scripts read empty `YADGAR_MCP_AUTH_TOKEN`, build bare bearer headers, get HTTP 401 from the daemon, exit non-zero with non-JSON stderr → Claude Code reports `Hook JSON output validation failed`. Nix users worked around by injecting the token via home-manager activation (see `~/git/nix/modules/home/claude-code.nix` `claudeCodeSettings`). Real fix options: (a) `yadgar install-hooks` host-side CLI installed via pipx editable; (b) HTTP endpoint that returns the settings.json snippet for the host to write; (c) detect container mode and emit a clear "run on host" error.

### Companion nix changes (already deployed)
- `~/git/nix/modules/home/yadgar.nix` master commits `61bf5f5` (backend `-e YADGAR_MCP_AUTH_TOKEN` passthrough — required because the storage client A1 fix sends the bearer but the backend endpoint had no token to compare against), `b7eb004` (vacuum `--db-path /data/surreal_db`), `1f2f1a6` (vacuum `--backend-url http://yadgar-backend:8000`). The latter two can be reverted once a v5.1.1 image is deployed because the CLI now reads the env vars directly.

## [5.1.0] - 2026-05-17
> Module decomposition + ops fixes + retrieval polish. 17 sub-branches integrated; FastMCP API, CLI, and storage public APIs preserved byte-identical via re-exports.

### Fixed
- `Storage.get_db_size()` server-mode path now sends `Authorization: Bearer <token>` to `/admin/dbsize`. v5.0.0 added bearer-auth to the endpoint but the client never passed the token, so `memory_stats.db_size` returned hardcoded zeros and silently disabled the `vacuum_now()` threshold gate + `DB_SIZE_WARNING_BYTES` nag. (v5.1 A1)
- Causal-discovery dispatch: `_consolidation_cycle` now accumulates `action_memories_created + cls_promoted + memify_derived` into `_events_since_last_discovery` AFTER the memory-producing phases. Gate was previously dead because `stats["memories_added"]` was never wired, so `pc_algorithm` never fired and `memory_stats.causal_edges` stayed 0. (v5.1 C1)
- Companion nix-module fix for `yadgar-vacuum.service` ExecStart: prefix CMD with `yadgar`, add `--yes`, pass `YADGAR_DB_USER/PASS` + `YADGAR_MCP_AUTH_TOKEN`. Service had been failing exit 127 since v4.8.3 — never ran successfully on schedule. Lands in the nix repo, not the yadgar container. (v5.1 A2)

### Changed
- Branch retrieval filter pushed from Python post-fetch into the SurrealQL `WHERE` clause; cuts wasted-row work on the hot recall path. (v5.1 C2)
- `_detect_branch` LRU cache bucket jittered per directory (`hash(directory) % 30`); removes the every-30s thundering-herd against `git symbolic-ref`. (v5.1 C3)
- Branch-match boost replaced hard `* 1.5` with convex combination `score + (1 - score) * BRANCH_BOOST_WEIGHT` (default 0.2); final scores stay in [0,1]. Boost-base also clamped to 1.0 to prevent inversion when WRRF emits scores > 1.0. (v5.1 C4)
- Temporal retrieval (`search_memories_by_content_date`, `search_memories_by_month`) accepts `branch_filter`; plugs an other-branch leak when `TEMPORAL_RETRIEVAL_ENABLED=True`. (v5.1 C4 follow-up)
- `RulesEngine.get_applicable_rules(directory)` now cached per-directory in an instance-level dict; first call issues 3 DB queries (global + directory + file scopes), subsequent calls bypass. `add_rule`/`delete_rule` clear the cache atomically. Removes the 3-queries-per-`memorize` overhead blocking the <5ms async-path target. (v5.1 C5)

### Refactored — module decomposition (v5.1 B)
- `yadgar/storage.py` (3742 LoC) → `yadgar/storage/` subpackage with 16 mixin modules.
- `yadgar/server.py` (4353 LoC) → `yadgar/server/` subpackage: `_app`, `_state`, `_helpers`, `lifecycle`, `http` + `tools/{memorize, recall, wiki, project, misc}` + `tools/admin_*`. FastMCP `_tool` registry preserved.
- `yadgar/__main__.py` (1354 LoC) → 136-LoC shim + `yadgar/cli/<subcommand>.py` per-command modules. `python -m yadgar <cmd> --help` byte-identical.
- `yadgar/retrieval/core.py` (1129 LoC) → 406-LoC + sibling mixins (`scoring`, `graph_helpers`, `quality`); extended `fusion`/`reranking`. `yadgar/retrieval/reranking.py` (701) → 205 assembly + 6 per-strategy mixins.
- `yadgar/consolidation.py` (1084 LoC) → `consolidation/` subpackage: `heat_decay`, `cls`, `causal`, `cleanup`, `orchestrator`. v5.1 C1 dispatch + v5.0 phase markers preserved.
- `yadgar/seed.py` (1041) → `seed/{_scan, _analysis, _generate}`.
- `yadgar/curation.py` (727) → `curation/{ingestion, prune_passes, strengthen, contradiction}`.
- `yadgar/causal_discovery.py` (602) → `causal_discovery/{pc, meek, independence, dag_io}` — finishes v5.0 partial split.
- `yadgar/vacuum.py` (555), `yadgar/file_queue.py` (548), `yadgar/sleep_compute.py` (522), `yadgar/cls_store.py` (776), `yadgar/enrichment.py` (690), `yadgar/metacognition.py` (579), `yadgar/server/tools/admin.py` (1103) — all split per audit-tier roadmap.
- Files left intact with `# Module size justified` annotations: `config_yaml.py`, `daemon.py`, `predictive_coding.py`, `server/tools/project.py`, `storage/client.py`, `server/tools/misc.py`, `cli/stats.py`, `server/http.py`, `server/tools/admin_invariants.py`. Each is single-responsibility per audit.

### Known follow-ups (v5.1.x / v5.2)
- 26 complexity-15+ functions remain (down from 70+ pre-B). Per roadmap each ships as its own PR with a characterization test. Largest: `_run_check_invariants` (85, justified), `memorize` (56), `cmd_stats` (42), `_memify_prune` (40), `_format_restoration` (37), `pc_algorithm` (37).

## [5.0.1] - 2026-05-16
> Snapshot 2026-05-16: 1 509 memories (550 active · 959 archived) · avg heat 0.164 · anchor/wiki/CLS counts require direct DB access

### Fixed
- Docker build: pin base image to `python:3.14-slim-trixie` (Debian 13 explicit), drop bookworm-era curl version pin that failed on the moved base. (#65)

## [5.0.0] - 2026-05-16

### Added
- Bearer-token MCP auth middleware on `/api/*`, `/hooks/*`, `/mcp`; `hmac.compare_digest` timing-safe compare; loopback-only binding for all services by default. (#64)
- Default-deny CORS (loopback origins only); configurable via `YADGAR_ALLOWED_ORIGINS`. (#64)
- `project_brief(mode)` layered session bootstrap: catalog mode (~500 tokens, default) surfaces `_project_init` table-of-contents memory, anchors, and signals; full mode (~1050 tokens) adds `_active_work`, hot memories, and git snapshot. Replaces `get_project_context`. (#64)
- `bootstrap_project` and `update_active_work` MCP tools for atomic project-init and active-work memory management. (#64)
- `wiki_refresh_stale` and `wiki_cleanup` MCP tools for stale-page refresh and merged-branch cleanup. (#64)
- Branch-aware retrieval: auto-captures current git branch on every write; 1.5× boost applied to current-branch matches in recall; wiki blending honours branch filter. Schema migration 004 adds `branch` column. (#64)
- `session_context` endpoint that pipes `project_brief._render` for session startup. (#64)
- Stop-hook signal-eval checkpoint every 25 messages, dispatching stale-wiki and active-work evaluation. (#64)
- Queue drainer payload validation (schema-version check on drain). (#64)
- `memory_get(id)` and `wiki_get(id)` read-only MCP tools; `memory_update(id, fields)` and `wiki_update(id, fields)` mutation tools. (#64)
- Prometheus `/metrics` endpoint (loopback only, bearer-token gated); structured JSON logging behind `YADGAR_LOG_FORMAT=json`; consolidation phase `phase_start`/`phase_end` log markers. (#64)
- Wiki backup loop in backend: snapshot every 6 hours, 14-day retention. (#64)
- Release-readiness CI gate: rejects PRs that touch `yadgar/` code without a version bump (suppressible with `no-release` label). `docs/RELEASE.md` runbook added. (#62, #64)
- Secret pattern coverage for AWS, GCP service-account JSON, Stripe, Slack, OpenAI, Anthropic API keys, JWT, PATs, private keys, and DB URIs in `secrets.py`. (#64)
- `_project_init` memory pattern: one protected memory per directory acting as project table-of-contents; `seed_project` drafts a starter from README + top-level docs. (#64)
- HEALTHCHECK directives in both Dockerfiles. (#64)
- Characterization tests for `recall` and `pc_algorithm` pinning decomposed behavior. (#64)
- Auto-generate bearer token and DB password at first run; write to `~/.yadgar/secrets.env` (chmod 600). (#64)

### Changed
- `get_project_context` renamed to `project_brief`; old name kept as deprecated alias for one release. (#64)
- `YADGAR_HOST` defaults to `127.0.0.1` everywhere; external exposure requires explicit env opt-in. (#64)
- `entrypoint-backend.sh` curl DB calls switch from `-u user:pass` flag to `Authorization` header (avoids credential exposure in `/proc`). (#64)
- `install_hooks` writes a real hook runner script (`yadgar/scripts/hook_runner.py`) referenced by absolute path; eliminates shell injection via project directory interpolation. (#64)
- Credentials: `root:root` fallback removed from all code paths; startup fails hard if `YADGAR_DB_PASS`/`YADGAR_MCP_AUTH_TOKEN` unset (escape hatch: `YADGAR_ALLOW_ROOT=1` for tests). (#64)
- systemd `EnvironmentFile` used for secrets instead of inline interpolation into unit file. (#64)
- `recreate_vector_table` wrapped in `BEGIN TRANSACTION … COMMIT TRANSACTION` with pre-flight embedding backup. (#64)
- `_memify_derive`, `insert_checkpoint`, `insert_profile`, `upsert_file_hash`, `replace_wiki_crossrefs` now execute inside single transactions. (#64)
- `re-seed` builds new memories before deleting old ones; partial-failure can no longer leave DB empty. (#64)
- `recall` and `pc_algorithm` mega-functions decomposed into named helpers (behavior pinned by characterization tests). (#64)
- `wiki_list` filter and limit pushed into SurrealQL (`LIMIT`, `WHERE category`, `string::starts_with`). (#64)
- CVE bumps: `python-multipart` 0.0.26→0.0.27 (CVE-2026-42561, HIGH), `urllib3` ≥2.7.0 (CVE-2026-44431/44432, HIGH), `pytest` 9.0.2→9.0.3 (CVE-2025-71176, MEDIUM). (#64)
- `ruff target-version` bumped from `py311` to `py314`. (#64)
- Stale `requirements.txt` deleted (repo uses `uv`). (#64)

### Fixed
- Meek R2 directed-edge orientation bug in `causal_discovery.py` that corrupted all persisted causal DAGs. (#64)
- Meek R3 missing non-adjacency precondition added. (#64)
- `build_event_matrix` substring false-positives replaced with word-boundary `re.search`. (#64)
- GTE reranker respects `GTE_RERANKER_FALLBACK_TO_FLASHRANK=False` (was silently overriding). (#64)
- `sleep_compute.py` lambda closure captured `dir_counts` by reference; fixed with default-arg capture. (#64)
- Daily 18:30 UTC consolidation cycle skipped when `DAEMON_CHECK_INTERVAL > 60s`; switch to range comparison with "fired today" guard. (#64)
- `rules_engine.py` parse error on hard rules now returns `False` instead of `True` (hard filter silently disabled). (#64)
- `_bytes_to_floats` validates length alignment and dimension before use. (#64)
- `asyncio.to_thread` wrapping for blocking SurrealDB calls in auto-capture endpoint. (#64)
- `_action_batch` protected by `asyncio.Lock`; `_system_metrics_cache` by `threading.Lock`. (#64)
- Atomic `tmp + os.replace` writes for `settings.json` and `CLAUDE.md` (previously truncation-on-crash risk). (#64)
- 26 None-dereference crash sites fixed: required engines raise `RuntimeError` at startup; optional enrichment/reranking engines skipped behind `_enabled` flags. (#64)
- Bare `except Exception: pass` replaced with `log.warning(…, exc_info=True)` at all silent-swallow sites. (#64)
- `_run_migrations` serialised under filesystem `flock` so concurrent daemon starts can't race. (#64)
- `init_engram_slots` float/int comparison fixed; no longer re-inserts all 5000 slots on every restart. (#64)
- Double-shutdown guard (`_shutdown_done`) prevents double-call of `shutdown()` from signal handler and `main()` finally. (#64)
- `cmd_context` wraps `StorageEngine` in `try/finally` to release SurrealKV lock file on exit. (#64)
- `dlq_requeue` filename validation rejects null bytes and Unicode separators. (#64)
- XSS fix in `static/index.html`: escape before syntax highlighting instead of direct `innerHTML` assignment. (#64)
- Embed service `/admin/*` endpoints gated by bearer token; `/embed` input bounded and bound to loopback. (#64)
- Memorize file-hash path traversal closed: only hashes files under registered project roots; hash stripped from `/api/graph` payload; large files stream-hashed in 64 KB chunks. (#64)
- `_q` retry restricted to read-only statements; write retries no longer cause double-inserts. (#64)
- `batch_writes` regex param substitution replaced with proper tokenizer to prevent corruption when content contains `$id`/`$content`. (#64)

### Security
- MCP API bearer-token auth closes wildcard-CORS + no-auth combination that allowed any web page to read or mutate the full memory graph. (#64)
- Loopback-only binding for all service ports by default. (#64)

## [4.9.0] - 2026-05-15

### Added
- `vacuum_now()` MCP tool (`power=True`): trigger SurrealKV vacuum on-demand; refuses if DB below 200 MiB threshold (overridable with `force=True`), service already running, or no supported service manager. (#58)
- Threshold auto-trigger: consolidation cycle fires vacuum when DB exceeds `VACUUM_AUTO_THRESHOLD_BYTES` (default 2 GiB) within the `19:00–23:00` window, with 6-hour cooldown. (#58)
- `caused_by` edge auto-repair in `check_invariants`: detects and deletes dangling edges; row ceiling via `MAX_CAUSED_BY_ROWS` (default 100 000). (#59)
- Per-table size estimate in `check_invariants` telemetry: row count and estimated bytes per table, surfaced in `memory_stats`. (#59)
- Degenerate CLS pattern guard: `find_recurring_patterns` skips patterns whose extracted body is under 20 characters or contains only stop-words. (#60)
- Test DB isolation guard in `conftest.py`: raises at collection time if `YADGAR_DB_URL` resolves to the production path. (#57)

### Fixed
- `test_repeated_cooccurrence_increases_weight`: batched UPSERT was resetting weight to 1.0 instead of accumulating; switched to `SET weight += $delta` with pre-aggregated batch. (#57)
- CI: `[ml]` extra installed in test environment so embedding-dependent tests run. (#57)

## [4.8.3] - 2026-05-14

### Fixed
- Auto-repair for dangling `memory_transition` rows (referencing deleted memory IDs) now runs in `check_invariants`. (#55)
- Engram slot rebalancer added to handle partition drift after bulk operations. (#55)

## [4.8.2] - 2026-05-14

### Changed
- Reranker ML dependencies moved from core image to backend via `MLClient` dependency injection; core container no longer requires `sentence_transformers` at runtime. (#54)

## [4.8.1] - 2026-05-14

### Fixed
- `_memify_derive` 413 errors: chunked batches by `MAX_BATCH_STATEMENTS` and `MAX_BATCH_BYTES`. (#53)
- `check_invariants` DB-size query now uses server-mode HTTP path instead of embedded-mode file scan. (#53)
- `memory_transition` invariant violations downgraded from CRITICAL to WARN log level (non-repairable but non-urgent). (#53)

## [4.8.0] - 2026-05-14

### Added
- `yadgar vacuum` CLI rewritten to mirror the manual SurrealKV rebuild procedure: export via HTTP, strip `action_log`, snapshot DB dir, drop and reimport, restart daemons. (#51)
- DB-size telemetry in `check_invariants`: `db_size_bytes`, `vlog_size_bytes`, `sstables_size_bytes`, `wal_size_bytes`, `vlog_pct_of_total`, `size_warning` flag. (#51)
- nix systemd timer `yadgar-vacuum.timer`: fires Sundays at 04:00 UTC with 30-minute randomised delay. (#51)
- Time-bound backup retention: age cap (`YADGAR_BACKUP_MAX_AGE_DAYS`, default 7) and size cap combined with existing count cap of 7 snapshots. (#51)
- `check_invariants` auto-repair for `memory_similarity_link` dangling edges. (#49)
- `install_hooks` extended to global scope. (#49)
- Core and backend versions tracked separately (`server.json:backend_version`); `YADGAR_LOG_LEVEL` config setting. (#50)

### Fixed
- Consolidation cycle cooldown: idle-triggered cycles fire at most once per `CONSOLIDATION_COOLDOWN_SECONDS` (default 30 min), ending fan spin-up/spin-down loops during laptop idle. (#51)
- Engram slot collapse regression fixed. (#44)
- Backend `/export` backup loop dropped (caused duplicate work with host-side snapshot); worker stack size raised. (#43)

## [4.5.0] - 2026-05-12

### Fixed
- Stabilization batch: 9 audit-identified landmines fixed (off-by-one bounds, bare-except swallows, unsafe concurrent dict access). (#45)
- `check_invariants` MCP tool added for on-demand DB integrity check and auto-repair. (#45)
- CI xdist OOM and flaky test retry logic. (#46)

## [4.4.10] - 2026-05-09

### Fixed
- O(N²) per-pair relationship scan in `consolidation` replaced with bulk SQL; consolidation cycle time reduced. (#40)

## [4.4.9] - 2026-05-09

### Security
- DB users bootstrapped `ON ROOT` for HTTP Basic auth compatibility with SurrealDB v3. (#39)

## [4.4.8] - 2026-05-09

### Fixed
- O(N²) per-pair HTTP scan in `memify` replaced with bulk SQL. (#38)

## [4.4.7] - 2026-05-09

### Security
- Three-tier DB user model: separate `rw_user`, `ro_user`, and root; credentials sourced from 1Password. (#37)

## [4.4.6] - 2026-05-09

### Changed
- Logging refactor: phase markers, unbuffered stdout, `propagate=False` on all loggers. (#36)

## [4.4.5] - 2026-05-09

### Added
- `YADGAR_LOG_LEVEL` env flag for opt-in INFO/DEBUG logging. (#35)

## [4.4.4] - 2026-05-09

### Fixed
- Remaining consolidation phases batched into fewer transactions. (#34)

## [4.4.3] - 2026-05-09

### Fixed
- Consolidation writes batched into a single transaction per phase; substantially reduces DB round-trips. (#33)

## [4.4.2] - 2026-05-09

### Fixed
- Entity-table bloat: similarity links routed to a dedicated `memory_similarity_link` table instead of the entity table. (#32)

## [4.4.1] - 2026-05-09

### Fixed
- Relationship indexes added; daily consolidation rescheduled to 18:30 UTC. (#30)

## [4.4.0] - 2026-05-08

### Added
- `memorize` and write operations are now truly async: enqueue immediately, return success without waiting for DB write. (#25)
- Write-gate skip: duplicate detection skips the write instead of queuing it twice. (#25)
- CI pipeline time reduced from 60 to 14 minutes via parallelisation and caching. (#25)

### Fixed
- Robustness improvements across queue drainer and consolidation. (#25)

## [4.3.0] - 2026-05-08

### Added
- Dead-letter queue (DLQ): failed queue entries moved to `~/.yadgar/dlq/` with `.error.json` sidecar after retry exhaustion. (#24)
- Per-file retry policy with exponential backoff; permanent vs transient failure classification. (#24)
- Nightly backup schedule for wiki pages. (#24)

### Fixed
- `wiki_add` SQL injection: markdown content no longer string-interpolated into SurrealQL; parameterised via proper escaping. (#24)

## [4.1.3] - 2026-05-03

### Fixed
- SurrealDB v3 compatibility: `type::thing` → `type::record`, FULLTEXT ANALYZER syntax, KNN operator `<|K, EF|>` form. (#23)
- Wiki durability: writes go through the file queue with dated archive directories. (#23)

## [4.1.2] - 2026-05-02

### Fixed
- Wiki writes are now truly async via queue; previously blocked the MCP response.
- Archive directories dated by day to prevent filename collisions.

## [4.1.0] - 2026-05-02

### Changed
- Wiki mirror collapsed from separate `wiki/` directory into `archive/wiki/` unified with the file queue archive.

## [4.0.5] - 2026-05-02

### Fixed
- Startup hang: `init_engram_slots` switched to bulk INSERT; previous row-by-row loop blocked the server for minutes on a populated DB.

## [4.0.3] - 2026-05-02

### Fixed
- Multi-field FULLTEXT indexes split into single-field definitions for SurrealDB v3 compatibility.

## [4.0.2] - 2026-05-02

### Fixed
- Version label corrected in pyproject.toml after Dockerfile label bump landed first.

## [4.0.1] - 2026-05-02

### Fixed
- Dockerfile labels updated to 4.0.1; Docker image tagging corrected to use exact version tags for both core and backend images. (#22)

## [4.0.0] - 2026-05-01

### Added
- Two-container split: backend (`SurrealDB` + embedding model + `/embed` endpoint) and core (MCP server, APIs, viz). (#21)
- HNSW vector indexes replacing MTREE; eliminates the MTREE corruption class entirely. (#21)
- File-based write queue (`~/.yadgar/queue/` → `archive/`) as the durable async write path. (#21)
- 3D knowledge-graph visualization via `react-force-graph-3d` (Three.js/WebGL). (#21)
- DB container network isolation: SurrealDB port not exposed to host; core-only private network. (#21)
- SurrealDB upgraded to v3.x (from v2.3.5). (#21)

### Changed
- `yadgar` package moves all heavy ML compute (embedding model, consolidation) to the backend container; core container restarts in ~2–3 s. (#21)

## [3.1.0] - 2026-04-26

### Added
- Docker-only deployment mode: `yadgar-backend` + `yadgar` containers managed by Docker Compose; no local Python install required. (#2)
- SurrealDB server mode: backend container runs `surreal` as a subprocess; Yadgar connects over HTTP. (#2)

### Fixed
- Visualization node ID extraction for string-keyed SurrealDB records. (#16)
- SurrealDB WebSocket reconnect on transient disconnect. (#14)

## [3.0.0] - 2026-04-25

### Added
- Portability and packaging: `yadgar` installable as a Python package; `yadgar` CLI entry point.
- `--host` flag and `YADGAR_HOST` env var for container-friendly binding.
- Default MCP port changed to 8765.

### Changed
- Forgejo CI workflow: pre-commit on PRs; multi-arch Docker build on push/tag.

## [2.1.0] - 2026-04-25

### Changed
- Python 3.14 required (drops 3.12/3.13 support).
- Package description updated.

## [2.0.0] - 2026-04-25

### Added
- Wiki/KB subsystem (Phase 3 MVP): `WikiStore`, 7 MCP tools (`wiki_add`, `wiki_query`, `wiki_read`, `wiki_list`, `wiki_approve`, `wiki_delete`, `wiki_ingest`). Wiki pages stored in SurrealDB and mirrored to `~/.yadgar/wiki/`.
- Wiki integration into recall: relevance-gated blending of wiki results into memory recall pipeline, bidirectional memory↔wiki links, episodic query detection.
- Rules Engine write-path policy: secret detection always-on, user-configurable write rules, read-path filtering.
- Retrieval profiles: minimal and full profiles configurable per session.
- Memory-only knowledge-graph visualization: semantic, temporal, and transition edges; top-K semantic edges, community detection, stats panels.
- Wiki end-to-end tests: 40 tests across 10 test classes.
- `vacuum` command to compact SurrealKV commit log.
- Stop-hook checkpoint interval raised from 15 to 25 messages.
- Pre-commit hooks: gitleaks, ruff lint+format.
- PID file self-registration so systemd-started daemons are trackable.
- CPU cap: 50% sustained / 66% burst via `resource.setrlimit`.
- Zombie-session fix: 90%+ CPU daemon loop resolved.
- YAML config management and daily consolidation schedule at 18:30 UTC.
- Daemon mode and stop-hook integration.
- 10 dead tools removed; remaining tools tiered into core and power profiles.
- `__version__` derived from package metadata (`importlib.metadata`).

### Changed
- Retrieval signals pruned: fractal, HDC, and Hopfield signals removed; FTS re-enabled; `retrieval.py` split into package.
- `AstrocyteEngine` → `ConsolidationScheduler`, `SensoryBuffer` → `ActionLogger`, `HippoRetriever` → `Retriever`, and other internal bio-metaphor renames.
- Reconsolidation and compression disabled (silently corrupted content; memory preserved verbatim).
- Suite speedup: 12.6× faster test run via parallelisation and fixture isolation.

### Fixed
- Forked from Zikkaron; all Zikkaron-specific DB schemas and modules replaced with SurrealDB-only backend.

[unreleased]: https://codeberg.org/maxagahi/yadgar/compare/v5.17.0...HEAD
[5.17.0]: https://codeberg.org/maxagahi/yadgar/compare/v5.15.0...v5.17.0
[5.15.0]: https://codeberg.org/maxagahi/yadgar/compare/v5.13.1...v5.15.0
[5.13.1]: https://codeberg.org/maxagahi/yadgar/compare/v5.13.0...v5.13.1
[5.13.0]: https://codeberg.org/maxagahi/yadgar/compare/v5.11.0...v5.13.0
[5.11.0]: https://codeberg.org/maxagahi/yadgar/compare/v5.10.11...v5.11.0
[5.10.11]: https://codeberg.org/maxagahi/yadgar/compare/v5.10.10...v5.10.11
[5.10.10]: https://codeberg.org/maxagahi/yadgar/compare/v5.10.9...v5.10.10
[5.10.9]: https://codeberg.org/maxagahi/yadgar/compare/v5.10.8...v5.10.9
[5.10.8]: https://codeberg.org/maxagahi/yadgar/compare/v5.10.7.3...v5.10.8
[5.10.7.3]: https://codeberg.org/maxagahi/yadgar/compare/v5.10.7.2...v5.10.7.3
[5.10.7.2]: https://codeberg.org/maxagahi/yadgar/compare/v5.10.7.1...v5.10.7.2
[5.10.7.1]: https://codeberg.org/maxagahi/yadgar/compare/v5.10.7...v5.10.7.1
[5.10.7]: https://codeberg.org/maxagahi/yadgar/compare/v5.10.6...v5.10.7
[5.10.6]: https://codeberg.org/maxagahi/yadgar/compare/v5.10.5...v5.10.6
[5.10.5]: https://codeberg.org/maxagahi/yadgar/compare/v5.10.4...v5.10.5
[5.10.4]: https://codeberg.org/maxagahi/yadgar/compare/v5.10.3...v5.10.4
[5.10.3]: https://codeberg.org/maxagahi/yadgar/compare/v5.10.2...v5.10.3
[5.10.2]: https://codeberg.org/maxagahi/yadgar/compare/v5.10.1...v5.10.2
[5.10.1]: https://codeberg.org/maxagahi/yadgar/compare/v5.10.0...v5.10.1
[5.10.0]: https://codeberg.org/maxagahi/yadgar/compare/v5.9.0...v5.10.0
[5.9.0]: https://codeberg.org/maxagahi/yadgar/compare/v5.8.0...v5.9.0
[5.8.0]: https://codeberg.org/maxagahi/yadgar/compare/v5.7.12...v5.8.0
[5.7.12]: https://codeberg.org/maxagahi/yadgar/compare/v5.7.11...v5.7.12
[5.7.11]: https://codeberg.org/maxagahi/yadgar/compare/v5.7.10...v5.7.11
[5.7.10]: https://codeberg.org/maxagahi/yadgar/compare/v5.7.9...v5.7.10
[5.7.9]: https://codeberg.org/maxagahi/yadgar/compare/v5.7.8...v5.7.9
[5.7.8]: https://codeberg.org/maxagahi/yadgar/compare/v5.7.7...v5.7.8
[5.7.7]: https://codeberg.org/maxagahi/yadgar/compare/v5.7.6...v5.7.7
[5.7.6]: https://codeberg.org/maxagahi/yadgar/compare/v5.7.5...v5.7.6
[5.7.5]: https://codeberg.org/maxagahi/yadgar/compare/v5.7.4...v5.7.5
[5.7.4]: https://codeberg.org/maxagahi/yadgar/compare/v5.7.3...v5.7.4
[5.7.3]: https://codeberg.org/maxagahi/yadgar/compare/v5.7.2...v5.7.3
[5.7.2]: https://codeberg.org/maxagahi/yadgar/compare/v5.7.1...v5.7.2
[5.7.1]: https://codeberg.org/maxagahi/yadgar/compare/v5.7.0...v5.7.1
[5.7.0]: https://codeberg.org/maxagahi/yadgar/compare/v5.6.7...v5.7.0
[5.6.7]: https://codeberg.org/maxagahi/yadgar/compare/v5.6.1...v5.6.7
[5.6.1]: https://codeberg.org/maxagahi/yadgar/compare/v5.6.0...v5.6.1
[5.6.0]: https://codeberg.org/maxagahi/yadgar/compare/v5.5.3...v5.6.0
[5.5.3]: https://codeberg.org/maxagahi/yadgar/compare/v5.5.2...v5.5.3
[5.5.2]: https://codeberg.org/maxagahi/yadgar/compare/v5.5.1...v5.5.2
[5.5.1]: https://codeberg.org/maxagahi/yadgar/compare/v5.5.0...v5.5.1
[5.5.0]: https://codeberg.org/maxagahi/yadgar/compare/v5.4.8...v5.5.0
[5.4.8]: https://codeberg.org/maxagahi/yadgar/compare/v5.4.7...v5.4.8
[5.4.7]: https://codeberg.org/maxagahi/yadgar/compare/v5.4.6...v5.4.7
[5.4.6]: https://codeberg.org/maxagahi/yadgar/compare/v5.4.3...v5.4.6
[5.4.3]: https://codeberg.org/maxagahi/yadgar/compare/v5.4.2...v5.4.3
[5.4.2]: https://codeberg.org/maxagahi/yadgar/compare/v5.4.1...v5.4.2
[5.3.7]: https://codeberg.org/maxagahi/yadgar/compare/v5.3.6...v5.3.7
[5.3.6]: https://codeberg.org/maxagahi/yadgar/compare/v5.3.5...v5.3.6
[5.3.5]: https://codeberg.org/maxagahi/yadgar/compare/v5.3.4...v5.3.5
[5.3.4]: https://codeberg.org/maxagahi/yadgar/compare/v5.3.3...v5.3.4
[5.3.3]: https://codeberg.org/maxagahi/yadgar/compare/v5.3.2...v5.3.3
[5.3.2]: https://codeberg.org/maxagahi/yadgar/compare/v5.3.1...v5.3.2
[5.3.1]: https://codeberg.org/maxagahi/yadgar/compare/v5.3.0...v5.3.1
[5.3.0]: https://codeberg.org/maxagahi/yadgar/compare/v5.2.0...v5.3.0
[5.2.0]: https://codeberg.org/maxagahi/yadgar/compare/v5.1.9...v5.2.0
[5.1.9]: https://codeberg.org/maxagahi/yadgar/compare/v5.1.8...v5.1.9
[5.1.8]: https://codeberg.org/maxagahi/yadgar/compare/v5.1.7...v5.1.8
[5.1.7]: https://codeberg.org/maxagahi/yadgar/compare/v5.1.6...v5.1.7
[5.1.6]: https://codeberg.org/maxagahi/yadgar/compare/v5.1.5...v5.1.6
[5.1.5]: https://codeberg.org/maxagahi/yadgar/compare/v5.0.1...v5.1.5
[5.0.1]: https://codeberg.org/maxagahi/yadgar/compare/v5.0.0...v5.0.1
[5.0.0]: https://codeberg.org/maxagahi/yadgar/compare/v4.9.0...v5.0.0
[4.9.0]: https://codeberg.org/maxagahi/yadgar/compare/v4.8.3...v4.9.0
[4.8.3]: https://codeberg.org/maxagahi/yadgar/compare/v4.8.2...v4.8.3
[4.8.2]: https://codeberg.org/maxagahi/yadgar/compare/v4.8.1...v4.8.2
[4.8.1]: https://codeberg.org/maxagahi/yadgar/compare/v4.8.0...v4.8.1
[4.8.0]: https://codeberg.org/maxagahi/yadgar/compare/v4.5.0...v4.8.0
[4.5.0]: https://codeberg.org/maxagahi/yadgar/compare/v4.4.10...v4.5.0
[4.4.10]: https://codeberg.org/maxagahi/yadgar/compare/v4.4.9...v4.4.10
[4.4.9]: https://codeberg.org/maxagahi/yadgar/compare/v4.4.8...v4.4.9
[4.4.8]: https://codeberg.org/maxagahi/yadgar/compare/v4.4.7...v4.4.8
[4.4.7]: https://codeberg.org/maxagahi/yadgar/compare/v4.4.6...v4.4.7
[4.4.6]: https://codeberg.org/maxagahi/yadgar/compare/v4.4.5...v4.4.6
[4.4.5]: https://codeberg.org/maxagahi/yadgar/compare/v4.4.4...v4.4.5
[4.4.4]: https://codeberg.org/maxagahi/yadgar/compare/v4.4.3...v4.4.4
[4.4.3]: https://codeberg.org/maxagahi/yadgar/compare/v4.4.2...v4.4.3
[4.4.2]: https://codeberg.org/maxagahi/yadgar/compare/v4.4.1...v4.4.2
[4.4.1]: https://codeberg.org/maxagahi/yadgar/compare/v4.4.0...v4.4.1
[4.4.0]: https://codeberg.org/maxagahi/yadgar/compare/v4.3.0...v4.4.0
[4.3.0]: https://codeberg.org/maxagahi/yadgar/compare/v4.1.3...v4.3.0
[4.1.3]: https://codeberg.org/maxagahi/yadgar/compare/v4.1.2...v4.1.3
[4.1.2]: https://codeberg.org/maxagahi/yadgar/compare/v4.1.0...v4.1.2
[4.1.0]: https://codeberg.org/maxagahi/yadgar/compare/v4.0.5...v4.1.0
[4.0.5]: https://codeberg.org/maxagahi/yadgar/compare/v4.0.3...v4.0.5
[4.0.3]: https://codeberg.org/maxagahi/yadgar/compare/v4.0.2...v4.0.3
[4.0.2]: https://codeberg.org/maxagahi/yadgar/compare/v4.0.1...v4.0.2
[4.0.1]: https://codeberg.org/maxagahi/yadgar/compare/v4.0.0...v4.0.1
[4.0.0]: https://codeberg.org/maxagahi/yadgar/compare/v3.1.0...v4.0.0
[3.1.0]: https://codeberg.org/maxagahi/yadgar/compare/v3.0.0...v3.1.0
[3.0.0]: https://codeberg.org/maxagahi/yadgar/compare/v2.1.0...v3.0.0
[2.1.0]: https://codeberg.org/maxagahi/yadgar/compare/v2.0.0...v2.1.0
[2.0.0]: https://codeberg.org/maxagahi/yadgar/releases/tag/v2.0.0
