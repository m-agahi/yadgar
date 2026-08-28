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
        *,
        scope_sql: str = "",
        scope_params: dict | None = None,
    ) -> list[tuple[int, float]]:
        """Vector search over memory embeddings, filtered by min_heat.

        Returns list of (memory_id, distance) tuples sorted by ascending distance.

        Car C7 — TWO SHAPES, same correctness argument as the wiki arm
        (``_shared/storage/wiki.py::search_wiki_vectors``):

        * **No scope** → HNSW KNN. SurrealDB v3 requires ``<|K, EF|>`` —
          single-param ``<|K|>`` is broken.
        * **Scoped** → BRUTE-FORCE cosine with the predicate in the ``WHERE``.
          The KNN operator picks its ``fetch_k`` neighbours FIRST, so an added
          ``AND project_id = $p`` would filter what KNN already chose — a silent
          under-return, not a slowdown. Pre-filtering is the only shape that
          cannot starve a scoped recall.

        Car F1 — the scoped arm needs BOTH emptiness guards. SurrealDB's NONE
        and NULL are different values: ``IS NOT NONE`` ADMITS an explicit NULL,
        and ``IS NOT NULL`` ADMITS a NONE (see ``backend/graph/graph_api.py``
        line 320 for the same trap on the other side). Either one alone lets a
        row through to ``vector::similarity::cosine()``, which rejects it with
        "Expected ``array<number>`` but found ``NULL``" and kills the WHOLE
        query — every scoped recall, not just that row. The KNN arm is immune:
        the HNSW index never offers a non-array row as a neighbour.
        """
        floats = self._bytes_to_floats(query_embedding)
        params: dict = {"qv": floats}
        if scope_sql:
            where = (
                "embedding IS NOT NONE AND embedding IS NOT NULL "
                f"AND heat >= $minh AND ({scope_sql})"
            )
            tail = "ORDER BY sim DESC LIMIT $lim"
            params.update({"minh": min_heat, "lim": top_k, **(scope_params or {})})
        else:
            fetch_k = min(top_k * 4, 4096)
            where = f"embedding <|{fetch_k}, 40|> $qv"
            tail = "ORDER BY sim DESC"
        rows = self._q(
            f"SELECT id, heat, vector::similarity::cosine(embedding, $qv) AS sim "
            f"FROM memory WHERE {where} {tail}",
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
        except Exception:  # BLE001-KEEP: upsert emulation: update-then-insert against storage, whose failures share no common base; a miss here falls through to the insert below
            try:
                self.insert_vector(memory_id, embedding)
            except Exception:  # BLE001-KEEP: second half of the upsert emulation; the row was already written by the UPDATE above, so the sidecar write is best-effort
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
        except Exception as exc:  # BLE001-KEEP: best-effort pre-flight backup before non-transactional DDL: it runs DEFINE/DELETE/INSERT whose failures share no common base, and a missing backup must not block the index rebuild it protects
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
        except Exception:  # BLE001-KEEP: corruption probe: its contract is that ANY failure of the KNN read means the MTREE index is unusable, so the catch breadth IS the answer being computed
            return False

    @observe(tier="stage")
    def rebuild_vector_indexes(self) -> bool:
        """Rebuild both MTREE indexes from stored embeddings. Returns True on success."""
        try:
            self._q("REBUILD INDEX memory_embedding_idx ON memory")
            self._q("REBUILD INDEX memory_implicit_idx ON memory")
            return True
        except Exception:  # BLE001-KEEP: same corruption contract as probe_vector_indexes: any REBUILD failure means the container must restart, so the breadth IS the answer
            _log.critical("MTREE index rebuild failed — container restart required")
            return False
