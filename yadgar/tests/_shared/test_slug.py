"""Tests for yadgar._shared.wiki.slug — shared pure slugify function.

Car A of #83 (repo-wiki page-type).

Parity suite: assert `slugify()` produces byte-for-byte identical output to
the old `WikiStore._slugify` inline.  Expected values were captured from the
EXACT original expression:
    re.sub(r"[^a-z0-9]+", "-", html.unescape(title).lower()).strip("-")[:64]
    → "untitled" when empty.

After delegation `WikiStore._slugify` calls `slugify(title)`, so the
delegation-proof assertion is included as a sanity check.
"""

from __future__ import annotations

# ── Parity: import must succeed (RED until slug.py created) ──────────────────
from yadgar._shared.wiki.slug import slugify  # noqa: E402

# ── A. Parity with old WikiStore._slugify inline ──────────────────────────────


class TestSlugifyParity:
    """slugify() matches the original inline expression byte-for-byte."""

    def test_plain_title(self):
        assert slugify("Hello World") == "hello-world"

    def test_special_characters(self):
        assert slugify("Auth (v2) — Design!") == "auth-v2-design"

    def test_max_length_capped_at_64(self):
        slug = slugify("a" * 100)
        assert slug == "a" * 64

    def test_empty_string_returns_untitled(self):
        assert slugify("") == "untitled"

    def test_numeric_only_title(self):
        assert slugify("123") == "123"

    def test_html_entity_amp_unescaped(self):
        """&amp; must not produce 'amp' — html.unescape runs before slugification."""
        assert slugify("Yadgar Roadmap &amp; Future Improvements") == (
            "yadgar-roadmap-future-improvements"
        )

    def test_amp_entity_and_raw_ampersand_identical(self):
        """&amp; and & must produce the same slug."""
        assert slugify("Foo &amp; Bar") == slugify("Foo & Bar")
        # Both are "foo-bar"
        assert slugify("Foo &amp; Bar") == "foo-bar"

    def test_unicode_letters_collapsed(self):
        """Non-ASCII letters outside [a-z0-9] collapse to a single '-'."""
        # 'Café Über' → unescape → lowercase → 'café über' → non-alnum → '-'
        # 'c' 'a' 'f' + é→'-' + ' '->'.' + 'b' 'e' 'r' → "caf-ber"
        assert slugify("Café Über") == "caf-ber"

    def test_dots_and_underscores(self):
        assert slugify("dots.and_underscores here") == "dots-and-underscores-here"

    def test_already_a_slug(self):
        assert slugify("already-a-slug") == "already-a-slug"

    def test_leading_trailing_whitespace(self):
        assert slugify("  leading and trailing  ") == "leading-and-trailing"

    def test_long_unicode_title_capped_at_64(self):
        """64-char cap applies after collapsing unicode runs."""
        long_title = "Héllo Wörld " * 20  # well over 64 chars
        result = slugify(long_title)
        assert len(result) <= 64

    def test_whitespace_only_title(self):
        """A title of only whitespace → 'untitled' after strip."""
        assert slugify("   ") == "untitled"


# ── B. Delegation proof (post-implementation) ─────────────────────────────────


class TestWikiStoreDelegates:
    """WikiStore._slugify delegates to slugify() (no separate logic)."""

    def test_method_matches_fn(self):
        """_slugify(title) == slugify(title) for a sample set."""
        import os

        # We need an initialised WikiStore; use a temp db
        import tempfile

        from yadgar.core import server

        with tempfile.TemporaryDirectory() as tmp:
            server.init_engines(
                db_path=os.path.join(tmp, "delegation_test.db"),
                embedding_model="all-MiniLM-L6-v2",
            )
            try:
                wiki = server._wiki
                for title in ["Hello World", "Auth &amp; Design", "", "a" * 100]:
                    assert wiki._slugify(title) == slugify(title), (
                        f"Delegation mismatch for {title!r}"
                    )
            finally:
                server.shutdown()
