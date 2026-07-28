"""Car B (#83): directory-scoped similarity gate.

Seam 1 — directory scoping (folds the general cross-project gate fix):
    ``find_similar_wiki_pages`` filters candidates by ``directory_context`` (via
    ``is_directory_eligible``).  A candidate page in an UNRELATED project
    directory is NOT a duplicate — the cross-project ``logging.py`` collision.

(Seam 2 — the type-aware identity gate that skipped content-similarity for
``page_type=repo_wiki`` and ran ``validate_repo_wiki_page`` instead — was
removed along with repo_wiki's decommission, #33/ADR-0162. No page_type sets
``gate_mode="identity"`` any more, so every wiki_add runs the similarity gate
below.)

Embedding independence
----------------------
The Seam-1 candidate-filter tests inject a fake KNN result + fake query encoder
onto the WikiStore so the directory filter is exercised deterministically —
they do NOT depend on the real embedding model (which requires
sentence-transformers + native libs that may be absent in a bare sandbox).
The positive control (`test_unscoped_finds_candidate`) fails LOUDLY if the
injected candidate is not surfaced, so a dir-filter regression cannot pass
vacuously.
"""

from __future__ import annotations

import pytest

from yadgar.core import server

_DIR_A = "/proj/alpha"
_DIR_B = "/proj/beta"


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("wiki_gate_dir_identity")
    server.init_engines(
        db_path=str(tmp_path / "gate_test.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


def _wiki():
    return server._wiki


# ── Seam 1: directory scoping (embedding-independent, injected candidate) ─────


class TestGateDirectoryScope:
    """Prove find_similar_wiki_pages applies the directory filter.

    A single fake candidate page (slug 'alpha-logging', directory_context
    '/proj/alpha') is injected via monkeypatched storage/embeddings so the
    KNN + similarity threshold always surface it. The ONLY variable is the
    caller's directory_context passed into find_similar_wiki_pages.
    """

    _FAKE_PAGE = {
        "id": 4242,
        "slug": "alpha-logging",
        "title": "Logging Module A",
        "branch": None,
        "directory_context": _DIR_A,
    }

    @pytest.fixture(autouse=True)
    def _inject(self, monkeypatch):
        w = _wiki()
        # Query encoder returns a non-None sentinel so the None-guard passes.
        monkeypatch.setattr(w._embeddings, "encode_query", lambda text: b"\x00" * 4)
        # KNN returns our fake page id at distance 0.05 → similarity 0.95 (> 0.80).
        monkeypatch.setattr(w._storage, "search_wiki_vectors", lambda emb, top_k=5: [(4242, 0.05)])
        monkeypatch.setattr(
            w._storage, "get_wiki_page", lambda pid: dict(self._FAKE_PAGE) if pid == 4242 else None
        )

    def test_unscoped_finds_candidate(self):
        """Positive control — no dir filter → the candidate IS surfaced.

        This asserts the injected fixture is wired correctly; if it fails, the
        negative test below is meaningless (would pass vacuously).
        """
        hits = _wiki().find_similar_wiki_pages(
            title="B", content="anything", directory_context=None
        )
        assert any(c["slug"] == "alpha-logging" for c in hits), (
            "positive control failed — injected candidate not surfaced unscoped"
        )

    def test_same_dir_still_finds_candidate(self):
        """Caller in the SAME directory → candidate still a duplicate."""
        hits = _wiki().find_similar_wiki_pages(
            title="B", content="anything", directory_context=_DIR_A
        )
        assert any(c["slug"] == "alpha-logging" for c in hits), (
            "same-directory candidate must still be found (similarity mode unchanged)"
        )

    def test_cross_dir_filters_candidate(self):
        """Caller in a DIFFERENT directory → candidate filtered out (Seam 1)."""
        hits = _wiki().find_similar_wiki_pages(
            title="B", content="anything", directory_context=_DIR_B
        )
        assert not any(c["slug"] == "alpha-logging" for c in hits), (
            "cross-directory candidate was NOT filtered — directory scoping missing"
        )

    def test_sentinel_global_candidate_always_eligible(self):
        """A candidate with directory_context='global' is eligible from any caller."""
        w = _wiki()
        # Re-point the fake page to a 'global' directory_context.
        global_page = dict(self._FAKE_PAGE)
        global_page["directory_context"] = "global"
        w._storage.get_wiki_page = lambda pid: dict(global_page) if pid == 4242 else None
        hits = w.find_similar_wiki_pages(title="B", content="anything", directory_context=_DIR_B)
        assert any(c["slug"] == "alpha-logging" for c in hits), (
            "global-sentinel candidate must be eligible from any directory"
        )
