"""v5.61.0 Wiki Edit Primitives — TDD test suite.

Tests cover:
  Layer 4: wiki_set_metadata
    - L4-1: set directory_context happy path
    - L4-2: set branch (non-null)
    - L4-3: idempotent no-op (no version row created)
    - L4-4: invalid field rejects
    - L4-5: directory_context validation (relative path rejects, empty rejects)
    - L4-6: branch empty string rejects
    - L4-7: branch → null clears field; page resolves via IS NONE query
    - L4-8: version row created on real change

  Layer 1: wiki_replace_text, wiki_delete_text, wiki_insert_after, wiki_insert_before
    - L1-01: replace_text happy path (unique match, occurrences=1)
    - L1-02: replace_text count mismatch → ok:False reject
    - L1-03: replace_text occurrences='all'
    - L1-04: replace_text old==new → no-op (ok:True, replaced_count=0, no version)
    - L1-05: replace_text text absent (default occurrences=1) → reject (count 0≠1)
    - L1-06: delete_text happy path
    - L1-07: delete_text absent → no-op (ok:True, replaced_count=0, no version)
    - L1-08: delete_text count mismatch → ok:False reject
    - L1-09: insert_after happy path
    - L1-10: insert_after anchor absent → reject
    - L1-11: insert_after anchor non-unique → reject (no occurrences param)
    - L1-12: insert_before happy path
    - L1-13: insert_before anchor absent → reject
    - L1-14: version row created for each successful edit
    - L1-15: secret gate called on new_text (replace, insert)
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from yadgar import server
from yadgar.storage.migrations import _migration_013_wiki_page_version

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    """Embedded storage with isolated temp database per test."""
    server.init_engines(
        db_path=str(tmp_path / "wiki_edit_test.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    with patch("yadgar.server._detect_branch", return_value="feat/test-branch"):
        _migration_013_wiki_page_version(_storage())
        yield
    server.shutdown()


def _storage():
    return server._get_storage()


def _wiki():
    return server._wiki


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
    """Insert page directly via storage. Returns page_id."""
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


# ── Layer 4: wiki_set_metadata ────────────────────────────────────────────────


class TestWikiSetMetadata:
    def test_set_directory_context_happy_path(self):
        """Setting directory_context to an absolute path succeeds."""
        pid = _insert_page("set-dir-page", directory_context="global")
        result = server.wiki_set_metadata(
            "set-dir-page", "directory_context", "/home/max/projects/myapp"
        )
        assert result.get("ok") is True
        assert result.get("page_id") == pid
        page = _storage().get_wiki_page(pid)
        assert page["directory_context"] == "/home/max/projects/myapp"

    def test_set_branch_non_null(self):
        """Setting branch to a non-empty string succeeds."""
        pid = _insert_page("set-branch-page", branch=None)
        result = server.wiki_set_metadata("set-branch-page", "branch", "feat/my-feature")
        assert result.get("ok") is True
        page = _storage().get_wiki_page(pid)
        assert page["branch"] == "feat/my-feature"

    def test_idempotent_noop_directory_context(self):
        """Same directory_context value → ok:True, no version created."""
        pid = _insert_page("idempotent-dir", directory_context="/home/max/project")
        initial_versions = _version_count(pid)
        result = server.wiki_set_metadata(
            "idempotent-dir", "directory_context", "/home/max/project"
        )
        assert result.get("ok") is True
        assert result.get("changed") is False
        assert _version_count(pid) == initial_versions

    def test_idempotent_noop_branch(self):
        """Same branch value → ok:True, no version created."""
        pid = _insert_page("idempotent-branch", branch="feat/stable")
        initial_versions = _version_count(pid)
        # Pass branch_hint so §25 resolution locates the page
        result = server.wiki_set_metadata(
            "idempotent-branch", "branch", "feat/stable", branch_hint="feat/stable"
        )
        assert result.get("ok") is True
        assert result.get("changed") is False
        assert _version_count(pid) == initial_versions

    def test_invalid_field_rejects(self):
        """Unknown field returns ok:False."""
        _insert_page("invalid-field-page")
        result = server.wiki_set_metadata("invalid-field-page", "content", "hack")
        assert result.get("ok") is False
        assert "field" in result.get("error", "")

    def test_directory_context_relative_path_rejects(self):
        """Relative path for directory_context → ok:False."""
        _insert_page("relative-dir-page")
        result = server.wiki_set_metadata("relative-dir-page", "directory_context", "relative/path")
        assert result.get("ok") is False

    def test_directory_context_empty_rejects(self):
        """Empty string for directory_context → ok:False."""
        _insert_page("empty-dir-page")
        result = server.wiki_set_metadata("empty-dir-page", "directory_context", "")
        assert result.get("ok") is False

    def test_branch_empty_string_rejects(self):
        """Empty string for branch → ok:False (use null/None to clear)."""
        _insert_page("empty-branch-page")
        result = server.wiki_set_metadata("empty-branch-page", "branch", "")
        assert result.get("ok") is False

    def test_branch_null_clears_field(self):
        """Setting branch to null clears it; page resolves via IS NONE query."""
        pid = _insert_page("null-branch-page", branch="feat/old-branch")
        page = _storage().get_wiki_page(pid)
        assert page["branch"] == "feat/old-branch"

        # Pass branch_hint so §25 resolution finds the page before clearing
        result = server.wiki_set_metadata(
            "null-branch-page", "branch", None, branch_hint="feat/old-branch"
        )
        assert result.get("ok") is True

        # Verify branch is truly NONE (not null) — resolves via IS NONE query
        page = _storage().get_wiki_page(pid)
        assert page.get("branch") is None

        # Critical: page must resolve via IS NONE in §25 resolution
        resolved = _storage().get_wiki_page_by_slug_directory_branch(
            "null-branch-page", "global", "feat/test-branch"
        )
        assert resolved is not None
        assert resolved["id"] == pid

    def test_version_row_created_on_real_change(self):
        """Successful metadata change creates a new wiki_page_version row."""
        pid = _insert_page("version-check-meta", directory_context="global")
        before = _version_count(pid)
        server.wiki_set_metadata("version-check-meta", "directory_context", "/home/max/work")
        assert _version_count(pid) == before + 1

    def test_page_not_found_returns_error(self):
        """Non-existent slug returns error dict."""
        result = server.wiki_set_metadata("no-such-page", "directory_context", "global")
        assert result.get("ok") is False
        assert "not found" in result.get("error", "").lower()

    def test_directory_context_global_accepted(self):
        """'global' is valid for directory_context."""
        pid = _insert_page("global-dir-reset", directory_context="/home/max/project")
        result = server.wiki_set_metadata("global-dir-reset", "directory_context", "global")
        assert result.get("ok") is True
        page = _storage().get_wiki_page(pid)
        assert page["directory_context"] == "global"


# ── Layer 1: anchor-text primitives ───────────────────────────────────────────


class TestWikiReplaceText:
    def test_replace_unique_match(self):
        """Replace a uniquely occurring text snippet."""
        pid = _insert_page("replace-happy", content="Hello world. Foo bar.")
        result = server.wiki_replace_text("replace-happy", "Hello world", "Hi there")
        assert result.get("ok") is True
        assert result.get("replaced_count") == 1
        assert result.get("length_delta") == len("Hi there") - len("Hello world")
        page = _storage().get_wiki_page(pid)
        assert "Hi there" in page["content"]
        assert "Hello world" not in page["content"]

    def test_replace_count_mismatch_rejects(self):
        """old_text appears 3x but occurrences=1 → ok:False."""
        pid = _insert_page("replace-mismatch", content="foo foo foo")
        result = server.wiki_replace_text("replace-mismatch", "foo", "bar", occurrences=1)
        assert result.get("ok") is False
        assert (
            "mismatch" in result.get("error", "").lower()
            or "occurrences" in result.get("error", "").lower()
        )
        # Content must be unchanged
        page = _storage().get_wiki_page(pid)
        assert page["content"] == "foo foo foo"

    def test_replace_all_occurrences(self):
        """occurrences='all' replaces every match."""
        pid = _insert_page("replace-all", content="cat cat cat")
        result = server.wiki_replace_text("replace-all", "cat", "dog", occurrences="all")
        assert result.get("ok") is True
        assert result.get("replaced_count") == 3
        page = _storage().get_wiki_page(pid)
        assert page["content"] == "dog dog dog"

    def test_replace_noop_same_text(self):
        """old_text == new_text → ok:True, replaced_count=0, no new version."""
        pid = _insert_page("replace-noop", content="unchanged content")
        before = _version_count(pid)
        result = server.wiki_replace_text("replace-noop", "unchanged", "unchanged")
        assert result.get("ok") is True
        assert result.get("replaced_count") == 0
        assert _version_count(pid) == before

    def test_replace_absent_text_rejects(self):
        """Text absent, default occurrences=1 → reject (count 0 ≠ 1)."""
        _insert_page("replace-absent", content="some content here")
        result = server.wiki_replace_text("replace-absent", "missing text", "replacement")
        assert result.get("ok") is False

    def test_replace_explicit_count_matches(self):
        """occurrences=2 with exactly 2 matches succeeds."""
        pid = _insert_page("replace-explicit", content="foo bar foo")
        result = server.wiki_replace_text("replace-explicit", "foo", "baz", occurrences=2)
        assert result.get("ok") is True
        assert result.get("replaced_count") == 2
        page = _storage().get_wiki_page(pid)
        assert page["content"] == "baz bar baz"

    def test_replace_creates_version(self):
        """Successful replace creates a new wiki_page_version."""
        pid = _insert_page("replace-version", content="alpha beta gamma")
        before = _version_count(pid)
        server.wiki_replace_text("replace-version", "beta", "delta")
        assert _version_count(pid) == before + 1

    def test_replace_returns_version_id(self):
        """Result includes version_id (new version number)."""
        _insert_page("replace-ver-id", content="one two three")
        result = server.wiki_replace_text("replace-ver-id", "two", "TWO")
        assert result.get("ok") is True
        assert "version_id" in result
        assert isinstance(result["version_id"], int)
        assert result["version_id"] >= 2

    def test_replace_page_not_found(self):
        """Non-existent slug returns ok:False."""
        result = server.wiki_replace_text("no-such-slug", "old", "new")
        assert result.get("ok") is False


class TestWikiDeleteText:
    def test_delete_happy_path(self):
        """Delete a unique text snippet."""
        pid = _insert_page("delete-happy", content="Keep this. Remove this. Keep rest.")
        result = server.wiki_delete_text("delete-happy", "Remove this. ")
        assert result.get("ok") is True
        assert result.get("replaced_count") == 1
        assert result.get("length_delta") < 0
        page = _storage().get_wiki_page(pid)
        assert "Remove this." not in page["content"]
        assert "Keep this." in page["content"]

    def test_delete_absent_is_noop(self):
        """Absent text → no-op: ok:True, replaced_count=0, no version."""
        pid = _insert_page("delete-absent", content="nothing to delete here")
        before = _version_count(pid)
        result = server.wiki_delete_text("delete-absent", "missing phrase")
        assert result.get("ok") is True
        assert result.get("replaced_count") == 0
        assert _version_count(pid) == before

    def test_delete_count_mismatch_rejects(self):
        """text appears 2x, occurrences=1 → reject."""
        _insert_page("delete-mismatch", content="dup dup")
        result = server.wiki_delete_text("delete-mismatch", "dup", occurrences=1)
        assert result.get("ok") is False

    def test_delete_all(self):
        """occurrences='all' deletes every match."""
        pid = _insert_page("delete-all", content="x and x and x")
        result = server.wiki_delete_text("delete-all", "x", occurrences="all")
        assert result.get("ok") is True
        assert result.get("replaced_count") == 3
        page = _storage().get_wiki_page(pid)
        assert "x" not in page["content"]

    def test_delete_creates_version(self):
        """Successful delete creates a new wiki_page_version."""
        pid = _insert_page("delete-version", content="keep this remove that")
        before = _version_count(pid)
        server.wiki_delete_text("delete-version", "remove that")
        assert _version_count(pid) == before + 1


class TestWikiInsertAfter:
    def test_insert_after_happy_path(self):
        """Insert text after a unique anchor."""
        pid = _insert_page("insert-after-happy", content="Line one.\nLine two.\n")
        result = server.wiki_insert_after("insert-after-happy", "Line one.", "\nInserted line.")
        assert result.get("ok") is True
        assert result.get("replaced_count") == 1
        page = _storage().get_wiki_page(pid)
        assert "Line one.\nInserted line.\nLine two." in page["content"]

    def test_insert_after_anchor_absent_rejects(self):
        """Anchor not found → ok:False."""
        _insert_page("insert-after-absent", content="only this line")
        result = server.wiki_insert_after("insert-after-absent", "missing anchor", "new text")
        assert result.get("ok") is False

    def test_insert_after_non_unique_anchor_rejects(self):
        """Anchor appears more than once → ok:False (no occurrences param)."""
        _insert_page("insert-after-dup", content="dup line\ndup line\n")
        result = server.wiki_insert_after("insert-after-dup", "dup line", "\nnew")
        assert result.get("ok") is False

    def test_insert_after_creates_version(self):
        """Successful insert_after creates a new wiki_page_version."""
        pid = _insert_page("insert-after-ver", content="anchor text here")
        before = _version_count(pid)
        server.wiki_insert_after("insert-after-ver", "anchor text", " added")
        assert _version_count(pid) == before + 1

    def test_insert_after_returns_length_delta(self):
        """length_delta equals len(new_text)."""
        _insert_page("insert-after-delta", content="some anchor content")
        result = server.wiki_insert_after("insert-after-delta", "some anchor", " ADDED")
        assert result.get("ok") is True
        assert result.get("length_delta") == len(" ADDED")


class TestWikiInsertBefore:
    def test_insert_before_happy_path(self):
        """Insert text before a unique anchor."""
        pid = _insert_page("insert-before-happy", content="Line one.\nLine two.\n")
        result = server.wiki_insert_before("insert-before-happy", "Line two.", "Inserted.\n")
        assert result.get("ok") is True
        page = _storage().get_wiki_page(pid)
        assert "Inserted.\nLine two." in page["content"]

    def test_insert_before_anchor_absent_rejects(self):
        """Anchor not found → ok:False."""
        _insert_page("insert-before-absent", content="only this line")
        result = server.wiki_insert_before("insert-before-absent", "missing anchor", "new text")
        assert result.get("ok") is False

    def test_insert_before_non_unique_anchor_rejects(self):
        """Anchor appears more than once → ok:False."""
        _insert_page("insert-before-dup", content="dup\ndup\n")
        result = server.wiki_insert_before("insert-before-dup", "dup", "prefix\n")
        assert result.get("ok") is False

    def test_insert_before_creates_version(self):
        """Successful insert_before creates a new wiki_page_version."""
        pid = _insert_page("insert-before-ver", content="anchor content here")
        before = _version_count(pid)
        server.wiki_insert_before("insert-before-ver", "anchor content", "PREFIX ")
        assert _version_count(pid) == before + 1


class TestEditPrimitivesSecretGate:
    """I26: secret gate on new_text for write ops; skip for delete."""

    def test_replace_text_gate_called(self):
        """gate_or_reject called on new_text for wiki_replace_text."""
        _insert_page("gate-replace", content="old text here")
        with patch("yadgar.server.tools.wiki.gate_or_reject") as mock_gate:
            mock_gate.return_value = None  # allow
            server.wiki_replace_text("gate-replace", "old text", "new text")
            mock_gate.assert_called()

    def test_insert_after_gate_called(self):
        """gate_or_reject called on new_text for wiki_insert_after."""
        _insert_page("gate-insert", content="anchor here")
        with patch("yadgar.server.tools.wiki.gate_or_reject") as mock_gate:
            mock_gate.return_value = None
            server.wiki_insert_after("gate-insert", "anchor", " appended")
            mock_gate.assert_called()

    def test_delete_text_gate_not_called(self):
        """gate_or_reject NOT called for wiki_delete_text (nothing new written)."""
        _insert_page("gate-delete", content="delete this text")
        with patch("yadgar.server.tools.wiki.gate_or_reject") as mock_gate:
            mock_gate.return_value = None
            server.wiki_delete_text("gate-delete", "delete this text")
            mock_gate.assert_not_called()
