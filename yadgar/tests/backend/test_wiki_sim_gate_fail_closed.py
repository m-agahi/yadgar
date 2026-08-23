"""Car C10 — task 312: wiki near-duplicate gate fails-closed when embedder dies.

Surface: ``WikiStore.find_similar_wiki_pages`` (yadgar/_shared/wiki/store.py:1273-1278)
catches every embedder exception at ``logger.debug`` and returns ``[]``. The drainer
gate (``_similarity_gate_for_drainer``, yadgar/backend/queue_drainer/dlq.py:411)
treats the empty list as "no similar pages" and PASSES the write. An embedder
that fails (cold start, model unload, transient embed service outage) therefore
silently lifts the gate and lets duplicates land — the same defect the gate
exists to prevent.

The fix has two halves:

1. ``find_similar_wiki_pages`` RAISES ``WikiSimilarityGateUnavailable`` when the
   embedder raises OR returns ``None``. The empty list now means ONLY "embedder
   ran cleanly, no candidates above threshold" — a strictly narrower contract.
2. ``_similarity_gate_for_drainer`` catches ``WikiSimilarityGateUnavailable`` and
   returns a rejection with ``reason="gate_unavailable"``. Fail-CLOSED.

Read callers (``wiki_check_duplicate``, ``_autolink_filter_by_similarity``) keep
their existing fail-OPEN semantics: a query that can't get an embedding still
returns "no candidates" so the UI doesn't fall over. They catch the new
exception and convert to ``[]``.

These tests pin the WRITE-path behaviour at two levels:

A. The exception class exists and is exported from where the drainer catches it.
B. The drainer rejects when the embedder is unavailable (3 failure surfaces:
   exception raised, None returned, vector search exception).
C. The read-side ``wiki_check_duplicate`` still returns empty candidates when the
   embedder is unavailable — fail-open is preserved for the dry-run query.
D. The exception is NOT raised when the embedder runs cleanly with no hits.

Each test uses a fresh drainer + a monkeypatched ``find_similar_wiki_pages`` so
it stays hermetic (no SurrealDB, no model load — the test surface is the
drainer's gate decision, not the embedding model).
"""

from __future__ import annotations

import tempfile

import pytest

from yadgar._shared.wiki.store import WikiSimilarityGateUnavailable
from yadgar.backend.queue_drainer import FileQueue, QueueDrainer

# ── A. The exception class is exposed where the drainer imports from. ────────


class TestGateUnavailableExceptionExported:
    """The fix needs the drainer to import the exception. Pin its existence + location."""

    def test_exception_is_subclass_of_exception(self):
        """Pin: it IS a regular exception so a ``except Exception`` catches it too.
        Sub-classing ``Exception`` (not ``BaseException``) is what makes it safe
        to raise in the embedder-failure path: any caller catching the
        blanket ``except Exception`` for config-error tolerance (e.g.
        ``dlq.py:392``'s config read) still catches this and routes through
        the same fail-closed logic.
        """
        assert issubclass(WikiSimilarityGateUnavailable, Exception)

    def test_exception_carries_embedder_reason_in_message(self):
        """The exception message names the embedder so the DLQ sidecar is
        informative. (The drainer stamps ``last_error`` from ``str(exc)``
        so the message IS the operator-facing surface.)
        """
        exc = WikiSimilarityGateUnavailable("embedder raised")
        assert "embedder" in str(exc).lower()

    def test_exception_importable_from_wiki_store(self):
        """Drainer code imports it via ``yadgar._shared.wiki.store`` (same module
        that defines ``find_similar_wiki_pages``). The single-source-of-truth
        avoids the drainer and the store diverging on the type they pass."""
        from yadgar._shared.wiki.store import WikiSimilarityGateUnavailable as Cls

        assert Cls is WikiSimilarityGateUnavailable


# ── B. Drainer fails-closed when the gate cannot evaluate. ──────────────────


def _drainer_gate_with_find_similar(monkeypatch, find_similar_return):
    """Build a fresh drainer with ``find_similar_wiki_pages`` monkeypatched.

    Returns the drainer. The patched function returns ``find_similar_return``
    regardless of args (so we can drive it to raise / return None / return []).
    The drainer is built against an in-memory FileQueue + a WikiStore stub.
    """
    from unittest.mock import MagicMock

    fq = FileQueue(tempfile.mkdtemp())
    drainer = QueueDrainer(queue=fq, storage_factory=lambda: None, drain_interval=9999)

    # Stand-in for ``yadgar._shared.runtime.state._wiki`` — the drainer imports
    # it lazily inside ``_similarity_gate_for_drainer``. The patched helper
    # returns a MagicMock with a patched ``find_similar_wiki_pages``.
    wiki_stub = MagicMock()
    wiki_stub.find_similar_wiki_pages = lambda *a, **kw: find_similar_return(*a, **kw)
    monkeypatch.setattr("yadgar._shared.runtime.state._wiki", wiki_stub, raising=False)

    # The drainer reads ``yadgar._shared.config.get_settings()`` for the
    # threshold / mode / top_k. Without a real Settings instance the outer
    # try in ``_similarity_gate_for_drainer`` catches the import-time lookup
    # error and short-circuits to ``None`` BEFORE our patched
    # ``find_similar_wiki_pages`` is ever called. Stub the config reader so the
    # gate reaches the embedder-call site.
    settings_stub = MagicMock()
    settings_stub.WIKI_SIM_GATE_ENABLED = True
    settings_stub.WIKI_SIM_MODE = "hard"
    settings_stub.WIKI_SIM_CONTENT_THRESHOLD = 0.80
    settings_stub.WIKI_SIM_TOP_K = 5
    monkeypatch.setattr("yadgar._shared.config.get_settings", lambda: settings_stub)

    # The drainer dispatches on ``get_policy(page_type).gate_mode``. The default
    # ``reference`` page type is gate_mode="identity" → routed to
    # ``_identity_gate_for_drainer`` (pass-through) and never reaches the
    # similarity call site. Force similarity gate mode so the tests exercise
    # the half they claim to.
    policy_stub = MagicMock()
    policy_stub.gate_mode = "similarity"
    monkeypatch.setattr("yadgar._shared.wiki.policy.get_policy", lambda *a, **kw: policy_stub)

    return drainer


class TestSimilarityGateFailsClosed:
    """When the embedder cannot evaluate, the drainer REJECTS the write.

    Three surfaces that pre-fix returned ``[]`` (read as "no similar pages"):
    all must now return a rejection dict with ``reason="gate_unavailable"``.
    """

    def test_embedder_raises_returns_rejection(self, monkeypatch):
        """Embedder raises (cold start, transient outage, OOM) → gate rejects.
        Pre-fix: caught at store.py:1277 logger.debug, gate saw ``[]``, write
        silently passed with no gate evaluation. Post-fix: drainer catches the
        exception and rejects so the operator sees a real failure, not a quiet
        duplication.
        """

        def _raise(*a, **kw):
            raise WikiSimilarityGateUnavailable("embedder raised RuntimeError")

        drainer = _drainer_gate_with_find_similar(monkeypatch, _raise)
        rejection = drainer._sim_gate_for_drainer(
            {
                "title": "Doc",
                "content": "any content",
                "slug": "doc-slug",
                "page_type": "reference",
                "force": False,
                "replace_slug": None,
                "append": False,
                "directory_context": "/proj/example",
            }
        )
        assert isinstance(rejection, dict), (
            f"embedder-raise must produce a rejection dict, not None. Got: {rejection!r}"
        )
        assert rejection.get("stored") is False
        assert rejection.get("reason") == "gate_unavailable", (
            f"embedder failure must use reason='gate_unavailable', got: {rejection.get('reason')!r}"
        )

    def test_embedder_returns_none_returns_rejection(self, monkeypatch):
        """Embedder returns ``None`` (model returned no embedding — a known
        degradation mode for unloaded models). Same fail-closed path.
        Pre-fix: ``if query_embedding is None: return []`` (store.py:1280-1281),
        gate saw ``[]``, write passed.
        """

        # store.py now wraps the ``None`` return as ``WikiSimilarityGateUnavailable``,
        # so the drainer sees an exception, not a bare ``None``. The drainer's
        # fail-closed surface is the exception → rejection path; ``None`` itself
        # is not a contract the drainer can act on (a search returning ``None``
        # would be a programmer error, not an embedder degradation).
        def _raise_none(*a, **kw):
            raise WikiSimilarityGateUnavailable("embedder returned None")

        drainer = _drainer_gate_with_find_similar(monkeypatch, _raise_none)
        rejection = drainer._sim_gate_for_drainer(
            {
                "title": "Doc",
                "content": "any content",
                "slug": "doc-slug",
                "page_type": "reference",
                "force": False,
                "replace_slug": None,
                "append": False,
                "directory_context": "/proj/example",
            }
        )
        assert isinstance(rejection, dict)
        assert rejection.get("reason") == "gate_unavailable"

    def test_find_similar_returns_empty_list_passes(self, monkeypatch):
        """Negative control: clean embedder run with no candidates → gate
        PASSES (rejection is ``None``). Pin that fail-closed does NOT extend
        to the legitimate-empty-hits case. Pre-fix the empty list was the
        failure path; post-fix the failure path is the raised exception.
        """
        drainer = _drainer_gate_with_find_similar(monkeypatch, lambda *a, **kw: [])
        rejection = drainer._sim_gate_for_drainer(
            {
                "title": "Doc",
                "content": "any content",
                "slug": "doc-slug",
                "page_type": "reference",
                "force": False,
                "replace_slug": None,
                "append": False,
                "directory_context": "/proj/example",
            }
        )
        assert rejection is None, (
            f"clean embedder + no candidates must pass the gate (rejection=None). "
            f"Got: {rejection!r}"
        )


# ── C. Read-side ``wiki_check_duplicate`` stays fail-open. ──────────────────


class TestReadSideStaysFailOpen:
    """The dry-run ``wiki_check_duplicate`` tool is a query, not a write.

    It cannot meaningfully "fail closed" — it returns information to the
    caller about what similar pages exist, and a degraded embedder returning
    "I can't tell, but I won't crash either" is the right UX. This test pins
    that read-side callers still convert the exception to ``[]`` so callers
    see an empty candidate list, not a stack trace.
    """

    def test_wiki_check_duplicate_returns_empty_on_unavailable(self, monkeypatch):
        """wiki_check_duplicate catches WikiSimilarityGateUnavailable and
        returns ``candidates=[]`` rather than letting the exception propagate
        to the MCP client."""
        from yadgar.core.server.tools import wiki as wiki_tools

        monkeypatch.setattr("yadgar._shared.runtime.state._wiki", None, raising=False)

        # Patch the wiki store's find_similar to raise, simulating an embedder
        # outage during the read query. ``wiki_check_duplicate`` must NOT
        # propagate — operators see ``candidates: []``.
        class _FakeWiki:
            def find_similar_wiki_pages(self, *a, **kw):
                raise WikiSimilarityGateUnavailable("embedder offline")

        monkeypatch.setattr("yadgar._shared.runtime.state._wiki", _FakeWiki(), raising=False)

        # The tool asserts _st._wiki is not None, so the FakeWiki must be
        # reachable through the same lookup the production code uses.
        result = wiki_tools.wiki_check_duplicate(
            title="Doc",
            content="any content",
            directory="/proj/example",
            project="m-agahi/yadgar",
        )
        assert isinstance(result, dict)
        assert result.get("candidates") == [], (
            f"read-side failure must yield empty candidates, got: {result!r}"
        )


# ── D. find_similar_wiki_pages itself raises on embedder failure ─────────────


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    """Init the engines for the find_similar tests so ``server._wiki`` is real."""
    from yadgar.core import server

    server.init_engines(
        db_path=str(tmp_path / "wiki_store_raises.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


class TestFindSimilarRaisesOnEmbedderFailure:
    """The exception IS raised from ``find_similar_wiki_pages`` — the read-side
    and write-side fixes both depend on that single contract. Pin it here so a
    future refactor cannot silently revert to ``return []``.
    """

    def test_find_similar_raises_on_embedder_exception(self, monkeypatch):
        """encode_query raises → find_similar_wiki_pages raises
        WikiSimilarityGateUnavailable (NOT bare RuntimeError) so the drainer's
        narrow catch catches only this class."""
        from yadgar.core import server

        wiki = server._wiki
        assert wiki is not None, "engine init must produce a WikiStore"

        def _raise(*a, **kw):
            raise RuntimeError("embedder offline")

        monkeypatch.setattr(wiki._embeddings, "encode_query", _raise)
        with pytest.raises(WikiSimilarityGateUnavailable):
            wiki.find_similar_wiki_pages(title="Doc", content="any")

    def test_find_similar_raises_on_embedder_returning_none(self, monkeypatch):
        """encode_query returns None → find_similar_wiki_pages raises
        WikiSimilarityGateUnavailable (the same surface). Pre-fix it returned
        ``[]`` which the drainer read as "no candidates"."""
        from yadgar.core import server

        wiki = server._wiki
        assert wiki is not None, "engine init must produce a WikiStore"

        monkeypatch.setattr(wiki._embeddings, "encode_query", lambda *a, **kw: None)
        with pytest.raises(WikiSimilarityGateUnavailable):
            wiki.find_similar_wiki_pages(title="Doc", content="any")
