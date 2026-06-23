"""Wiki knowledge base — curated, persistent knowledge pages with hybrid search."""

import difflib
import html
import logging
import re
import time as _time
from dataclasses import dataclass
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


def _inc_embed_failure(reason: str) -> None:
    """Increment yadgar_wiki_embedding_compute_failed_total counter. Never raises."""
    try:
        from yadgar.metrics import yadgar_wiki_embedding_compute_failed_total  # noqa: PLC0415

        yadgar_wiki_embedding_compute_failed_total.labels(reason=reason).inc()
    except Exception:
        pass


# ── Positional helpers (Layer 2) ─────────────────────────────────────────────

_ANCHOR_HINT_MIN_LEN = 20


def _line_col_to_offset(content: str, line: int, col: int) -> int | None:
    """Convert 1-indexed (line, col) to a char offset into content.

    Uses str.split('\\n') — NOT splitlines() — so \\r\\n content is handled
    consistently and the reverse of '\\n'.join(lines) holds.

    Returns None if line or col is out of range.
    """
    lines = content.split("\n")
    if line < 1 or line > len(lines):
        return None
    row = lines[line - 1]
    if col < 1 or col > len(row) + 1:
        return None
    offset = sum(len(lines[i]) + 1 for i in range(line - 1)) + (col - 1)
    return offset


def _check_anchor_hint_len(anchor_hint: str) -> dict | None:
    """Return an error dict if anchor_hint is shorter than the minimum, else None."""
    if len(anchor_hint) < _ANCHOR_HINT_MIN_LEN:
        return {
            "ok": False,
            "reason": "anchor_hint too short",
            "detail": (
                f"anchor_hint must be ≥{_ANCHOR_HINT_MIN_LEN} chars; got {len(anchor_hint)}"
            ),
        }
    return None


# ── Markdown block parser (Layer 3) ──────────────────────────────────────────

_VALID_BLOCK_TYPES = frozenset(
    {"paragraph", "heading", "code_fence", "blockquote", "list", "table"}
)


_LIST_RE = re.compile(r"^[-*+] |^\d+\. ")
_HEADING_RE_BLOCK = re.compile(r"^#{1,6} ")
_BLOCK_START_RE = re.compile(r"^```|^~~~|^#{1,6} |^> |^\||^[-*+] |^\d+\. ")


def _is_block_start(line: str) -> bool:
    """Return True if line starts a non-paragraph markdown block."""
    return bool(_BLOCK_START_RE.match(line))


def _consume_code_fence(lines: list[str], i: int) -> tuple[int, dict]:
    """Consume a fenced code block starting at i. Returns (new_i, block)."""
    fence_marker = lines[i][:3]
    start = i
    i += 1
    n = len(lines)
    while i < n and not lines[i].startswith(fence_marker):
        i += 1
    end = i + 1  # include closing fence line
    return end, {"type": "code_fence", "start_line": start, "end_line": end}


def _consume_blockquote(lines: list[str], i: int) -> tuple[int, dict]:
    """Consume contiguous blockquote lines starting at i. Returns (new_i, block)."""
    start = i
    i += 1
    n = len(lines)
    while i < n and (lines[i].startswith("> ") or lines[i] == ">"):
        i += 1
    return i, {"type": "blockquote", "start_line": start, "end_line": i}


def _consume_table(lines: list[str], i: int) -> tuple[int, dict]:
    """Consume contiguous table lines starting at i. Returns (new_i, block)."""
    start = i
    i += 1
    n = len(lines)
    while i < n and (lines[i].startswith("|") or "|" in lines[i]):
        i += 1
    return i, {"type": "table", "start_line": start, "end_line": i}


def _consume_list(lines: list[str], i: int) -> tuple[int, dict]:
    """Consume contiguous list lines starting at i. Returns (new_i, block)."""
    start = i
    i += 1
    n = len(lines)
    while i < n and (_LIST_RE.match(lines[i]) or (lines[i].startswith("  ") and lines[i].strip())):
        i += 1
    return i, {"type": "list", "start_line": start, "end_line": i}


def _consume_paragraph(lines: list[str], i: int) -> tuple[int, dict]:
    """Consume a paragraph (runs until blank line or block start). Returns (new_i, block)."""
    start = i
    i += 1
    n = len(lines)
    while i < n and lines[i].strip() and not _is_block_start(lines[i]):
        i += 1
    return i, {"type": "paragraph", "start_line": start, "end_line": i}


def _consume_next_block(lines: list[str], i: int) -> tuple[int, dict]:
    """Consume the next non-blank block starting at lines[i]. Returns (new_i, block).

    Extracted so _parse_markdown_blocks avoids a deeply nested if/elif chain
    (each elif is an AST If node in orelse, incrementing the nesting counter).
    """
    line = lines[i]
    if line.startswith("```") or line.startswith("~~~"):
        return _consume_code_fence(lines, i)
    if _HEADING_RE_BLOCK.match(line):
        return i + 1, {"type": "heading", "start_line": i, "end_line": i + 1}
    if line.startswith("> ") or line == ">":
        return _consume_blockquote(lines, i)
    if line.startswith("|") or (len(line) > 2 and "|" in line[1:-1]):
        return _consume_table(lines, i)
    if _LIST_RE.match(line):
        return _consume_list(lines, i)
    return _consume_paragraph(lines, i)


def _parse_markdown_blocks(content: str) -> list[dict]:
    """Parse markdown content into a list of block spans.

    Each block is a dict:
      {type, start_line, end_line}  — both 0-based, end_line is exclusive.

    Recognised block types: paragraph, heading, code_fence, blockquote,
    list, table.  Blank lines between blocks are NOT emitted as blocks.

    Delegates per-block-type parsing to _consume_next_block to keep
    nesting depth within I13 caps (while + if = 2 levels, not 2 + elif chain).
    """
    lines = content.split("\n")
    blocks: list[dict] = []
    i = 0
    n = len(lines)

    while i < n:
        if not lines[i].strip():
            i += 1
            continue
        i, block = _consume_next_block(lines, i)
        blocks.append(block)

    return blocks


def _replace_block_span(content: str, start_line: int, end_line: int, new_content: str) -> str:
    """Replace lines[start_line:end_line] with new_content, preserving surrounding blank lines."""
    lines = content.split("\n")
    new_lines = new_content.split("\n")
    replaced = lines[:start_line] + new_lines + lines[end_line:]
    return "\n".join(replaced)


# ── Bold/blockquote section-heading helpers (Layer 3 extension) ───────────────


def _find_bold_sections(content: str) -> list[dict]:
    """Find **Bold** first-line section headers (not inside fenced code blocks).

    Returns list of {text, line_idx} dicts.
    """
    lines = content.split("\n")
    sections: list[dict] = []
    in_fence = False
    fence_marker = ""
    _bold_re = re.compile(r"^\*\*(.+?)\*\*\s*$")

    for i, line in enumerate(lines):
        stripped = line.rstrip()
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
        m = _bold_re.match(stripped)
        if m:
            sections.append({"text": m.group(1).strip(), "line_idx": i, "level": 2})
    return sections


def _find_blockquote_sections(content: str) -> list[dict]:
    """Find > first-line blockquote section headers.

    A blockquote section is a line starting with '> ' that we treat as a heading.
    Returns list of {text, line_idx} dicts.
    """
    lines = content.split("\n")
    sections: list[dict] = []
    _bq_re = re.compile(r"^>\s+(.+)$")

    for i, line in enumerate(lines):
        m = _bq_re.match(line.rstrip())
        if m:
            # Only treat as a section header if not consecutive with previous blockquote
            if sections and sections[-1]["line_idx"] == i - 1:
                continue  # part of previous blockquote, not a new section header
            sections.append({"text": m.group(1).strip(), "line_idx": i, "level": 2})
    return sections


def _find_section_end_generic(lines: list[str], heading_line_idx: int) -> int:
    """Find end of a generic (bold/blockquote) section.

    Section ends at the next blank-line-preceded heading-like marker or EOF.
    Returns exclusive end index into lines.
    """
    _heading_re = re.compile(r"^#{2,3} |^\*\*.*\*\*\s*$|^>\s+")
    for i in range(heading_line_idx + 1, len(lines)):
        if _heading_re.match(lines[i].rstrip()):
            return i
    return len(lines)


def _patch_generic_section(
    content: str,
    target: dict,
    new_content: str,
    position: str,
) -> str:
    """Apply a section patch for bold/blockquote section headers.

    Mirrors _patch_section but uses _find_section_end_generic.
    Returns updated content string.
    """
    lines = content.split("\n")
    heading_line_idx = target["line_idx"]
    end_line_idx = _find_section_end_generic(lines, heading_line_idx)

    if position == "start_of_section":
        new_line = new_content if new_content.endswith("\n") else new_content + "\n"
        lines.insert(heading_line_idx + 1, new_line)

    elif position == "replace_section":
        body_lines = new_content.split("\n")
        lines[heading_line_idx + 1 : end_line_idx] = body_lines

    else:  # end_of_section (default)
        body_end = end_line_idx
        for i in range(end_line_idx - 1, heading_line_idx, -1):
            if lines[i].strip():
                body_end = i + 1
                break
        new_line = new_content if new_content.endswith("\n") else new_content + "\n"
        lines.insert(body_end, new_line)

    return "\n".join(lines)


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


@dataclass
class WikiAddOptions:
    """Optional metadata bundle for WikiStore.add().

    Bundles the five least-frequently-passed kwargs so the public add()
    signature stays at 6 params (self + title + content + category + tags + opts)
    — below the params_hard=8 cap (I13).

    v5.55 complexity-debt campaign: extracted from add() params=10 → params=6.
    """

    source_memory_ids: list[int] | None = None
    confidence: str = "medium"
    branch: str | None = None
    directory_context: str | None = None
    page_type: str | None = None


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
        opts: WikiAddOptions | None = None,
    ) -> dict:
        """Create or update a wiki page. Upserts by slug.

        v5.42.5: directory_context — absolute project path or 'global'.
        Defaults to 'global' when None (backward-compat for callers that pre-date
        the directory contract). DP-3: trailing slash stripped.
        v5.53.2: page_type — optional page-type tag from PAGE_TYPES registry.
        When provided, stored with wiki_schema_version=WIKI_SCHEMA_VERSION.
        When None, page_type is not written (backward-compat: existing pages
        without page_type continue to work exactly as before).

        v5.55: rare optional params bundled into WikiAddOptions (complexity-debt I13):
        source_memory_ids, confidence, branch, directory_context, page_type.
        params 10 → 6; HARD allowlist entry removed.
        """
        o = opts or WikiAddOptions()
        source_memory_ids = o.source_memory_ids
        confidence = o.confidence
        branch = o.branch
        directory_context = o.directory_context
        page_type = o.page_type

        slug = self._slugify(title)
        if category not in self.CATEGORIES:
            category = "reference"
        if confidence not in self.CONFIDENCE_LEVELS:
            confidence = "medium"
        tags = tags or []
        source_memory_ids = source_memory_ids or []
        # v5.42.5: normalise directory (DP-3 — strip trailing slash only)
        effective_dir = (directory_context or "global").rstrip("/") or "global"

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
                "directory_context": effective_dir,
            }
            # v5.53.2: only update page_type when caller provides one (don't clobber
            # an existing type with None — preserves type set in a previous write).
            if page_type is not None:
                from yadgar.wiki_meta import WIKI_SCHEMA_VERSION  # noqa: PLC0415

                updates["page_type"] = page_type
                updates["wiki_schema_version"] = WIKI_SCHEMA_VERSION
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
            "directory_context": effective_dir,
        }
        # v5.53.2: stamp page_type + wiki_schema_version on insert when provided.
        if page_type is not None:
            from yadgar.wiki_meta import WIKI_SCHEMA_VERSION  # noqa: PLC0415

            page["page_type"] = page_type
            page["wiki_schema_version"] = WIKI_SCHEMA_VERSION
        page_id = self._storage.insert_wiki_page(page, branch=branch)
        page["id"] = page_id
        # v5.43.0 (DP-2): include branch in returned dict so callers (e.g. wiki_approve)
        # can propagate branch context without a round-trip read.
        page["branch"] = branch
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

    def read_by_directory_branch(
        self,
        slug: str,
        caller_directory: str | None,
        current_branch: str | None,
    ) -> dict | None:
        """Read a wiki page with §25 4-step directory-aware resolution (v5.42.5).

        1. directory=$caller_dir  AND  branch=$current_branch  (project-branch-scoped)
        2. directory=$caller_dir  AND  branch IS NULL          (project-canonical)
        3. directory='global'     AND  branch IS NULL          (global fallback)
        4. Returns None if not found.

        When caller_directory is None: delegates to read_by_branch (legacy path).
        """
        return self._storage.get_wiki_page_by_slug_directory_branch(
            slug, caller_directory, current_branch
        )

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
        directory: str | None = None,
    ) -> list[dict]:
        """List wiki pages, optionally filtered by category/slug_prefix, limited, and directory-scoped.

        v5.42.5: directory filter added. When supplied, scopes to that dir + 'global'.
        """
        return self._storage.list_wiki_pages(
            category=category, slug_prefix=slug_prefix, limit=limit, directory=directory
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

        Design note: wiki_page stores one combined embedding (title + content[:4000]).
        v5.53.1: window raised from 2000 to 4000 chars for better near-duplicate recall.
        Existing pages keep [:2000]-based embeddings until reembed_all is run.
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
        # Embed the new page (same formula as _compute_embedding — must stay in sync).
        # v5.53.1: raised from [:2000] to [:4000] for better near-duplicate recall.
        # Existing stored pages keep their [:2000]-based embeddings until reembed_all.
        try:
            text = f"{title}\n{content[:4000]}"
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
            opts=WikiAddOptions(source_memory_ids=source_memory_ids),
        )

    def lint(self) -> dict:
        """Wiki health check.

        Returns dict with:
        - issues: list of {page, severity, type, message}
        - stats: {total_pages, orphan_count, stale_count, broken_ref_count,
                  low_confidence_count, format_violation_count}

        v5.53.2: for pages with a page_type, checks that all required sections
        (from PAGE_TYPES registry) are present as ## headings. Missing sections
        are reported as warn-level "missing_section" violations. Pages without
        page_type are skipped (no format check — backward-compat).
        """
        from yadgar.wiki_meta import check_page_type_format  # noqa: PLC0415

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
        format_violation_count = 0

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

            # v5.53.2: format check — only for typed pages (untyped pages skipped)
            page_type = page.get("page_type")
            if page_type:
                fmt = check_page_type_format(slug, page_type, page.get("content", ""))
                format_violation_count += len(fmt)
                issues.extend(fmt)

        return {
            "issues": issues,
            "stats": {
                "total_pages": len(pages),
                "orphan_count": orphan_count,
                "stale_count": stale_count,
                "broken_ref_count": broken_ref_count,
                "low_confidence_count": low_confidence_count,
                "format_violation_count": format_violation_count,
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

    _VALID_HEADING_TYPES = frozenset({"h2", "h3", "bold", "blockquote"})

    def _find_headings_by_type(self, page_content: str, heading_type: str) -> list[dict]:
        """Dispatch heading search by heading_type."""
        if heading_type in ("h2", "h3"):
            return _find_section_headings(page_content)
        if heading_type == "bold":
            return _find_bold_sections(page_content)
        return _find_blockquote_sections(page_content)

    def _patch_existing_section(
        self,
        page_content: str,
        headings: list[dict],
        heading_text: str,
        content: str,
        position: str,
        occurrence: int | None,
        heading_type: str,
    ) -> str | dict:
        """Locate and patch an existing section. Returns new content str or error dict."""
        matches = [h for h in headings if h["text"].lower() == heading_text.lower()]
        if not matches:
            return {
                "error": "section_not_found",
                "section_heading": heading_text,
                "available_sections": [h["text"] for h in headings],
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
        if heading_type in ("h2", "h3"):
            return _patch_section(page_content, target, content, position)
        return _patch_generic_section(page_content, target, content, position)

    def append_section(
        self,
        page_id: int,
        section_heading: str,
        content: str,
        position: str = "end_of_section",
        heading_type: str = "h2",
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

        Heading detection (controlled by heading_type, default 'h2' for backward compat):
          h2 (default) — Matches ## or ### at column 0. Case-insensitive. Ignores
            ## inside fenced code blocks. Supports Pipeline#2 syntax.
          h3 — same as h2 (both h2/h3 matched by existing _find_section_headings).
          bold — Matches **Text** first-line patterns outside fenced code blocks.
          blockquote — Matches "> Text" first-line patterns.

        Returns dict with action='appended' on success, or error dict.
        """
        if heading_type not in self._VALID_HEADING_TYPES:
            return {
                "error": "invalid_heading_type",
                "heading_type": heading_type,
                "allowed": sorted(self._VALID_HEADING_TYPES),
            }

        page = self._storage.get_wiki_page(page_id)
        if page is None:
            return {"error": "page_not_found", "page_id": page_id}

        page_content = page.get("content", "")
        heading_text, occurrence = _parse_section_heading_spec(section_heading)
        headings = self._find_headings_by_type(page_content, heading_type)

        if position in ("new_section_top", "new_section_bottom"):
            existing = [h for h in headings if h["text"].lower() == heading_text.lower()]
            if existing:
                return {"error": "section_exists", "section_heading": heading_text}
            if position == "new_section_bottom":
                new_content = page_content.rstrip("\n") + f"\n\n## {heading_text}\n\n{content}"
            else:
                new_content = f"## {heading_text}\n\n{content}\n\n" + page_content
            new_content = new_content if new_content.endswith("\n") else new_content + "\n"
        else:
            result = self._patch_existing_section(
                page_content, headings, heading_text, content, position, occurrence, heading_type
            )
            if isinstance(result, dict):
                return result  # error dict
            new_content = result

        size_before = len(page_content.encode())
        size_after = len(new_content.encode())

        self._storage.update_wiki_page(page_id, {"content": new_content})
        new_version = self._storage.get_max_version_for_page(page_id)

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

    # ── Edit primitives (v5.61.0) ─────────────────────────────────────────

    _METADATA_FIELDS: frozenset[str] = frozenset({"directory_context", "branch"})

    def set_metadata(
        self,
        page_id: int,
        field: str,
        value: str | None,
    ) -> dict:
        """Set directory_context or branch on a wiki page.

        Idempotent: no-op when current value already matches.
        Creates a wiki_page_version row on real change.
        branch=None uses UNSET so §25 IS NONE queries resolve correctly.

        Returns {ok, page_id, changed, version_id} or {ok: False, error}.
        """
        if field not in self._METADATA_FIELDS:
            return {
                "ok": False,
                "error": f"invalid field '{field}' — allowed: {sorted(self._METADATA_FIELDS)}",
            }

        # Validate value per field.
        if field == "directory_context":
            if not value or (value != "global" and not value.startswith("/")):
                return {
                    "ok": False,
                    "error": (
                        "directory_context must be 'global' or an absolute path "
                        f"(starts with '/'); got {value!r}"
                    ),
                }
        elif field == "branch":
            # None = canonical. Empty string invalid.
            if value is not None and value == "":
                return {
                    "ok": False,
                    "error": "branch must be null (canonical) or a non-empty string",
                }

        page = self._storage.get_wiki_page(page_id)
        if page is None:
            return {"ok": False, "error": f"page_id={page_id} not found"}

        current = page.get(field)
        if current == value:
            logger.info(
                "set_metadata no-op: page_id=%s field=%s value=%r (unchanged)",
                page_id,
                field,
                value,
            )
            return {
                "ok": True,
                "page_id": page_id,
                "changed": False,
                "version_id": self._storage.get_max_version_for_page(page_id),
            }

        logger.info(
            "set_metadata: page_id=%s field=%s old=%r new=%r",
            page_id,
            field,
            current,
            value,
        )
        self._storage.set_wiki_page_metadata(page_id, field, value)
        new_version = self._storage.get_max_version_for_page(page_id)
        return {
            "ok": True,
            "page_id": page_id,
            "changed": True,
            "version_id": new_version,
        }

    def set_metadata_by_slug(
        self,
        slug: str,
        field: str,
        value: str | None,
    ) -> dict:
        """Set directory_context or branch on ALL rows sharing a slug.

        Unlike set_metadata(page_id, ...) which targets one row, this method
        fetches EVERY page_id for the slug (across all branches + global
        stragglers) via storage.get_wiki_page_ids_by_slug and applies
        set_metadata to each.

        Field validation + no-op detection delegate to set_metadata per row
        so the audit trail, version rows, and idempotency all work correctly.

        Returns:
            {ok: True, slug, rows_updated, page_ids}   on success
            {ok: False, error}                          on validation failure or slug not found

        Ref: BC-G10.
        """
        # Validate field + value up front (mirrors set_metadata validation).
        if field not in self._METADATA_FIELDS:
            return {
                "ok": False,
                "error": f"invalid field '{field}' — allowed: {sorted(self._METADATA_FIELDS)}",
            }
        if field == "directory_context":
            if not value or (value != "global" and not value.startswith("/")):
                return {
                    "ok": False,
                    "error": (
                        "directory_context must be 'global' or an absolute path "
                        f"(starts with '/'); got {value!r}"
                    ),
                }
        elif field == "branch":
            if value is not None and value == "":
                return {
                    "ok": False,
                    "error": "branch must be null (canonical) or a non-empty string",
                }

        page_ids = self._storage.get_wiki_page_ids_by_slug(slug)
        if not page_ids:
            return {"ok": False, "error": f"Wiki page '{slug}' not found"}

        rows_updated = 0
        for pid in page_ids:
            result = self.set_metadata(pid, field, value)
            if result.get("ok") and result.get("changed"):
                rows_updated += 1

        logger.info(
            "set_metadata_by_slug: slug=%r field=%s value=%r rows_total=%d rows_updated=%d",
            slug,
            field,
            value,
            len(page_ids),
            rows_updated,
        )
        return {
            "ok": True,
            "slug": slug,
            "rows_updated": rows_updated,
            "page_ids": page_ids,
            # Back-compat keys for callers that check the single-row shape.
            # page_id = first resolved id; changed = any row was updated.
            "page_id": page_ids[0],
            "changed": rows_updated > 0,
        }

    def _apply_text_edit(
        self,
        page_id: int,
        new_content: str,
        old_content: str,
        replaced_count: int,
    ) -> dict:
        """Write new_content to page, create version row, return result dict."""
        self._storage.update_wiki_page(page_id, {"content": new_content})
        new_version = self._storage.get_max_version_for_page(page_id)
        return {
            "ok": True,
            "page_id": page_id,
            "version_id": new_version,
            "replaced_count": replaced_count,
            "length_delta": len(new_content.encode()) - len(old_content.encode()),
        }

    def replace_text(
        self,
        page_id: int,
        old_text: str,
        new_text: str,
        occurrences: int | str = 1,
    ) -> dict:
        """Replace old_text with new_text in page content.

        occurrences: int N → require exactly N matches; 'all' → replace all (≥1).
        No-op (ok:True, replaced_count=0) when old_text == new_text.
        Reject (ok:False) when found-count != occurrences.
        Does NOT call similarity gate — caller (MCP tool) does.

        Returns {ok, page_id, version_id, replaced_count, length_delta}.
        """
        page = self._storage.get_wiki_page(page_id)
        if page is None:
            return {"ok": False, "error": f"page_id={page_id} not found"}

        content = page.get("content", "")

        # No-op: same text
        if old_text == new_text:
            return {
                "ok": True,
                "page_id": page_id,
                "version_id": self._storage.get_max_version_for_page(page_id),
                "replaced_count": 0,
                "length_delta": 0,
            }

        found = content.count(old_text)
        if occurrences == "all":
            if found == 0:
                return {"ok": False, "error": f"text not found: {old_text!r}"}
            new_content = content.replace(old_text, new_text)
            return self._apply_text_edit(page_id, new_content, content, found)
        else:
            n = int(occurrences)
            if found != n:
                return {
                    "ok": False,
                    "error": f"occurrences mismatch: expected {n}, found {found}",
                    "expected_occurrences": n,
                    "found_occurrences": found,
                }
            new_content = content.replace(old_text, new_text, n)
            return self._apply_text_edit(page_id, new_content, content, n)

    def delete_text(
        self,
        page_id: int,
        text: str,
        occurrences: int | str = 1,
    ) -> dict:
        """Delete text from page content.

        Absent text is a no-op (ok:True, replaced_count=0) — unlike replace_text.
        occurrences mismatch (when text IS present but count != N) → reject.
        occurrences='all' deletes all matches.

        Returns {ok, page_id, version_id, replaced_count, length_delta}.
        """
        page = self._storage.get_wiki_page(page_id)
        if page is None:
            return {"ok": False, "error": f"page_id={page_id} not found"}

        content = page.get("content", "")
        found = content.count(text)

        # No-op: absent text
        if found == 0:
            return {
                "ok": True,
                "page_id": page_id,
                "version_id": self._storage.get_max_version_for_page(page_id),
                "replaced_count": 0,
                "length_delta": 0,
            }

        if occurrences == "all":
            new_content = content.replace(text, "")
            return self._apply_text_edit(page_id, new_content, content, found)
        else:
            n = int(occurrences)
            if found != n:
                return {
                    "ok": False,
                    "error": f"occurrences mismatch: expected {n}, found {found}",
                    "expected_occurrences": n,
                    "found_occurrences": found,
                }
            new_content = content.replace(text, "", n)
            return self._apply_text_edit(page_id, new_content, content, n)

    def insert_after(
        self,
        page_id: int,
        anchor_text: str,
        new_text: str,
    ) -> dict:
        """Insert new_text immediately after anchor_text.

        anchor_text must be unique (count == 1) else reject.
        Anchor absent → reject.
        Does NOT call similarity gate — caller (MCP tool) does.

        Returns {ok, page_id, version_id, replaced_count, length_delta}.
        """
        page = self._storage.get_wiki_page(page_id)
        if page is None:
            return {"ok": False, "error": f"page_id={page_id} not found"}

        content = page.get("content", "")
        found = content.count(anchor_text)
        if found == 0:
            return {"ok": False, "error": f"anchor_text not found: {anchor_text!r}"}
        if found > 1:
            return {
                "ok": False,
                "error": f"anchor_text not unique: found {found} occurrences",
                "found_occurrences": found,
            }

        new_content = content.replace(anchor_text, anchor_text + new_text, 1)
        return self._apply_text_edit(page_id, new_content, content, 1)

    def insert_before(
        self,
        page_id: int,
        anchor_text: str,
        new_text: str,
    ) -> dict:
        """Insert new_text immediately before anchor_text.

        anchor_text must be unique (count == 1) else reject.
        Anchor absent → reject.
        Does NOT call similarity gate — caller (MCP tool) does.

        Returns {ok, page_id, version_id, replaced_count, length_delta}.
        """
        page = self._storage.get_wiki_page(page_id)
        if page is None:
            return {"ok": False, "error": f"page_id={page_id} not found"}

        content = page.get("content", "")
        found = content.count(anchor_text)
        if found == 0:
            return {"ok": False, "error": f"anchor_text not found: {anchor_text!r}"}
        if found > 1:
            return {
                "ok": False,
                "error": f"anchor_text not unique: found {found} occurrences",
                "found_occurrences": found,
            }

        new_content = content.replace(anchor_text, new_text + anchor_text, 1)
        return self._apply_text_edit(page_id, new_content, content, 1)

    # ── Layer 2: positional primitives (v5.61.0) ──────────────────────────

    def replace_at(
        self,
        page_id: int,
        line: int,
        col: int,
        length: int,
        new_text: str,
        anchor_hint: str,
    ) -> dict:
        """Replace `length` chars at (line, col) after verifying anchor_hint.

        anchor_hint MUST be ≥20 chars. Actual text at offset must start with
        anchor_hint (guards against caller off-by-one).

        Returns {ok, page_id, version_id, applied, length_delta} or {ok:False, ...}.
        """
        hint_err = _check_anchor_hint_len(anchor_hint)
        if hint_err:
            return hint_err

        page = self._storage.get_wiki_page(page_id)
        if page is None:
            return {"ok": False, "error": f"page_id={page_id} not found"}

        content = page.get("content", "")
        offset = _line_col_to_offset(content, line, col)
        if offset is None:
            return {
                "ok": False,
                "error": f"coordinates out of range: line={line}, col={col}",
            }

        # Verify anchor_hint
        if not content[offset:].startswith(anchor_hint):
            preview = content[offset : offset + len(anchor_hint) + 10]
            return {
                "ok": False,
                "reason": "anchor_hint mismatch",
                "actual_text_preview": preview,
            }

        new_content = content[:offset] + new_text + content[offset + length :]
        result = self._apply_text_edit(page_id, new_content, content, 1)
        result["applied"] = True
        result.pop("replaced_count", None)
        return result

    def delete_at(
        self,
        page_id: int,
        line: int,
        col: int,
        length: int,
        anchor_hint: str,
    ) -> dict:
        """Delete `length` chars at (line, col) after verifying anchor_hint.

        anchor_hint MUST be ≥20 chars. Actual text at offset must start with
        anchor_hint.

        Returns {ok, page_id, version_id, applied, length_delta} or {ok:False, ...}.
        """
        hint_err = _check_anchor_hint_len(anchor_hint)
        if hint_err:
            return hint_err

        page = self._storage.get_wiki_page(page_id)
        if page is None:
            return {"ok": False, "error": f"page_id={page_id} not found"}

        content = page.get("content", "")
        offset = _line_col_to_offset(content, line, col)
        if offset is None:
            return {
                "ok": False,
                "error": f"coordinates out of range: line={line}, col={col}",
            }

        if not content[offset:].startswith(anchor_hint):
            preview = content[offset : offset + len(anchor_hint) + 10]
            return {
                "ok": False,
                "reason": "anchor_hint mismatch",
                "actual_text_preview": preview,
            }

        new_content = content[:offset] + content[offset + length :]
        result = self._apply_text_edit(page_id, new_content, content, 1)
        result["applied"] = True
        result.pop("replaced_count", None)
        return result

    def insert_at(
        self,
        page_id: int,
        line: int,
        col: int,
        new_text: str,
        anchor_hint: str,
    ) -> dict:
        """Insert new_text at (line, col) after verifying the text before the
        insertion point ends with anchor_hint.

        anchor_hint MUST be ≥20 chars. The `length` in chars of text immediately
        BEFORE the insertion point must end with anchor_hint.

        Returns {ok, page_id, version_id, applied, length_delta} or {ok:False, ...}.
        """
        hint_err = _check_anchor_hint_len(anchor_hint)
        if hint_err:
            return hint_err

        page = self._storage.get_wiki_page(page_id)
        if page is None:
            return {"ok": False, "error": f"page_id={page_id} not found"}

        content = page.get("content", "")
        offset = _line_col_to_offset(content, line, col)
        if offset is None:
            return {
                "ok": False,
                "error": f"coordinates out of range: line={line}, col={col}",
            }

        # anchor_hint = expected text immediately before insertion point
        before = content[:offset]
        if not before.endswith(anchor_hint):
            preview = before[-len(anchor_hint) - 10 :]
            return {
                "ok": False,
                "reason": "anchor_hint mismatch",
                "actual_text_preview": preview,
            }

        new_content = content[:offset] + new_text + content[offset:]
        result = self._apply_text_edit(page_id, new_content, content, 1)
        result["applied"] = True
        result.pop("replaced_count", None)
        return result

    # ── Layer 3: structural primitives (v5.61.0) ──────────────────────────

    def replace_markdown_block(
        self,
        page_id: int,
        block_type: str,
        block_index: int,
        new_content: str,
    ) -> dict:
        """Replace the Nth block of block_type with new_content.

        block_type ∈ {paragraph, heading, code_fence, blockquote, list, table}.
        block_index is 0-based within block_type.
        new_content replaces the entire block span (including markers).

        Returns {ok, page_id, version_id, replaced_count, length_delta} or {ok:False, ...}.
        """
        if block_type not in _VALID_BLOCK_TYPES:
            return {
                "ok": False,
                "error": (
                    f"invalid block_type '{block_type}' — allowed: {sorted(_VALID_BLOCK_TYPES)}"
                ),
            }

        page = self._storage.get_wiki_page(page_id)
        if page is None:
            return {"ok": False, "error": f"page_id={page_id} not found"}

        content = page.get("content", "")
        blocks = _parse_markdown_blocks(content)
        typed_blocks = [b for b in blocks if b["type"] == block_type]

        if block_index >= len(typed_blocks) or block_index < 0:
            return {
                "ok": False,
                "error": (
                    f"block_index {block_index} out of range: "
                    f"found {len(typed_blocks)} {block_type} block(s)"
                ),
            }

        target = typed_blocks[block_index]
        updated = _replace_block_span(
            content, target["start_line"], target["end_line"], new_content
        )
        return self._apply_text_edit(page_id, updated, content, 1)

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
        """Semantic anchoring: prepend title to content before embedding.

        v5.42.1: failures are surfaced via WARN log + Prometheus counter instead
        of being silently swallowed. The WIKI_EMBED_FAILURE_BLOCKS_WRITE knob
        (default False) controls whether a failure aborts the write or is tolerated.
        """
        from yadgar.config import get_settings  # noqa: PLC0415

        try:
            # v5.53.1: raised from [:2000] to [:4000] for better similarity matching.
            # In sync with find_similar_wiki_pages query embedding formula.
            text = f"{title}\n{content[:4000]}"
            result = self._embeddings.encode_document(text)
        except Exception as exc:
            _inc_embed_failure("exception")
            settings = get_settings()
            if settings.WIKI_EMBED_FAILURE_BLOCKS_WRITE:
                raise RuntimeError(
                    f"wiki embedding failed for '{title}' "
                    f"(WIKI_EMBED_FAILURE_BLOCKS_WRITE=True): {exc}"
                ) from exc
            logger.warning(
                "wiki embedding computation failed for '%s': %s — proceeding with NULL embedding",
                title,
                exc,
            )
            return None

        if result is None:
            _inc_embed_failure("returned_none")
            settings = get_settings()
            if settings.WIKI_EMBED_FAILURE_BLOCKS_WRITE:
                raise RuntimeError(
                    f"wiki embedding returned None for '{title}' "
                    "(WIKI_EMBED_FAILURE_BLOCKS_WRITE=True)"
                )
            logger.warning(
                "wiki embedding returned None for '%s' — proceeding with NULL embedding",
                title,
            )
            return None

        return result

    def backfill_null_embeddings(self, batch_size: int = 50) -> int:
        """Backfill embeddings for all wiki_page rows where embedding IS NULL.

        Called from server/lifecycle.py after both StorageEngine and
        EmbeddingEngine are ready. Idempotent: re-running finds 0 rows and
        returns 0 immediately.

        Per-batch transactional (each batch is a separate encode + update).
        Embed-service unavailable: logs warning + skips batch, returns count
        of rows successfully backfilled so far (incremental progress preserved).

        Returns:
            Number of rows that were successfully backfilled.
        """
        rows = self._storage.get_wiki_pages_without_embedding()
        if not rows:
            return 0

        _total = len(rows)
        logger.info("backfill_null_embeddings: backfilling %d wiki_page rows...", _total)

        backfilled = 0
        for i in range(0, _total, batch_size):
            batch = rows[i : i + batch_size]
            for row in batch:
                pid = row["id"]
                title = row["title"]
                content = row["content"]
                try:
                    emb = self._compute_embedding(title, content)
                    if emb is None:
                        logger.warning(
                            "backfill_null_embeddings: embed returned None for page_id=%d "
                            "slug='%s' — skipping",
                            pid,
                            self._slugify(title),
                        )
                        continue
                    self._storage.update_wiki_page_embedding_only(pid, emb)
                    backfilled += 1
                except Exception as exc:
                    logger.warning(
                        "backfill_null_embeddings: failed for page_id=%d title=%r: %s — skipping",
                        pid,
                        title,
                        exc,
                    )

        logger.info(
            "backfill_null_embeddings: done — %d/%d rows backfilled",
            backfilled,
            _total,
        )
        return backfilled

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
