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
from pathlib import Path
from typing import TYPE_CHECKING

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
        # Use ~/.yadgar as lock directory regardless of DB mode
        lock_dir = Path("~/.yadgar").expanduser()
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
        # memory: SEARCH index on content (FTS)
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

        # FTS on user_profile — one index per field (SurrealDB v3 FULLTEXT is single-field only)
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

        # FTS on derived_belief — one index per field
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
        # wiki_page: FTS on content (BM25 keyword search)
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
