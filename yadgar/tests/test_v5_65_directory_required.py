"""v5.65 Fix D — TDD tests (red-first).

Part 1: recall() and wiki_query() must raise ValueError when directory is omitted or empty.
Part 2: hook_prompt_recall must post-filter retriever results by caller directory.

These tests are written BEFORE implementation — they start RED.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers shared across parts
# ---------------------------------------------------------------------------


def _make_fake_memory(mid: int, directory_context: str = "global") -> dict:
    return {
        "id": mid,
        "content": f"memory {mid} in {directory_context}",
        "heat": 0.5,
        "tags": [],
        "branch": None,
        "_retrieval_score": 0.5,
        "directory_context": directory_context,
    }


def _make_mock_storage() -> Any:
    storage = MagicMock()
    storage.search_memories_fts.return_value = []
    storage.search_vectors.return_value = []
    storage.get_memory.return_value = _make_fake_memory(1)
    storage._now_iso.return_value = "2026-01-01T00:00:00"
    storage.update_memory_heat.return_value = None
    storage.update_memory_last_accessed.return_value = None
    return storage


def _make_mock_retriever(memories: list[dict] | None = None) -> Any:
    retriever = MagicMock()
    retriever.recall.return_value = memories if memories is not None else []
    return retriever


# ---------------------------------------------------------------------------
# Part 1 — recall() must raise ValueError when directory omitted or empty
# ---------------------------------------------------------------------------


class TestRecallDirectoryRequired:
    """recall() must raise ValueError immediately when directory not supplied."""

    def _call_recall(self, **kwargs):
        import yadgar.server._state as _st
        from yadgar.server.tools.recall import recall as recall_fn

        mock_retriever = _make_mock_retriever()
        mock_storage = _make_mock_storage()

        with (
            patch.object(_st, "_retriever", mock_retriever),
            patch.object(_st, "_storage", mock_storage),
            patch.object(_st, "_consolidation", None),
            patch.object(_st, "_thermo", None),
            patch.object(_st, "_cognitive_map", None),
            patch.object(_st, "_buffer", None),
            patch.object(_st, "_replay", None),
            patch.object(_st, "_wiki", None),
            patch.object(_st, "_last_recalled_ids", {}),
            patch("yadgar.server.tools.project._detect_branch", return_value=None),
            patch("yadgar.server.tools.project._get_default_branch", return_value="master"),
        ):
            return recall_fn(**kwargs)

    def test_recall_no_directory_raises(self):
        """RED: recall(query) without directory must raise ValueError.

        Pre-fix: silently returns results.
        Post-fix: raises immediately with "directory is required".
        """
        with pytest.raises(ValueError, match="directory is required"):
            self._call_recall(query="test query")

    def test_recall_directory_none_raises(self):
        """RED: recall(query, directory=None) must raise ValueError.

        Pre-fix: silently runs legacy all-pass mode.
        Post-fix: raises immediately.
        """
        with pytest.raises(ValueError, match="directory is required"):
            self._call_recall(query="test query", directory=None)

    def test_recall_directory_empty_string_raises(self):
        """RED: recall(query, directory='') must raise ValueError.

        Empty string after strip is not a valid directory.
        """
        with pytest.raises(ValueError, match="directory is required"):
            self._call_recall(query="test query", directory="")

    def test_recall_directory_whitespace_raises(self):
        """recall(query, directory='   ') must raise ValueError.

        Whitespace-only after strip equals empty.
        """
        with pytest.raises(ValueError, match="directory is required"):
            self._call_recall(query="test query", directory="   ")

    def test_recall_valid_directory_does_not_raise(self):
        """recall(query, directory='/home/max/git/yadgar') must NOT raise."""
        # Should return a list (possibly empty)
        result = self._call_recall(query="test query", directory="/home/max/git/yadgar")
        assert isinstance(result, list)

    def test_recall_raises_before_storage_access(self):
        """ValueError must fire BEFORE any storage/retriever access.

        Verify: if storage is None, directory-missing should still raise (not a
        storage-not-init error).
        """
        import yadgar.server._state as _st
        from yadgar.server.tools.recall import recall as recall_fn

        with (
            patch.object(_st, "_storage", None),
            patch.object(_st, "_retriever", None),
            patch.object(_st, "_consolidation", None),
        ):
            with pytest.raises(ValueError, match="directory is required"):
                recall_fn(query="test", directory=None)


# ---------------------------------------------------------------------------
# Part 1 — wiki_query() must raise ValueError when directory omitted or empty
# ---------------------------------------------------------------------------


class TestWikiQueryDirectoryRequired:
    """wiki_query() must raise ValueError immediately when directory not supplied."""

    def _call_wiki_query(self, **kwargs):
        import yadgar.server._state as _st
        from yadgar.server.tools.wiki import wiki_query as wq_fn

        mock_wiki = MagicMock()
        mock_wiki.query.return_value = []

        with (
            patch.object(_st, "_wiki", mock_wiki),
            patch("yadgar.server.tools.project._detect_branch", return_value=None),
            patch("yadgar.server.tools.project._get_default_branch", return_value="master"),
        ):
            return wq_fn(**kwargs)

    def test_wiki_query_no_directory_raises(self):
        """RED: wiki_query(query) without directory must raise ValueError.

        Pre-fix: silently returns results from all directories.
        Post-fix: raises immediately with "directory is required".
        """
        with pytest.raises(ValueError, match="directory is required"):
            self._call_wiki_query(query="test query")

    def test_wiki_query_directory_none_raises(self):
        """RED: wiki_query(query, directory=None) must raise ValueError."""
        with pytest.raises(ValueError, match="directory is required"):
            self._call_wiki_query(query="test query", directory=None)

    def test_wiki_query_directory_empty_raises(self):
        """RED: wiki_query(query, directory='') must raise ValueError."""
        with pytest.raises(ValueError, match="directory is required"):
            self._call_wiki_query(query="test query", directory="")

    def test_wiki_query_valid_directory_does_not_raise(self):
        """wiki_query with valid directory must NOT raise."""
        result = self._call_wiki_query(query="test", directory="/home/max/git/yadgar")
        assert isinstance(result, list)

    def test_wiki_query_raises_before_wiki_access(self):
        """ValueError must fire before any wiki store access."""
        import yadgar.server._state as _st
        from yadgar.server.tools.wiki import wiki_query as wq_fn

        with patch.object(_st, "_wiki", None):
            with pytest.raises(ValueError, match="directory is required"):
                wq_fn(query="test", directory=None)


# ---------------------------------------------------------------------------
# Part 2 — hook_prompt_recall must filter results by caller directory
# ---------------------------------------------------------------------------


def _make_yadgar_memory() -> dict:
    return {
        "id": 200,
        "content": "yadgar module design note",
        "heat": 0.8,
        "tags": [],
        "branch": None,
        "_retrieval_score": 0.8,
        "directory_context": "/home/max/git/yadgar",
    }


def _make_aws_memory() -> dict:
    return {
        "id": 100,
        "content": "aws IAM policy config",
        "heat": 0.7,
        "tags": [],
        "branch": None,
        "_retrieval_score": 0.7,
        "directory_context": "/home/max/aws-work",
    }


def _make_global_memory() -> dict:
    return {
        "id": 300,
        "content": "global yadgar rule",
        "heat": 0.6,
        "tags": [],
        "branch": None,
        "_retrieval_score": 0.6,
        "directory_context": "global",
    }


class TestHookPromptRecallDirectoryFiltering:
    """hook_prompt_recall must apply directory filter to retriever results.

    RED: pre-fix, aws-work memory leaks into prompt-recall when caller is /home/max/git/yadgar.
    GREEN: post-fix, only yadgar + global memories appear.
    """

    def _run_hook_prompt_recall(
        self,
        query: str,
        directory: str | None,
        retriever_results: list[dict],
    ) -> dict:
        """Call hook_prompt_recall with given directory + mock retriever results.

        Returns the JSON response body dict.
        """
        import yadgar.server._state as _st
        import yadgar.server.http as _http  # noqa: F401 — ensure routes registered
        from yadgar.server.http import hook_prompt_recall

        # Build fake retriever that returns given results
        mock_retriever = MagicMock()
        mock_retriever.recall.return_value = retriever_results

        # Build fake request
        query_params: dict[str, str] = {"query": query}
        if directory is not None:
            query_params["directory"] = directory

        class _FakeRequest:
            def __init__(self):
                self.query_params = query_params

        async def _run():
            with (
                patch.object(_st, "_retriever", mock_retriever),
                patch.object(_st, "_last_session_context", {}),
                patch.object(_st, "_last_prompt_recall", {}),
                patch("yadgar.server.http._build_dlq_alert_text", return_value=""),
                # _recall_with_timeout calls asyncio.to_thread(retriever.recall, ...).
                # Patch to_thread so it calls the retriever synchronously.
                patch(
                    "asyncio.to_thread",
                    side_effect=lambda fn, *a, **kw: asyncio.coroutine(lambda: fn(*a, **kw))(),
                ),
            ):
                # Also override wait_for so the timeout wrapper works synchronously
                async def _fake_to_thread(fn, *args, **kwargs):
                    return fn(*args, **kwargs)

                with patch("asyncio.to_thread", side_effect=_fake_to_thread):
                    resp = await hook_prompt_recall(_FakeRequest())
                    return resp.body if hasattr(resp, "body") else {}

        raw = asyncio.run(_run())
        import json

        if isinstance(raw, bytes):
            return json.loads(raw)
        return raw

    def test_aws_work_memory_excluded_when_caller_is_yadgar(self):
        """RED: aws-work memory must NOT appear in prompt-recall scoped to yadgar dir.

        Pre-fix: retriever.recall returns mixed results; hook writes all of them
        into the response text, including aws-work content.
        Post-fix: directory filter excludes aws-work memory.
        """
        results_mixed = [_make_aws_memory(), _make_yadgar_memory(), _make_global_memory()]
        body = self._run_hook_prompt_recall(
            query="yadgar scoping test",
            directory="/home/max/git/yadgar",
            retriever_results=results_mixed,
        )
        text = body.get("text", "")
        assert "aws IAM policy config" not in text, (
            f"BUG: aws-work memory leaked into prompt-recall scoped to /home/max/git/yadgar.\n"
            f"Response text: {text!r}\n"
            "hook_prompt_recall does not apply directory filter to retriever results."
        )

    def test_yadgar_and_global_memory_retained(self):
        """yadgar-dir and global memories must appear when caller is yadgar dir."""
        results_mixed = [_make_aws_memory(), _make_yadgar_memory(), _make_global_memory()]
        body = self._run_hook_prompt_recall(
            query="yadgar scoping test",
            directory="/home/max/git/yadgar",
            retriever_results=results_mixed,
        )
        text = body.get("text", "")
        assert "yadgar module design note" in text, (
            f"Yadgar-dir memory missing from prompt-recall. text={text!r}"
        )
        assert "global yadgar rule" in text, (
            f"Global memory missing from prompt-recall. text={text!r}"
        )

    def test_missing_directory_param_does_not_use_getcwd(self):
        """When directory query param is absent, hook must NOT call os.getcwd().

        Pre-fix: os.getcwd() is used as fallback → container path mis-scopes.
        Post-fix: directory is None → skip filter with warning (do not getcwd).
        """
        results = [_make_aws_memory()]
        # With no directory param, we can't assert scoping (no dir = skip filter),
        # but we CAN assert getcwd is never called.
        with patch("os.getcwd") as mock_getcwd:
            self._run_hook_prompt_recall(
                query="test",
                directory=None,
                retriever_results=results,
            )
            assert not mock_getcwd.called, (
                "hook_prompt_recall must NOT call os.getcwd() as directory fallback. "
                "Container cwd would mis-scope results."
            )
