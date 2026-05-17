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


_MIGRATIONS: list[dict] = [
    {"version": "001_hnsw_indexes", "fn": _migration_001_hnsw_indexes},
    {"version": "002_relationship_indexes", "fn": _migration_002_relationship_indexes},
    {
        "version": "003_memory_similarity_link_table",
        "fn": _migration_003_memory_similarity_link_table,
    },
    {"version": "004_branch_field", "fn": _migration_004_branch_field},
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

        # ---- Schema migration ----
        # Run AFTER all tables and indexes are defined so migrations can reference them
        self._run_migrations()
