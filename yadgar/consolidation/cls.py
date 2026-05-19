"""CLS consolidation mixin — episode processing, duplicate merging, similarity linking."""

import logging
import re
from itertools import combinations

logger = logging.getLogger("yadgar.consolidation")

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

    def _process_new_episodes(self, stats: dict) -> None:
        episodes = self._storage.get_episodes_since(self._last_consolidated_episode_id)
        for ep in episodes:
            # Use typed extraction for richer relationships
            typed_entities = self._graph.extract_entities_typed(
                ep["raw_content"], ep.get("directory", "")
            )
            # Fall back to legacy extraction for broad coverage
            legacy_entities = self._extract_entities(ep["raw_content"])

            # Merge: typed triples -> (name, type) pairs + relationship context
            entity_map: dict[str, str] = {}  # name -> type
            rel_contexts: dict[str, str] = {}  # name -> relationship context
            for name, etype, ctx in typed_entities:
                entity_map[name] = etype
                if ctx:
                    rel_contexts[name] = ctx
            for name, etype in legacy_entities:
                if name not in entity_map:
                    entity_map[name] = etype

            entity_ids = []
            entity_names = []
            for name, etype in entity_map.items():
                existing = self._storage.get_entity_by_name(name)
                if existing:
                    self._storage.reinforce_entity(existing["id"])
                    entity_ids.append(existing["id"])
                else:
                    eid = self._storage.insert_entity({"name": name, "type": etype})
                    entity_ids.append(eid)
                entity_names.append(name)

            # Build co-occurrence relationships — ONE bulk fetch + batched writes
            # instead of O(N²) per-pair HTTP calls.
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

            if to_reinforce:
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

            if batch:
                self._storage.batch_writes(batch)

            # Build typed relationships from extraction context
            for name, ctx in rel_contexts.items():
                if ctx == "imports":
                    # Find the module this was imported from (nearest dependency)
                    for other_name, other_type in entity_map.items():
                        if other_type == "dependency" and other_name != name:
                            self._graph.add_relationship(name, other_name, "imports")
                            break
                elif ctx == "calls":
                    pass  # calls are implicit from co_occurrence for now
                elif ctx == "resolved_by":
                    for other_name, other_type in entity_map.items():
                        if other_type == "solution" and other_name != name:
                            self._graph.add_relationship(other_name, name, "resolved_by")
                            break
                elif ctx == "decided_to_use":
                    pass  # decision pairs handled by extract_entities_typed

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

    def _link_similar_memories(self, stats: dict) -> None:
        """Create memory_similarity_link records between semantically similar memories.

        Uses numpy matrix multiplication for fast pairwise cosine similarity,
        then upserts into memory_similarity_link (no entity-table rows created).
        Capped per cycle to keep consolidation fast.
        """
        import numpy as np

        max_candidates = self._settings.SIMILARITY_MATRIX_MAX_CANDIDATES
        memories = self._storage.get_memories_with_embeddings(
            limit=max_candidates, order_by="last_accessed"
        )
        if len(memories) < 2:
            return

        threshold = self._settings.SIMILARITY_LINK_THRESHOLD
        max_new_links = 100  # cap per consolidation cycle
        max_degree = self._settings.MAX_SIMILARITY_LINKS_PER_MEMORY

        # Build embedding matrix (only memories with valid embeddings)
        valid = []
        for m in memories:
            emb = m.get("embedding")
            if emb and len(emb) > 0:
                try:
                    arr = np.frombuffer(emb, dtype=np.float32)
                    if len(arr) > 0 and np.linalg.norm(arr) > 0:
                        valid.append((m["id"], arr / np.linalg.norm(arr)))
                except Exception:
                    continue

        if len(valid) < 2:
            return

        ids = [v[0] for v in valid]
        matrix = np.stack([v[1] for v in valid])  # N x D

        # Pairwise cosine similarity via matrix multiplication (fast)
        sim_matrix = matrix @ matrix.T  # N x N

        # Pre-load all existing links to avoid per-pair read roundtrips.
        # Key is canonical (source_memory_id, target_memory_id) — already stored as min/max.
        existing_links: dict[tuple[int, int], dict] = {}
        degree: dict[int, int] = {}  # memory_id -> current link count
        for link in self._storage.get_all_memory_similarity_links():
            src_id, tgt_id = link["source_memory_id"], link["target_memory_id"]
            existing_links[(src_id, tgt_id)] = link
            degree[src_id] = degree.get(src_id, 0) + 1
            degree[tgt_id] = degree.get(tgt_id, 0) + 1

        # Find pairs above threshold (upper triangle only)
        # Get indices sorted by descending similarity for best-first linking
        links_created = 0
        rows, cols = np.where(np.triu(sim_matrix, k=1) >= threshold)
        sims = sim_matrix[rows, cols]
        order = np.argsort(-sims)

        pending_inserts: list[tuple[int, int, float]] = []  # (mid_a, mid_b, weight)
        pending_reinforces: list[tuple[int, float]] = []  # (link_id, delta)

        for idx in order:
            if links_created >= max_new_links:
                break
            i, j = int(rows[idx]), int(cols[idx])
            sim = float(sims[idx])
            mid_a, mid_b = ids[i], ids[j]

            # Canonical key matches storage convention (source < target)
            key = (mid_a, mid_b) if mid_a < mid_b else (mid_b, mid_a)
            existing = existing_links.get(key)
            if existing:
                # Reinforce if new similarity is higher
                if sim > existing.get("weight", 0):
                    pending_reinforces.append((existing["id"], sim - existing["weight"]))
                continue

            # Degree cap — keep the similarity graph sparse. Pairs are processed
            # in descending-similarity order, so a capped-out memory has already
            # kept its strongest links.
            if degree.get(mid_a, 0) >= max_degree or degree.get(mid_b, 0) >= max_degree:
                continue

            pending_inserts.append((mid_a, mid_b, round(sim, 4)))
            degree[mid_a] = degree.get(mid_a, 0) + 1
            degree[mid_b] = degree.get(mid_b, 0) + 1
            links_created += 1

        # Batch all writes into a single transaction
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
                        "csm": src,  # lower-id memory as canonical citation source
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

        self._storage.batch_writes(batch)

        stats["similarity_links_created"] = links_created

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
        import numpy as np

        max_candidates = self._settings.SIMILARITY_MATRIX_MAX_CANDIDATES
        memories = self._storage.get_memories_with_embeddings(
            limit=max_candidates, order_by="last_accessed"
        )
        if len(memories) < 2:
            return

        to_delete: set[int] = set()

        # Pass 1: exact-content match (cheap, handles missing embeddings)
        content_index: dict[str, int] = {}  # content → first-seen memory id
        content_heat: dict[str, float] = {}  # content → heat of winner
        for mem in memories:
            content = mem.get("content") or ""
            if not content:
                continue
            if content in content_index:
                existing_id = content_index[content]
                existing_heat = content_heat[content]
                if mem["heat"] > existing_heat:
                    # New one is hotter — evict the old winner
                    to_delete.add(existing_id)
                    content_index[content] = mem["id"]
                    content_heat[content] = mem["heat"]
                else:
                    to_delete.add(mem["id"])
            else:
                content_index[content] = mem["id"]
                content_heat[content] = mem["heat"]

        # Pass 2: embedding-similarity pass via numpy matmul
        # Only consider memories not already marked for deletion and with valid embeddings.
        valid: list[tuple[int, np.ndarray, float]] = []  # (id, unit_vec, heat)
        for mem in memories:
            if mem["id"] in to_delete:
                continue
            emb = mem.get("embedding")
            if not emb or len(emb) == 0:
                continue
            try:
                arr = np.frombuffer(emb, dtype=np.float32)
                norm = np.linalg.norm(arr)
                if len(arr) == 0 or norm == 0:
                    continue
                valid.append((mem["id"], arr / norm, mem["heat"]))
            except Exception:
                continue

        if len(valid) >= 2:
            ids = [v[0] for v in valid]
            heats = [v[2] for v in valid]
            matrix = np.stack([v[1] for v in valid])  # N x D

            # Pairwise cosine similarity via matrix multiplication (fast, O(N·D))
            sim_matrix = matrix @ matrix.T  # N x N

            # Find pairs strictly above 0.95 in upper triangle (same semantics as legacy > 0.95)
            rows, cols = np.where(np.triu(sim_matrix, k=1) > 0.95)

            for i, j in zip(rows.tolist(), cols.tolist(), strict=False):
                mid_a, mid_b = ids[i], ids[j]
                if mid_a in to_delete or mid_b in to_delete:
                    continue
                # Keep higher-heat memory; on tie, keep the one with lower index (stable)
                heat_a, heat_b = heats[i], heats[j]
                victim = mid_b if heat_a >= heat_b else mid_a
                to_delete.add(victim)

        for mid in to_delete:
            self._storage.delete_memory(mid)
            stats["memories_deleted"] += 1
