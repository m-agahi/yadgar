"""Wiki knowledge base — curated, persistent knowledge pages with hybrid search."""

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
