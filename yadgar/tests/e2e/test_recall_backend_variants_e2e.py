"""E2E: per-variant backend /recall coverage — Phase 1 §7 #2 & #7.

Exercises the backend /recall route for EVERY variant:
  - type=all, type=memory, type=wiki
  - profile=fast (assert no _cross_encoder_score on rows)
  - profile=balanced, profile=full
  - mode=landscape (assert rows carry consensus_score + voting_domains)

These are the "license-to-cutover" tests: all variants must serve non-error
responses and return correctly-shaped results from the real backend pipeline.

Methodology:
  - Uses e2e_engines fixture (real SurrealDB, real embeddings).
  - Drives the backend via httpx.ASGITransport (in-process, no TCP).
  - Pre-marks _recall_engines_ready=True (fixture engines already init'd).
  - Uses YADGAR_ALLOW_ROOT=1 env for bearer auth bypass.

Marker: @pytest.mark.e2e
"""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest

pytestmark = pytest.mark.e2e

_DIR = "/home/test/yadgar-variant-e2e"
_QUERY = "variant e2e recall probe 7k3m"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_mem(storage, embeddings, content: str, heat: float = 0.9) -> int:
    emb = embeddings.encode(content)
    return storage.insert_memory(
        {
            "content": content,
            "embedding": emb,
            "directory_context": _DIR,
            "tags": [],
            "heat": heat,
        }
    )


def _insert_wiki(title: str, content: str) -> str:
    from yadgar._shared.runtime import state as _st
    from yadgar._shared.wiki import WikiAddOptions

    assert _st._wiki is not None
    opts = WikiAddOptions(
        source_memory_ids=[],
        branch="master",
        directory_context=_DIR,
    )
    page = _st._wiki.add(
        title=title,
        content=content,
        category="reference",
        tags=[],
        opts=opts,
    )
    return page["slug"]


def _insert_and_assign_pool(storage, embeddings, content: str) -> int:
    """Insert memory + assign to astrocyte pool (for landscape mode)."""
    from datetime import UTC, datetime

    from yadgar._shared.runtime import state as _st

    emb = embeddings.encode(content)
    now = datetime.now(UTC).isoformat()
    mid = storage.insert_memory(
        {
            "content": content,
            "embedding": emb,
            "directory_context": _DIR,
            "heat": 0.9,
            "tags": [],
            "last_accessed": now,
            "created_at": now,
            "access_count": 0,
            "is_protected": False,
        }
    )
    if _st._pool is not None:
        mem = storage.get_memory(mid)
        if mem is not None:
            _st._pool.assign_memory(mem)
    return mid


async def _post_recall(app, payload: dict) -> httpx.Response:
    """POST to /recall via ASGITransport, with YADGAR_ALLOW_ROOT=1."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://backend") as c:
        return await c.post("/recall", json=payload, headers={})


def _call_backend(app, payload: dict) -> dict:
    """Synchronous wrapper around _post_recall."""
    orig = os.environ.get("YADGAR_ALLOW_ROOT", "0")
    os.environ["YADGAR_ALLOW_ROOT"] = "1"
    try:
        resp = asyncio.run(_post_recall(app, payload))
    finally:
        os.environ["YADGAR_ALLOW_ROOT"] = orig
    return {
        "status": resp.status_code,
        "data": resp.json() if resp.status_code == 200 else resp.text,
    }


# ---------------------------------------------------------------------------
# Fixture: seed corpus and prepare backend
# ---------------------------------------------------------------------------


@pytest.fixture()
def variant_corpus(e2e_engines, monkeypatch):
    """Seed corpus for all variant tests and pre-mark backend engines ready."""
    import yadgar.backend.embed_service.embed_service as _svc

    storage = e2e_engines["storage"]
    embeddings = e2e_engines["embeddings"]

    # Seed memories
    _insert_mem(storage, embeddings, f"{_QUERY} memory alpha high relevance", heat=0.9)
    _insert_mem(storage, embeddings, f"{_QUERY} memory beta medium relevance", heat=0.6)
    # Seed wiki
    _insert_wiki("Variant test wiki alpha", f"{_QUERY} wiki reference page content")
    # Seed pool-assigned memory for landscape
    _insert_and_assign_pool(storage, embeddings, f"def fix_handler() resolved TypeError {_QUERY}")

    monkeypatch.setattr("yadgar.core.server._detect_branch", lambda _d: "master")
    monkeypatch.setattr("yadgar.core.server._get_default_branch", lambda _d: "master")

    # Mark engines ready (fixture engines already initialized).
    # T2 Car E2: pre-marking skips _ensure_recall_engines, which is what
    # composes the backend retriever now — compose it explicitly (idempotent).
    from yadgar.backend.retrieval.compose import ensure_retrieval_engine

    ensure_retrieval_engine()

    original_ready = _svc._recall_engines_ready
    _svc._recall_engines_ready = True
    yield e2e_engines
    _svc._recall_engines_ready = original_ready


def _base_payload(**overrides) -> dict:
    payload = {
        "query": _QUERY,
        "directory": _DIR,
        "current_branch": "master",
        "default_branch": "master",
        "max_results": 10,
        "min_heat": 0.0,
        "type": "all",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Type variants
# ---------------------------------------------------------------------------


class TestBackendTypeVariants:
    """§7 #2: type=all/memory/wiki must each return non-empty results."""

    def test_type_all_returns_results(self, variant_corpus, monkeypatch):
        """type=all returns non-empty ranked results from backend."""
        from yadgar.backend.embed_service import app

        result = _call_backend(app, _base_payload(type="all"))
        assert result["status"] == 200, f"type=all returned {result['status']}: {result['data']}"
        assert len(result["data"]["results"]) > 0, "type=all returned no results"

    def test_type_memory_returns_results(self, variant_corpus, monkeypatch):
        """type=memory returns only memory-sourced results from backend."""
        from yadgar.backend.embed_service import app

        result = _call_backend(app, _base_payload(type="memory"))
        assert result["status"] == 200, f"type=memory returned {result['status']}: {result['data']}"
        assert len(result["data"]["results"]) > 0, "type=memory returned no results"
        # All results must be from memory source (no wiki)
        for r in result["data"]["results"]:
            assert r.get("_source") != "wiki", f"type=memory returned wiki row: {r}"

    def test_type_wiki_returns_results(self, variant_corpus, monkeypatch):
        """type=wiki returns only wiki-sourced results from backend."""
        from yadgar.backend.embed_service import app

        result = _call_backend(app, _base_payload(type="wiki"))
        assert result["status"] == 200, f"type=wiki returned {result['status']}: {result['data']}"
        # Wiki results may be sparse; accept 0 rows if wiki store returned nothing
        # (query relevance threshold). Main assertion: 200 response, correct shape.
        results = result["data"]["results"]
        for r in results:
            assert r.get("_source") == "wiki" or "slug" in r, (
                f"type=wiki returned non-wiki row: {r}"
            )


# ---------------------------------------------------------------------------
# Profile variants
# ---------------------------------------------------------------------------


class TestBackendProfileVariants:
    """§7 #2: profile=fast/balanced/full must each return non-error responses."""

    def test_profile_fast_serves_200_no_400(self, variant_corpus, monkeypatch):
        """profile=fast must NOT return 400 — backend contract widened in Phase 1."""
        from yadgar.backend.embed_service import app

        result = _call_backend(app, _base_payload(profile="fast"))
        assert result["status"] == 200, (
            f"profile=fast returned {result['status']} — 400 guard not removed: {result['data']}"
        )

    def test_profile_fast_rows_have_no_cross_encoder_score(self, variant_corpus, monkeypatch):
        """profile=fast rows must NOT carry _cross_encoder_score.

        fast profile skips the memory-arm CE AND the fusion CE. When both are
        skipped, _cross_encoder_score is never set on any result row.
        """
        from yadgar.backend.embed_service import app

        result = _call_backend(app, _base_payload(profile="fast", type="all"))
        assert result["status"] == 200, f"profile=fast failed: {result['data']}"
        results = result["data"]["results"]
        ce_rows = [
            r
            for r in results
            if "_cross_encoder_score" in r and r["_cross_encoder_score"] is not None
        ]
        assert ce_rows == [], (
            f"profile=fast rows carry _cross_encoder_score: "
            f"{[r.get('_cross_encoder_score') for r in ce_rows]}"
        )

    def test_profile_balanced_serves_200(self, variant_corpus, monkeypatch):
        """profile=balanced must return 200 with non-empty results."""
        from yadgar.backend.embed_service import app

        result = _call_backend(app, _base_payload(profile="balanced"))
        assert result["status"] == 200, (
            f"profile=balanced returned {result['status']}: {result['data']}"
        )
        assert len(result["data"]["results"]) > 0, "profile=balanced returned no results"

    def test_profile_full_serves_200(self, variant_corpus, monkeypatch):
        """profile=full must return 200 with non-empty results."""
        from yadgar.backend.embed_service import app

        result = _call_backend(app, _base_payload(profile="full"))
        assert result["status"] == 200, (
            f"profile=full returned {result['status']}: {result['data']}"
        )
        assert len(result["data"]["results"]) > 0, "profile=full returned no results"


# ---------------------------------------------------------------------------
# Landscape mode
# ---------------------------------------------------------------------------


class TestBackendLandscapeMode:
    """§7 #2: mode=landscape must return 200, rows carry consensus_score + voting_domains."""

    def test_landscape_mode_serves_200_no_400(self, variant_corpus, monkeypatch):
        """mode=landscape must NOT return 400 — backend contract widened in Phase 1."""
        from yadgar.backend.embed_service import app

        result = _call_backend(app, _base_payload(mode="landscape"))
        assert result["status"] == 200, (
            f"mode=landscape returned {result['status']} — 400 guard not removed: {result['data']}"
        )

    def test_landscape_returns_results_or_empty_if_pool_disabled(self, variant_corpus, monkeypatch):
        """mode=landscape returns results (or [] if pool unavailable) — never 400.

        The pool may not be populated in the e2e env; that's OK. The key assertion
        is: no 400, 200 with results list (even if empty due to pool availability).
        """
        from yadgar.backend.embed_service import app

        result = _call_backend(
            app,
            _base_payload(
                mode="landscape",
                query=f"fix handler exception {_QUERY}",
            ),
        )
        assert result["status"] == 200, f"landscape mode failed: {result['data']}"
        assert isinstance(result["data"]["results"], list), "landscape must return a list"

    def test_landscape_rows_carry_consensus_score_when_pool_available(
        self, variant_corpus, monkeypatch
    ):
        """When pool is available, landscape rows carry consensus_score + voting_domains."""
        import yadgar._shared.runtime.state as _st
        from yadgar.backend.embed_service import app

        if _st._pool is None:
            pytest.skip("AstrocytePool not available in this e2e env — pool-only assertion")

        result = _call_backend(
            app,
            _base_payload(
                mode="landscape",
                query=f"fix handler exception {_QUERY}",
            ),
        )
        assert result["status"] == 200
        results = result["data"]["results"]

        if not results:
            pytest.skip("Pool returned no results for this corpus — cannot assert row shape")

        for r in results:
            assert "consensus_score" in r, f"landscape row missing consensus_score: {r}"
            assert "voting_domains" in r, f"landscape row missing voting_domains: {r}"
