# Changelog

All notable changes to Yadgar are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning is [SemVer](https://semver.org/).

> Snapshots from v5.0.1 onward are captured from `yadgar stats` at release time.
> Earlier versions have no per-release snapshot (the practice started 2026-05-16).

## [Unreleased]

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

[unreleased]: https://codeberg.org/maxagahi/yadgar/compare/v5.0.1...HEAD
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
