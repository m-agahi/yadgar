# Changelog

Authoritative release log. Each entry links to the matching `MIGRATION_NOTES.md` section for full detail.

Format: terse one-line subject per change. Versions ordered newest-first. Tagged releases ship to `docker.io/openfantasy/yadgar:<version>`.

---

## [5.10.10] — 2026-05-30

Viz polish: 2x 3D node size + auto-zoom-fit on initial load (both 2D and 3D modes).

- **3D node size 2x** (`yadgar/static/index.html`): added `.nodeRelSize(8)` to 3D init chain. ForceGraph3D default is 4 — doubled radius makes nodes visibly larger on load without affecting layout coordinates.
- **Auto-zoom-fit on initial load** (`yadgar/static/index.html`): added `_zoomFitDone` module-level flag; extended `onEngineTick` callback in BOTH 2D and 3D init blocks to call `graph.zoomToFit(800, 50)` exactly once after tick 80 (layout well-settled, 30 ticks past the v5.10.8 pin threshold). Flag resets in `initGraph` (2D↔3D toggle re-fits) and `loadGraph` (reload button re-fits).
- **3 new static-asset regression tests** (`test_viz_static_assets.py::TestV51010VizPolish`): `test_nodeRelSize_set_to_8_in_3d_init`, `test_zoomFitDone_flag_declared`, `test_onEngineTick_calls_zoomToFit_at_threshold`.
- **Coloring logic untouched** — `_nodeColorFor`, `_linkColor`, `heatColor`, `WIKI_CAT_COLOR` unchanged per user instruction.

See [MIGRATION_NOTES.md §v5.10.10](MIGRATION_NOTES.md#v51010--viz-polish-2x-3d-node-size--auto-zoom-fit-2026-05-30) + `docs/PLAN_V5_10_10_VIZ_NODE_SIZE_AND_ZOOM_FIT.md`.

## [5.10.9] — 2026-05-30

Fix viz crash: filter orphan edges before passing to force-graph library (real root cause of all v5.10.7+ viz failures).

- **Root cause identified** (`force-graph.min.js`): library throws `Uncaught Error: node not found: entity:NNN` synchronously during `f.links` resolution when any link references an ID absent from the node set. One orphan edge crashes the entire physics simulation — no ticks run, all nodes clump at `(0,0,0)`. All v5.10.7–v5.10.8 symptom-chasing (mesh material, transparent flag, tick-count guard, mesh-leak removal) addressed downstream effects of this single crash.
- **Backend fix** (`yadgar/graph_api.py`): after assembling `nodes` + `edges`, filter edges to only those whose `source` AND `target` are in `{n["id"] for n in nodes}`. All `entity:*` causal edges are orphan-filtered because entity nodes are not included in the graph response (post-v5.0.0 monolith split). Logs count at INFO level. Increments `yadgar_graph_api_orphan_edges_dropped_total` counter.
- **New metric** (`yadgar/metrics.py`): `yadgar_graph_api_orphan_edges_dropped_total` Counter — tracks payload drift; non-zero after deploy confirms the fix fired on real data.
- **Frontend defensive filter** (`yadgar/static/index.html` `loadGraph()`): before `graph.graphData(...)`, builds `nodeIdSet` and filters `allLinks` to remove any edges whose endpoints are absent. `console.warn` logs count if any dropped — belt-and-suspenders for future backend drift.
- **5 new tests**: `test_graph_api_filters_orphan_edges`, `test_graph_api_orphan_drop_metric`, `test_graph_api_no_drops_in_healthy_payload` (backend); `test_loadGraph_filters_orphan_links`, `test_loadGraph_logs_dropped_count` (frontend static-asset).

See [MIGRATION_NOTES.md §v5.10.9](MIGRATION_NOTES.md#v5109--viz-orphan-edge-filter-2026-05-30) + `docs/PLAN_V5_10_9_VIZ_ORPHAN_EDGE_FILTER.md`.

## [5.10.8] — 2026-05-30

Fix 3D/2D viz physics hang (nodes clumped at origin) + Three.js mesh leak on filter cycles.

- **Bug A fixed** (`yadgar/static/index.html`): `onEngineStop` auto-pin guard — added `_engineTickCount` module-scope counter, incremented via `.onEngineTick()`. `onEngineStop` now returns early if `_engineTickCount < 50`, preventing premature pinning of all nodes at `(0,0,0)` before physics ran. Counter resets in `initGraph` so 2D↔3D toggle restarts it.
- **Bug B fixed** (`yadgar/static/index.html`): dropped `graph.graphData({ nodes: [], links: [] })` + `setTimeout(() => graph.graphData(d), 50)` empty-then-restore hack in `resetLayout`. ForceGraph3D does not dispose Three.js Mesh objects on the empty step — each call accumulated orphan meshes (700 nodes → 2297 meshes observed). Replaced with direct `graph.graphData(d)`.
- **3 new static-asset tests** (`test_viz_static_assets.py::TestV5108PhysicsAndMeshLeakFix`): `test_onEngineStop_has_tick_count_guard`, `test_onEngineTick_handler_present`, `test_no_empty_then_restore_pattern` (regression gate).

See [MIGRATION_NOTES.md §v5.10.8](MIGRATION_NOTES.md#v5108--viz-physics-hang--mesh-leak-fix-2026-05-30) + `docs/PLAN_V5_10_8_VIZ_PHYSICS_AND_MESH_LEAK_FIX.md`.

## [5.10.7.3] — 2026-05-30

Revert v5.10.7 custom 3D node geometry. Back to ForceGraph3D defaults.

- **Removed** `_makeNodeThreeObject` (custom THREE.Mesh factory for wiki/memory) from `yadgar/static/index.html`.
- **Removed** `.nodeThreeObject(_makeNodeThreeObject).nodeThreeObjectExtend(false)` from 3D graph init.
- **Removed** the 3D-mode `nodeThreeObject` re-call inside `_applySearchHighlight` (now only `.nodeColor()` re-fires).
- **Kept** `_nodeColorFor` + `.nodeColor(_nodeColorFor)` — applies heat-based colour to ForceGraph3D's default sphere material (may finally make 3D heat-coloring visible — bonus side-effect; was never working historically).
- **Why:** three attempts at custom 3D mesh (v5.10.7 Lambert; v5.10.7.1 Lambert→Basic; v5.10.7.2 conditional transparent) all rendered as fragmented triangle shards in user verification. Defaulting back to ForceGraph3D's library-managed solid spheres = last-known-good visual from v5.3.7.
- **Regression gates added** (`yadgar/tests/test_viz_static_assets.py::TestV510703RevertCustomMesh`): assert no `_makeNodeThreeObject` function, no `.nodeThreeObject(` call, no `new THREE.OctahedronGeometry`/`SphereGeometry` instantiation outside comments.
- **Removed** v5.10.7.1+v5.10.7.2 lighting/transparent tests (superseded by revert).
- **Trade-off:** S2.2 shape distinction (octahedra vs spheres) lost. User explicitly OK'd uniform shapes.

See [MIGRATION_NOTES.md §v5.10.7.3](MIGRATION_NOTES.md#v51073--revert-v5107-custom-3d-node-geometry-2026-05-30) + `docs/PLAN_V5_10_7_3_VIZ_REVERT_TO_DEFAULTS.md`.

## [5.10.7.2] — 2026-05-30

Hotfix: 3D viz wiki nodes still rendered as fragmented triangle shards after v5.10.7.1.

- **Root cause** (investigation 2026-05-30): `MeshBasicMaterial` with `transparent: true` + `opacity: 1.0` still places mesh in WebGL transparent render pass. Three.js sorts objects back-to-front in that pass but does NOT sort triangles within a single mesh. For an 8-faced `OctahedronGeometry`, back faces overdraw front faces → fragmented appearance. v5.10.7.1's Lambert→Basic swap was necessary but insufficient.
- **Fix** (`yadgar/static/index.html` line ~823): `transparent: true` → `transparent: !!node.__dimmed`. Mesh stays in opaque render pass when not dimmed → triangle ordering correct → solid octahedra (wiki) + solid spheres (memory) render properly. `opacity` value still controls dim-state alpha when `transparent` is true.
- **3D heat-coloring never worked** historically (PLAN_V5_10_7_VIZ_FIXES "soak-observed since 2026-05-20"); this fix restores SOLID-NODE rendering. Color treatment (whether heat gradient should be re-applied with proper material) is tracked as future work.

See [MIGRATION_NOTES.md §v5.10.7.2](MIGRATION_NOTES.md#v51072--3d-viz-transparent-flag-fix-2026-05-30).

## [5.10.7.1] — 2026-05-30

Bundled hotfix: sentinel filter + viz lighting fix.

- **Sentinel filter** (`yadgar/hooks/session-end-capture.py`): extended `SKIP_TAGS` frozenset to cover all slash-command output tags — `command-name`, `command-args`, `local-command-caveat`, `local-command-stdout`, `local-command-stderr` (in addition to existing `system-reminder`, `command-message`). Both `_count_human_messages` and `_parse_user_content` now reference the single module-level constant. Eliminates slash-command noise in `last_human_turns` sentinel field that was burying real human-turn context.
- **Viz lighting fix** (`yadgar/static/index.html`): `_makeNodeThreeObject` changed from `THREE.MeshLambertMaterial` → `THREE.MeshBasicMaterial`. ForceGraph3D adds no scene lights; Lambert rendered nodes as dark/fragmented triangle shards. Basic is unlit — colour always renders at set value. Wiki octahedra and memory spheres now render as solid coloured shapes.
- **8 new tests**: 6 sentinel-filter tests pinning per-tag skip behaviour + typo-turn survival; 2 viz tests asserting `MeshBasicMaterial` present + `MeshLambertMaterial` absent in `_makeNodeThreeObject` block.

See [MIGRATION_NOTES.md §v5.10.7.1](MIGRATION_NOTES.md#v51071--bundled-hotfix-sentinel-filter--viz-lighting-2026-05-30).

## [5.10.7] — 2026-05-30

Viz UX fixes S2.1–S2.4: heat colour in 3D, distinct node shapes, search mode fix, stats panel auto-refresh.

- **S2.1 — 3D heat colour**: `_nodeColorFor(node)` helper drives `.nodeColor()` in 3D init; heat gradient now visible in 3D (was uniform library default). `heatColor()` formula unchanged from 2D.
- **S2.2 — Node shape distinction**: `_makeNodeThreeObject(node)` returns `OctahedronGeometry` for wiki (visibly faceted) and `SphereGeometry` for memory. Material colour encodes heat (S2.1+S2.2 unified). Wired via `.nodeThreeObject()`.
- **S2.3 — Search in 3D**: `_applySearchHighlight()` now branches on `_graphMode`. 3D path re-fires `.nodeColor()` + `.nodeThreeObject()`. Old path called `nodeCanvasObject` (2D-only) causing `TypeError` in 3D.
- **S2.4 — Stats auto-refresh**: `openStats()` starts a 5 s `setInterval(refreshStats)`. `closeStats()` clears it. CPU/DB sparklines now animate while panel is open.
- **10 new static-asset tests** in `test_viz_static_assets.py`.

See [MIGRATION_NOTES.md §v5.10.7](MIGRATION_NOTES.md#v5107--viz-ux-fixes-2026-05-30).

## [5.10.6] — 2026-05-30

SESSION_END_CAPTURE sentinel-marker pattern + SessionStart extraction.

- **SessionEnd hook** (`yadgar/hooks/session-end-capture.py`): writes `~/.yadgar/session-ends/<session_id>.json` atomically on true exit (logout/other). Skips on `end_reason=clear/resume` and short sessions (`<SESSION_END_MIN_MESSAGES`). Embeds last N human turns + last 3 touched file paths for rotation resilience.
- **SessionStart import**: `hook_session_context` scans `~/.yadgar/session-ends/*.json`, imports each into memory with `_session_end_sentinel` tag, deletes on success. Retry semantics: `retries` counter incremented on failure; moved to `failed/` after 3 failures.
- **`_project_brief_signals` extension**: sentinel memory row → `extract_last_session_findings` recommended_action with `transcript_path`, `sentinel_id`, `last_human_turns`. Missing transcript → tombstone note + `forget(sentinel_id)` suggested_call.
- **Vacuum prune**: `_vacuum_stale_sentinels()` deletes `_session_end_sentinel` rows older than `SESSION_END_RETENTION_DAYS` (default 30).
- **4 new I25 env knobs**: `SESSION_END_CAPTURE_ENABLED=true`, `SESSION_END_RETENTION_DAYS=30`, `SESSION_END_SNIPPET_TURNS=5`, `SESSION_END_MIN_MESSAGES=2`.
- **`install_hooks` updated**: adds `SessionEnd` entry to `settings.json` (re-run required).
- **26 new tests** in `test_session_end_capture.py`.

See [MIGRATION_NOTES.md §v5.10.6](MIGRATION_NOTES.md#v5106--session-end-capture-sentinel-marker-pattern-2026-05-30).

## [5.10.5] — 2026-05-30

Patch: nightly cycle remaining bugs — vacuum URL second call site + prune deletes just-created snapshot.

- **Bug 1 — vacuum URL second call site**: `nightly_cycle.main()` and `cmd_vacuum_impl()` both had `getattr(args, "backend_url", "http://127.0.0.1:8080")` literals that bypassed `YADGAR_DB_URL` env when systemd invokes without `--backend-url`. Fixed both to `getattr(args, "backend_url", None) or os.environ.get("YADGAR_DB_URL", "http://127.0.0.1:8080")`. Eliminates `[vacuum] ERROR: backend at http://127.0.0.1:8080 is not reachable: HTTP 307`.
- **Bug 2 — prune deletes just-created snapshot**: `shutil.copytree` with `copy2` propagates the source DB directory's mtime to the new snapshot directory. If the DB dir is old (stopped core, no writes for hours), the snapshot sorts as "oldest" by mtime and gets pruned in the same cycle. Fixed `create_snapshot()` to call `target.touch()` after copytree, stamping the snapshot to current time.
- **7 new tests**: `test_vacuum_url.py` (3 — structural + env-read correctness for both call sites) + `test_backup.py::TestPruneDoesNotDeleteJustCreated` (3 — mtime stamp, round-trip cycle) + structural source-scan (1).

See [MIGRATION_NOTES.md §v5.10.5](MIGRATION_NOTES.md#v5105--nightly-cycle-remaining-bugs-2026-05-30).

## [5.10.4] — 2026-05-30

Hotfix: `consolidate_now` heavyweight fix + PreToolUse hook schema fix.

- **`consolidate_now(mode='light'|'full')`**: new `mode` param (default `'light'`). Light = `force_consolidate()` only, typically <30 s. Full = consolidation + sleep cycle + anchor audit; sets `_last_sleep_cycle` timestamp so 6-hour gate fires correctly. Fixes 13-minute surprise on every on-demand flush.
- **Hook schema fix**: `hook_runner.py:db-lockdown-check` now emits `{"hookSpecificOutput": {"permissionDecision": "allow"|"deny"}}` (new PreToolUse schema). Eliminates `(root): Invalid input` noise on Bash tool calls.
- **I13 compliance fix**: extracted 4 helper functions from `memory_stats()` to resolve pre-existing HARD complexity violations (cyclo=32, fn_loc=155, nesting=5). No behavior change.
- **Behavior change**: `consolidate_now()` (default/no args) no longer runs the sleep cycle or anchor audit. Callers requiring the full cycle must pass `mode='full'`.
- **License correction**: `pyproject.toml` `license` field MIT → Apache-2.0 (matches `LICENSE` file). Resolves YELLOW finding #3 in `docs/LICENSE_COMPLIANCE_AUDIT_2026-05-30.md`.
- **Verified live**: deployed via local merge + amd64 build + nix bump per `yadgar-dev-workflow-single-isolated-change-release-cycle` anchor. `/health` reports `version=5.10.4` post-restart.

See [MIGRATION_NOTES.md §v5.10.4](MIGRATION_NOTES.md#v5104--consolidate_now-mode-parameter-2026-05-30).

## [5.10.3] — 2026-05-29

Patch: `scripts/scan_db_for_secrets.py` end-to-end fix.

- **OTLP hang fix**: `os.environ.setdefault("YADGAR_OTLP_ENDPOINT", "")` at script top — suppresses `BatchSpanProcessor` that hung at exit (~10 s backoff) pushing HITS/Clean output past `| tail -10`.
- **ORDER BY id DESC**: memory + wiki queries now scan newest rows first; `--limit 200` catches memory 519107 (ghp_ 33-char leak at DB position 2994/3147).
- **`--storage-mock-leak`**: new flag — mock data with known secret, exercises exit-1 path without live DB.
- **9 new tests** in `yadgar/tests/test_scan_script.py` via subprocess; 2 live-DB tests gated on `YADGAR_TEST_LIVE_SCAN=1`.
- **v5.10.3 bump**: pyproject.toml, server.json, docker-compose.yml, uv.lock.

See [MIGRATION_NOTES.md §v5.10.3](MIGRATION_NOTES.md#v5103--scan_db_for_secretspy-end-to-end-fix-2026-05-29).

## [5.10.2] — 2026-05-29

Unified security + parity + nightly-cycle hotfix.

- **Secret-gate architecture (I26)**: dual-layer protection — Layer 2 `gate_or_reject()` on all write tool API boundaries; Layer 1 `SecretLeakBlocked` exception in `insert_memory()` as last-resort defence. `YADGAR_SECRET_GATE_DISABLED` kill switch with loud warning.
- **Pattern strictness**: GitHub PAT `{36,}→{20,}`, Anthropic key `{32,}→{20,}`, OpenAI key `{30,}→{20,}`. Tighter thresholds reduce false-negative window.
- **I26 invariant lint**: `scripts/check_secret_gate.py` — AST-walks all `@_tool()` write tools; fails if any lacks `gate_or_reject()`. Pre-commit hook added.
- **Backfill scan**: `scripts/scan_db_for_secrets.py` — read-only scan of all memory + wiki rows; `--storage-mock` for CI; report to `~/.yadgar/`.
- **DLQ handling**: `_classify_error()` treats `SecretLeakBlocked` as permanent → moves to DLQ after 3 attempts, no infinite retry.
- **memorize() anchor parity** (v5.10.x): `is_protected=True` now auto-sets `tier="conditional"`, injects `_anchor` tag, adds `anchor:{reason}` tag. `reason` kwarg added. `semantic_immortal` without reason rejected when `ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON=True`.
- **surrealdb dep fix**: promoted `surrealdb>=1.0.0` from `[dev]` to `[project.dependencies]` — `ImportError` on clean installs eliminated.
- **vacuum `:8080` literal fix**: `_log_consolidation_row` now uses `YADGAR_DB_URL` env var with `:8080` as fallback only.

See [MIGRATION_NOTES.md §v5.10.2](MIGRATION_NOTES.md#v5102--secret-gate-architecture--memorize-parity--nightly-cycle-hotfix-2026-05-29).

## [5.10.1] — 2026-05-29

`_active_work` soft warning tier + optional watchdog timer.

- `_build_recommended_actions`: new soft actions `consider_refresh_active_work` + `consider_refresh_checkpoint` when `WARN_HOURS < age ≤ STALE_HOURS`. Mutual exclusion with hard actions per row.
- `suggested_call` enrichment on soft + hard refresh actions (continues v5.9 pattern).
- `update_active_work()`: writes `~/.yadgar/active-work-tracked/<sha256[:12]>/directory.txt` registry marker.
- 3 new env knobs three-way registered: `ACTIVE_WORK_WARN_HOURS`, `CHECKPOINT_WARN_HOURS`, `AUTO_REFRESH_ACTIVE_WORK`.
- New systemd-user units: `yadgar-active-work-watchdog.{timer,service}` — user-managed, NOT enabled by default.

See [MIGRATION_NOTES.md §v5.10.1](MIGRATION_NOTES.md#v5101--_active_work-soft-warning-tier--watchdog-timer-2026-05-29).

## [backend-5.4.0] — 2026-05-29

Backend hot-path caching: CE score LRU cache + embedding vector LRU cache.

- `yadgar/cache.py` — new `LRUCache` class: `OrderedDict` LRU + msgpack snapshot with `YADCACHE\0` magic header + checkpoint-hash validation.
- CE score cache in `/rerank?mode=ce`: partial-hit path splits texts into cached vs. miss batches; only misses go to ML; results merged + back-filled.
- Embedding vector cache in `/embed`: per-text SHA256 key; hit avoids re-encode.
- Lifespan: restore both caches from snapshot before first request; `_run_cache_snapshot_task` asyncio background task; final snapshot on shutdown.
- 10 new I23-compliant Prometheus metrics: hits/misses/evictions/size_entries/size_bytes per cache + `cache_snapshot_age_seconds{cache}` gauge.
- 6 new env knobs three-way registered: `CE_CACHE_ENABLED`, `EMBED_CACHE_ENABLED`, `CE_CACHE_MAX_ENTRIES`, `EMBED_CACHE_MAX_ENTRIES`, `CACHE_SNAPSHOT_INTERVAL_SEC`, `CACHE_SNAPSHOT_DIR`.
- `msgpack>=1.0` added to `pyproject.toml`.
- Kill switch: `YADGAR_CE_CACHE_ENABLED=false` → pre-v5.4.0 code path.

See [MIGRATION_NOTES.md §backend-v5.4.0](MIGRATION_NOTES.md#backend-v540--recall-hot-path-caching-ce-score-cache--embedding-vector-cache-2026-05-29).

## [5.10.0] — 2026-05-29

Test harness hardening: orphan reap + port determinism + session isolation.

- Add `pytest-timeout` (300s default, thread method) to gate hung tests.
- Centralize SurrealDB subprocess spawn in `yadgar/tests/_surreal_helpers.py` with `atexit` registration → orphan workers reaped on pytest exit (clean or signal-killed).
- Deterministic xdist port assignment via `YADGAR_TEST_PORT_BASE` (default 12000) + retry-on-EADDRINUSE.
- `pytest_sessionfinish` conftest hook for last-chance cleanup.
- `YADGAR_TEST_NAMESPACE` env var for multi-agent tmp dir isolation.
- Optional watchdog systemd-user units at `scripts/systemd-user/` (user-installed).
- Closes recurring CPU-fan / orphan-SurrealDB / false-regression root cause investigation 2026-05-28.

See [MIGRATION_NOTES.md §v5.10.0](MIGRATION_NOTES.md#v5100--test-harness-hardening-orphan-reap--port-determinism--session-isolation-2026-05-29).

## [5.9.0] — 2026-05-28

Anchor audit: `audit_anchors()` MCP tool + `consolidate_now()` anchor pass.

- New tool `audit_anchors(directory, dry_run=True, cosine_threshold=None, include_global=False)` — surfaces forget_expired/merge/promote actions, safe-mutation-only when `dry_run=False`, NEVER auto-`wiki_add`.
- Extended `consolidate_now()` with per-directory anchor audit pass (gated by `ANCHOR_AUDIT_CONSOLIDATION_ENABLED`).
- `_audit_anchors` sentinel memory per directory (latest-wins, matches `_active_work` pattern).
- `recommended_actions.audit_anchors` now carries `suggested_call` field (copy-paste-able).
- 3 new I25-registered env knobs: `ANCHOR_AUDIT_CONSOLIDATION_ENABLED=true`, `ANCHOR_AUDIT_MAX_ACTIONS_PER_RUN=20`, `ANCHOR_AUDIT_HISTORY_RETENTION_DAYS=30`.
- `tier=semantic_immortal` + `is_protected=True` legacy rows NEVER auto-mutated.
- Idempotent: second call on unchanged state returns empty `applied` list.

See [MIGRATION_NOTES.md §v5.9.0](MIGRATION_NOTES.md#v590--anchor-audit-audit_anchors-tool--consolidate_now-anchor-pass-2026-05-28).

## [5.8.0] — 2026-05-28

Anchor hygiene foundation: `tier` enum + `valid_until` + 3 new signals + schema migration.

- New fields on `memorize()` and `anchor()`: `tier` (`semantic_immortal | conditional | ephemeral`), `valid_until` (datetime UTC), `ttl_days` (shorthand).
- Schema migration `migration_008` adds `tier`, `valid_until`, `migration_grace` columns to `memory` table (schemaless SurrealDB → no backend bump). Idempotent + sentinel-gated.
- 3 new `project_brief(mode="signals")` fields: `anchor_count_project`, `anchor_redundancy_candidates` (compact tuple-list encoding), `anchor_promote_candidates`. K=3 hard truncation to satisfy ≤100 token budget.
- 4 new `recommended_actions` action types: `audit_anchors`, `merge_redundant_anchors`, `promote_anchor_to_wiki`, `forget_expired_anchors`.
- 7 new I25-registered env knobs: `ANCHOR_CONDITIONAL_TTL_DAYS=90`, `ANCHOR_EPHEMERAL_TTL_DAYS=14`, `ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON=true`, `ANCHOR_REDUNDANCY_COSINE=0.92`, `ANCHOR_PROMOTE_WORDS=500`, `ANCHOR_PROMOTE_HEADERS=2`, `ANCHOR_AUDIT_THRESHOLD=15`.
- Backwards-compat: existing `anchor(...)` calls without `tier` default to `conditional` with 90d expiry.

See [MIGRATION_NOTES.md §v5.8.0](MIGRATION_NOTES.md#v580--anchor-hygiene-foundation-tier--valid_until--signals-2026-05-28).

## [5.7.13] — 2026-05-28 (test-only, no version tag)

Test isolation + xdist fixture scope fixes + anchor hygiene plan trilogy drafted.

- 5 test fixes for env-var/config.yaml pollution (`_isolate_yaml_config` autouse fixture, `monkeypatch.setenv` over bare `os.environ` mutation, correct `_state` module path for `_db_size_warn_last_logged_hour`).
- Function-scope `_engines` fixture in `test_memory_behavior.py` to prevent cross-test storage state pollution under xdist.
- `@pytest.mark.skipif` on 500-memory merge timing test under `PYTEST_XDIST_WORKER` (unreliable under parallel CPU contention; serial pass ~38.5s).
- Plans drafted: `PLAN_V5_8_ANCHOR_HYGIENE.md`, `PLAN_V5_9_ANCHOR_AUDIT.md`, `PLAN_V5_11_ANCHOR_CROSS_PROJECT.md` (originally numbered v5.10).

No production code touched → no version bump. No deployable artifact.

## [5.7.12] — 2026-05-27

`project_brief` two-audience split: `signals` + `restore` modes.

- New modes: `signals` (≤100 tokens, stop-hook target), `restore` (≤800 tokens, post-/clear target). `catalog` marked deprecated, back-compat preserved.
- Age numerics: `stale_checkpoint_hours`, `active_work_age_hours`, `init_memory_age_hours` exposed as floats|null.
- Pre-computed `recommended_actions` list: `refresh_active_work`, `refresh_checkpoint`, `bootstrap_project` (threshold-driven).
- Bug fix: `hot_memories` now excludes anchored entries (`_anchor NOT IN tags`) in all modes.
- `top_anchors_global` + `top_anchors_project` merged into single `top_anchors` with `scope: "global" | "project" | "both"` per entry.
- Stop hook (`yadgar/hooks/stop-memory-checkpoint.py`) rewritten to iterate `recommended_actions` instead of text-comparing signal state.
- 3 new I25 env knobs: `ACTIVE_WORK_STALE_HOURS=24`, `CHECKPOINT_STALE_HOURS=24`, `PROJECT_BRIEF_MAX_ANCHORS=12`.

See [MIGRATION_NOTES.md §v5.7.12](MIGRATION_NOTES.md#v5712--project_brief-two-audience-split--signalsrestore-modes-2026-05-27).

## [5.7.11] + backend [5.3.1] — 2026-05-27

Yamlify 5 OTLP + DBSIZE env knobs; drop dead `LOG_LEVEL`.

## [5.7.10] — 2026-05-27

Container yaml load + I25 invariant (three-way `KEY reason=<category>` allowlist) + nix `-e` cleanup.

## [5.7.0] — 2026-05-26

Nightly cycle redesign: single 19:00 UTC heavy cycle (backup → consolidation → vacuum → backup) replaces daemon 30-min trigger.

## Earlier releases

See `git log --oneline --grep='chore(release)'` for the complete release history pre-v5.7.0. Migration notes for older versions live in this file's earlier sections.
