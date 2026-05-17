"""Wiki page CRUD, search, cross-references, drafts, and project-init helpers.

_WikiMixin provides:
  - insert_wiki_page / update_wiki_page / get_wiki_page / get_wiki_page_by_slug
  - get_wiki_page_by_slug_and_branch / delete_wiki_page / list_wiki_pages
  - search_wiki_fts / search_wiki_fts_scored / search_wiki_vectors
  - replace_wiki_crossrefs / get_wiki_backlinks / get_all_wiki_crossrefs
  - insert_wiki_draft / get_wiki_draft_by_slug / list_wiki_drafts / delete_wiki_draft
  - upsert_project_init / upsert_active_work
"""

import logging

_log = logging.getLogger(__name__)


class _WikiMixin:
    """Wiki page CRUD and related helpers — mixed into StorageEngine."""

    # ------------------------------------------------------------------ Wiki Pages

    def insert_wiki_page(self, page: dict, branch: str | None = None) -> int:
        """Insert a new wiki page, return its integer ID."""
        now = self._now_iso()
        pid = self._next_id("wiki_page")
        embedding = page.get("embedding")
        emb_floats = self._bytes_to_floats(embedding) if isinstance(embedding, bytes) else embedding
        sql = (
            "CREATE type::record('wiki_page', $id) SET "
            "title = $title, slug = $slug, content = $content, "
            "category = $category, tags = $tags, links = $links, "
            "confidence = $confidence, embedding = $embedding, "
            "source_memory_ids = $source_memory_ids, "
            "created_at = $created_at, updated_at = $updated_at"
        )
        params: dict = {
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
        }
        if branch is not None:
            sql += ", branch = $branch"
            params["branch"] = branch
        self._q(sql, params)
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
            f"UPDATE type::record('wiki_page', $id) SET {', '.join(set_parts)}",
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

    def get_wiki_page_by_slug_and_branch(
        self,
        slug: str,
        current_branch: str | None,
        default_branch: str,
    ) -> dict | None:
        """§25 Branch-aware wiki page resolution.

        Resolution order:
        1. Exact slug match on current_branch (when not None).
        2. Exact slug match on default_branch.
        3. Exact slug match with branch IS NONE (legacy/canonical).
        4. Returns None if not found.
        """
        # Step 1: current branch (skip when current is None — non-git context)
        if current_branch is not None:
            rows = self._q(
                "SELECT * FROM wiki_page WHERE slug = $slug AND branch = $branch LIMIT 1",
                {"slug": slug, "branch": current_branch},
            )
            if rows:
                return self._row_to_dict(rows[0])

        # Step 2: default branch
        rows = self._q(
            "SELECT * FROM wiki_page WHERE slug = $slug AND branch = $branch LIMIT 1",
            {"slug": slug, "branch": default_branch},
        )
        if rows:
            return self._row_to_dict(rows[0])

        # Step 3: NONE branch (legacy/canonical)
        rows = self._q(
            "SELECT * FROM wiki_page WHERE slug = $slug AND branch IS NONE LIMIT 1",
            {"slug": slug},
        )
        return self._row_to_dict(rows[0]) if rows else None

    def delete_wiki_page(self, page_id: int) -> bool:
        """Delete a wiki page by ID. Return True if deleted."""
        pid = int(page_id)
        # Fetch slug before deleting so we can clean crossrefs keyed by slug
        rows = self._q(f"SELECT id, slug FROM wiki_page:{pid}")
        if not rows:
            return False
        slug = rows[0].get("slug", "")
        # Clean ALL crossrefs referencing this page's slug (both outgoing and
        # incoming).  wiki.delete() already calls replace_wiki_crossrefs(slug, [])
        # for outgoing rows, but the INCOMING rows (to_slug = slug) are never
        # removed, leaving dangling crossrefs after deletion.
        if slug:
            self._q(
                "DELETE FROM wiki_crossref WHERE from_slug = $slug OR to_slug = $slug",
                {"slug": slug},
            )
        self._q(
            "DELETE type::record('wiki_page', $id)",
            {"id": pid},
        )
        return True

    def list_wiki_pages(
        self,
        category: str | None = None,
        slug_prefix: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """List wiki pages, optionally filtered and limited at the DB layer."""
        conditions: list[str] = []
        params: dict = {}

        if category:
            conditions.append("category = $cat")
            params["cat"] = category

        if slug_prefix:
            conditions.append("string::starts_with(slug, $slug_prefix)")
            params["slug_prefix"] = slug_prefix

        where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        limit_clause = "LIMIT $lim" if (limit is not None and limit > 0) else ""
        if limit_clause:
            params["lim"] = limit

        sql = f"SELECT * FROM wiki_page {where_clause} ORDER BY updated_at DESC {limit_clause}".strip()
        rows = self._q(sql, params) if params else self._q(sql)
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
            f"FROM wiki_page WHERE embedding <|{fetch_k}, 40|> $qv "
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
        """Atomic replace: delete all existing crossrefs FROM this slug, insert new ones.

        §6 Q15: TX wraps the delete+inserts so a partial failure can't leave an
        empty crossref table (was possible before when the CREATE loop crashed).
        """
        import json as _json

        # Build atomic TX: DELETE existing + CREATE all new crossrefs.
        stmts = ["BEGIN TRANSACTION"]
        params: dict = {"slug": from_slug}
        stmts.append("DELETE FROM wiki_crossref WHERE from_slug = $slug")
        for idx, to_slug in enumerate(to_slugs):
            pk = f"to_{idx}"
            params[pk] = to_slug
            stmts.append(f"CREATE wiki_crossref SET from_slug = $slug, to_slug = ${pk}")
        stmts.append("COMMIT TRANSACTION")

        if self._db_url:
            # Server mode: build LET preamble + body
            lets = [f"LET ${k} = {_json.dumps(v, ensure_ascii=False)}" for k, v in params.items()]
            body = ";\n".join(lets + stmts) + ";"
            resp = self._http.post(
                "/sql", content=body.encode(), headers={"Content-Type": "text/plain"}
            )
            resp.raise_for_status()
        else:
            # Embedded mode: execute as single compound statement.
            self._embedded_db.query(";\n".join(stmts) + ";", params)

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

    # ------------------------------------------------------------------ Wiki Drafts

    def insert_wiki_draft(self, draft: dict) -> int:
        """Insert a wiki draft. Returns draft ID."""
        now = self._now_iso()
        did = self._next_id("wiki_draft")
        self._q(
            "CREATE type::record('wiki_draft', $id) SET "
            "title = $title, slug = $slug, content = $content, "
            "category = $category, tags = $tags, confidence = $confidence, "
            "source_memory_ids = $source_memory_ids, created_at = $created_at",
            {
                "id": did,
                "title": draft.get("title", ""),
                "slug": draft["slug"],
                "content": draft.get("content", ""),
                "category": draft.get("category", "reference"),
                "tags": draft.get("tags", []),
                "confidence": draft.get("confidence", "medium"),
                "source_memory_ids": draft.get("source_memory_ids", []),
                "created_at": draft.get("created_at", now),
            },
        )
        return did

    def get_wiki_draft_by_slug(self, slug: str) -> dict | None:
        """Get a wiki draft by slug."""
        rows = self._q(
            "SELECT * FROM wiki_draft WHERE slug = $slug LIMIT 1",
            {"slug": slug},
        )
        return self._row_to_dict(rows[0]) if rows else None

    def list_wiki_drafts(self) -> list[dict]:
        """List all wiki drafts ordered by creation time."""
        rows = self._q("SELECT * FROM wiki_draft ORDER BY created_at DESC")
        return self._rows_to_dicts(rows)

    def delete_wiki_draft(self, slug: str) -> bool:
        """Delete a wiki draft by slug. Return True if deleted."""
        rows = self._q(
            "SELECT id FROM wiki_draft WHERE slug = $slug LIMIT 1",
            {"slug": slug},
        )
        if not rows:
            return False
        did = self._extract_id(rows[0].get("id"))
        self._q(
            "DELETE type::record('wiki_draft', $id)",
            {"id": did},
        )
        return True

    # ------------------------------------------------------------------ _project_init / _active_work atomic helpers

    def upsert_project_init(self, directory: str, content: str) -> dict:
        """Atomic delete-then-insert for _project_init memory.

        Deletes all existing _project_init memories for the directory, then
        inserts a new one tagged [_project_init, _anchor] as semantic+protected.
        Returns the new memory dict (without embedding).
        """
        now = self._now_iso()
        mid = self._next_id("memory")
        # Delete all existing _project_init memories for this directory, then
        # create the new one in a single transaction.
        self._q(
            "BEGIN TRANSACTION;\n"
            "DELETE FROM memory WHERE directory_context = $dir "
            "AND '_project_init' INSIDE tags;\n"
            "CREATE type::record('memory', $id) SET "
            "content = $content, embedding = NONE, tags = $tags, "
            "source_episode_id = NONE, directory_context = $dir, "
            "created_at = $now, last_accessed = $now, "
            "heat = $heat, is_stale = false, file_hash = NONE, "
            "embedding_model = NONE, plasticity = 1.0, stability = 0.0, "
            "excitability = 1.0, store_type = $store_type, "
            "compression_level = 0, sr_x = 0.0, sr_y = 0.0, "
            "reconsolidation_count = 0, provenance_agent = $agent, "
            "vector_clock = '{}', is_protected = true;\n"
            "COMMIT TRANSACTION",
            {
                "id": mid,
                "content": content,
                "tags": ["_project_init", "_anchor"],
                "dir": directory,
                "now": now,
                "heat": 1.0,
                "store_type": "semantic",
                "agent": "default",
            },
        )
        return {
            "id": mid,
            "content": content,
            "tags": ["_project_init", "_anchor"],
            "directory_context": directory,
            "heat": 1.0,
            "is_protected": True,
            "store_type": "semantic",
            "created_at": now,
        }

    def upsert_active_work(self, directory: str, content: str) -> dict:
        """Atomic delete-then-insert for _active_work memory.

        Returns dict with keys: previous_content (str | None), new_memory (dict).
        """
        now = self._now_iso()
        mid = self._next_id("memory")
        # Fetch existing content before replacing
        existing = self._q(
            "SELECT content FROM memory WHERE directory_context = $dir "
            "AND '_active_work' INSIDE tags LIMIT 1",
            {"dir": directory},
        )
        previous_content: str | None = None
        if existing:
            previous_content = existing[0].get("content")

        # Atomic delete-then-insert
        self._q(
            "BEGIN TRANSACTION;\n"
            "DELETE FROM memory WHERE directory_context = $dir "
            "AND '_active_work' INSIDE tags;\n"
            "CREATE type::record('memory', $id) SET "
            "content = $content, embedding = NONE, tags = $tags, "
            "source_episode_id = NONE, directory_context = $dir, "
            "created_at = $now, last_accessed = $now, "
            "heat = $heat, is_stale = false, file_hash = NONE, "
            "embedding_model = NONE, plasticity = 1.0, stability = 0.0, "
            "excitability = 1.0, store_type = $store_type, "
            "compression_level = 0, sr_x = 0.0, sr_y = 0.0, "
            "reconsolidation_count = 0, provenance_agent = $agent, "
            "vector_clock = '{}', is_protected = true;\n"
            "COMMIT TRANSACTION",
            {
                "id": mid,
                "content": content,
                "tags": ["_active_work"],
                "dir": directory,
                "now": now,
                "heat": 1.0,
                "store_type": "episodic",
                "agent": "default",
            },
        )
        new_memory = {
            "id": mid,
            "content": content,
            "tags": ["_active_work"],
            "directory_context": directory,
            "heat": 1.0,
            "is_protected": True,
            "store_type": "episodic",
            "created_at": now,
        }
        return {"previous_content": previous_content, "new_memory": new_memory}
