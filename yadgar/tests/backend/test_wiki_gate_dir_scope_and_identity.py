"""Car B (#83): directory-scoped similarity gate + type-aware identity gate.

Seam 1 — directory scoping (folds the general cross-project gate fix):
    ``find_similar_wiki_pages`` filters candidates by ``directory_context`` (via
    ``is_directory_eligible``).  A candidate page in an UNRELATED project
    directory is NOT a duplicate — the cross-project ``logging.py`` collision.

Seam 2 — type-aware gate:
    ``_sim_gate_for_drainer`` resolves ``get_policy(page_type).gate_mode``.
    ``identity`` → skip content-similarity; run ``validate_repo_wiki_page``.
    Schema errors → reject (reason ``repo_wiki_schema_invalid``).  Valid →
    allow (slug-uniqueness + upsert handle identity).

Embedding independence
----------------------
The Seam-1 candidate-filter tests inject a fake KNN result + fake query encoder
onto the WikiStore so the directory filter is exercised deterministically —
they do NOT depend on the real embedding model (which requires
sentence-transformers + native libs that may be absent in a bare sandbox).
The positive control (`test_unscoped_finds_candidate`) fails LOUDLY if the
injected candidate is not surfaced, so a dir-filter regression cannot pass
vacuously.  The Seam-2 identity/schema tests need no embeddings at all.
"""

from __future__ import annotations

import tempfile

import pytest

from yadgar._shared.wiki.repo_wiki_schema import REPO_WIKI_PAGE_TYPE
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


def _drainer_gate(payload: dict) -> dict | None:
    """Call _sim_gate_for_drainer() directly to test gate logic in isolation."""
    from yadgar.backend.queue_drainer import FileQueue, QueueDrainer

    with tempfile.TemporaryDirectory() as tmp:
        fq = FileQueue(tmp)
        drainer = QueueDrainer(queue=fq, storage_factory=lambda: None, drain_interval=9999)
        return drainer._sim_gate_for_drainer(payload)


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


# ── Seam 2: type-aware identity gate (no embeddings needed) ──────────────────


class TestGateIdentityMode:
    def test_repo_wiki_valid_skips_similarity(self):
        """page_type=repo_wiki + valid schema → identity mode, no rejection.

        Identity mode short-circuits BEFORE find_similar_wiki_pages, so this
        needs no embedding at all. A valid repo_wiki page passes the gate.
        """
        payload = {
            "title": "Repo Wiki Incoming",
            "content": "## Purpose\nlogging",
            "slug": "proj-mod-logging",
            "branch": None,
            "directory_context": _DIR_A,
            "page_type": REPO_WIKI_PAGE_TYPE,
            "hash": "b" * 64,
            "source_file": "/proj/alpha/logging.py",
            "force": False,
            "replace_slug": None,
            "append": False,
        }
        rejection = _drainer_gate(payload)
        assert rejection is None, (
            f"valid identity-mode repo_wiki page must pass the gate. Got: {rejection}"
        )

    def test_repo_wiki_missing_hash_rejected(self):
        """repo_wiki with no hash → schema-invalid rejection."""
        payload = {
            "title": "Repo Wiki NoHash",
            "content": "## Purpose\nX",
            "slug": "proj-mod-nohash",
            "branch": None,
            "directory_context": _DIR_A,
            "page_type": REPO_WIKI_PAGE_TYPE,
            "hash": None,
            "source_file": "/proj/alpha/nohash.py",
            "force": False,
            "replace_slug": None,
            "append": False,
        }
        rejection = _drainer_gate(payload)
        assert rejection is not None
        assert rejection.get("reason") == "repo_wiki_schema_invalid"
        assert rejection.get("stored") is False

    def test_repo_wiki_relative_source_file_rejected(self):
        """repo_wiki with a relative source_file → schema-invalid rejection."""
        payload = {
            "title": "Repo Wiki Relative",
            "content": "## Purpose\nX",
            "slug": "proj-mod-rel",
            "branch": None,
            "directory_context": _DIR_A,
            "page_type": REPO_WIKI_PAGE_TYPE,
            "hash": "c" * 64,
            "source_file": "rel/path.py",  # not absolute
            "force": False,
            "replace_slug": None,
            "append": False,
        }
        rejection = _drainer_gate(payload)
        assert rejection is not None
        assert rejection.get("reason") == "repo_wiki_schema_invalid"

    def test_repo_wiki_slug_without_mod_rejected(self):
        """repo_wiki whose slug lacks '-mod-' → schema-invalid rejection."""
        payload = {
            "title": "Repo Wiki BadSlug",
            "content": "## Purpose\nX",
            "slug": "proj-logging",  # no -mod-
            "branch": None,
            "directory_context": _DIR_A,
            "page_type": REPO_WIKI_PAGE_TYPE,
            "hash": "d" * 64,
            "source_file": "/proj/alpha/logging.py",
            "force": False,
            "replace_slug": None,
            "append": False,
        }
        rejection = _drainer_gate(payload)
        assert rejection is not None
        assert rejection.get("reason") == "repo_wiki_schema_invalid"

    def test_identity_bypasses_similarity_config_disabled(self, monkeypatch):
        """Identity mode fires even when the similarity gate would bypass.

        Sanity: identity resolution happens after force/replace_slug/append
        bypasses but before the similarity-config block, so a valid repo_wiki
        page is allowed and an invalid one is rejected regardless of
        WIKI_SIM_GATE_ENABLED. Here we only assert the invalid path still
        rejects (schema check is independent of the similarity config).
        """
        payload = {
            "title": "Repo Wiki Invalid Under Any Config",
            "content": "## Purpose\nX",
            "slug": "proj-logging-plain",  # no '-mod-' → schema-invalid
            "branch": None,
            "directory_context": _DIR_A,
            "page_type": REPO_WIKI_PAGE_TYPE,
            "hash": "e" * 64,
            "source_file": "/proj/alpha/x.py",
            "force": False,
            "replace_slug": None,
            "append": False,
        }
        rejection = _drainer_gate(payload)
        assert rejection is not None
        assert rejection.get("reason") == "repo_wiki_schema_invalid"

    def test_force_still_bypasses_before_identity(self):
        """force=True short-circuits even an invalid repo_wiki page (bypass wins)."""
        payload = {
            "title": "Repo Wiki Forced",
            "content": "## Purpose\nX",
            "slug": "no-mod-forced",  # would be schema-invalid
            "branch": None,
            "directory_context": _DIR_A,
            "page_type": REPO_WIKI_PAGE_TYPE,
            "hash": None,
            "source_file": "rel.py",
            "force": True,  # bypass wins over identity schema check
            "replace_slug": None,
            "append": False,
        }
        rejection = _drainer_gate(payload)
        assert rejection is None, "force=True must bypass the gate entirely"
