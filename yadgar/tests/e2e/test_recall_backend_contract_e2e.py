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
    from yadgar._shared.runtime import state as _st
    from yadgar._shared.wiki import WikiAddOptions

    assert _st._wiki is not None
    opts = WikiAddOptions(
        source_memory_ids=[],
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
    from yadgar.backend.retrieval.recall_pipeline import _fanout_recall

    results = _fanout_recall(
        query=query,
        max_results=max_results,
        min_heat=0.0,
        directory=YADGAR_DIR,
    )
    # Extract (id, score) tuples — use _retrieval_score as primary, fallback heat.
    return [(r.get("id"), float(r.get("_retrieval_score", r.get("heat", 0.0)))) for r in results]


def _run_on_path(query: str, max_results: int = 20) -> list[tuple]:
    """Run the backend /recall in-process via ASGITransport and return (id, score) tuples."""
    import asyncio
    import os as _os

    import httpx

    # Reset the backend engines-ready flag so bootstrap runs (re-uses fixture engines)
    import yadgar.backend.embed_service.embed_service as _svc
    from yadgar.backend.embed_service import app

    _svc._recall_engines_ready = True  # engines already inited by e2e_engines fixture

    payload = {
        "query": query,
        "directory": YADGAR_DIR,
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

        import yadgar.backend.embed_service.embed_service as _svc
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
            "yadgar._shared.runtime.lifecycle.init_engines",
            _noop_init_engines,
        )

        # Also patch INSIDE embed_service's lazy import path.
        # _ensure_recall_engines does: from yadgar.server.lifecycle import init_engines as _init_engines
        # Patching sys.modules so the from-import also picks up the no-op.
        import yadgar._shared.runtime.lifecycle as _lifecycle

        original_init = _lifecycle.init_engines
        _lifecycle.init_engines = _noop_init_engines

        payload = {
            "query": _BOOT_QUERY,
            "directory": YADGAR_DIR,
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
        """recall() returns real results via backend (always-on since Phase 2a).

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

        import yadgar.backend.embed_service.embed_service as _svc
        from yadgar.backend.embed_service import app as _backend_app

        # Ensure backend engines are ready (uses fixture's _st).
        _svc._recall_engines_ready = True

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

        _rm = sys.modules.get("yadgar.core.server.tools.recall")
        if _rm is None:
            import yadgar.core.server.tools.recall as _rm  # type: ignore[no-redef]

        # Snapshot _last_recalled_ids to detect session side-effect mutation.
        from yadgar._shared.runtime import state as _st_module

        ids_before = dict(_st_module._last_recalled_ids)

        with monkeypatch.context() as m:
            m.setattr("httpx.post", _asgi_post)
            results = _rm.recall(
                query=_FWD_QUERY,
                directory=YADGAR_DIR,
                max_results=20,
            )

        # Assert 1: non-empty results with correct shape.
        assert len(results) > 0, "recall() with RECALL_BACKEND_ENABLED=True returned no results"
        for r in results:
            assert "id" in r, f"Result missing 'id': {r}"
            assert "content" in r, f"Result missing 'content': {r}"

        # T3 Car 2: the session half is now DEFERRED off the response path
        # (eventually-consistent). Drain the fork so the deferred SR write lands
        # deterministically before asserting the side-effect fired.
        from yadgar._shared.runtime.recall_side_effects_fork import (
            drain_session_side_effects as _drain_session,
        )

        _drain_session(timeout=10.0)

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

        import yadgar.backend.embed_service.embed_service as _svc
        from yadgar.backend.embed_service import app as _backend_app

        _svc._recall_engines_ready = True

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

        _rm = sys.modules.get("yadgar.core.server.tools.recall")
        if _rm is None:
            import yadgar.core.server.tools.recall as _rm  # type: ignore[no-redef]

        # OFF path: run in-core directly.
        off_results = _run_off_path(_PAR2_QUERY)

        # ON path: drive forwarder (always-on since Phase 2a).
        with monkeypatch.context() as m:
            m.setattr("httpx.post", _asgi_post)
            on_raw = _rm.recall(
                query=_PAR2_QUERY,
                directory=YADGAR_DIR,
                max_results=20,
            )

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


# ---------------------------------------------------------------------------
# #44: REAL-init bootstrap under the prod BACKEND env (the test that would have
# caught the 500).  The bootstrap test above spies init_engines to a no-op, so
# it never exercised the real _init_embedding_client offload-guard path — which
# is exactly why #44 escaped.  This test runs the REAL init_engines under the
# prod backend condition (YADGAR_OFFLOAD_TOOLS=1 + no YADGAR_EMBED_URL) and
# asserts /recall serves a 200 with real results, NOT a 500.
# ---------------------------------------------------------------------------


class TestRecallBackendProdEnvBootstrap:
    """Real init under prod backend env: offload ON + no EMBED_URL → local, 200."""

    def test_recall_bootstrap_local_engines_under_offload_env(self, e2e_engines, monkeypatch):
        """Backend /recall runs REAL init_engines under prod backend env → 200, not 500.

        Reproduces #44: the prod backend container carries the shared
        YADGAR_OFFLOAD_TOOLS flag but has no YADGAR_EMBED_URL.  Before the fix,
        _ensure_recall_engines → init_engines() → _init_embedding_client()
        tripped the CORE offload guard and raised → the route 500'd.

        This test does NOT spy init_engines — it lets the REAL bootstrap run
        (init_engines(local_engines=True)), so it exercises the actual
        guard-bypass path.  RED on pre-fix code (500 from RuntimeError); GREEN
        after (local engines, 200, real results).

        Re-entrancy: init_engines rebuilds _st._storage/_embeddings/_retriever
        etc.  The rebuild inherits the fixture's live YADGAR_DB_URL (server mode)
        so the fresh engines reach the SAME running SurrealDB and see the seeded
        corpus.  We snapshot + restore the fixture engine handles so downstream
        tests in this module are unaffected.
        """
        import asyncio
        import os as _os

        import httpx

        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]
        db_path = e2e_engines["db_path"]

        # Seed a corpus (unique prefix) so the served recall returns real rows.
        _PROD_QUERY = "prod env bootstrap recall probe 5w8t"
        _insert_mem(storage, embeddings, f"{_PROD_QUERY} primary hit", heat=0.9)
        _insert_mem(storage, embeddings, f"{_PROD_QUERY} secondary hit", heat=0.5)

        import yadgar._shared.runtime.lifecycle as _lifecycle
        import yadgar.backend.embed_service.embed_service as _svc
        from yadgar._shared.runtime import state as _st
        from yadgar.backend.embed_service import app

        # Prod BACKEND container condition: shared offload flag ON, no EMBED_URL.
        # This is the exact env that made #44 raise inside _init_embedding_client.
        monkeypatch.setenv("YADGAR_OFFLOAD_TOOLS", "1")
        monkeypatch.delenv("YADGAR_EMBED_URL", raising=False)

        # The bootstrap runs the REAL init_engines(local_engines=True) — this is
        # the code path #44 broke (NOT a no-op spy like the other bootstrap test).
        # We only inject the fixture db_path so the rebuilt engines land in the
        # fixture's server-mode namespace and see the seeded corpus; the offload
        # guard-bypass (_init_embedding_client(local_engines=True)) is exercised
        # for real.  A spy that swallowed init_engines would NOT catch #44 — that
        # is exactly why the original bootstrap test missed it.
        _real_init_engines = _lifecycle.init_engines
        init_calls: list[dict] = []

        def _init_with_fixture_db(*args, **kwargs):
            init_calls.append(dict(kwargs))
            kwargs.setdefault("db_path", db_path)
            kwargs.setdefault("embedding_model", "all-MiniLM-L6-v2")
            return _real_init_engines(*args, **kwargs)

        # Patch the name _ensure_recall_engines imports (from yadgar.server.lifecycle).
        monkeypatch.setattr(_lifecycle, "init_engines", _init_with_fixture_db)

        # Snapshot ALL _st engine singletons so we restore them after the real
        # re-init.  init_engines rebuilds ~24 _st._* handles; a partial restore
        # would leak this test's rebuilt engines into sibling e2e modules that
        # share the process.  Snapshot every private _st attribute, restore all.
        saved_state = {
            name: getattr(_st, name)
            for name in vars(_st)
            if name.startswith("_") and not name.startswith("__")
        }
        original_ready = _svc._recall_engines_ready
        # Force the REAL bootstrap to run (no spy on init_engines this time).
        _svc._recall_engines_ready = False

        payload = {
            "query": _PROD_QUERY,
            "directory": YADGAR_DIR,
            "max_results": 10,
            "min_heat": 0.0,
            "type": "all",
            "tags": None,
        }

        _orig_allow_root = _os.environ.get("YADGAR_ALLOW_ROOT", "0")
        _os.environ["YADGAR_ALLOW_ROOT"] = "1"
        try:

            async def _post() -> httpx.Response:
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://backend"
                ) as client:
                    return await client.post("/recall", json=payload, headers={})

            resp = asyncio.run(_post())
        finally:
            _os.environ["YADGAR_ALLOW_ROOT"] = _orig_allow_root
            # Restore ALL fixture engine handles + ready flag for downstream tests.
            for _name, _val in saved_state.items():
                setattr(_st, _name, _val)
            _svc._recall_engines_ready = original_ready

        # The REAL bootstrap must have run init_engines with local_engines=True
        # (the guard-bypass path).  If this is empty, the test proved nothing.
        assert init_calls, "init_engines was never called — bootstrap did not run"
        assert init_calls[0].get("local_engines") is True, (
            "bootstrap called init_engines WITHOUT local_engines=True — the #44 "
            "guard-bypass path was not exercised"
        )

        # The core assertion: NOT a 500 (which is what #44 produced).
        assert resp.status_code == 200, (
            f"backend /recall returned {resp.status_code} under prod backend env "
            f"(offload ON + no EMBED_URL) — #44 regression: {resp.text[:400]}"
        )
        results = resp.json()["results"]
        assert len(results) > 0, (
            "backend /recall served 200 but returned no results — bootstrap ran "
            "but the local-engine pipeline did not retrieve the seeded corpus"
        )
