# PLAN — v5.13.0: Bi-temporal edges extension (Adopt-3)

**Status:** drafted 2026-05-30 from competitor audit 2026-05-30 Item 3 ("Bi-temporal edges on all relationships — 75% done"). Plan-first per I27.

**Master at draft time:** `e8c5a4b` (post-D2/D3 audit-decision record).

**Audit-doc references (per yadgar-roadmap-future-improvements wiki):**

- `docs/competitor-audit-2026-05-30.md` and `docs/AUDIT_DECISIONS.md` are referenced as source-of-truth in the roadmap wiki but were not present on disk at draft time. This plan treats the wiki entry as canonical; if those docs land later the plan should be reconciled against them. Coordinator owns committing those source docs — this plan does not create them.

**Sequencing:** independent of v5.10.x train; ships after v5.10.x and `backend-v5.4.x` are stable. Slots BEFORE v5.14.x (recall plugin arch — R2 from 2026-05-30 audit) because the bi-temporal as-of-date helper this plan introduces is a building block the plugin architecture may want to plug into.

**Hard rules honoured:** No terraform. No co-author trailers. No hook bypass. Branch first. Schema migrations require `MIGRATION_NOTES.md` entry — see §10.

---

## 1. Background — what shipped already

Three migrations have already laid the bi-temporal foundation (CHANGELOG line 126; `yadgar/storage/migrations.py`):

| Migration | Shipped | Tables affected | Columns added |
|---|---|---|---|
| `_migration_007_bitemporal_edges` | v5.3.4 | `causal_dag_edge`, `relationship`, `memory_similarity_link` | `valid_from`, `valid_until` |
| `_migration_006_source_memory_id` | v5.3.3 | same three tables | `source_memory_id` (citation provenance) |
| `_migration_008_anchor_tier` | v5.8.0 | `memory` | `tier`, `valid_until`, `migration_grace` |

Runtime support shipped alongside:

- `yadgar/storage/bitemporal.py::invalidate_edge(storage, edge_table, edge_id, reason)` — never deletes; sets `valid_until = now()`. Allowed-table set is hard-coded (injection guard).
- `yadgar/storage/causal.py::get_all_causal_edges(include_invalidated=False)` — filtered query by default.
- `yadgar/graph_api.py::get_full_graph(include_invalidated=False)` — propagates the filter through the public graph read path.
- Tests: `yadgar/tests/test_bitemporal_edges.py` — five tests covering schema, insert defaults, invalidation, default-exclusion, and `include_invalidated=True`.

Audit verdict in roadmap wiki: **"75% done"** — three edge tables covered; remaining relationship-shaped state has no bi-temporal columns yet.

---

## 2. Edge-table survey — uncovered candidates

The audit demands "all relationship types". To respond honestly, every table that carries graph-edge semantics (source-target pair, attribute about an entity, or membership) was inspected. Each entry below records what it is, who mutates it, and whether bi-temporal genuinely helps.

### Tier 1 — true KG attribute edges (Zep canonical use case)

These tables hold entity-attribute facts that mutate over time, and the current schema **loses** the prior state on mutation. Bi-temporal adds genuine history-reconstruction capability.

#### T1A — `user_profile` (STRONGEST CANDIDATE)

- **Shape:** `(entity_name, attribute_type, attribute_key, directory_context)` UNIQUE → `attribute_value`, `confidence`, `evidence_memory_ids`, `created_at`, `updated_at`.
- **Mutation site:** `yadgar/storage/user.py:126-139` — UPSERT in-place. When a profile fact is observed twice the old `attribute_value` is overwritten in a single `UPDATE`. Prior value is **lost**.
- **Why bi-temporal:** this is the Zep canonical example ("user worked at Quinyx from 2023-01 to present"). Without `valid_from`/`valid_until` per attribute-value version, we cannot answer "what was the user's role on 2026-03-15?".
- **Rationale:** insert pattern must change from UPSERT-in-place to "close prior row + insert new row". The unique constraint on `(entity_name, attribute_type, attribute_key, directory_context)` is incompatible with multi-version storage and must be **scoped to `valid_until IS NONE`** (current only).

#### T1B — `derived_belief`

- **Shape:** `(subject, belief_type, content, directory_context, evidence_memory_ids, confidence, embedding, created_at, updated_at)`. No UNIQUE constraint.
- **Mutation site:** `yadgar/storage/narrative.py:168` — pure `CREATE`, never UPDATE. Multiple belief rows for the same `subject` already coexist.
- **Why bi-temporal partial:** because the table is already append-only, we do not need full bi-temporal — we need a **sentinel for "current"** so callers can answer "what does the system currently believe about subject X?". Two options:
  1. Add `valid_from`/`valid_until` — symmetry with edge tables, supersedes prior beliefs by setting `valid_until` on each older row when a new one is inserted.
  2. Add `superseded_by: option<int>` — pointer to the successor belief. Cheaper, but breaks the "uniform bi-temporal protocol" pattern.
- **Recommendation:** Option 1 for protocol uniformity, even though it's narrower-utility than T1A. Insert-time path adds: "find rows for same `(subject, belief_type, directory_context)` with `valid_until IS NONE`; close them."

### Tier 2 — graph edges, plausible utility, lower-priority

These tables carry relationship-shaped data; mutating writers currently **hard-DELETE** rows. Bi-temporal would preserve history but the calling code is unlikely to query it. Defer pending a use-case trigger.

| Table | Source | Mutation site | Why defer |
|---|---|---|---|
| `wiki_crossref` | `yadgar/storage/wiki.py:240-275` `replace_wiki_crossrefs` | DELETE FROM + CREATE inside one TX on every wiki save | Use case requires a "wiki rename history" feature that does not yet exist. Audit invariant code at `yadgar/server/tools/admin_invariants.py:341` also DELETEs dangling crossrefs — bi-temporal here would just bloat the table. |
| `memory_transition` | `yadgar/storage/rules.py:148-186` `insert_transition` / `increment_transition` | DELETE on memory removal (`yadgar/storage/memory.py:298`) | Co-recall count is a heuristic for surfacing, not a historical fact. No caller would ask "what was the co-recall count two weeks ago?". |
| `memory.cluster_id` (FK, not a join table) | `yadgar/storage/cluster.py` reassignment paths | overwrite in-place | Cluster membership churn is high-frequency consolidation noise; history not useful. Would need to split into a `memory_cluster_assignment` join table — too invasive. |

### Tier 3 — reject / out of scope

| Table | Why rejected |
|---|---|
| `memory_cluster.parent_cluster_id` | Hierarchy churn rarely queried point-in-time. |
| `astrocyte_process.memory_ids` / `entity_ids` | Array-encoded membership, not a join table. Bi-temporal needs full schema redesign — out of scope. |
| `memory_archive` | Already preserves history by versioning (one archive row per reconsolidation). |
| `narrative_entry` | Already temporally bounded (`period_start` / `period_end`). |
| `checkpoint` | `is_active` flag already encodes "current"; only one active at a time. Bi-temporal would duplicate semantics. |
| `prospective_memory` | Carries its own lifecycle (`is_active`, `triggered_at`). Not edge-shaped. |
| `engram_slot`, `file_hash`, `counter`, `schema_version` | Operational state, not relationships. |

---

## 3. Recommendation — selective extension

**Ship Tier 1 only (T1A + T1B).** Tier 2 deferred with explicit revisit triggers (§11). Tier 3 rejected.

Rationale:
- T1A delivers real Zep-parity capability — the canonical bi-temporal use case (user-attribute history) becomes queryable.
- T1B is small additional surface (one migration, one insert-path change in `insert_derived_belief`) and preserves protocol uniformity across all bi-temporal-aware tables.
- Tier 2 would require designing a new use-case-facing API to be worth the storage growth. Without that API, columns just sit unused.
- The bonus as-of-date helper (§5) is single-table-agnostic — adding it once unlocks all five bi-temporal tables (3 already covered + T1A + T1B) uniformly.

---

## 4. Schema migrations

Two new migrations following the `_migration_007_bitemporal_edges` template (idempotent DDL, single-TX backfill).

### `_migration_009_bitemporal_user_profile`

```python
def _migration_009_bitemporal_user_profile(storage) -> None:
    """Add valid_from / valid_until to user_profile (Adopt-3, v5.13.0).

    Pivot semantics: from "UPSERT in-place" to "close prior row + insert new row".
    The existing UNIQUE index (entity_name, attribute_type, attribute_key, directory_context)
    must be scoped to currently-valid rows or it blocks the new write pattern.
    """
    storage._q("DEFINE FIELD IF NOT EXISTS valid_from ON TABLE user_profile TYPE option<string>;")
    storage._q("DEFINE FIELD IF NOT EXISTS valid_until ON TABLE user_profile TYPE option<string>;")

    # Backfill valid_from from created_at on existing rows (best-effort).
    storage._q(
        "BEGIN TRANSACTION;\n"
        "UPDATE user_profile SET valid_from = created_at "
        "WHERE valid_from IS NONE AND created_at IS NOT NONE;\n"
        "COMMIT TRANSACTION"
    )

    # Drop the old unconditional UNIQUE; recreate as partial-unique scoped to
    # currently-valid rows (valid_until IS NONE). This is the critical step that
    # enables multi-version storage without breaking the duplicate-prevention guarantee
    # for current state.
    storage._q("REMOVE INDEX IF EXISTS profile_unique_idx ON user_profile;")
    storage._q("""
        DEFINE INDEX IF NOT EXISTS profile_unique_current_idx
            ON user_profile
            FIELDS entity_name, attribute_type, attribute_key, directory_context
            WHERE valid_until IS NONE;
    """)
```

> **SurrealDB capability check.** `DEFINE INDEX ... WHERE ...` (partial index) is a feature we MUST confirm in the SurrealDB v3 version yadgar ships. If unsupported, the fallback is: **drop UNIQUE entirely**, enforce uniqueness application-side in `insert_profile` by querying for `valid_until IS NONE` first. Plan acceptance criteria call out the verification.

### `_migration_010_bitemporal_derived_belief`

```python
def _migration_010_bitemporal_derived_belief(storage) -> None:
    """Add valid_from / valid_until to derived_belief (Adopt-3, v5.13.0)."""
    storage._q("DEFINE FIELD IF NOT EXISTS valid_from ON TABLE derived_belief TYPE option<string>;")
    storage._q("DEFINE FIELD IF NOT EXISTS valid_until ON TABLE derived_belief TYPE option<string>;")

    # Backfill: every existing belief is the current one for its (subject, belief_type,
    # directory_context) group. valid_from = created_at; valid_until = NULL.
    storage._q(
        "BEGIN TRANSACTION;\n"
        "UPDATE derived_belief SET valid_from = created_at "
        "WHERE valid_from IS NONE AND created_at IS NOT NONE;\n"
        "COMMIT TRANSACTION"
    )
```

No index rework needed — `derived_belief` has no UNIQUE constraint today.

### Append to `_MIGRATIONS` list (no reorder, additive only)

```python
_MIGRATIONS = [
    ... existing 001-008 ...,
    {"version": "009_bitemporal_user_profile",   "fn": _migration_009_bitemporal_user_profile},
    {"version": "010_bitemporal_derived_belief", "fn": _migration_010_bitemporal_derived_belief},
]
```

---

## 5. Code changes

### 5A. Extend `_VALID_EDGE_TABLES` in `yadgar/storage/bitemporal.py`

```python
_VALID_EDGE_TABLES = frozenset({
    "causal_dag_edge",
    "relationship",
    "memory_similarity_link",
    "user_profile",       # NEW v5.13.0
    "derived_belief",     # NEW v5.13.0
})
```

`invalidate_edge` works unchanged — it just sets `valid_until = now()`.

### 5B. Rework `insert_profile` (`yadgar/storage/user.py`)

Pivot from UPSERT-in-place to close-and-insert:

1. Find existing row for `(entity_name, attribute_type, attribute_key, directory_context)` with `valid_until IS NONE`.
2. If found AND `attribute_value` differs OR `confidence` changes by ≥`PROFILE_BITEMPORAL_VERSION_DELTA` (env knob, default `0.05`): call `invalidate_edge(storage, "user_profile", existing_id)`; INSERT new row with `valid_from = now()`, `valid_until = NULL`, evidence-list carried forward.
3. If found AND change is below threshold: keep the existing row, merely append to `evidence_memory_ids` and bump `updated_at` — avoids row-explosion on confidence drift.
4. If not found: INSERT new row exactly as today plus `valid_from = now()`.

Env knob:
- `PROFILE_BITEMPORAL_VERSION_DELTA` (float, default 0.05) — change threshold below which we update in place instead of inserting a new version. Prevents unbounded row growth from noisy confidence drift.

### 5C. Rework `insert_derived_belief` (`yadgar/storage/narrative.py`)

When inserting a belief with `(subject, belief_type, directory_context)` matching existing currently-valid rows, **close those rows** (`invalidate_edge` on each) before CREATE. This converts the append-only table into a versioned-supersession table.

Caller-controllable: add optional `supersede: bool = True` parameter to `insert_derived_belief` so callers that genuinely want multiple co-existing beliefs (e.g. competing hypotheses) can opt out.

### 5D. Filtered read helpers (consistency with `get_all_causal_edges`)

Add `include_invalidated: bool = False` parameter to:
- `yadgar/storage/user.py::search_profiles_fts`
- `yadgar/storage/user.py::get_profiles_for_entity` (the function called by lines 180-188)
- `yadgar/storage/narrative.py::search_beliefs_fts`
- `yadgar/storage/narrative.py::get_beliefs_for_subject`

Default `False` — current-state-only. Pass `True` from the as-of-date helper (§6).

### 5E. NO breaking changes to existing `_VALID_EDGE_TABLES` callers

`invalidate_edge` already raises `ValueError` for unknown tables. Adding tables to the frozenset is purely additive and cannot break callers.

---

## 6. Bonus — point-in-time / as-of-date query API

Add a single shared helper in `yadgar/storage/bitemporal.py`:

```python
def as_of_filter(table: str, as_of: str | None = None) -> str:
    """Return a SQL WHERE-fragment that selects rows valid at `as_of` (ISO-8601 string).

    as_of=None  → currently-valid rows: valid_until IS NONE OR valid_until > time::now()
    as_of=<ts>  → historically-valid at ts: valid_from <= ts AND (valid_until IS NONE OR valid_until > ts)

    Returns a fragment beginning with " AND " ready for splicing after an existing WHERE.
    """
```

Wire this into:
- `get_all_causal_edges(as_of=None)` — gains a new param; existing `include_invalidated` retained for back-compat.
- `get_full_graph(as_of=None)` — propagates through `GraphAPI`.
- `recall(as_of=None)` — opt-in temporal recall. Default None preserves current behaviour.

**Single MCP-tool surface change:** `recall` gains an optional `as_of: str | None = None` parameter — usable as `recall(query="user role at quinyx", as_of="2026-03-15T00:00:00Z")`.

Storage of `as_of` as ISO-8601 string is consistent with how `valid_from` / `valid_until` are stored (see migration_007 comment: "Dates are stored as ISO-8601 strings").

---

## 7. Tests (red-first per global rule)

All tests added under `yadgar/tests/test_bitemporal_extension.py` (new file — keeps the existing `test_bitemporal_edges.py` untouched for git-history clarity on what shipped in v5.3.4).

### T1 — Migrations applied (red until migrations exist)

- `test_migration_009_adds_columns_to_user_profile`
- `test_migration_010_adds_columns_to_derived_belief`

### T2 — Insert defaults (red until inserts populate `valid_from`)

- `test_insert_profile_defaults_valid_from_now`
- `test_insert_derived_belief_defaults_valid_from_now`

### T3 — Supersession (red until close-and-insert lands)

- `test_user_profile_change_value_supersedes_prior_row` — insert v1, insert v2 with same key/different value, assert v1.valid_until is set and v2 is current.
- `test_user_profile_minor_confidence_drift_does_not_create_new_row` — insert with conf=0.5, insert again with conf=0.52 (below 0.05 threshold), assert no new row.
- `test_derived_belief_new_supersedes_prior` — insert belief, insert competing belief for same subject, assert prior is closed.
- `test_derived_belief_supersede_false_keeps_old` — opt-out path.

### T4 — As-of-date queries (red until §6 helper lands)

- `test_as_of_filter_current_state_excludes_invalidated`
- `test_as_of_filter_past_date_returns_historical_value` — set up two profile versions with known timestamps, query as_of between them, assert old value returned.
- `test_recall_with_as_of_returns_historical_belief`

### T5 — Partial unique index (capability gate)

- `test_user_profile_unique_constraint_scoped_to_current` — insert v1, close v1 (valid_until set), insert v2 with same key — must succeed (UNIQUE only on currently-valid rows). If SurrealDB partial indexes are unsupported (see §4 warning), this test pins the **application-side fallback** behaviour.

### T6 — Back-compat (existing callers untouched)

- `test_existing_insert_profile_callers_pass_unchanged` — call `insert_profile` exactly as v5.12.x callers do, assert no exceptions and current-state queries return the same shape.
- `test_get_full_graph_default_unchanged` — without `as_of=`, output identical to v5.12.x baseline.

---

## 8. Acceptance criteria

- All migrations idempotent: run pytest twice in succession, no failures.
- Existing five bi-temporal tests in `test_bitemporal_edges.py` still pass unchanged.
- New tests in `test_bitemporal_extension.py` all green.
- `invalidate_edge(storage, "user_profile", id)` and `invalidate_edge(storage, "derived_belief", id)` work without `ValueError`.
- `recall(query="...", as_of="<past-ts>")` returns historically-valid memories/profiles/beliefs.
- `get_full_graph(as_of="<past-ts>")` returns a graph snapshot reflecting that point in time.
- SurrealDB partial-unique-index capability verified (§4): either confirmed working OR application-side fallback implemented and tested.
- Row-growth ceiling verified: bulk insert 1000 user_profile UPSERT cycles for the same key with confidence drift below threshold ⇒ row count stays bounded (< 5 versions for the key, governed by the delta knob).
- `MIGRATION_NOTES.md` v5.13.0 entry added — operators warned about row-count growth on `user_profile` and `derived_belief`, with `vacuum_now` recommendation post-deploy if data volume is large.
- `CHANGELOG.md` v5.13.0 entry added.
- Roadmap wiki + `AUDIT_DECISIONS.md` (when it exists) updated: Item 3 status moves 75% → 100%.

---

## 9. Effort estimate

| Task | Estimate |
|---|---|
| Migration 009 + 010 (mirror 007 template) | 0.5 day |
| SurrealDB partial-index capability verification + fallback path | 0.5 day |
| `insert_profile` rework + delta-knob | 0.5 day |
| `insert_derived_belief` supersede logic | 0.25 day |
| `_VALID_EDGE_TABLES` extension + read-helper filters | 0.25 day |
| `as_of_filter` helper + wiring through `get_all_causal_edges`, `get_full_graph`, `recall` MCP tool | 1 day |
| Tests T1–T6 (~14 tests, mostly schema-level) | 1 day |
| MIGRATION_NOTES + CHANGELOG + wiki refresh | 0.25 day |
| **Total** | **~4 days** of focused agent work, single feature branch |

Risk multiplier ×1.5 for partial-index verification → real estimate **5–6 days** including SurrealDB version-capability discovery. No external dependencies.

---

## 10. Migration notes (excerpt for `MIGRATION_NOTES.md`)

Operators upgrading from v5.12.x to v5.13.0:

- **Schema migrations 009 + 010 run automatically on first daemon start.** No manual intervention.
- **Behaviour change — `insert_profile`:** profile UPSERT becomes "supersede + insert new row" when `attribute_value` changes. Row count for `user_profile` will grow over time. Mitigations: (a) `PROFILE_BITEMPORAL_VERSION_DELTA` env knob (default `0.05`) suppresses row creation for noise-level confidence drift; (b) `vacuum_now` retains historical rows by default — to prune, set `VACUUM_USER_PROFILE_HISTORY_DAYS` (new knob, default `None` = keep forever).
- **Behaviour change — `insert_derived_belief`:** by default, new beliefs for the same `(subject, belief_type, directory_context)` now close prior rows. Callers wanting old behaviour must pass `supersede=False`.
- **MCP tool surface:** `recall` gains optional `as_of` parameter (ISO-8601 string). All existing call sites continue to work — default `None` preserves current behaviour.
- **Storage cost estimate:** at typical write rates (~5 profile facts/day per project) and 5-version average ceiling per key, `user_profile` row count grows by ~5×; `derived_belief` by ~2–3× over a year. Negligible for typical yadgar deployments (<1 MB).

---

## 11. Deferred decisions (per AUDIT_DECISIONS.md protocol)

The following are deliberate "NOT NOW" decisions with revisit triggers. Each goes into `docs/AUDIT_DECISIONS.md` (when that file is committed by the audit-decisions coordinator) and into the roadmap-wiki deferred-items section.

| Item | Decision | Revisit when |
|---|---|---|
| **Tier 2 — `wiki_crossref` bi-temporal** | DEFER | User asks for "wiki rename history" feature OR a citation-tracing path requires reconstructing wiki link state at past timestamps. |
| **Tier 2 — `memory_transition` bi-temporal** | DEFER | Recall-quality investigation needs co-recall history (e.g. "did transition pattern shift after consolidation event X?"). |
| **Tier 2 — `memory.cluster_id` bi-temporal split-out** | DEFER | A consolidation invariant or audit requires tracking cluster reassignment history. Would require splitting cluster_id FK into a separate `memory_cluster_assignment` join table — invasive. |
| **Bi-temporal columns on `narrative_entry`** | REJECT | Already has `period_start` / `period_end` — overlapping semantics. |
| **Bi-temporal on `checkpoint`** | REJECT | `is_active` flag already encodes current vs historical. |

---

## 12. Version-slot reasoning

Slotted as v5.13.0 because:
- v5.10.x train is hot-fix oriented (consolidate_now, secret-gate, viz fixes, CPU bursts) — schema migrations are too heavy.
- v5.11.0 is reserved for anchor cross-project work, gated on 4 weeks of audit history (mid-June 2026 earliest).
- v5.12.0 is Wiki Bookmarks (MCP-only, no schema migration).
- v5.13.0 — the first free slot for schema work. Independent of all v5.10/v5.11/v5.12 work.
- v5.14.x is reserved for "Recall pipeline plugin architecture (R2)" per AUDIT_DECISIONS. Slotting Adopt-3 BEFORE that lets the plugin architecture optionally consume the `as_of_filter` helper from day one.
- v6.x is the LLM-tier shift — out of scope for schema work.

---

## 13. Open questions for main thread

1. **SurrealDB partial-index support** — does our pinned SurrealDB v3 version support `DEFINE INDEX ... WHERE ...`? If not, fallback to application-side uniqueness check is fine, but the migration text needs a different shape (drop UNIQUE entirely, not redefine partial).
2. **Row-growth ceiling for `user_profile`** — is the `PROFILE_BITEMPORAL_VERSION_DELTA = 0.05` default acceptable, or should it be configurable per-attribute-type (e.g. role-change always inserts new row regardless of confidence delta)?
3. **`recall(as_of=...)`** — should we expose this as a separate MCP tool (`recall_as_of`) for discoverability, or keep it as an optional kwarg on `recall`? This plan defaults to kwarg; main thread may decide otherwise.
4. **Tier 2 trigger sensitivity** — should we ship the read-helper `as_of_filter` for `wiki_crossref` and `memory_transition` even without the `valid_from`/`valid_until` columns, so future migration is a pure additive? (Probably yes — it's a one-line addition to a helper.)
