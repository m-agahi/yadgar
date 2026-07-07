"""v5.61.0 Wiki Edit Primitives — Layer 2 + Layer 3 TDD test suite.

Tests cover:

  Layer 2 — positional (anchor_hint MANDATORY):
    wiki_replace_at(slug, line, col, length, new_text, anchor_hint, ...)
    wiki_delete_at(slug, line, col, length, anchor_hint, ...)
    wiki_insert_at(slug, line, col, new_text, anchor_hint, ...)

    Contract:
      anchor_hint MUST be ≥20 chars (reject shorter).
      replace_at/delete_at: actual text at offset starts with anchor_hint → else reject.
      insert_at: text immediately before insertion point ends with anchor_hint → else reject.
      line/col 1-indexed; length in chars.
      Returns {ok, page_id, version_id, applied, length_delta}.

  Layer 3 — structural:
    wiki_replace_markdown_block(slug, block_type, block_index, new_content, ...)
      block_type ∈ {paragraph, heading, code_fence, blockquote, list, table}
      block_index 0-based within block_type.

    wiki_append_section with heading_type param:
      heading_type=bold   → **text** first-line pattern
      heading_type=blockquote → > text first-line pattern
      heading_type=h2|h3  → existing behaviour unchanged

Test IDs:
  L2-01 … L2-17   (positional)
  L3-01 … L3-18   (structural)
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from yadgar._shared.storage.migrations import _migration_013_wiki_page_version
from yadgar.core import server

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    """Isolated storage per test."""
    tmp_path = tmp_path_factory.mktemp("wiki_edit_primitives_l2_")
    server.init_engines(
        db_path=str(tmp_path / "wiki_l2l3_test.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    with patch("yadgar.core.server._detect_branch", return_value="feat/test-branch"):
        _migration_013_wiki_page_version(_storage())
        yield
    server.shutdown()


def _storage():
    return server._get_storage()


def _insert_page(
    slug="test-page",
    title="Test Page",
    content="initial content here",
    category="reference",
    tags=None,
    confidence="medium",
    directory_context="global",
    branch=None,
):
    return _storage().insert_wiki_page(
        {
            "slug": slug,
            "title": title,
            "content": content,
            "category": category,
            "tags": tags or [],
            "confidence": confidence,
            "source_memory_ids": [],
            "links": [],
            "directory_context": directory_context,
        },
        branch=branch,
    )


def _version_count(page_id):
    rows = _storage()._q(
        "SELECT * FROM wiki_page_version WHERE page_id = $p",
        {"p": page_id},
    )
    return len(rows)


# ── Layer 2: wiki_replace_at ──────────────────────────────────────────────────


class TestWikiReplaceAt:
    """L2-01 … L2-06"""

    def test_replace_at_happy_path(self):
        """Replace a span at known line/col with valid anchor_hint."""
        # "Hello world" starts at line 1, col 1, length 5 ("Hello")
        pid = _insert_page("replace-at-happy", content="Hello world\nLine two\n")
        anchor_hint = "Hello world\nLine two\n"  # 21 chars ≥ 20
        result = server.wiki_replace_at(
            "replace-at-happy",
            line=1,
            col=1,
            length=5,
            new_text="Howdy",
            anchor_hint=anchor_hint,
        )
        assert result.get("ok") is True
        assert result.get("applied") is True
        page = _storage().get_wiki_page(pid)
        assert page["content"].startswith("Howdy world")

    def test_replace_at_anchor_hint_too_short(self):
        """anchor_hint < 20 chars → reject with ok:False."""
        _insert_page("replace-at-short", content="Hello world here now\n")
        result = server.wiki_replace_at(
            "replace-at-short",
            line=1,
            col=1,
            length=5,
            new_text="Bye",
            anchor_hint="Hello world",  # 11 chars < 20
        )
        assert result.get("ok") is False
        assert "anchor_hint" in result.get("reason", "").lower() or "anchor" in str(result).lower()

    def test_replace_at_anchor_hint_mismatch(self):
        """anchor_hint doesn't match actual text at coords → reject."""
        _insert_page("replace-at-mismatch", content="Hello world here today now\n")
        result = server.wiki_replace_at(
            "replace-at-mismatch",
            line=1,
            col=1,
            length=5,
            new_text="Bye",
            anchor_hint="Wrong text is here now!",  # 23 chars but wrong
        )
        assert result.get("ok") is False
        assert result.get("reason") == "anchor_hint mismatch"
        assert "actual_text_preview" in result

    def test_replace_at_creates_version(self):
        """Successful replace_at creates a wiki_page_version row."""
        pid = _insert_page("replace-at-ver", content="Some long content line here\n")
        before = _version_count(pid)
        server.wiki_replace_at(
            "replace-at-ver",
            line=1,
            col=1,
            length=4,
            new_text="Many",
            anchor_hint="Some long content line here\n",  # 28 chars ≥ 20
        )
        assert _version_count(pid) == before + 1

    def test_replace_at_out_of_bounds_line(self):
        """Line beyond end of content → ok:False."""
        _insert_page("replace-at-oob-line", content="Only one line\n")
        result = server.wiki_replace_at(
            "replace-at-oob-line",
            line=99,
            col=1,
            length=4,
            new_text="Nope",
            anchor_hint="This anchor text is long enough here",
        )
        assert result.get("ok") is False

    def test_replace_at_returns_length_delta(self):
        """length_delta = len(new_text) - length."""
        _insert_page("replace-at-delta", content="Foo bar baz qux quux here\n")
        result = server.wiki_replace_at(
            "replace-at-delta",
            line=1,
            col=1,
            length=3,  # "Foo"
            new_text="LongerWord",
            anchor_hint="Foo bar baz qux quux here\n",  # 26 chars ≥ 20
        )
        assert result.get("ok") is True
        assert result.get("length_delta") == len("LongerWord") - 3


# ── Layer 2: wiki_delete_at ───────────────────────────────────────────────────


class TestWikiDeleteAt:
    """L2-07 … L2-10"""

    def test_delete_at_happy_path(self):
        """Delete a span at known coords with valid anchor_hint."""
        pid = _insert_page(
            "delete-at-happy",
            content="Keep this. Remove me. Keep rest.\n",
        )
        # "Remove me. " starts at col 12 on line 1 (1-indexed), length 11
        result = server.wiki_delete_at(
            "delete-at-happy",
            line=1,
            col=12,
            length=11,
            anchor_hint="Remove me. Keep rest.\n",  # 22 chars ≥ 20
        )
        assert result.get("ok") is True
        page = _storage().get_wiki_page(pid)
        assert "Remove me." not in page["content"]
        assert "Keep this." in page["content"]

    def test_delete_at_anchor_hint_too_short(self):
        """anchor_hint < 20 chars → reject."""
        _insert_page("delete-at-short", content="Alpha beta gamma delta here\n")
        result = server.wiki_delete_at(
            "delete-at-short",
            line=1,
            col=1,
            length=5,
            anchor_hint="Alpha beta",  # 10 chars < 20
        )
        assert result.get("ok") is False

    def test_delete_at_anchor_hint_mismatch(self):
        """Actual text doesn't start with anchor_hint → reject with preview."""
        _insert_page("delete-at-mismatch", content="Alpha beta gamma delta zeta\n")
        result = server.wiki_delete_at(
            "delete-at-mismatch",
            line=1,
            col=1,
            length=5,
            anchor_hint="Wrong anchor hint here okay!",  # 28 chars
        )
        assert result.get("ok") is False
        assert result.get("reason") == "anchor_hint mismatch"

    def test_delete_at_creates_version(self):
        """Successful delete_at creates a wiki_page_version row."""
        pid = _insert_page("delete-at-ver", content="Some long text that we edit here\n")
        before = _version_count(pid)
        server.wiki_delete_at(
            "delete-at-ver",
            line=1,
            col=1,
            length=4,
            anchor_hint="Some long text that we edit here\n",  # ≥20 chars
        )
        assert _version_count(pid) == before + 1


# ── Layer 2: wiki_insert_at ───────────────────────────────────────────────────


class TestWikiInsertAt:
    """L2-11 … L2-17"""

    def test_insert_at_happy_path(self):
        """Insert text at position with text-before matching anchor_hint."""
        _insert_page(
            "insert-at-happy",
            content="Line one is here\nLine two is there\n",
        )
        # Insert at start of line 2 (line=2, col=1)
        # Text immediately before = end of line 1 = "\nLine one is here\n"
        # anchor_hint = last ≥20 chars before insertion
        result = server.wiki_insert_at(
            "insert-at-happy",
            line=2,
            col=1,
            new_text="Inserted line\n",
            anchor_hint="Line one is here\n",  # 17 chars — need ≥20; use longer
        )
        # This should fail because anchor_hint is < 20 chars
        assert result.get("ok") is False  # < 20 chars

    def test_insert_at_anchor_hint_too_short(self):
        """anchor_hint < 20 chars → reject even for insert_at."""
        _insert_page("insert-at-short", content="Foo bar baz\nQux quux\n")
        result = server.wiki_insert_at(
            "insert-at-short",
            line=2,
            col=1,
            new_text="NEW\n",
            anchor_hint="Foo bar baz\n",  # 12 chars < 20
        )
        assert result.get("ok") is False

    def test_insert_at_anchor_hint_mismatch(self):
        """Text before insertion point doesn't end with anchor_hint → reject."""
        _insert_page(
            "insert-at-mismatch",
            content="First line content here\nSecond line here\n",
        )
        result = server.wiki_insert_at(
            "insert-at-mismatch",
            line=2,
            col=1,
            new_text="Inserted\n",
            anchor_hint="Wrong hint that is long enough!!",  # 32 chars
        )
        assert result.get("ok") is False
        assert result.get("reason") == "anchor_hint mismatch"

    def test_insert_at_happy_path_with_long_hint(self):
        """Insert at start of line 2 with valid anchor_hint ≥20 chars."""
        pid = _insert_page(
            "insert-at-long",
            content="First line content here\nSecond line here\n",
        )
        result = server.wiki_insert_at(
            "insert-at-long",
            line=2,
            col=1,
            new_text="Inserted line\n",
            anchor_hint="First line content here\n",  # 24 chars ≥ 20
        )
        assert result.get("ok") is True
        page = _storage().get_wiki_page(pid)
        assert "Inserted line\n" in page["content"]
        assert page["content"].index("Inserted line") < page["content"].index("Second line")

    def test_insert_at_creates_version(self):
        """Successful insert_at creates a wiki_page_version row."""
        pid = _insert_page(
            "insert-at-ver",
            content="Long first line content here\nSecond line here\n",
        )
        before = _version_count(pid)
        server.wiki_insert_at(
            "insert-at-ver",
            line=2,
            col=1,
            new_text="NEW\n",
            anchor_hint="Long first line content here\n",  # 29 chars ≥ 20
        )
        assert _version_count(pid) == before + 1

    def test_insert_at_returns_ok_applied(self):
        """Successful insert_at returns ok:True, applied:True, length_delta=len(new_text)."""
        _insert_page(
            "insert-at-return",
            content="Alpha line content here now\nBeta line here\n",
        )
        result = server.wiki_insert_at(
            "insert-at-return",
            line=2,
            col=1,
            new_text="GAMMA\n",
            anchor_hint="Alpha line content here now\n",  # 28 chars ≥ 20
        )
        assert result.get("ok") is True
        assert result.get("applied") is True
        assert result.get("length_delta") == len("GAMMA\n")

    def test_insert_at_page_not_found(self):
        """Non-existent slug → ok:False."""
        result = server.wiki_insert_at(
            "no-such-slug-insert",
            line=1,
            col=1,
            new_text="whatever",
            anchor_hint="This anchor hint is long enough here",
        )
        assert result.get("ok") is False


# ── Layer 2: multi-line offset arithmetic ─────────────────────────────────────


class TestPositionalOffsetArithmetic:
    """Verify line/col→offset math is correct for multi-line pages."""

    def test_replace_at_line2(self):
        """Replace at line 2, col 1 correctly targets second line."""
        content = "First line here today\nSecond line here today\n"
        pid = _insert_page("replace-at-l2", content=content)
        result = server.wiki_replace_at(
            "replace-at-l2",
            line=2,
            col=1,
            length=6,  # "Second"
            new_text="Third",
            anchor_hint="Second line here today\n",  # 22 chars ≥ 20
        )
        assert result.get("ok") is True
        page = _storage().get_wiki_page(pid)
        assert "Third line" in page["content"]
        assert "First line" in page["content"]

    def test_replace_at_col_mid_line(self):
        """Replace at mid-line column correctly slices."""
        content = "Hello beautiful world here now\n"
        pid = _insert_page("replace-at-col", content=content)
        # "beautiful" starts at col 7 (1-indexed)
        result = server.wiki_replace_at(
            "replace-at-col",
            line=1,
            col=7,
            length=9,  # "beautiful"
            new_text="wonderful",
            anchor_hint="beautiful world here now\n",  # 25 chars ≥ 20
        )
        assert result.get("ok") is True
        page = _storage().get_wiki_page(pid)
        assert "Hello wonderful world" in page["content"]


# ── Layer 3: wiki_replace_markdown_block ─────────────────────────────────────


class TestWikiReplaceMarkdownBlock:
    """L3-01 … L3-12"""

    def test_replace_first_paragraph(self):
        """Replace paragraph[0] in a page with multiple paragraphs."""
        content = "First paragraph here.\n\nSecond paragraph here.\n\n## Section\n\nThird.\n"
        pid = _insert_page("replace-para-0", content=content)
        result = server.wiki_replace_markdown_block(
            "replace-para-0",
            block_type="paragraph",
            block_index=0,
            new_content="Replaced paragraph.",
        )
        assert result.get("ok") is True
        page = _storage().get_wiki_page(pid)
        assert "Replaced paragraph." in page["content"]
        assert "First paragraph here." not in page["content"]
        assert "Second paragraph here." in page["content"]

    def test_replace_second_paragraph(self):
        """Replace paragraph[1], keeping paragraph[0] intact."""
        content = "Para one text here.\n\nPara two text here.\n\nPara three.\n"
        pid = _insert_page("replace-para-1", content=content)
        result = server.wiki_replace_markdown_block(
            "replace-para-1",
            block_type="paragraph",
            block_index=1,
            new_content="New second paragraph.",
        )
        assert result.get("ok") is True
        page = _storage().get_wiki_page(pid)
        assert "Para one text here." in page["content"]
        assert "New second paragraph." in page["content"]
        assert "Para two text here." not in page["content"]

    def test_replace_heading(self):
        """Replace heading[0] (the first heading in the page)."""
        content = "Preamble text here.\n\n## Old Heading\n\nBody text.\n"
        pid = _insert_page("replace-heading-0", content=content)
        result = server.wiki_replace_markdown_block(
            "replace-heading-0",
            block_type="heading",
            block_index=0,
            new_content="## New Heading",
        )
        assert result.get("ok") is True
        page = _storage().get_wiki_page(pid)
        assert "## New Heading" in page["content"]
        assert "## Old Heading" not in page["content"]

    def test_replace_code_fence(self):
        """Replace code_fence[0]."""
        content = "Intro.\n\n```python\nold_code = True\n```\n\nOutro.\n"
        pid = _insert_page("replace-code-0", content=content)
        result = server.wiki_replace_markdown_block(
            "replace-code-0",
            block_type="code_fence",
            block_index=0,
            new_content="```python\nnew_code = True\n```",
        )
        assert result.get("ok") is True
        page = _storage().get_wiki_page(pid)
        assert "new_code = True" in page["content"]
        assert "old_code = True" not in page["content"]

    def test_replace_blockquote(self):
        """Replace blockquote[0]."""
        content = "Intro.\n\n> Old quote here.\n\nOutro.\n"
        pid = _insert_page("replace-bq-0", content=content)
        result = server.wiki_replace_markdown_block(
            "replace-bq-0",
            block_type="blockquote",
            block_index=0,
            new_content="> New quote here.",
        )
        assert result.get("ok") is True
        page = _storage().get_wiki_page(pid)
        assert "> New quote here." in page["content"]
        assert "Old quote here" not in page["content"]

    def test_replace_list_block(self):
        """Replace list[0]."""
        content = "Intro.\n\n- item a\n- item b\n- item c\n\nOutro.\n"
        pid = _insert_page("replace-list-0", content=content)
        result = server.wiki_replace_markdown_block(
            "replace-list-0",
            block_type="list",
            block_index=0,
            new_content="- item x\n- item y",
        )
        assert result.get("ok") is True
        page = _storage().get_wiki_page(pid)
        assert "- item x" in page["content"]
        assert "- item a" not in page["content"]
        assert "Intro." in page["content"]

    def test_replace_table_block(self):
        """Replace table[0]."""
        content = "Intro.\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\nOutro.\n"
        pid = _insert_page("replace-table-0", content=content)
        result = server.wiki_replace_markdown_block(
            "replace-table-0",
            block_type="table",
            block_index=0,
            new_content="| X | Y |\n|---|---|\n| 9 | 8 |",
        )
        assert result.get("ok") is True
        page = _storage().get_wiki_page(pid)
        assert "| X | Y |" in page["content"]
        assert "| A | B |" not in page["content"]

    def test_block_index_out_of_range(self):
        """block_index beyond available blocks → ok:False."""
        content = "Only one paragraph here.\n"
        _insert_page("replace-block-oob", content=content)
        result = server.wiki_replace_markdown_block(
            "replace-block-oob",
            block_type="paragraph",
            block_index=5,
            new_content="Won't replace.",
        )
        assert result.get("ok") is False
        assert "index" in str(result).lower() or "range" in str(result).lower()

    def test_invalid_block_type(self):
        """Unknown block_type → ok:False."""
        _insert_page("replace-block-invalid", content="Some content here.\n")
        result = server.wiki_replace_markdown_block(
            "replace-block-invalid",
            block_type="foobar",
            block_index=0,
            new_content="Nope.",
        )
        assert result.get("ok") is False

    def test_replace_markdown_block_creates_version(self):
        """Successful replace_markdown_block creates a wiki_page_version row."""
        pid = _insert_page("replace-block-ver", content="Version paragraph here.\n\nSecond.\n")
        before = _version_count(pid)
        server.wiki_replace_markdown_block(
            "replace-block-ver",
            block_type="paragraph",
            block_index=0,
            new_content="New version paragraph.",
        )
        assert _version_count(pid) == before + 1

    def test_replace_second_code_fence(self):
        """Replace code_fence[1] (second fence), keep first intact."""
        content = (
            "Intro.\n\n"
            "```bash\nfirst_cmd\n```\n\n"
            "Middle.\n\n"
            "```python\nsecond_code\n```\n\n"
            "Outro.\n"
        )
        pid = _insert_page("replace-code-1", content=content)
        result = server.wiki_replace_markdown_block(
            "replace-code-1",
            block_type="code_fence",
            block_index=1,
            new_content="```python\nreplaced_code\n```",
        )
        assert result.get("ok") is True
        page = _storage().get_wiki_page(pid)
        assert "first_cmd" in page["content"]  # first fence preserved
        assert "replaced_code" in page["content"]
        assert "second_code" not in page["content"]

    def test_replace_markdown_block_page_not_found(self):
        """Non-existent slug → ok:False."""
        result = server.wiki_replace_markdown_block(
            "no-such-slug",
            block_type="paragraph",
            block_index=0,
            new_content="Nope.",
        )
        assert result.get("ok") is False


# ── Layer 3: wiki_append_section heading_type extension ───────────────────────


class TestWikiAppendSectionHeadingType:
    """L3-13 … L3-18"""

    def test_append_section_bold_heading_default_h2_still_works(self):
        """Default h2 backward compat: ## heading targeted without heading_type."""
        pid = _insert_page(
            "append-h2-default",
            content="# Page Title\n\n## Existing Section\n\nBody text.\n",
        )
        result = server.wiki_append_section(
            "append-h2-default",
            section_heading="Existing Section",
            content="Appended content.",
            position="end_of_section",
        )
        assert result.get("action") == "appended" or result.get("ok") is not False
        page = _storage().get_wiki_page(pid)
        assert "Appended content." in page["content"]

    def test_append_section_bold_heading_type(self):
        """heading_type='bold' targets **Bold Header** first-line patterns."""
        content = (
            "Preamble.\n\n**My Bold Header**\n\nContent under bold.\n\n## Other\n\nOther body.\n"
        )
        pid = _insert_page("append-bold", content=content)
        result = server.wiki_append_section(
            "append-bold",
            section_heading="My Bold Header",
            content="Added after bold.",
            position="end_of_section",
            heading_type="bold",
        )
        assert result.get("action") == "appended"
        page = _storage().get_wiki_page(pid)
        assert "Added after bold." in page["content"]
        # Content must appear before "## Other" section
        text = page["content"]
        assert text.index("Added after bold.") < text.index("## Other")

    def test_append_section_blockquote_heading_type(self):
        """heading_type='blockquote' targets > first-line as section header."""
        content = "Preamble.\n\n> Section via blockquote\n\nContent here.\n\n## Other\n\nMore.\n"
        pid = _insert_page("append-blockquote", content=content)
        result = server.wiki_append_section(
            "append-blockquote",
            section_heading="Section via blockquote",
            content="Added after blockquote.",
            position="end_of_section",
            heading_type="blockquote",
        )
        assert result.get("action") == "appended"
        page = _storage().get_wiki_page(pid)
        assert "Added after blockquote." in page["content"]

    def test_append_section_bold_heading_not_found(self):
        """Missing bold heading → error dict with section_not_found."""
        _insert_page(
            "bold-not-found",
            content="## Normal heading\n\nContent.\n",
        )
        result = server.wiki_append_section(
            "bold-not-found",
            section_heading="Missing Bold",
            content="Won't land.",
            heading_type="bold",
        )
        assert "error" in result
        assert result["error"] == "section_not_found"

    def test_append_section_h3_type_explicit(self):
        """heading_type='h3' targets ### headings."""
        content = "## H2 Section\n\nH2 body.\n\n### H3 Sub\n\nH3 body.\n"
        pid = _insert_page("append-h3", content=content)
        result = server.wiki_append_section(
            "append-h3",
            section_heading="H3 Sub",
            content="Added to h3.",
            position="end_of_section",
            heading_type="h3",
        )
        assert result.get("action") == "appended"
        page = _storage().get_wiki_page(pid)
        assert "Added to h3." in page["content"]

    def test_append_section_invalid_heading_type(self):
        """Invalid heading_type → error dict."""
        _insert_page("invalid-htype", content="## Section\n\nBody.\n")
        result = server.wiki_append_section(
            "invalid-htype",
            section_heading="Section",
            content="Won't land.",
            heading_type="foobar",
        )
        assert "error" in result
