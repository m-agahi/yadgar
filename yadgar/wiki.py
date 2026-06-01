"""Wiki knowledge base — curated, persistent knowledge pages with hybrid search."""

import difflib
import html
import logging
import re
import time as _time
from datetime import UTC, datetime

from yadgar.tracing import trace_span

logger = logging.getLogger(__name__)


def _wiki_observe_stage(stage: str, elapsed_ms: float) -> None:
    """Observe a wiki query stage duration. No-op on import error."""
    try:
        from yadgar.metrics import yadgar_wiki_query_stage_ms  # noqa: PLC0415

        yadgar_wiki_query_stage_ms.labels(stage=stage).observe(elapsed_ms)
    except Exception:
        pass


WIKI_STALE_DAYS = 90


# ── Section-parsing helpers (wiki_append_section) ─────────────────────────────


def _parse_section_heading_spec(spec: str) -> tuple[str, int | None]:
    """Parse 'Pipeline#2' → ('Pipeline', 2). Bare name → (name, None)."""
    if "#" in spec:
        parts = spec.rsplit("#", 1)
        try:
            return parts[0].strip(), int(parts[1])
        except ValueError:
            pass
    return spec.strip(), None


def _find_section_headings(content: str) -> list[dict]:
    """Find all ## / ### headings at column 0, skipping fenced code blocks.

    Returns list of dicts: {text, level, line_idx, prefix}
    line_idx is 0-based index into content.splitlines().
    """
    lines = content.splitlines()
    headings: list[dict] = []
    in_fence = False
    fence_marker = ""
    _heading_re = re.compile(r"^(#{2,3}) (.+)")

    for i, line in enumerate(lines):
        stripped = line.rstrip()
        # Track fenced code blocks
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif marker == fence_marker:
                in_fence = False
            continue

        if in_fence:
            continue

        m = _heading_re.match(stripped)
        if m:
            headings.append(
                {
                    "text": m.group(2).strip(),
                    "level": len(m.group(1)),
                    "line_idx": i,
                    "prefix": m.group(1),
                }
            )
    return headings


def _find_section_end(lines: list[str], heading_line_idx: int, target_level: int) -> int:
    """Return index of the line that ends the section (exclusive).

    Skips fenced code blocks. Returns len(lines) if no subsequent heading found.
    """
    _heading_re = re.compile(r"^#{2,3} ")
    in_fence = False
    fence_marker = ""
    for i in range(heading_line_idx + 1, len(lines)):
        stripped = lines[i].rstrip("\n")
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif marker == fence_marker:
                in_fence = False
            continue
        if in_fence:
            continue
        m = _heading_re.match(stripped)
        if m:
            level_here = len(stripped) - len(stripped.lstrip("#"))
            if level_here <= target_level:
                return i
    return len(lines)


def _patch_section(
    content: str,
    target: dict,
    new_content: str,
    position: str,
) -> str:
    """Apply a section patch. Returns updated content string."""
    lines = content.splitlines(keepends=True)
    heading_line_idx = target["line_idx"]
    target_level = target["level"]
    end_line_idx = _find_section_end(lines, heading_line_idx, target_level)

    if position == "start_of_section":
        new_line = new_content if new_content.endswith("\n") else new_content + "\n"
        lines.insert(heading_line_idx + 1, new_line)

    elif position == "replace_section":
        body_lines = new_content.splitlines(keepends=True)
        if body_lines and not body_lines[-1].endswith("\n"):
            body_lines[-1] += "\n"
        lines[heading_line_idx + 1 : end_line_idx] = body_lines

    else:  # end_of_section (default)
        # Find last non-blank line in section body; insert after it
        body_end = end_line_idx
        for i in range(end_line_idx - 1, heading_line_idx, -1):
            if lines[i].strip():
                body_end = i + 1
                break
        new_line = new_content if new_content.endswith("\n") else new_content + "\n"
        lines.insert(body_end, new_line)

    return "".join(lines)


def _diff_json(page_id: int, v1: int, v2: int, lines1: list[str], lines2: list[str]) -> dict:
    """Compute JSON-format diff between two version content lists."""
    _heading_re = re.compile(r"^##+ (.+)")
    added_lines = 0
    removed_lines = 0
    hunks: list[dict] = []
    sections_changed: list[str] = []

    for group in difflib.SequenceMatcher(None, lines1, lines2).get_grouped_opcodes(3):
        hunk: dict = {
            "old_start": group[0][1] + 1,
            "old_count": group[-1][2] - group[0][1],
            "new_start": group[0][3] + 1,
            "new_count": group[-1][4] - group[0][3],
            "removed": [],
            "added": [],
        }
        for tag, i1, i2, j1, j2 in group:
            if tag in ("replace", "delete"):
                hunk["removed"].extend(lines1[i1:i2])
                removed_lines += i2 - i1
                _collect_headings(lines1[i1:i2], _heading_re, sections_changed)
            if tag in ("replace", "insert"):
                hunk["added"].extend(lines2[j1:j2])
                added_lines += j2 - j1
        hunks.append(hunk)

    return {
        "page_id": page_id,
        "v1": v1,
        "v2": v2,
        "fmt": "json",
        "hunks": hunks,
        "added_lines": added_lines,
        "removed_lines": removed_lines,
        "sections_changed": sections_changed,
    }


def _collect_headings(lines: list[str], pattern: re.Pattern[str], result: list[str]) -> None:
    """Append section heading texts from lines to result if not already present."""
    for line in lines:
        m = pattern.match(line.rstrip())
        if m:
            heading = m.group(1).strip()
            if heading not in result:
                result.append(heading)


class WikiStore:
    """Manages wiki pages in SurrealDB with hybrid FTS + vector search."""

    CATEGORIES = frozenset(
        {
            "architecture",
            "decision",
            "pattern",
            "debugging",
            "reference",
            "convention",
            "fact",
            "analysis",
        }
    )
    CONFIDENCE_LEVELS = frozenset({"high", "medium", "low"})

    def __init__(self, storage, embeddings) -> None:
        self._storage = storage
        self._embeddings = embeddings

    # ── Public API ────────────────────────────────────────────────────────

    def add(
        self,
        title: str,
        content: str,
        category: str = "reference",
        tags: list[str] | None = None,
        source_memory_ids: list[int] | None = None,
        confidence: str = "medium",
        branch: str | None = None,
    ) -> dict:
        """Create or update a wiki page. Upserts by slug."""
        slug = self._slugify(title)
        if category not in self.CATEGORIES:
            category = "reference"
        if confidence not in self.CONFIDENCE_LEVELS:
            confidence = "medium"
        tags = tags or []
        source_memory_ids = source_memory_ids or []

        existing = self._storage.get_wiki_page_by_slug(slug)
        now = datetime.now(UTC).isoformat()

        if existing:
            merged_tags = list(dict.fromkeys(existing.get("tags", []) + tags))
            merged_sources = list(
                dict.fromkeys(existing.get("source_memory_ids", []) + source_memory_ids)
            )
            # Keep higher confidence
            conf_rank = {"high": 2, "medium": 1, "low": 0}
            best_conf = (
                confidence
                if conf_rank.get(confidence, 0)
                > conf_rank.get(existing.get("confidence", "low"), 0)
                else existing.get("confidence", confidence)
            )
            links = self._extract_wikilinks(content)
            embedding = self._compute_embedding(title, content)
            updates = {
                "content": content,
                "tags": merged_tags,
                "source_memory_ids": merged_sources,
                "confidence": best_conf,
                "category": category,
                "links": links,
                "embedding": embedding,
                "updated_at": now,
            }
            self._storage.update_wiki_page(existing["id"], updates)
            self._sync_crossrefs(slug, links)
            self._link_memories(slug, source_memory_ids)
            return {**existing, **updates}

        links = self._extract_wikilinks(content)
        embedding = self._compute_embedding(title, content)
        page = {
            "slug": slug,
            "title": title,
            "content": content,
            "category": category,
            "tags": tags,
            "source_memory_ids": source_memory_ids,
            "confidence": confidence,
            "links": links,
            "embedding": embedding,
            "created_at": now,
            "updated_at": now,
        }
        page_id = self._storage.insert_wiki_page(page, branch=branch)
        page["id"] = page_id
        self._sync_crossrefs(slug, links)
        self._link_memories(slug, source_memory_ids)
        return page

    def read(self, slug: str) -> dict | None:
        """Read a wiki page by slug (legacy — no branch resolution)."""
        return self._storage.get_wiki_page_by_slug(slug)

    def read_by_branch(
        self,
        slug: str,
        current_branch: str | None,
        default_branch: str,
    ) -> dict | None:
        """Read a wiki page with §25 branch resolution order.

        1. Exact slug match on current_branch.
        2. Exact slug match on default_branch.
        3. Exact slug match with branch IS NONE (legacy/canonical).
        4. Returns None if not found.
        """
        return self._storage.get_wiki_page_by_slug_and_branch(slug, current_branch, default_branch)

    def _collect_wiki_fts_scores(
        self, query: str, scores: dict[int, float], max_results: int
    ) -> None:
        """Collect BM25 FTS scores for wiki pages. Observes fts stage metric."""
        _fts_t0 = _time.perf_counter()
        try:
            fts_results = self._storage.search_wiki_fts_scored(query, limit=max_results * 3)
            if fts_results:
                # SurrealDB returns negative BM25 scores — use min-max normalization
                bm25_vals = [s for _, s in fts_results]
                bm25_min, bm25_max = min(bm25_vals), max(bm25_vals)
                bm25_range = bm25_max - bm25_min
                for page_id, bm25_score in fts_results:
                    normalized = (bm25_score - bm25_min) / bm25_range if bm25_range > 1e-9 else 0.5
                    scores[page_id] = scores.get(page_id, 0.0) + 0.4 * normalized
        except Exception:
            logger.debug("Wiki FTS search failed for query '%s'", query)
        finally:
            _wiki_observe_stage("fts", (_time.perf_counter() - _fts_t0) * 1000)

    def _collect_wiki_vector_scores(
        self, query: str, scores: dict[int, float], max_results: int
    ) -> None:
        """Collect vector similarity scores for wiki pages. Observes embed_query + hnsw stages."""
        try:
            _embed_t0 = _time.perf_counter()
            query_embedding = self._embeddings.encode_query(query)
            _wiki_observe_stage("embed_query", (_time.perf_counter() - _embed_t0) * 1000)
            if query_embedding is not None:
                _hnsw_t0 = _time.perf_counter()
                vec_results = self._storage.search_wiki_vectors(
                    query_embedding, top_k=max_results * 3
                )
                _wiki_observe_stage("hnsw", (_time.perf_counter() - _hnsw_t0) * 1000)
                if vec_results:
                    for page_id, distance in vec_results:
                        similarity = 1.0 / (1.0 + distance)
                        scores[page_id] = scores.get(page_id, 0.0) + 0.6 * similarity
        except Exception:
            logger.debug("Wiki vector search failed for query '%s'", query)

    @trace_span("wiki.query")
    def query(
        self,
        query: str,
        tags: list[str] | None = None,
        category: str | None = None,
        max_results: int = 5,
    ) -> list[dict]:
        """Hybrid search: FTS + vector, filtered by tags/category.

        Combines BM25 keyword scores with cosine similarity scores using
        min-max normalization and reciprocal rank fusion.
        """
        # P11: set dynamic span attributes on the active wiki.query span.
        try:
            from opentelemetry import trace as _otel_trace  # noqa: PLC0415

            _span = _otel_trace.get_current_span()
            if _span and _span.is_recording():
                _span.set_attribute("query_len", len(query))
                _span.set_attribute("tags", ",".join(tags) if tags else "")
                _span.set_attribute("category", category or "")
                _span.set_attribute("max_results", max_results)
        except Exception:
            pass

        scores: dict[int, float] = {}

        # 1. FTS search with BM25 scores
        self._collect_wiki_fts_scores(query, scores, max_results)

        # 2. Vector similarity search (embed_query + hnsw)
        self._collect_wiki_vector_scores(query, scores, max_results)

        if not scores:
            return []

        # 3. Sort by combined score, load full pages
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        results = []
        for page_id, score in ranked:
            page = self._storage.get_wiki_page(page_id)
            if page is None:
                continue
            # Filter by tags if provided
            if tags:
                page_tags = page.get("tags", [])
                if not any(t in page_tags for t in tags):
                    continue
            # Filter by category if provided
            if category and page.get("category") != category:
                continue
            page["_retrieval_score"] = score
            results.append(page)
            if len(results) >= max_results:
                break

        return results

    def delete(self, slug: str) -> bool:
        """Delete a wiki page by slug."""
        page = self._storage.get_wiki_page_by_slug(slug)
        if page is None:
            return False
        self._storage.replace_wiki_crossrefs(slug, [])
        return self._storage.delete_wiki_page(page["id"])

    def list_pages(
        self,
        category: str | None = None,
        slug_prefix: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """List wiki pages, optionally filtered by category/slug_prefix and limited."""
        return self._storage.list_wiki_pages(
            category=category, slug_prefix=slug_prefix, limit=limit
        )

    def find_similar_wiki_pages(
        self,
        title: str,
        content: str,
        branch: str | None = None,
        threshold: float = 0.80,
        top_k: int = 5,
        exclude_slug: str | None = None,
    ) -> list[dict]:
        """Return wiki pages with combined embedding similarity >= threshold.

        Design note: wiki_page stores one combined embedding (title + content[:2000]).
        Separate title-only / content-only embeddings would require a schema change
        (violates §4 non-goals). Gate uses a single cosine similarity threshold on
        the combined embedding.

        Scope: branch-aware. Candidates must have branch == branch OR branch IS NULL
        (canonical). Pages on unrelated branches are excluded.

        Args:
            title: Title of the candidate new page.
            content: Content of the candidate new page.
            branch: Branch context for scope filtering (None = canonical/NULL slot).
            threshold: Minimum cosine similarity to include a page. Default 0.80.
            top_k: Maximum number of candidates to return.
            exclude_slug: Exclude this slug (used to skip self-comparison on upsert).

        Returns:
            List of dicts with keys: slug, title, similarity, branch.
            Sorted descending by similarity.
        """
        # Embed the new page (same formula as _compute_embedding)
        try:
            text = f"{title}\n{content[:2000]}"
            query_embedding = self._embeddings.encode_query(text)
        except Exception:
            logger.debug("find_similar_wiki_pages: embedding failed for '%s'", title)
            return []

        if query_embedding is None:
            return []

        # KNN search — get top_k * 4 candidates so we have room after branch + threshold filter
        try:
            vec_results = self._storage.search_wiki_vectors(query_embedding, top_k=top_k * 4)
        except Exception:
            logger.debug("find_similar_wiki_pages: vector search failed")
            return []

        if not vec_results:
            return []

        # Branch-aware scope: allowed = {branch, None}
        # (branch=None means canonical/NULL slot — always included)
        allowed_branches: set[str | None] = {None}
        if branch is not None:
            allowed_branches.add(branch)

        candidates = []
        for page_id, distance in vec_results:
            similarity = 1.0 - distance  # cosine similarity from cosine distance
            if similarity < threshold:
                continue

            page = self._storage.get_wiki_page(page_id)
            if page is None:
                continue

            # Branch scope filter
            page_branch = page.get("branch")
            if page_branch not in allowed_branches:
                continue

            # Exclude self-slug (used for upsert path)
            if exclude_slug is not None and page.get("slug") == exclude_slug:
                continue

            candidates.append(
                {
                    "slug": page.get("slug", ""),
                    "title": page.get("title", ""),
                    "similarity": round(similarity, 4),
                    "branch": page_branch,
                }
            )
            if len(candidates) >= top_k:
                break

        return sorted(candidates, key=lambda c: c["similarity"], reverse=True)

    def ingest(
        self,
        content: str,
        title: str | None = None,
        tags: list[str] | None = None,
        source_memory_ids: list[int] | None = None,
    ) -> dict:
        """Ingest content. If title matches existing page, append with timestamp."""
        if title is None:
            title = "Untitled"
        slug = self._slugify(title)
        existing = self._storage.get_wiki_page_by_slug(slug)

        if existing:
            now = datetime.now(UTC).isoformat()
            appended = existing["content"] + f"\n\n---\n\n## Update ({now})\n\n{content}"
            merged_tags = list(dict.fromkeys(existing.get("tags", []) + (tags or [])))
            merged_sources = list(
                dict.fromkeys(existing.get("source_memory_ids", []) + (source_memory_ids or []))
            )
            links = self._extract_wikilinks(appended)
            embedding = self._compute_embedding(title, appended)
            updates = {
                "content": appended,
                "tags": merged_tags,
                "source_memory_ids": merged_sources,
                "links": links,
                "embedding": embedding,
                "updated_at": now,
            }
            self._storage.update_wiki_page(existing["id"], updates)
            self._sync_crossrefs(slug, links)
            self._link_memories(slug, source_memory_ids or [])
            return {**existing, **updates}

        return self.add(
            title=title,
            content=content,
            tags=tags,
            source_memory_ids=source_memory_ids,
        )

    def lint(self) -> dict:
        """Wiki health check.

        Returns dict with:
        - issues: list of {page, severity, type, message}
        - stats: {total_pages, orphan_count, stale_count, broken_ref_count, low_confidence_count}
        """
        pages = self._storage.list_wiki_pages()
        slug_set = {p["slug"] for p in pages}
        issues: list[dict] = []

        # Build incoming links map from crossrefs
        all_refs = self._storage.get_all_wiki_crossrefs()
        incoming: dict[str, set[str]] = {slug: set() for slug in slug_set}
        for ref in all_refs:
            target = ref.get("to_slug", "")
            source = ref.get("from_slug", "")
            if target in incoming:
                incoming[target].add(source)

        orphan_count = 0
        stale_count = 0
        broken_ref_count = 0
        low_confidence_count = 0

        now = datetime.now(UTC)
        for page in pages:
            slug = page["slug"]

            # Orphans: no incoming links (except index-like pages)
            if not incoming.get(slug) and slug not in ("index", "home", "readme"):
                orphan_count += 1
                issues.append(
                    {
                        "page": slug,
                        "severity": "info",
                        "type": "orphan",
                        "message": "No incoming links from other pages",
                    }
                )

            # Broken refs: links to non-existent slugs
            for link in page.get("links", []):
                if link not in slug_set:
                    broken_ref_count += 1
                    issues.append(
                        {
                            "page": slug,
                            "severity": "warning",
                            "type": "broken_ref",
                            "message": f"Links to non-existent page '{link}'",
                        }
                    )

            # Stale: updated_at older than WIKI_STALE_DAYS
            updated_at = page.get("updated_at")
            if updated_at:
                try:
                    updated = datetime.fromisoformat(updated_at)
                    if (now - updated).days > WIKI_STALE_DAYS:
                        stale_count += 1
                        issues.append(
                            {
                                "page": slug,
                                "severity": "info",
                                "type": "stale",
                                "message": f"Not updated in over {WIKI_STALE_DAYS} days",
                            }
                        )
                except (ValueError, TypeError) as _e:
                    pass

            # Low confidence
            if page.get("confidence") == "low":
                low_confidence_count += 1
                issues.append(
                    {
                        "page": slug,
                        "severity": "warning",
                        "type": "low_confidence",
                        "message": "Page has low confidence rating",
                    }
                )

        return {
            "issues": issues,
            "stats": {
                "total_pages": len(pages),
                "orphan_count": orphan_count,
                "stale_count": stale_count,
                "broken_ref_count": broken_ref_count,
                "low_confidence_count": low_confidence_count,
            },
        }

    # ── Versioning API ────────────────────────────────────────────────────

    def history(self, page_id: int, limit: int = 20) -> list[dict]:
        """Return version history for a page, newest first, without content field."""
        return self._storage.list_wiki_page_versions(page_id, limit=limit)

    def read_version(self, page_id: int, version: int) -> dict:
        """Return a specific version with full content, or error dict if missing."""
        row = self._storage.get_wiki_page_version(page_id, version)
        if row is None:
            max_ver = self._storage.get_max_version_for_page(page_id)
            return {
                "error": f"version {version} not found for page_id={page_id}",
                "max_version": max_ver,
            }
        row.pop("id", None)  # internal field
        return row

    def diff(self, page_id: int, v1: int, v2: int, fmt: str = "unified") -> dict:
        """Diff two versions of a page. fmt='unified' or 'json'."""
        snap1 = self._storage.get_wiki_page_version(page_id, v1)
        snap2 = self._storage.get_wiki_page_version(page_id, v2)
        if snap1 is None:
            return {"error": f"version {v1} not found for page_id={page_id}"}
        if snap2 is None:
            return {"error": f"version {v2} not found for page_id={page_id}"}

        c1 = snap1.get("content", "")
        c2 = snap2.get("content", "")
        lines1 = c1.splitlines(keepends=True)
        lines2 = c2.splitlines(keepends=True)
        ts1 = snap1.get("created_at", "")
        ts2 = snap2.get("created_at", "")

        if fmt == "json":
            result = _diff_json(page_id, v1, v2, lines1, lines2)
            return result
        # unified
        diff_text = "".join(
            difflib.unified_diff(
                lines1,
                lines2,
                fromfile=f"v{v1} ({ts1})",
                tofile=f"v{v2} ({ts2})",
            )
        )
        return {
            "page_id": page_id,
            "v1": v1,
            "v2": v2,
            "fmt": "unified",
            "diff": diff_text,
        }

    def restore_version(self, page_id: int, version: int) -> dict:
        """Restore a wiki page to a previous version by creating a new version.

        Creates a NEW version (does not delete intervening versions).
        The restored content becomes the new current content.
        Rebuilds embedding from restored title+content.

        Note: wiki_restore bypasses the v5.39 similarity gate because restore is
        explicit user intent (recovery from corruption, not a new duplicate page).
        This method calls storage.update_wiki_page directly, not the gated wiki_add
        MCP path, so the gate is naturally avoided.
        """
        snap = self._storage.get_wiki_page_version(page_id, version)
        if snap is None:
            max_ver = self._storage.get_max_version_for_page(page_id)
            return {
                "error": f"version {version} not found for page_id={page_id}",
                "max_version": max_ver,
            }

        # Recompute embedding from restored content (embedding not stored in version rows)
        title = snap.get("title", "")
        content = snap.get("content", "")
        embedding = self._compute_embedding(title, content)

        updates = {
            "title": title,
            "content": content,
            "category": snap.get("category"),
            "tags": snap.get("tags", []),
            "confidence": snap.get("confidence"),
            "source_memory_ids": snap.get("source_memory_ids", []),
        }
        if embedding is not None:
            updates["embedding"] = embedding

        self._storage.update_wiki_page(page_id, updates)
        new_version = self._storage.get_max_version_for_page(page_id)

        # Rebuild crossrefs from restored content
        links = self._extract_wikilinks(content)
        page = self._storage.get_wiki_page(page_id)
        if page:
            self._sync_crossrefs(page.get("slug", ""), links)

        return {
            "page_id": page_id,
            "restored_from_version": version,
            "new_version": new_version,
            "note": f"version {new_version} created from snapshot of version {version}",
        }

    def append_section(
        self,
        page_id: int,
        section_heading: str,
        content: str,
        position: str = "end_of_section",
    ) -> dict:
        """Section-atomic write: patch a specific section without replacing entire content.

        Prevents the 2026-05-31 corruption pattern where agents replaced the full
        wiki_page content with only their section, destroying everything else.

        Positions:
          end_of_section   — append after section body, before next heading (default)
          start_of_section — insert immediately after the heading line
          replace_section  — replace section body (heading line preserved)
          new_section_top  — create section at top of page (error if heading exists)
          new_section_bottom — create section at bottom of page (error if heading exists)

        Heading detection:
          Matches ## or ### at column 0. Case-insensitive. Ignores ## inside fenced
          code blocks (``` ... ```). Supports Pipeline#2 syntax for nth occurrence
          (1-based; bare name matches first).

        Returns dict with action='appended' on success, or error dict.
        """
        page = self._storage.get_wiki_page(page_id)
        if page is None:
            return {"error": "page_not_found", "page_id": page_id}

        page_content = page.get("content", "")
        heading_text, occurrence = _parse_section_heading_spec(section_heading)

        headings = _find_section_headings(page_content)

        if position in ("new_section_top", "new_section_bottom"):
            # Error if heading already exists
            existing = [h for h in headings if h["text"].lower() == heading_text.lower()]
            if existing:
                return {"error": "section_exists", "section_heading": heading_text}
            # Create new section
            if position == "new_section_bottom":
                new_content = page_content.rstrip("\n") + f"\n\n## {heading_text}\n\n{content}"
            else:  # new_section_top
                new_content = f"## {heading_text}\n\n{content}\n\n" + page_content
            new_content = new_content if new_content.endswith("\n") else new_content + "\n"
        else:
            # Requires existing heading
            matches = [h for h in headings if h["text"].lower() == heading_text.lower()]

            if not matches:
                available = [h["text"] for h in headings]
                return {
                    "error": "section_not_found",
                    "section_heading": heading_text,
                    "available_sections": available,
                }

            if len(matches) > 1 and position != "replace_section" and occurrence is None:
                return {
                    "error": "ambiguous_section",
                    "section_heading": heading_text,
                    "occurrences": len(matches),
                    "hint": "Use 'Pipeline#2' syntax to target nth occurrence",
                }

            occ_idx = (occurrence - 1) if occurrence is not None else 0
            if occ_idx >= len(matches):
                return {
                    "error": "occurrence_out_of_range",
                    "section_heading": heading_text,
                    "requested": occurrence,
                    "max": len(matches),
                }

            target = matches[occ_idx]
            new_content = _patch_section(page_content, target, content, position)

        size_before = len(page_content.encode())
        size_after = len(new_content.encode())

        self._storage.update_wiki_page(page_id, {"content": new_content})
        new_version = self._storage.get_max_version_for_page(page_id)

        # Sync crossrefs from updated content
        links = self._extract_wikilinks(new_content)
        self._sync_crossrefs(page.get("slug", ""), links)

        return {
            "page_id": page_id,
            "new_version": new_version,
            "section_heading": heading_text,
            "action": "appended",
            "size_before": size_before,
            "size_after": size_after,
        }

    # ── Internal ──────────────────────────────────────────────────────────

    def _slugify(self, title: str) -> str:
        """Convert title to URL-safe slug. Max 64 chars.

        HTML entities (&amp;, &lt;, etc.) are unescaped before slug generation
        so titles created via different code paths (direct API vs repo_wiki)
        always produce identical slugs. v5.24.1: fixes &amp; → 'amp' drift.
        """
        slug = re.sub(r"[^a-z0-9]+", "-", html.unescape(title).lower()).strip("-")
        return slug[:64] if slug else "untitled"

    def _extract_wikilinks(self, content: str) -> list[str]:
        """Extract [[slug]] references from markdown content."""
        raw = re.findall(r"\[\[([^\]]+)\]\]", content)
        return list(dict.fromkeys(self._slugify(r) for r in raw))  # dedupe, preserve order

    def _compute_embedding(self, title: str, content: str) -> bytes | None:
        """Semantic anchoring: prepend title to content before embedding."""
        try:
            text = f"{title}\n{content[:2000]}"
            return self._embeddings.encode_document(text)
        except Exception:
            logger.debug("Wiki embedding computation failed for '%s'", title)
            return None

    def _sync_crossrefs(self, slug: str, links: list[str]) -> None:
        """Update wiki_crossref table to match extracted links."""
        self._storage.replace_wiki_crossrefs(slug, links)

    def _link_memories(self, slug: str, memory_ids: list[int]) -> None:
        """Add this wiki page's slug to wiki_refs on each source memory."""
        for mid in memory_ids:
            try:
                mem = self._storage.get_memory(mid)
                if mem is None:
                    continue
                refs = list(mem.get("wiki_refs") or [])
                if slug not in refs:
                    refs.append(slug)
                    self._storage.update_memory_fields(mid, wiki_refs=refs)
            except Exception:
                logger.debug("Failed to link memory %s to wiki page %s", mid, slug)
