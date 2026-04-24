import atexit
import fcntl
import json
import logging
import re
import shutil
import struct
from datetime import UTC, datetime
from pathlib import Path

_log = logging.getLogger(__name__)

_FTS_STOP_WORDS = frozenset(
    {
        # Standard English stop words
        "a",
        "an",
        "the",
        "is",
        "it",
        "in",
        "on",
        "at",
        "to",
        "of",
        "for",
        "and",
        "or",
        "but",
        "not",
        "with",
        "by",
        "from",
        "as",
        "be",
        "was",
        "were",
        "been",
        "are",
        "am",
        "do",
        "did",
        "does",
        "has",
        "had",
        "have",
        "will",
        "would",
        "could",
        "should",
        "may",
        "can",
        "this",
        "that",
        "these",
        "those",
        "what",
        "which",
        "who",
        "how",
        "when",
        "where",
        "why",
        "if",
        "then",
        "so",
        "no",
        "yes",
        "all",
        "any",
        "some",
        "my",
        "your",
        "its",
        "our",
        "their",
        "we",
        "he",
        "she",
        "they",
        "me",
        "him",
        "her",
        "us",
        "them",
        # Coding/conversation domain stop words
        "use",
        "using",
        "used",
        "like",
        "just",
        "get",
        "got",
        "set",
        "make",
        "made",
        "let",
        "try",
        "need",
        "want",
        "know",
        "think",
        "code",
        "file",
        "thing",
        "stuff",
    }
)

_CAMEL_CASE_RE = re.compile(r"([a-z])([A-Z])")

_enrichment_pipeline = None


def _get_enrichment_pipeline(settings, embeddings_engine=None):
    global _enrichment_pipeline
    if _enrichment_pipeline is None:
        from yadgar.enrichment import EnrichmentPipeline

        _enrichment_pipeline = EnrichmentPipeline(settings, embeddings_engine)
    return _enrichment_pipeline


# Embedding fields that hold float arrays in SurrealDB and must be converted to bytes on read
_EMBEDDING_FIELDS = ("embedding", "centroid_embedding", "hdc_vector", "implicit_embedding")


class StorageEngine:
    def __init__(self, db_path: str, embedding_dim: int = 384):
        from surrealdb import Surreal

        resolved = Path(db_path).expanduser()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        self._embedding_dim = embedding_dim
        self._conn = None  # some callers access storage._conn.execute() directly
        self._resolved_path = resolved

        # surrealkv embedded mode does not support concurrent connections.
        # Use an exclusive file lock to ensure only one process owns the DB.
        self._lock_path = resolved.parent / "yadgar.lock"
        self._lock_file = open(self._lock_path, "w")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_file.write(str(__import__("os").getpid()))
            self._lock_file.flush()
        except OSError:
            self._lock_file.close()
            raise RuntimeError(
                f"Another yadgar process holds the DB lock ({self._lock_path}). "
                "surrealkv does not support concurrent access. "
                "Close other Claude sessions or kill stale yadgar processes."
            ) from None

        # Backup DB before opening — defense against crash corruption.
        # Keeps one rolling backup so we can restore if the clog is damaged.
        self._backup_path = resolved.parent / "surreal_db.bak"
        if resolved.exists():
            try:
                if self._backup_path.exists():
                    shutil.rmtree(self._backup_path)
                shutil.copytree(resolved, self._backup_path)
                _log.debug("DB backup created at %s", self._backup_path)
            except Exception as e:
                _log.warning("DB backup failed (non-fatal): %s", e)

        self._db = Surreal(f"surrealkv://{resolved}")
        self._db.use("yadgar", "main")
        self._init_schema()

        # Health check: verify we can read field data, not just count records.
        # Detects corruption from prior crashes (records exist but fields are null).
        self._verify_health()

        # Register atexit handler for clean shutdown even if close() isn't called
        atexit.register(self.close)

    def _verify_health(self):
        """Post-startup health check — detect corrupted DB state."""
        try:
            count_rows = self._q("SELECT count() AS c FROM memory GROUP ALL")
            total = int(count_rows[0]["c"]) if count_rows else 0
            if total == 0:
                return  # Empty DB, nothing to check

            heat_rows = self._q("SELECT math::mean(heat) AS avg FROM memory GROUP ALL")
            avg_heat = (
                float(heat_rows[0]["avg"])
                if heat_rows and heat_rows[0].get("avg") is not None
                else 0.0
            )

            if total > 0 and avg_heat == 0.0:
                _log.warning(
                    "DB health check: %d memories but avg_heat=0.0 — possible corruption. "
                    "Attempting restore from backup.",
                    total,
                )
                if self._backup_path.exists():
                    self._restore_from_backup()
                else:
                    _log.error("No backup available to restore from.")
        except Exception as e:
            _log.warning("DB health check failed: %s", e)

    def _restore_from_backup(self):
        """Restore DB from the rolling backup after detecting corruption."""
        try:
            self._db.close()
        except Exception:
            pass

        from surrealdb import Surreal

        resolved = self._resolved_path
        _log.warning("Restoring DB from backup %s", self._backup_path)
        try:
            shutil.rmtree(resolved)
            shutil.copytree(self._backup_path, resolved)
            self._db = Surreal(f"surrealkv://{resolved}")
            self._db.use("yadgar", "main")
            self._init_schema()
            _log.warning("DB restored from backup successfully.")
        except Exception as e:
            _log.error("DB restore failed: %s", e)

    # ------------------------------------------------------------------ helpers

    def _bytes_to_floats(self, data: bytes) -> list[float]:
        n = len(data) // 4
        return list(struct.unpack(f"<{n}f", data))

    def _floats_to_bytes(self, floats: list[float]) -> bytes:
        return struct.pack(f"<{len(floats)}f", *floats)

    def _extract_id(self, record_id) -> int:
        if record_id is None:
            return None
        if hasattr(record_id, "id") and hasattr(record_id, "table_name"):
            return int(record_id.id)
        if isinstance(record_id, str) and ":" in record_id:
            return int(record_id.split(":")[1])
        return int(record_id)

    def _next_id(self, table: str) -> int:
        result = self._db.query(f"UPSERT counter:{table} SET val = (val ?? 0) + 1")
        if result and isinstance(result, list) and result[0]:
            row = result[0]
            if isinstance(row, list):
                row = row[0]
            return int(row.get("val", 1))
        return 1

    def _row_to_dict(self, record: dict | None) -> dict | None:
        if record is None:
            return None
        d = dict(record)
        # Convert RecordID id to int
        if "id" in d:
            d["id"] = self._extract_id(d["id"])
        # Convert embedding float arrays -> bytes
        for emb_field in _EMBEDDING_FIELDS:
            if emb_field in d and isinstance(d[emb_field], list):
                d[emb_field] = self._floats_to_bytes(d[emb_field])
        # JSON fields — SurrealDB stores them as native lists; ensure they are lists
        for json_field in (
            "tags",
            "key_decisions",
            "key_events",
            "memory_ids",
            "entity_ids",
            "evidence_memory_ids",
            "files_being_edited",
            "open_questions",
            "next_steps",
            "active_errors",
        ):
            if json_field in d and isinstance(d[json_field], str):
                d[json_field] = json.loads(d[json_field])
        # Booleans
        for bool_field in (
            "archived",
            "is_stale",
            "is_prospective",
            "is_causal",
            "is_active",
            "compressed",
            "is_protected",
            "is_validated",
        ):
            if bool_field in d:
                d[bool_field] = bool(d[bool_field])
        return d

    def _rows_to_dicts(self, rows: list[dict]) -> list[dict]:
        return [self._row_to_dict(r) for r in rows if r is not None]

    def _now_iso(self) -> str:
        return datetime.now(UTC).isoformat()

    def _q(self, surql: str, params: dict | None = None) -> list:
        """Run a parameterised query and return rows as a flat list of dicts.

        surrealdb Python client 1.0.x returns results in different shapes:
        - Single statement: flat list of dicts [row1, row2, ...]
        - Multi-statement: list of result sets [[row1, ...], [row2, ...]]
        - CREATE/UPDATE: a single dict (not wrapped in a list)
        We normalise all cases to a flat list of dicts.
        """
        result = self._db.query(surql, params or {})
        if result is None:
            return []
        if isinstance(result, dict):
            # Single record (e.g. from CREATE returning one object)
            return [result]
        if isinstance(result, list):
            if len(result) == 0:
                return []
            first = result[0]
            if isinstance(first, list):
                # Nested result sets — return the first set
                return first
            if isinstance(first, dict):
                # Flat list of row dicts — return as-is
                return result
            if first is None:
                return []
            return result
        return []

    def _enrich_content_for_fts(self, content: str) -> str:
        """Enrich content with split identifier tokens for better FTS matching."""
        tokens = content.split()
        extra_tokens = []
        for token in tokens:
            split = _CAMEL_CASE_RE.sub(r"\1 \2", token)
            split = split.replace("_", " ")
            sub_tokens = split.split()
            if len(sub_tokens) > 1:
                extra_tokens.extend(t for t in sub_tokens if t != token)
        if extra_tokens:
            return content + " " + " ".join(extra_tokens)
        return content

    def _preprocess_fts_query(self, query: str) -> str:
        """Preprocess query for SurrealDB full-text search.

        SurrealDB's analyzer handles tokenization, lowercasing, and stemming.
        We just strip punctuation, split identifiers, and remove stop words.
        No FTS5 OR syntax — return plain space-separated terms.
        """
        parts = []
        raw_tokens = query.split()

        for token in raw_tokens:
            token = token.strip('?!,;:()[]{}"\'"')  # noqa: B005
            if not token:
                continue

            split_term = _CAMEL_CASE_RE.sub(r"\1 \2", token)
            split_term = split_term.replace("_", " ").replace(".", " ")
            sub_tokens = split_term.split()

            filtered = [t for t in sub_tokens if t.lower() not in _FTS_STOP_WORDS and len(t) >= 2]

            parts.extend(filtered)

        return " ".join(parts) if parts else query

    # ------------------------------------------------------------------ schema

    def _init_schema(self):
        db = self._db

        # ---- Analyzers ----
        db.query("""
            DEFINE ANALYZER IF NOT EXISTS mem_analyzer
                TOKENIZERS blank, class
                FILTERS lowercase, snowball(english);
        """)
        db.query("""
            DEFINE ANALYZER IF NOT EXISTS profile_analyzer
                TOKENIZERS blank, class
                FILTERS lowercase, snowball(english);
        """)
        db.query("""
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
        ):
            db.query(f"DEFINE TABLE IF NOT EXISTS {table} SCHEMALESS;")

        db.query("DEFINE TABLE IF NOT EXISTS memory SCHEMALESS;")

        # ---- Indexes ----

        # memory: MTREE vector index on embedding
        db.query(f"""
            DEFINE INDEX IF NOT EXISTS memory_embedding_idx
                ON memory FIELDS embedding
                MTREE DIMENSION {self._embedding_dim} DIST COSINE;
        """)
        # memory: SEARCH index on content (FTS)
        db.query("""
            DEFINE INDEX IF NOT EXISTS memory_content_idx
                ON memory FIELDS content
                SEARCH ANALYZER mem_analyzer BM25;
        """)
        # memory: MTREE for implicit embedding
        db.query(f"""
            DEFINE INDEX IF NOT EXISTS memory_implicit_idx
                ON memory FIELDS implicit_embedding
                MTREE DIMENSION {self._embedding_dim} DIST COSINE;
        """)

        # file_hash: index on filepath (non-UNIQUE — surrealkv UNIQUE breaks WHERE)
        db.query("""
            DEFINE INDEX IF NOT EXISTS file_hash_filepath_idx
                ON file_hash FIELDS filepath;
        """)

        # memory_transition: index on (from_memory_id, to_memory_id)
        db.query("""
            DEFINE INDEX IF NOT EXISTS transition_unique_idx
                ON memory_transition FIELDS from_memory_id, to_memory_id;
        """)

        # user_profile: index on (entity_name, attribute_type, attribute_key, directory_context)
        db.query("""
            DEFINE INDEX IF NOT EXISTS profile_unique_idx
                ON user_profile
                FIELDS entity_name, attribute_type, attribute_key, directory_context;
        """)

        # FTS on user_profile
        db.query("""
            DEFINE INDEX IF NOT EXISTS profile_fts_idx
                ON user_profile
                FIELDS entity_name, attribute_type, attribute_key, attribute_value
                SEARCH ANALYZER profile_analyzer BM25;
        """)

        # FTS on derived_belief
        db.query("""
            DEFINE INDEX IF NOT EXISTS belief_fts_idx
                ON derived_belief
                FIELDS subject, belief_type, content
                SEARCH ANALYZER belief_analyzer BM25;
        """)

        # engram_slot: index on slot_index
        db.query("""
            DEFINE INDEX IF NOT EXISTS engram_slot_idx
                ON engram_slot FIELDS slot_index;
        """)

        # wiki_page: FTS on content (BM25 keyword search)
        db.query("""
            DEFINE INDEX IF NOT EXISTS wiki_content_idx
                ON wiki_page FIELDS content
                SEARCH ANALYZER mem_analyzer BM25;
        """)
        # wiki_page: MTREE vector index on embedding (semantic search)
        db.query(f"""
            DEFINE INDEX IF NOT EXISTS wiki_embedding_idx
                ON wiki_page FIELDS embedding
                MTREE DIMENSION {self._embedding_dim} DIST COSINE;
        """)
        # wiki_page: slug lookup
        db.query("""
            DEFINE INDEX IF NOT EXISTS wiki_slug_idx
                ON wiki_page FIELDS slug;
        """)
        # wiki_crossref: from/to indexes
        db.query("""
            DEFINE INDEX IF NOT EXISTS wiki_crossref_from_idx
                ON wiki_crossref FIELDS from_slug;
        """)
        db.query("""
            DEFINE INDEX IF NOT EXISTS wiki_crossref_to_idx
                ON wiki_crossref FIELDS to_slug;
        """)

    # ------------------------------------------------------------------ Episodes

    def insert_episode(self, episode: dict) -> int:
        eid = self._next_id("episode")
        self._db.query(
            "CREATE type::thing('episode', $id) SET "
            "session_id = $session_id, timestamp = $timestamp, "
            "directory = $directory, raw_content = $raw_content, "
            "overlap_start = $overlap_start, overlap_end = $overlap_end",
            {
                "id": eid,
                "session_id": episode["session_id"],
                "timestamp": episode.get("timestamp", self._now_iso()),
                "directory": episode["directory"],
                "raw_content": episode["raw_content"],
                "overlap_start": episode.get("overlap_start"),
                "overlap_end": episode.get("overlap_end"),
            },
        )
        return eid

    def get_session_episodes(self, session_id: str) -> list[dict]:
        rows = self._q(
            "SELECT * FROM episode WHERE session_id = $sid ORDER BY id",
            {"sid": session_id},
        )
        return self._rows_to_dicts(rows)

    def get_episodes_since(self, episode_id: int) -> list[dict]:
        rows = self._q(
            "SELECT * FROM episode WHERE id > $eid ORDER BY id",
            {"eid": episode_id},
        )
        return self._rows_to_dicts(rows)

    def get_max_episode_id(self) -> int:
        row = self._q("SELECT val FROM counter:episode")
        if row:
            return int(row[0].get("val", 0))
        return 0

    # ------------------------------------------------------------------ Memories

    def insert_memory(self, memory: dict, embeddings_engine=None, settings=None) -> int:
        now = self._now_iso()
        mid = self._next_id("memory")
        embedding = memory.get("embedding")
        emb_floats = self._bytes_to_floats(embedding) if embedding else None

        self._db.query(
            "CREATE type::thing('memory', $id) SET "
            "content = $content, embedding = $embedding, tags = $tags, "
            "source_episode_id = $source_episode_id, "
            "directory_context = $directory_context, "
            "created_at = $created_at, last_accessed = $last_accessed, "
            "heat = $heat, is_stale = $is_stale, file_hash = $file_hash, "
            "embedding_model = $embedding_model",
            {
                "id": mid,
                "content": memory["content"],
                "embedding": emb_floats,
                "tags": memory.get("tags", []),
                "source_episode_id": memory.get("source_episode_id"),
                "directory_context": memory["directory_context"],
                "created_at": memory.get("created_at", now),
                "last_accessed": memory.get("last_accessed", now),
                "heat": memory.get("heat", 1.0),
                "is_stale": bool(memory.get("is_stale", False)),
                "file_hash": memory.get("file_hash"),
                "embedding_model": memory.get("embedding_model"),
            },
        )

        # Enrichment pipeline
        enrichment_data = {}
        if (
            settings
            and getattr(settings, "INDEX_ENRICHMENT_ENABLED", False)
            and len(memory["content"]) >= getattr(settings, "ENRICHMENT_MIN_CONTENT_LENGTH", 20)
            and embeddings_engine is not None
            and embedding is not None
        ):
            try:
                pipeline = _get_enrichment_pipeline(settings, embeddings_engine)
                result = pipeline.enrich(memory["content"], embedding, settings)
                enrichment_data = {
                    "enrichment_concepts": result.concepts if result.concepts else None,
                    "enrichment_comet": result.comet_inferences
                    if result.comet_inferences
                    else None,
                    "enrichment_queries": result.queries if result.queries else None,
                    "enrichment_logic": result.logic_expansions
                    if result.logic_expansions
                    else None,
                    "enriched_content": result.enriched_content or None,
                    "enrichment_model_versions": result.model_versions
                    if result.model_versions
                    else None,
                }
                if any(v is not None for v in enrichment_data.values()):
                    set_parts = []
                    params = {"id": mid}
                    for col, val in enrichment_data.items():
                        if val is not None:
                            set_parts.append(f"{col} = ${col}")
                            params[col] = val
                    if set_parts:
                        self._db.query(
                            f"UPDATE type::thing('memory', $id) SET {', '.join(set_parts)}",
                            params,
                        )
                        if (
                            enrichment_data.get("enriched_content")
                            and embeddings_engine is not None
                        ):
                            new_embedding = embeddings_engine.encode_document_enriched(
                                memory["content"], enrichment_data["enriched_content"]
                            )
                            if new_embedding is not None:
                                new_floats = self._bytes_to_floats(new_embedding)
                                self._db.query(
                                    "UPDATE type::thing('memory', $id) SET embedding = $emb",
                                    {"id": mid, "emb": new_floats},
                                )
            except Exception as e:
                import logging

                logging.getLogger(__name__).warning("Enrichment failed: %s", e)

        # insert_vector is a no-op for the separate table, but keep for API compat
        if embedding is not None:
            self.insert_vector(mid, embedding)

        return mid

    def get_memory(self, memory_id: int) -> dict | None:
        # Use direct record ID syntax — more reliable than type::thing() in surrealkv
        mid = int(memory_id)  # sanitize
        rows = self._q(f"SELECT * FROM memory:{mid}")
        return self._row_to_dict(rows[0]) if rows else None

    def get_memories_by_heat(self, min_heat: float, limit: int = 100) -> list[dict]:
        rows = self._q(
            "SELECT * FROM memory WHERE heat >= $min ORDER BY heat DESC LIMIT $lim",
            {"min": min_heat, "lim": limit},
        )
        return self._rows_to_dicts(rows)

    def update_memory_heat(self, memory_id: int, new_heat: float):
        self._db.query(
            "UPDATE type::thing('memory', $id) SET heat = $heat",
            {"id": memory_id, "heat": new_heat},
        )

    def update_memory_staleness(self, memory_id: int, is_stale: bool):
        self._db.query(
            "UPDATE type::thing('memory', $id) SET is_stale = $stale",
            {"id": memory_id, "stale": is_stale},
        )

    def delete_memory(self, memory_id: int):
        # Delete FK dependents first
        self._db.query(
            "DELETE FROM memory_archive WHERE original_memory_id = $mid",
            {"mid": memory_id},
        )
        self._db.query(
            "DELETE FROM memory_transition WHERE from_memory_id = $mid OR to_memory_id = $mid",
            {"mid": memory_id},
        )
        # Clear vector fields (no separate table)
        try:
            self.delete_vector(memory_id)
        except Exception:
            pass
        try:
            self._db.query(
                "UPDATE type::thing('memory', $id) SET implicit_embedding = NONE",
                {"id": memory_id},
            )
        except Exception:
            pass
        self._db.query(
            "DELETE type::thing('memory', $id)",
            {"id": memory_id},
        )

    def get_memories_for_directory(self, directory: str, min_heat: float = 0.1) -> list[dict]:
        rows = self._q(
            "SELECT * FROM memory WHERE directory_context = $dir AND heat >= $min "
            "ORDER BY heat DESC",
            {"dir": directory, "min": min_heat},
        )
        return self._rows_to_dicts(rows)

    def get_stale_memories(self) -> list[dict]:
        rows = self._q("SELECT * FROM memory WHERE is_stale = true")
        return self._rows_to_dicts(rows)

    def get_memories_by_file_hash(self, file_hash: str) -> list[dict]:
        rows = self._q(
            "SELECT * FROM memory WHERE file_hash = $fh",
            {"fh": file_hash},
        )
        return self._rows_to_dicts(rows)

    def get_all_memories_for_decay(self) -> list[dict]:
        rows = self._q(
            "SELECT * FROM memory WHERE heat > 0 AND (is_protected = false OR is_protected = NONE)"
        )
        return self._rows_to_dicts(rows)

    def get_all_memories_with_embeddings(self) -> list[dict]:
        rows = self._q("SELECT * FROM memory WHERE embedding IS NOT NONE AND heat > 0")
        return self._rows_to_dicts(rows)

    def get_memories_without_embeddings(self) -> list[dict]:
        rows = self._q("SELECT * FROM memory WHERE embedding IS NONE AND heat > 0")
        return self._rows_to_dicts(rows)

    def search_memories_fts(self, query: str, min_heat: float = 0.1, limit: int = 5) -> list[dict]:
        fts_query = self._preprocess_fts_query(query)
        rows = self._q(
            "SELECT * FROM memory WHERE content @@ $q AND heat >= $min "
            "ORDER BY heat DESC LIMIT $lim",
            {"q": fts_query, "min": min_heat, "lim": limit},
        )
        return self._rows_to_dicts(rows)

    def search_memories_fts_scored(
        self, query: str, min_heat: float = 0.1, limit: int = 50
    ) -> list[tuple[int, float]]:
        """FTS search returning (memory_id, bm25_score) tuples. Higher = better."""
        fts_query = self._preprocess_fts_query(query)
        rows = self._q(
            "SELECT id, search::score(1) AS score FROM memory "
            "WHERE content @1@ $q AND heat >= $min "
            "ORDER BY score DESC LIMIT $lim",
            {"q": fts_query, "min": min_heat, "lim": limit},
        )
        results = []
        for row in rows:
            mid = self._extract_id(row.get("id"))
            score = float(row.get("score", 0.0))
            results.append((mid, score))
        return results

    def search_memories_by_content_date(
        self,
        date_hints: list[str],
        month_hints: list[str],
        session_hints: list[str],
        min_heat: float = 0.0,
        limit: int = 50,
    ) -> list[dict]:
        """Search memory content for temporal references using FTS."""
        terms = []
        for hint in date_hints:
            terms.append('"' + hint + '"')
        for hint in month_hints:
            terms.append(hint)
        for hint in session_hints:
            terms.append(hint)
        if not terms:
            return []
        fts_query = " OR ".join(terms)
        rows = self._q(
            "SELECT * FROM memory WHERE content @@ $q AND heat >= $min "
            "ORDER BY heat DESC LIMIT $lim",
            {"q": fts_query, "min": min_heat, "lim": limit},
        )
        return self._rows_to_dicts(rows)

    def search_memories_by_timestamp_range(
        self,
        start_date: str,
        end_date: str,
        min_heat: float = 0.0,
        limit: int = 50,
    ) -> list[dict]:
        rows = self._q(
            "SELECT * FROM memory WHERE created_at >= $start AND created_at <= $end "
            "AND heat >= $min ORDER BY created_at DESC LIMIT $lim",
            {"start": start_date, "end": end_date, "min": min_heat, "lim": limit},
        )
        return self._rows_to_dicts(rows)

    def search_memories_by_month(
        self,
        month_hints: list[str],
        min_heat: float = 0.0,
        limit: int = 200,
    ) -> list[int]:
        """Find memory IDs whose created_at falls in the given month(s)."""
        month_map = {
            "january": "01",
            "february": "02",
            "march": "03",
            "april": "04",
            "may": "05",
            "june": "06",
            "july": "07",
            "august": "08",
            "september": "09",
            "october": "10",
            "november": "11",
            "december": "12",
        }
        # Build list of 2-char month codes
        month_codes = [month_map[h.lower()] for h in month_hints if h.lower() in month_map]
        if not month_codes:
            return []

        # Pull candidate memories (no month substring in SurrealQL, filter in Python)
        rows = self._q(
            "SELECT id, created_at FROM memory WHERE heat >= $min LIMIT $lim",
            {"min": min_heat, "lim": limit * 10},
        )
        results = []
        for row in rows:
            ca = row.get("created_at", "")
            # ISO format: YYYY-MM-...  month is chars 5-7 (0-indexed)
            if isinstance(ca, str) and len(ca) >= 7:
                if ca[5:7] in month_codes:
                    results.append(self._extract_id(row["id"]))
            if len(results) >= limit:
                break
        return results

    # ------------------------------------------------------------------ Vector Search

    def insert_vector(self, memory_id: int, embedding: bytes):
        """Update the embedding field on the memory record."""
        floats = self._bytes_to_floats(embedding)
        self._db.query(
            "UPDATE type::thing('memory', $id) SET embedding = $emb",
            {"id": memory_id, "emb": floats},
        )

    def delete_vector(self, memory_id: int):
        """Clear the embedding field on the memory record."""
        self._db.query(
            "UPDATE type::thing('memory', $id) SET embedding = NONE",
            {"id": memory_id},
        )

    def update_vector(self, memory_id: int, embedding: bytes):
        """Update embedding (same as insert in SurrealDB — field update)."""
        self.insert_vector(memory_id, embedding)

    def insert_implicit_vector(self, memory_id: int, embedding: bytes):
        """Store implicit embedding on the memory record."""
        floats = self._bytes_to_floats(embedding)
        self._db.query(
            "UPDATE type::thing('memory', $id) SET implicit_embedding = $emb",
            {"id": memory_id, "emb": floats},
        )

    def search_vectors(
        self,
        query_embedding: bytes,
        top_k: int = 10,
        min_heat: float = 0.1,
    ) -> list[tuple[int, float]]:
        """KNN search via MTREE index, filtered by min_heat.

        Returns list of (memory_id, distance) tuples sorted by ascending distance.
        """
        fetch_k = min(top_k * 4, 4096)
        floats = self._bytes_to_floats(query_embedding)
        # KNN limit <|K|> must be a literal, not a parameter
        rows = self._q(
            f"SELECT id, heat, vector::similarity::cosine(embedding, $qv) AS sim "
            f"FROM memory WHERE embedding <|{fetch_k}|> $qv "
            f"ORDER BY sim DESC",
            {"qv": floats},
        )
        results = []
        for row in rows:
            if float(row.get("heat", 0)) < min_heat:
                continue
            mid = self._extract_id(row.get("id"))
            # Convert similarity to distance for backward compat with sqlite-vec
            dist = 1.0 - float(row.get("sim", 0.0))
            results.append((mid, dist))
            if len(results) >= top_k:
                break
        return results

    def search_implicit_vectors(
        self,
        query_embedding: bytes,
        top_k: int = 10,
    ) -> list[tuple[int, float]]:
        """KNN search over implicit embedding vectors.

        Returns list of (memory_id, distance) tuples sorted by ascending distance.
        """
        fetch_k = min(top_k * 4, 4096)
        floats = self._bytes_to_floats(query_embedding)
        rows = self._q(
            f"SELECT id, vector::similarity::cosine(implicit_embedding, $qv) AS sim "
            f"FROM memory WHERE implicit_embedding <|{fetch_k}|> $qv "
            f"ORDER BY sim DESC",
            {"qv": floats},
        )
        results = []
        for row in rows:
            mid = self._extract_id(row.get("id"))
            dist = 1.0 - float(row.get("sim", 0.0))
            results.append((mid, dist))
            if len(results) >= top_k:
                break
        return results

    def get_memories_needing_reembedding(self, current_model: str) -> list[dict]:
        rows = self._q(
            "SELECT * FROM memory WHERE embedding IS NOT NONE "
            "AND (embedding_model IS NONE OR embedding_model != $model)",
            {"model": current_model},
        )
        return self._rows_to_dicts(rows)

    def update_memory_embedding(self, memory_id: int, embedding: bytes, embedding_model: str):
        floats = self._bytes_to_floats(embedding)
        self._db.query(
            "UPDATE type::thing('memory', $id) SET embedding = $emb, embedding_model = $model",
            {"id": memory_id, "emb": floats, "model": embedding_model},
        )
        # update_vector is a no-op distinction in SurrealDB (already done above)
        try:
            self.update_vector(memory_id, embedding)
        except Exception:
            try:
                self.insert_vector(memory_id, embedding)
            except Exception:
                pass

    def recreate_vector_table(self, new_dim: int):
        """Drop and recreate the MTREE index with new dimensions; clear all embeddings."""
        self._db.query("REMOVE INDEX IF EXISTS memory_embedding_idx ON memory")
        self._db.query("UPDATE memory SET embedding = NONE")
        self._db.query(
            f"DEFINE INDEX memory_embedding_idx ON memory FIELDS embedding "
            f"MTREE DIMENSION {new_dim} DIST COSINE"
        )
        self._embedding_dim = new_dim

    def update_memory_compression(
        self,
        memory_id: int,
        content: str,
        embedding: bytes | None,
        compression_level: int,
        original_content: str | None = None,
    ):
        floats = self._bytes_to_floats(embedding) if embedding else None
        params: dict = {
            "id": memory_id,
            "content": content,
            "emb": floats,
            "level": compression_level,
        }
        if original_content is not None:
            params["orig"] = original_content
            self._db.query(
                "UPDATE type::thing('memory', $id) SET content = $content, "
                "embedding = $emb, compression_level = $level, original_content = $orig",
                params,
            )
        else:
            self._db.query(
                "UPDATE type::thing('memory', $id) SET content = $content, "
                "embedding = $emb, compression_level = $level",
                params,
            )
        if embedding is not None:
            try:
                self.update_vector(memory_id, embedding)
            except Exception:
                try:
                    self.insert_vector(memory_id, embedding)
                except Exception:
                    pass

    # ------------------------------------------------------------------ Entities

    def insert_entity(self, entity: dict) -> int:
        now = self._now_iso()
        eid = self._next_id("entity")
        self._db.query(
            "CREATE type::thing('entity', $id) SET "
            "name = $name, type = $type, created_at = $created_at, "
            "last_accessed = $last_accessed, heat = $heat, archived = $archived",
            {
                "id": eid,
                "name": entity["name"],
                "type": entity["type"],
                "created_at": entity.get("created_at", now),
                "last_accessed": entity.get("last_accessed", now),
                "heat": entity.get("heat", 1.0),
                "archived": bool(entity.get("archived", False)),
            },
        )
        return eid

    def get_entity_by_name(self, name: str) -> dict | None:
        rows = self._q(
            "SELECT * FROM entity WHERE name = $name LIMIT 1",
            {"name": name},
        )
        return self._row_to_dict(rows[0]) if rows else None

    def get_all_entities(self, min_heat: float = 0.0, include_archived: bool = False) -> list[dict]:
        if include_archived:
            rows = self._q(
                "SELECT * FROM entity WHERE heat >= $min ORDER BY heat DESC",
                {"min": min_heat},
            )
        else:
            rows = self._q(
                "SELECT * FROM entity WHERE heat >= $min AND archived = false ORDER BY heat DESC",
                {"min": min_heat},
            )
        return self._rows_to_dicts(rows)

    def update_entity_heat(self, entity_id: int, new_heat: float):
        self._db.query(
            "UPDATE type::thing('entity', $id) SET heat = $heat",
            {"id": entity_id, "heat": new_heat},
        )

    def get_all_entities_for_decay(self) -> list[dict]:
        rows = self._q("SELECT * FROM entity WHERE archived = false")
        return self._rows_to_dicts(rows)

    def archive_entity(self, entity_id: int):
        self._db.query(
            "UPDATE type::thing('entity', $id) SET archived = true",
            {"id": entity_id},
        )

    def reinforce_entity(self, entity_id: int, heat_bump: float = 0.1):
        self._db.query(
            "UPDATE type::thing('entity', $id) SET "
            "heat = math::min(heat + $bump, 1.0), last_accessed = $now",
            {"id": entity_id, "bump": heat_bump, "now": self._now_iso()},
        )

    # ------------------------------------------------------------------ Relationships

    def insert_relationship(self, relationship: dict) -> int:
        now = self._now_iso()
        rid = self._next_id("relationship")
        self._db.query(
            "CREATE type::thing('relationship', $id) SET "
            "source_entity_id = $src, target_entity_id = $tgt, "
            "relationship_type = $rtype, weight = $weight, "
            "created_at = $created_at, last_reinforced = $last_reinforced",
            {
                "id": rid,
                "src": relationship["source_entity_id"],
                "tgt": relationship["target_entity_id"],
                "rtype": relationship["relationship_type"],
                "weight": relationship.get("weight", 1.0),
                "created_at": relationship.get("created_at", now),
                "last_reinforced": relationship.get("last_reinforced", now),
            },
        )
        return rid

    def get_relationship_between(self, source_id: int, target_id: int) -> dict | None:
        rows = self._q(
            "SELECT * FROM relationship WHERE "
            "(source_entity_id = $src AND target_entity_id = $tgt) OR "
            "(source_entity_id = $tgt AND target_entity_id = $src) LIMIT 1",
            {"src": source_id, "tgt": target_id},
        )
        return self._row_to_dict(rows[0]) if rows else None

    def get_typed_relationship(self, source_id: int, target_id: int, rel_type: str) -> dict | None:
        """Return a relationship between two entities of a specific type (directional)."""
        rows = self._q(
            "SELECT * FROM relationship WHERE "
            "source_entity_id = $src AND target_entity_id = $tgt "
            "AND relationship_type = $rt LIMIT 1",
            {"src": source_id, "tgt": target_id, "rt": rel_type},
        )
        return self._row_to_dict(rows[0]) if rows else None

    def get_relationships_for_entity(
        self, entity_id: int, rel_types: list[str] | None = None
    ) -> list[dict]:
        """Return all relationships where entity_id is source or target, with entity names."""
        if rel_types:
            rows = self._q(
                "SELECT * FROM relationship WHERE "
                "(source_entity_id = $eid OR target_entity_id = $eid) "
                "AND relationship_type IN $types",
                {"eid": entity_id, "types": rel_types},
            )
        else:
            rows = self._q(
                "SELECT * FROM relationship WHERE "
                "source_entity_id = $eid OR target_entity_id = $eid",
                {"eid": entity_id},
            )
        results = self._rows_to_dicts(rows)
        # Enrich with entity names via lookup
        for d in results:
            src_id = int(d.get("source_entity_id", 0))
            tgt_id = int(d.get("target_entity_id", 0))
            src_rows = self._q(f"SELECT name FROM entity:{src_id}") if src_id else []
            tgt_rows = self._q(f"SELECT name FROM entity:{tgt_id}") if tgt_id else []
            d["source_name"] = src_rows[0]["name"] if src_rows else None
            d["target_name"] = tgt_rows[0]["name"] if tgt_rows else None
        return results

    def get_relationships_by_type_and_weight(
        self, rel_type: str, min_weight: float = 0.0
    ) -> list[dict]:
        """Return all relationships of a given type with weight >= min_weight."""
        rows = self._q(
            "SELECT * FROM relationship WHERE relationship_type = $rt AND weight >= $mw",
            {"rt": rel_type, "mw": min_weight},
        )
        return self._rows_to_dicts(rows)

    def update_relationship_fields(self, rel_id: int, **fields) -> None:
        """Update arbitrary columns on a relationship row."""
        if not fields:
            return
        sets = ", ".join(f"{k} = ${k}" for k in fields)
        params = dict(fields)
        params["id"] = rel_id
        self._db.query(f"UPDATE type::thing('relationship', $id) SET {sets}", params)

    def insert_typed_relationship(
        self,
        source_entity_id: int,
        target_entity_id: int,
        relationship_type: str,
        weight: float = 1.0,
        event_time: str | None = None,
        record_time: str | None = None,
        is_causal: int = 0,
        confidence: float = 1.0,
    ) -> int:
        """Insert a relationship with bi-temporal and causal metadata."""
        now = self._now_iso()
        rid = self._next_id("relationship")
        self._db.query(
            "CREATE type::thing('relationship', $id) SET "
            "source_entity_id = $src, target_entity_id = $tgt, "
            "relationship_type = $rt, weight = $w, "
            "created_at = $cat, last_reinforced = $lr, "
            "event_time = $et, record_time = $rct, "
            "is_causal = $ic, confidence = $conf",
            {
                "id": rid,
                "src": source_entity_id,
                "tgt": target_entity_id,
                "rt": relationship_type,
                "w": weight,
                "cat": record_time or now,
                "lr": record_time or now,
                "et": event_time or now,
                "rct": record_time or now,
                "ic": bool(is_causal),
                "conf": confidence,
            },
        )
        return rid

    def get_all_episodes(self) -> list[dict]:
        """Return all episodes ordered by timestamp ascending."""
        rows = self._q("SELECT * FROM episode ORDER BY timestamp ASC")
        return self._rows_to_dicts(rows)

    def reinforce_relationship(self, rel_id: int, weight_increase: float = 1.0):
        self._db.query(
            "UPDATE type::thing('relationship', $id) SET "
            "weight = weight + $inc, last_reinforced = $now",
            {"id": rel_id, "inc": weight_increase, "now": self._now_iso()},
        )

    # ------------------------------------------------------------------ File Hashes

    def upsert_file_hash(self, filepath: str, hash_value: str):
        now = self._now_iso()
        rows = self._q(
            "SELECT id FROM file_hash WHERE filepath = $fp LIMIT 1",
            {"fp": filepath},
        )
        if rows:
            fid = self._extract_id(rows[0]["id"])
            self._db.query(
                "UPDATE type::thing('file_hash', $id) SET hash = $hash, last_checked = $now",
                {"id": fid, "hash": hash_value, "now": now},
            )
        else:
            fid = self._next_id("file_hash")
            self._db.query(
                "CREATE type::thing('file_hash', $id) SET "
                "filepath = $fp, hash = $hash, last_checked = $now",
                {"id": fid, "fp": filepath, "hash": hash_value, "now": now},
            )

    def get_file_hash(self, filepath: str) -> str | None:
        rows = self._q(
            "SELECT hash FROM file_hash WHERE filepath = $fp LIMIT 1",
            {"fp": filepath},
        )
        return rows[0]["hash"] if rows else None

    def get_filepath_by_hash(self, hash_value: str) -> str | None:
        rows = self._q(
            "SELECT filepath FROM file_hash WHERE hash = $hash LIMIT 1",
            {"hash": hash_value},
        )
        return rows[0]["filepath"] if rows else None

    # ------------------------------------------------------------------ Consolidation Log

    def insert_consolidation_log(self, log: dict) -> int:
        cid = self._next_id("consolidation_log")
        self._db.query(
            "CREATE type::thing('consolidation_log', $id) SET "
            "timestamp = $timestamp, memories_added = $added, "
            "memories_updated = $updated, memories_archived = $archived, "
            "memories_deleted = $deleted, duration_ms = $duration_ms",
            {
                "id": cid,
                "timestamp": log.get("timestamp", self._now_iso()),
                "added": log.get("memories_added", 0),
                "updated": log.get("memories_updated", 0),
                "archived": log.get("memories_archived", 0),
                "deleted": log.get("memories_deleted", 0),
                "duration_ms": log.get("duration_ms", 0),
            },
        )
        return cid

    # ------------------------------------------------------------------ Stats

    def get_memory_stats(self) -> dict:
        total_rows = self._q("SELECT count() AS c FROM memory GROUP ALL")
        total = int(total_rows[0]["c"]) if total_rows else 0

        active_rows = self._q(
            "SELECT count() AS c FROM memory WHERE is_stale = false AND heat >= 0.05 GROUP ALL"
        )
        active = int(active_rows[0]["c"]) if active_rows else 0

        archived_rows = self._q("SELECT count() AS c FROM memory WHERE heat < 0.05 GROUP ALL")
        archived = int(archived_rows[0]["c"]) if archived_rows else 0

        stale_rows = self._q("SELECT count() AS c FROM memory WHERE is_stale = true GROUP ALL")
        stale = int(stale_rows[0]["c"]) if stale_rows else 0

        heat_rows = self._q("SELECT math::mean(heat) AS avg FROM memory GROUP ALL")
        avg_heat = (
            float(heat_rows[0]["avg"]) if heat_rows and heat_rows[0].get("avg") is not None else 0.0
        )

        log_rows = self._q("SELECT * FROM consolidation_log ORDER BY timestamp DESC LIMIT 1")
        last_consolidation = log_rows[0]["timestamp"] if log_rows else None

        return {
            "total_memories": total,
            "active_count": active,
            "archived_count": archived,
            "stale_count": stale,
            "avg_heat": avg_heat,
            "last_consolidation": last_consolidation,
        }

    # ------------------------------------------------------------------ Memory Clusters

    def insert_cluster(self, cluster: dict) -> int:
        now = self._now_iso()
        cid = self._next_id("memory_cluster")
        centroid = cluster.get("centroid_embedding")
        centroid_floats = self._bytes_to_floats(centroid) if centroid else None
        self._db.query(
            "CREATE type::thing('memory_cluster', $id) SET "
            "name = $name, level = $level, parent_cluster_id = $parent, "
            "summary = $summary, centroid_embedding = $centroid, "
            "member_count = $member_count, created_at = $created_at, "
            "last_updated = $last_updated, heat = $heat",
            {
                "id": cid,
                "name": cluster["name"],
                "level": cluster.get("level", 0),
                "parent": cluster.get("parent_cluster_id"),
                "summary": cluster.get("summary", ""),
                "centroid": centroid_floats,
                "member_count": cluster.get("member_count", 0),
                "created_at": cluster.get("created_at", now),
                "last_updated": cluster.get("last_updated", now),
                "heat": cluster.get("heat", 1.0),
            },
        )
        return cid

    def get_cluster(self, cluster_id: int) -> dict | None:
        cid = int(cluster_id)
        rows = self._q(f"SELECT * FROM memory_cluster:{cid}")
        return self._row_to_dict(rows[0]) if rows else None

    def get_clusters_by_level(self, level: int) -> list[dict]:
        rows = self._q(
            "SELECT * FROM memory_cluster WHERE level = $level ORDER BY heat DESC",
            {"level": level},
        )
        return self._rows_to_dicts(rows)

    def update_cluster(self, cluster_id: int, updates: dict):
        allowed = {
            "name",
            "level",
            "parent_cluster_id",
            "summary",
            "centroid_embedding",
            "member_count",
            "heat",
            "last_updated",
        }
        fields = {k: v for k, v in updates.items() if k in allowed}
        if not fields:
            return
        if "last_updated" not in fields:
            fields["last_updated"] = self._now_iso()
        # Convert centroid bytes if present
        if "centroid_embedding" in fields and isinstance(fields["centroid_embedding"], bytes):
            fields["centroid_embedding"] = self._bytes_to_floats(fields["centroid_embedding"])
        params = {"id": cluster_id}
        set_parts = []
        for k, v in fields.items():
            params[k] = v
            set_parts.append(f"{k} = ${k}")
        self._db.query(
            f"UPDATE type::thing('memory_cluster', $id) SET {', '.join(set_parts)}",
            params,
        )

    # ------------------------------------------------------------------ Prospective Memories

    def insert_prospective_memory(self, pm: dict) -> int:
        now = self._now_iso()
        pid = self._next_id("prospective_memory")
        self._db.query(
            "CREATE type::thing('prospective_memory', $id) SET "
            "content = $content, trigger_condition = $trigger_condition, "
            "trigger_type = $trigger_type, target_directory = $target_directory, "
            "is_active = $is_active, created_at = $created_at, "
            "triggered_at = $triggered_at, triggered_count = $triggered_count",
            {
                "id": pid,
                "content": pm["content"],
                "trigger_condition": pm["trigger_condition"],
                "trigger_type": pm["trigger_type"],
                "target_directory": pm.get("target_directory"),
                "is_active": bool(pm.get("is_active", True)),
                "created_at": pm.get("created_at", now),
                "triggered_at": pm.get("triggered_at"),
                "triggered_count": pm.get("triggered_count", 0),
            },
        )
        return pid

    def get_active_prospective_memories(self) -> list[dict]:
        rows = self._q("SELECT * FROM prospective_memory WHERE is_active = true")
        return self._rows_to_dicts(rows)

    def trigger_prospective_memory(self, pm_id: int):
        now = self._now_iso()
        self._db.query(
            "UPDATE type::thing('prospective_memory', $id) SET "
            "triggered_at = $now, triggered_count = triggered_count + 1",
            {"id": pm_id, "now": now},
        )

    # ------------------------------------------------------------------ Narrative Entries

    def insert_narrative_entry(self, entry: dict) -> int:
        now = self._now_iso()
        nid = self._next_id("narrative_entry")
        self._db.query(
            "CREATE type::thing('narrative_entry', $id) SET "
            "directory_context = $dir, summary = $summary, "
            "period_start = $period_start, period_end = $period_end, "
            "key_decisions = $key_decisions, key_events = $key_events, "
            "created_at = $created_at, heat = $heat",
            {
                "id": nid,
                "dir": entry["directory_context"],
                "summary": entry["summary"],
                "period_start": entry["period_start"],
                "period_end": entry["period_end"],
                "key_decisions": entry.get("key_decisions", []),
                "key_events": entry.get("key_events", []),
                "created_at": entry.get("created_at", now),
                "heat": entry.get("heat", 1.0),
            },
        )
        return nid

    def get_narratives_for_directory(self, directory: str, limit: int = 10) -> list[dict]:
        rows = self._q(
            "SELECT * FROM narrative_entry WHERE directory_context = $dir "
            "ORDER BY period_end DESC LIMIT $lim",
            {"dir": directory, "lim": limit},
        )
        return self._rows_to_dicts(rows)

    # ------------------------------------------------------------------ Astrocyte Processes

    def insert_astrocyte_process(self, proc: dict) -> int:
        now = self._now_iso()
        aid = self._next_id("astrocyte_process")
        self._db.query(
            "CREATE type::thing('astrocyte_process', $id) SET "
            "name = $name, domain = $domain, specialization = $specialization, "
            "memory_ids = $memory_ids, entity_ids = $entity_ids, "
            "heat = $heat, created_at = $created_at, last_active = $last_active",
            {
                "id": aid,
                "name": proc["name"],
                "domain": proc["domain"],
                "specialization": proc.get("specialization", ""),
                "memory_ids": proc.get("memory_ids", []),
                "entity_ids": proc.get("entity_ids", []),
                "heat": proc.get("heat", 1.0),
                "created_at": proc.get("created_at", now),
                "last_active": proc.get("last_active", now),
            },
        )
        return aid

    def get_astrocyte_processes(self) -> list[dict]:
        rows = self._q("SELECT * FROM astrocyte_process ORDER BY heat DESC")
        return self._rows_to_dicts(rows)

    def update_astrocyte_process(self, proc_id: int, updates: dict):
        allowed = {
            "name",
            "domain",
            "specialization",
            "memory_ids",
            "entity_ids",
            "heat",
            "last_active",
        }
        fields = {}
        for k, v in updates.items():
            if k not in allowed:
                continue
            fields[k] = v
        if not fields:
            return
        if "last_active" not in fields:
            fields["last_active"] = self._now_iso()
        params = {"id": proc_id}
        set_parts = []
        for k, v in fields.items():
            params[k] = v
            set_parts.append(f"{k} = ${k}")
        self._db.query(
            f"UPDATE type::thing('astrocyte_process', $id) SET {', '.join(set_parts)}",
            params,
        )

    # ------------------------------------------------------------------ Thermodynamics

    def update_memory_scores(
        self,
        memory_id: int,
        surprise_score: float | None = None,
        importance: float | None = None,
        emotional_valence: float | None = None,
    ):
        fields = {}
        if surprise_score is not None:
            fields["surprise_score"] = surprise_score
        if importance is not None:
            fields["importance"] = importance
        if emotional_valence is not None:
            fields["emotional_valence"] = emotional_valence
        if not fields:
            return
        params = {"id": memory_id}
        set_parts = []
        for k, v in fields.items():
            params[k] = v
            set_parts.append(f"{k} = ${k}")
        self._db.query(
            f"UPDATE type::thing('memory', $id) SET {', '.join(set_parts)}",
            params,
        )

    def update_memory_metamemory(
        self,
        memory_id: int,
        access_count: int,
        useful_count: int,
        confidence: float,
    ):
        self._db.query(
            "UPDATE type::thing('memory', $id) SET "
            "access_count = $ac, useful_count = $uc, confidence = $conf",
            {
                "id": memory_id,
                "ac": access_count,
                "uc": useful_count,
                "conf": confidence,
            },
        )

    def get_memories_in_time_window(self, center_time: str, window_minutes: int) -> list[dict]:
        """Return memories created within window_minutes of center_time."""
        # Parse center_time and compute window bounds in Python (no julianday in SurrealDB)
        try:
            center_dt = datetime.fromisoformat(center_time)
        except ValueError:
            return []
        from datetime import timedelta

        delta = timedelta(minutes=window_minutes)
        start = (center_dt - delta).isoformat()
        end = (center_dt + delta).isoformat()
        rows = self._q(
            "SELECT * FROM memory WHERE heat > 0 AND created_at >= $start AND created_at <= $end",
            {"start": start, "end": end},
        )
        return self._rows_to_dicts(rows)

    # ------------------------------------------------------------------ Memory Rules

    def insert_rule(self, rule: dict) -> int:
        now = self._now_iso()
        rid = self._next_id("memory_rule")
        self._db.query(
            "CREATE type::thing('memory_rule', $id) SET "
            "rule_type = $rule_type, scope = $scope, scope_value = $scope_value, "
            "condition = $condition, action = $action, priority = $priority, "
            "created_at = $created_at, is_active = $is_active",
            {
                "id": rid,
                "rule_type": rule["rule_type"],
                "scope": rule["scope"],
                "scope_value": rule.get("scope_value"),
                "condition": rule["condition"],
                "action": rule["action"],
                "priority": rule.get("priority", 0),
                "created_at": rule.get("created_at", now),
                "is_active": bool(rule.get("is_active", True)),
            },
        )
        return rid

    def get_rules_for_scope(self, scope: str, scope_value: str | None = None) -> list[dict]:
        if scope == "global":
            rows = self._q(
                "SELECT * FROM memory_rule WHERE scope = 'global' AND is_active = true "
                "ORDER BY priority DESC",
            )
        else:
            rows = self._q(
                "SELECT * FROM memory_rule WHERE scope = $scope AND scope_value = $sv "
                "AND is_active = true ORDER BY priority DESC",
                {"scope": scope, "sv": scope_value},
            )
        return self._rows_to_dicts(rows)

    def update_rule(self, rule_id: int, updates: dict):
        allowed = {
            "rule_type",
            "scope",
            "scope_value",
            "condition",
            "action",
            "priority",
            "is_active",
        }
        fields = {k: v for k, v in updates.items() if k in allowed}
        if not fields:
            return
        params = {"id": rule_id}
        set_parts = []
        for k, v in fields.items():
            params[k] = v
            set_parts.append(f"{k} = ${k}")
        self._db.query(
            f"UPDATE type::thing('memory_rule', $id) SET {', '.join(set_parts)}",
            params,
        )

    def delete_rule(self, rule_id: int):
        self._db.query(
            "DELETE type::thing('memory_rule', $id)",
            {"id": rule_id},
        )

    # ------------------------------------------------------------------ Memory Archives

    def insert_archive(self, archive: dict) -> int:
        now = self._now_iso()
        aid = self._next_id("memory_archive")
        emb = archive.get("embedding")
        emb_floats = self._bytes_to_floats(emb) if emb else None
        self._db.query(
            "CREATE type::thing('memory_archive', $id) SET "
            "original_memory_id = $orig, content = $content, embedding = $emb, "
            "archived_at = $archived_at, mismatch_score = $mismatch_score, "
            "archive_reason = $archive_reason",
            {
                "id": aid,
                "orig": archive["original_memory_id"],
                "content": archive["content"],
                "emb": emb_floats,
                "archived_at": archive.get("archived_at", now),
                "mismatch_score": archive.get("mismatch_score", 0.0),
                "archive_reason": archive.get("archive_reason", ""),
            },
        )
        return aid

    def get_archives_for_memory(self, memory_id: int) -> list[dict]:
        rows = self._q(
            "SELECT * FROM memory_archive WHERE original_memory_id = $mid "
            "ORDER BY archived_at DESC",
            {"mid": memory_id},
        )
        return self._rows_to_dicts(rows)

    # ------------------------------------------------------------------ Memory Transitions

    def insert_transition(self, transition: dict) -> int:
        now = self._now_iso()
        tid = self._next_id("memory_transition")
        self._db.query(
            "CREATE type::thing('memory_transition', $id) SET "
            "from_memory_id = $from_id, to_memory_id = $to_id, count = $count, "
            "last_transition = $last_transition, session_id = $session_id",
            {
                "id": tid,
                "from_id": transition["from_memory_id"],
                "to_id": transition["to_memory_id"],
                "count": transition.get("count", 1),
                "last_transition": transition.get("last_transition", now),
                "session_id": transition.get("session_id", ""),
            },
        )
        return tid

    def get_transition(self, from_id: int, to_id: int) -> dict | None:
        rows = self._q(
            "SELECT * FROM memory_transition "
            "WHERE from_memory_id = $from_id AND to_memory_id = $to_id LIMIT 1",
            {"from_id": from_id, "to_id": to_id},
        )
        return self._row_to_dict(rows[0]) if rows else None

    def increment_transition(self, from_id: int, to_id: int):
        now = self._now_iso()
        self._db.query(
            "UPDATE memory_transition SET count = count + 1, last_transition = $now "
            "WHERE from_memory_id = $from_id AND to_memory_id = $to_id",
            {"now": now, "from_id": from_id, "to_id": to_id},
        )

    def get_transitions_from(self, memory_id: int) -> list[dict]:
        rows = self._q(
            "SELECT * FROM memory_transition WHERE from_memory_id = $mid ORDER BY count DESC",
            {"mid": memory_id},
        )
        return self._rows_to_dicts(rows)

    def get_all_transitions(self) -> list[dict]:
        rows = self._q("SELECT from_memory_id, to_memory_id, count FROM memory_transition")
        # No id to extract; pass through as-is (no embedding fields)
        return [dict(r) for r in rows]

    def update_memory_sr_coords(self, memory_id: int, sr_x: float, sr_y: float):
        self._db.query(
            "UPDATE type::thing('memory', $id) SET sr_x = $x, sr_y = $y",
            {"id": memory_id, "x": sr_x, "y": sr_y},
        )

    def get_memories_with_sr_coords(self) -> list[dict]:
        rows = self._q("SELECT id, sr_x, sr_y FROM memory WHERE sr_x != 0.0 OR sr_y != 0.0")
        return [
            {
                "id": self._extract_id(r.get("id")),
                "sr_x": r.get("sr_x", 0.0),
                "sr_y": r.get("sr_y", 0.0),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------ Causal DAG Edges

    def insert_causal_edge(self, edge: dict) -> int:
        now = self._now_iso()
        eid = self._next_id("causal_dag_edge")
        self._db.query(
            "CREATE type::thing('causal_dag_edge', $id) SET "
            "source_entity_id = $src, target_entity_id = $tgt, "
            "algorithm = $algo, confidence = $conf, "
            "discovered_at = $discovered_at, is_validated = $is_validated",
            {
                "id": eid,
                "src": edge["source_entity_id"],
                "tgt": edge["target_entity_id"],
                "algo": edge.get("algorithm", "pc"),
                "conf": edge.get("confidence", 1.0),
                "discovered_at": edge.get("discovered_at", now),
                "is_validated": bool(edge.get("is_validated", False)),
            },
        )
        return eid

    def get_causal_edges_for_entity(self, entity_id: int) -> list[dict]:
        rows = self._q(
            "SELECT * FROM causal_dag_edge "
            "WHERE source_entity_id = $eid OR target_entity_id = $eid",
            {"eid": entity_id},
        )
        return self._rows_to_dicts(rows)

    def get_all_causal_edges(self) -> list[dict]:
        rows = self._q("SELECT * FROM causal_dag_edge ORDER BY confidence DESC")
        return self._rows_to_dicts(rows)

    # ------------------------------------------------------------------ Engram Slots

    def init_engram_slots(self, num_slots: int):
        """Ensure all slot indices exist in the engram_slot table."""
        now = self._now_iso()
        for i in range(num_slots):
            existing = self._q(
                "SELECT id FROM engram_slot WHERE slot_index = $si LIMIT 1",
                {"si": i},
            )
            if not existing:
                sid = self._next_id("engram_slot")
                self._db.query(
                    "CREATE type::thing('engram_slot', $id) SET "
                    "slot_index = $si, excitability = 0.0, last_activated = $now",
                    {"id": sid, "si": i, "now": now},
                )

    def get_engram_slot(self, slot_index: int) -> dict | None:
        rows = self._q(
            "SELECT * FROM engram_slot WHERE slot_index = $si LIMIT 1",
            {"si": slot_index},
        )
        return self._row_to_dict(rows[0]) if rows else None

    def get_all_engram_slots(self) -> list[dict]:
        rows = self._q("SELECT * FROM engram_slot ORDER BY slot_index")
        return [self._row_to_dict(r) for r in rows]

    def update_engram_slot(self, slot_index: int, excitability: float, last_activated: str):
        self._db.query(
            "UPDATE engram_slot SET excitability = $exc, last_activated = $la "
            "WHERE slot_index = $si",
            {"si": slot_index, "exc": excitability, "la": last_activated},
        )

    def assign_memory_slot(self, memory_id: int, slot_index: int):
        now = self._now_iso()
        self._db.query(
            "UPDATE type::thing('memory', $id) SET "
            "slot_index = $si, excitability = 1.0, last_excitability_update = $now",
            {"id": memory_id, "si": slot_index, "now": now},
        )

    def get_memories_in_slot(self, slot_index: int) -> list[dict]:
        rows = self._q(
            "SELECT * FROM memory WHERE slot_index = $si AND heat > 0 ORDER BY created_at",
            {"si": slot_index},
        )
        return self._rows_to_dicts(rows)

    def get_slot_occupancy(self) -> dict:
        """Return {slot_index: count} for all occupied slots."""
        rows = self._q(
            "SELECT slot_index, count() AS cnt FROM memory "
            "WHERE slot_index IS NOT NONE GROUP BY slot_index"
        )
        result = {}
        for row in rows:
            si = row.get("slot_index")
            cnt = int(row.get("cnt", 0))
            if si is not None:
                result[si] = cnt
        return result

    # ------------------------------------------------------------------ Checkpoints

    def insert_checkpoint(self, data: dict) -> int:
        """Insert a new checkpoint, deactivating all previous ones."""
        now = self._now_iso()
        self._db.query("UPDATE checkpoint SET is_active = false WHERE is_active = true")
        cid = self._next_id("checkpoint")
        self._db.query(
            "CREATE type::thing('checkpoint', $id) SET "
            "session_id = $session_id, directory_context = $dir, "
            "current_task = $task, files_being_edited = $files, "
            "key_decisions = $decisions, open_questions = $questions, "
            "next_steps = $steps, active_errors = $errors, "
            "custom_context = $custom, epoch = $epoch, "
            "created_at = $now, is_active = true",
            {
                "id": cid,
                "session_id": data.get("session_id", "default"),
                "dir": data["directory_context"],
                "task": data.get("current_task", ""),
                "files": data.get("files_being_edited", []),
                "decisions": data.get("key_decisions", []),
                "questions": data.get("open_questions", []),
                "steps": data.get("next_steps", []),
                "errors": data.get("active_errors", []),
                "custom": data.get("custom_context", ""),
                "epoch": data.get("epoch", 0),
                "now": now,
            },
        )
        return cid

    def get_active_checkpoint(self) -> dict | None:
        """Get the most recent active checkpoint."""
        rows = self._q(
            "SELECT * FROM checkpoint WHERE is_active = true ORDER BY created_at DESC LIMIT 1"
        )
        if not rows:
            return None
        return self._row_to_dict(rows[0])

    def get_current_epoch(self) -> int:
        """Get the current compaction epoch number."""
        rows = self._q("SELECT math::max(epoch) AS max_epoch FROM checkpoint GROUP ALL")
        if rows and rows[0].get("max_epoch") is not None:
            return int(rows[0]["max_epoch"])
        return 0

    def increment_epoch(self) -> int:
        """Increment and return the new epoch number."""
        current = self.get_current_epoch()
        return current + 1

    # ------------------------------------------------------------------ User Profiles

    def insert_profile(
        self,
        entity_name: str,
        attribute_type: str,
        attribute_key: str,
        attribute_value: str,
        memory_id: int | None = None,
        confidence: float = 0.5,
        directory_context: str | None = None,
    ) -> int:
        now = self._now_iso()
        # Check if profile already exists
        existing = self._q(
            "SELECT id, confidence, evidence_memory_ids FROM user_profile "
            "WHERE entity_name = $en AND attribute_type = $at AND attribute_key = $ak "
            "AND directory_context = $dc LIMIT 1",
            {
                "en": entity_name,
                "at": attribute_type,
                "ak": attribute_key,
                "dc": directory_context,
            },
        )

        if existing:
            row = existing[0]
            pid = self._extract_id(row["id"])
            new_confidence = min(float(row.get("confidence", 0.5)) + 0.1, 1.0)
            evidence = row.get("evidence_memory_ids", [])
            if isinstance(evidence, str):
                evidence = json.loads(evidence)
            if memory_id is not None and memory_id not in evidence:
                evidence.append(memory_id)
            self._db.query(
                "UPDATE type::thing('user_profile', $id) SET "
                "attribute_value = $av, confidence = $conf, "
                "evidence_memory_ids = $evids, updated_at = $now",
                {
                    "id": pid,
                    "av": attribute_value,
                    "conf": new_confidence,
                    "evids": evidence,
                    "now": now,
                },
            )
            return pid

        evidence = [memory_id] if memory_id is not None else []
        pid = self._next_id("user_profile")
        self._db.query(
            "CREATE type::thing('user_profile', $id) SET "
            "entity_name = $en, attribute_type = $at, attribute_key = $ak, "
            "attribute_value = $av, evidence_memory_ids = $evids, "
            "confidence = $conf, created_at = $now, updated_at = $now, "
            "directory_context = $dc",
            {
                "id": pid,
                "en": entity_name,
                "at": attribute_type,
                "ak": attribute_key,
                "av": attribute_value,
                "evids": evidence,
                "conf": confidence,
                "now": now,
                "dc": directory_context,
            },
        )
        return pid

    def search_profiles_fts(self, query: str, limit: int = 10) -> list[dict]:
        rows = self._q(
            "SELECT * FROM user_profile WHERE entity_name @@ $q "
            "OR attribute_type @@ $q OR attribute_key @@ $q OR attribute_value @@ $q "
            "LIMIT $lim",
            {"q": query, "lim": limit},
        )
        return self._rows_to_dicts(rows)

    def get_profiles_for_entity(
        self, entity_name: str, directory_context: str | None = None
    ) -> list[dict]:
        if directory_context is not None:
            rows = self._q(
                "SELECT * FROM user_profile WHERE entity_name = $en AND directory_context = $dc",
                {"en": entity_name, "dc": directory_context},
            )
        else:
            rows = self._q(
                "SELECT * FROM user_profile WHERE entity_name = $en",
                {"en": entity_name},
            )
        return self._rows_to_dicts(rows)

    # ------------------------------------------------------------------ Derived Beliefs

    def insert_belief(
        self,
        belief_type: str,
        subject: str,
        content: str,
        evidence_memory_ids: list[int] | None = None,
        confidence: float = 0.5,
        embedding: bytes | None = None,
        embedding_model: str | None = None,
        directory_context: str | None = None,
    ) -> int:
        now = self._now_iso()
        evidence = evidence_memory_ids or []
        emb_floats = self._bytes_to_floats(embedding) if embedding else None
        bid = self._next_id("derived_belief")
        self._db.query(
            "CREATE type::thing('derived_belief', $id) SET "
            "belief_type = $bt, subject = $subject, content = $content, "
            "evidence_memory_ids = $evids, confidence = $conf, "
            "embedding = $emb, embedding_model = $em, "
            "created_at = $now, updated_at = $now, directory_context = $dc",
            {
                "id": bid,
                "bt": belief_type,
                "subject": subject,
                "content": content,
                "evids": evidence,
                "conf": confidence,
                "emb": emb_floats,
                "em": embedding_model,
                "now": now,
                "dc": directory_context,
            },
        )
        return bid

    def search_beliefs_fts(self, query: str, limit: int = 10) -> list[dict]:
        rows = self._q(
            "SELECT * FROM derived_belief WHERE subject @@ $q "
            "OR belief_type @@ $q OR content @@ $q LIMIT $lim",
            {"q": query, "lim": limit},
        )
        return self._rows_to_dicts(rows)

    def get_beliefs_for_subject(
        self, subject: str, directory_context: str | None = None
    ) -> list[dict]:
        if directory_context is not None:
            rows = self._q(
                "SELECT * FROM derived_belief WHERE subject = $subj AND directory_context = $dc",
                {"subj": subject, "dc": directory_context},
            )
        else:
            rows = self._q(
                "SELECT * FROM derived_belief WHERE subject = $subj",
                {"subj": subject},
            )
        return self._rows_to_dicts(rows)

    # ------------------------------------------------------------------ Store-type + episode + relationship queries

    def get_memories_by_store_type(
        self, store_type: str, directory: str | None = None
    ) -> list[dict]:
        if directory:
            rows = self._q(
                "SELECT * FROM memory WHERE store_type = $st "
                "AND heat > 0 AND embedding IS NOT NONE "
                "AND directory_context = $dir",
                {"st": store_type, "dir": directory},
            )
        else:
            rows = self._q(
                "SELECT * FROM memory WHERE store_type = $st "
                "AND heat > 0 AND embedding IS NOT NONE",
                {"st": store_type},
            )
        return self._rows_to_dicts(rows)

    def get_episode_session_id(self, episode_id: int) -> str | None:
        rows = self._q(f"SELECT session_id FROM episode:{episode_id}")
        return rows[0].get("session_id") if rows else None

    def get_relationship_by_source_and_type(
        self, source_entity_id: int, relationship_type: str
    ) -> dict | None:
        rows = self._q(
            "SELECT * FROM relationship WHERE source_entity_id = $src "
            "AND relationship_type = $rt LIMIT 1",
            {"src": source_entity_id, "rt": relationship_type},
        )
        return self._row_to_dict(rows[0]) if rows else None

    # ------------------------------------------------------------------ Generic helpers

    def insert_action_log(
        self,
        tool_name: str,
        tool_input_summary: str,
        directory: str,
        session_id: str,
        timestamp: str,
    ):
        aid = self._next_id("action_log")
        self._db.create(
            f"action_log:{aid}",
            {
                "tool_name": tool_name,
                "tool_input_summary": tool_input_summary,
                "directory": directory,
                "session_id": session_id,
                "timestamp": timestamp,
                "processed": False,
            },
        )

    def update_memory_fields(self, memory_id: int, **fields):
        if not fields:
            return
        converted = {}
        for k, v in fields.items():
            if k in _EMBEDDING_FIELDS and isinstance(v, bytes):
                converted[k] = self._bytes_to_floats(v)
            elif k in ("is_protected", "is_stale", "is_prospective", "compressed"):
                converted[k] = bool(v)
            else:
                converted[k] = v
        set_parts = []
        params = {}
        for i, (k, v) in enumerate(converted.items()):
            pname = f"v{i}"
            set_parts.append(f"{k} = ${pname}")
            params[pname] = v
        self._q(f"UPDATE memory:{memory_id} SET {', '.join(set_parts)}", params)

    def update_memory_last_accessed(self, memory_id: int, timestamp: str):
        self._q(
            f"UPDATE memory:{memory_id} SET last_accessed = $ts",
            {"ts": timestamp},
        )

    def get_total_reconsolidation_count(self) -> int:
        rows = self._q("SELECT math::sum(reconsolidation_count) AS total FROM memory GROUP ALL")
        return int(rows[0]["total"]) if rows and rows[0].get("total") is not None else 0

    def count_memories_by_store_type(self, store_type: str) -> int:
        rows = self._q(
            "SELECT count() AS c FROM memory WHERE store_type = $st AND heat > 0 GROUP ALL",
            {"st": store_type},
        )
        return int(rows[0]["c"]) if rows else 0

    def count_memories_by_compression_level(self, level: int) -> int:
        rows = self._q(
            "SELECT count() AS c FROM memory WHERE compression_level = $lvl AND heat > 0 GROUP ALL",
            {"lvl": level},
        )
        return int(rows[0]["c"]) if rows else 0

    # ------------------------------------------------------------------ Action Log

    def get_unprocessed_actions(self, limit: int = 200) -> list[dict]:
        rows = self._q(
            "SELECT * FROM action_log WHERE processed = false ORDER BY timestamp ASC LIMIT $lim",
            {"lim": limit},
        )
        return self._rows_to_dicts(rows)

    def mark_actions_processed(self, ids: list[int]):
        if not ids:
            return
        for aid in ids:
            self._q(f"UPDATE action_log:{aid} SET processed = true")

    def get_entity_by_id(self, entity_id: int) -> dict | None:
        """Fetch a single entity row by its integer ID."""
        eid = int(entity_id)
        rows = self._q(f"SELECT * FROM entity:{eid}")
        return self._row_to_dict(rows[0]) if rows else None

    def find_memory_ids_by_entity_name(self, entity_name: str) -> list[int]:
        """Find memory IDs whose content contains the entity name.

        Uses SurrealDB full-text search; falls back to string::contains if
        the FTS index is not available.
        """
        try:
            rows = self._q(
                "SELECT id FROM memory WHERE content @@ $q AND heat > 0",
                {"q": entity_name},
            )
            return [self._extract_id(r.get("id")) for r in rows]
        except Exception:
            rows = self._q(
                "SELECT id FROM memory WHERE string::contains(content, $name) AND heat > 0",
                {"name": entity_name},
            )
            return [self._extract_id(r.get("id")) for r in rows]

    # ------------------------------------------------------------------ Rules (additional queries)

    def get_all_active_rules_by_scope(self, scope: str) -> list[dict]:
        """Return all active rules for a given scope type (no scope_value filtering).

        Used by the rules engine to do its own prefix/glob matching in Python.
        """
        rows = self._q(
            "SELECT * FROM memory_rule WHERE scope = $scope AND is_active = true "
            "ORDER BY priority DESC",
            {"scope": scope},
        )
        return self._rows_to_dicts(rows)

    def get_rule(self, rule_id: int) -> dict | None:
        """Fetch a single rule by ID."""
        rid = int(rule_id)
        rows = self._q(f"SELECT * FROM memory_rule:{rid}")
        return self._row_to_dict(rows[0]) if rows else None

    def get_all_active_rules(self) -> list[dict]:
        """Return all active rules, sorted by scope then priority descending."""
        rows = self._q(
            "SELECT * FROM memory_rule WHERE is_active = true ORDER BY scope, priority DESC"
        )
        return self._rows_to_dicts(rows)

    # ------------------------------------------------------------------ Memory protection and anchoring

    def protect_memory(
        self,
        memory_id: int,
        is_protected: bool,
        importance: float,
        contextual_prefix: str | None = None,
    ):
        """Set is_protected, importance, and optionally contextual_prefix on a memory."""
        if contextual_prefix is not None:
            self._db.query(
                "UPDATE type::thing('memory', $id) SET "
                "is_protected = $prot, importance = $imp, contextual_prefix = $prefix",
                {
                    "id": memory_id,
                    "prot": is_protected,
                    "imp": importance,
                    "prefix": contextual_prefix,
                },
            )
        else:
            self._db.query(
                "UPDATE type::thing('memory', $id) SET is_protected = $prot, importance = $imp",
                {"id": memory_id, "prot": is_protected, "imp": importance},
            )

    def get_anchored_memories(self, limit: int = 20) -> list[dict]:
        """Return protected memories tagged with _anchor, ordered by creation date desc."""
        rows = self._q(
            "SELECT * FROM memory "
            "WHERE is_protected = true AND heat > 0 AND '_anchor' INSIDE tags "
            "ORDER BY created_at DESC LIMIT $lim",
            {"lim": limit},
        )
        return self._rows_to_dicts(rows)

    def get_recent_memories(self, limit: int = 20, exclude_anchored: bool = True) -> list[dict]:
        """Return recent non-protected memories, ordered by creation date desc."""
        if exclude_anchored:
            rows = self._q(
                "SELECT * FROM memory "
                "WHERE heat > 0 AND (is_protected = false OR is_protected = NONE) AND '_anchor' NOTINSIDE tags "
                "ORDER BY created_at DESC LIMIT $lim",
                {"lim": limit},
            )
        else:
            rows = self._q(
                "SELECT * FROM memory WHERE heat > 0 AND (is_protected = false OR is_protected = NONE) "
                "ORDER BY created_at DESC LIMIT $lim",
                {"lim": limit},
            )
        return self._rows_to_dicts(rows)

    # ------------------------------------------------------------------ Checkpoint updates

    def update_checkpoint_epoch(self, checkpoint_id: int, epoch: int):
        """Update the epoch field on an existing checkpoint."""
        self._db.query(
            "UPDATE type::thing('checkpoint', $id) SET epoch = $epoch",
            {"id": checkpoint_id, "epoch": epoch},
        )

    # ------------------------------------------------------------------ Memory excitability

    def update_memory_excitability(self, memory_id: int, excitability: float):
        """Update excitability and last_excitability_update for a memory."""
        now = self._now_iso()
        self._db.query(
            "UPDATE type::thing('memory', $id) SET excitability = $exc, "
            "last_excitability_update = $now",
            {"id": memory_id, "exc": excitability, "now": now},
        )

    # ------------------------------------------------------------------ Wiki Pages

    def insert_wiki_page(self, page: dict) -> int:
        """Insert a new wiki page, return its integer ID."""
        now = self._now_iso()
        pid = self._next_id("wiki_page")
        embedding = page.get("embedding")
        emb_floats = self._bytes_to_floats(embedding) if isinstance(embedding, bytes) else embedding
        self._db.query(
            "CREATE type::thing('wiki_page', $id) SET "
            "title = $title, slug = $slug, content = $content, "
            "category = $category, tags = $tags, links = $links, "
            "confidence = $confidence, embedding = $embedding, "
            "source_memory_ids = $source_memory_ids, "
            "created_at = $created_at, updated_at = $updated_at",
            {
                "id": pid,
                "title": page.get("title", ""),
                "slug": page["slug"],
                "content": page.get("content", ""),
                "category": page.get("category"),
                "tags": page.get("tags", []),
                "links": page.get("links", []),
                "confidence": page.get("confidence", 1.0),
                "embedding": emb_floats,
                "source_memory_ids": page.get("source_memory_ids", []),
                "created_at": page.get("created_at", now),
                "updated_at": page.get("updated_at", now),
            },
        )
        return pid

    def update_wiki_page(self, page_id: int, updates: dict) -> bool:
        """Update fields on an existing wiki page. Return True if found."""
        if not updates:
            return False
        # Handle embedding conversion if present
        if "embedding" in updates and isinstance(updates["embedding"], bytes):
            updates = dict(updates)
            updates["embedding"] = self._bytes_to_floats(updates["embedding"])
        updates = dict(updates)
        updates["updated_at"] = self._now_iso()
        set_parts = []
        params = {"id": int(page_id)}
        for col, val in updates.items():
            set_parts.append(f"{col} = ${col}")
            params[col] = val
        rows = self._q(
            f"UPDATE type::thing('wiki_page', $id) SET {', '.join(set_parts)}",
            params,
        )
        return len(rows) > 0

    def get_wiki_page(self, page_id: int) -> dict | None:
        """Get a wiki page by ID."""
        pid = int(page_id)
        rows = self._q(f"SELECT * FROM wiki_page:{pid}")
        return self._row_to_dict(rows[0]) if rows else None

    def get_wiki_page_by_slug(self, slug: str) -> dict | None:
        """Get a wiki page by slug."""
        rows = self._q(
            "SELECT * FROM wiki_page WHERE slug = $slug LIMIT 1",
            {"slug": slug},
        )
        return self._row_to_dict(rows[0]) if rows else None

    def delete_wiki_page(self, page_id: int) -> bool:
        """Delete a wiki page by ID. Return True if deleted."""
        pid = int(page_id)
        # Check existence first
        rows = self._q(f"SELECT id FROM wiki_page:{pid}")
        if not rows:
            return False
        self._db.query(
            "DELETE type::thing('wiki_page', $id)",
            {"id": pid},
        )
        return True

    def list_wiki_pages(self, category: str | None = None) -> list[dict]:
        """List all wiki pages, optionally filtered by category."""
        if category:
            rows = self._q(
                "SELECT * FROM wiki_page WHERE category = $cat ORDER BY updated_at DESC",
                {"cat": category},
            )
        else:
            rows = self._q("SELECT * FROM wiki_page ORDER BY updated_at DESC")
        return self._rows_to_dicts(rows)

    # ------------------------------------------------------------------ Wiki Search

    def search_wiki_fts(self, query: str, limit: int = 10) -> list[dict]:
        """BM25 full-text search on wiki page content."""
        fts_query = self._preprocess_fts_query(query)
        rows = self._q(
            "SELECT * FROM wiki_page WHERE content @@ $q ORDER BY search::score(1) DESC LIMIT $lim",
            {"q": fts_query, "lim": limit},
        )
        return self._rows_to_dicts(rows)

    def search_wiki_fts_scored(self, query: str, limit: int = 10) -> list[tuple[int, float]]:
        """BM25 search returning (page_id, score) tuples."""
        fts_query = self._preprocess_fts_query(query)
        rows = self._q(
            "SELECT id, search::score(1) AS score FROM wiki_page "
            "WHERE content @1@ $q "
            "ORDER BY score DESC LIMIT $lim",
            {"q": fts_query, "lim": limit},
        )
        results = []
        for row in rows:
            pid = self._extract_id(row.get("id"))
            score = float(row.get("score", 0.0))
            results.append((pid, score))
        return results

    def search_wiki_vectors(
        self, query_embedding: bytes, top_k: int = 5
    ) -> list[tuple[int, float]]:
        """KNN search on wiki page embeddings. Returns (page_id, distance)."""
        fetch_k = min(top_k * 4, 4096)
        floats = self._bytes_to_floats(query_embedding)
        rows = self._q(
            f"SELECT id, vector::similarity::cosine(embedding, $qv) AS sim "
            f"FROM wiki_page WHERE embedding <|{fetch_k}|> $qv "
            f"ORDER BY sim DESC",
            {"qv": floats},
        )
        results = []
        for row in rows:
            pid = self._extract_id(row.get("id"))
            dist = 1.0 - float(row.get("sim", 0.0))
            results.append((pid, dist))
            if len(results) >= top_k:
                break
        return results

    # ------------------------------------------------------------------ Wiki Cross-References

    def replace_wiki_crossrefs(self, from_slug: str, to_slugs: list[str]) -> None:
        """Atomic replace: delete all existing crossrefs FROM this slug, insert new ones."""
        self._db.query(
            "DELETE FROM wiki_crossref WHERE from_slug = $slug",
            {"slug": from_slug},
        )
        for to_slug in to_slugs:
            self._db.query(
                "CREATE wiki_crossref SET from_slug = $from, to_slug = $to",
                {"from": from_slug, "to": to_slug},
            )

    def get_wiki_backlinks(self, slug: str) -> list[str]:
        """Get all slugs that link TO this slug."""
        rows = self._q(
            "SELECT from_slug FROM wiki_crossref WHERE to_slug = $slug",
            {"slug": slug},
        )
        return [row["from_slug"] for row in rows if "from_slug" in row]

    def get_all_wiki_crossrefs(self) -> list[dict]:
        """Get all cross-references for graph visualization."""
        rows = self._q("SELECT from_slug, to_slug FROM wiki_crossref")
        return [{"from_slug": r["from_slug"], "to_slug": r["to_slug"]} for r in rows]

    # ------------------------------------------------------------------ Context manager

    def close(self):
        # Unregister atexit to avoid double-close
        try:
            atexit.unregister(self.close)
        except Exception:
            pass
        try:
            self._db.close()
        except Exception:
            pass
        # Release the file lock
        if hasattr(self, "_lock_file") and self._lock_file and not self._lock_file.closed:
            try:
                fcntl.flock(self._lock_file, fcntl.LOCK_UN)
                self._lock_file.close()
            except Exception:
                pass
            try:
                self._lock_path.unlink(missing_ok=True)
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
