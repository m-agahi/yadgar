"""TDD (RED-first) — Stage 3 item 4: per-pattern usage counter.

agent_dispatch_prelude increments a per-pattern counter on each assembly
(core forwards the write to the backend increment_prompt_usage admin op per
ADR-0078). Counts persist in a single global `_prompt_usage` memory row
(JSON dict, delete-then-insert like the _dispatch_prelude marker) and are
surfaced in the agent-prompt-toc page as a ` (uses: N)` row suffix —
throttled (count == 1 or count % 10 == 0) to bound wiki-version churn.
Dead patterns stay visible: no suffix = never dispatched.
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


class TestStorageCounter:
    def test_increment_returns_running_count(self, storage):
        assert storage.increment_prompt_usage("counter-pat-a") == 1
        assert storage.increment_prompt_usage("counter-pat-a") == 2
        assert storage.increment_prompt_usage("counter-pat-b") == 1

    def test_counts_persisted_as_single_row(self, storage):
        storage.increment_prompt_usage("counter-pat-c")
        counts = storage.get_prompt_usage_counts()
        assert counts.get("counter-pat-c", 0) >= 1
        rows = storage._q("SELECT id FROM memory WHERE '_prompt_usage' INSIDE tags")
        assert len(rows) == 1, f"expected exactly one _prompt_usage row, got {len(rows)}"


class TestBackendOp:
    def test_op_registered(self):
        from yadgar.backend.admin_exec import _ADMIN_OPS

        assert "increment_prompt_usage" in _ADMIN_OPS

    def test_op_increments(self, storage):
        from yadgar.backend.admin_exec.wiki import increment_prompt_usage

        r1 = increment_prompt_usage({"pattern": "op-pat"})
        assert r1["incremented"] is True
        assert r1["count"] == 1
        r2 = increment_prompt_usage({"pattern": "op-pat"})
        assert r2["count"] == 2

    def test_op_empty_pattern_noop(self, storage):
        from yadgar.backend.admin_exec.wiki import increment_prompt_usage

        r = increment_prompt_usage({"pattern": ""})
        assert r["incremented"] is False


class TestTocSurfacing:
    def test_first_use_stamps_toc_row(self, storage):
        """count==1 → the pattern's TOC row gains a (uses: 1) suffix."""
        from yadgar.backend.admin_exec.wiki import _TOC_SLUG, increment_prompt_usage
        from yadgar.core.server.tools.agent_prompts import agent_prompt_save

        agent_prompt_save("toc-count-pat", "body text", directory="global", storage=storage)
        r = increment_prompt_usage({"pattern": "toc-count-pat"})
        assert r["count"] == 1
        assert r["toc_updated"] is True

        toc = storage.get_wiki_page_by_slug(_TOC_SLUG)
        assert toc is not None
        assert "- `toc-count-pat` → " in toc["content"]
        assert "(uses: 1)" in toc["content"]

    def test_intermediate_counts_throttled(self, storage):
        """count==2..9 → no TOC write (throttle bounds version churn)."""
        from yadgar.backend.admin_exec.wiki import increment_prompt_usage
        from yadgar.core.server.tools.agent_prompts import agent_prompt_save

        agent_prompt_save("toc-throttle-pat", "body", directory="global", storage=storage)
        assert increment_prompt_usage({"pattern": "toc-throttle-pat"})["toc_updated"] is True
        for expected in range(2, 10):
            r = increment_prompt_usage({"pattern": "toc-throttle-pat"})
            assert r["count"] == expected
            assert r["toc_updated"] is False, f"count={expected} must be throttled"
        r10 = increment_prompt_usage({"pattern": "toc-throttle-pat"})
        assert r10["count"] == 10
        assert r10["toc_updated"] is True

    def test_uses_suffix_replaced_not_stacked(self, storage):
        from yadgar.backend.admin_exec.wiki import _TOC_SLUG, increment_prompt_usage
        from yadgar.core.server.tools.agent_prompts import agent_prompt_save

        agent_prompt_save("toc-replace-pat", "body", directory="global", storage=storage)
        for _ in range(10):
            increment_prompt_usage({"pattern": "toc-replace-pat"})
        toc = storage.get_wiki_page_by_slug(_TOC_SLUG)
        row = next(
            line for line in toc["content"].splitlines() if line.startswith("- `toc-replace-pat`")
        )
        assert row.count("(uses:") == 1, f"stacked suffixes: {row}"
        assert "(uses: 10)" in row

    def test_unknown_pattern_no_toc_update(self, storage):
        from yadgar.backend.admin_exec.wiki import increment_prompt_usage

        r = increment_prompt_usage({"pattern": "never-in-toc-pat"})
        assert r["incremented"] is True
        assert r["toc_updated"] is False


class TestPreludeForwardsUsage:
    def test_prelude_assembly_increments(self, storage):
        from yadgar.core.server.tools.agent_prompts import agent_prompt_save
        from yadgar.core.server.tools.dispatch_helper import (
            _prompt_cache,
            agent_dispatch_prelude,
        )

        agent_prompt_save("prelude-usage-pat", "body", directory="global", storage=storage)
        _prompt_cache.clear()
        before = storage.get_prompt_usage_counts().get("prelude-usage-pat", 0)
        agent_dispatch_prelude("prelude-usage-pat", "topic", storage=storage)
        after = storage.get_prompt_usage_counts().get("prelude-usage-pat", 0)
        assert after == before + 1

    def test_unresolved_pattern_not_counted(self, storage):
        from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

        agent_dispatch_prelude("no-such-pattern-xyz", "topic", storage=storage)
        counts = storage.get_prompt_usage_counts()
        assert "no-such-pattern-xyz" not in counts
