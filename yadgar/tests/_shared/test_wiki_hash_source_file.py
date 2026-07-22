"""Car B0 (#83): hash / source_file persist through the wiki write path.

The storage column exists (``_shared/storage/wiki.py`` insert/update) but
``WikiAddOptions`` never carried ``hash``/``source_file`` and ``WikiStore.add``
never emitted them → ``wiki_add(hash=..., source_file=...)`` was a silent no-op.
Car B's ``--stale-only`` needs the stored hash to diff, so B0 forwards them.

Covers (direct WikiStore layer — the drainer round-trip lives in
``tests/core/test_wiki_add_wait.py``):
  - add() with hash/source_file → persisted to storage
  - add() upsert WITHOUT re-passing hash → does NOT clobber the stored hash
  - add() upsert WITH a new hash → hash updated (Car B regen path)
  - bulk slug→hash read returns the repo-wiki pages, directory-scoped
"""

from __future__ import annotations

import pytest

from yadgar._shared.wiki.contract import WikiAddOptions
from yadgar.core import server

_REPO_DIR = "/home/max/git/yadgar"


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("wiki_hash_source")
    server.init_engines(
        db_path=str(tmp_path / "wiki_hash_test.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


def _wiki():
    return server._wiki


def _storage():
    return server._wiki._storage


# ── A. persist on insert ─────────────────────────────────────────────────────


class TestHashSourceFilePersist:
    def test_add_with_hash_source_file_persisted(self):
        """add(hash=..., source_file=...) survives the storage round-trip."""
        _wiki().add(
            "Mod Alpha",
            "## Purpose\nA.\n## Exports\nfoo\n## Design\nX.",
            category="reference",
            opts=WikiAddOptions(
                page_type="module",
                directory_context=_REPO_DIR,
                hash="abc123",
                source_file="/x/mod_alpha.py",
            ),
        )
        page = _storage().get_wiki_page_by_slug("mod-alpha")
        assert page is not None
        assert page.get("hash") == "abc123"
        assert page.get("source_file") == "/x/mod_alpha.py"

    def test_add_returns_hash_source_file(self):
        """The dict returned by add() carries the fields (no round-trip read needed)."""
        result = _wiki().add(
            "Mod Return",
            "## Purpose\nR.\n## Exports\nbar\n## Design\nY.",
            category="reference",
            opts=WikiAddOptions(
                page_type="module",
                directory_context=_REPO_DIR,
                hash="ret999",
                source_file="/x/mod_return.py",
            ),
        )
        assert result.get("hash") == "ret999"
        assert result.get("source_file") == "/x/mod_return.py"

    def test_add_without_hash_backward_compat(self):
        """A normal add() without hash/source_file still works and stores neither."""
        _wiki().add("Plain Page NoHash", "content", category="reference")
        page = _storage().get_wiki_page_by_slug("plain-page-nohash")
        assert page is not None
        assert page.get("hash") is None
        assert page.get("source_file") is None


# ── B. upsert clobber-guard + update ─────────────────────────────────────────


class TestHashUpsert:
    def test_upsert_without_hash_preserves_stored_hash(self):
        """A plain re-add of an existing repo-wiki page must NOT null its stored hash."""
        _wiki().add(
            "Mod Keep",
            "## Purpose\nfirst.\n## Exports\nk\n## Design\nZ.",
            category="reference",
            opts=WikiAddOptions(
                page_type="module",
                directory_context=_REPO_DIR,
                hash="keep111",
                source_file="/x/mod_keep.py",
            ),
        )
        # Re-add WITHOUT hash/source_file (e.g. a generic wiki edit).
        _wiki().add(
            "Mod Keep",
            "## Purpose\nsecond.\n## Exports\nk\n## Design\nZ2.",
            category="reference",
            opts=WikiAddOptions(page_type="module", directory_context=_REPO_DIR),
        )
        page = _storage().get_wiki_page_by_slug("mod-keep")
        assert page is not None
        assert page.get("hash") == "keep111", "stored hash was clobbered by a hashless upsert"
        assert page.get("source_file") == "/x/mod_keep.py"

    def test_upsert_with_new_hash_updates(self):
        """Regen (Car B) re-persists a new hash on an existing page."""
        _wiki().add(
            "Mod Drift",
            "## Purpose\nv1.\n## Exports\nd\n## Design\nA.",
            category="reference",
            opts=WikiAddOptions(
                page_type="module",
                directory_context=_REPO_DIR,
                hash="old000",
                source_file="/x/mod_drift.py",
            ),
        )
        _wiki().add(
            "Mod Drift",
            "## Purpose\nv2 edited.\n## Exports\nd\n## Design\nA2.",
            category="reference",
            opts=WikiAddOptions(
                page_type="module",
                directory_context=_REPO_DIR,
                hash="new777",
                source_file="/x/mod_drift.py",
            ),
        )
        page = _storage().get_wiki_page_by_slug("mod-drift")
        assert page is not None
        assert page.get("hash") == "new777"


# ── C. bulk slug→hash read (host-callable, one call, directory-scoped) ────────


class TestBulkHashRead:
    def test_wiki_list_includes_hash(self):
        """wiki_list output carries the hash field so --stale-only diffs in one call."""
        from yadgar.core.server.tools.wiki import wiki_list

        _wiki().add(
            "Mod Listed",
            "## Purpose\nL.\n## Exports\nl\n## Design\nB.",
            category="reference",
            opts=WikiAddOptions(
                page_type="module",
                directory_context=_REPO_DIR,
                hash="listed42",
                source_file="/x/mod_listed.py",
            ),
        )
        rows = wiki_list(directory=_REPO_DIR)
        by_slug = {r["slug"]: r for r in rows}
        assert "mod-listed" in by_slug
        assert by_slug["mod-listed"].get("hash") == "listed42"

    def test_bulk_hashes_returns_slug_hash_map(self):
        """repo_wiki_hashes(directory) → {slug: hash} for pages that carry a hash."""
        _wiki().add(
            "Mod Bulk One",
            "## Purpose\nB1.\n## Exports\nb\n## Design\nC.",
            category="reference",
            opts=WikiAddOptions(
                page_type="module",
                directory_context=_REPO_DIR,
                hash="bulk-one",
                source_file="/x/mod_bulk_one.py",
            ),
        )
        mapping = _wiki().repo_wiki_hashes(directory=_REPO_DIR)
        assert isinstance(mapping, dict)
        assert mapping.get("mod-bulk-one") == "bulk-one"
        # Pages without a hash are excluded from the map.
        _wiki().add("Bulk No Hash Page", "plain", category="reference")
        mapping2 = _wiki().repo_wiki_hashes(directory=_REPO_DIR)
        assert "bulk-no-hash-page" not in mapping2
