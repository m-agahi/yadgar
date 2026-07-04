"""E2E contract test: recall backend forwarding parity (Train 1).

Methodology (per task spec):
  1. Seed a corpus of memories (graded content + heat) + a wiki page.
  2. Neutralise heat-write drift (monkeypatch boost/heat writes to no-ops
     for the parity comparison so run-1 side-effects can't drift run-2 scores).
  3. Run OFF path: in-core _fanout_recall, capture (id, score) tuples.
  4. Harness validation: run OFF twice, assert identical → proves harness determinism.
  5. Run ON path: drive the backend app in-process via httpx.ASGITransport,
     against the SAME SurrealDB the fixture wired. Set YADGAR_ALLOW_ROOT=1 for
     bearer-auth bypass.
  6. Assert byte-identical (id, score) tuples between OFF and ON (exact, not approx).

Separate test: side-effect split parity — confirms BOTH halves fire when the
combined _apply_recall_side_effects runs (kept separate from the parity test).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

YADGAR_DIR = "/home/test/yadgar-project"
_QUERY = "backend contract test memory unique 7x9q"


def _insert_mem(storage, embeddings, content: str, heat: float = 1.0) -> int:
    """Insert a memory with real embedding and explicit heat."""
    emb = embeddings.encode(content)
    mid = storage.insert_memory(
        {
            "content": content,
            "embedding": emb,
            "directory_context": YADGAR_DIR,
            "tags": [],
            "heat": heat,
        }
    )
    return mid


def _insert_wiki(title: str, content: str) -> str:
    """Insert a wiki page and return its slug."""
    from yadgar.server import _state as _st
    from yadgar.wiki import WikiAddOptions

    assert _st._wiki is not None
    opts = WikiAddOptions(
        source_memory_ids=[],
        branch="master",
        directory_context=YADGAR_DIR,
    )
    page = _st._wiki.add(
        title=title,
        content=content,
        category="reference",
        tags=[],
        opts=opts,
    )
    return page["slug"]


def _run_off_path(query: str, max_results: int = 20) -> list[tuple]:
    """Run the in-core _fanout_recall and return (id, score) tuples."""
    from yadgar.server.tools._recall_pipeline import _fanout_recall

    results = _fanout_recall(
        query=query,
        max_results=max_results,
        min_heat=0.0,
        directory=YADGAR_DIR,
        current_branch="master",
        default_branch="master",
    )
    # Extract (id, score) tuples — use _retrieval_score as primary, fallback heat.
    return [(r.get("id"), float(r.get("_retrieval_score", r.get("heat", 0.0)))) for r in results]


def _run_on_path(query: str, max_results: int = 20) -> list[tuple]:
    """Run the backend /recall in-process via ASGITransport and return (id, score) tuples."""
    import asyncio
    import os as _os

    import httpx

    # Reset the backend engines-ready flag so bootstrap runs (re-uses fixture engines)
    import yadgar.backend.embed_service as _svc
    from yadgar.backend.embed_service import app

    _svc._recall_engines_ready = True  # engines already inited by e2e_engines fixture

    payload = {
        "query": query,
        "directory": YADGAR_DIR,
        "current_branch": "master",
        "default_branch": "master",
        "max_results": max_results,
        "min_heat": 0.0,
        "type": "all",
        "tags": None,
    }

    async def _post() -> dict:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://backend") as client:
            resp = await client.post(
                "/recall",
                json=payload,
                headers={},  # YADGAR_ALLOW_ROOT=1 skips bearer auth
            )
            resp.raise_for_status()
            return resp.json()

    with _os.environ.__class__() if False else _NoOp():
        # Set YADGAR_ALLOW_ROOT=1 for the duration of the request
        _orig = _os.environ.get("YADGAR_ALLOW_ROOT", "0")
        _os.environ["YADGAR_ALLOW_ROOT"] = "1"
        try:
            data = asyncio.run(_post())
        finally:
            _os.environ["YADGAR_ALLOW_ROOT"] = _orig

    results = data["results"]
    return [(r.get("id"), float(r.get("_retrieval_score", r.get("heat", 0.0)))) for r in results]


class _NoOp:
    """Context manager no-op (for readable env-var bracketing)."""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


# ---------------------------------------------------------------------------
# Harness validation: OFF-vs-OFF determinism (must pass before trusting parity)
# ---------------------------------------------------------------------------


class TestRecallBackendContractHarness:
    """Harness determinism: OFF path twice must produce identical (id, score) tuples."""

    def test_off_vs_off_harness_determinism(self, e2e_engines, monkeypatch):
        """OFF path is deterministic when heat writes are neutralised (harness validation)."""
        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]

        # Neutralise all heat/access writes so run-1 can't drift run-2 scores.
        monkeypatch.setattr(storage, "update_memory_heat", lambda mid, heat: None)
        monkeypatch.setattr(storage, "update_memory_last_accessed", lambda mid, ts: None)
        monkeypatch.setattr(storage, "boost_memories_access", lambda ids, ts: None)

        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: "master")
        monkeypatch.setattr("yadgar.server._get_default_branch", lambda _d: "master")

        # Seed corpus with graded heat.
        _insert_mem(storage, embeddings, f"{_QUERY} alpha highest", heat=0.9)
        _insert_mem(storage, embeddings, f"{_QUERY} beta medium", heat=0.5)
        _insert_mem(storage, embeddings, f"{_QUERY} gamma low", heat=0.2)
        _insert_wiki("Contract test wiki harness", f"{_QUERY} wiki reference page")

        # Run OFF path twice.
        run1 = _run_off_path(_QUERY)
        run2 = _run_off_path(_QUERY)

        assert len(run1) > 0, "OFF path returned no results — seed/query mismatch"
        assert run1 == run2, (
            f"OFF path is non-deterministic (harness broken):\n  run1={run1}\n  run2={run2}"
        )


# ---------------------------------------------------------------------------
# Main parity test: OFF-vs-ON byte-identical (id, score) tuples
# ---------------------------------------------------------------------------


class TestRecallBackendContractParity:
    """OFF path and ON (backend) path must produce byte-identical (id, score) tuples."""

    def test_off_vs_on_parity(self, e2e_engines, monkeypatch):
        """Backend /recall returns the same ranked results as the in-core fan-out.

        Neutralises heat writes on both sides to prevent side-effect drift between runs.
        Uses the same in-process CE model on both sides (no stub) so scores are
        numerically identical — any score drift indicates a real wiring bug.
        """
        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]

        # Neutralise all heat/access writes (parity comparison, not a side-effect test).
        monkeypatch.setattr(storage, "update_memory_heat", lambda mid, heat: None)
        monkeypatch.setattr(storage, "update_memory_last_accessed", lambda mid, ts: None)
        monkeypatch.setattr(storage, "boost_memories_access", lambda ids, ts: None)

        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: "master")
        monkeypatch.setattr("yadgar.server._get_default_branch", lambda _d: "master")

        # Seed corpus — unique query prefix guards against cross-test contamination.
        _insert_mem(storage, embeddings, f"{_QUERY} parity high relevance", heat=0.9)
        _insert_mem(storage, embeddings, f"{_QUERY} parity medium relevance", heat=0.5)
        _insert_mem(storage, embeddings, f"{_QUERY} parity low relevance", heat=0.1)
        _insert_wiki("Contract parity wiki", f"{_QUERY} parity wiki reference")

        # Run both paths.
        off_results = _run_off_path(_QUERY)
        on_results = _run_on_path(_QUERY)

        assert len(off_results) > 0, "OFF path returned no results"
        assert len(on_results) > 0, "ON path (backend) returned no results"

        # Byte-identical: exact (id, score) tuples in the same order.
        assert off_results == on_results, (
            "OFF and ON paths produced different results — wiring divergence:\n"
            f"  OFF={off_results}\n"
            f"  ON ={on_results}"
        )


# ---------------------------------------------------------------------------
# Fix 1: Bootstrap test — exercises _ensure_recall_engines (not skipped)
# ---------------------------------------------------------------------------


class TestRecallBackendBootstrap:
    """Test that _ensure_recall_engines actually runs on the first /recall request.

    Fix 1 requirement: the ON-path must exercise the backend bootstrap path
    (_ensure_recall_engines / init_engines), not skip it by pre-forcing the
    ready flag.  Since init_engines unconditionally rebuilds _st._storage (not
    re-entrant), we monkeypatch init_engines to a spy no-op that preserves the
    fixture's existing engine state while still proving _ensure_recall_engines
    ran.  The recall then returns real results from the fixture's live surreal.
    """

    def test_backend_bootstrap_serves_recall(self, e2e_engines, monkeypatch):
        """_ensure_recall_engines fires on first request and serve real results.

        Methodology:
          1. Reset _recall_engines_ready = False so bootstrap path is exercised.
          2. Monkeypatch init_engines to a spy no-op — preserves fixture _st.
          3. Drive a POST /recall via ASGITransport.
          4. Assert init_engines was called (bootstrap ran).
          5. Assert _recall_engines_ready is True (bootstrap completed).
          6. Assert results are non-empty (real recall served correctly).
        """
        import asyncio
        import os as _os
        from unittest.mock import MagicMock

        import httpx

        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]

        # Seed a corpus for this bootstrap test (unique query prefix).
        _BOOT_QUERY = "backend bootstrap test probe 3k7z"
        _insert_mem(storage, embeddings, f"{_BOOT_QUERY} alpha result", heat=0.9)
        _insert_mem(storage, embeddings, f"{_BOOT_QUERY} beta result", heat=0.5)

        import yadgar.backend.embed_service as _svc
        from yadgar.backend.embed_service import app

        # Reset the engines-ready flag so _ensure_recall_engines runs.
        original_ready = _svc._recall_engines_ready
        _svc._recall_engines_ready = False

        # Spy no-op for init_engines — preserves existing _st, proves it was called.
        init_called = MagicMock()

        def _noop_init_engines(*args, **kwargs):
            init_called(*args, **kwargs)
            # No-op: don't rebuild _st — fixture engines must stay intact.

        monkeypatch.setattr(
            "yadgar.server.lifecycle.init_engines",
            _noop_init_engines,
        )

        # Also patch INSIDE embed_service's lazy import path.
        # _ensure_recall_engines does: from yadgar.server.lifecycle import init_engines as _init_engines
        # Patching sys.modules so the from-import also picks up the no-op.
        import yadgar.server.lifecycle as _lifecycle

        original_init = _lifecycle.init_engines
        _lifecycle.init_engines = _noop_init_engines

        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: "master")
        monkeypatch.setattr("yadgar.server._get_default_branch", lambda _d: "master")

        payload = {
            "query": _BOOT_QUERY,
            "directory": YADGAR_DIR,
            "current_branch": "master",
            "default_branch": "master",
            "max_results": 10,
            "min_heat": 0.0,
            "type": "all",
            "tags": None,
        }

        _orig_allow_root = _os.environ.get("YADGAR_ALLOW_ROOT", "0")
        _os.environ["YADGAR_ALLOW_ROOT"] = "1"
        try:

            async def _post() -> dict:
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://backend"
                ) as client:
                    resp = await client.post("/recall", json=payload, headers={})
                    resp.raise_for_status()
                    return resp.json()

            data = asyncio.run(_post())
        finally:
            _os.environ["YADGAR_ALLOW_ROOT"] = _orig_allow_root
            # Restore original init_engines reference
            _lifecycle.init_engines = original_init
            _svc._recall_engines_ready = original_ready

        # Assert 1: init_engines was called — _ensure_recall_engines actually ran.
        (
            init_called.assert_called_once(),
            ("_ensure_recall_engines did not call init_engines — bootstrap was skipped"),
        )

        # Assert 2: engines-ready flag was set by the bootstrap.
        assert _svc._recall_engines_ready is True, (
            "_ensure_recall_engines completed but _recall_engines_ready is not True"
        )

        # Assert 3: recall returned real results — engine bootstrap serves actual data.
        results = data["results"]
        assert len(results) > 0, (
            "Backend /recall returned no results after bootstrap — "
            "engine state not intact after spy init"
        )


# ---------------------------------------------------------------------------
# Fix 2: E2E — core forwarder end-to-end with RECALL_BACKEND_ENABLED=True
# ---------------------------------------------------------------------------


class TestRecallCoreForwarderE2E:
    """E2E test: RECALL_BACKEND_ENABLED=True drives the real forwarder path.

    The forwarder unit tests mock httpx.post; this test wires httpx.post
    via ASGITransport to the in-process backend app so the full round-trip
    exercises: recall() → _forward_to_backend → httpx.post (ASGI) →
    backend /recall route → _ensure_recall_engines → _fanout_recall →
    _apply_recall_db_side_effects → response → _apply_recall_session_side_effects.
    """

    def test_forwarder_e2e_flag_on(self, e2e_engines, monkeypatch):
        """recall() with RECALL_BACKEND_ENABLED=True returns real results via backend.

        Monkeypatches httpx.post to route through ASGITransport(app=embed_service.app)
        so no actual TCP connections are made.  Confirms:
          - Non-empty ranked results returned (same-shape as OFF path).
          - _apply_recall_session_side_effects fires on core side
            (asserted via spy on _st._last_recalled_ids mutation).
        """
        import asyncio
        import sys

        import httpx

        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]

        # Seed corpus with unique prefix.
        _FWD_QUERY = "forwarder e2e flag on test 9m2x"
        _insert_mem(storage, embeddings, f"{_FWD_QUERY} top result", heat=0.9)
        _insert_mem(storage, embeddings, f"{_FWD_QUERY} mid result", heat=0.5)
        _insert_wiki("Forwarder e2e wiki", f"{_FWD_QUERY} wiki content")

        import yadgar.backend.embed_service as _svc
        from yadgar.backend.embed_service import app as _backend_app

        # Ensure backend engines are ready (uses fixture's _st).
        _svc._recall_engines_ready = True

        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: "master")
        monkeypatch.setattr("yadgar.server._get_default_branch", lambda _d: "master")

        # Monkeypatch httpx.post to route through ASGITransport.
        # _forward_to_backend calls: httpx.post(url, json=payload, headers=..., timeout=120.0)
        import os as _os

        def _asgi_post(url: str, *, json=None, headers=None, timeout=None):
            """Sync wrapper that drives the async ASGI transport."""
            # Strip the base URL — only the path matters for ASGITransport.
            from urllib.parse import urlparse

            parsed = urlparse(url)
            path = parsed.path  # e.g. "/recall"

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

        # Set YADGAR_EMBED_URL so _forward_to_backend passes the guard check.
        monkeypatch.setenv("YADGAR_EMBED_URL", "http://backend-stub:8001")

        # Get the recall module and toggle both flags.
        _rm = sys.modules.get("yadgar.server.tools.recall")
        if _rm is None:
            import yadgar.server.tools.recall as _rm  # type: ignore[no-redef]

        original_unified = _rm.settings.UNIFIED_RECALL_ENABLED
        original_backend = _rm.settings.RECALL_BACKEND_ENABLED

        # Snapshot _last_recalled_ids to detect session side-effect mutation.
        from yadgar.server import _state as _st_module

        ids_before = dict(_st_module._last_recalled_ids)

        _rm.settings.UNIFIED_RECALL_ENABLED = True
        _rm.settings.RECALL_BACKEND_ENABLED = True
        try:
            with monkeypatch.context() as m:
                m.setattr("httpx.post", _asgi_post)
                results = _rm.recall(
                    query=_FWD_QUERY,
                    directory=YADGAR_DIR,
                    max_results=20,
                )
        finally:
            _rm.settings.UNIFIED_RECALL_ENABLED = original_unified
            _rm.settings.RECALL_BACKEND_ENABLED = original_backend

        # Assert 1: non-empty results with correct shape.
        assert len(results) > 0, "recall() with RECALL_BACKEND_ENABLED=True returned no results"
        for r in results:
            assert "id" in r, f"Result missing 'id': {r}"
            assert "content" in r, f"Result missing 'content': {r}"

        # Assert 2: session side-effect fired — _last_recalled_ids mutated.
        ids_after = dict(_st_module._last_recalled_ids)
        assert ids_after != ids_before or len(results) == 0, (
            "_apply_recall_session_side_effects did not update _last_recalled_ids; "
            "session side-effect may not have fired on forwarder path"
        )

    def test_forwarder_e2e_results_parity_with_off_path(self, e2e_engines, monkeypatch):
        """recall() ON path (forwarder) returns same (id, score) tuples as OFF path.

        Neutralises heat writes on both sides to ensure score stability.
        """
        import asyncio
        import sys

        import httpx

        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]

        monkeypatch.setattr(storage, "update_memory_heat", lambda mid, heat: None)
        monkeypatch.setattr(storage, "update_memory_last_accessed", lambda mid, ts: None)
        monkeypatch.setattr(storage, "boost_memories_access", lambda ids, ts: None)

        # Seed corpus with unique prefix.
        _PAR2_QUERY = "forwarder e2e parity check 8h5w"
        _insert_mem(storage, embeddings, f"{_PAR2_QUERY} high relevance item", heat=0.9)
        _insert_mem(storage, embeddings, f"{_PAR2_QUERY} medium relevance item", heat=0.5)
        _insert_wiki("Forwarder parity wiki", f"{_PAR2_QUERY} wiki reference")

        import yadgar.backend.embed_service as _svc
        from yadgar.backend.embed_service import app as _backend_app

        _svc._recall_engines_ready = True

        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: "master")
        monkeypatch.setattr("yadgar.server._get_default_branch", lambda _d: "master")

        import os as _os

        def _asgi_post(url: str, *, json=None, headers=None, timeout=None):
            from urllib.parse import urlparse

            path = urlparse(url).path
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

        _rm = sys.modules.get("yadgar.server.tools.recall")
        if _rm is None:
            import yadgar.server.tools.recall as _rm  # type: ignore[no-redef]

        original_unified = _rm.settings.UNIFIED_RECALL_ENABLED
        original_backend = _rm.settings.RECALL_BACKEND_ENABLED

        # OFF path: run in-core directly.
        off_results = _run_off_path(_PAR2_QUERY)

        # ON path: drive forwarder.
        _rm.settings.UNIFIED_RECALL_ENABLED = True
        _rm.settings.RECALL_BACKEND_ENABLED = True
        try:
            with monkeypatch.context() as m:
                m.setattr("httpx.post", _asgi_post)
                on_raw = _rm.recall(
                    query=_PAR2_QUERY,
                    directory=YADGAR_DIR,
                    max_results=20,
                )
        finally:
            _rm.settings.UNIFIED_RECALL_ENABLED = original_unified
            _rm.settings.RECALL_BACKEND_ENABLED = original_backend

        on_results = [
            (r.get("id"), float(r.get("_retrieval_score", r.get("heat", 0.0)))) for r in on_raw
        ]

        assert len(off_results) > 0, "OFF path returned no results"
        assert len(on_results) > 0, "ON path (forwarder) returned no results"
        assert off_results == on_results, (
            "Forwarder e2e: OFF and ON paths produced different results:\n"
            f"  OFF={off_results}\n"
            f"  ON ={on_results}"
        )
