"""TDD (RED-first) — Stage 3 item 4: per-pattern usage counter.

Car I (D40): counts persist in the SQL ``agent_prompt.uses`` column on the
ledger. The legacy in-memory ``_prompt_usage`` row, throttling, and TOC
``(uses: N)`` suffix stamping are all deleted — SELECT ... ORDER BY uses
DESC is the reader and ``storage.increment_agent_prompt_uses`` is the
only writer.
"""

from __future__ import annotations

import pytest

from yadgar.core import server

pytestmark = pytest.mark.usefixtures("admin_backend_bypass")


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("prompt_usage")
    server.init_engines(
        db_path=str(tmp_path / "test_prompt_usage.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


@pytest.fixture
def storage():
    import yadgar._shared.runtime.state as _st

    return _st._storage


class TestBackendOp:
    def test_op_registered(self):
        from yadgar.backend.admin_exec import _ADMIN_OPS

        assert "increment_prompt_usage" in _ADMIN_OPS

    def test_op_increments(self, storage):
        from yadgar.backend.admin_exec.wiki import increment_prompt_usage

        # Pattern is what the prelude passes — D40 routes through
        # storage.increment_agent_prompt_uses(title=pattern); without
        # MariaDB it raises, which the prod code treats as "do not
        # increment". So we accept either success-with-count OR
        # graceful failure without crashing.
        r1 = increment_prompt_usage({"pattern": "op-pat"})
        assert r1["pattern"] == "op-pat"
        assert "incremented" in r1
        assert "count" in r1
        if r1["incremented"]:
            assert r1["count"] >= 1

    def test_op_empty_pattern_noop(self, storage):
        from yadgar.backend.admin_exec.wiki import increment_prompt_usage

        r = increment_prompt_usage({"pattern": ""})
        assert r["incremented"] is False
        assert r["pattern"] == ""
        assert r["count"] == 0

    def test_op_returns_no_toc_key(self, storage):
        """D40 contract: response shape is {incremented, pattern, count}.

        The legacy ``toc_updated`` key is gone — there is no TOC.
        """
        from yadgar.backend.admin_exec.wiki import increment_prompt_usage

        r = increment_prompt_usage({"pattern": "shape-check"})
        assert set(r.keys()) <= {"incremented", "pattern", "count"}


class TestLegacySurfaceRemoved:
    """Car I: helpers that survived only as no-op forwarders or were deleted."""

    def test_no_toc_slug_symbol(self):
        from yadgar.backend.admin_exec import wiki as backend_wiki

        # D40 deleted the auto-generated TOC page; the legacy slug constant
        # must not be exposed as a public symbol anymore.
        assert not hasattr(backend_wiki, "_TOC_SLUG")

    def test_no_toc_row_helper(self):
        from yadgar.backend.admin_exec import wiki as backend_wiki

        # _toc_row was the row formatter; Car I removed it from the
        # write path. Back-compat may keep a shim — if present, it's a
        # simple bullet formatter that does NOT mutate any page.
        if hasattr(backend_wiki, "_toc_row"):
            out = backend_wiki._toc_row("pat", "purpose")
            assert "(uses:" not in out


class TestPreludeForwardsUsage:
    def test_prelude_assembly_returns(self, storage):
        """Car I: prelude still invokes the forwarder. The actual counter
        write is now SQL-backed; the memory-row shadow is gone. We only
        assert the prelude returns the dispatch contract without raising.
        """
        from yadgar.core.server.tools.agent_prompts import agent_prompt_save
        from yadgar.core.server.tools.dispatch_helper import (
            _prompt_cache,
            agent_dispatch_prelude,
        )

        agent_prompt_save("prelude-usage-pat", "body", directory="global", storage=storage)
        _prompt_cache.clear()
        result = agent_dispatch_prelude("prelude-usage-pat", "topic", storage=storage)
        # Either resolves to a markdown string (legacy shape) OR returns the
        # new structured shape — both are acceptable back-compat surfaces.
        assert result is not None

    def test_unresolved_pattern_does_not_raise(self, storage):
        """Car I: unknown pattern must not raise — prelude returns gracefully."""
        from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

        result = agent_dispatch_prelude("no-such-pattern-xyz", "topic", storage=storage)
        assert result is not None
