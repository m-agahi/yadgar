"""Wiki page CRUD, search, cross-references, drafts, and project-init helpers.

_WikiMixin provides:
  - insert_wiki_page / update_wiki_page / get_wiki_page / get_wiki_page_by_slug
  - get_wiki_page_by_slug_and_branch / delete_wiki_page / list_wiki_pages
  - search_wiki_fts / search_wiki_fts_scored / search_wiki_vectors
  - replace_wiki_crossrefs / get_wiki_backlinks / get_all_wiki_crossrefs
  - insert_wiki_draft / get_wiki_draft_by_slug / list_wiki_drafts / delete_wiki_draft
  - upsert_project_init / upsert_active_work
  - insert_wiki_page_version / get_max_version_for_page
  - list_wiki_page_versions / get_wiki_page_version
  - _compute_change_summary

v5.41.1 audit: all version-write paths reviewed for try/except masking.
  - insert_wiki_page: compound BEGIN/COMMIT txn (no masking).
  - update_wiki_page: compound BEGIN/COMMIT txn (no masking).
  - wiki_restore (wiki.py caller): calls storage.update_wiki_page — no masking.
  - wiki_append_section (wiki.py caller): calls storage.update_wiki_page — no masking.
  - insert_wiki_page_version: kept for migration seeder; not called by write paths.
  - replace_wiki_crossrefs: separate txn scope (crossref consistency, not version).
  No other version-write try/except patterns found.
"""

import difflib
import logging
import re as _re

from yadgar.tracing import trace_span

_log = logging.getLogger(__name__)


# ── Change-summary helpers ─────────────────────────────────────────────────────


_HEADING_RE = _re.compile(r"^##+ (.+)")


def _diff_context_line(diff_line: str) -> str:
    """Strip unified-diff prefix (+/-/@/ ) to get the raw text for heading detection."""
    if diff_line.startswith("@"):
        return diff_line.lstrip("+-@ ")
    return diff_line[1:] if diff_line else ""


def _find_nearby_heading(diff: list[str], i: int, touched: list[str]) -> None:
    """Look back up to 5 diff lines for a ## heading; append to touched if found."""
    for j in range(max(0, i - 5), i):
        m = _HEADING_RE.match(_diff_context_line(diff[j]))
        if m:
            heading = m.group(1).strip()
            if heading not in touched:
                touched.append(heading)
            return


def _compute_change_summary(old_content: str, new_content: str) -> str:
    """Generate a concise diff summary for a wiki page version.

    Format: "+N -M lines | sections: 'Foo', 'Bar' | size: X → Y bytes"
    Capped at 300 chars. No LLM — pure difflib (I9: no LLM on write path).

    Section detection: markdown ## / ### headings at column 0 that appear
    within 5 lines above changed (added/removed) content.
    """
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    diff = list(difflib.unified_diff(old_lines, new_lines, lineterm=""))

    added = 0
    removed = 0
    touched_sections: list[str] = []

    for i, line in enumerate(diff):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
            _find_nearby_heading(diff, i, touched_sections)
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1

    size_old = len(old_content.encode())
    size_new = len(new_content.encode())

    parts = [f"+{added} -{removed} lines"]
    if touched_sections:
        section_str = ", ".join(f"'{s}'" for s in touched_sections[:5])
        parts.append(f"sections: {section_str}")
    parts.append(f"size: {size_old} → {size_new} bytes")

    summary = " | ".join(parts)
    if len(summary) > 300:
        summary = summary[:299] + "…"
    return summary


class _WikiMixin:
    """Wiki page CRUD and related helpers — mixed into StorageEngine."""

    # ------------------------------------------------------------------ Wiki Pages

    @trace_span("storage.wiki.insert_wiki_page")
    def insert_wiki_page(self, page: dict, branch: str | None = None) -> int:
        """Insert a new wiki page, return its integer ID.

        v5.41.1: wiki_page CREATE and wiki_page_version CREATE are wrapped in a
        single BEGIN/COMMIT transaction. Either both succeed or both roll back —
        no orphan wiki_page rows without a version, and no orphan version rows.
        """
        now = self._now_iso()
        pid = self._next_id("wiki_page")
        # Reserve version row ID outside the txn (counter bump is non-transactional).
        vid = self._next_id("wiki_page_version")
        embedding = page.get("embedding")
        emb_floats = self._bytes_to_floats(embedding) if isinstance(embedding, bytes) else embedding

        page_set = (
            "title = $title, slug = $slug, content = $content, "
            "category = $category, tags = $tags, links = $links, "
            "confidence = $confidence, embedding = $embedding, "
            "source_memory_ids = $source_memory_ids, "
            "created_at = $created_at, updated_at = $updated_at"
        )
        params: dict = {
            "pid": pid,
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
            "vid": vid,
            "ver_title": page.get("title", ""),
            "ver_content": page.get("content", ""),
            "ver_category": page.get("category"),
            "ver_tags": page.get("tags", []),
            "ver_confidence": page.get("confidence"),
            "ver_source_memory_ids": page.get("source_memory_ids", []),
            "ver_branch": branch,
            "ver_now": now,
        }
        if branch is not None:
            page_set += ", branch = $branch"
            params["branch"] = branch
        # v5.42.5: directory_context — NOT NULL per migration 016 schema constraint.
        # Value comes from page dict (preferred) or falls back to "global".
        directory_context = page.get("directory_context") or "global"
        page_set += ", directory_context = $directory_context"
        params["directory_context"] = directory_context

        # Single compound transaction: wiki_page + wiki_page_version version=1.
        # I1: no LLM/embed inside txn — pure DB writes only.
        self._q(
            "BEGIN TRANSACTION;\n"
            f"CREATE type::record('wiki_page', $pid) SET {page_set};\n"
            "CREATE type::record('wiki_page_version', $vid) SET "
            "page_id = $pid, version = 1, title = $ver_title, "
            "content = $ver_content, category = $ver_category, tags = $ver_tags, "
            "confidence = $ver_confidence, "
            "source_memory_ids = $ver_source_memory_ids, branch = $ver_branch, "
            "change_summary = 'initial version', created_at = $ver_now, "
            "provenance_agent = 'default';\n"
            "COMMIT TRANSACTION",
            params,
        )
        return pid

    @trace_span("storage.wiki.update_wiki_page")
    def update_wiki_page(self, page_id: int, updates: dict) -> bool:
        """Update fields on an existing wiki page. Return True if found.

        v5.41.1: wiki_page UPDATE and wiki_page_version INSERT are wrapped in a
        single BEGIN/COMMIT transaction. Either both succeed or both roll back.
        Version is always recorded regardless of content identity (I6: no skip
        on hash-identical content — preserves full history).

        Pre-txn reads (get_wiki_page, get_max_version_for_page, _next_id) happen
        outside the transaction. In embedded single-writer mode this is safe.
        In server mode a race window exists between read and txn open, but that
        is a pre-existing constraint scoped out per plan §Non-goals.
        """
        if not updates:
            return False

        # Read old state before txn (for change_summary + snapshot fields).
        old_page: dict | None = self.get_wiki_page(int(page_id))
        if old_page is None:
            return False

        # Handle embedding conversion if present.
        if "embedding" in updates and isinstance(updates["embedding"], bytes):
            updates = dict(updates)
            updates["embedding"] = self._bytes_to_floats(updates["embedding"])
        updates = dict(updates)
        now = self._now_iso()
        updates["updated_at"] = now

        # Build post-update snapshot from old page merged with updates.
        merged = dict(old_page)
        merged.update(updates)
        old_content = old_page.get("content", "")
        new_content = updates.get("content", old_page.get("content", ""))
        change_summary = _compute_change_summary(old_content, new_content)

        # Reserve version row ID + number outside the txn (counters are non-txn).
        vid = self._next_id("wiki_page_version")
        new_ver = self.get_max_version_for_page(int(page_id)) + 1

        # Build UPDATE SET clause for wiki_page.
        set_parts = []
        params: dict = {"pid": int(page_id)}
        for col, val in updates.items():
            set_parts.append(f"{col} = $upd_{col}")
            params[f"upd_{col}"] = val

        # Version snapshot params.
        params.update(
            {
                "vid": vid,
                "new_ver": new_ver,
                "ver_title": merged.get("title", ""),
                "ver_content": new_content,
                "ver_category": merged.get("category"),
                "ver_tags": merged.get("tags", []),
                "ver_confidence": merged.get("confidence"),
                "ver_source_memory_ids": merged.get("source_memory_ids", []),
                "ver_branch": merged.get("branch"),
                "ver_change_summary": change_summary,
                "ver_now": now,
            }
        )

        # Single compound transaction: wiki_page UPDATE + wiki_page_version CREATE.
        # I1: no LLM/embed inside txn — pure DB writes only.
        self._q(
            "BEGIN TRANSACTION;\n"
            f"UPDATE type::record('wiki_page', $pid) SET {', '.join(set_parts)};\n"
            "CREATE type::record('wiki_page_version', $vid) SET "
            "page_id = $pid, version = $new_ver, title = $ver_title, "
            "content = $ver_content, category = $ver_category, tags = $ver_tags, "
            "confidence = $ver_confidence, "
            "source_memory_ids = $ver_source_memory_ids, branch = $ver_branch, "
            "change_summary = $ver_change_summary, created_at = $ver_now, "
            "provenance_agent = 'default';\n"
            "COMMIT TRANSACTION",
            params,
        )
        return True

    @trace_span("storage.wiki.get_wiki_page")
    def get_wiki_page(self, page_id: int) -> dict | None:
        """Get a wiki page by ID."""
        pid = int(page_id)
        rows = self._q(f"SELECT * FROM wiki_page:{pid}")
        return self._row_to_dict(rows[0]) if rows else None

    @trace_span("storage.wiki.get_wiki_page_by_slug")
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

    def get_wiki_page_by_slug_directory_branch(
        self,
        slug: str,
        caller_directory: str | None,
        current_branch: str | None,
    ) -> dict | None:
        """§25 4-step directory-aware wiki page resolution (v5.42.5).

        Resolution order:
        1. directory = $caller_dir  AND  branch = $current_branch  (project-branch-scoped)
        2. directory = $caller_dir  AND  branch IS NONE            (project-canonical)
        3. directory = 'global'     AND  branch IS NONE            (global fallback)
        4. Returns None if not found.

        When caller_directory is None (legacy / no caller context), falls back to
        the old 3-step branch-only resolution via get_wiki_page_by_slug_and_branch.

        DP-3: trailing slash stripped from caller_directory before comparison.
        """
        if caller_directory is None:
            # Legacy fallback — no directory context supplied.
            return self.get_wiki_page_by_slug_and_branch(slug, current_branch, None)

        caller_dir = caller_directory.rstrip("/")

        # Step 1: project-branch-scoped (directory + current branch)
        if current_branch is not None:
            rows = self._q(
                "SELECT * FROM wiki_page WHERE slug = $slug "
                "AND directory_context = $dir AND branch = $branch LIMIT 1",
                {"slug": slug, "dir": caller_dir, "branch": current_branch},
            )
            if rows:
                return self._row_to_dict(rows[0])

        # Step 2: project-canonical (directory + branch IS NULL)
        rows = self._q(
            "SELECT * FROM wiki_page WHERE slug = $slug "
            "AND directory_context = $dir AND branch IS NONE LIMIT 1",
            {"slug": slug, "dir": caller_dir},
        )
        if rows:
            return self._row_to_dict(rows[0])

        # Step 3: global fallback (directory='global' + branch IS NULL)
        rows = self._q(
            "SELECT * FROM wiki_page WHERE slug = $slug "
            "AND directory_context = 'global' AND branch IS NONE LIMIT 1",
            {"slug": slug},
        )
        return self._row_to_dict(rows[0]) if rows else None

    @trace_span("storage.wiki.delete_wiki_page")
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

    @trace_span("storage.wiki.list_wiki_pages")
    def list_wiki_pages(
        self,
        category: str | None = None,
        slug_prefix: str | None = None,
        limit: int | None = None,
        directory: str | None = None,
    ) -> list[dict]:
        """List wiki pages, optionally filtered and limited at the DB layer.

        v5.42.5: when directory is supplied, scope to that directory + 'global'.
        When absent (legacy call), return all pages (backward-compat; WARNING logged
        at MCP layer). DP-3: trailing slash stripped.
        """
        conditions: list[str] = []
        params: dict = {}

        if category:
            conditions.append("category = $cat")
            params["cat"] = category

        if slug_prefix:
            conditions.append("string::starts_with(slug, $slug_prefix)")
            params["slug_prefix"] = slug_prefix

        if directory is not None:
            caller_dir = directory.rstrip("/")
            conditions.append("(directory_context = $dir OR directory_context = 'global')")
            params["dir"] = caller_dir

        where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        limit_clause = "LIMIT $lim" if (limit is not None and limit > 0) else ""
        if limit_clause:
            params["lim"] = limit

        sql = f"SELECT * FROM wiki_page {where_clause} ORDER BY updated_at DESC {limit_clause}".strip()
        rows = self._q(sql, params) if params else self._q(sql)
        return self._rows_to_dicts(rows)

    # ------------------------------------------------------------------ Wiki Version CRUD

    def insert_wiki_page_version(
        self,
        page_id: int,
        snapshot: dict,
        change_summary: str,
        provenance_agent: str = "default",
    ) -> int:
        """Insert a wiki_page_version row. Returns the version number assigned."""
        max_ver = self.get_max_version_for_page(page_id)
        new_ver = max_ver + 1
        vid = self._next_id("wiki_page_version")
        now = self._now_iso()
        self._q(
            "CREATE type::record('wiki_page_version', $id) SET "
            "page_id = $page_id, version = $version, title = $title, "
            "content = $content, category = $category, tags = $tags, "
            "confidence = $confidence, "
            "source_memory_ids = $source_memory_ids, branch = $branch, "
            "change_summary = $change_summary, created_at = $created_at, "
            "provenance_agent = $provenance_agent",
            {
                "id": vid,
                "page_id": page_id,
                "version": new_ver,
                "title": snapshot.get("title", ""),
                "content": snapshot.get("content", ""),
                "category": snapshot.get("category"),
                "tags": snapshot.get("tags", []),
                "confidence": snapshot.get("confidence"),
                "source_memory_ids": snapshot.get("source_memory_ids", []),
                "branch": snapshot.get("branch"),
                "change_summary": change_summary,
                "created_at": now,
                "provenance_agent": provenance_agent,
            },
        )
        return new_ver

    def get_max_version_for_page(self, page_id: int) -> int:
        """Return the highest version number for a page, or 0 if none."""
        rows = self._q(
            "SELECT version FROM wiki_page_version WHERE page_id = $p "
            "ORDER BY version DESC LIMIT 1",
            {"p": int(page_id)},
        )
        if not rows:
            return 0
        return int(rows[0].get("version", 0))

    def list_wiki_page_versions(self, page_id: int, limit: int = 20) -> list[dict]:
        """List version history for a page, newest first, without 'content' field."""
        rows = self._q(
            "SELECT id, page_id, version, title, category, tags, confidence, "
            "change_summary, created_at, provenance_agent, "
            "string::len(content) AS size_bytes "
            "FROM wiki_page_version WHERE page_id = $p "
            "ORDER BY version DESC LIMIT $lim",
            {"p": int(page_id), "lim": int(limit)},
        )
        return self._rows_to_dicts(rows)

    def get_wiki_page_version(self, page_id: int, version: int) -> dict | None:
        """Fetch a single version row with full content."""
        rows = self._q(
            "SELECT * FROM wiki_page_version WHERE page_id = $p AND version = $v LIMIT 1",
            {"p": int(page_id), "v": int(version)},
        )
        return self._row_to_dict(rows[0]) if rows else None

    # ------------------------------------------------------------------ Wiki Search

    @trace_span("storage.wiki.search_wiki_fts")
    def search_wiki_fts(self, query: str, limit: int = 10) -> list[dict]:
        """BM25 full-text search on wiki page content."""
        fts_query = self._preprocess_fts_query(query)
        rows = self._q(
            "SELECT * FROM wiki_page WHERE content @@ $q ORDER BY search::score(1) DESC LIMIT $lim",
            {"q": fts_query, "lim": limit},
        )
        return self._rows_to_dicts(rows)

    @trace_span("storage.wiki.search_wiki_fts_scored")
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

    @trace_span("storage.wiki.search_wiki_vectors")
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

    # ------------------------------------------------------------------ Embedding backfill helpers

    def get_wiki_pages_without_embedding(self) -> list[dict]:
        """Return wiki_page rows where embedding is absent or null.

        SurrealDB distinguishes NONE (field absent) from null (explicit null).
        We catch both: rows inserted via JSON params receive null (not NONE)
        when embedding=None is passed. Both indicate a missing embedding.

        Used by migration_014 backfill to find rows that need re-embedding.
        Returns list of dicts with keys: id, title, content.
        """
        rows = self._q(
            "SELECT id, title, content FROM wiki_page WHERE embedding IS NONE OR embedding IS NULL"
        )
        result = []
        for row in rows:
            pid = self._extract_id(row.get("id"))
            if pid is None:
                continue
            result.append(
                {
                    "id": pid,
                    "title": row.get("title", ""),
                    "content": row.get("content", ""),
                }
            )
        return result

    def update_wiki_page_embedding_only(self, page_id: int, embedding: bytes) -> None:
        """Set embedding on a wiki_page row WITHOUT creating a version entry.

        Used exclusively by migration_014 backfill — updating only the embedding
        column is not a content change and should not produce a version snapshot.
        """
        floats = self._bytes_to_floats(embedding)
        self._q(
            "UPDATE type::record('wiki_page', $pid) SET embedding = $emb",
            {"pid": int(page_id), "emb": floats},
        )

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

    @trace_span("storage.wiki.insert_wiki_draft")
    def insert_wiki_draft(self, draft: dict) -> int:
        """Insert a wiki draft. Returns draft ID.

        v5.42.3: branch field added (migration 015). Pass branch=<value> to associate
        the draft with a specific branch context; branch=None (default) for legacy/canonical
        writes. wiki_approve reads this field and propagates it to the stored wiki page.
        """
        now = self._now_iso()
        did = self._next_id("wiki_draft")
        branch = draft.get("branch")
        # v5.42.3: only include branch in SET when non-None.
        # SurrealDB option<string> requires NONE (omission), not JSON null.
        draft_set = (
            "title = $title, slug = $slug, content = $content, "
            "category = $category, tags = $tags, confidence = $confidence, "
            "source_memory_ids = $source_memory_ids, created_at = $created_at"
        )
        params: dict = {
            "id": did,
            "title": draft.get("title", ""),
            "slug": draft["slug"],
            "content": draft.get("content", ""),
            "category": draft.get("category", "reference"),
            "tags": draft.get("tags", []),
            "confidence": draft.get("confidence", "medium"),
            "source_memory_ids": draft.get("source_memory_ids", []),
            "created_at": draft.get("created_at", now),
        }
        if branch is not None:
            draft_set += ", branch = $branch"
            params["branch"] = branch
        self._q(
            f"CREATE type::record('wiki_draft', $id) SET {draft_set}",
            params,
        )
        return did

    @trace_span("storage.wiki.get_wiki_draft_by_slug")
    def get_wiki_draft_by_slug(self, slug: str) -> dict | None:
        """Get a wiki draft by slug."""
        rows = self._q(
            "SELECT * FROM wiki_draft WHERE slug = $slug LIMIT 1",
            {"slug": slug},
        )
        return self._row_to_dict(rows[0]) if rows else None

    @trace_span("storage.wiki.list_wiki_drafts")
    def list_wiki_drafts(self) -> list[dict]:
        """List all wiki drafts ordered by creation time."""
        rows = self._q("SELECT * FROM wiki_draft ORDER BY created_at DESC")
        return self._rows_to_dicts(rows)

    @trace_span("storage.wiki.delete_wiki_draft")
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
