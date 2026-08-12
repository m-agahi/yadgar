"""v5.65 Fix D — TDD tests (red-first).

Part 1: recall() and wiki_query() must raise ValueError when directory is omitted or empty.
        Still true, and still directory-keyed: the tool surface rejects a blank
        directory independently of scoping (ADR-0225 retired directory as a
        SCOPE key, not as a required parameter).
Part 2: hook_prompt_recall must post-filter retriever results by caller PROJECT.
        Was "by caller directory" — see ``TestHookPromptRecallProjectFiltering``
        for why the premise moved (Car C7 re-keyed the filter onto
        ``project_id`` + the ``'global'`` reach tag).

These tests are written BEFORE implementation — they start RED.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from yadgar._shared.errors import UnresolvedProjectError

pytestmark = pytest.mark.usefixtures("recall_backend_bypass")

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
    """recall() must fail loud immediately when nothing names a scope.

    CONTRACT MOVED, NOT DROPPED (C5 / ADR-0227). v5.65 Fix D added a
    ``ValueError("recall: directory is required")`` because the container
    cannot see the caller's tree, so a directory-less recall used to run in a
    legacy all-pass mode that returned another project's rows. C5 put the
    identity resolver AHEAD of that guard: ``_resolve_project_for_recall``
    resolves first and raises ``UnresolvedProjectError`` when neither
    ``project`` nor a session value names an identity, so the ValueError below
    it can no longer be reached (``project`` truthy short-circuits its second
    condition; ``project`` absent never gets there).

    These tests therefore assert the SAME property against the SAME inputs —
    a recall that names no scope fails immediately, before any storage or
    retriever access, rather than silently answering from the whole DB — with
    the exception type the boundary now raises. What is deliberately NOT done
    here is relaxing them to ``pytest.raises(Exception)``: the point of the
    v5.65 file is that this specific input is refused, and a test that accepts
    any exception would keep passing if the refusal became an unrelated crash.

    A directory alone is no longer sufficient (``test_recall_directory_without_project_raises``)
    and a project alone now IS (``test_recall_project_without_directory_is_accepted``);
    both are new, and together they pin which of the two arguments carries
    identity — the distinction the whole car exists to draw.
    """

    def _call_recall(self, **kwargs):
        import yadgar._shared.runtime.state as _st
        from yadgar.core.server.tools.recall import recall as recall_fn

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
        ):
            return recall_fn(**kwargs)

    def test_recall_no_directory_raises(self):
        """recall(query) with nothing naming a scope must fail loud.

        Pre-v5.65: silently returned results from the whole DB.
        Pre-C5: ValueError("directory is required").
        Post-C5: UnresolvedProjectError — the identity gate fires first.
        """
        with pytest.raises(UnresolvedProjectError, match="no project_id was supplied"):
            self._call_recall(query="test query")

    def test_recall_directory_none_raises(self):
        """recall(query, directory=None) must fail loud, not run a legacy all-pass."""
        with pytest.raises(UnresolvedProjectError, match="no project_id was supplied"):
            self._call_recall(query="test query", directory=None)

    def test_recall_directory_empty_string_raises(self):
        """Empty string after strip is not a valid scope."""
        with pytest.raises(UnresolvedProjectError, match="no project_id was supplied"):
            self._call_recall(query="test query", directory="")

    def test_recall_directory_whitespace_raises(self):
        """Whitespace-only after strip equals empty."""
        with pytest.raises(UnresolvedProjectError, match="no project_id was supplied"):
            self._call_recall(query="test query", directory="   ")

    def test_recall_directory_without_project_raises(self):
        """A directory is a filesystem hint, not an identity (ADR-0227).

        This is the assertion v5.65 could not make and C5 makes mandatory: the
        directory is well-formed and present, and the call is still refused,
        because the process answering it cannot see the tree that path names.
        """
        with pytest.raises(UnresolvedProjectError, match="no project_id was supplied"):
            self._call_recall(query="test query", directory="/home/max/git/yadgar")

    def test_recall_project_without_directory_is_accepted(self):
        """The converse: naming the identity alone is sufficient.

        The dead ValueError's second condition (``and not project``) encoded
        this before C5 and is the reason it can never fire now. Pinning it here
        keeps the two-argument contract explicit rather than implied by absence.
        """
        result = self._call_recall(query="test query", project="owner/repo")
        assert isinstance(result, list)

    def test_recall_valid_directory_does_not_raise(self):
        """recall(directory=..., project=...) must NOT raise."""
        result = self._call_recall(
            query="test query", directory="/home/max/git/yadgar", project="owner/repo"
        )
        assert isinstance(result, list)

    def test_recall_raises_before_storage_access(self):
        """The refusal must fire BEFORE any storage/retriever access.

        Verified by nulling storage AND retriever: a scope-less recall must
        still produce the scope error, never a storage-not-initialised one.
        This property is what makes the guard a boundary check rather than a
        late failure after work has already been done.
        """
        import yadgar._shared.runtime.state as _st
        from yadgar.core.server.tools.recall import recall as recall_fn

        with (
            patch.object(_st, "_storage", None),
            patch.object(_st, "_retriever", None),
            patch.object(_st, "_consolidation", None),
        ):
            with pytest.raises(UnresolvedProjectError, match="no project_id was supplied"):
                recall_fn(query="test", directory=None)


# ---------------------------------------------------------------------------
# Part 1 — wiki_query() must raise ValueError when directory omitted or empty
# ---------------------------------------------------------------------------


class TestWikiQueryDirectoryRequired:
    """wiki_query() must raise ValueError immediately when directory not supplied."""

    def _call_wiki_query(self, **kwargs):
        import yadgar._shared.runtime.state as _st
        from yadgar.core.server.tools.wiki import wiki_query as wq_fn

        mock_wiki = MagicMock()
        mock_wiki.query.return_value = []

        with patch.object(_st, "_wiki", mock_wiki):
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
        """wiki_query with a valid directory AND a project must NOT raise.

        Unlike ``recall``, wiki_query's own ``directory is required`` guard is
        still REACHABLE — it runs ahead of the resolver, which is why the three
        raise-cases above still assert ValueError. The identity is what this
        call additionally needs since C5, so it names one.
        """
        result = self._call_wiki_query(
            query="test", directory="/home/max/git/yadgar", project="owner/repo"
        )
        assert isinstance(result, list)

    def test_wiki_query_raises_before_wiki_access(self):
        """ValueError must fire before any wiki store access."""
        import yadgar._shared.runtime.state as _st
        from yadgar.core.server.tools.wiki import wiki_query as wq_fn

        with patch.object(_st, "_wiki", None):
            with pytest.raises(ValueError, match="directory is required"):
                wq_fn(query="test", directory=None)


# ---------------------------------------------------------------------------
# Part 2 — hook_prompt_recall must filter results by caller directory
# ---------------------------------------------------------------------------


#: The caller's project, and the other one. Car C7 re-keyed
#: ``_filter_prompt_recall_results`` from ``directory_context`` onto
#: ``is_project_eligible(row_project_id, row_tags, caller_project)``, so these —
#: not the ``directory_context`` values below — are what admit or exclude a row.
CALLER_PROJECT_ID = "test-owner/test-repo"
AWS_PROJECT_ID = "other-owner/aws-work"


def _make_yadgar_memory() -> dict:
    return {
        "id": 200,
        "content": "yadgar module design note",
        "heat": 0.8,
        "tags": [],
        "branch": None,
        "_retrieval_score": 0.8,
        "directory_context": "/home/max/git/yadgar",
        "project_id": CALLER_PROJECT_ID,
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
        "project_id": AWS_PROJECT_ID,
    }


def _make_global_memory() -> dict:
    """A cross-project row: admitted by the ``'global'`` REACH TAG, not by value.

    Its ``project_id`` is deliberately the OTHER project. Under Car C7 the row
    is visible from every project because ``GLOBAL_REACH_TAG`` is in ``tags`` —
    the arm that replaced the old ``directory_context='global'`` sentinel VALUE.
    Stamping ``project_id='global'`` instead would test a key ADR-0227 abolished.
    """
    return {
        "id": 300,
        "content": "global yadgar rule",
        "heat": 0.6,
        "tags": ["global"],
        "branch": None,
        "_retrieval_score": 0.6,
        "directory_context": "global",
        "project_id": AWS_PROJECT_ID,
    }


class TestHookPromptRecallProjectFiltering:
    """hook_prompt_recall must apply the PROJECT filter to retriever results.

    Was ``TestHookPromptRecallDirectoryFiltering``, and the rename records a
    changed premise, not a tidy-up. The RED this class was written against
    (v5.65 Fix D) was "hook_prompt_recall forwards retriever results with no
    scoping at all", and it proved the fix by seeding rows that differed only in
    ``directory_context``. Car C7 re-keyed ``_filter_prompt_recall_results``
    onto ``project_id`` + the ``'global'`` reach tag, and ADR-0225 retired
    ``directory`` as a scope key, so that seeding could no longer distinguish
    anything: the rows carried no ``project_id`` at all and the request carried
    no ``?project=``, which makes ``hook_project_id`` raise, which makes the
    handler skip the filter by design — and the aws row came back.

    The leak the class exists to catch is unchanged and so are its assertions.
    Only the axis the corpus varies has moved onto the key the filter reads.
    """

    def _run_hook_prompt_recall(
        self,
        query: str,
        directory: str | None,
        retriever_results: list[dict],
        project: str | None = CALLER_PROJECT_ID,
    ) -> dict:
        """Call hook_prompt_recall with given directory/project + given recall results.

        ``project`` is threaded as the ``?project=`` query param because it is
        the ONLY signal that can scope a hook recall: hooks carry no session
        transport, and C5 deleted every directory-derivation tier, so a request
        with a directory and no project makes ``hook_project_id`` raise and the
        handler degrade to unfiltered rows.

        v5.113.0: prompt-recall now FORWARDS to the backend (via
        _HookRecallForwarder) when a directory is present, so injecting via
        mock_retriever.recall no longer reaches the result set. Patch
        _recall_with_timeout instead — the ONE seam both the forward path and the
        directory=None in-core fallback funnel through. This tests exactly what
        this class asserts: that _filter_prompt_recall_results drops
        project-ineligible rows, regardless of which recall path produced them.

        The seam has a cost, recorded because it decides where a contract can be
        pinned: patching it also bypasses ``_forward_hook_recall``, where the
        project guard lives. Any assertion ABOUT that guard therefore belongs on
        the guard itself — see ``test_no_project_param_raises_before_any_forward``.

        Returns the JSON response body dict.
        """
        import yadgar._shared.runtime.state as _st
        import yadgar.core.server.http as _http  # noqa: F401 — ensure routes registered
        from yadgar.core.server.http import hook_prompt_recall

        # Build fake request
        query_params: dict[str, str] = {"query": query}
        if directory is not None:
            query_params["directory"] = directory
        if project is not None:
            query_params["project"] = project

        class _FakeRequest:
            def __init__(self):
                self.query_params = query_params

        async def _recall_returns_injected(retriever, handler_name, *args, **kwargs):
            # Path-agnostic: whatever recall path the handler chose, return the
            # injected results so the directory post-filter is exercised.
            return list(retriever_results)

        async def _run():
            with (
                patch.object(_st, "_retriever", MagicMock()),
                patch.object(_st, "_last_session_context", {}),
                patch.object(_st, "_last_prompt_recall", {}),
                patch("yadgar.core.server.http._build_dlq_alert_text", return_value=""),
                patch(
                    "yadgar.core.server.http._recall_with_timeout",
                    side_effect=_recall_returns_injected,
                ),
            ):
                resp = await hook_prompt_recall(_FakeRequest())
                return resp.body if hasattr(resp, "body") else {}

        raw = asyncio.run(_run())
        import json

        if isinstance(raw, bytes):
            return json.loads(raw)
        return raw

    def test_other_project_memory_excluded_when_caller_is_yadgar(self):
        """RED: other-project memory must NOT appear in prompt-recall scoped to the caller.

        Pre-fix: retriever.recall returns mixed results; hook writes all of them
        into the response text, including the other project's content.
        Post-fix: the project filter excludes it.
        """
        results_mixed = [_make_aws_memory(), _make_yadgar_memory(), _make_global_memory()]
        body = self._run_hook_prompt_recall(
            query="yadgar scoping test",
            directory="/home/max/git/yadgar",
            retriever_results=results_mixed,
        )
        text = body.get("text", "")
        assert "aws IAM policy config" not in text, (
            f"BUG: {AWS_PROJECT_ID} memory leaked into prompt-recall "
            f"scoped to project={CALLER_PROJECT_ID}.\n"
            f"Response text: {text!r}\n"
            "hook_prompt_recall does not apply the project filter to retriever results."
        )

    def test_no_project_param_raises_before_any_forward(self):
        """A directory with NO ``?project=`` must RAISE, not forward unscoped.

        This is the case Car C7 changed, and it is pinned at the seam that
        actually enforces it rather than through the handler: the guard lives in
        ``_forward_hook_recall``, which evaluates ``hook_project_id(directory,
        project)`` as an ARGUMENT to the backend forward, so the raise happens
        before any request is issued. The handler's own post-filter cannot be
        that guard — reaching it with ``_scoped=None`` makes
        ``_filter_prompt_recall_results`` skip filtering by design (a container
        must not guess a project), which is safe only BECAUSE nothing scoped can
        get that far. Patching ``_recall_with_timeout``, as the sibling tests in
        this class do to inject a corpus, bypasses the guard entirely and would
        pin a state production cannot reach.

        The documented trade: losing an injection beats leaking another
        project's memories into this project's prompt (the v5.65 leak).
        """
        from yadgar.core.server.http import _forward_hook_recall

        with pytest.raises(UnresolvedProjectError):
            _forward_hook_recall(
                "yadgar scoping test",
                max_results=5,
                min_heat=0.0,
                directory="/home/max/git/yadgar",
                project=None,
            )

    def test_forward_failure_degrades_to_empty_injection(self):
        """When the scoped forward raises, the hook injects NOTHING.

        The second half of the contract above: the raise is only an acceptable
        guard because the handler turns it into an empty injection rather than
        into a broken prompt or an unscoped fallback.
        """
        import yadgar._shared.runtime.state as _st
        from yadgar.core.server.http import hook_prompt_recall

        class _FakeRequest:
            query_params = {
                "query": "yadgar scoping test",
                "directory": "/home/max/git/yadgar",
            }

        async def _run():
            with (
                patch.object(_st, "_retriever", MagicMock()),
                patch.object(_st, "_last_session_context", {}),
                patch.object(_st, "_last_prompt_recall", {}),
                patch("yadgar.core.server.http._build_dlq_alert_text", return_value=""),
                patch(
                    "yadgar.core.server.http._recall_with_timeout",
                    side_effect=UnresolvedProjectError("no project"),
                ),
            ):
                resp = await hook_prompt_recall(_FakeRequest())
                return resp.body if hasattr(resp, "body") else {}

        raw = asyncio.run(_run())
        import json

        body = json.loads(raw) if isinstance(raw, bytes) else raw
        assert body.get("text", "") == "", (
            f"An unscoped-forward failure must inject nothing; got {body!r}"
        )

    def test_caller_project_and_reach_tagged_memory_retained(self):
        """Caller-project and reach-tagged memories must appear for the caller's project."""
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
