"""CLS consolidation mixin — episode processing, duplicate merging, similarity linking."""

import logging
import re
from itertools import combinations

from yadgar._shared.observability.observe import observe

logger = logging.getLogger("yadgar.consolidation")


def _batch_entity_degree(graph: object, all_entities: list[dict]) -> dict:
    """Return {entity_id: total_weight_sum} via ONE batched adjacency query (N+1 fix).

    Replaces the per-entity ``_get_adjacent`` loop in ``_compute_graph_priors``.
    The degree computation only reads ``n["weight"]``, never a neighbour name, so
    the name-free batch (``with_names`` defaults False) is a drop-in.
    """
    entity_ids = [ent["id"] for ent in all_entities]
    adjacency = graph._get_adjacent_batch(entity_ids, None)
    return {eid: sum(n["weight"] for n in adjacency.get(eid, [])) for eid in entity_ids}


def _bump_cache_epoch_global(updated: int) -> None:
    """Bump the global cache-invalidation epoch after a consolidation prior recompute.

    Consolidation prior recompute changes prior scalars across all directories →
    bump the global epoch so Car 1 (project_brief) and Car 2 (wiki/prelude) caches
    for ALL directories are invalidated.  No-op when nothing was updated.
    NOT the removed recall-output shadow counter (killed ADR-0071).

    Fully guarded — must never break the consolidation cycle.
    """
    if not updated:
        return
    try:
        from yadgar._shared.runtime.cache_epoch import bump_epoch  # noqa: PLC0415

        bump_epoch(None)  # None → the shared "global" generation
    except Exception:  # pragma: no cover  # noqa: BLE001 — bump_epoch reaches the on-disk epoch store, whose failures share no common base, and cache instrumentation must never break the consolidation cycle
        pass


# Regex patterns for entity extraction
_FILE_PATH_RE = re.compile(r"(?:\.{0,2}/)?(?:[\w@.-]+/)+[\w@.-]+\.\w+")
_PYTHON_DEF_RE = re.compile(r"\b(def|class)\s+(\w+)")
_JS_FUNCTION_RE = re.compile(r"\bfunction\s+(\w+)")
_ERROR_RE = re.compile(r"\b(\w*(?:Error|Exception))\b")
_TRACEBACK_RE = re.compile(r"Traceback \(most recent call last\)")
_IMPORT_RE = re.compile(r"(?:^|\n)\s*import\s+([\w.]+)")
_FROM_IMPORT_RE = re.compile(r"(?:^|\n)\s*from\s+([\w.]+)\s+import")
_REQUIRE_RE = re.compile(r"require\(['\"]([^'\"]+)['\"]\)")
_DECISION_RE = re.compile(
    r"(?:decided|chose|choosing|using|switched to|migrated to|replaced with)"
    r"\s+(\w+(?:\s+\w+){0,3})",
    re.IGNORECASE,
)

_CODE_EXTENSIONS = frozenset(
    (
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".go",
        ".rs",
        ".java",
        ".c",
        ".h",
        ".cpp",
        ".rb",
        ".toml",
        ".yaml",
        ".yml",
        ".json",
        ".md",
        ".txt",
        ".cfg",
        ".ini",
        ".sh",
        ".css",
        ".html",
        ".sql",
        ".proto",
    )
)


class _CLSMixin:
    """Episode processing, duplicate merging, and semantic similarity linking."""

    # ── _process_new_episodes helpers ─────────────────────────────────────────

    @staticmethod
    def _build_entity_map(
        typed_entities: list,
        legacy_entities: list[tuple[str, str]],
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Merge typed + legacy entity extractions into (entity_map, rel_contexts)."""
        entity_map: dict[str, str] = {}  # name -> type
        rel_contexts: dict[str, str] = {}  # name -> relationship context
        for name, etype, ctx in typed_entities:
            entity_map[name] = etype
            if ctx:
                rel_contexts[name] = ctx
        for name, etype in legacy_entities:
            if name not in entity_map:
                entity_map[name] = etype
        return entity_map, rel_contexts

    def _upsert_entities(self, entity_map: dict[str, str]) -> tuple[list[int], list[str]]:
        """Get-or-create entities; return (entity_ids, entity_names)."""
        entity_ids: list[int] = []
        entity_names: list[str] = []
        for name, etype in entity_map.items():
            existing = self._storage.get_entity_by_name(name)
            if existing:
                self._storage.reinforce_entity(existing["id"])
                entity_ids.append(existing["id"])
            else:
                eid = self._storage.insert_entity({"name": name, "type": etype})
                entity_ids.append(eid)
            entity_names.append(name)
        return entity_ids, entity_names

    def _build_cooccurrence_batch(self, entity_ids: list[int]) -> list[tuple[str, dict | None]]:
        """Build batched SQL writes for co-occurrence relationships (bulk, O(1) round trips)."""
        existing_rels = self._storage.get_relationships_among_entities(entity_ids)
        rel_index: dict[tuple[int, int], dict] = {
            (
                min(r["source_entity_id"], r["target_entity_id"]),
                max(r["source_entity_id"], r["target_entity_id"]),
            ): r
            for r in existing_rels
        }
        to_reinforce: list[int] = []
        to_insert: list[tuple[int, int]] = []
        for id_a, id_b in combinations(entity_ids, 2):
            key = (min(id_a, id_b), max(id_a, id_b))
            rel = rel_index.get(key)
            if rel:
                to_reinforce.append(rel["id"])
            else:
                to_insert.append((id_a, id_b))

        now = self._storage._now_iso()
        batch: list[tuple[str, dict | None]] = []
        for rid in to_reinforce:
            batch.append(
                (
                    "UPDATE type::record('relationship', $id) SET "
                    "weight = weight + $inc, last_reinforced = $now",
                    {"id": rid, "inc": 1.0, "now": now},
                )
            )
        if to_insert:
            new_ids = self._storage._reserve_ids("relationship", len(to_insert))
            for (id_a, id_b), rid in zip(to_insert, new_ids, strict=True):
                batch.append(
                    (
                        "CREATE type::record('relationship', $id) SET "
                        "source_entity_id = $src, target_entity_id = $tgt, "
                        "relationship_type = 'co_occurrence', weight = 1.0, "
                        "created_at = $now, last_reinforced = $now",
                        {"id": rid, "src": id_a, "tgt": id_b, "now": now},
                    )
                )
        return batch

    @staticmethod
    def _find_entity_by_type(
        entity_map: dict[str, str], target_type: str, exclude_name: str
    ) -> str | None:
        """Return first entity name of target_type excluding exclude_name, or None."""
        for other_name, other_type in entity_map.items():
            if other_type == target_type and other_name != exclude_name:
                return other_name
        return None

    def _apply_one_typed_relationship(
        self, name: str, ctx: str, entity_map: dict[str, str]
    ) -> None:
        """Emit one typed graph edge for a single (name, ctx) pair."""
        if ctx == "imports":
            dep = self._find_entity_by_type(entity_map, "dependency", name)
            if dep:
                self._graph.add_relationship(name, dep, "imports")
        elif ctx == "resolved_by":
            sol = self._find_entity_by_type(entity_map, "solution", name)
            if sol:
                self._graph.add_relationship(sol, name, "resolved_by")
        # ctx == "calls": implicit from co_occurrence — no edge needed
        # ctx == "decided_to_use": handled by extract_entities_typed

    def _apply_typed_relationships(
        self, rel_contexts: dict[str, str], entity_map: dict[str, str]
    ) -> None:
        """Emit typed graph edges for entities with relationship context."""
        for name, ctx in rel_contexts.items():
            self._apply_one_typed_relationship(name, ctx, entity_map)

    @observe(tier="stage", metric="consolidation.process_episodes")
    def _process_new_episodes(self, stats: dict) -> None:
        episodes = self._storage.get_episodes_since(self._last_consolidated_episode_id)
        for ep in episodes:
            typed_entities = self._graph.extract_entities_typed(
                ep["raw_content"], ep.get("directory", "")
            )
            legacy_entities = self._extract_entities(ep["raw_content"])

            entity_map, rel_contexts = self._build_entity_map(typed_entities, legacy_entities)
            entity_ids, _entity_names = self._upsert_entities(entity_map)

            batch = self._build_cooccurrence_batch(entity_ids)
            if batch:
                self._storage.batch_writes(batch)
                # Car 4: the co-occurrence batch writes edges (raw CREATE/UPDATE
                # relationship) that bypass the storage insert/reinforce bump sites.
                # Bump every entity in this episode's set so the graph adjacency
                # cache reflects the new/reinforced edges (pure-structural read, no
                # fresh recheck). All edges here connect entities within entity_ids.
                for _eid in entity_ids:
                    self._storage._bump_entity_version(_eid)

            self._apply_typed_relationships(rel_contexts, entity_map)

            # Synaptic boost: if any associated memory has high importance,
            # boost nearby memories in the time window
            if ep.get("source_episode_id") is not None:
                source_mem = self._storage.get_memory(ep["source_episode_id"])
                if source_mem and source_mem.get("importance", 0.5) > 0.7:
                    self._thermo.synaptic_boost(source_mem["id"], source_mem["heat"])

            self._last_consolidated_episode_id = max(self._last_consolidated_episode_id, ep["id"])

    @staticmethod
    def _extract_entities(content: str) -> list[tuple[str, str]]:
        """Extract (name, type) pairs from raw episode content."""
        entities: list[tuple[str, str]] = []

        # File paths
        for m in _FILE_PATH_RE.finditer(content):
            path = m.group(0)
            if any(path.endswith(ext) for ext in _CODE_EXTENSIONS):
                entities.append((path, "file"))

        # Python def/class
        for m in _PYTHON_DEF_RE.finditer(content):
            entities.append((m.group(2), "function"))

        # JS function keyword
        for m in _JS_FUNCTION_RE.finditer(content):
            entities.append((m.group(1), "function"))

        # Error/Exception types
        for m in _ERROR_RE.finditer(content):
            entities.append((m.group(1), "error"))

        # Traceback header
        if _TRACEBACK_RE.search(content):
            entities.append(("Traceback", "error"))

        # Python imports
        for m in _IMPORT_RE.finditer(content):
            entities.append((m.group(1), "dependency"))
        for m in _FROM_IMPORT_RE.finditer(content):
            entities.append((m.group(1), "dependency"))

        # JS require
        for m in _REQUIRE_RE.finditer(content):
            entities.append((m.group(1), "dependency"))

        # Decisions
        for m in _DECISION_RE.finditer(content):
            entities.append((m.group(0).strip(), "decision"))

        # Deduplicate preserving order
        seen: set[tuple[str, str]] = set()
        unique: list[tuple[str, str]] = []
        for pair in entities:
            if pair not in seen:
                seen.add(pair)
                unique.append(pair)
        return unique

    @observe(tier="stage", metric="consolidation.graph_priors")
    def _compute_graph_priors(self, stats: dict) -> None:
        """Precompute per-memory graph_prior scalar and store on memory rows (v5.54.1).

        Formula (v1 — simple, defensible):
          For each memory, find which entities appear in its content via a substring
          match against all known entity names. Sum the total relationship weight
          (degree) for those entities in the entity graph. Normalize the scores
          across all memories processed this cycle to [0, 1].

        This runs in CONSOLIDATION (background), NOT on the request path — satisfies
        the I8/I9 latency constraint. The fast-profile recall reads graph_prior as an
        O(1) field value at fusion time.

        Staleness window: one consolidation cadence (typically nightly). Acceptable —
        graph_prior is a secondary nudge, not a primary retrieval signal.

        Bounded per cycle by SIMILARITY_MATRIX_MAX_CANDIDATES (same cap as
        _link_similar_memories) to prevent PHASE_DURATION_WARN_MS overrun.
        """
        cap = getattr(self._settings, "SIMILARITY_MATRIX_MAX_CANDIDATES", 4000)

        # Fetch bounded candidate set (most-recently-accessed first, same as link_similar)
        rows = self._storage.get_memories_with_embeddings(limit=cap, order_by="last_accessed")
        if not rows:
            stats["graph_prior_updated"] = 0
            return

        # Load all entities once — avoid per-memory HTTP round trips
        all_entities = self._storage.get_all_entities(min_heat=0.0, include_archived=True)
        if not all_entities:
            stats["graph_prior_updated"] = 0
            return

        # Build entity-name → total degree (sum of relationship weights) map
        # via ONE name-free batched adjacency query for the whole entity set.
        # The per-entity path defaults to with_names=True — each call fires
        # 1 + 2*K name-enrichment queries per relationship. Degree computation
        # only reads n["weight"], never a neighbour name, so the name-free batch
        # is a drop-in that collapses the N per-entity round-trips to one query.
        entity_degree: dict[int, float] = _batch_entity_degree(self._graph, all_entities)

        # Build entity name list once for substring matching
        entity_name_list: list[tuple[int, str]] = [
            (ent["id"], ent["name"]) for ent in all_entities if ent.get("name")
        ]

        # Compute raw prior score per memory
        raw_scores: dict[int, float] = {}
        for mem in rows:
            mid = mem["id"]
            content = mem.get("content", "") or ""
            score = 0.0
            for eid, ename in entity_name_list:
                if (
                    len(ename) >= getattr(self._settings, "GRAPH_ENTITY_MIN_LENGTH", 3)
                    and ename in content
                ):
                    score += entity_degree.get(eid, 0.0)
            raw_scores[mid] = score

        # Normalize to [0, 1] by cycle-max; avoid divide-by-zero
        max_score = max(raw_scores.values()) if raw_scores else 0.0

        updated = 0
        batch: list[tuple[str, dict | None]] = []
        for mid, raw in raw_scores.items():
            prior = (raw / max_score) if max_score > 1e-9 else 0.0
            batch.append(
                (
                    "UPDATE type::record('memory', $id) SET graph_prior = $gp",
                    {"id": mid, "gp": round(prior, 6)},
                )
            )

        if batch:
            self._storage.batch_writes(batch)
            updated = len(batch)

        stats["graph_prior_updated"] = updated
        logger.info("graph_prior: computed and stored for %d memories", updated)
        _bump_cache_epoch_global(updated)

    @observe(tier="stage", metric="consolidation.cofire_priors")
    def _compute_cofire_priors(self, stats: dict) -> None:
        """Precompute per-memory cofire_prior scalar and store on memory rows (v5.54.2).

        Formula (v1 — simple, defensible):
          For each memory in the bounded candidate set, sum the transition counts from
          the memory_transition table where the memory appears as from_memory_id OR
          to_memory_id. Normalize the scores across all memories to [0, 1].

        Data source: storage.get_all_transitions() — one bulk read, no per-memory
        traversal. The co-recall frequency is a learned association signal: "memories
        that appeared together in past recalls are likely to be relevant together."

        This runs in CONSOLIDATION (background), NOT on the request path — satisfies
        the I8/I9 latency constraint. The fast-profile recall reads cofire_prior as an
        O(1) field value at fusion time, with NO transition-table access.

        Staleness window: one consolidation cadence (typically nightly). Acceptable —
        cofire_prior is a secondary nudge, not a primary retrieval signal.

        Bounded per cycle by SIMILARITY_MATRIX_MAX_CANDIDATES (same cap as
        _link_similar_memories) to prevent PHASE_DURATION_WARN_MS overrun.
        """
        cap = getattr(self._settings, "SIMILARITY_MATRIX_MAX_CANDIDATES", 4000)

        # Fetch bounded candidate set (most-recently-accessed first, same as link_similar)
        rows = self._storage.get_memories_with_embeddings(limit=cap, order_by="last_accessed")
        if not rows:
            stats["cofire_prior_updated"] = 0
            return

        # Build candidate memory ID set for fast membership test
        candidate_ids: set[int] = {mem["id"] for mem in rows}

        # Load all transitions ONCE — avoid per-memory DB round trips
        all_transitions = self._storage.get_all_transitions()

        # Aggregate co-recall count per memory (from_memory_id + to_memory_id symmetric)
        cofire_count: dict[int, float] = {mid: 0.0 for mid in candidate_ids}
        for tr in all_transitions:
            from_id = tr.get("from_memory_id")
            to_id = tr.get("to_memory_id")
            count = float(tr.get("count") or 1)
            if from_id in cofire_count:
                cofire_count[from_id] += count
            if to_id in cofire_count:
                cofire_count[to_id] += count

        # Normalize to [0, 1] by cycle-max; avoid divide-by-zero
        max_count = max(cofire_count.values()) if cofire_count else 0.0

        batch: list[tuple[str, dict | None]] = []
        for mid, raw in cofire_count.items():
            prior = (raw / max_count) if max_count > 1e-9 else 0.0
            batch.append(
                (
                    "UPDATE type::record('memory', $id) SET cofire_prior = $cp",
                    {"id": mid, "cp": round(prior, 6)},
                )
            )

        if batch:
            self._storage.batch_writes(batch)
            updated = len(batch)
        else:
            updated = 0

        stats["cofire_prior_updated"] = updated
        logger.info("cofire_prior: computed and stored for %d memories", updated)
        _bump_cache_epoch_global(updated)

    # ── _link_similar_memories helpers ────────────────────────────────────────

    @staticmethod
    @observe(tier="stage", metric="consolidation.build_valid_embedding_matrix")
    def _build_valid_embedding_matrix(
        memories: list[dict],
        min_count: int = 2,
    ) -> tuple[list[int], object] | None:
        """Build normalized embedding matrix from memories with valid embeddings.

        Returns (ids, matrix) where matrix is an NxD numpy float32 array,
        or None if fewer than `min_count` valid embeddings exist. The full N×N
        pass needs ≥2 (default); the incremental probe set may legitimately have
        only one member (probe-of-1 × corpus-of-N), so callers pass min_count=1.
        """
        import numpy as np

        valid = []
        for m in memories:
            emb = m.get("embedding")
            if not emb or len(emb) == 0:
                continue
            try:
                arr = np.frombuffer(emb, dtype=np.float32)
                norm = np.linalg.norm(arr)
                if len(arr) > 0 and norm > 0:
                    valid.append((m["id"], arr / norm))
            except (TypeError, ValueError):  # fmt: skip
                continue

        if len(valid) < min_count:
            return None

        ids = [v[0] for v in valid]
        matrix = np.stack([v[1] for v in valid])  # N x D
        return ids, matrix

    @staticmethod
    @observe(tier="stage", metric="consolidation.collect_link_candidates")
    def _collect_link_candidates(
        ids: list[int],
        sim_matrix: object,
        existing_links: dict,
        degree: dict[int, int],
        threshold: float,
        max_new_links: int,
        max_degree: int,
    ) -> tuple[list[tuple[int, int, float]], list[tuple[int, float]]]:
        """Iterate sorted similar pairs and accumulate pending inserts and reinforces."""
        import numpy as np

        rows, cols = np.where(np.triu(sim_matrix, k=1) >= threshold)
        sims = sim_matrix[rows, cols]
        order = np.argsort(-sims)

        pending_inserts: list[tuple[int, int, float]] = []
        pending_reinforces: list[tuple[int, float]] = []
        links_created = 0

        for idx in order:
            if links_created >= max_new_links:
                break
            i, j = int(rows[idx]), int(cols[idx])
            sim = float(sims[idx])
            mid_a, mid_b = ids[i], ids[j]

            key = (mid_a, mid_b) if mid_a < mid_b else (mid_b, mid_a)
            existing = existing_links.get(key)
            if existing:
                if sim > existing.get("weight", 0):
                    pending_reinforces.append((existing["id"], sim - existing["weight"]))
                continue

            # Degree cap — pairs processed in descending-similarity order, so a
            # capped-out memory has already kept its strongest links.
            if degree.get(mid_a, 0) >= max_degree or degree.get(mid_b, 0) >= max_degree:
                continue

            pending_inserts.append((mid_a, mid_b, round(sim, 4)))
            degree[mid_a] = degree.get(mid_a, 0) + 1
            degree[mid_b] = degree.get(mid_b, 0) + 1
            links_created += 1

        return pending_inserts, pending_reinforces

    @observe(tier="stage", metric="consolidation.build_similarity_batch")
    def _build_similarity_batch(
        self,
        pending_inserts: list[tuple[int, int, float]],
        pending_reinforces: list[tuple[int, float]],
    ) -> list[tuple[str, dict | None]]:
        """Convert pending inserts/reinforces to a SQL batch for similarity links."""
        now = self._storage._now_iso()
        batch: list[tuple[str, dict | None]] = []

        for mid_a, mid_b, weight in pending_inserts:
            src, tgt = (mid_a, mid_b) if mid_a < mid_b else (mid_b, mid_a)
            lid = self._storage._next_id("memory_similarity_link")
            # C3: citation_source_memory_id = lower-id endpoint (canonical choice for
            # co-occurrence links derived from two memories with equal provenance).
            batch.append(
                (
                    "CREATE type::record('memory_similarity_link', $id) SET "
                    "source_memory_id = $src, target_memory_id = $tgt, "
                    "weight = $weight, created_at = $created_at, updated_at = $updated_at, "
                    "citation_source_memory_id = $csm",
                    {
                        "id": lid,
                        "src": src,
                        "tgt": tgt,
                        "weight": weight,
                        "created_at": now,
                        "updated_at": now,
                        "csm": src,
                    },
                )
            )

        for link_id, delta in pending_reinforces:
            batch.append(
                (
                    "UPDATE type::record('memory_similarity_link', $id) SET "
                    "weight = weight + $delta, updated_at = $now",
                    {"id": link_id, "delta": delta, "now": now},
                )
            )

        return batch

    @observe(tier="stage", metric="consolidation.load_existing_links_and_degree")
    def _load_existing_links_and_degree(self) -> tuple[dict[tuple[int, int], dict], dict[int, int]]:
        """Pre-load all existing links + per-memory degree to avoid per-pair reads."""
        existing_links: dict[tuple[int, int], dict] = {}
        degree: dict[int, int] = {}
        for link in self._storage.get_all_memory_similarity_links():
            src_id, tgt_id = link["source_memory_id"], link["target_memory_id"]
            existing_links[(src_id, tgt_id)] = link
            degree[src_id] = degree.get(src_id, 0) + 1
            degree[tgt_id] = degree.get(tgt_id, 0) + 1
        return existing_links, degree

    @observe(tier="stage", metric="consolidation.link_similar")
    def _link_similar_memories(self, stats: dict) -> None:
        """Create memory_similarity_link records between semantically similar memories.

        Uses numpy matrix multiplication for fast pairwise cosine similarity,
        then upserts into memory_similarity_link (no entity-table rows created).
        Capped per cycle to keep consolidation fast.

        This is the FULL pass (every candidate × every candidate). v5.86 (OT-C4)
        adds an incremental fast-path (`_link_similar_memories_incremental`); this
        method stays the default and the weekly/post-reembed safety-net reconcile.
        """
        max_candidates = self._settings.SIMILARITY_MATRIX_MAX_CANDIDATES
        memories = self._storage.get_memories_with_embeddings(
            limit=max_candidates, order_by="last_accessed"
        )
        if len(memories) < 2:
            return

        result = self._build_valid_embedding_matrix(memories)
        if result is None:
            return
        ids, matrix = result

        sim_matrix = matrix @ matrix.T  # N x N — pairwise cosine via matmul

        existing_links, degree = self._load_existing_links_and_degree()

        threshold = self._settings.SIMILARITY_LINK_THRESHOLD
        max_new_links = 100
        max_degree = self._settings.MAX_SIMILARITY_LINKS_PER_MEMORY

        pending_inserts, pending_reinforces = self._collect_link_candidates(
            ids, sim_matrix, existing_links, degree, threshold, max_new_links, max_degree
        )

        batch = self._build_similarity_batch(pending_inserts, pending_reinforces)
        self._storage.batch_writes(batch)

        stats["similarity_links_created"] = len(pending_inserts)

    @staticmethod
    @observe(tier="stage", metric="consolidation.collect_link_candidates_rect")
    def _collect_link_candidates_rect(
        probe_ids: list[int],
        corpus_ids: list[int],
        sim_matrix: object,
        existing_links: dict,
        degree: dict[int, int],
        threshold: float,
        max_new_links: int,
        max_degree: int,
    ) -> tuple[list[tuple[int, int, float]], list[tuple[int, float]]]:
        """Rectangular variant of _collect_link_candidates for the incremental path.

        `sim_matrix` is probe×corpus (len(probe_ids) rows, len(corpus_ids) cols).
        Each candidate pair has at least one *probe* (recently-created) endpoint —
        the only pairs a full pass would newly link when older embeddings are
        unchanged. Self-pairs (same memory id) are skipped, and the canonical
        (lo, hi) key is de-duplicated via a seen-set (a probe∩corpus overlap can
        otherwise surface the same pair twice).
        """
        import numpy as np

        rows, cols = np.where(sim_matrix >= threshold)
        sims = sim_matrix[rows, cols]
        order = np.argsort(-sims)

        pending_inserts: list[tuple[int, int, float]] = []
        pending_reinforces: list[tuple[int, float]] = []
        seen: set[tuple[int, int]] = set()
        links_created = 0

        for idx in order:
            if links_created >= max_new_links:
                break
            i, j = int(rows[idx]), int(cols[idx])
            mid_a, mid_b = probe_ids[i], corpus_ids[j]
            if mid_a == mid_b:
                continue  # self-pair (same memory in both probe and corpus)

            key = (mid_a, mid_b) if mid_a < mid_b else (mid_b, mid_a)
            if key in seen:
                continue
            seen.add(key)

            sim = float(sims[idx])
            existing = existing_links.get(key)
            if existing:
                if sim > existing.get("weight", 0):
                    pending_reinforces.append((existing["id"], sim - existing["weight"]))
                continue

            if degree.get(mid_a, 0) >= max_degree or degree.get(mid_b, 0) >= max_degree:
                continue

            pending_inserts.append((key[0], key[1], round(sim, 4)))
            degree[mid_a] = degree.get(mid_a, 0) + 1
            degree[mid_b] = degree.get(mid_b, 0) + 1
            links_created += 1

        return pending_inserts, pending_reinforces

    @observe(tier="stage", metric="consolidation.link_similar_incremental")
    def _link_similar_memories_incremental(self, stats: dict, since: str) -> None:
        """Link only memories created since `since` against the full candidate corpus.

        Probe = memories with created_at >= since (capped). Corpus = the full
        candidate set (same cap/order as the full pass). With stable embeddings,
        every link the full pass would create that prior runs missed has at least
        one probe endpoint, so probe×corpus is link-set-equivalent to the full
        N×N pass while costing O(N_probe × N) instead of O(N²).
        """
        max_candidates = self._settings.SIMILARITY_MATRIX_MAX_CANDIDATES
        probe_mems = self._storage.get_memories_with_embeddings(
            limit=max_candidates, order_by="created_at", since=since
        )
        if not probe_mems:
            stats["similarity_links_created"] = 0
            return
        corpus_mems = self._storage.get_memories_with_embeddings(
            limit=max_candidates, order_by="last_accessed"
        )
        if len(corpus_mems) < 2:
            stats["similarity_links_created"] = 0
            return

        probe_result = self._build_valid_embedding_matrix(probe_mems, min_count=1)
        corpus_result = self._build_valid_embedding_matrix(corpus_mems, min_count=2)
        if probe_result is None or corpus_result is None:
            stats["similarity_links_created"] = 0
            return
        probe_ids, probe_matrix = probe_result
        corpus_ids, corpus_matrix = corpus_result

        sim_matrix = probe_matrix @ corpus_matrix.T  # N_probe × N_corpus cosine

        existing_links, degree = self._load_existing_links_and_degree()

        threshold = self._settings.SIMILARITY_LINK_THRESHOLD
        max_new_links = 100
        max_degree = self._settings.MAX_SIMILARITY_LINKS_PER_MEMORY

        pending_inserts, pending_reinforces = self._collect_link_candidates_rect(
            probe_ids,
            corpus_ids,
            sim_matrix,
            existing_links,
            degree,
            threshold,
            max_new_links,
            max_degree,
        )

        batch = self._build_similarity_batch(pending_inserts, pending_reinforces)
        self._storage.batch_writes(batch)

        stats["similarity_links_created"] = len(pending_inserts)

    # ── _merge_duplicates helpers ──────────────────────────────────────────────

    @staticmethod
    @observe(tier="stage", metric="consolidation.exact_content_dedup")
    def _exact_content_dedup(memories: list[dict]) -> set[int]:
        """Pass 1: collect IDs to delete via exact-content match.

        Keeps the hotter copy; on equal heat keeps the first-seen (stable sort).
        Handles missing embeddings since it only looks at content strings.
        """
        to_delete: set[int] = set()
        content_index: dict[str, int] = {}  # content → winning memory id
        content_heat: dict[str, float] = {}  # content → heat of winner
        for mem in memories:
            content = mem.get("content") or ""
            if not content:
                continue
            if content in content_index:
                existing_heat = content_heat[content]
                if mem["heat"] > existing_heat:
                    to_delete.add(content_index[content])
                    content_index[content] = mem["id"]
                    content_heat[content] = mem["heat"]
                else:
                    to_delete.add(mem["id"])
            else:
                content_index[content] = mem["id"]
                content_heat[content] = mem["heat"]
        return to_delete

    @staticmethod
    def _parse_unit_embedding(emb: bytes) -> object | None:
        """Parse raw embedding bytes to a unit-normalized float32 ndarray, or None on failure."""
        import numpy as np

        if not emb or len(emb) == 0:
            return None
        try:
            arr = np.frombuffer(emb, dtype=np.float32)
            norm = np.linalg.norm(arr)
            if len(arr) == 0 or norm == 0:
                return None
            return arr / norm
        except (TypeError, ValueError):  # fmt: skip
            return None

    @staticmethod
    @observe(tier="stage", metric="consolidation.embedding_dedup")
    def _embedding_dedup(memories: list[dict], to_delete: set[int]) -> set[int]:
        """Pass 2: collect IDs to delete via embedding cosine similarity > 0.95.

        Skips memories already marked for deletion. Keeps the hotter copy;
        on tie keeps the lower-index memory (stable sort).
        """
        import numpy as np

        valid: list[tuple[int, np.ndarray, float]] = []
        for mem in memories:
            if mem["id"] in to_delete:
                continue
            unit = _CLSMixin._parse_unit_embedding(mem.get("embedding"))
            if unit is not None:
                valid.append((mem["id"], unit, mem["heat"]))

        if len(valid) < 2:
            return to_delete

        ids = [v[0] for v in valid]
        heats = [v[2] for v in valid]
        matrix = np.stack([v[1] for v in valid])
        sim_matrix = matrix @ matrix.T  # N x N pairwise cosine (O(N·D))

        rows, cols = np.where(np.triu(sim_matrix, k=1) > 0.95)
        for i, j in zip(rows.tolist(), cols.tolist(), strict=False):
            mid_a, mid_b = ids[i], ids[j]
            if mid_a in to_delete or mid_b in to_delete:
                continue
            victim = mid_b if heats[i] >= heats[j] else mid_a
            to_delete.add(victim)

        return to_delete

    @observe(tier="stage", metric="consolidation.merge_duplicates")
    def _merge_duplicates(self, stats: dict) -> None:
        """Delete near-duplicate memories (cosine similarity > 0.95), keeping the hotter one.

        Uses numpy matrix multiplication for O(N·D) pairwise cosine similarity
        (same approach as _link_similar_memories) instead of O(N²) per-pair calls
        to EmbeddingEngine.similarity().

        Two-pass approach:
        1. Exact-content match pre-pass — catches duplicates even when one embedding
           is missing/corrupt (preserves existing short-circuit semantics).
        2. Embedding-similarity pass — numpy matmul over valid-embedding subset.
        """
        max_candidates = self._settings.SIMILARITY_MATRIX_MAX_CANDIDATES
        memories = self._storage.get_memories_with_embeddings(
            limit=max_candidates, order_by="last_accessed"
        )
        if len(memories) < 2:
            return

        to_delete = self._exact_content_dedup(memories)
        to_delete = self._embedding_dedup(memories, to_delete)

        for mid in to_delete:
            self._storage.delete_memory(mid)
            stats["memories_deleted"] += 1
