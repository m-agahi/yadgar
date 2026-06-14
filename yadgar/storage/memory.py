"""Memory CRUD and primary-table operations.

_MemoryMixin provides memory insert/fetch/update/delete/search, plus
primary-table helpers that operate directly on the `memory` row:
  - Memory CRUD: insert_memory, get_memory, get_memories_by_heat,
    update_memory_heat, update_memory_staleness, delete_memory,
    get_memories_for_directory, get_stale_memories, get_memories_by_file_hash,
    get_all_memories_for_decay, get_all_memories_with_embeddings,
    get_memories_with_embeddings, get_memories_without_embeddings,
    search_memories_fts, search_memories_fts_scored,
    search_memories_by_content_date, search_memories_by_timestamp_range,
    search_memories_by_month
  - Vector field helpers: update_memory_compression (primary table; not a
    vector-index operation)
  - Store-type queries, generic field helpers
  - Memory protection and anchoring
  - Memory excitability

Domain tables extracted to dedicated mixins:
  consolidation_log, stats     → storage/ops.py     (_OpsMixin)
  prospective_memory, narrative_entry, astrocyte_process,
  derived_belief               → storage/narrative.py (_NarrativeMixin)
  memory_rule, memory_archive,
  memory_transition            → storage/rules.py    (_RulesMixin)
  causal_dag_edge              → storage/causal.py   (_CausalMixin)
  user_profile, thermodynamics → storage/user.py     (_UserMixin)
  engram_slot, checkpoint,
  prune_old_rows               → storage/ops.py      (_OpsMixin)

Episodes → storage/episode.py (_EpisodeMixin)
Entities + relationships → storage/entity.py (_EntityMixin)
Vector search → storage/vector.py (_VectorMixin)
Clusters + similarity links → storage/cluster.py (_ClusterMixin)
"""

import logging
import os
import re as _re

from yadgar.secrets import SecretLeakBlocked, check_secrets
from yadgar.tracing import trace_span

_log = logging.getLogger(__name__)

# S1 allowlist for provenance_agent values: ascii alphanumeric, hyphens, underscores.
# Rejects semicolons, quotes, spaces and other SQL-significant characters.
_PROVENANCE_AGENT_RE = _re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _validate_provenance_agent(value: str) -> str:
    """Validate and return provenance_agent value.

    Rules:
    - Non-empty string
    - ASCII alphanumeric + hyphens + underscores only (no SQL-injection chars)
    - Length ≤ 64 characters

    Raises ValueError on invalid input. Returns the value unchanged on success.
    """
    if not value or not isinstance(value, str):
        raise ValueError(f"provenance_agent must be a non-empty string, got {value!r}")
    if not _PROVENANCE_AGENT_RE.match(value):
        raise ValueError(
            f"provenance_agent {value!r} is invalid: must be ≤64 chars, "
            "ASCII alphanumeric/hyphen/underscore only"
        )
    return value


# Imported here so memory methods can reference these constants directly.
# The same constants are defined in client.py and re-exported from __init__.py;
# we import lazily to avoid a circular reference at module load time.
# (Methods reference them via self.__class__ module globals at runtime.)


def _get_consts():
    from yadgar.storage.client import (
        _EMBEDDING_FIELDS,
        _MEMORY_UPDATABLE_FIELDS,
        _RELATIONSHIP_UPDATABLE_FIELDS,
    )

    return _EMBEDDING_FIELDS, _MEMORY_UPDATABLE_FIELDS, _RELATIONSHIP_UPDATABLE_FIELDS


class _MemoryMixin:
    """Memory CRUD and primary-table operations — mixed into StorageEngine."""

    # ------------------------------------------------------------------ Memories

    def _build_memory_insert_clause(
        self, memory: dict, mid: int, now: str, branch: str | None, emb_floats
    ) -> tuple[str, dict]:
        """Build the CREATE SQL and params dict for a new memory row.

        Pure computation — no I/O, no side-effects.  Optional fields
        (branch, tier, valid_until, migration_grace) are appended only
        when present so SurrealDB does not store explicit NULLs for them.
        """
        sql = (
            "CREATE type::record('memory', $id) SET "
            "content = $content, embedding = $embedding, tags = $tags, "
            "source_episode_id = $source_episode_id, "
            "directory_context = $directory_context, "
            "created_at = $created_at, last_accessed = $last_accessed, "
            "heat = $heat, is_stale = $is_stale, file_hash = $file_hash, "
            "embedding_model = $embedding_model, "
            "plasticity = $plasticity, stability = $stability, "
            "excitability = $excitability, store_type = $store_type, "
            "compression_level = $compression_level, sr_x = $sr_x, sr_y = $sr_y, "
            "reconsolidation_count = $reconsolidation_count, "
            "provenance_agent = $provenance_agent, vector_clock = $vector_clock, "
            "is_protected = $is_protected"
        )
        params: dict = {
            "id": mid,
            "content": memory["content"],
            "embedding": emb_floats,
            "tags": memory.get("tags", []),
            "source_episode_id": memory.get("source_episode_id"),
            # v5.46.6: normalise empty-string directory_context to 'global' so it
            # surfaces in the global anchor bucket without relying on '' equality
            # (SurrealDB 2 embedded may not round-trip '' reliably in comparisons).
            "directory_context": memory["directory_context"] or "global",
            "created_at": memory.get("created_at", now),
            "last_accessed": memory.get("last_accessed", now),
            "heat": memory.get("heat", 1.0),
            "is_stale": bool(memory.get("is_stale", False)),
            "file_hash": memory.get("file_hash"),
            "embedding_model": memory.get("embedding_model"),
            "plasticity": memory.get("plasticity", 1.0),
            "stability": memory.get("stability", 0.0),
            "excitability": memory.get("excitability", 1.0),
            "store_type": memory.get("store_type", "episodic"),
            "compression_level": memory.get("compression_level", 0),
            "sr_x": memory.get("sr_x", 0.0),
            "sr_y": memory.get("sr_y", 0.0),
            "reconsolidation_count": memory.get("reconsolidation_count", 0),
            "provenance_agent": memory.get("provenance_agent", "default"),
            "vector_clock": memory.get("vector_clock", "{}"),
            "is_protected": bool(memory.get("is_protected", False)),
        }
        if branch is not None:
            sql += ", branch = $branch"
            params["branch"] = branch
        # v5.8.0: tier / valid_until / migration_grace (optional, nullable)
        if memory.get("tier") is not None:
            sql += ", tier = $tier"
            params["tier"] = memory["tier"]
        if memory.get("valid_until") is not None:
            sql += ", valid_until = $valid_until"
            params["valid_until"] = memory["valid_until"]
        if memory.get("migration_grace") is not None:
            sql += ", migration_grace = $migration_grace"
            params["migration_grace"] = bool(memory["migration_grace"])
        return sql, params

    def _validate_memory_secrets(self, memory: dict) -> None:
        """Layer 1 storage-level secret gate — last line of defence (P13/v5.10.2).

        Fires only if the API-boundary (Layer 2) gate was bypassed.
        YADGAR_SECRET_GATE_DISABLED=1 is a kill switch for emergencies only.

        SECURITY-CRITICAL: preserve exact gate semantics (audit P13) — same
        fields checked (content, tags, reason), same env bypass path, same
        exception type and message.  Do not weaken.

        Raises SecretLeakBlocked when a secret is detected.
        """
        if not os.environ.get("YADGAR_SECRET_GATE_DISABLED"):
            _content_str = memory.get("content", "") or ""
            _tags_str = " ".join(str(t) for t in (memory.get("tags") or []))
            _reason_str = memory.get("reason", "") or ""
            _blocked, _reason, _preview = check_secrets(_content_str)
            if not _blocked and _tags_str:
                _blocked, _reason, _preview = check_secrets(_tags_str)
            if not _blocked and _reason_str:
                _blocked, _reason, _preview = check_secrets(_reason_str)
            if _blocked:
                try:
                    from yadgar.metrics import yadgar_writegate_outcome  # noqa: PLC0415

                    yadgar_writegate_outcome.labels(outcome="rejected_secret_at_storage").inc()
                except Exception:
                    pass
                _log.error(
                    "storage_secret_gate_blocked",
                    extra={
                        "component": "storage.memory.insert_memory",
                        "outcome": "rejected_secret_at_storage",
                        "reason": _reason,
                        "preview": _preview,
                    },
                )
                raise SecretLeakBlocked(_reason, _preview)
        elif os.environ.get("YADGAR_SECRET_GATE_DISABLED"):
            _log.warning(
                "YADGAR_SECRET_GATE_DISABLED is set — storage-level secret gate bypassed. "
                "This is a kill switch for emergencies only. Remove it when resolved."
            )

    def _enrich_memory_if_enabled(  # noqa: C901 — pipeline flag+length+embedding guards + 6-field mapping; extract further degrades locality without reducing count
        self, mid: int, memory: dict, settings, embeddings_engine, embedding
    ) -> None:
        """Run optional INDEX_ENRICHMENT_ENABLED pipeline, update memory row in-place.

        Guard clauses at the top mean default deployments exit immediately
        (flag=False or missing) with zero overhead — identical to before.
        """
        from yadgar.storage import _get_enrichment_pipeline

        if not (
            settings
            and getattr(settings, "INDEX_ENRICHMENT_ENABLED", False)
            and len(memory["content"]) >= getattr(settings, "ENRICHMENT_MIN_CONTENT_LENGTH", 20)
            and embeddings_engine is not None
            and embedding is not None
        ):
            return

        try:
            pipeline = _get_enrichment_pipeline(settings, embeddings_engine)
            result = pipeline.enrich(memory["content"], embedding, settings)
            enrichment_data = {
                "enrichment_concepts": result.concepts if result.concepts else None,
                "enrichment_comet": result.comet_inferences if result.comet_inferences else None,
                "enrichment_queries": result.queries if result.queries else None,
                "enrichment_logic": result.logic_expansions if result.logic_expansions else None,
                "enriched_content": result.enriched_content or None,
                "enrichment_model_versions": result.model_versions
                if result.model_versions
                else None,
            }
            if not any(v is not None for v in enrichment_data.values()):
                return
            set_parts = []
            update_params: dict = {"id": mid}
            for col, val in enrichment_data.items():
                if val is not None:
                    set_parts.append(f"{col} = ${col}")
                    update_params[col] = val
            if not set_parts:
                return
            self._q(
                f"UPDATE type::record('memory', $id) SET {', '.join(set_parts)}",
                update_params,
            )
            if not enrichment_data.get("enriched_content"):
                return
            new_embedding = embeddings_engine.encode_document_enriched(
                memory["content"], enrichment_data["enriched_content"]
            )
            if new_embedding is None:
                return
            new_floats = self._bytes_to_floats(new_embedding)
            self._q(
                "UPDATE type::record('memory', $id) SET embedding = $emb",
                {"id": mid, "emb": new_floats},
            )
        except Exception as e:
            import logging

            logging.getLogger(__name__).warning("Enrichment failed: %s", e)

    @trace_span("storage.memory.insert_memory")
    def insert_memory(
        self, memory: dict, embeddings_engine=None, settings=None, branch: str | None = None
    ) -> int:
        now = self._now_iso()
        mid = self._next_id("memory")
        embedding = memory.get("embedding")
        emb_floats = self._bytes_to_floats(embedding) if embedding else None

        self._validate_memory_secrets(memory)
        sql, params = self._build_memory_insert_clause(memory, mid, now, branch, emb_floats)
        self._q(sql, params)
        self._enrich_memory_if_enabled(mid, memory, settings, embeddings_engine, embedding)

        # insert_vector is a no-op for the separate table, but keep for API compat
        if embedding is not None:
            self.insert_vector(mid, embedding)

        return mid

    @trace_span("storage.memory.get_memory")
    def get_memory(self, memory_id: int) -> dict | None:
        # Use direct record ID syntax — more reliable than type::record() in surrealkv
        mid = int(memory_id)  # sanitize
        rows = self._q(f"SELECT * FROM memory:{mid}")
        result = self._row_to_dict(rows[0]) if rows else None
        if result is not None:
            # SurrealDB omits fields set to NONE; add expected nullable defaults
            result.setdefault("embedding_model", None)
            result.setdefault("file_hash", None)
            result.setdefault("last_excitability_update", None)
            result.setdefault("original_content", None)
            result.setdefault("last_reconsolidated", None)
        return result

    @trace_span("storage.memory.get_memories_by_heat")
    def get_memories_by_heat(self, min_heat: float, limit: int = 100) -> list[dict]:
        rows = self._q(
            "SELECT * FROM memory WHERE heat >= $min ORDER BY heat DESC LIMIT $lim",
            {"min": min_heat, "lim": limit},
        )
        return self._rows_to_dicts(rows)

    def update_memory_heat(self, memory_id: int, new_heat: float):
        self._q(
            "UPDATE type::record('memory', $id) SET heat = $heat",
            {"id": memory_id, "heat": new_heat},
        )

    def update_memory_staleness(self, memory_id: int, is_stale: bool):
        self._q(
            "UPDATE type::record('memory', $id) SET is_stale = $stale",
            {"id": memory_id, "stale": is_stale},
        )

    @trace_span("storage.memory.delete_memory")
    def delete_memory(self, memory_id: int):
        # Delete FK dependents first
        self._q(
            "DELETE FROM memory_archive WHERE original_memory_id = $mid",
            {"mid": memory_id},
        )
        self._q(
            "DELETE FROM memory_transition WHERE from_memory_id = $mid OR to_memory_id = $mid",
            {"mid": memory_id},
        )
        # Without this, deleted memories leave dangling similarity links that
        # accumulate forever and bloat the store.
        self._q(
            "DELETE FROM memory_similarity_link "
            "WHERE source_memory_id = $mid OR target_memory_id = $mid",
            {"mid": memory_id},
        )
        # Clean up synthetic memory:<id> entity rows (created by curation, cls_store,
        # sleep_compute) and all relationship rows that reference them.  Without
        # this they accumulate forever — the historical "entity-table bloat" noted
        # in migration 003.
        ent_name = f"memory:{memory_id}"
        ent = self.get_entity_by_name(ent_name)  # resolved via _EntityMixin in MRO
        if ent is not None:
            eid = ent["id"]  # already an int after _row_to_dict normalisation
            self._q(
                "DELETE FROM relationship WHERE source_entity_id = $eid OR target_entity_id = $eid",
                {"eid": eid},
            )
            self._q("DELETE FROM entity WHERE name = $n", {"n": ent_name})
        # Clear vector fields (no separate table)
        try:
            self.delete_vector(memory_id)  # resolved via _VectorMixin in MRO
        except Exception:
            _log.warning("delete_vector failed for memory %s", memory_id, exc_info=True)
        try:
            self._q(
                "UPDATE type::record('memory', $id) SET implicit_embedding = NONE",
                {"id": memory_id},
            )
        except Exception:
            _log.warning("clear implicit_embedding failed for memory %s", memory_id, exc_info=True)
        self._q(
            "DELETE type::record('memory', $id)",
            {"id": memory_id},
        )

    def get_memories_for_directory(self, directory: str, min_heat: float = 0.1) -> list[dict]:
        rows = self._q(
            "SELECT * FROM memory WHERE directory_context = $dir AND heat >= $min "
            "AND (valid_until IS NONE OR valid_until > $now) "
            "ORDER BY heat DESC",
            {"dir": directory, "min": min_heat, "now": self._now_iso()},
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

    def get_memories_with_embeddings(
        self, limit: int | None = None, order_by: str = "last_accessed"
    ) -> list[dict]:
        """Return memories that have embeddings, ordered by `order_by` DESC.

        When `limit` is None behaves identically to get_all_memories_with_embeddings.
        Intended for callers that build an N×N similarity matrix and need to bound N.
        """
        allowed_order = {"last_accessed", "heat", "created_at"}
        if order_by not in allowed_order:
            order_by = "last_accessed"
        if limit is None:
            rows = self._q(
                f"SELECT * FROM memory WHERE embedding IS NOT NONE AND heat > 0 "
                f"ORDER BY {order_by} DESC"
            )
        else:
            rows = self._q(
                f"SELECT * FROM memory WHERE embedding IS NOT NONE AND heat > 0 "
                f"ORDER BY {order_by} DESC LIMIT $lim",
                {"lim": int(limit)},
            )
        return self._rows_to_dicts(rows)

    def get_memories_without_embeddings(self) -> list[dict]:
        rows = self._q("SELECT * FROM memory WHERE embedding IS NONE AND heat > 0")
        return self._rows_to_dicts(rows)

    @trace_span("storage.memory.search_memories_fts")
    def search_memories_fts(self, query: str, min_heat: float = 0.1, limit: int = 5) -> list[dict]:
        fts_query = self._preprocess_fts_query(query)
        rows = self._q(
            "SELECT * FROM memory WHERE content @@ $q AND heat >= $min "
            "ORDER BY heat DESC LIMIT $lim",
            {"q": fts_query, "min": min_heat, "lim": limit},
        )
        return self._rows_to_dicts(rows)

    @trace_span("storage.memory.search_memories_fts_scored")
    def search_memories_fts_scored(
        self,
        query: str,
        min_heat: float = 0.1,
        limit: int = 50,
        branch_filter=None,
    ) -> list[tuple[int, float]]:
        """FTS search returning (memory_id, bm25_score) tuples. Higher = better.

        When branch_filter is provided, restricts results to memories whose
        branch is NULL, equals default_branch, or equals current_branch (when
        current_branch is not None).
        """
        from yadgar.storage.branch import _build_branch_clause

        fts_query = self._preprocess_fts_query(query)
        branch_clause, branch_params = _build_branch_clause(branch_filter)
        branch_and = f" AND {branch_clause}" if branch_clause else ""
        params: dict = {"q": fts_query, "min": min_heat, "lim": limit}
        params.update(branch_params)
        rows = self._q(
            f"SELECT id, heat, search::score(1) AS score "
            f"FROM memory WHERE content @1@ $q AND heat >= $min{branch_and} "
            f"ORDER BY score DESC LIMIT $lim",
            params,
        )
        results = []
        for row in rows:
            mid = self._extract_id(row.get("id"))
            score = float(row.get("score", 0.0))
            results.append((mid, score))
        return results

    @trace_span("storage.memory.search_memories_by_content_date")
    def search_memories_by_content_date(
        self,
        date_hints: list[str],
        month_hints: list[str],
        session_hints: list[str],
        min_heat: float = 0.0,
        limit: int = 50,
        branch_filter=None,
    ) -> list[dict]:
        """Search memory content for temporal references using FTS.

        When branch_filter is provided, restricts results to memories whose
        branch is NULL, equals default_branch, or equals current_branch.
        """
        from yadgar.storage.branch import _build_branch_clause

        terms = []
        for hint in date_hints:
            safe = hint.replace('"', "").replace("\\", "")
            if safe:
                terms.append(f'"{safe}"')
        for hint in month_hints:
            terms.append(hint)
        for hint in session_hints:
            terms.append(hint)
        if not terms:
            return []
        fts_query = " OR ".join(terms)
        branch_clause, branch_params = _build_branch_clause(branch_filter)
        where_extra = f" AND {branch_clause}" if branch_clause else ""
        params: dict = {"q": fts_query, "min": min_heat, "lim": limit}
        params.update(branch_params)
        rows = self._q(
            f"SELECT * FROM memory WHERE content @@ $q AND heat >= $min{where_extra} "
            "ORDER BY heat DESC LIMIT $lim",
            params,
        )
        return self._rows_to_dicts(rows)

    @trace_span("storage.memory.search_memories_by_timestamp_range")
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

    @trace_span("storage.memory.search_memories_by_month")
    def search_memories_by_month(
        self,
        month_hints: list[str],
        min_heat: float = 0.0,
        limit: int = 200,
        branch_filter=None,
    ) -> list[int]:
        """Find memory IDs whose created_at falls in the given month(s).

        When branch_filter is provided, restricts candidates to memories whose
        branch is NULL, equals default_branch, or equals current_branch.
        """
        from yadgar.storage.branch import _build_branch_clause

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
        branch_clause, branch_params = _build_branch_clause(branch_filter)
        where_extra = f" AND {branch_clause}" if branch_clause else ""
        params: dict = {"min": min_heat, "lim": limit * 10}
        params.update(branch_params)
        rows = self._q(
            f"SELECT id, created_at FROM memory WHERE heat >= $min{where_extra} LIMIT $lim",
            params,
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

    # ------------------------------------------------------------------ Vector field helpers
    # update_memory_compression stays here: primary table is memory; it updates
    # content + compression_level fields (not a vector-index operation).

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
        sql = (
            "UPDATE type::record('memory', $id) SET "
            "content = $content, embedding = $emb, compression_level = $level, "
            "compressed = true"
        )
        if original_content is not None:
            sql += ", original_content = $orig"
            params["orig"] = original_content
        self._q(sql, params)

    # ------------------------------------------------------------------ Store-type queries

    def get_memories_by_store_type(
        self,
        store_type: str,
        directory: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Return memories for a store type.

        When `limit` is given, returns the most-recently-accessed memories up
        to that count (ORDER BY last_accessed DESC).  Callers that build pairwise
        similarity matrices should pass limit=CLS_PATTERN_MAX_CANDIDATES to avoid
        allocating an unbounded N×N matrix.
        """
        order_clause = " ORDER BY last_accessed DESC" if limit is not None else ""
        limit_clause = " LIMIT $lim" if limit is not None else ""
        if directory:
            params: dict = {"st": store_type, "dir": directory}
            if limit is not None:
                params["lim"] = int(limit)
            rows = self._q(
                f"SELECT * FROM memory WHERE store_type = $st "
                f"AND heat > 0 AND embedding IS NOT NONE "
                f"AND directory_context = $dir{order_clause}{limit_clause}",
                params,
            )
        else:
            params = {"st": store_type}
            if limit is not None:
                params["lim"] = int(limit)
            rows = self._q(
                f"SELECT * FROM memory WHERE store_type = $st "
                f"AND heat > 0 AND embedding IS NOT NONE{order_clause}{limit_clause}",
                params,
            )
        return self._rows_to_dicts(rows)

    # ------------------------------------------------------------------ Generic helpers

    def update_memory_fields(self, memory_id: int, **fields):
        from yadgar.storage.client import _EMBEDDING_FIELDS, _MEMORY_UPDATABLE_FIELDS

        if not fields:
            return
        fields = {k: v for k, v in fields.items() if k in _MEMORY_UPDATABLE_FIELDS}
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
        mid = int(memory_id)  # §5: cast to int to prevent record-ID injection
        self._q(f"UPDATE memory:{mid} SET {', '.join(set_parts)}", params)

    def update_memory_last_accessed(self, memory_id: int, timestamp: str):
        mid = int(memory_id)  # §5: cast to int
        self._q(
            f"UPDATE memory:{mid} SET last_accessed = $ts",
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
            self._q(
                "UPDATE type::record('memory', $id) SET "
                "is_protected = $prot, importance = $imp, contextual_prefix = $prefix",
                {
                    "id": memory_id,
                    "prot": is_protected,
                    "imp": importance,
                    "prefix": contextual_prefix,
                },
            )
        else:
            self._q(
                "UPDATE type::record('memory', $id) SET is_protected = $prot, importance = $imp",
                {"id": memory_id, "prot": is_protected, "imp": importance},
            )

    def get_anchored_memories(self, limit: int = 20) -> list[dict]:
        """Return protected memories tagged with _anchor, ordered by creation date desc.

        v5.8.0: excludes rows where valid_until < now() (expired anchors).
        valid_until is stored as ISO-8601 UTC string; comparison uses string $now param
        to avoid type-mismatch between stored strings and SurrealDB datetime functions.
        """
        rows = self._q(
            "SELECT * FROM memory "
            "WHERE is_protected = true AND heat > 0 AND '_anchor' INSIDE tags "
            "AND (valid_until IS NONE OR valid_until > $now) "
            "ORDER BY created_at DESC LIMIT $lim",
            {"lim": limit, "now": self._now_iso()},
        )
        return self._rows_to_dicts(rows)

    def get_anchored_memories_scoped(
        self,
        directory: str,
        limit: int = 20,
    ) -> list[dict]:
        """Return anchors in scope priority order: global first, then project.

        Two queries, hard cap `limit` each (safety cap 50 per design).
        Global = directory_context IN ('', 'global', 'system').
        Project = directory_context = directory (exact repo root match).
        Deduplicates by memory id. Returns global anchors first, then project.
        No rank-filter applied — anchors surface unconditionally (design §2).

        v5.19.0: replaces flat get_anchored_memories() in restore() path.
        """
        _now = self._now_iso()
        _cap = min(limit, 50)  # hard safety cap

        global_rows = self._q(
            "SELECT * FROM memory "
            "WHERE '_anchor' INSIDE tags AND is_protected = true "
            "AND (directory_context = '' OR directory_context = 'global' "
            "     OR directory_context = 'system') "
            "AND (valid_until IS NONE OR valid_until > $now) "
            "ORDER BY heat DESC LIMIT $lim",
            {"now": _now, "lim": _cap},
        )
        project_rows = self._q(
            "SELECT * FROM memory "
            "WHERE '_anchor' INSIDE tags AND is_protected = true "
            "AND directory_context = $dir "
            "AND (valid_until IS NONE OR valid_until > $now) "
            "ORDER BY heat DESC LIMIT $lim",
            {"dir": directory, "now": _now, "lim": _cap},
        )

        seen: set[int] = set()
        merged: list = []
        for row in global_rows + project_rows:
            mid = self._extract_id(row.get("id"))
            if mid in seen:
                continue
            seen.add(mid)
            merged.append(row)

        return self._rows_to_dicts(merged)

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

    # ------------------------------------------------------------------ Graph prior (v5.54.1)

    def get_memory_graph_priors(self, memory_ids: list[int]) -> dict[int, float]:
        """Bulk-fetch graph_prior scalars for a list of memory IDs (v5.54.1).

        Returns {memory_id: graph_prior} for IDs that have a non-NULL prior.
        Missing or NULL entries are omitted — caller treats absence as 0.0.
        O(1) per candidate: reads a stored field, no traversal.
        """
        if not memory_ids:
            return {}
        # Fetch graph_prior for each candidate memory.
        # Use individual point reads (one per ID) to stay compatible with both
        # SurrealDB embedded (v2) and server (v3) modes.  The candidate set is
        # bounded by the rerank_pool cap (≤50 by default) so N round trips are fine.
        result: dict[int, float] = {}
        for mid in memory_ids:
            rows = self._q(
                "SELECT id, graph_prior FROM type::record('memory', $id) "
                "WHERE graph_prior IS NOT NONE",
                {"id": mid},
            )
            for row in rows:
                gp = row.get("graph_prior")
                if gp is not None:
                    result[mid] = float(gp)
        return result

    def update_memory_graph_prior(self, memory_id: int, prior: float) -> None:
        """Store precomputed graph_prior scalar on a memory row (v5.54.1).

        Called by consolidation._compute_graph_priors — NOT on the request path.
        graph_prior is additive: 0.0 = no boost (same as today); NULL (absent) is
        treated identically to 0.0 by the fusion layer.
        """
        self._q(
            "UPDATE type::record('memory', $id) SET graph_prior = $gp",
            {"id": memory_id, "gp": prior},
        )

    # ------------------------------------------------------------------ Co-recall prior (v5.54.2)

    def get_memory_cofire_priors(self, memory_ids: list[int]) -> dict[int, float]:
        """Bulk-fetch cofire_prior scalars for a list of memory IDs (v5.54.2).

        Returns {memory_id: cofire_prior} for IDs that have a non-NULL prior.
        Missing or NULL entries are omitted — caller treats absence as 0.0.
        O(1) per candidate: reads a stored field, no graph traversal.
        """
        if not memory_ids:
            return {}
        result: dict[int, float] = {}
        for mid in memory_ids:
            rows = self._q(
                "SELECT id, cofire_prior FROM type::record('memory', $id) "
                "WHERE cofire_prior IS NOT NONE",
                {"id": mid},
            )
            for row in rows:
                cp = row.get("cofire_prior")
                if cp is not None:
                    result[mid] = float(cp)
        return result

    def update_memory_cofire_prior(self, memory_id: int, prior: float) -> None:
        """Store precomputed cofire_prior scalar on a memory row (v5.54.2).

        Called by consolidation._compute_cofire_priors — NOT on the request path.
        cofire_prior is additive: 0.0 = no boost (same as today); NULL (absent) is
        treated identically to 0.0 by the fusion layer.
        """
        self._q(
            "UPDATE type::record('memory', $id) SET cofire_prior = $cp",
            {"id": memory_id, "cp": prior},
        )

    # ------------------------------------------------------------------ Memory excitability

    def update_memory_excitability(self, memory_id: int, excitability: float):
        """Update excitability and last_excitability_update for a memory."""
        now = self._now_iso()
        self._q(
            "UPDATE type::record('memory', $id) SET excitability = $exc, "
            "last_excitability_update = $now",
            {"id": memory_id, "exc": excitability, "now": now},
        )
