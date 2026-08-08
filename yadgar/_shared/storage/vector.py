"""Vector search storage mixin — embedding CRUD and HNSW/MTREE index management."""

import logging

from yadgar._shared.observability.observe import observe
from yadgar._shared.observability.tracing import trace_span

_log = logging.getLogger(__name__)


class _VectorMixin:
    """Vector and implicit-vector embedding ops — mixed into StorageEngine."""

    # ------------------------------------------------------------------ Vector Search

    def insert_vector(self, memory_id: int, embedding: bytes):
        """Update the embedding field on the memory record."""
        floats = self._bytes_to_floats(embedding)
        self._q(
            "UPDATE type::record('memory', $id) SET embedding = $emb",
            {"id": memory_id, "emb": floats},
        )

    def delete_vector(self, memory_id: int):
        """Clear the embedding field on the memory record."""
        self._q(
            "UPDATE type::record('memory', $id) SET embedding = NONE",
            {"id": memory_id},
        )

    def update_vector(self, memory_id: int, embedding: bytes):
        """Update embedding (same as insert in SurrealDB — field update)."""
        self.insert_vector(memory_id, embedding)

    def insert_implicit_vector(self, memory_id: int, embedding: bytes):
        """Store implicit embedding on the memory record."""
        floats = self._bytes_to_floats(embedding)
        self._q(
            "UPDATE type::record('memory', $id) SET implicit_embedding = $emb",
            {"id": memory_id, "emb": floats},
        )

    @trace_span()
    def search_vectors(
        self,
        query_embedding: bytes,
        top_k: int = 10,
        min_heat: float = 0.1,
    ) -> list[tuple[int, float]]:
        """KNN search via HNSW index, filtered by min_heat.

        Returns list of (memory_id, distance) tuples sorted by ascending distance.
        SurrealDB v3: KNN operator requires <|K, EF|> — single-param <|K|> is broken.
        """
        fetch_k = min(top_k * 4, 4096)
        floats = self._bytes_to_floats(query_embedding)
        params = {"qv": floats}
        rows = self._q(
            f"SELECT id, heat, vector::similarity::cosine(embedding, $qv) AS sim "
            f"FROM memory WHERE embedding <|{fetch_k}, 40|> $qv "
            f"ORDER BY sim DESC",
            params,
        )
        results = []
        for row in rows:
            if float(row.get("heat", 0)) < min_heat:
                continue
            mid = self._extract_id(row.get("id"))
            # Convert similarity to distance
            dist = 1.0 - float(row.get("sim", 0.0))
            results.append((mid, dist))
            if len(results) >= top_k:
                break
        return results

    @trace_span()
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
            f"FROM memory WHERE implicit_embedding <|{fetch_k}, 40|> $qv "
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

    @observe(tier="stage")
    def update_memory_embedding(self, memory_id: int, embedding: bytes, embedding_model: str):
        floats = self._bytes_to_floats(embedding)
        self._q(
            "UPDATE type::record('memory', $id) SET embedding = $emb, embedding_model = $model",
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
        # Car 2 (backend 5.22.0): the memory_doc cache holds content+embedding by id.
        # This is the reembed path (reembed_stale / reembed_all) — a raw embedding
        # UPDATE that bypasses update_memory_fields' per-id evict. Without this bust
        # the cache serves a STALE embedding for up to TTL(2700s). Evict this id so
        # the next recall re-fetches the new embedding (mirrors memory.py's evict).
        try:
            self._resolve_memory_doc_cache().invalidate(int(memory_id))
        except Exception:  # noqa: BLE001 — cache bust must never fail a write
            _log.debug("memory_doc cache invalidate failed for %s", memory_id, exc_info=True)

    @observe(tier="stage")
    def recreate_vector_table(self, new_dim: int):
        """Drop and recreate the vector index with new dimensions; clear all embeddings.

        §6 C5: Wraps DROP INDEX → UPDATE embedding=NONE → REDEFINE INDEX in a
        transaction.  Pre-flight: copies existing embeddings to a sidecar table
        (memory_embedding_backup) so recovery is possible even if the TX fails.
        """
        # Pre-flight: back up existing embeddings to sidecar table.
        try:
            self._q("DEFINE TABLE IF NOT EXISTS memory_embedding_backup SCHEMALESS")
            self._q("DELETE FROM memory_embedding_backup")
            self._q(
                "INSERT INTO memory_embedding_backup "
                "(SELECT id, embedding FROM memory WHERE embedding IS NOT NONE)"
            )
            _log.info("recreate_vector_table: embedding backup written to memory_embedding_backup")
        except Exception as exc:
            _log.warning("recreate_vector_table: backup failed (%s) — proceeding anyway", exc)

        # DDL statements (REMOVE INDEX, DEFINE INDEX) are not transactional in
        # SurrealDB — wrapping in BEGIN/COMMIT causes the query to fail.
        # Recovery is handled via the pre-flight sidecar backup above.
        if self._db_url:
            index_def = (
                f"DEFINE INDEX memory_embedding_idx ON memory FIELDS embedding "
                f"HNSW DIMENSION {new_dim} DIST COSINE TYPE F32 EFC 150 M 12"
            )
        else:
            index_def = (
                f"DEFINE INDEX memory_embedding_idx ON memory FIELDS embedding "
                f"MTREE DIMENSION {new_dim} DIST COSINE TYPE F32"
            )
        try:
            self._q("REMOVE INDEX IF EXISTS memory_embedding_idx ON memory")
            self._q("UPDATE memory SET embedding = NONE")
            self._q(index_def)
        except Exception:
            _log.error("recreate_vector_table failed; embeddings may be in memory_embedding_backup")
            raise
        self._embedding_dim = new_dim

    @observe(tier="stage")
    def probe_vector_indexes(self) -> bool:
        """Quick KNN probe — returns False if either MTREE index is corrupted."""
        count = self._q("SELECT count() AS c FROM memory GROUP ALL")
        if not count or int(count[0]["c"]) == 0:
            return True  # empty table, nothing to corrupt
        try:
            zero = [0.0] * self._embedding_dim
            self._q(
                "SELECT id FROM memory WHERE embedding <|1, 40|> $qv LIMIT 1",
                {"qv": zero},
            )
            self._q(
                "SELECT id FROM memory WHERE implicit_embedding <|1, 40|> $qv LIMIT 1",
                {"qv": zero},
            )
            return True
        except Exception:
            return False

    @observe(tier="stage")
    def rebuild_vector_indexes(self) -> bool:
        """Rebuild both MTREE indexes from stored embeddings. Returns True on success."""
        try:
            self._q("REBUILD INDEX memory_embedding_idx ON memory")
            self._q("REBUILD INDEX memory_implicit_idx ON memory")
            return True
        except Exception:
            _log.critical("MTREE index rebuild failed — container restart required")
            return False
