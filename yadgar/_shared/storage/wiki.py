"""Wiki page CRUD, search, cross-references, and project-init helpers.

_WikiMixin provides:
  - insert_wiki_page / update_wiki_page / get_wiki_page / get_wiki_page_by_slug
  - get_wiki_page_by_slug_directory / delete_wiki_page / list_wiki_pages
  - search_wiki_fts / search_wiki_fts_scored / search_wiki_vectors
  - replace_wiki_crossrefs / get_wiki_backlinks / get_all_wiki_crossrefs
  - upsert_project_init / upsert_active_work
  - insert_wiki_page_version / get_max_version_for_page
  - list_wiki_page_versions / get_wiki_page_version
  - compute_change_summary (in wiki_change_summary.py)

v5.41.1 audit: all version-write paths reviewed for try/except masking.
  - insert_wiki_page: compound BEGIN/COMMIT txn (no masking).
  - update_wiki_page: compound BEGIN/COMMIT txn (no masking).
  - wiki_restore (wiki.py caller): calls storage.update_wiki_page — no masking.
  - wiki_append_section (wiki.py caller): calls storage.update_wiki_page — no masking.
  - insert_wiki_page_version: kept for migration seeder; not called by write paths.
  - replace_wiki_crossrefs: separate txn scope (crossref consistency, not version).
  No other version-write try/except patterns found.

Car J (0047 §7 D25/D26): insert/update/delete carry ``_sanctioned=False``.
True bypasses the mutability gate for server-side lifecycle transitions
(Car G supersede retype, Car K nightly sweep). Default rejects locked/derived
writes with PermissionError.
"""

import logging

from yadgar._shared.observability.observe import observe
from yadgar._shared.observability.tracing import trace_span
from yadgar._shared.storage._project_id_writer import _resolve_project_id_for_write
from yadgar._shared.storage.mutability_gate import enforce_mutability
from yadgar._shared.storage.wiki_change_summary import compute_change_summary

_log = logging.getLogger(__name__)


class _WikiMixin:
    """Wiki page CRUD and related helpers — mixed into StorageEngine."""

    # ------------------------------------------------------------------ Wiki Pages

    @observe(tier="hot", metric="storage.wiki._bump_wiki_epoch")
    def _bump_wiki_epoch(self) -> None:
        """Car 2 (v5.113): advance the structural epoch on ANY wiki mutation.

        This is the single provably-complete invalidation chokepoint for the
        wiki_read / wiki_query / agent_dispatch_prelude caches: every wiki write
        funnels through insert/update/delete_wiki_page or set_wiki_page_metadata,
        and each calls this on success. A GLOBAL bump (``bump_epoch(None)`` →
        ``_GLOBAL_GEN``) is used deliberately: at the storage layer we hold only
        a ``page_id``, not the normalized ``(directory, branch)`` the reads key
        on (wiki_read keys on ``caller_dir.rstrip("/")`` + an ``os.getcwd()``
        branch — nothing like project_brief's git-root). A global bump folds into
        ``_current_epoch(dir)`` for EVERY dir, so it busts the cached read
        regardless of how read and write each normalized the directory — the
        normalization-proof bust (no decorative-epoch bug, cf. Car 1).

        agent_prompt_save is itself a wiki write (wiki.add → update/insert), so
        the prelude cache busts through this same hook — no separate trigger.

        Coarser than strictly needed (busts all dirs + the project_brief/recall
        shadow keys), but correct-by-superset and wiki writes are rare, so the
        hit-rate cost is negligible. Fully guarded: instrumentation must never
        break or roll back the write it follows.
        """
        try:
            from yadgar._shared.runtime.cache_epoch import bump_epoch  # noqa: PLC0415

            bump_epoch(None)
        except Exception:  # pragma: no cover - must never break the write
            pass

    @trace_span()
    def insert_wiki_page(self, page: dict, branch: str | None = None) -> int:
        """Insert a new wiki page, return its integer ID.

        v5.41.1: wiki_page CREATE and wiki_page_version CREATE are wrapped in a
        single BEGIN/COMMIT transaction. Either both succeed or both roll back —
        no orphan wiki_page rows without a version, and no orphan version rows.

        Car J: gate enforced below. Locked/derived pages reject insert; pass
        ``_sanctioned=True`` for server-side lifecycle transitions (Car G).
        """
        # Symmetry with update/delete — `_sanctioned` is opt-in for callers
        # that seed derived rollups (Car K nightly sweep) or write the
        # mutability_override back during a sanctioned migration. Read via
        # get+delete-from-copy so we don't mutate the caller's dict.
        page_copy = dict(page) if isinstance(page, dict) else page
        _sanctioned = bool(page_copy.pop("_sanctioned", False))
        # The mutability gate consults the page_type (no override yet on
        # first insert — row hasn't been written). We synthesise the page
        # dict the gate reads.
        gate_page: dict = {"page_type": page_copy.get("page_type")}
        enforce_mutability(gate_page, op="insert_wiki_page", sanctioned=_sanctioned)

        now = self._now_iso()
        pid = self._next_id("wiki_page")
        # Reserve version row ID outside the txn (counter bump is non-transactional).
        vid = self._next_id("wiki_page_version")
        embedding = page_copy.get("embedding")
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
            "title": page_copy.get("title", ""),
            "slug": page_copy["slug"],
            "content": page_copy.get("content", ""),
            "category": page_copy.get("category"),
            "tags": page_copy.get("tags", []),
            "links": page_copy.get("links", []),
            "confidence": page_copy.get("confidence", 1.0),
            "embedding": emb_floats,
            "source_memory_ids": page_copy.get("source_memory_ids", []),
            "created_at": page_copy.get("created_at", now),
            "updated_at": page_copy.get("updated_at", now),
            "vid": vid,
            "ver_title": page_copy.get("title", ""),
            "ver_content": page_copy.get("content", ""),
            "ver_category": page_copy.get("category"),
            "ver_tags": page_copy.get("tags", []),
            "ver_confidence": page_copy.get("confidence"),
            "ver_source_memory_ids": page_copy.get("source_memory_ids", []),
            "ver_branch": branch,
            "ver_now": now,
        }
        if branch is not None:
            page_set += ", branch = $branch"
            params["branch"] = branch
        # v5.42.5: directory_context — NOT NULL per migration 016 schema constraint.
        # Value comes from page dict (preferred) or falls back to "global".
        directory_context = page_copy.get("directory_context") or "global"
        page_set += ", directory_context = $directory_context"
        params["directory_context"] = directory_context
        # v5.53.2: page_type + wiki_schema_version — optional (option<string> /
        # option<int>). Only included in SET clause when non-None so SurrealDB
        # stores NONE (absent) rather than explicit null for untyped pages.
        if page_copy.get("page_type") is not None:
            page_set += ", page_type = $page_type, wiki_schema_version = $wiki_schema_version"
            params["page_type"] = page_copy["page_type"]
            params["wiki_schema_version"] = page_copy.get("wiki_schema_version", 1)
        # Car L (0047 §16.9): project_id alongside directory_context, REQUIRED
        # on page_copy. C13: the "fall back to the lazy classifier … then to
        # 'unresolved' so the write never blocks" note that stood here described
        # behaviour C5 DELETED — an unstamped page RAISES, and blocking the write
        # is the point (ADR-0227). ``directory_context`` above keeps its
        # ``or "global"`` deliberately: that is REACH, on a column alive until C11.
        project_id = _resolve_project_id_for_write(
            caller_value=page_copy.get("project_id"),
            directory_context=directory_context,
        )
        page_set += ", project_id = $project_id"
        params["project_id"] = project_id

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
        self._bump_wiki_epoch()  # Car 2: bust wiki_read/query/prelude caches
        return pid

    @trace_span()
    def update_wiki_page(
        self,
        page_id: int,
        updates: dict,
        _sanctioned: bool = False,
    ) -> bool:
        """Update fields on an existing wiki page. Return True if found.

        v5.41.1: wiki_page UPDATE and wiki_page_version INSERT are wrapped in a
        single BEGIN/COMMIT transaction. Either both succeed or both roll back.
        Version is always recorded regardless of content identity (I6: no skip
        on hash-identical content — preserves full history).

        Pre-txn reads (get_wiki_page, get_max_version_for_page, _next_id) happen
        outside the transaction. In embedded single-writer mode this is safe.
        In server mode a race window exists between read and txn open, but that
        is a pre-existing constraint scoped out per plan §Non-goals.

        Car J: gate enforced HERE — single chokepoint for all write paths
        (``WikiStore._apply_text_edit``/``append_section``/``restore_version``,
        ``WikiStore.add`` upsert, ``admin_exec.wiki_update`` that bypasses
        ``WikiStore`` entirely). Override wins over per-type default.
        ``_sanctioned=True`` for Car G supersede retype, Car K sweep.
        """
        if not updates:
            return False

        # Read old state before txn (for change_summary + snapshot fields).
        old_page: dict | None = self.get_wiki_page(int(page_id))
        if old_page is None:
            return False

        # Car J: enforce mutability BEFORE the txn — locked/derived pages
        # reject the write with PermissionError. Sanctioned transitions
        # (Car G supersede retype, Car K nightly sweep) pass _sanctioned=True.
        enforce_mutability(old_page, op="update_wiki_page", sanctioned=_sanctioned)

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
        change_summary = compute_change_summary(old_content, new_content)

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
        self._bump_wiki_epoch()  # Car 2: bust wiki_read/query/prelude caches
        return True

    @trace_span()
    def set_wiki_page_metadata(
        self,
        page_id: int,
        field: str,
        value: str | None,
    ) -> bool:
        """Set directory_context on a wiki page. Returns True if page found.

        ADR-0215: ``branch`` was the other settable field here. It is gone — the
        caller-side allowlist (``WikiStore._METADATA_FIELDS``) admits
        ``directory_context`` alone, and because ``wiki_page`` is SCHEMALESS a
        surviving branch writer would silently re-create the column that
        migration 029 just dropped. This method therefore has ONE update shape:
        ``<field> = $upd_val``. The former ``branch = NONE`` special case (needed
        because a Python ``None`` param stores an explicit null, which ``IS NONE``
        does NOT match) is gone with the field it served.

        The ``wiki_page_version`` snapshot below still carries ``branch``: that
        column is an audit-trail record of past versions and migration 029
        deliberately leaves it in place (see
        ``_migration_029_drop_branch_column``, which drops the field on ``memory``
        and ``wiki_page`` only).

        Creates a wiki_page_version row in the same compound transaction.
        """
        old_page = self.get_wiki_page(int(page_id))
        if old_page is None:
            return False
        now = self._now_iso()
        vid = self._next_id("wiki_page_version")
        new_ver = self.get_max_version_for_page(int(page_id)) + 1

        merged = dict(old_page)
        merged[field] = value

        new_content = merged.get("content", "")
        change_summary = compute_change_summary(old_page.get("content", ""), new_content)

        params: dict = {
            "pid": int(page_id),
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

        page_set_clause = f"{field} = $upd_val, updated_at = $upd_now"
        params["upd_val"] = value
        params["upd_now"] = now
        # ``option<T>`` fields (e.g. ``mutability_override`` added by migration
        # 030) need ``= NONE`` to clear, not ``= $upd_val`` — a Python ``None``
        # param serialises to explicit null, which an ``option<T>`` typed
        # column rejects (schema expects ``none`` for absent). Non-nullable
        # fields (the historical case) keep the parameterised form.
        if value is None:
            page_set_clause = f"{field} = NONE, updated_at = $upd_now"
            params.pop("upd_val", None)

        self._q(
            "BEGIN TRANSACTION;\n"
            f"UPDATE type::record('wiki_page', $pid) SET {page_set_clause};\n"
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
        self._bump_wiki_epoch()  # Car 2: bust wiki_read/query/prelude caches
        return True

    @trace_span()
    def get_wiki_page(self, page_id: int) -> dict | None:
        """Get a wiki page by ID."""
        pid = int(page_id)
        rows = self._q(f"SELECT * FROM wiki_page:{pid}")
        return self._row_to_dict(rows[0]) if rows else None

    @trace_span()
    def get_wiki_page_by_slug(self, slug: str) -> dict | None:
        """Get a wiki page by slug."""
        rows = self._q(
            "SELECT * FROM wiki_page WHERE slug = $slug LIMIT 1",
            {"slug": slug},
        )
        return self._row_to_dict(rows[0]) if rows else None

    @trace_span()
    def get_wiki_page_ids_by_slug(self, slug: str) -> list[int]:
        """Return ALL integer page_ids for a given slug across all branches.

        Unlike get_wiki_page_by_slug (LIMIT 1), this returns every row whose
        slug matches — including per-branch rows and 'global' stragglers.
        Used by WikiStore.set_metadata_by_slug to update all rows in one call.

        Returns an empty list when no rows exist for the slug.
        """
        rows = self._q(
            "SELECT id FROM wiki_page WHERE slug = $slug",
            {"slug": slug},
        )
        ids: list[int] = []
        for row in rows:
            d = self._row_to_dict(row)
            if d is not None and "id" in d:
                ids.append(int(d["id"]))
        return ids

    @observe(tier="stage")
    def get_wiki_page_by_slug_directory(
        self,
        slug: str,
        caller_directory: str | None,
    ) -> dict | None:
        """§25 directory-aware wiki page resolution.

        ADR-0215 removed the branch axis; what remains is the directory ladder:
        1. directory = $caller_dir   (project-scoped)
        2. directory = 'global'      (global fallback)
        3. Returns None if not found.

        When caller_directory is None (no caller context), matches on slug alone.

        DP-3: trailing slash stripped from caller_directory before comparison.
        """
        if caller_directory is None:
            rows = self._q(
                "SELECT * FROM wiki_page WHERE slug = $slug LIMIT 1",
                {"slug": slug},
            )
            return self._row_to_dict(rows[0]) if rows else None

        caller_dir = caller_directory.rstrip("/")

        # Step 1: project-scoped
        rows = self._q(
            "SELECT * FROM wiki_page WHERE slug = $slug AND directory_context = $dir LIMIT 1",
            {"slug": slug, "dir": caller_dir},
        )
        if rows:
            return self._row_to_dict(rows[0])

        # Step 2: global fallback
        rows = self._q(
            "SELECT * FROM wiki_page WHERE slug = $slug AND directory_context = 'global' LIMIT 1",
            {"slug": slug},
        )
        return self._row_to_dict(rows[0]) if rows else None

    @trace_span()
    def delete_wiki_page(self, page_id: int, _sanctioned: bool = False) -> bool:
        """Delete a wiki page by ID. Return True if deleted.

        Car J: symmetric with update_wiki_page — locked/derived reject delete.
        ``_sanctioned=True`` for Car K sweep on derived rollups.
        """
        pid = int(page_id)
        # Fetch slug + page_type before deleting so we can enforce mutability
        # AND clean crossrefs keyed by slug in one read.
        rows = self._q(f"SELECT id, slug, page_type, mutability_override FROM wiki_page:{pid}")
        if not rows:
            return False
        row = rows[0]
        slug = row.get("slug", "")
        # Car J: enforce mutability. Derive the row dict the gate reads from
        # the SELECT — same shape ``get_wiki_page`` would return.
        enforce_mutability(
            {
                "id": pid,
                "slug": slug,
                "page_type": row.get("page_type"),
                "mutability_override": row.get("mutability_override"),
            },
            op="delete_wiki_page",
            sanctioned=_sanctioned,
        )
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
        self._bump_wiki_epoch()  # Car 2: bust wiki_read/query/prelude caches
        return True

    @trace_span()
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

    @trace_span()
    def list_wiki_catalog(
        self,
        directory: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Fetch metadata-only rows for catalog rendering (no content/embedding).

        Returns slug, title, category, updated_at per page — excludes heavy
        content and embedding columns so this is safe on the bootstrap hot path.

        v5.53.0: scoped to directory + 'global' when directory supplied.
        When absent, returns all pages (backward-compat).
        """
        conditions: list[str] = []
        params: dict = {}

        if directory is not None:
            caller_dir = directory.rstrip("/")
            conditions.append("(directory_context = $dir OR directory_context = 'global')")
            params["dir"] = caller_dir

        where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        limit_clause = "LIMIT $lim" if (limit is not None and limit > 0) else ""
        if limit_clause:
            params["lim"] = limit

        # v5.53.2: include page_type so catalog can group by type when present.
        sql = (
            f"SELECT slug, title, category, page_type, updated_at FROM wiki_page "
            f"{where_clause} ORDER BY updated_at DESC {limit_clause}"
        ).strip()
        rows = self._q(sql, params) if params else self._q(sql)
        return self._rows_to_dicts(rows)

    # ------------------------------------------------------------------ Wiki Version CRUD

    @observe(tier="stage")
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

    @observe(tier="stage")
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

    @trace_span()
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

    @trace_span()
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

    @trace_span()
    def search_wiki_vectors_tagged(
        self, query_embedding: bytes, include_tag: str, top_k: int = 5
    ) -> list[tuple[int, float]]:
        """Brute-force cosine over ``tags CONTAINS $tag`` rows. Returns (page_id, similarity).
        Tag-scoped brute-force vector search (any tag). No HNSW — avoids dilution.
        Returns similarity NOT distance — accumulate directly, skip 1/(1+distance).
        """
        floats = self._bytes_to_floats(query_embedding)
        rows = self._q(
            "SELECT id, vector::similarity::cosine(embedding, $qv) AS sim "
            "FROM wiki_page WHERE tags CONTAINS $tag AND embedding IS NOT NONE "
            "ORDER BY sim DESC LIMIT $lim",
            {"qv": floats, "tag": include_tag, "lim": top_k},
        )
        results: list[tuple[int, float]] = []
        for row in rows:
            pid = self._extract_id(row.get("id"))
            sim = float(row.get("sim", 0.0))
            results.append((pid, sim))
        return results

    # ------------------------------------------------------------------ Embedding backfill helpers

    @observe(tier="stage")
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

    @observe(tier="stage")
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

    @observe(tier="stage")
    def get_all_wiki_crossrefs(self, limit: int = 0) -> list[dict]:
        """Get all cross-references for graph visualization.

        viz-render-perf (Car A): optional ``limit`` (0/-1 = unlimited). No natural
        weight column, so when limiting we order deterministically on the selected
        slug pair for a stable subset. The unlimited path stays byte-identical for
        non-viz callers (invariants / wiki store backlinks).
        """
        sql = "SELECT from_slug, to_slug FROM wiki_crossref"
        if limit and limit > 0:
            sql += " ORDER BY from_slug, to_slug LIMIT $lim"
            rows = self._q(sql, {"lim": limit})
        else:
            rows = self._q(sql)
        return [{"from_slug": r["from_slug"], "to_slug": r["to_slug"]} for r in rows]

    # ------------------------------------------------------------------ _project_init / _active_work atomic helpers

    @observe(tier="stage")
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

    @observe(tier="stage")
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

    @observe(tier="stage")
    def upsert_dispatch_prelude_marker(self, directory: str) -> dict:
        """Atomic delete-then-insert for _dispatch_prelude marker memory.

        Mirrors upsert_active_work but uses tag '_dispatch_prelude' and a fixed
        content string.  Only the latest timestamp persists (no memory spam).
        Returns dict with keys: id, created_at.
        """
        now = self._now_iso()
        mid = self._next_id("memory")
        self._q(
            "BEGIN TRANSACTION;\n"
            "DELETE FROM memory WHERE directory_context = $dir "
            "AND '_dispatch_prelude' INSIDE tags;\n"
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
                "content": "dispatch_prelude marker",
                "tags": ["_dispatch_prelude"],
                "dir": directory,
                "now": now,
                "heat": 1.0,
                "store_type": "episodic",
                "agent": "default",
            },
        )
        return {"id": mid, "created_at": now}

    @observe(tier="stage")
    def get_prompt_usage_counts(self) -> dict:
        """Return the per-pattern prelude-usage counts (Stage 3.4, #33).

        Counts live in a single global memory row tagged '_prompt_usage' whose
        content is a JSON dict {pattern: count}. Missing row → {}.
        """
        import json  # noqa: PLC0415

        rows = self._q("SELECT content FROM memory WHERE '_prompt_usage' INSIDE tags LIMIT 1")
        if not rows:
            return {}
        try:
            counts = json.loads(rows[0].get("content") or "{}")
        except (ValueError, TypeError):  # fmt: skip
            return {}
        return counts if isinstance(counts, dict) else {}

    @observe(tier="stage")
    def increment_prompt_usage(self, pattern: str) -> int:
        """Increment the prelude-usage counter for *pattern*; return the new count.

        Read-modify-write on the single '_prompt_usage' row via the same atomic
        delete-then-insert as upsert_dispatch_prelude_marker (memory rows are not
        wiki-versioned — no churn). Best-effort counter: a lost increment under
        concurrent prelude calls is acceptable.
        """
        import json  # noqa: PLC0415

        counts = self.get_prompt_usage_counts()
        counts[pattern] = int(counts.get(pattern, 0)) + 1
        now = self._now_iso()
        mid = self._next_id("memory")
        self._q(
            "BEGIN TRANSACTION;\n"
            "DELETE FROM memory WHERE '_prompt_usage' INSIDE tags;\n"
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
                "content": json.dumps(counts, sort_keys=True),
                "tags": ["_prompt_usage"],
                "dir": "global",
                "now": now,
                "heat": 1.0,
                "store_type": "episodic",
                "agent": "default",
            },
        )
        return counts[pattern]
