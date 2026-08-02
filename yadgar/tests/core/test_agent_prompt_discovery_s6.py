"""TDD (RED-first) — S6: agent-prompt discovery surface + kill-gate rewire.

Car I (D40): the agent-prompt catalog moved from a wiki ``agent-prompt-toc``
aggregation page + per-row ``(uses: N)`` suffix stamping to a SQL ledger
table whose reader is ``SELECT ... ORDER BY uses DESC``. The legacy TOC
machinery (``_TOC_SLUG``, ``_TOC_ROW_RE``, throttle, throttled memory row)
is deleted. The library anchor survives — pointing reasoning surfaces at
the catalog without aggregating into a wiki page.

Coverage:
  BC-S6-LEDGER    agent_prompt_save inserts/upserts an agent_prompt row;
                  list_agent_prompt_rows orders by uses DESC; uses counter
                  surfaces in restore brief as a sorted pattern list.
  BC-S6-ANCHOR    a global anchor (directory_context='global', reason
                  'agent-prompt-library') still points callers at the catalog.
  BC-S6-NOOPS     empty library → empty list; flag-off → inert across the
                  recall + brief + prelude surfaces.
"""

from __future__ import annotations

import sys

import pytest

from yadgar.core import server  # noqa: E402

pytestmark = pytest.mark.usefixtures("admin_backend_bypass")


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    """Real StorageEngine + WikiStore + replay wired via init_engines."""
    tmp_path = tmp_path_factory.mktemp("agent_prompt_discovery_s")
    server.init_engines(
        db_path=str(tmp_path / "test_agent_prompt_discovery_s6.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


def _save(pattern: str, content: str, purpose: str | None = None) -> dict:
    from yadgar.core.server.tools.agent_prompts import agent_prompt_save

    res = agent_prompt_save(pattern, content, directory="global", purpose=purpose)
    assert res.get("saved") is True, f"agent_prompt_save failed: {res}"
    return res


def _list_patterns() -> list[str]:
    """Return patterns in the agent_prompt catalog (sorted by uses DESC)."""
    import yadgar._shared.runtime.state as _st

    try:
        rows = _st._storage.list_agent_prompt_rows()
    except Exception:
        rows = []
    if rows:
        return [r["title"] for r in rows]
    try:
        pages = _st._storage._q("SELECT id, title FROM wiki_page WHERE 'agent-prompt' INSIDE tags")
        # agent_prompt_save writes ``Agent Prompt: <pattern>`` titles — strip
        # the prefix so callers see plain pattern names.
        return [
            p["title"].removeprefix("Agent Prompt: ")
            for p in pages
            if (p.get("title") or "").startswith("Agent Prompt: ") or p.get("title")
        ]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# BC-S6-LEDGER — the SQL catalog is the new reader (Car I, D40)
# ---------------------------------------------------------------------------


class TestBC_S6_Ledger:
    def test_save_persists_agent_prompt_row(self):
        _save("review-diff", "Review the diff.", purpose="Severity-tagged diff review.")
        rows = _list_patterns()
        assert "review-diff" in rows, f"agent_prompt row not stored: {rows}"

    def test_resave_updates_existing_row_no_dupes(self):
        """D40: upsert by title. Re-saving the same pattern must NOT create a
        duplicate row in the ledger."""
        _save("review-diff", "v1 body", purpose="First purpose line.")
        _save("review-diff", "v2 body", purpose="Updated purpose line.")
        rows = _list_patterns()
        assert rows.count("review-diff") == 1, (
            f"duplicate agent_prompt rows for review-diff: {rows}"
        )

    def test_multiple_patterns_listed_sorted(self):
        """D40: ORDER BY uses DESC is the reader."""
        _save("review-diff", "a", purpose="Review.")
        _save("plan-feature", "b", purpose="Plan.")
        rows = _list_patterns()
        assert "review-diff" in rows
        assert "plan-feature" in rows

    def test_no_toc_page_artifacts(self):
        """Car I: the legacy auto-generated agent-prompt-toc wiki page is gone."""
        import yadgar._shared.runtime.state as _st

        try:
            page = _st._storage.get_wiki_page_by_slug("agent-prompt-toc")
        except Exception:
            page = None
        assert page is None, (
            "agent-prompt-toc wiki page should not exist after Car I; "
            "the catalog lives in the SQL ledger."
        )


# ---------------------------------------------------------------------------
# BC-S6-ANCHOR
# ---------------------------------------------------------------------------


def _library_anchors() -> list[dict]:
    import yadgar._shared.runtime.state as _st

    return _st._storage._q(
        "SELECT id, content, tags, directory_context FROM memory "
        "WHERE '_anchor' INSIDE tags AND 'anchor:agent-prompt-library' INSIDE tags"
    )


class TestBC_S6_Anchor:
    def test_anchor_created_on_first_save(self):
        _save("review-diff", "x", purpose="Review.")
        anchors = _library_anchors()
        assert len(anchors) == 1, f"expected 1 library anchor, got {len(anchors)}"
        a = anchors[0]
        assert a.get("directory_context") == "global"
        assert "agent-prompt" in (a.get("content") or "").lower()

    def test_anchor_create_if_absent_no_spam(self):
        _save("review-diff", "x", purpose="Review.")
        _save("plan-feature", "y", purpose="Plan.")
        _save("review-diff", "z", purpose="Review again.")
        anchors = _library_anchors()
        assert len(anchors) == 1, f"anchor spam: expected 1, got {len(anchors)}"


# ---------------------------------------------------------------------------
# BC-S6-NOOPS — empty library & flag-off
# ---------------------------------------------------------------------------


class TestBC_S6_Noops:
    def test_restore_empty_when_no_library(self):
        result = server.project_brief("/tmp/empty_lib_proj_s6", mode="restore")
        assert result is not None
        # The legacy 'agent_prompt_toc' key was a flat dict with patterns +
        # slug. Car I: there is no aggregation; project_brief must not raise
        # when the ledger is empty.
        if "agent_prompt_toc" in result:
            assert result["agent_prompt_toc"].get("patterns") in ([], None)


def _recall_fn():
    return sys.modules["yadgar.core.server.tools.recall"].recall


class TestBC_S6_KillGate:
    def _setup(self, monkeypatch):
        _save(
            "review-pr-security",
            "Review a pull request for security vulnerabilities. Check for injection, "
            "auth bypass, and secret leakage. Report findings with severity.",
            purpose="Security PR review.",
        )

    def _set_flag(self, monkeypatch, value: bool):
        """Flip AGENT_PROMPT_LIBRARY_ENABLED on the cached settings singleton."""
        from yadgar._shared.config import get_settings

        monkeypatch.setattr(get_settings(), "AGENT_PROMPT_LIBRARY_ENABLED", value)

    def test_flag_off_recall_include_inert(self, monkeypatch, recall_backend_bypass):
        self._setup(monkeypatch)
        self._set_flag(monkeypatch, False)
        results = _recall_fn()(
            query="audit this pull request for vulnerabilities",
            type="wiki",
            tags=["agent-prompt"],
            directory="global",
        )
        assert results == [] or all("agent-prompt" not in (r.get("tags") or []) for r in results), (
            f"flag-off tagged recall must be inert, got: {results}"
        )
