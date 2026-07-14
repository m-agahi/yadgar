> ARCHIVED 2026-07-14 — SHIPPED across two cars of `feat/stophook-tasklist-train`:
> Car 5 (v5.139.0, merge `ca23f158`) did Fix B (record `memify_pruned`/`cls_promoted`
> in `consolidation_log` + viz relabel "Pruned/Promoted/Archived") and Fix A doc part
> (stale `architecture.md`); Car 6 (v5.139.1, merge `1097b116`) removed the dead
> `daemon_check_interval` knob end-to-end (Settings/registry/FIELD_META/control.py/docs/tests).
> The live `~/.config/yadgar/config.yaml` orphan lines (`idle_threshold_seconds`,
> `daemon_check_interval`) were removed by the user 2026-07-14. Open follow-up (separate bug,
> noted not fixed): `derived_belief` dead-writer path (`insert_derived_belief` has zero
> non-test callers → `derived: N` logs each cycle but the table stays empty).

# Consolidation stat-recording fix + idle dead-knob cleanup

**Status:** ARCHIVED — SHIPPED (Cars 5+6, v5.139.0/v5.139.1) (Car 5 of `feat/stophook-tasklist-train`). Sequencing gate lifted (SurrealDB 3.1.5 shipped #136, b02f6397). Fix B (stat recording) still needed: `insert_consolidation_log` in `_shared/storage/ops.py:52` whitelists only 5 fields and drops `memify_pruned`, `cls_promoted`, `actions_processed`, etc. — orchestrator passes `{**stats}` but the impl discards them. Schema is SCHEMALESS so no migration needed; the fix is expanding the SET clause in `insert_consolidation_log`.

Source: 2026-06-30 drift diagnosis. Both findings are the same disease — *self-reporting the user can't trust*: a UI/registry asserts truth the runtime contradicts, and no automated check closes the loop ("discover by accident").

---

## Fix B (the real bug) — consolidation-stat recording

**Symptom:** the viz consolidation-activity panel shows **0 added / 0 deleted / 0 archived** across the last 30 cycles, while `journalctl --user -u yadgar-nightly-cycle.service` shows real work every night (Memify pruned 3–71, CLS promoted 1–7).

**Root cause (proven, both ends read):**
- Consumer: `static/index.html:3885` → `GET /api/metrics/consolidation-log?limit=30`; chart maps `r.added/r.deleted/r.archived` (`index.html:3625-3627`). Server SQL `server/http.py:1792-1794` selects `memories_added, memories_updated, memories_archived, memories_deleted`.
- Producer: cycle stats init `orchestrator.py:339-344` (the four `memories_*`, all 0). Memify writes `stats["memify_pruned"]` (`orchestrator.py:287-290`); CLS writes `stats["cls_promoted"]` (`orchestrator.py:305-307`). Persist at `orchestrator.py:416` → `insert_consolidation_log`.
- **The drop:** `storage/ops.py:51-68` `insert_consolidation_log` CREATE persists **only** `memories_added/updated/archived/deleted` via `log.get("memories_*", 0)`. It never reads `memify_pruned`/`cls_promoted` → those keys are silently discarded. The four columns the viz reads are fed only by decay/archival + causal discovery, ~0 most nights.
- Why the journal differs: `orchestrator.py:421` logs the *full* stats dict (incl. `memify_pruned`/`cls_promoted`) → journalctl shows the real numbers the table never stored.

**Classification:** RECORDING broken (key-name mismatch) + a SEMANTIC layer (even the recorded columns measure side-effects, not the headline prune/promote work).

**Fix:**
1. **Schema:** add columns `memify_pruned` + `cls_promoted` to `consolidation_log` (consider `cls_skipped`, `links_created` if cheap).
2. **Writer** (`storage/ops.py:54-65`): persist `memify_pruned`/`cls_promoted` from the stats dict.
3. **Read** (`server/http.py:1792-1794`): add the new columns to the SELECT.
4. **Viz** (`index.html:3625-3627`): map the new fields + **relabel the panel "pruned / promoted / archived"** so the three surfaced metrics are the phases that actually mutate memory.
5. **TDD:** assert a consolidation cycle's `memify_pruned`/`cls_promoted` land in `consolidation_log` and surface via `/api/metrics/consolidation-log`.

**Minor anomaly to check while here:** `derived: 2` logs every cycle but `derived_belief` table = 0 rows — derive does work but persists nothing. Possible separate bug.

---

## Fix A (cleanup) — idle dead-knob

`idle_threshold_seconds` is **fully dead**: idle-triggered consolidation removed v5.7.0 (`consolidation/orchestrator.py:3-7`); the `IDLE_THRESHOLD_SECONDS` Settings field deleted v5.76.0 (`f28faf5f`). Zero code readers.

- Remove the **orphan `idle_threshold_seconds: 300`** line from `~/.config/yadgar/config.yaml` (dead, pydantic ignores it).
- Fix **`docs/reference/architecture.md:107`** — STALE, still describes the knob firing after idle.
- **Verify `daemon_check_interval`** (`config.yaml:46`) isn't also dead/stale (the astrocyte loop it drove may also be gone); clean if dead.

---

## Sequencing

After SurrealDB 3.1.5 (#136) deploys. The `consolidation_log` schema add (Fix B step 1) lands on 3.1.5.

## Broader follow-up (optional)

The diagnosis flagged "nothing reconciles producer↔consumer." A `drift-audit` on the stat panels (source = the phase stats dict; derived = the `consolidation_log` columns + the viz fields) + a test that fails when a producer key isn't persisted/surfaced would close the loop permanently — the structural fix for the "discover by accident" class.
