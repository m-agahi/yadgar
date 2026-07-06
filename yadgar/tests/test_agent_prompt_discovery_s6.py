"""TDD (RED-first) — S6: agent-prompt discovery surface + kill-gate rewire.

Coverage:
  BC-S6-TOC        agent_prompt_save upserts a `pattern -> purpose` row into the
                   global `agent-prompt-toc` wiki page; re-save updates the line
                   (idempotent, no dupes).
  BC-S6-ANCHOR     a global anchor (directory_context='global', reason
                   'agent-prompt-library') pointing at the TOC exists after a save;
                   create-if-absent (no anchor spam on repeat saves).
  BC-S6-BRIEF      project_brief(mode='restore') surfaces `agent_prompt_toc`
                   (slug + capped pattern list) in an unrelated project dir.
  BC-S6-KILLGATE   AGENT_PROMPT_LIBRARY_ENABLED=False makes the library INERT:
                   recall(tags=['agent-prompt']) returns nothing, project_brief
                   omits the agent_prompt_toc surface, and agent_dispatch_prelude
                   injects no prompt. Flag True restores all three.
"""

from __future__ import annotations

import sys

import pytest

from yadgar import server  # noqa: E402


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
    from yadgar.server.tools.agent_prompts import agent_prompt_save

    res = agent_prompt_save(pattern, content, directory="global", purpose=purpose)
    assert res.get("saved") is True, f"agent_prompt_save failed: {res}"
    return res


def _read_toc() -> str | None:
    """Return the raw content of the global agent-prompt-toc page (or None)."""
    import yadgar.server._state as _st

    page = _st._storage.get_wiki_page_by_slug("agent-prompt-toc")
    return page.get("content") if page else None


# ---------------------------------------------------------------------------
# BC-S6-TOC
# ---------------------------------------------------------------------------


class TestBC_S6_TOC:
    def test_save_creates_toc_with_row(self):
        _save("review-diff", "Review the diff.", purpose="Severity-tagged diff review.")
        content = _read_toc()
        assert content is not None, "agent-prompt-toc page was not created on save"
        assert "review-diff" in content
        assert "Severity-tagged diff review." in content

    def test_resave_updates_row_no_dupes(self):
        _save("review-diff", "v1 body", purpose="First purpose line.")
        _save("review-diff", "v2 body", purpose="Updated purpose line.")
        content = _read_toc()
        assert content is not None
        # Exactly one row for the pattern (idempotent upsert).
        assert content.count("`review-diff`") == 1, f"duplicate TOC rows:\n{content}"
        assert "Updated purpose line." in content
        assert "First purpose line." not in content

    def test_multiple_patterns_listed(self):
        _save("review-diff", "a", purpose="Review.")
        _save("plan-feature", "b", purpose="Plan.")
        content = _read_toc()
        assert "`review-diff`" in content
        assert "`plan-feature`" in content


# ---------------------------------------------------------------------------
# BC-S6-ANCHOR
# ---------------------------------------------------------------------------


def _library_anchors() -> list[dict]:
    import yadgar.server._state as _st

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
        assert "agent-prompt-toc" in (a.get("content") or "")

    def test_anchor_create_if_absent_no_spam(self):
        _save("review-diff", "x", purpose="Review.")
        _save("plan-feature", "y", purpose="Plan.")
        _save("review-diff", "z", purpose="Review again.")
        anchors = _library_anchors()
        assert len(anchors) == 1, f"anchor spam: expected 1, got {len(anchors)}"


# ---------------------------------------------------------------------------
# BC-S6-BRIEF — project_brief restore surfaces the TOC
# ---------------------------------------------------------------------------


class TestBC_S6_Brief:
    def test_restore_surfaces_agent_prompt_toc(self):
        _save("review-diff", "a", purpose="Review.")
        _save("plan-feature", "b", purpose="Plan.")
        # Unrelated project dir — the global TOC must still surface.
        result = server.project_brief("/tmp/some_unrelated_proj_s6", mode="restore")
        assert "agent_prompt_toc" in result, (
            f"agent_prompt_toc missing from restore keys: {list(result.keys())}"
        )
        toc = result["agent_prompt_toc"]
        assert toc["slug"] == "agent-prompt-toc"
        assert "review-diff" in toc["patterns"]
        assert "plan-feature" in toc["patterns"]
        # Cheap: no full body, only the pattern list.
        assert "body" not in toc

    def test_restore_toc_empty_when_no_library(self):
        result = server.project_brief("/tmp/empty_lib_proj_s6", mode="restore")
        assert "agent_prompt_toc" in result
        assert result["agent_prompt_toc"]["patterns"] == []

    def test_toc_page_does_not_leak_into_general_recall(self, monkeypatch, recall_backend_bypass):
        """The global agent-prompt-toc page must NOT pollute general recall.

        Regression guard: the TOC carries tag 'agent-prompt-toc' (≠ 'agent-prompt'),
        so the default exclude must list it too — otherwise it reintroduces the
        every-project leak S3 exists to kill.
        """
        _save("review-diff", "x", purpose="Reusable subagent dispatch prompts.")
        results = _recall_fn()(
            query="reusable subagent dispatch prompts",
            type="wiki",
            directory="global",
        )
        assert all(r.get("slug") != "agent-prompt-toc" for r in results), (
            f"agent-prompt-toc leaked into general recall: {[r.get('slug') for r in results]}"
        )


# ---------------------------------------------------------------------------
# BC-S6-KILLGATE — flag False => library inert across all three surfaces
# ---------------------------------------------------------------------------


def _recall_fn():
    return sys.modules["yadgar.server.tools.recall"].recall


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
        from yadgar.config import get_settings

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

    def test_flag_on_recall_include_surfaces(self, monkeypatch, recall_backend_bypass):
        self._setup(monkeypatch)
        self._set_flag(monkeypatch, True)
        results = _recall_fn()(
            query="audit this pull request for vulnerabilities",
            type="wiki",
            tags=["agent-prompt"],
            directory="global",
        )
        assert results, "flag-on tagged recall must return the agent-prompt page"
        assert any("agent-prompt" in (r.get("tags") or []) for r in results)

    def test_flag_off_project_brief_no_toc(self, monkeypatch):
        self._setup(monkeypatch)
        self._set_flag(monkeypatch, False)
        result = server.project_brief("/tmp/killgate_proj_s6", mode="restore")
        assert "agent_prompt_toc" not in result, "flag-off restore must suppress agent_prompt_toc"

    def test_flag_on_project_brief_has_toc(self, monkeypatch):
        self._setup(monkeypatch)
        self._set_flag(monkeypatch, True)
        result = server.project_brief("/tmp/killgate_proj_s6", mode="restore")
        assert "agent_prompt_toc" in result

    def test_flag_off_dispatch_prelude_no_prompt(self, monkeypatch):
        self._setup(monkeypatch)
        self._set_flag(monkeypatch, False)
        import yadgar.server._state as _st
        from yadgar.server.tools.dispatch_helper import agent_dispatch_prelude

        prelude = agent_dispatch_prelude("review-pr-security", "review task", storage=_st._storage)
        assert "Agent-prompt" not in prelude, (
            f"flag-off dispatch prelude must inject no prompt, got:\n{prelude}"
        )

    def test_flag_on_dispatch_prelude_injects_prompt(self, monkeypatch):
        self._setup(monkeypatch)
        self._set_flag(monkeypatch, True)
        import yadgar.server._state as _st
        from yadgar.server.tools.dispatch_helper import agent_dispatch_prelude

        prelude = agent_dispatch_prelude("review-pr-security", "review task", storage=_st._storage)
        assert "Agent-prompt" in prelude
