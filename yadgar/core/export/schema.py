"""Per-table column definitions for the DuckDB analytics export.

Each table descriptor lists:
  - columns: ordered list of (surreal_field, duckdb_col, duckdb_type)
  - Known fields are typed; everything else lands in extra_fields JSON.

General rules (from plan §Schema-mapping):
  1. SurrealDB id (record ID) → VARCHAR id + id_table + id_pk derived columns.
  2. Timestamps (ISO strings in SurrealDB) → TIMESTAMP. Parse at export; NULL on failure.
  3. Vectors → FLOAT[<dim>] (fixed-length array). dim injected at runtime.
  4. JSON-shaped fields → JSON type.
  5. Tag lists → JSON + exploded into memory_tag junction table.
  6. Record links → VARCHAR (preserve table:pk form).

§secret_gate: v5.10.2 secret-gate operates at write-time (SecretLeakBlocked raised
before any row is stored). There is no secret_flag column on memory rows. The
--include-secrets flag is reserved for future row-level tagging; today its WHERE
clause is a no-op. Field name kept here for documentation only.
"""

from __future__ import annotations

from typing import NamedTuple


class Column(NamedTuple):
    surreal_field: str
    duckdb_col: str
    duckdb_type: str


# Sentinel: embedding column placeholder — caller injects actual dim at DDL time.
EMBEDDING_PLACEHOLDER = "__EMBEDDING__"

# Tables excluded from export (plan §Tables-NOT-exported).
EXCLUDED_TABLES: frozenset[str] = frozenset(
    {
        "counter",
        "checkpoint",
        "engram_slot",
        "file_hash",
        "astrocyte_process",
        "memory_embedding_backup",
        "episode",  # raw episodes; not in plan export list
        "wiki_bookmark",  # operational UI state; not in plan export list
    }
)

# Ordered list of tables to export (plan §Tables-exported).
EXPORT_TABLE_NAMES: list[str] = [
    "memory",
    "wiki_page",
    "wiki_draft",
    "wiki_crossref",
    "action_log",
    "consolidation_log",
    "entity",
    "relationship",
    "causal_dag_edge",
    "memory_cluster",
    "memory_archive",
    "memory_transition",
    "narrative_entry",
    "derived_belief",
    "user_profile",
    "memory_rule",
    "prospective_memory",
    "schema_version",
    "memory_similarity_link",
]

# ---------------------------------------------------------------------------
# Per-table column specifications
# Each tuple: (surreal_field_name, duckdb_column_name, duckdb_type)
# ---------------------------------------------------------------------------

MEMORY_COLUMNS: list[Column] = [
    Column("id", "id", "VARCHAR"),
    Column("id", "id_table", "VARCHAR"),  # derived from record ID
    Column("id", "id_pk", "VARCHAR"),  # derived from record ID
    Column("content", "content", "VARCHAR"),
    Column("embedding", "embedding", EMBEDDING_PLACEHOLDER),
    Column("tags", "tags", "JSON"),
    Column("directory_context", "directory_context", "VARCHAR"),
    Column("created_at", "created_at", "TIMESTAMP"),
    Column("last_accessed", "last_accessed", "TIMESTAMP"),
    Column("heat", "heat", "DOUBLE"),
    Column("is_stale", "is_stale", "BOOLEAN"),
    Column("file_hash", "file_hash", "VARCHAR"),
    Column("surprise_score", "surprise_score", "DOUBLE"),
    Column("importance", "importance", "DOUBLE"),
    Column("emotional_valence", "emotional_valence", "DOUBLE"),
    Column("confidence", "confidence", "DOUBLE"),
    Column("access_count", "access_count", "BIGINT"),
    Column("useful_count", "useful_count", "BIGINT"),
    Column("embedding_model", "embedding_model", "VARCHAR"),
    Column("cluster_id", "cluster_id", "VARCHAR"),
    Column("store_type", "store_type", "VARCHAR"),
    Column("compression_level", "compression_level", "INTEGER"),
    Column("reconsolidation_count", "reconsolidation_count", "INTEGER"),
    Column("is_protected", "is_protected", "BOOLEAN"),
    Column("provenance_agent", "provenance_agent", "VARCHAR"),
    Column("branch", "branch", "VARCHAR"),
    Column("anchor_id", "anchor_id", "VARCHAR"),
    Column("tier", "tier", "VARCHAR"),
    Column("narrative_weight", "narrative_weight", "DOUBLE"),
    Column("plasticity", "plasticity", "DOUBLE"),
    Column("stability", "stability", "DOUBLE"),
]

WIKI_PAGE_COLUMNS: list[Column] = [
    Column("id", "id", "VARCHAR"),
    Column("id", "id_table", "VARCHAR"),
    Column("id", "id_pk", "VARCHAR"),
    Column("slug", "slug", "VARCHAR"),
    Column("title", "title", "VARCHAR"),
    Column("content", "content", "VARCHAR"),
    Column("tags", "tags", "JSON"),
    Column("approved", "approved", "BOOLEAN"),
    Column("created_at", "created_at", "TIMESTAMP"),
    Column("updated_at", "updated_at", "TIMESTAMP"),
    Column("branch", "branch", "VARCHAR"),
    Column("summary", "summary", "VARCHAR"),
]

WIKI_DRAFT_COLUMNS: list[Column] = [
    Column("id", "id", "VARCHAR"),
    Column("id", "id_table", "VARCHAR"),
    Column("id", "id_pk", "VARCHAR"),
    Column("slug", "slug", "VARCHAR"),
    Column("content", "content", "VARCHAR"),
    Column("tags", "tags", "JSON"),
    Column("created_at", "created_at", "TIMESTAMP"),
    Column("updated_at", "updated_at", "TIMESTAMP"),
]

WIKI_CROSSREF_COLUMNS: list[Column] = [
    Column("id", "id", "VARCHAR"),
    Column("id", "id_table", "VARCHAR"),
    Column("id", "id_pk", "VARCHAR"),
    Column("from_slug", "from_slug", "VARCHAR"),
    Column("to_slug", "to_slug", "VARCHAR"),
    Column("created_at", "created_at", "TIMESTAMP"),
]

ACTION_LOG_COLUMNS: list[Column] = [
    Column("id", "id", "VARCHAR"),
    Column("id", "id_table", "VARCHAR"),
    Column("id", "id_pk", "VARCHAR"),
    Column("tool", "tool", "VARCHAR"),
    Column("ts", "ts", "TIMESTAMP"),
    Column("processed", "processed", "BOOLEAN"),
    Column("memory_id", "memory_id", "VARCHAR"),
    Column("query", "query", "VARCHAR"),
    Column("result_count", "result_count", "INTEGER"),
    Column("latency_ms", "latency_ms", "DOUBLE"),
    Column("directory_context", "directory_context", "VARCHAR"),
]

CONSOLIDATION_LOG_COLUMNS: list[Column] = [
    Column("id", "id", "VARCHAR"),
    Column("id", "id_table", "VARCHAR"),
    Column("id", "id_pk", "VARCHAR"),
    Column("timestamp", "timestamp", "TIMESTAMP"),
    Column("memories_added", "memories_added", "INTEGER"),
    Column("memories_updated", "memories_updated", "INTEGER"),
    Column("memories_archived", "memories_archived", "INTEGER"),
    Column("memories_deleted", "memories_deleted", "INTEGER"),
    Column("duration_ms", "duration_ms", "INTEGER"),
]

ENTITY_COLUMNS: list[Column] = [
    Column("id", "id", "VARCHAR"),
    Column("id", "id_table", "VARCHAR"),
    Column("id", "id_pk", "VARCHAR"),
    Column("name", "name", "VARCHAR"),
    Column("entity_type", "entity_type", "VARCHAR"),
    Column("created_at", "created_at", "TIMESTAMP"),
    Column("source_memory_id", "source_memory_id", "VARCHAR"),
    Column("tags", "tags", "JSON"),
]

RELATIONSHIP_COLUMNS: list[Column] = [
    Column("id", "id", "VARCHAR"),
    Column("id", "id_table", "VARCHAR"),
    Column("id", "id_pk", "VARCHAR"),
    Column("from_entity", "from_entity", "VARCHAR"),
    Column("to_entity", "to_entity", "VARCHAR"),
    Column("rel_type", "rel_type", "VARCHAR"),
    Column("valid_from", "valid_from", "TIMESTAMP"),
    Column("valid_until", "valid_until", "TIMESTAMP"),
    Column("source_memory_id", "source_memory_id", "VARCHAR"),
    Column("weight", "weight", "DOUBLE"),
]

CAUSAL_DAG_EDGE_COLUMNS: list[Column] = [
    Column("id", "id", "VARCHAR"),
    Column("id", "id_table", "VARCHAR"),
    Column("id", "id_pk", "VARCHAR"),
    Column("source", "source", "VARCHAR"),
    Column("target", "target", "VARCHAR"),
    Column("weight", "weight", "DOUBLE"),
    Column("created_at", "created_at", "TIMESTAMP"),
    Column("algorithm", "algorithm", "VARCHAR"),
]

MEMORY_CLUSTER_COLUMNS: list[Column] = [
    Column("id", "id", "VARCHAR"),
    Column("id", "id_table", "VARCHAR"),
    Column("id", "id_pk", "VARCHAR"),
    Column("cluster_id", "cluster_id", "INTEGER"),
    Column("centroid_label", "centroid_label", "VARCHAR"),
    Column("member_count", "member_count", "INTEGER"),
    Column("created_at", "created_at", "TIMESTAMP"),
    Column("tags", "tags", "JSON"),
]

MEMORY_ARCHIVE_COLUMNS: list[Column] = [
    Column("id", "id", "VARCHAR"),
    Column("id", "id_table", "VARCHAR"),
    Column("id", "id_pk", "VARCHAR"),
    Column("content", "content", "VARCHAR"),
    Column("archived_at", "archived_at", "TIMESTAMP"),
    Column("original_memory_id", "original_memory_id", "VARCHAR"),
    Column("tags", "tags", "JSON"),
    Column("heat", "heat", "DOUBLE"),
]

MEMORY_TRANSITION_COLUMNS: list[Column] = [
    Column("id", "id", "VARCHAR"),
    Column("id", "id_table", "VARCHAR"),
    Column("id", "id_pk", "VARCHAR"),
    Column("memory_id", "memory_id", "VARCHAR"),
    Column("from_tier", "from_tier", "VARCHAR"),
    Column("to_tier", "to_tier", "VARCHAR"),
    Column("transitioned_at", "transitioned_at", "TIMESTAMP"),
    Column("reason", "reason", "VARCHAR"),
]

NARRATIVE_ENTRY_COLUMNS: list[Column] = [
    Column("id", "id", "VARCHAR"),
    Column("id", "id_table", "VARCHAR"),
    Column("id", "id_pk", "VARCHAR"),
    Column("content", "content", "VARCHAR"),
    Column("created_at", "created_at", "TIMESTAMP"),
    Column("period", "period", "VARCHAR"),
    Column("tags", "tags", "JSON"),
    Column("memory_ids", "memory_ids", "JSON"),
]

DERIVED_BELIEF_COLUMNS: list[Column] = [
    Column("id", "id", "VARCHAR"),
    Column("id", "id_table", "VARCHAR"),
    Column("id", "id_pk", "VARCHAR"),
    Column("belief", "belief", "VARCHAR"),
    Column("confidence", "confidence", "DOUBLE"),
    Column("created_at", "created_at", "TIMESTAMP"),
    Column("tags", "tags", "JSON"),
    Column("source_memory_ids", "source_memory_ids", "JSON"),
]

USER_PROFILE_COLUMNS: list[Column] = [
    Column("id", "id", "VARCHAR"),
    Column("id", "id_table", "VARCHAR"),
    Column("id", "id_pk", "VARCHAR"),
    Column("attribute", "attribute", "VARCHAR"),
    Column("value", "value", "VARCHAR"),
    Column("updated_at", "updated_at", "TIMESTAMP"),
]

MEMORY_RULE_COLUMNS: list[Column] = [
    Column("id", "id", "VARCHAR"),
    Column("id", "id_table", "VARCHAR"),
    Column("id", "id_pk", "VARCHAR"),
    Column("rule_text", "rule_text", "VARCHAR"),
    Column("is_active", "is_active", "BOOLEAN"),
    Column("created_at", "created_at", "TIMESTAMP"),
    Column("tags", "tags", "JSON"),
]

PROSPECTIVE_MEMORY_COLUMNS: list[Column] = [
    Column("id", "id", "VARCHAR"),
    Column("id", "id_table", "VARCHAR"),
    Column("id", "id_pk", "VARCHAR"),
    Column("content", "content", "VARCHAR"),
    Column("trigger_condition", "trigger_condition", "VARCHAR"),
    Column("is_active", "is_active", "BOOLEAN"),
    Column("triggered_count", "triggered_count", "INTEGER"),
    Column("created_at", "created_at", "TIMESTAMP"),
    Column("last_triggered", "last_triggered", "TIMESTAMP"),
]

SCHEMA_VERSION_COLUMNS: list[Column] = [
    Column("id", "id", "VARCHAR"),
    Column("id", "id_table", "VARCHAR"),
    Column("id", "id_pk", "VARCHAR"),
    Column("version", "version", "VARCHAR"),
    Column("applied_at", "applied_at", "TIMESTAMP"),
]

MEMORY_SIMILARITY_LINK_COLUMNS: list[Column] = [
    Column("id", "id", "VARCHAR"),
    Column("id", "id_table", "VARCHAR"),
    Column("id", "id_pk", "VARCHAR"),
    Column("source_memory_id", "source_memory_id", "VARCHAR"),
    Column("target_memory_id", "target_memory_id", "VARCHAR"),
    Column("similarity", "similarity", "DOUBLE"),
    Column("valid_from", "valid_from", "TIMESTAMP"),
    Column("citation_source_memory_id", "citation_source_memory_id", "VARCHAR"),
]

# Master registry: table_name → column list
TABLE_COLUMNS: dict[str, list[Column]] = {
    "memory": MEMORY_COLUMNS,
    "wiki_page": WIKI_PAGE_COLUMNS,
    "wiki_draft": WIKI_DRAFT_COLUMNS,
    "wiki_crossref": WIKI_CROSSREF_COLUMNS,
    "action_log": ACTION_LOG_COLUMNS,
    "consolidation_log": CONSOLIDATION_LOG_COLUMNS,
    "entity": ENTITY_COLUMNS,
    "relationship": RELATIONSHIP_COLUMNS,
    "causal_dag_edge": CAUSAL_DAG_EDGE_COLUMNS,
    "memory_cluster": MEMORY_CLUSTER_COLUMNS,
    "memory_archive": MEMORY_ARCHIVE_COLUMNS,
    "memory_transition": MEMORY_TRANSITION_COLUMNS,
    "narrative_entry": NARRATIVE_ENTRY_COLUMNS,
    "derived_belief": DERIVED_BELIEF_COLUMNS,
    "user_profile": USER_PROFILE_COLUMNS,
    "memory_rule": MEMORY_RULE_COLUMNS,
    "prospective_memory": PROSPECTIVE_MEMORY_COLUMNS,
    "schema_version": SCHEMA_VERSION_COLUMNS,
    "memory_similarity_link": MEMORY_SIMILARITY_LINK_COLUMNS,
}


def build_create_table_ddl(table_name: str, columns: list[Column], embedding_dim: int) -> str:
    """Build CREATE TABLE IF NOT EXISTS DDL for the given table."""
    col_defs: list[str] = []
    seen_duckdb_cols: set[str] = set()

    for col in columns:
        duck_col = col.duckdb_col
        if duck_col in seen_duckdb_cols:
            continue
        seen_duckdb_cols.add(duck_col)
        col_type = col.duckdb_type
        if col_type == EMBEDDING_PLACEHOLDER:
            col_type = f"FLOAT[{embedding_dim}]"
        col_defs.append(f"    {duck_col} {col_type}")

    col_defs.append("    extra_fields JSON")
    cols_sql = ",\n".join(col_defs)
    return f"CREATE TABLE IF NOT EXISTS {table_name} (\n{cols_sql}\n);"


def build_junction_table_ddl() -> str:
    """Build memory_tag junction table DDL."""
    return "CREATE TABLE IF NOT EXISTS memory_tag (\n    memory_id VARCHAR,\n    tag VARCHAR\n);"
