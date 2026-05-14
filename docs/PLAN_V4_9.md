# Yadgar v4.9 Plan — Vacuum Followup, Bugfixes, Hygiene

## Context

v4.8 (`docs/PLAN_V4_8.md`) ships the working `yadgar vacuum` CLI + a weekly nix timer. v4.9 polishes that layer (on-demand trigger, automatic size-driven trigger) and clears two known-but-untriaged bugs that have been carried since v4.4.11. It is the last minor release before v5.0.0 (security + observability rewrite).

Version bump: `4.8.0 → 4.9.0`. No breaking API changes. No backend image change (`server.json:backend_version` stays at whatever v4.8 ships, currently planned 4.7.0).

---

## 1. `vacuum_now()` MCP tool

Thin wrapper around the v4.8 service unit. New in `yadgar/server.py`:

```python
@mcp.tool(power=True)
def vacuum_now(force: bool = False) -> dict:
    """Trigger a SurrealKV vacuum. Daemon downtime ~2-5 min on a 500 MB DB.

    Returns:
        {
            "started": bool,
            "service_unit": "yadgar-vacuum.service",
            "before_bytes": int,
            "skipped_reason": str | None,
        }
    """
```

Behavior:

- Reads `db_size_bytes` from current `check_invariants` snapshot. If `< 200 MiB` and `force=False`, refuses with `skipped_reason="db_below_threshold"`. Override with `force=True`.
- Refuses if `yadgar-vacuum.service` is already in `activating` or `active` state (`systemctl --user is-active`). `skipped_reason="vacuum_already_running"`.
- Refuses if `service-mode` detected as `manual` (no systemd, no docker compose) — returns a `skipped_reason="no_supported_service_manager"` plus the shell command the user should run.
- Otherwise: `systemctl --user start --no-block yadgar-vacuum.service`. Returns immediately with `started=True` and `before_bytes`. The caller polls `check_invariants` for completion.

`power=True` because vacuum stops the daemon and any active Claude Code session loses its MCP connection.

## 2. Threshold auto-trigger

New config keys (`yadgar/config.py`):

- `VACUUM_AUTO_THRESHOLD_BYTES`, default `2_147_483_648` (2 GiB).
- `VACUUM_AUTO_WINDOW_START`, default `"03:00"` (local time).
- `VACUUM_AUTO_WINDOW_END`, default `"06:00"`.
- `VACUUM_AUTO_ENABLED`, default `True`.

`ConsolidationScheduler` cycle (`yadgar/consolidation.py`) adds an end-of-cycle check:

```python
if settings.VACUUM_AUTO_ENABLED:
    size = check_invariants_db_size()["db_size_bytes"]
    if size > settings.VACUUM_AUTO_THRESHOLD_BYTES:
        if _in_window(now, settings.VACUUM_AUTO_WINDOW_START, settings.VACUUM_AUTO_WINDOW_END):
            _fire_vacuum_service()
            log.warning("Auto-vacuum triggered: db=%d MiB > %d MiB", size>>20, threshold>>20)
        else:
            log.warning("DB over auto-vacuum threshold (%d MiB) but outside window; deferred", size>>20)
```

Single-fire per cycle. No retry loop. Cooldown: skip auto-trigger if `vacuum_now()` last ran < 6 hours ago (`last_vacuum_at` timestamp in `counter` table).

Interaction with the weekly nix timer: orthogonal. Timer fires unconditionally on Sun 04:00. Threshold trigger fires any night between 03:00–06:00 if the DB blew past 2 GiB earlier than the weekly run could catch.

User-active flag: skipped. v4.9 has no presence detection; the 03:00–06:00 window is the only guard. Document in MIGRATION_NOTES that users on different sleep schedules should override the window.

## 3. Fix `test_repeated_cooccurrence_increases_weight`

Failure (from known-issues memory, 2026-05-12): co_occurrence relationship weight stays at 1.0 instead of accumulating with each repeat occurrence. Suspected regression: consolidation-batching PRs #33 / #34 collapsed multiple co-occurrence emit-calls into a single batched UPSERT that always sets `weight = 1.0` instead of `weight += 1`.

Investigation order:

1. `git log --oneline -- yadgar/consolidation.py yadgar/knowledge_graph.py | grep -E "#3[34]"` — locate exact commits.
2. Read the batching diff. Look for `UPSERT ... CONTENT $data` where `$data["weight"] = 1.0` instead of `UPSERT ... SET weight += 1`.
3. Switch to `UPSERT ... SET weight += $delta` or pre-aggregate the batch (`Counter` of co-occurrence pairs, single statement per pair with the summed delta).
4. Re-run `tests/test_knowledge_graph.py::test_repeated_cooccurrence_increases_weight` — must pass.

Add a regression-protection test: emit the same co-occurrence pair 5 times across 3 batches, assert final `weight == 5`. Cross-batch summing is the case the current test misses.

## 4. `caused_by` relationship pruning

Extend `check_invariants` auto-repair to include `caused_by` edges (already in the same RELATE family as `wiki_crossref`, `memory_similarity_link`).

Dangling-edge detection: `SELECT id, in, out FROM caused_by WHERE in IS NONE OR out IS NONE OR record::exists(in) = false OR record::exists(out) = false`. Delete matches in the auto-repair pass. Log count in `fixed.caused_by`.

Add to the row-count ceiling check: `caused_by` should not exceed `MAX_CAUSED_BY_ROWS` (new setting, default 100_000). Beyond that, prune oldest by `created_at`.

## 5. Per-table size estimate in telemetry

Extend the v4.8 `db_size` block in `check_invariants` with a per-table breakdown:

```json
"per_table": {
  "memory": {"rows": 8421, "estimated_bytes": 14580000},
  "wiki_page": {"rows": 1588, "estimated_bytes": 3920000},
  ...
}
```

Computed from `SELECT count(), array::sum(string::len(content)) FROM <table> GROUP ALL` per table. Cheap proxy — surrealkv stores more than raw content bytes per row, but the proportions are what matters for "which table is growing".

Surface in `memory_stats` output too. Tells the user which table is driving bloat *before* a vacuum.

## Test plan

1. **`vacuum_now()` happy path** — mock `systemctl`, assert it invokes `--no-block` and returns `started=True` with `before_bytes` populated.
2. **`vacuum_now()` refusals** — DB below threshold (no `force`), service already active, no service manager. Each returns the expected `skipped_reason`.
3. **Threshold auto-trigger window** — feed `_in_window(now)` a clock at 02:59 and at 03:01 with the default config; assert only the latter fires.
4. **Threshold auto-trigger cooldown** — set `last_vacuum_at` to 1 hour ago, assert auto-trigger skips.
5. **co_occurrence accumulation** — the regression test above (5 emits, 3 batches, `weight == 5`).
6. **caused_by pruning** — fixture with 10 caused_by edges, 4 pointing to deleted memories. Run `check_invariants`, assert 4 deleted, 6 remain.
7. **Per-table size** — assert `per_table["memory"]["rows"]` matches `SELECT count() FROM memory`.

## Open decisions

1. **Auto-trigger default ON or OFF?** ON for single-user nix install (matches the user's deployment). Make the nix module flip it OFF for multi-user installs.
2. **Cooldown duration.** 6 hours. Short enough that a recovery-after-failure cycle can still fire; long enough that a buggy threshold computation can't loop. No setting — hard-coded for v4.9.
3. **Per-table breakdown computation cost.** `array::sum(string::len(content))` per table runs once per consolidation cycle (daily). Cost on a 500 MB DB is < 1 s based on the existing `SELECT *` timings in `cmd_vacuum`. Acceptable.

## Order of work

1. Fix `test_repeated_cooccurrence_increases_weight` first — small, decouples from vacuum work, unblocks CI on master.
2. `caused_by` pruning — extend the v4.5.0 auto-repair set.
3. Per-table size in `check_invariants`.
4. `vacuum_now()` MCP tool.
5. Threshold auto-trigger in consolidation cycle.
6. Bump `pyproject.toml:version` 4.8.0 → 4.9.0.
7. Open PR.

## Deferred to v5.0+

- HTTP `/admin/vacuum` endpoint — bundle with bearer-token middleware in v5.0.
- Per-table actual on-disk bytes (vlog accounting) — needs surrealkv internals or a sidecar process; estimated bytes is enough for routing decisions.
- Wiki slug dedup (1588 pages with `mod: __main__` variants) — folded into the v5.0 hygiene commit alongside the bare-except cleanup.
- Native online compaction — track upstream SurrealKV. Nothing to do until they ship a primitive.
