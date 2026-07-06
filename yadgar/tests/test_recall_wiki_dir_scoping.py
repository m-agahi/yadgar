"""v5.65.0 TDD: recall wiki-path directory scoping.

The bug: wiki-blend branch in recall() fetched _st._wiki.query() results and
filtered only by _retrieval_score and branch — no directory filter.  Wiki pages
stamped directory_context="/home/max/aws-work" leaked into a recall() scoped to
"/home/max/git/yadgar".

The fix: apply is_directory_eligible() to wiki results inside the wiki-blend
branch, the same way memories are filtered (~line 248-255).

Test strategy:
- Mock _st._wiki.query() to return two wiki dicts:
    1. aws_wiki: directory_context="/home/max/aws-work"  (must be filtered OUT)
    2. yadgar_wiki: directory_context="global"           (must stay IN)
- Both pass all other gates: branch=None (always eligible), score=0.9.
- Call recall() with directory="/home/max/git/yadgar".
- RED: before fix, aws_wiki leaks into results (AssertionError).
- GREEN: after fix, aws_wiki absent; yadgar_wiki present.

Mocking mirrors test_recall_wiki_metrics.py::_call_recall_mcp_tool().
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.usefixtures("recall_backend_bypass")

# ---------------------------------------------------------------------------
# Helpers (mirrored from test_recall_wiki_metrics.py)
# ---------------------------------------------------------------------------


def _make_fake_memory(mid: int = 1) -> dict:
    return {
        "id": mid,
        "content": f"memory {mid}",
        "heat": 0.5,
        "tags": [],
        "branch": None,
        "_retrieval_score": 0.5,
        "directory_context": "global",
    }


def _make_mock_storage() -> Any:
    storage = MagicMock()
    mems = [_make_fake_memory(1)]
    storage.search_memories_fts.return_value = mems
    storage.search_vectors.return_value = []
    storage.get_memory.return_value = mems[0]
    storage._now_iso.return_value = "2026-01-01T00:00:00"
    storage.update_memory_heat.return_value = None
    storage.update_memory_last_accessed.return_value = None
    return storage


def _make_mock_retriever(memories: list[dict] | None = None) -> Any:
    retriever = MagicMock()
    retriever.recall.return_value = memories if memories is not None else [_make_fake_memory(1)]
    return retriever


def _call_recall_with_wiki(
    query: str,
    directory: str | None,
    wiki_results: list[dict],
    max_results: int = 10,
) -> list[dict]:
    """Call recall() with controlled wiki mock and directory scoping.

    Returns the list of results from recall().
    """
    import yadgar.server._state as _st
    from yadgar.server.tools.recall import recall as recall_fn

    mock_retriever = _make_mock_retriever()
    mock_storage = _make_mock_storage()
    mock_wiki = MagicMock()
    mock_wiki.query.return_value = wiki_results

    with (
        patch.object(_st, "_retriever", mock_retriever),
        patch.object(_st, "_storage", mock_storage),
        patch.object(_st, "_consolidation", None),
        patch.object(_st, "_thermo", None),
        patch.object(_st, "_cognitive_map", None),
        patch.object(_st, "_buffer", None),
        patch.object(_st, "_replay", None),
        patch.object(_st, "_wiki", mock_wiki),
        patch.object(_st, "_last_recalled_ids", {}),
        patch("yadgar.server.tools.project._detect_branch", return_value=None),
        patch("yadgar.server.tools.project._get_default_branch", return_value="master"),
    ):
        return recall_fn(query=query, max_results=max_results, directory=directory)


# ---------------------------------------------------------------------------
# Wiki result factories
# ---------------------------------------------------------------------------

_QUERY = "wiki scoping directory test"  # must not be episodic (no temporal keywords)


def _make_aws_wiki() -> dict:
    """A wiki page stamped to /home/max/aws-work — must be excluded when caller is yadgar."""
    return {
        "id": 100,
        "slug": "aws-work-page",
        "title": "AWS work page",
        "content": "AWS infrastructure notes",
        "tags": [],
        "branch": None,  # in _allowed_branches → branch filter passes
        "directory_context": "/home/max/aws-work",
        "_retrieval_score": 0.9,  # high score → passes score gate
    }


def _make_yadgar_wiki() -> dict:
    """A global wiki page — always eligible regardless of caller dir."""
    return {
        "id": 200,
        "slug": "yadgar-global-page",
        "title": "Yadgar global page",
        "content": "Yadgar module notes",
        "tags": [],
        "branch": None,
        "directory_context": "global",
        "_retrieval_score": 0.8,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRecallWikiDirectoryScoping:
    def test_cross_project_wiki_does_not_leak(self):
        """Wiki page from /home/max/aws-work must NOT appear when recall is scoped to yadgar.

        RED: before fix, aws_wiki leaks because wiki-blend branch has no directory filter.
        GREEN: after fix, is_directory_eligible() filters it out.
        """
        aws_wiki = _make_aws_wiki()
        yadgar_wiki = _make_yadgar_wiki()

        results = _call_recall_with_wiki(
            query=_QUERY,
            directory="/home/max/git/yadgar",
            wiki_results=[aws_wiki, yadgar_wiki],
        )

        result_ids = [r.get("id") for r in results]
        assert 100 not in result_ids, (
            f"BUG: aws-work wiki (id=100) leaked into yadgar-scoped recall. "
            f"Result ids: {result_ids}. "
            f"The wiki-blend branch does not apply is_directory_eligible()."
        )

    def test_global_wiki_survives_directory_filter(self):
        """Wiki page with directory_context='global' must remain after directory filtering.

        Ensures we don't over-filter: the always-eligible sentinel 'global' must pass.
        """
        aws_wiki = _make_aws_wiki()
        yadgar_wiki = _make_yadgar_wiki()

        results = _call_recall_with_wiki(
            query=_QUERY,
            directory="/home/max/git/yadgar",
            wiki_results=[aws_wiki, yadgar_wiki],
        )

        result_ids = [r.get("id") for r in results]
        assert 200 in result_ids, (
            f"Regression: global wiki (id=200) was wrongly filtered out. "
            f"Result ids: {result_ids}. "
            f"'global' directory_context is always eligible."
        )

    def test_directory_none_raises_value_error(self):
        """v5.65 Fix D: directory=None is no longer silently allowed — must raise ValueError.

        Previous legacy mode (no-filter) is removed: callers MUST supply a directory.
        """
        import pytest

        aws_wiki = _make_aws_wiki()
        yadgar_wiki = _make_yadgar_wiki()

        with pytest.raises(ValueError, match="directory is required"):
            _call_recall_with_wiki(
                query=_QUERY,
                directory=None,
                wiki_results=[aws_wiki, yadgar_wiki],
            )

    def test_caller_dir_wiki_survives_directory_filter(self):
        """Wiki stamped with the exact caller dir must remain in results.

        Ensures the filter admits caller-dir-stamped pages, not just global ones.
        """
        yadgar_wiki_stamped = {
            "id": 300,
            "slug": "yadgar-stamped-page",
            "title": "Yadgar stamped page",
            "content": "Yadgar-specific module notes",
            "tags": [],
            "branch": None,
            "directory_context": "/home/max/git/yadgar",
            "_retrieval_score": 0.85,
        }

        results = _call_recall_with_wiki(
            query=_QUERY,
            directory="/home/max/git/yadgar",
            wiki_results=[yadgar_wiki_stamped],
        )

        result_ids = [r.get("id") for r in results]
        assert 300 in result_ids, (
            f"Wiki stamped with exact caller dir should be eligible. Result ids: {result_ids}."
        )
