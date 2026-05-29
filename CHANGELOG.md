# Changelog

Authoritative release log. Each entry links to the matching `MIGRATION_NOTES.md` section for full detail.

Format: terse one-line subject per change. Versions ordered newest-first. Tagged releases ship to `docker.io/openfantasy/yadgar:<version>`.

---

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
