"""Car 1 — project_brief cache wiring + epoch-key normalization (v5.111).

MODEL-FREE: none of these tests call `init_engines` (which loads MiniLM/torch).
They monkeypatch the inner compute (`_project_brief_catalog_full`) and the git
resolver (`_resolve_project_root`) so no storage/model is touched.

The load-bearing correctness item is EPOCH-KEY NORMALIZATION: memorize bumps the
epoch under one directory key while project_brief reads it under another; if the
two keys differ (subdir vs git-root) the epoch is decorative and structural
writes never bust the cached brief. Both sides must normalize to git-root via the
SAME cached resolver.
"""

from __future__ import annotations

import pytest

from yadgar.core.cache import _REGISTRY


@pytest.fixture(autouse=True)
def _reset_epoch_and_cache(monkeypatch):
    from yadgar._shared.runtime import cache_epoch as _recall_shadow
    from yadgar.core.server.tools import project

    _recall_shadow._reset_for_test()
    # Isolate each test: clear stored entries AND reset the module singleton's
    # hit/miss/evict counters (they accumulate across tests otherwise).
    cache = getattr(project, "_project_brief_cache", None)
    if cache is not None:
        cache.clear()
        cache.hits = cache.misses = cache.evictions = 0
    yield
    _recall_shadow._reset_for_test()
    _REGISTRY.pop("project_brief", None)


# ── epoch-key normalization (the STOP-clause correctness item) ───────────────


def test_subdir_memorize_busts_parent_project_brief_epoch(monkeypatch):
    """A memorize into a SUBDIR must advance the epoch that the PARENT dir's
    project_brief key reads — i.e. both sides normalize to the same git-root.

    Written at the epoch layer with the resolver patched so subdir + parent both
    map to one git-root (a bare tmpdir would fall back to raw input on a non-git
    path and pass for the wrong reason)."""
    from yadgar._shared import server_helpers
    from yadgar._shared.runtime import cache_epoch as _recall_shadow

    ROOT = "/repo"

    def fake_resolve(directory: str) -> str:
        # Both the repo root and any subdir resolve to the same git-root.
        return ROOT

    # _bump_epoch_for_context resolves via server_helpers._resolve_project_root
    # (Car 1: both moved to _shared.server_helpers) — patch it there.
    monkeypatch.setattr(server_helpers, "_resolve_project_root", fake_resolve)

    # project_brief reads the epoch on the RESOLVED root.
    parent_resolved = server_helpers._resolve_project_root("/repo")
    epoch_before = _recall_shadow._current_epoch(parent_resolved)

    # A memorize into a subdir bumps via the SAME normalization the read uses.
    subdir_ctx = "/repo/subpkg/deep"
    server_helpers._bump_epoch_for_context(subdir_ctx)

    epoch_after = _recall_shadow._current_epoch(parent_resolved)
    assert epoch_after == epoch_before + 1, (
        "subdir memorize did not bust the parent project_brief epoch — "
        "epoch is decorative (key normalization mismatch)"
    )


def test_bump_helper_normalizes_before_bump(monkeypatch):
    """The shared bump helper resolves its dir arg to git-root before bumping,
    so a raw subdir and the git-root land on the SAME epoch key."""
    from yadgar._shared import server_helpers
    from yadgar._shared.runtime import cache_epoch as _recall_shadow

    ROOT = "/x/root"
    monkeypatch.setattr(server_helpers, "_resolve_project_root", lambda d: ROOT)

    e0 = _recall_shadow._current_epoch(ROOT)
    server_helpers._bump_epoch_for_context("/x/root/a/b")
    server_helpers._bump_epoch_for_context("/x/root")  # raw root, same normalization
    assert _recall_shadow._current_epoch(ROOT) == e0 + 2


# ── project_brief cache wiring ───────────────────────────────────────────────


def _patch_compute(monkeypatch, calls: list):
    """Patch the catalog/full compute to a model-free sentinel + count calls."""
    from yadgar.core.server.tools import project

    def fake_compute(ctx: dict) -> dict:
        calls.append(ctx["resolved"])
        return {"resolved": ctx["resolved"], "mode": ctx["mode"], "payload": {"heat": 1}}

    monkeypatch.setattr(project, "_project_brief_catalog_full", fake_compute)
    # Bypass the shared presence-row DB fetch (no storage in this test).
    monkeypatch.setattr(project, "_get_storage", lambda: None)
    monkeypatch.setattr(
        project,
        "_fetch_presence_rows",
        lambda storage, resolved: ([], [], []),
    )
    monkeypatch.setattr(project, "_get_current_branch", lambda resolved: "master")
    monkeypatch.setattr(project, "_resolve_project_root", lambda d: d)


def test_catalog_cache_miss_then_hit(monkeypatch):
    from yadgar.core.server.tools import project

    calls: list = []
    _patch_compute(monkeypatch, calls)

    r1 = project.project_brief("/repo", mode="catalog")
    r2 = project.project_brief("/repo", mode="catalog")
    assert r1 == r2
    assert len(calls) == 1, "second identical call should be served from cache (1 compute)"


def test_epoch_bump_busts_catalog_cache(monkeypatch):
    from yadgar._shared import server_helpers
    from yadgar.core.server.tools import project

    calls: list = []
    _patch_compute(monkeypatch, calls)

    project.project_brief("/repo", mode="catalog")  # miss → compute
    server_helpers._bump_epoch_for_context("/repo")  # structural write
    project.project_brief("/repo", mode="catalog")  # epoch moved → recompute
    assert len(calls) == 2


def test_deep_copy_isolation_returned_brief(monkeypatch):
    """Mutating the returned brief dict must not corrupt the cached value."""
    from yadgar.core.server.tools import project

    calls: list = []
    _patch_compute(monkeypatch, calls)

    first = project.project_brief("/repo", mode="catalog")
    first["payload"]["heat"] = 999
    first["injected"] = "mutation"
    second = project.project_brief("/repo", mode="catalog")  # still cached (1 compute)
    assert len(calls) == 1
    assert second["payload"]["heat"] == 1
    assert "injected" not in second


def test_signals_mode_bypasses_cache(monkeypatch):
    """signals mode has low staleness tolerance (drives stop-hook writes) → it is
    NOT served from the whole-payload cache (option A)."""
    from yadgar.core.server.tools import project

    calls: list = []
    _patch_compute(monkeypatch, calls)
    monkeypatch.setattr(
        project,
        "_project_brief_signals",
        lambda **kw: {"mode": "signals", "recommended_actions": []},
    )

    project.project_brief("/repo", mode="signals")
    project.project_brief("/repo", mode="signals")
    cache = project._project_brief_cache
    assert cache.stats()["hits"] == 0, "signals mode must never hit the cache"


def test_different_mode_different_key(monkeypatch):
    from yadgar.core.server.tools import project

    calls: list = []
    _patch_compute(monkeypatch, calls)

    project.project_brief("/repo", mode="catalog")
    project.project_brief("/repo", mode="full")
    assert len(calls) == 2, "catalog and full are distinct keys"
