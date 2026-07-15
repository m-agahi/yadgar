"""Car 0 — canonical-write + trusted-gitness branch model (§0.9 TDD).

Covers the 5 flows + provenance/non-forgeability + restart-safety + cache
read-through/invalidation + the _get_default_branch trusted-var fix.

The daemon decides the branch outcome at the MCP boundary (wiki.py
_check_wiki_add_context) from the TRUSTED per-directory gitness, read via the
dir_branch_context read-through cache which fills from the durable backend store.

Tests use the admin_backend_bypass fixture so _forward_admin runs the real
run_admin_op against the same _st storage (durable-store writes are real).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("admin_backend_bypass")

_GIT_DIR = "/home/user/car0-git-proj"
_NONGIT_DIR = "/home/user/car0-nongit-proj"
_UNKNOWN_DIR = "/home/user/car0-never-seen"


@pytest.fixture(autouse=True)
def _engines(tmp_path_factory):
    from yadgar.core import server

    tmp_path = tmp_path_factory.mktemp("car0")
    db_path = str(tmp_path / "test.db")
    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


@pytest.fixture(autouse=True)
def _reset_dir_branch_cache():
    """Empty the dir_branch_context cache per test (avoid cross-test bleed).

    The Cache self-registers by name and raises on a duplicate, so we clear() the
    singleton's store rather than rebuild it.
    """
    from yadgar.core.server.tools import _dir_branch

    _dir_branch._get_cache().clear()
    yield
    _dir_branch._get_cache().clear()


def _enforce_on(monkeypatch):
    monkeypatch.setenv("YADGAR_BRANCH_ENFORCEMENT", "true")
    monkeypatch.setenv("YADGAR_DIRECTORY_ENFORCEMENT", "true")


def _seed_dir(directory: str, gitness: bool, default_branch):
    """Simulate a SessionStart upsert of the trusted vars via the durable store."""
    from yadgar.core.server.tools._forward import _forward_admin

    _forward_admin(
        "upsert_dir_branch_context",
        {"directory": directory, "gitness": gitness, "default_branch": default_branch},
    )


def _page_branch(slug: str):
    from yadgar.core import server

    page = server._wiki._storage.get_wiki_page_by_slug(slug)
    assert page is not None, f"page {slug!r} not found after drain"
    return page.get("branch")


# ── §0.4 flow table ──────────────────────────────────────────────────────────


class TestFlows:
    def test_flow2a_git_with_branch_hint_is_branch_scoped(self, monkeypatch, _unit_backend_harness):
        """Flow 2a: normal page, gitness=true, branch_hint present → branch-scoped."""
        _enforce_on(monkeypatch)
        _seed_dir(_GIT_DIR, gitness=True, default_branch="master")
        drainer = _unit_backend_harness
        from yadgar.core import server

        res = server.wiki_add(
            title="Flow 2a page",
            content="branch-scoped content for flow 2a",
            directory=_GIT_DIR,
            branch_hint="feat/xyz",
        )
        assert "slug" in res, res
        drainer.drain_now()
        assert _page_branch(res["slug"]) == "feat/xyz"

    def test_flow2b_git_without_branch_rejects(self, monkeypatch, _unit_backend_harness):
        """Flow 2b: normal page, gitness=true, branch missing → REJECT missing_branch."""
        _enforce_on(monkeypatch)
        _seed_dir(_GIT_DIR, gitness=True, default_branch="master")
        from yadgar.core import server

        res = server.wiki_add(
            title="Flow 2b page",
            content="should be rejected — git dir, no branch",
            directory=_GIT_DIR,
        )
        assert res.get("error") == "missing_branch", res
        assert res.get("stored") is False

    def test_flow3_nongit_is_canonical_hint_ignored(self, monkeypatch, _unit_backend_harness):
        """Flow 3: normal page, gitness=false → canonical (branch IS NULL); hint ignored."""
        _enforce_on(monkeypatch)
        _seed_dir(_NONGIT_DIR, gitness=False, default_branch=None)
        drainer = _unit_backend_harness
        from yadgar.core import server

        res = server.wiki_add(
            title="Flow 3 page",
            content="canonical content for a non-git directory",
            directory=_NONGIT_DIR,
            branch_hint="feat/should-be-ignored",
        )
        assert "slug" in res, res
        drainer.drain_now()
        assert _page_branch(res["slug"]) is None  # canonical slot; hint ignored

    def test_flow4_unknown_dir_requires_branch_hint(self, monkeypatch, _unit_backend_harness):
        """Flow 4: unknown directory (no SessionStart row) → require branch_hint."""
        _enforce_on(monkeypatch)
        from yadgar.core import server

        # missing branch → REJECT
        res = server.wiki_add(
            title="Flow 4 reject",
            content="unknown dir, no branch → reject",
            directory=_UNKNOWN_DIR,
        )
        assert res.get("error") == "missing_branch", res

    def test_flow4_unknown_dir_with_branch_hint_proceeds(self, monkeypatch, _unit_backend_harness):
        """Flow 4: unknown dir + branch_hint → branch-scoped (conservative)."""
        _enforce_on(monkeypatch)
        drainer = _unit_backend_harness
        from yadgar.core import server

        res = server.wiki_add(
            title="Flow 4 proceed",
            content="unknown dir but a branch hint is supplied",
            directory=_UNKNOWN_DIR,
            branch_hint="feat/known-branch",
        )
        assert "slug" in res, res
        drainer.drain_now()
        assert _page_branch(res["slug"]) == "feat/known-branch"


# ── §0.5 flow 1: sanctioned canonical helper ──────────────────────────────────


class TestCanonicalHelper:
    def test_flow1_canonical_helper_writes_null_branch_regardless_of_gitness(
        self, monkeypatch, _unit_backend_harness
    ):
        """Flow 1: sanctioned helper → canonical (branch IS NULL) even in a git dir."""
        _enforce_on(monkeypatch)
        _seed_dir(_GIT_DIR, gitness=True, default_branch="master")
        drainer = _unit_backend_harness
        from yadgar.core.server.tools import wiki as wiki_tools

        payload = {
            "wiki_schema_version": 2,
            "slug": "car0-adr-log",
            "title": "Car0 ADR Log",
            "content": "## Context\n## Decision\n## Consequences\n",
            "category": "decision",
            "tags": ["adr", "decisions"],
            "confidence": "high",
            "directory_context": _GIT_DIR,
            "page_type": "adr",
        }
        res = wiki_tools._wiki_write_canonical(payload)
        assert res.get("stored") is True, res
        drainer.drain_now()
        assert _page_branch("car0-adr-log") is None  # canonical despite git dir

    def test_canonical_helper_rejects_non_allowlisted_page_type(self, monkeypatch):
        """Defense-in-depth: helper refuses a page_type outside CANONICAL_PAGE_TYPES."""
        from yadgar.core.server.tools import wiki as wiki_tools

        payload = {"slug": "x", "title": "X", "content": "y", "page_type": "reference"}
        with pytest.raises(ValueError, match="page_type"):
            wiki_tools._wiki_write_canonical(payload)

    def test_canonical_page_types_allowlist(self):
        from yadgar.core.server.tools import wiki as wiki_tools

        assert wiki_tools.CANONICAL_PAGE_TYPES == frozenset({"task_list", "adr"})


# ── provenance / non-forgeability ─────────────────────────────────────────────


class TestNonForgeability:
    def test_wiki_add_has_no_internal_param(self):
        """The model-facing wiki_add signature exposes NO _internal / branch=None trick."""
        import inspect

        from yadgar.core.server.tools import wiki as wiki_tools

        params = set(inspect.signature(wiki_tools.wiki_add).parameters)
        assert "_internal" not in params
        assert "gitness" not in params

    def test_no_model_callable_tool_sets_gitness(self):
        """gitness/default_branch are set ONLY via the durable upsert admin op,
        never a registered MCP @_tool."""
        # The upsert is a backend admin op, not a core MCP tool. Assert no tool
        # named for the trusted vars exists on the tools surface.
        from yadgar.core.server import tools as _tools_pkg
        from yadgar.core.server._app import _tool  # noqa: F401 — import proves the surface

        names = dir(_tools_pkg)
        assert "upsert_dir_branch_context" not in names
        assert "set_gitness" not in names

    def test_flow3_sets_internal_from_trusted_gitness_not_a_param(
        self, monkeypatch, _unit_backend_harness
    ):
        """A model passing a bogus branch_hint to a NON-git dir still lands canonical —
        the decision falls out of trusted gitness, not the caller arg."""
        _enforce_on(monkeypatch)
        _seed_dir(_NONGIT_DIR, gitness=False, default_branch=None)
        drainer = _unit_backend_harness
        from yadgar.core import server

        res = server.wiki_add(
            title="Forge attempt",
            content="model tries to force a branch on a non-git dir",
            directory=_NONGIT_DIR,
            branch="feat/forged",
            branch_hint="feat/forged",
        )
        drainer.drain_now()
        assert _page_branch(res["slug"]) is None  # trusted gitness wins


# ── restart-safety + durable store ────────────────────────────────────────────


class TestDurableStore:
    def test_durable_store_survives_cache_reset(self, monkeypatch):
        """The durable row is readable without a fresh SessionStart (restart-sim):
        clearing the in-memory cache still yields the trusted context from backend."""
        from yadgar.core.server.tools import _dir_branch

        _seed_dir(_GIT_DIR, gitness=True, default_branch="main")
        # Simulate daemon restart: wipe the in-memory cache (durable store intact).
        _dir_branch._get_cache().clear()
        ctx = _dir_branch.get_context(_GIT_DIR)
        assert ctx["found"] is True
        assert ctx["gitness"] is True
        assert ctx["default_branch"] == "main"

    def test_unknown_dir_returns_not_found(self, monkeypatch):
        from yadgar.core.server.tools import _dir_branch

        ctx = _dir_branch.get_context(_UNKNOWN_DIR)
        assert ctx["found"] is False


# ── cache read-through + invalidation ─────────────────────────────────────────


class TestCache:
    def test_read_through_fills_on_miss_then_hits(self, monkeypatch):
        from yadgar.core.server.tools import _dir_branch

        _seed_dir(_GIT_DIR, gitness=True, default_branch="master")
        cache = _dir_branch._get_cache()

        calls = {"n": 0}
        import yadgar.core.server.tools._forward as _fwd

        real = _fwd._forward_admin

        def _counting(op, payload, timeout_s=30.0):
            if op == "get_dir_branch_context":
                calls["n"] += 1
            return real(op, payload)

        monkeypatch.setattr(_fwd, "_forward_admin", _counting)

        ctx1 = _dir_branch.get_context(_GIT_DIR)  # miss → 1 backend read + fill
        ctx2 = _dir_branch.get_context(_GIT_DIR)  # hit → no backend read
        assert ctx1["gitness"] is True and ctx2["gitness"] is True
        assert calls["n"] == 1, "second get must be a cache hit (no backend read)"
        assert cache.get(_GIT_DIR) is not None

    def test_invalidate_clears_stale_entry(self, monkeypatch):
        from yadgar.core.server.tools import _dir_branch

        _seed_dir(_GIT_DIR, gitness=True, default_branch="master")
        _dir_branch.get_context(_GIT_DIR)  # fill
        assert _dir_branch._get_cache().get(_GIT_DIR) is not None
        _dir_branch.invalidate(_GIT_DIR)
        assert _dir_branch._get_cache().get(_GIT_DIR) is None

    def test_backend_error_is_failsafe_not_canonical(self, monkeypatch):
        """A backend read error surfaces as error=True (→ require branch_hint),
        NEVER silently found/canonical."""
        import yadgar.core.server.tools._forward as _fwd
        from yadgar.core.server.tools import _dir_branch

        def _boom(op, payload, timeout_s=30.0):
            raise RuntimeError("backend down")

        monkeypatch.setattr(_fwd, "_forward_admin", _boom)
        ctx = _dir_branch.get_context(_GIT_DIR)
        assert ctx.get("error") is True
        assert ctx.get("found") is False


# ── _get_default_branch trusted-var fix (§0.6) ────────────────────────────────


class TestGetDefaultBranch:
    def test_returns_trusted_default_for_git_dir(self, monkeypatch):
        from yadgar.core.server.tools.project import _get_default_branch

        _seed_dir(_GIT_DIR, gitness=True, default_branch="main")
        assert _get_default_branch(_GIT_DIR) == "main"

    def test_returns_none_for_nongit_dir(self, monkeypatch):
        from yadgar.core.server.tools.project import _get_default_branch

        _seed_dir(_NONGIT_DIR, gitness=False, default_branch=None)
        assert _get_default_branch(_NONGIT_DIR) is None

    def test_returns_none_for_unknown_dir_never_master(self, monkeypatch):
        from yadgar.core.server.tools.project import _get_default_branch

        assert _get_default_branch(_UNKNOWN_DIR) is None  # NOT a manufactured "master"


# ── drainer honors + strips _internal (confirm, don't rebuild) ────────────────


class TestDrainerInternal:
    def test_flow3_internal_stripped_before_db(self, monkeypatch, _unit_backend_harness):
        """The _internal token set by flow 3 is stripped before the DB write."""
        _enforce_on(monkeypatch)
        _seed_dir(_NONGIT_DIR, gitness=False, default_branch=None)
        drainer = _unit_backend_harness
        from yadgar.core import server

        res = server.wiki_add(
            title="Internal strip check",
            content="non-git canonical write, _internal must not persist",
            directory=_NONGIT_DIR,
        )
        drainer.drain_now()
        page = server._wiki._storage.get_wiki_page_by_slug(res["slug"])
        assert page is not None
        assert "_internal" not in page
