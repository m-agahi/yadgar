"""C1 characterization — embed_service package public-surface + route parity.

Pins the split contract BEFORE/AFTER the C1 module split (module-standardization
train, task #18): every public symbol must stay resolvable on the canonical
``yadgar.backend.embed_service.embed_service`` submodule (tests rebind through
it) AND every FastAPI route must stay registered on ``app`` with the same
method + path. Model-free (no embedding/rerank model load) → fast gate.

This is the pilot car's parity guard. The slow ``/embed`` + ``/rerank``
behavioural parity run lives in the existing suites (semaphore / metrics /
v530 tests); this file guards the STRUCTURAL surface a split can silently break.
"""

from __future__ import annotations

# Symbols the test suite + uvicorn entrypoint reach via
# ``yadgar.backend.embed_service.embed_service.<name>`` (monkeypatch targets,
# route handlers, singletons, pydantic models, cache/knob helpers). If a split
# drops one from the canonical submodule namespace, importers break silently.
_REQUIRED_SUBMODULE_SYMBOLS = {
    # app + framework
    "app",
    "lifespan",
    "time",
    "os",
    # singletons / accessors (monkeypatched by string path + patch.object)
    "_get_engine",
    "_get_reranker",
    "_engine",
    "_reranker",
    "_ensure_recall_engines",
    "_recall_engines_ready",
    "_queue_drainer",
    "_dbsize_cache",
    "_dbsize_cache_ts",
    # cache instances + sync
    "_ce_cache",
    "_embed_cache",
    "_update_cache_metrics",
    # config / knob / ckpt / cache-maker helpers
    "CE_SCORING_VERSION",
    "_get_ce_checkpoint_hash",
    "_get_embed_checkpoint_hash",
    "_make_ce_cache",
    "_make_embed_cache",
    "_ce_cache_enabled",
    "_embed_cache_enabled",
    "_cache_snapshot_dir",
    "_cache_snapshot_interval_sec",
    "_dbsize_cache_ttl",
    "_shutdown_marker_path",
    "_rerank_acquire_timeout",
    "_make_rerank_semaphores",
    "_configure_torch_threads",
    # lifecycle background tasks
    "_run_cache_snapshot_task",
    "_run_model_warmup",
    # queue drainer lifecycle
    "_queue_base_path",
    "_start_queue_drainer",
    "_stop_queue_drainer",
    # auth
    "_require_admin_token",
    # pydantic models
    "EmbedRequest",
    "EmbedResponse",
    "RerankRequest",
    "RerankResponse",
    "RecallRequest",
    "RecallResponse",
    "RestoreRequest",
    "RestoreResponse",
    "ConsolidateRequest",
    "ConsolidateResponse",
    "AdminRequest",
    "AdminResponse",
    "VizRequest",
    "VizResponse",
    # route handlers + their helpers
    "embed",
    "rerank",
    "_score_ce_with_cache",
    "health",
    "admin_dbsize",
    "_walk_db_sizes",
    "recall_route",
    "_run_landscape_backend",
    "_forked_boost_write",
    "restore_route",
    "consolidate_route",
    "admin_route",
    "viz_route",
    "metrics",
}

# Every route the app must expose (method, path). A split that fails to import a
# route module drops routes silently — this pins the full set.
_REQUIRED_ROUTES = {
    ("GET", "/metrics"),
    ("POST", "/embed"),
    ("POST", "/rerank"),
    ("GET", "/health"),
    ("GET", "/admin/dbsize"),
    ("POST", "/recall"),
    ("POST", "/restore"),
    ("POST", "/consolidate"),
    ("POST", "/admin"),
    ("POST", "/viz"),
}


def test_canonical_submodule_exposes_all_symbols():
    import yadgar.backend.embed_service.embed_service as es

    missing = {name for name in _REQUIRED_SUBMODULE_SYMBOLS if not hasattr(es, name)}
    assert not missing, f"embed_service.embed_service missing symbols: {sorted(missing)}"


def test_package_forwards_symbols():
    """The package __getattr__ catch-all must still forward every symbol."""
    import yadgar.backend.embed_service as pkg

    # app is the uvicorn entrypoint attribute (yadgar.backend.embed_service:app)
    assert hasattr(pkg, "app")
    assert hasattr(pkg, "RecallRequest")
    assert hasattr(pkg, "_get_ce_checkpoint_hash")


def test_all_routes_registered():
    from yadgar.backend.embed_service.embed_service import app

    registered = {
        (method, route.path)
        for route in app.routes
        for method in getattr(route, "methods", set()) or set()
        if method in {"GET", "POST", "PUT", "DELETE", "PATCH"}
    }
    missing = _REQUIRED_ROUTES - registered
    assert not missing, f"routes dropped by split: {sorted(missing)}"
