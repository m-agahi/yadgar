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

from yadgar._shared.storage.migrations import _migration_013_wiki_page_version
from yadgar.core import server
from yadgar.tests.core.conftest import TEST_PROJECT_ID

# R3 Car 3c: the wiki-edit primitives (replace_text, delete_text, insert_after,
# insert_before, set_metadata, etc.) forward their DB write to the backend /admin
# endpoint. Route _forward_admin → run_admin_op directly (no HTTP) so tests
# exercise the real storage write without a running backend server.
pytestmark = pytest.mark.usefixtures("admin_backend_bypass")

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    """Storage initialized ONCE per module (v5.101 P1 module-scope).

    Per-test DATA isolation is provided by conftest's function-scoped
    `_wipe_surrealdb_data`; uses tmp_path_factory (a module-scoped fixture
    cannot request the function-scoped tmp_path).
    """
    tmp_path = tmp_path_factory.mktemp("wiki_edit")
    server.init_engines(
        db_path=str(tmp_path / "wiki_edit_test.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
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
):
    """Insert page directly via storage. Returns page_id.

    ``project_id`` is stamped from the module constant rather than exposed as
    a ninth parameter (the I13 arg cap): C5 made an unstamped insert raise, and
    no test in this file needs a second owner. Ownership is not the same axis
    as ``directory_context``, so both are written.
    """
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
            "project_id": TEST_PROJECT_ID,
            "directory_context": directory_context,
        }
    )


def _insert_page_owned(slug, project_id, directory_context="global"):
    """Insert a page under an EXPLICIT ``project_id``. Returns page_id.

    ``_insert_page`` stamps the module constant, which is right for every test
    whose subject is not ownership. Task 246's subject IS ownership: the
    all-rows restamp has to prove it reaches rows that disagree about who owns
    them, so those rows must be seedable with different ``project_id`` values.
    A separate 3-arg helper rather than a ninth parameter on ``_insert_page``
    (the I13 arg cap its docstring names).
    """
    return _storage().insert_wiki_page(
        {
            "slug": slug,
            "title": f"Test page {slug}",
            "content": f"content for {slug} owned by {project_id}",
            "category": "reference",
            "tags": [],
            "confidence": "medium",
            "source_memory_ids": [],
            "links": [],
            "project_id": project_id,
            "directory_context": directory_context,
        }
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


# ── Layer 4: wiki_set_metadata(field="project_id") — ledger task 246 ──────────


class TestWikiSetMetadataProjectId:
    """``project_id`` is settable — ADR-0233 made it the sole scoping key.

    Before task 246 the tool accepted ``directory_context`` ALONE, so there was
    no MCP path of any kind to correct a mis-stamped ``project_id`` on an
    existing page (``wiki_add`` with ``replace_slug`` / ``force`` / ``upsert``
    all update the row without restamping it). The only remaining option was
    delete-and-recreate.
    """

    def test_set_project_id_happy_path(self):
        """Setting project_id to a real owner/repo key succeeds and is stored."""
        pid = _insert_page_owned("set-pid-page", "wrong-owner/wrong-repo")
        result = server.wiki_set_metadata("set-pid-page", "project_id", "m-agahi/yadgar")
        assert result.get("ok") is True
        assert _storage().get_wiki_page(pid)["project_id"] == "m-agahi/yadgar"

    def test_set_project_id_updates_all_rows_for_slug(self):
        """ONE call restamps EVERY row sharing the slug, straggler included.

        The discriminating test. Two rows, same slug, DIFFERENT owners and
        different directory_context (one of them the 'global' straggler the
        BC-G10 all-rows path exists for). Reds if the write reaches only the
        row §25 resolution would have returned.
        """
        slug = "task246-allrows-pid"
        pid_straggler = _insert_page_owned(slug, "wrong-owner/wrong-repo", "global")
        pid_scoped = _insert_page_owned(slug, "other-owner/other-repo", "/home/max/project")
        assert pid_straggler != pid_scoped

        result = server.wiki_set_metadata(slug, "project_id", "m-agahi/yadgar")

        assert result.get("ok") is True
        assert sorted(result.get("page_ids", [])) == sorted([pid_straggler, pid_scoped])
        # Success-shaped-but-zero-writes is the failure mode this pins: the
        # all-rows loop only counts rows whose per-row write returned changed.
        assert result.get("rows_updated") == 2
        for pid in (pid_straggler, pid_scoped):
            assert _storage().get_wiki_page(pid)["project_id"] == "m-agahi/yadgar", (
                f"row page_id={pid} was not restamped"
            )

    def test_idempotent_noop_project_id(self):
        """Same project_id twice → ok:True, changed:False, no second version row."""
        pid = _insert_page_owned("idempotent-pid", "m-agahi/yadgar")
        before = _version_count(pid)
        result = server.wiki_set_metadata("idempotent-pid", "project_id", "m-agahi/yadgar")
        assert result.get("ok") is True
        assert result.get("changed") is False
        assert _version_count(pid) == before

    def test_version_row_created_on_real_project_id_change(self):
        """A real project_id change mints a wiki_page_version row."""
        pid = _insert_page_owned("version-check-pid", "wrong-owner/wrong-repo")
        before = _version_count(pid)
        server.wiki_set_metadata("version-check-pid", "project_id", "m-agahi/yadgar")
        assert _version_count(pid) == before + 1

    def test_project_id_empty_rejects(self):
        """Empty string names no project → ok:False."""
        _insert_page_owned("empty-pid-page", "m-agahi/yadgar")
        result = server.wiki_set_metadata("empty-pid-page", "project_id", "")
        assert result.get("ok") is False
        assert "project_id" in result.get("error", "")

    def test_project_id_none_rejects(self):
        """None would null the column and make the page unreachable → ok:False."""
        pid = _insert_page_owned("none-pid-page", "m-agahi/yadgar")
        result = server.wiki_set_metadata("none-pid-page", "project_id", None)
        assert result.get("ok") is False
        assert _storage().get_wiki_page(pid)["project_id"] == "m-agahi/yadgar"

    def test_project_id_non_string_rejects(self):
        """A non-string value is rejected rather than coerced."""
        _insert_page_owned("nonstr-pid-page", "m-agahi/yadgar")
        result = server.wiki_set_metadata("nonstr-pid-page", "project_id", 42)  # type: ignore[arg-type]
        assert result.get("ok") is False

    @pytest.mark.parametrize("sentinel", ["global", "unresolved", "system"])
    def test_project_id_sentinel_rejects(self, sentinel):
        """The ADR-0227 manufactured identities are not settable values.

        Global REACH is the Car C7 tag, not ``project_id='global'`` — writing
        the sentinel here mints exactly the phantom identity ADR-0227 deletes.
        """
        pid = _insert_page_owned(f"sentinel-pid-{sentinel}", "m-agahi/yadgar")
        result = server.wiki_set_metadata(f"sentinel-pid-{sentinel}", "project_id", sentinel)
        assert result.get("ok") is False
        assert _storage().get_wiki_page(pid)["project_id"] == "m-agahi/yadgar"

    def test_invalid_field_message_names_current_key_and_marks_legacy(self):
        """The rejection message must not steer a caller onto the retired key.

        The original message read ``allowed: ['directory_context']`` — so a
        caller who asked for ``project_id`` (the ADR-0233 scoping key) was told
        by the tool itself to use the concept ADR-0233 retired. Asserting on
        the message text because the message IS the defect.
        """
        _insert_page_owned("bad-field-page", "m-agahi/yadgar")
        error = server.wiki_set_metadata("bad-field-page", "content", "hack").get("error", "")
        assert "project_id" in error
        assert "directory_context" in error
        assert "legacy" in error.lower()

    def test_locked_adr_page_is_restampable(self):
        """A ``page_type='adr'`` page restamps — the re-key's main cohort.

        ADR body pages resolve to effective mutability ``locked``, so Car J's
        gate refuses ``insert_wiki_page`` / ``update_wiki_page`` /
        ``delete_wiki_page`` for every unsanctioned caller. The metadata write
        does NOT go through those: ``set_wiki_page_metadata`` issues its own
        ``UPDATE type::record('wiki_page', $pid)``, so it never reaches
        ``enforce_mutability``. Pinned as a test rather than left as an
        accident, because ledger task 41's corpus is mostly ADR pages and
        "does the restamp path work on a locked page" is the question that
        decides whether this fix unblocks it at all.
        """
        pid = _storage().insert_wiki_page(
            {
                "slug": "locked-adr-restamp",
                "title": "Locked ADR page",
                "content": "adr body",
                "category": "decision",
                "tags": [],
                "confidence": "high",
                "source_memory_ids": [],
                "links": [],
                "project_id": "wrong-owner/wrong-repo",
                "directory_context": "global",
                "page_type": "adr",
                # Seeding only — insert_wiki_page IS gated, the metadata write is not.
                "_sanctioned": True,
            }
        )
        result = server.wiki_set_metadata("locked-adr-restamp", "project_id", "m-agahi/yadgar")
        assert result.get("ok") is True, f"locked ADR page refused the restamp: {result}"
        assert _storage().get_wiki_page(pid)["project_id"] == "m-agahi/yadgar"

    def test_mcp_gate_matches_the_store_allowlist(self):
        """The MCP field gate and ``WikiStore._METADATA_FIELDS`` name one set.

        The shell rejects before forwarding, so its accepted set is hand-written
        there (the store's ``sorted(frozenset)`` cannot say which member is
        current and which is legacy). Two hand-maintained copies drift: adding a
        third field to the frozenset alone would leave it silently rejected at
        the MCP boundary, by a message that does not mention it.
        """
        from yadgar._shared.wiki.store import WikiStore

        accepted = {
            f
            for f in ("project_id", "directory_context", "branch", "content")
            if "invalid field"
            not in server.wiki_set_metadata("no-such-page", f, "x").get("error", "")
        }
        assert accepted == set(WikiStore._METADATA_FIELDS)

    def test_directory_context_still_settable(self):
        """Regression guard: widening the allowlist is ADDITIVE."""
        pid = _insert_page_owned("regression-dir-page", "m-agahi/yadgar", "global")
        result = server.wiki_set_metadata(
            "regression-dir-page", "directory_context", "/home/max/projects/myapp"
        )
        assert result.get("ok") is True
        page = _storage().get_wiki_page(pid)
        assert page["directory_context"] == "/home/max/projects/myapp"
        assert page["project_id"] == "m-agahi/yadgar"


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
        with patch("yadgar.core.server.tools.wiki.gate_or_reject") as mock_gate:
            mock_gate.return_value = None  # allow
            server.wiki_replace_text("gate-replace", "old text", "new text")
            mock_gate.assert_called()

    def test_insert_after_gate_called(self):
        """gate_or_reject called on new_text for wiki_insert_after."""
        _insert_page("gate-insert", content="anchor here")
        with patch("yadgar.core.server.tools.wiki.gate_or_reject") as mock_gate:
            mock_gate.return_value = None
            server.wiki_insert_after("gate-insert", "anchor", " appended")
            mock_gate.assert_called()

    def test_delete_text_gate_not_called(self):
        """gate_or_reject NOT called for wiki_delete_text (nothing new written)."""
        _insert_page("gate-delete", content="delete this text")
        with patch("yadgar.core.server.tools.wiki.gate_or_reject") as mock_gate:
            mock_gate.return_value = None
            server.wiki_delete_text("gate-delete", "delete this text")
            mock_gate.assert_not_called()
