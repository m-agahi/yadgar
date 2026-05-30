# Changelog

Authoritative release log. Each entry links to the matching `MIGRATION_NOTES.md` section for full detail.

Format: terse one-line subject per change. Versions ordered newest-first. Tagged releases ship to `docker.io/openfantasy/yadgar:<version>`.

---

## [5.10.4] — 2026-05-30

Hotfix: `consolidate_now` heavyweight fix + PreToolUse hook schema fix.

- **`consolidate_now(mode='light'|'full')`**: new `mode` param (default `'light'`). Light = `force_consolidate()` only, typically <30 s. Full = consolidation + sleep cycle + anchor audit; sets `_last_sleep_cycle` timestamp so 6-hour gate fires correctly. Fixes 13-minute surprise on every on-demand flush.
- **Hook schema fix**: `hook_runner.py:db-lockdown-check` now emits `{"hookSpecificOutput": {"permissionDecision": "allow"|"deny"}}` (new PreToolUse schema). Eliminates `(root): Invalid input` noise on Bash tool calls.
- **I13 compliance fix**: extracted 4 helper functions from `memory_stats()` to resolve pre-existing HARD complexity violations (cyclo=32, fn_loc=155, nesting=5). No behavior change.
- **Behavior change**: `consolidate_now()` (default/no args) no longer runs the sleep cycle or anchor audit. Callers requiring the full cycle must pass `mode='full'`.

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
