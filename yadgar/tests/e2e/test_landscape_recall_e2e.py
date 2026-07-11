"""E2E tests for recall(mode="landscape") — BC-AC3a.

Exposes AstrocytePool.consensus_retrieve() via the recall() MCP tool
as an opt-in "landscape" mode.  Landscape recall merges results across
all astrocyte domains into one ranked consensus list; results carry
``consensus_score`` and ``voting_domains`` metadata.

ASSERTIONS:
    a. recall(mode="landscape") returns a merged ranked list.
    b. Results carry ``consensus_score`` (float > 0).
    c. A cross-domain memory (keywords in ≥2 domains) has ≥2 voting_domains.
    d. Directory scoping is respected: a memory stored under a DIFFERENT
       directory must not appear in landscape results scoped to yadgar_dir.
    e. Invalid mode raises ValueError before any DB work.

TEST PLAN
---------
- Seed memories through the storage + pool assign path (not async drain) —
  mirrors test_astrocyte_pool.py approach on the live e2e DB.
- Cross-domain memory uses content with both code-domain keywords
  ("def", "class", "function") and error-domain keywords ("error", "bug",
  "exception") — same "fix_handler" pattern that test_astrocyte_pool.py
  uses for TestConsensusRetrieval::test_cross_domain_boost.
- Foreign-dir memory uses the other_dir fixture path.
- Assertion on ≥1 result for landscape mode (not strict top-1 because
  the pool may have other memories from concurrent e2e fixture seeding).

MARKERS: @pytest.mark.e2e — collected by make e2e / scripts/reap-test-surreal.sh.

Ref: BC-AC3a, CAP-RETR-025.
"""

from __future__ import annotations

import asyncio
import os as _os

import httpx
import pytest

pytestmark = pytest.mark.e2e


@pytest.fixture
def landscape_asgi_wiring(e2e_engines, monkeypatch):
    """Wire httpx.post through ASGITransport so recall(mode='landscape') round-trips
    via the in-process backend embed_service.app without real TCP.

    Phase 2 test-fix (Pattern 2): mirrors TestRecallCoreForwarderE2E.test_forwarder_e2e_flag_on.
    Required for landscape tests because recall_backend_bypass returns [] for landscape.
    """
    import yadgar.backend.embed_service.embed_service as _svc
    from yadgar.backend.embed_service import app as _backend_app

    _svc._recall_engines_ready = True

    def _asgi_post(url: str, *, json=None, headers=None, timeout=None):
        from urllib.parse import urlparse

        parsed = urlparse(url)
        path = parsed.path

        _orig_root = _os.environ.get("YADGAR_ALLOW_ROOT", "0")
        _os.environ["YADGAR_ALLOW_ROOT"] = "1"
        try:

            async def _do():
                transport = httpx.ASGITransport(app=_backend_app)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://backend"
                ) as client:
                    resp = await client.post(path, json=json, headers=headers or {})
                    resp.raise_for_status()
                    return resp

            return asyncio.run(_do())
        finally:
            _os.environ["YADGAR_ALLOW_ROOT"] = _orig_root

    monkeypatch.setenv("YADGAR_EMBED_URL", "http://backend-stub:8001")
    monkeypatch.setattr("httpx.post", _asgi_post)
    yield


_UNIQUE_TAG = "xzlandscape801"
_YADGAR_DIR = "/home/test/yadgar-project"
_OTHER_DIR = "/home/test/other-project"

# Domain-crossing content: code keywords (def/class/function) + error keywords
# (error/bug/exception) → assigned to both "code-patterns" and "errors" domains.
_CROSS_DOMAIN_CONTENT = (
    f"def fix_handler(): resolved TypeError exception in the function implementation {_UNIQUE_TAG}"
)
# Single-domain content (code only).
_CODE_ONLY_CONTENT = f"class DataPipeline: implements transform method {_UNIQUE_TAG}"
# Foreign-dir content — same tokens so pool would score it, but wrong directory.
_FOREIGN_CONTENT = (
    f"def fix_handler(): resolved TypeError exception in function {_UNIQUE_TAG}_foreign"
)


def _insert_and_assign(e2e_engines, content: str, directory: str, heat: float = 0.9) -> int:
    """Insert a memory with embedding and assign it to the astrocyte pool.

    Mirrors test_astrocyte_pool.py seeding: insert via storage.insert_memory(),
    then call _st._pool.assign_memory(mem).  Returns the inserted memory id.
    """
    import yadgar._shared.runtime.state as _st

    storage = e2e_engines["storage"]
    embeddings = e2e_engines["embeddings"]

    emb = embeddings.encode(content)
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    mid = storage.insert_memory(
        {
            "content": content,
            "embedding": emb,
            "directory_context": directory,
            "heat": heat,
            "tags": [_UNIQUE_TAG],
            "last_accessed": now,
            "created_at": now,
            "access_count": 0,
            "is_protected": False,
        }
    )

    # Assign to astrocyte pool so consensus_retrieve has domain membership data.
    assert _st._pool is not None, "AstrocytePool must be initialized by e2e_engines"
    mem = storage.get_memory(mid)
    assert mem is not None, f"get_memory({mid}) returned None after insert"
    _st._pool.assign_memory(mem)

    return mid


def _run_landscape_recall(
    monkeypatch,
    query: str,
    directory: str = _YADGAR_DIR,
    max_results: int = 20,
) -> list[dict]:
    """Run recall(mode="landscape") via the MCP tool module."""
    import sys

    monkeypatch.setattr("yadgar.core.server._detect_branch", lambda _d: "master")
    monkeypatch.setattr("yadgar.core.server._get_default_branch", lambda _d: "master")

    _rm = sys.modules.get("yadgar.core.server.tools.recall")
    if _rm is None:
        import yadgar.core.server.tools.recall as _rm  # type: ignore[no-redef]

    return _rm.recall(
        query=query,
        directory=directory,
        max_results=max_results,
        mode="landscape",
    )


class TestLandscapeRecallE2E:
    """E2E tests for recall(mode="landscape") — BC-AC3a."""

    def test_landscape_returns_nonempty_list(self, e2e_engines, monkeypatch, landscape_asgi_wiring):
        """recall(mode="landscape") returns ≥1 result for a relevant query.

        Ref: BC-AC3a (a).
        """
        _insert_and_assign(e2e_engines, _CROSS_DOMAIN_CONTENT, _YADGAR_DIR)

        results = _run_landscape_recall(monkeypatch, f"fix handler exception {_UNIQUE_TAG}")
        assert len(results) >= 1, (
            f"recall(mode='landscape') must return ≥1 result for seeded cross-domain memory; "
            f"got {results}"
        )

    def test_landscape_results_carry_consensus_score(
        self, e2e_engines, monkeypatch, landscape_asgi_wiring
    ):
        """recall(mode="landscape") stamps each result with consensus_score > 0.

        Ref: BC-AC3a (b).
        """
        _insert_and_assign(e2e_engines, _CROSS_DOMAIN_CONTENT, _YADGAR_DIR)

        results = _run_landscape_recall(monkeypatch, f"fix handler exception {_UNIQUE_TAG}")
        assert results, "No results returned; cannot assert consensus_score"

        for r in results:
            assert "consensus_score" in r, (
                f"Result missing consensus_score: {r.get('content', '')[:60]}"
            )
            assert isinstance(r["consensus_score"], (int, float)), (
                f"consensus_score must be numeric; got {type(r['consensus_score'])}"
            )
            assert r["consensus_score"] > 0, (
                f"consensus_score must be > 0; got {r['consensus_score']}"
            )

    def test_cross_domain_memory_has_multiple_voting_domains(
        self, e2e_engines, monkeypatch, landscape_asgi_wiring
    ):
        """A memory with keywords in ≥2 domains carries ≥2 voting_domains.

        Ref: BC-AC3a (c).
        """
        mid = _insert_and_assign(e2e_engines, _CROSS_DOMAIN_CONTENT, _YADGAR_DIR, heat=0.95)

        results = _run_landscape_recall(monkeypatch, f"fix handler exception {_UNIQUE_TAG}")
        target = next((r for r in results if r.get("id") == mid), None)
        assert target is not None, (
            f"Cross-domain memory id={mid} not found in landscape results; "
            f"all ids: {[r.get('id') for r in results]}"
        )
        voting = target.get("voting_domains", [])
        assert len(voting) >= 2, (
            f"Cross-domain memory must have ≥2 voting_domains; got {voting}. "
            f"content={_CROSS_DOMAIN_CONTENT[:60]}"
        )

    def test_directory_scoping_excludes_foreign_dir(
        self, e2e_engines, monkeypatch, landscape_asgi_wiring
    ):
        """recall(mode="landscape", directory=X) must exclude memories from directory Y.

        Ref: BC-AC3a (d) — directory contract.
        """
        # Seed a memory in the target directory.
        target_mid = _insert_and_assign(e2e_engines, _CROSS_DOMAIN_CONTENT, _YADGAR_DIR)
        # Seed an otherwise-matching memory in a foreign directory.
        foreign_mid = _insert_and_assign(e2e_engines, _FOREIGN_CONTENT, _OTHER_DIR)

        results = _run_landscape_recall(
            monkeypatch,
            f"fix handler exception {_UNIQUE_TAG}",
            directory=_YADGAR_DIR,
        )
        result_ids = {r.get("id") for r in results}

        # Target directory memory must appear.
        assert target_mid in result_ids, (
            f"Memory from target dir must appear in landscape results; "
            f"target_mid={target_mid}, result_ids={result_ids}"
        )

        # Foreign directory memory must NOT appear.
        assert foreign_mid not in result_ids, (
            f"Memory from foreign dir ({_OTHER_DIR!r}) must be excluded from landscape "
            f"results scoped to {_YADGAR_DIR!r}; foreign_mid={foreign_mid} found in results."
        )

    def test_invalid_mode_raises_valueerror(self, e2e_engines, monkeypatch):
        """recall(mode="bogus") raises ValueError before any DB work.

        Ref: BC-AC3a validation gate.
        """
        import sys

        monkeypatch.setattr("yadgar.core.server._detect_branch", lambda _d: "master")
        monkeypatch.setattr("yadgar.core.server._get_default_branch", lambda _d: "master")

        _rm = sys.modules.get("yadgar.core.server.tools.recall")
        if _rm is None:
            import yadgar.core.server.tools.recall as _rm  # type: ignore[no-redef]

        with pytest.raises(ValueError, match="mode"):
            _rm.recall(
                query="any query",
                directory=_YADGAR_DIR,
                mode="bogus",
            )

    def test_none_mode_uses_normal_recall(self, e2e_engines, monkeypatch, recall_backend_bypass):
        """mode=None (default) must NOT trigger landscape path — normal recall behavior.

        Ensures the default path is 100% unchanged when mode is absent.
        """
        import sys

        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]

        unique = f"{_UNIQUE_TAG}_default"
        emb = embeddings.encode(f"default mode normal recall content {unique}")
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        mid = storage.insert_memory(
            {
                "content": f"default mode normal recall content {unique}",
                "embedding": emb,
                "directory_context": _YADGAR_DIR,
                "heat": 0.9,
                "tags": [],
                "last_accessed": now,
                "created_at": now,
                "access_count": 0,
                "is_protected": False,
            }
        )

        monkeypatch.setattr("yadgar.core.server._detect_branch", lambda _d: "master")
        monkeypatch.setattr("yadgar.core.server._get_default_branch", lambda _d: "master")

        _rm = sys.modules.get("yadgar.core.server.tools.recall")
        if _rm is None:
            import yadgar.core.server.tools.recall as _rm  # type: ignore[no-redef]

        # mode=None — must not raise, must not carry consensus_score keys.
        results = _rm.recall(
            query=f"default mode normal recall {unique}",
            directory=_YADGAR_DIR,
            max_results=10,
            mode=None,
        )
        # Verify it doesn't break (returns a list).
        assert isinstance(results, list), f"recall(mode=None) must return list; got {type(results)}"
        # None of the normal results should carry consensus_score (not landscape path).
        landscape_contamination = [r for r in results if "consensus_score" in r]
        assert not landscape_contamination, (
            f"mode=None must not return landscape-stamped results; "
            f"got consensus_score in: {[r.get('content', '')[:40] for r in landscape_contamination]}"
        )
        _ = mid  # seeded for query relevance; silence unused warning
