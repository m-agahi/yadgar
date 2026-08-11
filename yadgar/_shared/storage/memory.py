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
from typing import TYPE_CHECKING

from yadgar._shared.observability.observe import observe
from yadgar._shared.observability.tracing import trace_span
from yadgar._shared.security.secrets import SecretLeakBlocked, check_secrets
from yadgar._shared.storage._project_id_writer import _resolve_project_id_for_write
from yadgar._shared.storage.directory import build_project_scope_clause

_log = logging.getLogger(__name__)

# S1 allowlist for provenance_agent values: ascii alphanumeric, hyphens, underscores.
# Rejects semicolons, quotes, spaces and other SQL-significant characters.
_PROVENANCE_AGENT_RE = _re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@observe(tier="hot")
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


class _MemoryMixin:
    """Memory CRUD and primary-table operations — mixed into StorageEngine."""

    if TYPE_CHECKING:
        # Provided by _ClientMixin, which is composed alongside this mixin into
        # StorageEngine. Declared here (type-check time only, zero runtime effect)
        # so mypy can resolve the ~100 `self._q` / `self._now_iso` call sites in
        # this module instead of reporting them all as attr-defined errors.
        # Signatures mirror client.py exactly so the composed class has no
        # incompatible-base conflict.
        def _q(self, surql: str, params: dict | None = None) -> list: ...

        def _now_iso(self) -> str: ...

    # ------------------------------------------------------------------ Memories

    @observe(tier="hot")
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
            "project_id = $project_id, "
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
            # C5 (0047 PR#40 §5): the v5.46.6 ``or "global"`` normalisation is
            # DELETED. It was written to make an empty directory_context surface
            # in the global anchor bucket, but the bucket it fed is keyed on the
            # ``global`` TAG now (§1.4: reach is a tag, ownership is project_id),
            # and the same expression was the second of two sites minting the
            # sentinel inside one dict literal. An empty directory_context is
            # stored as it arrived; nothing is invented on its behalf.
            "directory_context": memory["directory_context"],
            # Car L (0047 §16.9): project_id alongside directory_context. C5:
            # the caller's value or a raise — there is no classifier seam left
            # to fall back to, and directory_context is passed only so the
            # raise can name the write.
            "project_id": _resolve_project_id_for_write(
                caller_value=memory.get("project_id"),
                directory_context=memory.get("directory_context"),
            ),
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

    @observe(tier="hot")
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
                    from yadgar._shared.observability.metrics import (
                        yadgar_writegate_outcome,  # noqa: PLC0415
                    )

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

    @observe(tier="stage")
    def _enrich_memory_if_enabled(  # noqa: C901 — pipeline flag+length+embedding guards + 6-field mapping; extract further degrades locality without reducing count
        self, mid: int, memory: dict, settings, embeddings_engine, embedding
    ) -> None:
        """Run optional INDEX_ENRICHMENT_ENABLED pipeline, update memory row in-place.

        Guard clauses at the top mean default deployments exit immediately
        (flag=False or missing) with zero overhead — identical to before.
        """
        from yadgar._shared.storage import _get_enrichment_pipeline

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

    @observe(tier="boundary", metric="storage.memory.insert_memory")
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

    @trace_span()
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

    @trace_span()
    def get_memories_by_ids(self, memory_ids: list[int]) -> list[dict]:
        """Bulk-fetch full memory rows for a list of ids in ONE query (v5.97.0).

        Collapses the fusion final-result N+1 — `_build_initial_results` previously
        looped `get_memory(mid)` per fused candidate (52-55 serial round-trips,
        ~1100 ms warm; see docs/plans/recall-warm-profile-2026-07-02.md). This is
        the same batch shape v5.96 used for the priors, but SELECT * (not a scalar
        projection) so the caller gets full hydrated rows.

        Semantics match get_memory exactly, per id:
          - `SELECT *` then `_row_to_dict` (id → int, embedding list → bytes, JSON
            fields parsed, booleans coerced) — so the `embedding` bytes are
            byte-identical to get_memory's, which MMR depends on
            (np.frombuffer(embedding, dtype=float32)).
          - the five nullable fields SurrealDB omits when NONE are setdefault-ed to
            None (embedding_model, file_hash, last_excitability_update,
            original_content, last_reconsolidated).
        Missing ids are simply absent from the result (get_memory would return None
        → the fusion loop skipped them). Order is NOT guaranteed — the caller
        re-orders by fused score. Duplicate input ids collapse to one row.

        Record ids are inlined into the IN list (WHERE id IN [memory:N, ...]) rather
        than bound as a $param — parameterised IN with record-ids is not portable to
        the embedded SurrealKV SDK; inline record-id literals work in both embedded
        and server modes (mirrors get_memory_graph_priors / get_memories_by_ids_minimal).
        int() sanitises each id (mirrors get_memory) so the inlined literal can never
        carry injection.
        """
        if not memory_ids:
            return []
        # de-dup while preserving determinism; the caller re-orders anyway.
        unique_ids = list(dict.fromkeys(int(mid) for mid in memory_ids))

        # Car 2 (backend 5.19.0): the `memory_doc` cache holds ONLY the two
        # immutable KB-scale columns (content + embedding), keyed by memory_id.
        # Everything else — heat, access_count and every consolidation/decay-mutated
        # field — is fetched FRESH here via `SELECT * OMIT content, embedding`, so the
        # returned rows are byte-identical to the old single-`SELECT *` path INCLUDING
        # live heat. Only the KB-scale content/embedding transfer is elided on a
        # cache hit (the ~866 ms warm cost). A `NullCache` (default until wired /
        # kill-switched) makes the cache a no-op: every id misses, one heavy fetch,
        # behaviour identical to pre-Car-2. See yadgar/backend/cache.py
        # get_memory_doc_cache for the invalidation contract (TTL + per-id evict;
        # DELETE inert; monotonic ids).
        cache = self._resolve_memory_doc_cache()

        # 1. FRESH scalars for every requested id (never cached — includes heat).
        id_list = ", ".join(f"memory:{mid}" for mid in unique_ids)
        fresh_rows = self._q(
            f"SELECT * OMIT content, embedding FROM memory WHERE id IN [{id_list}]"
        )
        fresh_by_id = {self._extract_id(r.get("id")): r for r in fresh_rows if r is not None}

        # 2. content+embedding — from cache where present, heavy-fetch the misses.
        heavy_by_id: dict[int, dict] = {}
        miss_ids: list[int] = []
        for mid in unique_ids:
            cached = cache.get(mid)
            if cached is not None:
                heavy_by_id[mid] = cached
            else:
                miss_ids.append(mid)
        if miss_ids:
            miss_list = ", ".join(f"memory:{mid}" for mid in miss_ids)
            heavy_rows = self._q(
                f"SELECT id, content, embedding FROM memory WHERE id IN [{miss_list}]"
            )
            for r in heavy_rows:
                if r is None:
                    continue
                hid = self._extract_id(r.get("id"))
                heavy = {"content": r.get("content"), "embedding": r.get("embedding")}
                heavy_by_id[hid] = heavy
                cache.put(hid, heavy)

        # 3. merge raw fresh + raw heavy per id, then normalise ONCE — so the cached
        #    and uncached paths run the identical `_rows_to_dicts` transform (same
        #    id→int, embedding list→bytes, JSON/bool coercion). Missing ids are simply
        #    absent (a fused candidate deleted between fusion + hydration → skipped by
        #    the caller, exactly as the old per-id get_memory loop did).
        merged_raw: list[dict] = []
        for mid in unique_ids:
            fresh = fresh_by_id.get(mid)
            if fresh is None:
                continue  # deleted / missing — not a candidate
            row = dict(fresh)
            heavy = heavy_by_id.get(mid)
            if heavy is not None:
                row["content"] = heavy.get("content")
                row["embedding"] = heavy.get("embedding")
            merged_raw.append(row)

        results = self._rows_to_dicts(merged_raw)
        for result in results:
            # SurrealDB omits fields set to NONE; add expected nullable defaults
            # (identical to get_memory).
            result.setdefault("embedding_model", None)
            result.setdefault("file_hash", None)
            result.setdefault("last_excitability_update", None)
            result.setdefault("original_content", None)
            result.setdefault("last_reconsolidated", None)
        return results

    @observe(tier="hot", metric="storage.memory.resolve_memory_doc_cache")
    def _resolve_memory_doc_cache(self):
        """Return the injected ``memory_doc`` cache, or the process-global default.

        Constructor-DI seam (mirrors Reranker's ``_ce_cache``): a ``StorageEngine``
        may set ``self._memory_doc_cache`` to a ``NullCache`` (disable) or a test
        double; otherwise the shared registered instance is used. Import is deferred
        to keep the storage import graph free of the FastAPI embed_service module.
        """
        cache = getattr(self, "_memory_doc_cache", None)
        if cache is not None:
            return cache
        # Car 2 (folder-split #17): the lazy `backend.cache.get_memory_doc_cache`
        # fallback is deleted (it was a _shared→backend edge). The composition root
        # (lifecycle.init_engines) injects the REAL registered instance; the bare
        # default is a _shared NullCache (all-miss ≡ today's single-query fetch).
        from yadgar._shared.contracts.protocols import NullCache  # noqa: PLC0415

        cache = NullCache()
        self._memory_doc_cache = cache
        return cache

    @trace_span()
    def get_memories_by_heat(self, min_heat: float, limit: int = 100) -> list[dict]:
        rows = self._q(
            "SELECT * FROM memory WHERE heat >= $min ORDER BY heat DESC LIMIT $lim",
            {"min": min_heat, "lim": limit},
        )
        return self._rows_to_dicts(rows)

    @observe(tier="stage", metric="storage.memory.update_memory_heat")
    def update_memory_heat(self, memory_id: int, new_heat: float):
        self._q(
            "UPDATE type::record('memory', $id) SET heat = $heat",
            {"id": memory_id, "heat": new_heat},
        )

    @observe(tier="stage", metric="storage.memory.update_memory_staleness")
    def update_memory_staleness(self, memory_id: int, is_stale: bool):
        self._q(
            "UPDATE type::record('memory', $id) SET is_stale = $stale",
            {"id": memory_id, "stale": is_stale},
        )

    @observe(tier="stage", metric="storage.memory.delete_memory")
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
            # Car 4: bump both endpoints of every edge about to be removed so the
            # graph adjacency cache doesn't serve the pruned edges (pure-structural
            # read, no fresh recheck). Resolve endpoints BEFORE the bulk delete.
            try:
                doomed = self._q(
                    "SELECT source_entity_id, target_entity_id FROM relationship "
                    "WHERE source_entity_id = $eid OR target_entity_id = $eid",
                    {"eid": eid},
                )
                bump_ids: set[int] = {eid}
                for r in doomed:
                    bump_ids.add(r["source_entity_id"])
                    bump_ids.add(r["target_entity_id"])
                for bid in bump_ids:
                    self._bump_entity_version(bid)
            except Exception:  # noqa: BLE001 — bump must never fail the delete
                _log.debug("graph-cache bump on memory %s delete failed", memory_id, exc_info=True)
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

    def get_all_memories_for_decay_scalar(self) -> list[dict]:
        """Return scalar-only projection for heat decay computation (C2).

        Identical WHERE clause as get_all_memories_for_decay but excludes the
        large `content` and `embedding` columns that the decay math never reads.

        Fields returned — the complete set that _decay_memories + compute_decay
        access:
            id, heat, is_protected, last_accessed, last_decay_at,
            access_count_since_decay, tags,
            importance, emotional_valence, confidence

        The original get_all_memories_for_decay() is kept unchanged for the 4
        other callers that need content/cluster_id/compressed/etc.

        None-stripping: explicit projection causes SurrealDB to return None for
        unset optional columns (a full-row scan omits them entirely).  We strip
        None values from each row so that callers' .get(field, default)
        fallbacks work identically — the key must be absent, not
        present-with-None, for .get() defaults to fire.
        """
        rows = self._q(
            "SELECT meta::id(id) AS id, heat, is_protected, last_accessed, last_decay_at, "
            "access_count_since_decay, tags, importance, emotional_valence, confidence "
            "FROM memory WHERE heat > 0 AND (is_protected = false OR is_protected = NONE)"
        )
        dicts = self._rows_to_dicts(rows)
        # Strip None-valued keys so .get(field, default) in decay math fires correctly.
        return [{k: v for k, v in d.items() if v is not None} for d in dicts if d is not None]

    def get_all_memories_with_embeddings(self) -> list[dict]:
        rows = self._q("SELECT * FROM memory WHERE embedding IS NOT NONE AND heat > 0")
        return self._rows_to_dicts(rows)

    # ------------------------------------------------------------------
    # C1 projected helpers — avoid SELECT * for bulk embedding/heat scans.
    # These return only the columns needed; the full-row shims above are
    # kept for callers that need content/metadata (see deferred list below).
    #
    # Deferred callers (all need extra fields beyond id+embedding or id+heat):
    #   get_all_memories_with_embeddings:
    #     dream.py:23         — MIGRATED to C3 two-phase fetch (get_candidate_memory_ids
    #                           + get_memories_by_ids_projected)
    #     community.py:179    — _build_cluster_summary/_compute_centroid need content+cluster_id
    #   get_all_memories_for_decay:
    #     heat_decay.py:94    — MIGRATED to get_all_memories_for_decay_scalar() (C2)
    #     community.py:162    — _find_memories_for_entities needs content
    #     community.py:231    — _create_root_clusters needs cluster_id, directory_context
    #     embed_compress.py:56 — needs created_at, content, compressed
    #     gap_detection.py:20  — needs heat, tags, confidence, content, id
    # ------------------------------------------------------------------

    @observe(tier="stage")
    def get_candidate_memory_ids(self) -> list[int]:
        """Return all memory IDs that have an embedding and heat > 0.

        Projects only id — avoids fetching content/metadata/embedding.
        Used by C3 two-phase fetch pattern: sample IDs first (cheap), then
        fetch only the sampled rows via get_memories_by_ids_projected().
        Filter matches get_all_memories_with_embeddings exactly so population
        set and size are identical (preserves uniform random-pair semantics).
        """
        rows = self._q(
            "SELECT meta::id(id) AS id FROM memory WHERE embedding IS NOT NONE AND heat > 0"
        )
        result: list[int] = []
        for row in rows:
            if row is None:
                continue
            mem_id = self._extract_id(row.get("id"))
            if mem_id is not None:
                result.append(mem_id)
        return result

    @observe(tier="stage")
    def get_memories_by_ids_projected(self, ids: list[int]) -> list[dict]:
        """Return id, embedding, content and project_id for a list of memory ids.

        Projected (not SELECT *) — returns only the fields dream_replay needs:
          - id         → _build_connected_pair_index_by_ids, _ensure_memory_entity
          - embedding  → similarity check
          - content    → _create_dream_insight
          - project_id → _create_dream_insight's resolve_project_id_from_rows
        Rows pass through _rows_to_dicts so id is a bare int and embedding is
        bytes — identical to the row format produced by get_all_memories_with_embeddings.
        Used by C3 two-phase fetch: phase-2 fetch for only the ~40 sampled ids.

        **C13: ``project_id`` joins the projection because C4 gave this fetch's
        sole consumer a fourth need and the SELECT was not widened with it.**
        ``_create_dream_insight`` (``sleep_compute/dream.py``) resolves the
        insight's owner from the PAIR's own rows — inheritance, the sanctioned
        substitute for the derivation ADR-0227 deleted. A projection that omits
        the column makes ``resolve_project_id_from_rows`` see two rows naming no
        project, so it returns ``None`` and every single dream insight is
        skipped and counted. The rows carry the value in the DB; only this
        SELECT lost it. The failure is invisible in ``dream_replay``'s stats,
        which count candidate pairs rather than committed writes — a projected
        column is exactly the kind of dependency that goes stale silently, so
        the list above is a contract, not a comment.

        Note: inlines record ids directly into the query (WHERE id IN [memory:N, ...])
        because parameterised IN with string values is not supported by the embedded
        SurrealKV SDK (it expects native RecordID objects which the Python SDK does not
        expose as a public parameter type in embedded mode).
        """
        if not ids:
            return []
        id_list = ", ".join(f"memory:{i}" for i in ids)
        rows = self._q(
            f"SELECT meta::id(id) AS id, embedding, content, project_id "
            f"FROM memory WHERE id IN [{id_list}]"
        )
        return self._rows_to_dicts(rows)

    @observe(tier="stage")
    def iter_embeddings_minimal(self) -> list[tuple[int, bytes]]:
        """Return (id, embedding) for every memory that has an embedding and heat > 0.

        Projects only id and embedding — avoids fetching content/metadata columns.
        Use instead of get_all_memories_with_embeddings() when only the vector is needed.
        """
        rows = self._q(
            "SELECT meta::id(id) AS id, embedding FROM memory "
            "WHERE embedding IS NOT NONE AND heat > 0"
        )
        result: list[tuple[int, bytes]] = []
        for row in rows:
            if row is None:
                continue
            emb = row.get("embedding")
            if emb is None:
                continue
            mem_id = self._extract_id(row.get("id"))
            if mem_id is None:
                continue
            if isinstance(emb, list):
                emb = self._floats_to_bytes(emb)
            result.append((mem_id, emb))
        return result

    @observe(tier="stage")
    def get_embeddings_by_ids(self, ids: list[int]) -> list[tuple[int, bytes]]:
        """Return (id, embedding) for a given list of memory ids.

        Projects only id and embedding. Used by C3 two-phase fetch pattern
        (sample candidate IDs first, then fetch only their vectors).
        """
        if not ids:
            return []
        rows = self._q(
            "SELECT meta::id(id) AS id, embedding FROM memory WHERE id IN $ids",
            {"ids": [f"memory:{i}" for i in ids]},
        )
        result: list[tuple[int, bytes]] = []
        for row in rows:
            if row is None:
                continue
            emb = row.get("embedding")
            if emb is None:
                continue
            mem_id = self._extract_id(row.get("id"))
            if mem_id is None:
                continue
            if isinstance(emb, list):
                emb = self._floats_to_bytes(emb)
            result.append((mem_id, emb))
        return result

    @observe(tier="stage")
    def get_ids_with_heat(self) -> list[tuple[int, float]]:
        """Return (id, heat) for every memory with heat > 0.

        Projects only id and heat — avoids fetching content/metadata columns.
        Use instead of get_all_memories_for_decay() when only heat values are needed.
        """
        rows = self._q("SELECT meta::id(id) AS id, heat FROM memory WHERE heat > 0")
        result: list[tuple[int, float]] = []
        for row in rows:
            if row is None:
                continue
            mem_id = self._extract_id(row.get("id"))
            if mem_id is None:
                continue
            result.append((mem_id, float(row.get("heat", 0.0))))
        return result

    @observe(tier="stage")
    def get_memories_with_embeddings(
        self,
        limit: int | None = None,
        order_by: str = "last_accessed",
        since: str | None = None,
    ) -> list[dict]:
        """Return memories that have embeddings, ordered by `order_by` DESC.

        When `limit` is None behaves identically to get_all_memories_with_embeddings.
        Intended for callers that build an N×N similarity matrix and need to bound N.

        When `since` (an ISO-8601 timestamp) is provided, only memories whose
        `created_at >= since` are returned. v5.86 (OT-C4) uses this to fetch the
        incremental "probe" set of recently-created memories.
        """
        allowed_order = {"last_accessed", "heat", "created_at"}
        if order_by not in allowed_order:
            order_by = "last_accessed"
        where = "embedding IS NOT NONE AND heat > 0"
        params: dict = {}
        if since is not None:
            where += " AND created_at >= $since"
            params["since"] = since
        if limit is None:
            rows = self._q(f"SELECT * FROM memory WHERE {where} ORDER BY {order_by} DESC", params)
        else:
            params["lim"] = int(limit)
            rows = self._q(
                f"SELECT * FROM memory WHERE {where} ORDER BY {order_by} DESC LIMIT $lim",
                params,
            )
        return self._rows_to_dicts(rows)

    def get_memories_without_embeddings(self) -> list[dict]:
        rows = self._q("SELECT * FROM memory WHERE embedding IS NONE AND heat > 0")
        return self._rows_to_dicts(rows)

    @trace_span()
    def search_memories_fts(self, query: str, min_heat: float = 0.1, limit: int = 5) -> list[dict]:
        fts_query = self._preprocess_fts_query(query)
        rows = self._q(
            "SELECT * FROM memory WHERE content @@ $q AND heat >= $min "
            "ORDER BY heat DESC LIMIT $lim",
            {"q": fts_query, "min": min_heat, "lim": limit},
        )
        return self._rows_to_dicts(rows)

    @trace_span()
    def search_memories_fts_scored(
        self,
        query: str,
        min_heat: float = 0.1,
        limit: int = 50,
        *,
        scope_sql: str = "",
        scope_params: dict | None = None,
    ) -> list[tuple[int, float]]:
        """FTS search returning (memory_id, bm25_score) tuples. Higher = better.

        Car C7: ``scope_sql`` carries the project predicate + ``global`` reach
        tag. The FTS arm composes safely — ``LIMIT`` is applied AFTER the
        ``WHERE``, so the limit is spent on in-scope rows.
        """
        fts_query = self._preprocess_fts_query(query)
        params: dict = {"q": fts_query, "min": min_heat, "lim": limit}
        where = "content @1@ $q AND heat >= $min"
        if scope_sql:
            where = f"{where} AND ({scope_sql})"
            params.update(scope_params or {})
        rows = self._q(
            f"SELECT id, heat, search::score(1) AS score "
            f"FROM memory WHERE {where} "
            f"ORDER BY score DESC LIMIT $lim",
            params,
        )
        results = []
        for row in rows:
            mid = self._extract_id(row.get("id"))
            score = float(row.get("score", 0.0))
            results.append((mid, score))
        return results

    @trace_span()
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
        params: dict = {"q": fts_query, "min": min_heat, "lim": limit}
        rows = self._q(
            "SELECT * FROM memory WHERE content @@ $q AND heat >= $min "
            "ORDER BY heat DESC LIMIT $lim",
            params,
        )
        return self._rows_to_dicts(rows)

    @trace_span()
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

    @trace_span()
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
        params: dict = {"min": min_heat, "lim": limit * 10}
        rows = self._q(
            "SELECT id, created_at FROM memory WHERE heat >= $min LIMIT $lim",
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

    @observe(tier="stage", metric="storage.memory.update_memory_compression")
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

    @observe(tier="stage")
    def get_memories_by_store_type(
        self,
        store_type: str,
        project_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Return memories for a store type, optionally scoped to one project.

        When `limit` is given, returns the most-recently-accessed memories up
        to that count (ORDER BY last_accessed DESC).  Callers that build pairwise
        similarity matrices should pass limit=CLS_PATTERN_MAX_CANDIDATES to avoid
        allocating an unbounded N×N matrix.

        C9c (0047 §5): ``directory`` renamed to ``project_id`` AND the predicate
        re-keyed off ``directory_context`` in the SAME change, per ADR-0225.
        Doing only the rename would ship a caller-facing lie — the caller passes
        ``owner/repo`` into ``WHERE directory_context = $dir``, matches zero rows,
        and nothing raises. ``memory`` carries ``project_id`` (migration 031), so
        there is a column to re-key ONTO here; the C11 tables have none yet.

        The predicate is ``build_project_scope_clause`` rather than a hand-rolled
        ``project_id = $pid`` so this arm agrees BY CONSTRUCTION with the Car C7
        retrieval arm — same two arms (project match OR the global reach tag),
        same treatment of unstamped rows. Unstamped rows (``project_id`` IS NONE,
        i.e. everything the C6 operator backfill has not reached) DELIBERATELY do
        not match: admitting them would rebuild the permissive fallback ADR-0227
        exists to delete. Zero results over an un-backfilled corpus is the
        sanctioned cost of that window, recorded in the plan's §8 step 5b runbook.
        """
        order_clause = " ORDER BY last_accessed DESC" if limit is not None else ""
        limit_clause = " LIMIT $lim" if limit is not None else ""
        # Empty/None project_id yields ("", {}) — an unscoped, corpus-wide read.
        scope_clause, scope_params = build_project_scope_clause(project_id)
        scope_sql = f" AND {scope_clause}" if scope_clause else ""
        params: dict = {"st": store_type, **scope_params}
        if limit is not None:
            params["lim"] = int(limit)
        rows = self._q(
            f"SELECT * FROM memory WHERE store_type = $st "
            f"AND heat > 0 AND embedding IS NOT NONE"
            f"{scope_sql}{order_clause}{limit_clause}",
            params,
        )
        return self._rows_to_dicts(rows)

    # ------------------------------------------------------------------ Generic helpers

    @observe(tier="stage", metric="storage.memory.update_memory_fields")
    def update_memory_fields(self, memory_id: int, **fields):
        from yadgar._shared.storage.client import _EMBEDDING_FIELDS, _MEMORY_UPDATABLE_FIELDS

        if not fields:
            return
        fields = {k: v for k, v in fields.items() if k in _MEMORY_UPDATABLE_FIELDS}
        if not fields:
            return
        converted = {}
        for k, v in fields.items():
            if k in _EMBEDDING_FIELDS and isinstance(v, bytes):
                converted[k] = self._bytes_to_floats(v)
            elif k in ("is_protected", "is_stale", "is_prospective", "compressed", "would_reject"):
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
        # Car 2 (backend 5.19.0): the memory_doc cache holds content+embedding by id.
        # A content or embedding edit makes any cached entry stale → evict this id so
        # the next recall re-fetches it (the interactive per-id backstop that
        # complements the TTL). Other field updates (heat, is_stale, tags, …) are not
        # cached, so they need no bust.
        if "content" in converted or any(k in _EMBEDDING_FIELDS for k in converted):
            try:
                self._resolve_memory_doc_cache().invalidate(mid)
            except Exception:  # noqa: BLE001 — cache bust must never fail a write
                _log.debug("memory_doc cache invalidate failed for %s", mid, exc_info=True)

    @observe(tier="stage", metric="storage.memory.clear_memory_valid_until")
    def clear_memory_valid_until(self, memory_id: int) -> None:
        """Clear ``valid_until`` back to NONE (no expiry).

        Cannot go through ``update_memory_fields``: the field is
        ``option<string>``, so a Python ``None`` serialises to JSON null and
        SurrealDB rejects it outright —
        ``Couldn't coerce value for field `valid_until`: Expected `none | string`
        but found `NULL```.  Only the bare ``NONE`` literal sets it.

        The distinction is load-bearing rather than cosmetic: every anchor
        surfacing query tests ``valid_until IS NONE OR valid_until > $now``, and a
        stored NULL reports ``IS NONE`` as false while ``NULL > $now`` is also
        false — so a null-cleared row would silently stop surfacing instead of
        becoming immortal.
        """
        mid = int(memory_id)  # §5: cast to int to prevent record-ID injection
        self._q(f"UPDATE memory:{mid} SET valid_until = NONE")

    @observe(tier="stage", metric="storage.memory.update_memory_last_accessed")
    def update_memory_last_accessed(self, memory_id: int, timestamp: str):
        mid = int(memory_id)  # §5: cast to int
        self._q(
            f"UPDATE memory:{mid} SET last_accessed = $ts",
            {"ts": timestamp},
        )

    @observe(tier="stage", metric="storage.memory.boost_memories_access")
    def boost_memories_access(self, memory_ids: list[int], timestamp: str) -> None:
        """Bump heat (+0.1, clamped at 1.0) and last_accessed for a set of memories.

        v5.102: single batched UPDATE replacing the per-memory
        ``update_memory_heat`` + ``update_memory_last_accessed`` pair that
        ``_apply_recall_side_effects`` used to fire in a loop — 2 sequential
        SurrealDB round-trips × N results on the recall hot path (the ~407ms
        tail of the recall trace). One ``WHERE id IN [...]`` query instead.

        RESULT-PRESERVING: the new heat is computed in-DB as
        ``math::min([heat + 0.1, 1.0])`` — byte-identical to the Python
        ``min(m["heat"] + 0.1, 1.0)`` the caller applies to the returned dicts.
        Speed only, no value/behaviour change.

        Empty ``memory_ids`` is a no-op (guards against an empty ``IN []`` clause).
        """
        if not memory_ids:
            return
        id_list = ", ".join(f"memory:{int(mid)}" for mid in memory_ids)
        self._q(
            f"UPDATE memory SET heat = math::min([heat + 0.1, 1.0]), "
            f"last_accessed = $ts WHERE id IN [{id_list}]",
            {"ts": timestamp},
        )

    def count_memories_by_store_type(self, store_type: str) -> int:
        rows = self._q(
            "SELECT count() AS c FROM memory WHERE store_type = $st AND heat > 0 GROUP ALL",
            {"st": store_type},
        )
        return int(rows[0]["c"]) if rows else 0

    @observe(tier="stage")
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

    @observe(tier="stage")
    def find_memory_ids_by_entities(self, entity_names: list[str]) -> dict[str, list[int]]:
        """Batched ``find_memory_ids_by_entity_name`` — one round-trip for N names (v5.102.0).

        Runs one ``SELECT id FROM memory WHERE content @@ $q AND heat > 0`` per name
        as a MULTI-STATEMENT read (``_q_multi``), so per-name attribution is preserved
        (a naive ``content @@ $a OR content @@ $b`` would return the union and lose
        which memory matched which name). Returns a ``{name: [memory_id, ...]}`` map;
        per-name result order is byte-identical to the per-name query (same statement,
        same ordering), so callers relying on FTS order see no change.

        EXACT-PARITY with the per-name loop:
          - Duplicate names are de-duplicated for the query but every input name is
            present in the returned map (dupes share the same list).
          - On ANY batch failure the whole call degrades to the per-name loop, which
            replays ``find_memory_ids_by_entity_name`` verbatim — including its own
            FTS→``string::contains`` fallback. So a missing FTS index (embedded test
            mode) or a transport hiccup yields IDENTICAL results to N serial calls.
        """
        if not entity_names:
            return {}
        unique_names = list(dict.fromkeys(entity_names))
        try:
            statements = [
                ("SELECT id FROM memory WHERE content @@ $q AND heat > 0", {"q": name})
                for name in unique_names
            ]
            per_stmt = self._q_multi(statements)
            result = {
                name: [self._extract_id(r.get("id")) for r in rows]
                for name, rows in zip(unique_names, per_stmt, strict=True)
            }
        except Exception:
            # Degrade to the exact per-name path (preserves the FTS→contains fallback).
            result = {name: self.find_memory_ids_by_entity_name(name) for name in unique_names}
        # Ensure every original name (including duplicates) maps to its list.
        return {name: result[name] for name in entity_names}

    # ------------------------------------------------------------------ Memory protection and anchoring

    @observe(tier="stage")
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

    @observe(tier="stage")
    def get_anchored_memories_scoped(
        self,
        directory: str,
        limit: int = 20,
    ) -> list[dict]:
        """Return anchors in scope priority order: global-reach first, then project.

        Two queries, hard cap `limit` each (safety cap 50 per design).
        Global = ``'global' IN tags`` (**C5**).
        Project = directory_context = directory (exact repo root match).
        v5.65: 'system' removed from global bucket (mis-stamp sink; v5.64 stopped new writes).
        Deduplicates by memory id. Returns global anchors first, then project.
        No rank-filter applied — anchors surface unconditionally (design §2).

        v5.19.0: replaces flat get_anchored_memories() in restore() path.

        **C5 (0047 PR#40 §5) re-keyed the global bucket from
        ``directory_context IN ('', 'global')`` to the ``global`` TAG.** §1.4
        splits ownership from reach: ``project_id`` is always a real registered
        project and ``"global"`` is never one, so a *reader* keyed on
        ``directory_context = 'global'`` reads a concept that no longer exists
        on the write side — C5 deleted every site that minted it.

        **Known, quantified consequence — this bucket is NARROW until C6.** The
        plan measured the live corpus (§1.5 D2, 2026-08-10): **7** memory rows
        already carry a ``global`` tag against **~349** stamped
        ``directory_context = 'global'``. Until C6's operator-invoked backfill
        re-keys those 349 rows to a real owner + the reach tag, this query
        returns the 7, not the 349. That is deliberate — surfacing rows through
        a predicate the write path can no longer produce is how the sentinel
        stayed alive — but it is a C6 dependency, not a free rename.
        """
        _now = self._now_iso()
        _cap = min(limit, 50)  # hard safety cap

        global_rows = self._q(
            "SELECT * FROM memory "
            "WHERE '_anchor' INSIDE tags AND is_protected = true "
            "AND 'global' INSIDE tags "
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

    @observe(tier="stage")
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

    @observe(tier="stage")
    def get_recent_memories_since(
        self,
        since: str,
        limit: int = 10,
        directory: str | None = None,
    ) -> list[dict]:
        """Return memories created since the given ISO-8601 cutoff, newest first.

        Args:
            since: ISO-8601 UTC datetime string (cutoff; memories after this are returned).
            limit: max rows (caller is responsible for capping; default 10).
            directory: restrict to this directory_context. None or 'global' = all directories.
        """
        params: dict = {"lim": limit, "since": since}
        if directory and directory != "global":
            rows = self._q(
                "SELECT id, content, created_at, tags, store_type, heat, is_protected, "
                "directory_context FROM memory "
                "WHERE heat > 0 AND created_at >= $since AND directory_context = $dir "
                "ORDER BY created_at DESC LIMIT $lim",
                {**params, "dir": directory},
            )
        else:
            rows = self._q(
                "SELECT id, content, created_at, tags, store_type, heat, is_protected, "
                "directory_context FROM memory "
                "WHERE heat > 0 AND created_at >= $since "
                "ORDER BY created_at DESC LIMIT $lim",
                params,
            )
        return self._rows_to_dicts(rows)

    # ------------------------------------------------------------------ Graph prior (v5.54.1)

    @trace_span()
    def get_memory_graph_priors(self, memory_ids: list[int]) -> dict[int, float]:
        """Bulk-fetch graph_prior scalars for a list of memory IDs (v5.54.1).

        Returns {memory_id: graph_prior} for IDs that have a non-NULL prior.
        Missing or NULL entries are omitted — caller treats absence as 0.0.
        O(1) per candidate: reads a stored field, no traversal.
        """
        if not memory_ids:
            return {}
        # v5.96.0: single batched read instead of one point-read per id (N+1 fix).
        # graph_prior is a precomputed scalar on the row (consolidation writes it via
        # update_memory_graph_prior) — so this is a pure fetch, and a single
        # `WHERE id IN [...]` collapses the N round trips into one query.
        #
        # Record ids are inlined into the IN list (WHERE id IN [memory:N, ...]) rather
        # than bound as a $param, mirroring get_memories_by_ids_minimal (~line 485):
        # parameterised IN with string record-ids is not portable to the embedded
        # SurrealKV SDK, whereas inline record-id literals work in both embedded and
        # server modes (validated on 3.1.5).  int() sanitises the id (mirrors
        # get_memory) so the inlined literal can never carry injection.
        # meta::id(id) projects the id back to a bare int, so no _extract_id needed.
        id_list = ", ".join(f"memory:{int(mid)}" for mid in memory_ids)
        rows = self._q(
            f"SELECT meta::id(id) AS id, graph_prior FROM memory "
            f"WHERE id IN [{id_list}] AND graph_prior IS NOT NONE"
        )
        result: dict[int, float] = {}
        for row in rows:
            gp = row.get("graph_prior")
            if gp is None:
                continue
            mid = row.get("id")
            if mid is None:
                continue
            result[int(mid)] = float(gp)
        return result

    @observe(tier="stage", metric="storage.memory.update_memory_graph_prior")
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

    @trace_span()
    def get_memory_cofire_priors(self, memory_ids: list[int]) -> dict[int, float]:
        """Bulk-fetch cofire_prior scalars for a list of memory IDs (v5.54.2).

        Returns {memory_id: cofire_prior} for IDs that have a non-NULL prior.
        Missing or NULL entries are omitted — caller treats absence as 0.0.
        O(1) per candidate: reads a stored field, no graph traversal.
        """
        if not memory_ids:
            return {}
        # v5.96.0: single batched read instead of one point-read per id (N+1 fix).
        # See get_memory_graph_priors for the full rationale — cofire_prior is the
        # same precomputed-scalar fetch pattern (consolidation writes it via
        # update_memory_cofire_prior), collapsed into one `WHERE id IN [...]` query.
        id_list = ", ".join(f"memory:{int(mid)}" for mid in memory_ids)
        rows = self._q(
            f"SELECT meta::id(id) AS id, cofire_prior FROM memory "
            f"WHERE id IN [{id_list}] AND cofire_prior IS NOT NONE"
        )
        result: dict[int, float] = {}
        for row in rows:
            cp = row.get("cofire_prior")
            if cp is None:
                continue
            mid = row.get("id")
            if mid is None:
                continue
            result[int(mid)] = float(cp)
        return result

    @observe(tier="stage", metric="storage.memory.update_memory_cofire_prior")
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

    @observe(tier="stage", metric="storage.memory.update_memory_excitability")
    def update_memory_excitability(self, memory_id: int, excitability: float):
        """Update excitability and last_excitability_update for a memory."""
        now = self._now_iso()
        self._q(
            "UPDATE type::record('memory', $id) SET excitability = $exc, "
            "last_excitability_update = $now",
            {"id": memory_id, "exc": excitability, "now": now},
        )
