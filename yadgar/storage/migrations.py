"""Schema migrations and _init_schema.

_MigrationsMixin provides:
  - _run_migrations / _run_migrations_locked
  - _init_schema (table/index definitions + runs migrations)

Migration functions (_migration_001..004) are module-level so that
test_branch_schema_migration.py can import them directly:
  from yadgar.storage import _migration_004_branch_field
"""

from __future__ import annotations

import fcntl
import logging
from typing import TYPE_CHECKING

import yadgar.paths as _paths

if TYPE_CHECKING:
    pass  # StorageEngine referenced only in annotations below — no import needed

_log = logging.getLogger(__name__)


# ── Schema migrations ──────────────────────────────────────────────────────────
# Each entry: {"version": str, "fn": callable(StorageEngine) -> None}
# Applied exactly once, in order, on first run with a new version.
# Add new migrations at the END of this list only — never reorder or edit existing ones.


def _migration_001_hnsw_indexes(storage) -> None:
    """Migrate MTREE vector indexes to HNSW (SurrealDB v3 upgrade)."""
    dim = storage._embedding_dim
    # Drop old MTREE indexes (IF EXISTS so it's safe even if already gone)
    for idx in ("memory_embedding_idx", "memory_implicit_idx"):
        storage._q(f"REMOVE INDEX IF EXISTS {idx} ON memory;")
    storage._q("REMOVE INDEX IF EXISTS wiki_embedding_idx ON wiki_page;")
    # Recreate as HNSW
    storage._q(f"""
        DEFINE INDEX IF NOT EXISTS memory_embedding_idx
            ON memory FIELDS embedding
            HNSW DIMENSION {dim} DIST COSINE TYPE F32 EFC 150 M 12;
    """)
    storage._q(f"""
        DEFINE INDEX IF NOT EXISTS memory_implicit_idx
            ON memory FIELDS implicit_embedding
            HNSW DIMENSION {dim} DIST COSINE TYPE F32 EFC 150 M 12;
    """)
    storage._q(f"""
        DEFINE INDEX IF NOT EXISTS wiki_embedding_idx
            ON wiki_page FIELDS embedding
            HNSW DIMENSION {dim} DIST COSINE TYPE F32 EFC 150 M 12;
    """)


def _migration_002_relationship_indexes(storage) -> None:
    """Add indexes on relationship.source_entity_id / target_entity_id (perf v4.4.1)."""
    storage._q("""
        DEFINE INDEX IF NOT EXISTS rel_source_target_idx
            ON relationship FIELDS source_entity_id, target_entity_id;
    """)
    storage._q("""
        DEFINE INDEX IF NOT EXISTS rel_target_source_idx
            ON relationship FIELDS target_entity_id, source_entity_id;
    """)


def _migration_003_memory_similarity_link_table(storage) -> None:
    """Add memory_similarity_link table to stop entity-table bloat (perf v4.4.2)."""
    storage._q("DEFINE TABLE IF NOT EXISTS memory_similarity_link TYPE ANY SCHEMALESS;")
    storage._q("""
        DEFINE INDEX IF NOT EXISTS memory_sim_link_pair_idx
            ON memory_similarity_link FIELDS source_memory_id, target_memory_id UNIQUE;
    """)


def _migration_005_provenance_agent_field(storage) -> None:
    """Add provenance_agent column to memory with default 'default'; backfill NULLs.

    DDL: DEFINE FIELD IF NOT EXISTS is idempotent — safe to call twice.
    Backfill: only updates rows where provenance_agent IS NONE (not set),
    preserving existing non-null values.
    """
    storage._q(
        "DEFINE FIELD IF NOT EXISTS provenance_agent ON TABLE memory TYPE string DEFAULT 'default';"
    )
    # Backfill pre-v5.3 rows that have no provenance_agent value
    storage._q(
        "BEGIN TRANSACTION;\n"
        "UPDATE memory SET provenance_agent = 'default' "
        "WHERE provenance_agent IS NONE;\n"
        "COMMIT TRANSACTION"
    )


def _migration_004_branch_field(storage) -> None:
    """Add nullable branch column to memory + wiki_page; backfill pre-v5 rows.

    DDL: DEFINE FIELD IF NOT EXISTS is idempotent — safe to call twice.
    Backfill: single transaction — partial state impossible on TX failure.
    Pre-v5 rows (branch IS NONE) are tagged 'master' as the canonical default.
    """
    storage._q("DEFINE FIELD IF NOT EXISTS branch ON TABLE memory TYPE option<string>;")
    storage._q("DEFINE FIELD IF NOT EXISTS branch ON TABLE wiki_page TYPE option<string>;")
    # Backfill pre-v5 rows inside a single transaction
    storage._q(
        "BEGIN TRANSACTION;\n"
        "UPDATE memory SET branch = 'master' WHERE branch IS NONE;\n"
        "UPDATE wiki_page SET branch = 'master' WHERE branch IS NONE;\n"
        "COMMIT TRANSACTION"
    )


def _migration_007_bitemporal_edges(storage) -> None:
    """Add valid_from / valid_until bi-temporal columns to all KG edge tables (C1, v5.3.4).

    Three edge tables receive the new columns:
    - causal_dag_edge
    - relationship
    - memory_similarity_link

    valid_from  — datetime (required). Default time::now() applied on insert.
                  Backfilled from created_at if present, else time::now().
    valid_until — datetime (nullable). NULL means currently valid.
                  Filter: valid_until IS NONE OR valid_until > time::now()

    DEFINE FIELD IF NOT EXISTS is idempotent — safe to run twice.
    """
    # Tables are SCHEMALESS. Dates are stored as ISO-8601 strings (same as created_at).
    # TYPE option<string> allows string values; valid_until defaults to NONE (NULL).
    for table in ("causal_dag_edge", "relationship", "memory_similarity_link"):
        storage._q(f"DEFINE FIELD IF NOT EXISTS valid_from ON TABLE {table} TYPE option<string>;")
        storage._q(f"DEFINE FIELD IF NOT EXISTS valid_until ON TABLE {table} TYPE option<string>;")
    # Backfill: set valid_from = created_at for rows that already exist.
    # Rows without created_at receive time::now() via the DEFAULT above on next touch;
    # best-effort backfill sets it explicitly for existing rows.
    storage._q(
        "BEGIN TRANSACTION;\n"
        "UPDATE causal_dag_edge SET valid_from = created_at "
        "WHERE valid_from IS NONE AND created_at IS NOT NONE;\n"
        "UPDATE relationship SET valid_from = created_at "
        "WHERE valid_from IS NONE AND created_at IS NOT NONE;\n"
        "UPDATE memory_similarity_link SET valid_from = created_at "
        "WHERE valid_from IS NONE AND created_at IS NOT NONE;\n"
        "COMMIT TRANSACTION"
    )


def _migration_006_source_memory_id(storage) -> None:
    """Add source_memory_id (citation provenance) to KG edge tables (C3, v5.3.3).

    Three edge tables receive the new column:
    - causal_dag_edge: source_memory_id → the memory that triggered causal discovery.
    - relationship: source_memory_id → the memory that triggered entity linking.
    - memory_similarity_link: citation_source_memory_id → the originating memory
      (field is named 'citation_source_memory_id' because 'source_memory_id' and
      'target_memory_id' are already the primary endpoint keys on this table).

    All columns are optional (nullable). Existing rows have NULL — back-compat.
    DEFINE FIELD IF NOT EXISTS is idempotent — safe to run twice.
    """
    # causal_dag_edge: source_memory_id
    storage._q(
        "DEFINE FIELD IF NOT EXISTS source_memory_id ON TABLE causal_dag_edge TYPE option<int>;"
    )
    # relationship: source_memory_id
    storage._q(
        "DEFINE FIELD IF NOT EXISTS source_memory_id ON TABLE relationship TYPE option<int>;"
    )
    # memory_similarity_link: citation_source_memory_id
    # (source_memory_id / target_memory_id are already the edge endpoint fields)
    storage._q(
        "DEFINE FIELD IF NOT EXISTS citation_source_memory_id "
        "ON TABLE memory_similarity_link TYPE option<int>;"
    )


def _migration_009_wiki_bookmark_table(storage) -> None:
    """Add wiki_bookmark table + slug UNIQUE index (v5.23.0).

    Table is SCHEMALESS for SurrealDB embedded compatibility.
    Unique index on slug enforces one bookmark per wiki page.
    position is a dense integer (0-based) managed by the storage layer.

    Additive only — no impact on existing data.
    """
    storage._q("DEFINE TABLE IF NOT EXISTS wiki_bookmark SCHEMALESS;")
    storage._q("""
        DEFINE INDEX IF NOT EXISTS wiki_bookmark_slug_idx
            ON wiki_bookmark FIELDS slug UNIQUE;
    """)
    storage._q("""
        DEFINE INDEX IF NOT EXISTS wiki_bookmark_position_idx
            ON wiki_bookmark FIELDS position;
    """)


def _migration_008_anchor_tier(storage) -> dict:
    """Add tier / valid_until / migration_grace columns to memory table (v5.8.0).

    DDL: DEFINE FIELD IF NOT EXISTS is idempotent — safe to run twice.

    Backfill:
      All existing _anchor memories without tier set receive:
        tier = 'conditional'
        valid_until = now() + ANCHOR_CONDITIONAL_TTL_DAYS  (default 90 days)
        migration_grace = true

    Skips rows that already have tier set — idempotent.

    Returns dict with anchor_tier_migrated_count for surface signal.
    """
    from datetime import UTC, datetime, timedelta

    # DDL — add new columns (no-op if already present)
    storage._q("DEFINE FIELD IF NOT EXISTS tier ON TABLE memory TYPE option<string>;")
    storage._q("DEFINE FIELD IF NOT EXISTS valid_until ON TABLE memory TYPE option<string>;")
    storage._q("DEFINE FIELD IF NOT EXISTS migration_grace ON TABLE memory TYPE option<bool>;")

    # Load TTL from settings (respects ANCHOR_CONDITIONAL_TTL_DAYS env knob)
    try:
        from yadgar.config import get_settings as _get_settings

        _ttl_days = int(_get_settings().ANCHOR_CONDITIONAL_TTL_DAYS)
    except Exception:
        _ttl_days = 90

    valid_until_str = (datetime.now(UTC) + timedelta(days=_ttl_days)).isoformat()

    # Find all anchors without tier
    rows = storage._q("SELECT id FROM memory WHERE '_anchor' INSIDE tags AND tier IS NONE")
    migrated = 0
    for row in rows:
        mid = storage._extract_id(row.get("id"))
        storage._q(
            f"UPDATE memory:{int(mid)} SET "
            "tier = $tier, valid_until = $vu, migration_grace = $grace",
            {"tier": "conditional", "vu": valid_until_str, "grace": True},
        )
        _log.info(
            "anchor_tier_migration: memory:%d → tier=conditional valid_until=%s",
            mid,
            valid_until_str,
        )
        migrated += 1

    _log.info("anchor_tier_migration complete: migrated=%d", migrated)
    return {"anchor_tier_migrated_count": migrated}


def _migration_010_bitemporal_user_profile(storage) -> None:
    """Add valid_from / valid_until to user_profile (Adopt-3, v5.29.0).

    Pivot semantics: from "UPSERT in-place" to "close prior row + insert new row".

    SurrealDB v3.0.5 does not support DEFINE INDEX ... WHERE (partial index), so
    the old UNIQUE constraint on (entity_name, attribute_type, attribute_key,
    directory_context) is DROPPED entirely. Uniqueness for currently-valid rows is
    enforced application-side in insert_profile (query valid_until IS NONE first).

    DEFINE FIELD IF NOT EXISTS is idempotent — safe to run twice.
    Backfill: set valid_from = created_at on existing rows (best-effort).
    """
    storage._q("DEFINE FIELD IF NOT EXISTS valid_from ON TABLE user_profile TYPE option<string>;")
    storage._q("DEFINE FIELD IF NOT EXISTS valid_until ON TABLE user_profile TYPE option<string>;")

    # Backfill valid_from from created_at on existing rows
    storage._q(
        "BEGIN TRANSACTION;\n"
        "UPDATE user_profile SET valid_from = created_at "
        "WHERE valid_from IS NONE AND created_at IS NOT NONE;\n"
        "COMMIT TRANSACTION"
    )

    # Drop the old unconditional UNIQUE index — application-side enforcement replaces it.
    # REMOVE INDEX IF EXISTS is idempotent.
    storage._q("REMOVE INDEX IF EXISTS profile_unique_idx ON user_profile;")


def _migration_011_bitemporal_derived_belief(storage) -> None:
    """Add valid_from / valid_until to derived_belief (Adopt-3, v5.29.0).

    derived_belief has no UNIQUE constraint, so no index rework needed.
    Existing rows are append-only (no UPSERT); backfill sets valid_from = created_at.

    DEFINE FIELD IF NOT EXISTS is idempotent — safe to run twice.
    """
    storage._q("DEFINE FIELD IF NOT EXISTS valid_from ON TABLE derived_belief TYPE option<string>;")
    storage._q(
        "DEFINE FIELD IF NOT EXISTS valid_until ON TABLE derived_belief TYPE option<string>;"
    )

    # Backfill: every existing belief is the current one for its group.
    # valid_from = created_at; valid_until = NONE (currently valid).
    storage._q(
        "BEGIN TRANSACTION;\n"
        "UPDATE derived_belief SET valid_from = created_at "
        "WHERE valid_from IS NONE AND created_at IS NOT NONE;\n"
        "COMMIT TRANSACTION"
    )


def _migration_012_memory_block_table(storage) -> None:
    """Add memory_block table for named in-context memory blocks (v5.33.0, Adopt-4).

    Separate table (not extending memory) for cleaner isolation:
    - No cross-contamination with anchor audit, heat decay, or secret-gate scans.
    - No field aliasing complications.
    - Uniqueness enforced application-side (no DB-level constraint to allow
      schema flexibility in v5.33.x).

    Schema is SCHEMALESS for SurrealDB embedded compatibility.
    Unique index on (name, scope, directory) enforced by application layer.
    Additive only — no impact on existing data.
    """
    storage._q("DEFINE TABLE IF NOT EXISTS memory_block SCHEMALESS;")
    storage._q("""
        DEFINE INDEX IF NOT EXISTS memory_block_name_scope_dir_idx
            ON memory_block FIELDS name, scope, directory;
    """)
    storage._q("""
        DEFINE INDEX IF NOT EXISTS memory_block_scope_dir_idx
            ON memory_block FIELDS scope, directory;
    """)


def _migration_013_wiki_page_version(storage) -> None:
    """Add wiki_page_version table for per-write version history (v5.41.0).

    Three-stage idempotent migration:
    1. DDL — define table + three indexes.
    2. Seed — for every existing wiki_page row, create version=1 row (skip if
       versions already exist for that page_id — idempotency guard).
    3. No-op on subsequent calls via schema_version table guard (standard pattern).

    Schema is SCHEMALESS. Embedding intentionally excluded from version rows
    (storage cost; recomputed on restore). Unique (page_id, version) index
    enforces correct increment ordering.
    """
    import json as _json  # noqa: PLC0415

    # Stage 1: DDL
    storage._q("DEFINE TABLE IF NOT EXISTS wiki_page_version SCHEMALESS;")
    storage._q("""
        DEFINE INDEX IF NOT EXISTS wiki_page_version_page_idx
            ON wiki_page_version FIELDS page_id;
    """)
    storage._q("""
        DEFINE INDEX IF NOT EXISTS wiki_page_version_page_version_idx
            ON wiki_page_version FIELDS page_id, version UNIQUE;
    """)
    storage._q("""
        DEFINE INDEX IF NOT EXISTS wiki_page_version_created_idx
            ON wiki_page_version FIELDS created_at;
    """)

    # Stage 2: Seed — create version=1 from every existing wiki_page row
    # that has no version rows yet (idempotency guard).
    pages = storage._q("SELECT * FROM wiki_page")
    now = storage._now_iso()
    for page in pages:
        raw_id = page.get("id")
        page_id = storage._extract_id(raw_id)
        if page_id is None:
            continue

        existing_versions = storage._q(
            "SELECT id FROM wiki_page_version WHERE page_id = $p LIMIT 1",
            {"p": page_id},
        )
        if existing_versions:
            continue  # already seeded — skip

        vid = storage._next_id("wiki_page_version")
        row = {
            "id": vid,
            "page_id": page_id,
            "version": 1,
            "title": page.get("title", ""),
            "content": page.get("content", ""),
            "category": page.get("category"),
            "tags": page.get("tags", []),
            "confidence": page.get("confidence"),
            "source_memory_ids": page.get("source_memory_ids", []),
            "branch": page.get("branch"),
            "change_summary": "initial version",
            "created_at": now,
            "provenance_agent": "migration_seed",
        }

        if storage._db_url:
            # Server mode: LET preamble + SQL
            lets = [f"LET ${k} = {_json.dumps(v, ensure_ascii=False)}" for k, v in row.items()]
            body = (
                ";\n".join(
                    lets
                    + [
                        "CREATE type::record('wiki_page_version', $id) SET "
                        "page_id = $page_id, version = $version, title = $title, "
                        "content = $content, category = $category, tags = $tags, "
                        "confidence = $confidence, "
                        "source_memory_ids = $source_memory_ids, branch = $branch, "
                        "change_summary = $change_summary, created_at = $created_at, "
                        "provenance_agent = $provenance_agent"
                    ]
                )
                + ";"
            )
            resp = storage._http.post(
                "/sql", content=body.encode(), headers={"Content-Type": "text/plain"}
            )
            resp.raise_for_status()
        else:
            # Embedded mode
            storage._q(
                "CREATE type::record('wiki_page_version', $id) SET "
                "page_id = $page_id, version = $version, title = $title, "
                "content = $content, category = $category, tags = $tags, "
                "confidence = $confidence, "
                "source_memory_ids = $source_memory_ids, branch = $branch, "
                "change_summary = $change_summary, created_at = $created_at, "
                "provenance_agent = $provenance_agent",
                {k: v for k, v in row.items()},
            )


def _migration_014_wiki_page_embedding_backfill(storage) -> None:
    """Register schema migration slot for wiki_page embedding backfill (v5.42.1).

    This migration marks the version slot so the framework records it as applied.
    The actual backfill — encoding NULL-embedding wiki_page rows with the live
    embedding model — cannot run here because storage migrations execute before
    the EmbeddingEngine is initialised in init_engines().

    The real backfill runs via WikiStore.backfill_null_embeddings(), called from
    server/lifecycle.py after both StorageEngine and EmbeddingEngine are ready.
    This split is intentional (I1: thin request path; backfill is a one-time
    startup cost, not an in-handler operation).

    Idempotent: re-running this migration is a no-op (version already in
    schema_version table prevents re-entry). The backfill itself is also
    idempotent — subsequent calls find 0 NULL rows and exit immediately.
    """
    # Count NULL-embedding rows and emit a diagnostic log.
    # This runs only once (on first startup after upgrade).
    rows = storage._q("SELECT count() AS c FROM wiki_page WHERE embedding IS NONE GROUP ALL")
    null_count = int(rows[0].get("c", 0)) if rows else 0
    if null_count > 0:
        _log.warning(
            "migration_014: %d wiki_page rows have embedding=NULL — "
            "backfill will run after EmbeddingEngine is ready",
            null_count,
        )
    else:
        _log.info("migration_014: no NULL-embedding wiki_page rows found — nothing to backfill")


def _migration_015_wiki_draft_branch(storage) -> None:
    """Add branch column to wiki_draft table (v5.42.3).

    DDL: DEFINE FIELD IF NOT EXISTS is idempotent — safe to run twice.
    Backfill: existing rows get branch=None (canonical slot) implicitly since
    the field is option<string> and absent = NULL in SurrealDB.

    Rationale: wiki_approve previously lost the originating branch — drafts
    had no branch column so every approval wrote to the NULL-branch canonical
    slot regardless of the branch context at draft-creation time. Migration 015
    adds the column; wiki_add draft path now stores branch; wiki_approve reads
    and propagates it. Legacy NULL-branch drafts use _internal=True carve-out
    (backward-compat path, now explicit rather than accidental).
    """
    storage._q("DEFINE FIELD IF NOT EXISTS branch ON TABLE wiki_draft TYPE option<string>;")
    _log.info("migration_015: added branch column to wiki_draft (option<string>)")


# ── tag sets for migration_016 backfill heuristic ────────────────────────────
_AWS_TAGS: frozenset[str] = frozenset(
    {
        "s3",
        "iam",
        "lambda",
        "sns",
        "sqs",
        "cloudfront",
        "route53",
        "rds",
        "ec2",
        "dynamodb",
        "aws",
        # Extended tags from corpus analysis (v5.42.6)
        "eks",
        "eventbridge",
        "kafka",
        "msk",
        "cloudformation",
    }
)

_YADGAR_TAGS: frozenset[str] = frozenset({"yadgar"})
_NIX_TAGS: frozenset[str] = frozenset({"nix"})
_LEDGER_TAGS: frozenset[str] = frozenset({"ledger"})


def _classify_directory_by_tags(tags: set[str]) -> str:
    """Apply tag-based heuristic to assign a directory_context value.

    Priority order (first match wins):
    1. 'yadgar' tag → /home/max/git/yadgar
    2. 'nix' tag (without aws) → /home/max/git/nix
    3. 'ledger' tag → /home/max/git/ledger
    4. Any AWS infra tag → /home/max/aws-work
    5. otherwise → 'global'

    Used by both migration 016 (original, now fixed) and migration 018 (repair).
    """
    if tags & _YADGAR_TAGS:
        return "/home/max/git/yadgar"
    if tags & _NIX_TAGS and not (tags & _AWS_TAGS):
        return "/home/max/git/nix"
    if tags & _LEDGER_TAGS:
        return "/home/max/git/ledger"
    if tags & _AWS_TAGS:
        return "/home/max/aws-work"
    return "global"


def _migration_016_directory_context(storage) -> None:  # noqa: C901
    """Add directory_context NOT NULL to wiki_page + memory (v5.42.5).

    Phase A — backfill wiki_page rows (tag-based heuristic):
      - tag contains 'yadgar' → /home/max/git/yadgar
      - tag contains aws/cloud infra terms → /home/max/git/aws-work
      - otherwise → 'global'
    Backfill runs BEFORE the schema constraint so existing rows remain readable.

    Phase B — define schema constraint for wiki_page (NOT NULL, len > 0).
    Phase C — add index on wiki_page.directory_context.

    Phase D — backfill memory rows (NULL / empty '' → 'global').
              Preserves 'system' and existing non-empty values.
    Phase E — define schema constraint for memory.directory_context.

    Phase F — add directory_context (option<string>) to wiki_draft for future use.

    DP-2 (open design point): directory values are not validated against disk
    — arbitrary non-empty strings accepted (including deleted-repo archaeology).
    DP-3: trailing slashes normalised at write time (rstrip("/")); no symlink
    resolution; no case-folding.

    Idempotent: DEFINE FIELD IF NOT EXISTS is safe to re-run.
    """
    # Phase A: backfill wiki_page rows that have no directory_context yet.
    # v5.42.6 fix: fetch ALL rows + Python-filter for absent/empty directory_context.
    # SurrealDB `IS NONE` matches explicit-NULL only — NOT field-absent rows from pre-DEFINE
    # records. Python-side filter catches both field-absent (key missing) and explicit-NULL.
    all_wiki_rows = storage._q("SELECT id, tags, directory_context FROM wiki_page")
    rows = [r for r in all_wiki_rows if r.get("directory_context") in (None, "")]
    backfilled = 0
    for row in rows:
        tags = set(row.get("tags") or [])
        dc = _classify_directory_by_tags(tags)
        raw_id = row.get("id")
        # Extract numeric ID — SurrealDB HTTP returns "wiki_page:N"; type::record() needs N.
        try:
            num_id = storage._extract_id(raw_id)
        except Exception:
            _log.warning("migration_016: could not parse id %r — skipping", raw_id)
            continue
        try:
            storage._q(
                "UPDATE type::record('wiki_page', $id) SET directory_context = $dc",
                {"id": num_id, "dc": dc},
            )
            backfilled += 1
        except Exception as _e:
            _log.warning(
                "migration_016: backfill failed for wiki_page id=%s (%s) — defaulting to 'global'",
                raw_id,
                _e,
            )
            try:
                storage._q(
                    "UPDATE type::record('wiki_page', $id) SET directory_context = 'global'",
                    {"id": num_id},
                )
                backfilled += 1
            except Exception as _e2:
                _log.error(
                    "migration_016: fallback backfill also failed for id=%s: %s",
                    raw_id,
                    _e2,
                )
    _log.info("migration_016: backfilled %d wiki_page rows with directory_context", backfilled)

    # Phase B: define wiki_page.directory_context schema constraint (NOT NULL)
    storage._q(
        "DEFINE FIELD IF NOT EXISTS directory_context ON TABLE wiki_page TYPE string "
        "ASSERT $value != NONE AND string::len($value) > 0;"
    )

    # Phase C: add index for wiki_page.directory_context
    storage._q(
        "DEFINE INDEX IF NOT EXISTS wiki_page_directory_context_idx "
        "ON TABLE wiki_page FIELDS directory_context;"
    )

    # Phase D: backfill memory rows with empty/None directory_context.
    # v5.42.6 fix: Python-side filter (same IS NONE bug as Phase A).
    all_mem_rows = storage._q("SELECT id, directory_context FROM memory")
    mem_rows = [r for r in all_mem_rows if r.get("directory_context") in (None, "")]
    mem_backfilled = 0
    for row in mem_rows:
        raw_mem_id = row.get("id")
        try:
            num_mem_id = storage._extract_id(raw_mem_id)
        except Exception:
            _log.warning("migration_016: could not parse memory id %r — skipping", raw_mem_id)
            continue
        try:
            storage._q(
                "UPDATE type::record('memory', $id) SET directory_context = 'global'",
                {"id": num_mem_id},
            )
            mem_backfilled += 1
        except Exception as _e:
            _log.warning(
                "migration_016: memory backfill failed for id=%s: %s",
                raw_mem_id,
                _e,
            )
    _log.info(
        "migration_016: backfilled %d memory rows with directory_context='global'", mem_backfilled
    )

    # Phase E: define memory.directory_context schema constraint (NOT NULL)
    storage._q(
        "DEFINE FIELD IF NOT EXISTS directory_context ON TABLE memory TYPE string "
        "ASSERT $value != NONE AND string::len($value) > 0;"
    )

    # Phase F: add directory_context to wiki_draft (option<string> — nullable for
    # legacy rows; explicit backward-compat path). NOT NULL deferred to v5.43+.
    storage._q(
        "DEFINE FIELD IF NOT EXISTS directory_context ON TABLE wiki_draft TYPE option<string>;"
    )

    _log.info(
        "migration_016: directory_context schema constraints applied to wiki_page + memory + wiki_draft"
    )


def _migration_018_directory_context_backfill_repair(storage) -> None:  # noqa: C901
    """Repair directory_context backfill for deployed databases (v5.42.6).

    Migration 016 (v5.42.5) had a bug: its SurrealDB `WHERE directory_context IS NONE`
    query only matched rows with an *explicit* NULL value — not rows where the field
    is entirely absent (pre-DEFINE records). All 200+ legacy wiki_page rows were missed.

    This migration re-applies the tag-based heuristic backfill using the corrected
    Python-side filter (fetch-all + `row.get("directory_context") in (None, "")`).

    Empirical finding (v5.42.6): SurrealDB throws a coerce error even on
    `UPDATE ... SET directory_context = $value` when the DEFINE FIELD ASSERT was
    applied (migration 016 Phase B) and the row has a field-absent value. SurrealDB
    validates ALL defined fields on every UPDATE, causing:
        "Couldn't coerce value for field `directory_context`: Expected `string` but found `NONE`"

    Workaround: temporarily relax the schema to `option<string>` before the backfill
    (Phase A), do the UPDATE, then re-tighten to `string NOT NULL` after (Phase C).

    Idempotent: rows already having a non-empty directory_context are skipped.

    Note: migration 017 is reserved for v5.61 (wiki_source_hash). This migration
    takes number 018 as the next available slot.
    """
    # Phase A: temporarily relax wiki_page.directory_context to allow NONE during UPDATE.
    # This is required because SurrealDB validates ASSERT on every UPDATE, not just the
    # updated field — field-absent rows trigger coerce errors without this relaxation.
    # OVERWRITE is required: migration 016 already defined this field; without OVERWRITE
    # SurrealDB v3 rejects the redefinition.
    storage._q("DEFINE FIELD OVERWRITE directory_context ON TABLE wiki_page TYPE option<string>;")

    # Phase B: backfill wiki_page rows with missing/empty directory_context.
    all_wiki_rows = storage._q("SELECT id, tags, directory_context FROM wiki_page")
    wiki_rows_to_fix = [r for r in all_wiki_rows if r.get("directory_context") in (None, "")]
    wiki_backfilled = 0
    wiki_buckets: dict[str, int] = {}

    for row in wiki_rows_to_fix:
        tags = set(row.get("tags") or [])
        dc = _classify_directory_by_tags(tags)
        # Extract the numeric part of the record ID.
        # SurrealDB HTTP returns id as "wiki_page:N"; type::record() requires just N (int).
        raw_id = row.get("id")
        try:
            num_id = storage._extract_id(raw_id)
        except Exception:
            _log.warning("migration_018: could not parse id %r — skipping", raw_id)
            continue
        try:
            storage._q(
                "UPDATE type::record('wiki_page', $id) SET directory_context = $dc",
                {"id": num_id, "dc": dc},
            )
            wiki_backfilled += 1
            wiki_buckets[dc] = wiki_buckets.get(dc, 0) + 1
        except Exception as _e:
            _log.warning(
                "migration_018: backfill failed for wiki_page id=%s (%s) — defaulting to 'global'",
                raw_id,
                _e,
            )
            try:
                storage._q(
                    "UPDATE type::record('wiki_page', $id) SET directory_context = 'global'",
                    {"id": num_id},
                )
                wiki_backfilled += 1
                wiki_buckets["global"] = wiki_buckets.get("global", 0) + 1
            except Exception as _e2:
                _log.error(
                    "migration_018: fallback backfill also failed for wiki_page id=%s: %s",
                    raw_id,
                    _e2,
                )

    _log.info(
        "migration_018: backfilled %d wiki_page rows — buckets: %s",
        wiki_backfilled,
        wiki_buckets,
    )

    # Phase C: re-tighten wiki_page.directory_context to NOT NULL string.
    storage._q(
        "DEFINE FIELD OVERWRITE directory_context ON TABLE wiki_page TYPE string "
        "ASSERT $value != NONE AND string::len($value) > 0;"
    )

    # Phase D: temporarily relax memory.directory_context similarly.
    storage._q("DEFINE FIELD OVERWRITE directory_context ON TABLE memory TYPE option<string>;")

    # Phase E: backfill memory rows with missing/empty directory_context.
    all_mem_rows = storage._q("SELECT id, directory_context FROM memory")
    mem_rows_to_fix = [r for r in all_mem_rows if r.get("directory_context") in (None, "")]
    mem_backfilled = 0

    for row in mem_rows_to_fix:
        raw_mem_id = row.get("id")
        try:
            num_mem_id = storage._extract_id(raw_mem_id)
        except Exception:
            _log.warning("migration_018: could not parse memory id %r — skipping", raw_mem_id)
            continue
        try:
            storage._q(
                "UPDATE type::record('memory', $id) SET directory_context = 'global'",
                {"id": num_mem_id},
            )
            mem_backfilled += 1
        except Exception as _e:
            _log.warning(
                "migration_018: memory backfill failed for id=%s: %s",
                raw_mem_id,
                _e,
            )

    _log.info(
        "migration_018: backfilled %d memory rows with directory_context='global'",
        mem_backfilled,
    )

    # Phase F: re-tighten memory.directory_context to NOT NULL string.
    storage._q(
        "DEFINE FIELD OVERWRITE directory_context ON TABLE memory TYPE string "
        "ASSERT $value != NONE AND string::len($value) > 0;"
    )


def _migration_020_memory_graph_prior(storage) -> None:
    """Add graph_prior (option<float>) to memory table (v5.54.1).

    Precomputed entity-graph centrality scalar stored during consolidation.
    Additive, nullable — no row rewrite needed. Existing memories have graph_prior=NONE,
    which the fusion layer treats identically to 0.0 (no boost).

    The field is computed by ConsolidationScheduler._compute_graph_priors() on each
    consolidation cadence (typically nightly). Staleness window = one consolidation
    cycle — acceptable; the prior is a secondary nudge, not a primary signal.

    Idempotent: DEFINE FIELD IF NOT EXISTS is safe to re-run.
    Note: option<float> — SurrealDB will coerce JSON number to float at write time.
    Live server verification recommended on first deploy (no backfill needed).
    """
    storage._q("DEFINE FIELD IF NOT EXISTS graph_prior ON TABLE memory TYPE option<float>;")
    _log.info("migration_020: added graph_prior field to memory table (additive/nullable, v5.54.1)")


def _migration_021_memory_cofire_prior(storage) -> None:
    """Add cofire_prior (option<float>) to memory table (v5.54.2).

    Precomputed co-recall (transition-edge) prior stored during consolidation.
    Additive, nullable — no row rewrite needed. Existing memories have cofire_prior=NONE,
    which the fusion layer treats identically to 0.0 (no boost).

    The field is computed by ConsolidationScheduler._compute_cofire_priors() on each
    consolidation cadence (typically nightly). Formula: sum of memory_transition.count
    where the memory appears as from_memory_id or to_memory_id, normalized to [0,1]
    across all candidates in the bounded cycle.

    Staleness window: one consolidation cycle — acceptable; the prior is a secondary
    nudge, not a primary retrieval signal. "Recalled together before" = learned
    association from the memory_transition table.

    Idempotent: DEFINE FIELD IF NOT EXISTS is safe to re-run.
    Note: option<float> — SurrealDB will coerce JSON number to float at write time.
    """
    storage._q("DEFINE FIELD IF NOT EXISTS cofire_prior ON TABLE memory TYPE option<float>;")
    _log.info(
        "migration_021: added cofire_prior field to memory table (additive/nullable, v5.54.2)"
    )


def _migration_019_wiki_page_type(storage) -> None:
    """Add page_type (option<string>) and wiki_schema_version (option<int>) to wiki_page (v5.53.2).

    Additive, nullable — no row rewrite. Existing pages have these fields absent (NONE),
    which is the correct untyped state. New typed pages written via wiki_add(page_type=...)
    will have these fields set.

    page_type: one of the PAGE_TYPES registry keys (function, module, service,
      architecture, decision, analysis). Nullable (option<string>) so legacy pages
      remain readable without any update.
    wiki_schema_version: integer schema stamp. 1 = v5.53.2 B-schema. 0 / absent = pre-5.53.2.
      Nullable (option<int>) for the same backward-compat reason.

    Idempotent: DEFINE FIELD IF NOT EXISTS is safe to re-run.
    """
    storage._q("DEFINE FIELD IF NOT EXISTS page_type ON TABLE wiki_page TYPE option<string>;")
    storage._q(
        "DEFINE FIELD IF NOT EXISTS wiki_schema_version ON TABLE wiki_page TYPE option<int>;"
    )
    _log.info(
        "migration_019: added page_type + wiki_schema_version fields to wiki_page (additive/nullable)"
    )


def _migration_022_shadow_gate_fields(storage) -> None:
    """Add shadow-gate fields to memory table (v5.73.0).

    surprise_score (option<float>) — the WRITE GATE's surprisal score, distinct from
      the thermo compute_surprise() score used for heat boost.  Already usable via
      update_memory_fields; DEFINE FIELD IF NOT EXISTS is idempotent.
    would_reject (option<bool>)   — True when gate WOULD reject at WRITE_GATE_SHADOW_THRESHOLD.
      WRITE_GATE_THRESHOLD stays 0.0 — nothing is dropped; this is a shadow stamp only.

    Both fields are nullable (option<>) so pre-migration rows have NONE (no boost/effect).
    No backfill needed — historical memories simply lack the shadow stamp.
    Idempotent: DEFINE FIELD IF NOT EXISTS is safe to re-run.
    """
    storage._q("DEFINE FIELD IF NOT EXISTS surprise_score ON TABLE memory TYPE option<float>;")
    storage._q("DEFINE FIELD IF NOT EXISTS would_reject ON TABLE memory TYPE option<bool>;")
    _log.info(
        "migration_022: added surprise_score (gate surprisal) + would_reject "
        "(shadow decision) fields to memory table (nullable, v5.73.0)"
    )


def _migration_023_memory_directory_context_backfill(storage) -> None:
    """Pre-flip backfill: guarantee all memory rows have directory_context='global' (v5.80).

    Context:
      Migration 016 Phase D/E and migration 018 Phase D/E/F both backfilled memory rows
      with absent/empty/NULL directory_context to 'global'.  On any DB that ran 018 the
      memory.directory_context DEFINE FIELD ASSERT (NOT NULL, len > 0) prevents any new
      field-absent insert, so this migration is effectively a no-op on deployed databases.

      This migration runs as a defensive pre-flip gate immediately before
      UNIFIED_RECALL_ENABLED is enabled by default (v5.80).  It re-checks and repairs
      any residual field-absent rows on databases that may have been upgraded in a
      non-standard order or that ran 016 but not 018 (edge case).

    Phases mirror migration 018 memory phases (D/E/F):
      Phase A — temporarily relax memory.directory_context to option<string> so the
                UPDATE does not trigger the ASSERT coerce error on field-absent rows.
                (SurrealDB v3 validates ASSERT on every UPDATE including the SET field,
                causing "Expected `string` but found `NONE`" on field-absent rows.)
                OVERWRITE is required because 016 already defined this field.
      Phase B — fetch all memory rows; Python-filter for absent/empty/NULL
                directory_context; UPDATE each to 'global'. Skip already-stamped rows.
      Phase C — re-tighten memory.directory_context to the original NOT NULL string
                ASSERT (restores the constraint relaxed in Phase A).

    Idempotent: rows already having a non-empty directory_context are skipped.
    On a fully-migrated database this migration touches 0 rows.
    """
    # Phase A: temporarily relax the ASSERT so field-absent rows accept the UPDATE.
    storage._q("DEFINE FIELD OVERWRITE directory_context ON TABLE memory TYPE option<string>;")

    # Phase B: fetch all rows; Python-filter catches field-absent (key missing),
    # explicit NULL (IS NONE), and empty string — same filter as 016 + 018.
    all_mem_rows = storage._q("SELECT id, directory_context FROM memory")
    rows_to_fix = [r for r in all_mem_rows if r.get("directory_context") in (None, "")]
    mem_backfilled = 0

    for row in rows_to_fix:
        raw_id = row.get("id")
        try:
            num_id = storage._extract_id(raw_id)
        except Exception:
            _log.warning("migration_023: could not parse memory id %r — skipping", raw_id)
            continue
        try:
            storage._q(
                "UPDATE type::record('memory', $id) SET directory_context = 'global'",
                {"id": num_id},
            )
            mem_backfilled += 1
        except Exception as _e:
            _log.warning(
                "migration_023: memory backfill failed for id=%s: %s",
                raw_id,
                _e,
            )

    _log.info(
        "migration_023: backfilled %d memory rows with directory_context='global' "
        "(pre-flip gate for UNIFIED_RECALL_ENABLED default-on)",
        mem_backfilled,
    )

    # Phase C: re-tighten memory.directory_context to NOT NULL string.
    storage._q(
        "DEFINE FIELD OVERWRITE directory_context ON TABLE memory TYPE string "
        "ASSERT $value != NONE AND string::len($value) > 0;"
    )


_MIGRATIONS: list[dict] = [  # noqa: E501 — append only, never reorder
    {"version": "001_hnsw_indexes", "fn": _migration_001_hnsw_indexes},
    {"version": "002_relationship_indexes", "fn": _migration_002_relationship_indexes},
    {
        "version": "003_memory_similarity_link_table",
        "fn": _migration_003_memory_similarity_link_table,
    },
    {"version": "004_branch_field", "fn": _migration_004_branch_field},
    {
        "version": "005_provenance_agent_field",
        "fn": _migration_005_provenance_agent_field,
    },
    {
        "version": "006_source_memory_id",
        "fn": _migration_006_source_memory_id,
    },
    {
        "version": "007_bitemporal_edges",
        "fn": _migration_007_bitemporal_edges,
    },
    {
        "version": "008_anchor_tier",
        "fn": _migration_008_anchor_tier,
    },
    {
        "version": "009_wiki_bookmark_table",
        "fn": _migration_009_wiki_bookmark_table,
    },
    {
        "version": "010_bitemporal_user_profile",
        "fn": _migration_010_bitemporal_user_profile,
    },
    {
        "version": "011_bitemporal_derived_belief",
        "fn": _migration_011_bitemporal_derived_belief,
    },
    {
        "version": "012_memory_block_table",
        "fn": _migration_012_memory_block_table,
    },
    {
        "version": "013_wiki_page_version",
        "fn": _migration_013_wiki_page_version,
    },
    {
        "version": "014_wiki_page_embedding_backfill",
        "fn": _migration_014_wiki_page_embedding_backfill,
    },
    {
        "version": "015_wiki_draft_branch",
        "fn": _migration_015_wiki_draft_branch,
    },
    {
        "version": "016_directory_context",
        "fn": _migration_016_directory_context,
    },
    # NOTE: 017 is RESERVED for v5.61 (wiki_source_hash table). Do not use.
    {
        "version": "018_directory_context_backfill_repair",
        "fn": _migration_018_directory_context_backfill_repair,
    },
    {
        "version": "019_wiki_page_type",
        "fn": _migration_019_wiki_page_type,
    },
    {
        "version": "020_memory_graph_prior",
        "fn": _migration_020_memory_graph_prior,
    },
    {
        "version": "021_memory_cofire_prior",
        "fn": _migration_021_memory_cofire_prior,
    },
    {
        "version": "022_shadow_gate_fields",
        "fn": _migration_022_shadow_gate_fields,
    },
    {
        "version": "023_memory_directory_context_backfill",
        "fn": _migration_023_memory_directory_context_backfill,
    },
]


class _MigrationsMixin:
    """Schema migration and initialisation — mixed into StorageEngine."""

    # ------------------------------------------------------------------ schema

    def _run_migrations(self) -> None:
        """Apply pending schema migrations in order.

        Migration state is stored in a `schema_version` table. Each migration
        runs exactly once; the version table records which have been applied.

        Migrations only run in server mode (SurrealDB v3 HTTP). The embedded
        Python surrealdb package uses SurrealDB v2 which predates HNSW indexes
        and is only used for local development/testing.
        """
        if not self._db_url:
            return  # embedded mode: no migrations needed

        # Serialize concurrent daemon starts — flock for duration of migrations
        # Use XDG state dir for lock regardless of DB mode
        lock_dir = _paths.STATE_DIR
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / ".migration.lock"
        with open(lock_path, "w") as _lock_fh:
            fcntl.flock(_lock_fh, fcntl.LOCK_EX)
            try:
                self._run_migrations_locked()
            finally:
                fcntl.flock(_lock_fh, fcntl.LOCK_UN)

    def _run_migrations_locked(self) -> None:
        """Run migrations while holding the migration lock."""
        self._q("DEFINE TABLE IF NOT EXISTS schema_version SCHEMALESS;")

        for migration in _MIGRATIONS:
            ver = migration["version"]
            rows = self._q("SELECT version FROM schema_version WHERE version = $v", {"v": ver})
            if rows:
                continue  # already applied
            migration["fn"](self)
            self._q(
                "CREATE schema_version SET version = $v, applied_at = $ts",
                {"v": ver, "ts": self._now_iso()},
            )

    def _init_schema(self):
        # ---- Analyzers ----
        self._q("""
            DEFINE ANALYZER IF NOT EXISTS mem_analyzer
                TOKENIZERS blank, class
                FILTERS lowercase, snowball(english);
        """)
        self._q("""
            DEFINE ANALYZER IF NOT EXISTS profile_analyzer
                TOKENIZERS blank, class
                FILTERS lowercase, snowball(english);
        """)
        self._q("""
            DEFINE ANALYZER IF NOT EXISTS belief_analyzer
                TOKENIZERS blank, class
                FILTERS lowercase, snowball(english);
        """)

        # ---- Tables (SCHEMALESS) ----
        for table in (
            "episode",
            "entity",
            "relationship",
            "consolidation_log",
            "file_hash",
            "memory_cluster",
            "prospective_memory",
            "narrative_entry",
            "astrocyte_process",
            "memory_rule",
            "memory_archive",
            "memory_transition",
            "causal_dag_edge",
            "engram_slot",
            "checkpoint",
            "action_log",
            "user_profile",
            "derived_belief",
            "counter",
            "wiki_page",
            "wiki_crossref",
            "wiki_draft",
            "wiki_bookmark",
            "wiki_page_version",
        ):
            self._q(f"DEFINE TABLE IF NOT EXISTS {table} SCHEMALESS;")

        self._q("DEFINE TABLE IF NOT EXISTS memory SCHEMALESS;")

        # ---- Indexes ----

        # memory: vector index on embedding
        # Server mode (SurrealDB v3): HNSW; embedded mode (Python surrealdb v2): MTREE
        if self._db_url:
            self._q(f"""
                DEFINE INDEX IF NOT EXISTS memory_embedding_idx
                    ON memory FIELDS embedding
                    HNSW DIMENSION {self._embedding_dim} DIST COSINE TYPE F32 EFC 150 M 12;
            """)
        else:
            self._q(f"""
                DEFINE INDEX IF NOT EXISTS memory_embedding_idx
                    ON memory FIELDS embedding
                    MTREE DIMENSION {self._embedding_dim} DIST COSINE TYPE F32;
            """)
        # memory: SEARCH index on content (FTS) — server/daemon only.
        # Embedded SurrealDB (Python v2) has no FULLTEXT/BM25 support; issuing it
        # throws a parse error in _init_schema, which silently killed the embedded
        # nightly consolidation cycle (no heat decay/prune). Guard like the
        # HNSW/MTREE vector-index split above.
        if self._db_url:
            self._q("""
                DEFINE INDEX IF NOT EXISTS memory_content_idx
                    ON memory FIELDS content
                    FULLTEXT ANALYZER mem_analyzer BM25;
            """)
        # memory: vector index on implicit embedding
        if self._db_url:
            self._q(f"""
                DEFINE INDEX IF NOT EXISTS memory_implicit_idx
                    ON memory FIELDS implicit_embedding
                    HNSW DIMENSION {self._embedding_dim} DIST COSINE TYPE F32 EFC 150 M 12;
            """)
        else:
            self._q(f"""
                DEFINE INDEX IF NOT EXISTS memory_implicit_idx
                    ON memory FIELDS implicit_embedding
                    MTREE DIMENSION {self._embedding_dim} DIST COSINE TYPE F32;
            """)

        # file_hash: index on filepath (non-UNIQUE — surrealkv UNIQUE breaks WHERE)
        self._q("""
            DEFINE INDEX IF NOT EXISTS file_hash_filepath_idx
                ON file_hash FIELDS filepath;
        """)

        # memory_transition: index on (from_memory_id, to_memory_id)
        self._q("""
            DEFINE INDEX IF NOT EXISTS transition_unique_idx
                ON memory_transition FIELDS from_memory_id, to_memory_id;
        """)

        # user_profile: index on (entity_name, attribute_type, attribute_key, directory_context)
        self._q("""
            DEFINE INDEX IF NOT EXISTS profile_unique_idx
                ON user_profile
                FIELDS entity_name, attribute_type, attribute_key, directory_context;
        """)

        # FTS on user_profile — server/daemon only (embedded has no FULLTEXT).
        # One index per field (SurrealDB v3 FULLTEXT is single-field only).
        if self._db_url:
            for _field, _idx in [
                ("entity_name", "profile_entity_name_idx"),
                ("attribute_type", "profile_attribute_type_idx"),
                ("attribute_key", "profile_attribute_key_idx"),
                ("attribute_value", "profile_attribute_value_idx"),
            ]:
                self._q(f"""
                    DEFINE INDEX IF NOT EXISTS {_idx}
                        ON user_profile FIELDS {_field}
                        FULLTEXT ANALYZER profile_analyzer BM25;
                """)

        # FTS on derived_belief — server/daemon only (embedded has no FULLTEXT).
        if self._db_url:
            for _field, _idx in [
                ("subject", "belief_subject_idx"),
                ("belief_type", "belief_type_idx"),
                ("content", "belief_content_idx"),
            ]:
                self._q(f"""
                    DEFINE INDEX IF NOT EXISTS {_idx}
                        ON derived_belief FIELDS {_field}
                        FULLTEXT ANALYZER belief_analyzer BM25;
                """)

        # engram_slot: index on slot_index
        self._q("""
            DEFINE INDEX IF NOT EXISTS engram_slot_idx
                ON engram_slot FIELDS slot_index;
        """)

        self._init_wiki_indexes()

        # ---- Schema migration ----
        # Run AFTER all tables and indexes are defined so migrations can reference them
        self._run_migrations()

    def _init_wiki_indexes(self) -> None:
        """Define wiki-related indexes (extracted for fn_loc compliance)."""
        # wiki_page: FTS on content (BM25 keyword search) — server/daemon only.
        # Embedded SurrealDB (Python v2) has no FULLTEXT support (crashes _init_schema).
        if self._db_url:
            self._q("""
                DEFINE INDEX IF NOT EXISTS wiki_content_idx
                    ON wiki_page FIELDS content
                    FULLTEXT ANALYZER mem_analyzer BM25;
            """)
        # wiki_page: vector index on embedding (semantic search)
        if self._db_url:
            self._q(f"""
                DEFINE INDEX IF NOT EXISTS wiki_embedding_idx
                    ON wiki_page FIELDS embedding
                    HNSW DIMENSION {self._embedding_dim} DIST COSINE TYPE F32 EFC 150 M 12;
            """)
        else:
            self._q(f"""
                DEFINE INDEX IF NOT EXISTS wiki_embedding_idx
                    ON wiki_page FIELDS embedding
                    MTREE DIMENSION {self._embedding_dim} DIST COSINE TYPE F32;
            """)
        # wiki_page: slug lookup
        self._q("""
            DEFINE INDEX IF NOT EXISTS wiki_slug_idx
                ON wiki_page FIELDS slug;
        """)
        # wiki_crossref: from/to indexes
        self._q("""
            DEFINE INDEX IF NOT EXISTS wiki_crossref_from_idx
                ON wiki_crossref FIELDS from_slug;
        """)
        self._q("""
            DEFINE INDEX IF NOT EXISTS wiki_crossref_to_idx
                ON wiki_crossref FIELDS to_slug;
        """)
        # wiki_bookmark: UNIQUE index on slug + position index (v5.23.0)
        self._q("""
            DEFINE INDEX IF NOT EXISTS wiki_bookmark_slug_idx
                ON wiki_bookmark FIELDS slug UNIQUE;
        """)
        self._q("""
            DEFINE INDEX IF NOT EXISTS wiki_bookmark_position_idx
                ON wiki_bookmark FIELDS position;
        """)
