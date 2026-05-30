# PLAN — v5.13.1: DuckDB analytics export

**Status:** drafted 2026-05-30. Plan-first per I27. Source: Adopt-6 from `docs/competitor-audit-2026-05-30.md` (item 6, "DuckDB analytics export") + decision logged in `docs/AUDIT_DECISIONS.md`.

**Master at draft time:** core v5.10.3 shipped; v5.10.4 in flight on `feat/v5.10.4-consolidate-now-mode-hook-schema`.

**Sequencing:** v5.13.0 slot. Independent of:
- v5.10.x train (consolidation/sleep cycle fixes)
- v5.11.0 (anchor cross-project)
- v5.12.0 (wiki bookmarks viz)
- v5.14.x (R2 retrieval pipeline plugin arch)
- v5.20.0 (roadmap freshness)

No dependency on benchmarks landing (Adopt-1) — this is read-only analytics tooling that can ship anytime.

**Effort estimate:** 3–5 days (matches audit estimate; do not pad/shrink).

---

## Why

Yadgar's current observability answers **operational** questions: latency p50/p99, error rate, queue depth, OTLP span timing. It does NOT answer **behavioral** questions:

- Which memories are recalled most? Least?
- What's the heat decay distribution across the corpus?
- Which tags drive recall efficacy (recall→use ratio)?
- Which anchors are most frequently referenced?
- Do nightly consolidation cycles actually change heat distribution?
- Which memory clusters dominate (semantic domain clustering)?
- What's the contradiction/conflict rate by tag?

SurrealDB is built for OLTP point lookups; scan-heavy analytical aggregations over the full memory corpus (~2.7K rows today, growing) are an anti-pattern. Running these queries against operational SurrealDB also creates contention with serving recall calls.

DuckDB is the right tool: embedded (no server), columnar (fast scan), full SQL (analyst-friendly), MIT-licensed (no infra friction). The competitor audit's framing: "DuckDB + VSS extension could serve as Yadgar's analytics sidecar — export memory snapshots to DuckDB for analytics and debugging — without replacing SurrealDB as the operational store."

This is **low-impact, low-effort, high-yield-once-shipped** because the moment behavioral queries are possible, audit/debug/curation workflows that today require ad-hoc Claude analysis become one-line SQL.

---

## Scope (chosen: Option C — dump + analytics pack)

Three options were considered:

- **Option A: dump-only.** Ship the export, user writes their own SQL.
- **Option B: incremental.** Export + later merge new rows over time. Adds state tracking + conflict semantics.
- **Option C: dump + analytics pack.** Ship the export PLUS pre-built `VIEW` DDL covering the common behavioral questions above.

**Decision: Option C.**

Rationale:
- Option A leaves the user to figure out the schema and queries. Marginal effort to ship views is small; they multiply the value of the export.
- Option B is over-engineering for a tool labeled "analytics sidecar". Incremental sync requires watermark tracking, conflict resolution, and schema-evolution handling on a lossy snapshot. The whole point is to be cheap and disposable — re-run the export. If exports become expensive (millions of memories), Option B becomes worth revisiting; today (2.7K memories) full re-export is sub-second.
- Option C lands the audit's promised value ("behavioral analytics") in one shipped release without committing to long-running incremental infrastructure.

---

## Non-goals

- **Not a backup.** Explicitly analytics-only and lossy. Backups are owned by `cli/vacuum.py` (SurrealDB `.surql` export) and nightly snapshots. The DuckDB export drops fields that don't matter for analytics (e.g. raw embedding bytes when VSS not present; see schema notes) and is not designed to be reversible. State this in `--help` text.
- **Not real-time.** Snapshot semantics. The exported file reflects DB state at the moment of export; subsequent SurrealDB writes are not propagated. To get fresh data, re-run.
- **Not multi-tenant.** Single DuckDB file per export run.
- **No MCP tool wrapper (v1).** This is an admin/operator op (CLI), not an agent op (MCP). Keeping MCP surface clean and dependency optional. Future MCP wrapper possible if usage justifies it; out of scope here.
- **No incremental / append mode.** See Option B discussion above.
- **No cloud DWH integration.** No BigQuery/Snowflake/MotherDuck targets. Local DuckDB file only.
- **No automatic / scheduled exports.** User invokes manually. (If demand emerges, a systemd timer could wrap it later — out of scope.)
- **No VSS index creation.** The export writes embedding columns as `FLOAT[<dim>]` arrays; user can `INSTALL vss; LOAD vss; CREATE INDEX ... HNSW ...` themselves if they want vector search. Including VSS auto-setup forces the dependency on every user.

---

## CLI surface

```bash
yadgar export duckdb \
    --output /path/to/yadgar-snapshot.duckdb \
    [--include-secrets] \
    [--action-log-since 30d] \
    [--action-log-limit 100000] \
    [--no-views] \
    [--tables memory,wiki_page,...]
```

Sub-subcommand registration: extend `yadgar/cli/__init__.py` with a new `export` module exposing `register(subparsers)` that adds an `export` parser with a `duckdb` sub-subparser. Pattern mirrors how `vacuum`, `stats`, etc. register today (one parser per CLI file; see `yadgar/cli/stats.py:15` `cmd_stats`).

**Flags:**

| Flag | Default | Purpose |
|---|---|---|
| `--output` | required | path to write `.duckdb` file. Overwrites if exists (warn if not `--force`). |
| `--include-secrets` | `False` | export rows even if `secret_flag=true`. Default is to exclude (respect v5.10.2 secret-gate). |
| `--action-log-since` | `30d` | export `action_log` rows newer than this. Accepts `Nd`/`Nh`/`Nm`. `all` = no time filter. |
| `--action-log-limit` | `100000` | hard cap on `action_log` rows, sorted by ts desc. Belt-and-suspenders against runaway tables. |
| `--no-views` | `False` | skip creating the analytics views. Plain table dump only. |
| `--tables` | all | comma-separated subset to export (advanced). |
| `--force` | `False` | overwrite existing output file without prompt. |

**Exit codes:** 0 success, 1 generic failure, 2 DuckDB import unavailable (with help message: `pip install yadgar[analytics]`).

**Lazy import:** the DuckDB module loads `import duckdb` only when the `export duckdb` subcommand actually runs. `pyproject.toml` adds optional extra `analytics = ["duckdb>=0.10"]`. If the user runs the command without the extra installed, the CLI prints a friendly message and exits 2.

---

## Schema mapping (SurrealDB → DuckDB)

### General rules

1. Each SurrealDB SCHEMALESS table → one DuckDB typed table. Explicit per-table column list (no `SELECT *` style inference at export time). Plus one `extra_fields JSON` column for unknown keys, so future SurrealDB field additions don't break the export.
2. SurrealDB `id` (record ID like `memory:abc123`) → DuckDB `id VARCHAR` (stringified). Add `id_table VARCHAR` and `id_pk VARCHAR` for users who want to join.
3. Timestamps stored as ISO strings in SurrealDB → DuckDB `TIMESTAMP`. Parse during export; fall back to NULL + log on parse failure.
4. Vectors (embedding fields, dimension from `EMBEDDING_DIM` setting) → DuckDB `FLOAT[<dim>]` (native fixed-length array). Cosine similarity expressible as `array_cosine_similarity(a, b)` in DuckDB SQL. **NOT** `BLOB` — BLOB requires UDFs to do anything useful.
5. JSON-shaped fields (e.g. `metadata`, `tags`) → DuckDB native `JSON` type. Tag lists also exploded into a separate junction table `memory_tag(memory_id, tag)` for clean `GROUP BY tag` queries (denormalized for analytics speed).
6. SurrealDB record links → DuckDB VARCHAR (preserve original `table:id` form). Don't try to resolve into foreign keys.

### Tables exported (with rationale)

| Table | Why exported |
|---|---|
| `memory` | core — everything else hangs off this. Heat, decay, embedding, tags, content, branch, valid_from/until, tier, secret_flag. |
| `wiki_page` | curated knowledge analytics — coverage by tag, page age, branch distribution. |
| `wiki_draft` | drafts pending approval; useful for "drift between drafts and approved" queries. |
| `wiki_crossref` | wiki internal link graph; analytics on connectedness. |
| `action_log` | recall events, tool calls — THE table for behavioral analytics. Subject to `--action-log-since` / `--action-log-limit` windowing because it grows unbounded. |
| `consolidation_log` | when nightly cycles ran, what they did. Enables before/after heat comparisons. |
| `entity` | KG entity nodes. Domain clustering needs these. |
| `relationship` | KG edges (with v5.3.4 bi-temporal `valid_from`/`valid_until` + v5.10.x `source_memory_id`). |
| `causal_dag_edge` | PC-algorithm output. Even if Adopt-D3 defers a verdict on causal discovery, the exported edges enable post-hoc analysis. |
| `memory_cluster` | semantic clustering output — direct domain-clustering input. |
| `memory_archive` | archived memories; analytics on what gets archived. |
| `memory_transition` | tier transitions (working → archive etc.); shows lifecycle flow. |
| `narrative_entry` | autobiographical narrative; analytics on narrative density per period. |
| `derived_belief` | curator-derived beliefs; analytics on belief volume / contradiction. |
| `user_profile` | profile attributes; persona-stability queries. |
| `memory_rule` | active rules engine state; useful for rule-coverage queries. |
| `prospective_memory` | future-triggered memories; trigger-pattern analytics. |
| `schema_version` | provenance — record which migrations were applied at export time. |
| `memory_similarity_link` | nearest-neighbor links per migration (similarity graph analytics). |

### Tables NOT exported (with rationale)

| Table | Why excluded |
|---|---|
| `counter` | operational scalars (sequence values etc.); no analytic value. |
| `checkpoint` | session-state ephemerals (`/clear` resume). Not behavior, machinery. |
| `engram_slot` | CLS-store internals (`cls_store/`). Low analytic value; high schema churn risk. |
| `file_hash` | content-deduplication state. Operational. |
| `astrocyte_process` | lifecycle scaffolding — process state, not behavior. |
| `memory_embedding_backup` | one-time migration backup created in `vector.py:144`. Pure operational. |

These exclusions are explicit so future agents don't waste cycles re-asking. Adding any later is a one-line whitelist change.

### Secret-gate respect (v5.10.2 integration)

Default behavior: rows where `secret_flag=true` (or any equivalent flag set by v5.10.2 secret-gate) are SKIPPED at SELECT time, NOT after read. The export query embeds a `WHERE secret_flag != true` clause. Counts of redacted rows reported in a `schema_version` row tagged `redacted_count`.

`--include-secrets` opt-in for owner debugging. CLI banner warns when enabled.

If the v5.10.2 secret-gate uses a different field name than `secret_flag`, reconcile during implementation (single source-of-truth lookup; do NOT duplicate field-name logic). See `yadgar/sanitize.py` and v5.10.2 plan files.

---

## Analytics pack (the "C" in Option C)

Ships as DuckDB `CREATE VIEW` DDL inside the same `.duckdb` file (or as a sidecar `.sql` file users can re-apply; pick one — recommend in-file since DuckDB views are cheap and self-documenting).

### Views

1. **`v_decay_distribution`** — histogram of `heat` bucketed into 10 quantiles. Columns: `bucket, count, min_heat, max_heat, avg_access_count`. Answers "what does decay look like across the corpus?".
2. **`v_recall_efficacy_by_tag`** — joins `action_log` (recall events) with `memory_tag`. Columns: `tag, recall_count, distinct_memories_recalled, avg_rank_when_recalled, last_recalled_at`. Answers "which tags actually surface?".
3. **`v_anchor_usage`** — for memories with `anchor_id` set, count refs / last access / decay state. Answers "are our anchors earning their pinning?".
4. **`v_high_heat_memories`** — top-N memories by current heat with content snippet + tag list. Answers "what's the hot core right now?".
5. **`v_domain_clustering`** — joins `memory_cluster` with `memory_tag`. Columns: `cluster_id, dominant_tag, member_count, avg_heat`. Answers "what semantic domains dominate?".
6. **`v_consolidation_effect`** — for each `consolidation_log` row, compute heat avg/sum delta between before/after windows (requires snapshot-of-snapshots; v1 just exposes the log rows joined with timestamp windows and lets users self-compute). Answers "do nightly cycles actually move heat?".
7. **`v_conflict_density`** — count of relationship rows of type `contradicts` per tag. Answers "where do contradictions cluster?".
8. **`v_wiki_coverage`** — wiki pages grouped by tag, with last-update age. Answers "what's documented vs stale?".
9. **`v_tool_call_volume`** — `action_log` aggregated by tool name + day. Answers "which MCP tools are actually used?".
10. **`v_branch_distribution`** — memory counts per branch + per (branch × tag). Answers "are non-master branches diverging?".

Each view comes with a `COMMENT ON VIEW` describing the question it answers. DuckDB supports this; it ships in the file so the file is self-describing without external docs.

`--no-views` flag skips view creation for users who only want raw tables.

---

## Implementation outline

### New files

- `yadgar/cli/export.py` — CLI subcommand registration + dispatcher.
- `yadgar/export/__init__.py`
- `yadgar/export/duckdb_exporter.py` — main export logic. Class `DuckDBExporter` with methods: `connect`, `create_schema`, `export_table(name)`, `create_views`, `close`.
- `yadgar/export/schema.py` — per-table column lists + DuckDB DDL strings. One source of truth for what fields are typed vs in `extra_fields`.
- `yadgar/export/views.sql` — pre-built view DDL. Loaded as a resource and executed.
- `yadgar/tests/test_export_duckdb.py` — unit tests (see Tests section).

### Touched files

- `yadgar/__main__.py` — register `export` CLI module (one line in the registration block).
- `pyproject.toml` — add `[project.optional-dependencies] analytics = ["duckdb>=0.10"]`.
- `CHANGELOG.md` — v5.13.0 entry.
- `MIGRATION_NOTES.md` — note the new optional extra + how to install it.
- `README.md` — one-line section under "CLI commands" pointing to `yadgar export duckdb --help`.
- `docs/AUDIT_DECISIONS.md` — Adopt-6 entry: status → ADOPTED → IMPLEMENTED with PR/SHA link.

### NOT touched (collision avoidance)

- v5.10.4 in-flight files: `yadgar/server/tools/admin_other.py`, `yadgar/scripts/hook_runner.py`, anything under `yadgar/consolidation/`. Verified clean of overlap during planning.
- `yadgar/cli/vacuum.py` — different export concept (SurrealDB `.surql` backup), don't conflate.

---

## Tests (TDD per workflow)

Per "HARD RULE — Test-Driven": failing tests first, then implementation.

### Unit tests

- `test_exporter_creates_tables` — given a fixture SurrealDB with one row per exported table, run exporter, assert DuckDB file has the expected tables with expected row counts.
- `test_exporter_respects_secret_gate` — fixture row with `secret_flag=true` is excluded by default; included when `--include-secrets` passed.
- `test_exporter_action_log_window` — fixture rows older than `--action-log-since` are dropped; row cap honored.
- `test_exporter_embedding_roundtrip` — embedding stored as `FLOAT[dim]`, read back via DuckDB, cosine similarity computes correctly.
- `test_exporter_extra_fields` — fixture row with an unknown field lands in `extra_fields JSON`, not lost.
- `test_exporter_handles_missing_table` — exported table absent from SurrealDB → exporter logs warning, continues, writes empty DuckDB table.
- `test_exporter_skipped_tables_not_present` — confirm `counter`, `checkpoint`, `engram_slot`, `file_hash`, `astrocyte_process`, `memory_embedding_backup` are NOT in the output DuckDB.
- `test_views_created` — when `--no-views` not passed, all 10 views exist in output file.
- `test_views_executable` — every view returns without error on a fixture-populated file.
- `test_cli_lazy_import` — patch `duckdb` import to raise `ImportError`, run CLI, assert exit code 2 + helpful message printed.
- `test_force_flag_required_to_overwrite` — without `--force`, existing output file → exit non-zero with prompt-like error.

### Integration test

- `test_export_full_corpus_smoke` (marked `@pytest.mark.integration`) — spins up real SurrealDB with seed data, runs full export, opens output with `duckdb.connect` and executes every view. Asserts every view returns ≥0 rows without error. Slow; behind integration marker.

### Existing CLI test pattern

Match existing `yadgar/cli/` test style (see `yadgar/tests/test_cli_*.py` if present; otherwise mirror `test_stats.py` pattern).

---

## Acceptance criteria

1. `yadgar export duckdb --output /tmp/snap.duckdb` runs on a seeded test DB, produces a valid `.duckdb` file readable by `duckdb /tmp/snap.duckdb`.
2. All tables in the export-list section exist in the output, no excluded tables present.
3. All 10 views exist and return rows without error on a populated fixture.
4. `--include-secrets` opt-in works; default redacts secret-flagged rows.
5. `--action-log-since 30d` and `--action-log-limit 100000` honored.
6. `--no-views` skips view creation.
7. CLI without `duckdb` installed prints `pip install yadgar[analytics]` and exits 2.
8. CLI with `--help` shows all flags + brief usage.
9. Existing pre-commit (ruff, mccabe ≤15, pylint args ≤8) passes on all new files.
10. New tests added in `yadgar/tests/test_export_duckdb.py`, all passing under `pytest -m 'not integration'`.
11. Integration test runs under `pytest -m integration` on a fresh SurrealDB.
12. README + CHANGELOG + MIGRATION_NOTES updated.
13. `docs/AUDIT_DECISIONS.md` Adopt-6 line updated from "Agent dispatch pending" → ADOPTED with version slot + implementation SHA.

---

## Risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| DuckDB API breakage between 0.10 → 1.x | Medium | Pin `duckdb>=0.10,<2`. Add version-check guard with friendly message. |
| Embedding column doesn't fit `FLOAT[dim]` (variable-length embeddings) | Low | Yadgar uses fixed `EMBEDDING_DIM` from config. Test explicitly. Fall back to `JSON` array if encountered. |
| Schema drift breaks export silently (new SurrealDB field) | High | `extra_fields JSON` catch-all column + warn-log on unknown field. |
| `action_log` table grows unbounded → export OOMs | Medium | `--action-log-limit` cap (default 100k) + `--action-log-since` window (default 30d). Process in batches via SurrealDB `LIMIT/START`. |
| Secret-gate field name changes between releases | Low | Single source-of-truth import from `yadgar/sanitize.py` or `yadgar/secrets.py`. Don't hardcode field name in export module. |
| Output file accidentally checked into git | Low | Add `*.duckdb` to `.gitignore`. |
| Optional-dep install confuses users | Low | Clear error message + README + `--help` text point at `pip install yadgar[analytics]`. |
| Concurrent SurrealDB writes during export → inconsistent snapshot | Medium | Document in `--help` that export is a "best-effort point-in-time snapshot, not transactional". Acceptable for analytics use case. If transactional needed, run after `yadgar vacuum` or during low-write window. |

---

## Open questions (surface, don't resolve)

- **DuckDB VSS bundling.** Ship with `INSTALL vss; LOAD vss` auto-run, or assume user installs themselves? Bundling adds ~few MB to startup and requires network access on first run. Recommend: don't bundle; document the user-side opt-in.
- **Output format: DuckDB native only, or also Parquet export?** Parquet is the more interoperable analytics format (Spark, Polars, Athena all read it). DuckDB can `COPY ... TO 'file.parquet'`. v1 ships DuckDB only; Parquet could be a follow-up flag (`--format parquet`).
- **Lossy-vs-backup confirmation.** Plan says analytics-only / lossy. Is there any case where DuckDB should be treated as a partial backup (e.g. wiki pages, which are user-curated content)? Probably no — user already has `cli/vacuum.py` for canonical backups — but worth explicit confirmation before implementation.
- **Optional-dep extra name.** `analytics` proposed. Alternative: `export` or `duckdb`. `analytics` reads best in `pip install yadgar[analytics]`. Confirm during implementation.

---

## Revisit triggers (record in AUDIT_DECISIONS.md once shipped)

- DuckDB removed from PyPI / project archived (unlikely, MIT-licensed, very active 2026)
- Yadgar memory corpus grows past ~1M memories — Option B (incremental) revisit makes sense
- User demand for MCP wrapper around `export duckdb` (agent-callable)
- User demand for Parquet / Snowflake / BigQuery destinations
- Adopt-1 (formal benchmarks) lands and wants to use the export as a benchmarking input

---

## References

- `docs/competitor-audit-2026-05-30.md` lines 612–625 (DuckDB middle-ground analysis) and line 672 (Adopt-6 recommendation)
- `docs/AUDIT_DECISIONS.md` — Adopt-6 entry (currently "Agent dispatch pending")
- `yadgar/storage/migrations.py:322-345` — canonical SurrealDB table list (source of truth for what's exportable)
- `yadgar/storage/vector.py:144` — `memory_embedding_backup` (excluded; operational)
- `yadgar/vacuum/strip.py` — existing `action_log` strip pattern (informs windowing approach)
- `yadgar/cli/stats.py` — pattern reference for CLI subcommand structure
- `pyproject.toml:138` — existing `[project.optional-dependencies]` block (where `analytics` extra will be added)
