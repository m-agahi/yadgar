"""Car 3 — the core-side unscoped corpus reads (cross-project leaks).

Three sinks on the MCP surface read the whole ``memory`` table with no project
predicate at all:

* ``memory://hot``  → ``get_memories_by_heat(HOT_THRESHOLD)`` — and
  ``HOT_THRESHOLD`` defaults to ``0.0``, i.e. every row in the DB.
* ``memory://stale`` → ``get_stale_memories()`` — no predicate whatsoever.
* ``agent_dispatch_prelude(include_context=True)`` → ``_build_context_block``,
  which called ``recall``/``wiki_query`` with a ``directory=`` but no
  ``project=``, and swallowed every failure into ``logger.debug``.

**The resources cannot resolve an identity, and that is the finding, not a gap
in this car.** An MCP resource takes no parameters, and ``session_project`` is
hardcoded ``None`` at every core call site (``_project_param.py``,
``dispatch_helper.py``, ``adr.py``, ``memorize.py``, ``recall.py``,
``http.py``), so ``resolve_effective_project`` raises there by construction
(ADR-0227: never defaulted, never inferred). The only correct behaviour left is
to fail CLOSED and say why — the same posture ``_fetch_hot_memories`` takes:
losing an injection is recoverable, leaking one is not. A parameterised
``memory://hot/{project}`` template would restore the capability; it is new API
surface and is queued, not built here.

The callee signatures (``get_memories_by_heat``, ``get_stale_memories``) are
deliberately NOT changed: consolidation, narrative, prune_passes and
cold_retention are legitimately corpus-wide callers of both.
"""

from __future__ import annotations

import importlib
import json

import pytest


class TestHotResourceFailsClosed:
    """``memory://hot`` must never return the corpus."""

    def test_hot_resource_returns_no_rows_and_names_the_reason(self, monkeypatch):
        import yadgar.core.server.tools.misc as misc

        class _Storage:
            @staticmethod
            def get_memories_by_heat(*_a, **_kw):
                raise AssertionError(
                    "resource_hot reached the corpus-wide heat read — it has no "
                    "identity to scope with and must fail closed instead"
                )

        monkeypatch.setattr(misc, "_get_storage", lambda: _Storage())

        payload = json.loads(misc.resource_hot())

        assert payload["memories"] == []
        assert payload["reason"] == "unresolved_project"


class TestStaleResourceFailsClosed:
    """``memory://stale`` must never return the corpus."""

    def test_stale_resource_returns_no_rows_and_names_the_reason(self, monkeypatch):
        import yadgar.core.server.tools.misc as misc

        class _Storage:
            @staticmethod
            def get_stale_memories(*_a, **_kw):
                raise AssertionError(
                    "resource_stale reached the corpus-wide stale read — it has "
                    "no identity to scope with and must fail closed instead"
                )

        monkeypatch.setattr(misc, "_get_storage", lambda: _Storage())

        payload = json.loads(misc.resource_stale())

        assert payload["memories"] == []
        assert payload["reason"] == "unresolved_project"


class TestContextBlockCarriesTheIdentity:
    """``_build_context_block`` must pass the resolved project to both fan-outs."""

    @staticmethod
    def _capture(monkeypatch):
        """Patch recall + wiki_query at their SOURCE modules; return the kwargs seen."""
        # NOT ``import ... as`` — ``tools/__init__`` re-exports the ``recall``
        # FUNCTION under the module's own name, so attribute-style import
        # resolves to the function and setattr lands on it.
        recall_mod = importlib.import_module("yadgar.core.server.tools.recall")
        wiki_mod = importlib.import_module("yadgar.core.server.tools.wiki")

        seen: dict[str, dict] = {}

        def _recall(**kw):
            seen["recall"] = kw
            return [{"content": "a memory"}]

        def _wiki_query(**kw):
            seen["wiki"] = kw
            return [{"title": "A Page"}]

        monkeypatch.setattr(recall_mod, "recall", _recall)
        monkeypatch.setattr(wiki_mod, "wiki_query", _wiki_query)
        return seen

    def test_recall_and_wiki_query_both_receive_the_project(self, monkeypatch):
        import yadgar.core.server.tools.dispatch_helper as dh

        seen = self._capture(monkeypatch)

        block = dh._build_context_block(
            task_topic="topic",
            directory="/home/max/git/yadgar",
            subagent_type="Explore",
            storage=None,
            project="m-agahi/yadgar",
        )

        assert seen["recall"]["project"] == "m-agahi/yadgar"
        assert seen["wiki"]["project"] == "m-agahi/yadgar"
        assert "a memory" in block

    def test_the_prelude_threads_the_callers_project_through(self, monkeypatch):
        """End-to-end: the tool's ``project=`` must reach the context fan-out.

        The leak was structural, not a typo: ``agent_dispatch_prelude`` DID
        validate ``project`` (via ``accept_project_param``) and then dropped the
        return value on the floor, so the context block was assembled with a
        directory and no identity.
        """
        import yadgar.core.server.tools.dispatch_helper as dh

        seen = self._capture(monkeypatch)
        monkeypatch.setattr(
            dh, "_record_prelude_marker", lambda storage, directory, project=None: None
        )
        monkeypatch.setattr(dh, "_get_contract_text", lambda storage: "## Contract\n\nbody")

        dh.agent_dispatch_prelude(
            pattern="",
            task_topic="topic",
            storage=object(),
            directory="/home/max/git/yadgar",
            include_context=True,
            project="m-agahi/yadgar",
        )

        assert seen["recall"]["project"] == "m-agahi/yadgar"
        assert seen["wiki"]["project"] == "m-agahi/yadgar"


class TestContextBlockFailuresAreVisible:
    """A suppressed fan-out failure must be LOUD, not a ``logger.debug``.

    Both calls sat inside ``except Exception: logger.debug(...)``, so
    ``include_context=True`` silently produced an empty context block — the
    caller could not tell "no results" from "recall raised". The backstop stays
    broad on purpose (prelude assembly must not fail a dispatch, and an
    unresolved project is now an EXPECTED outcome), but it must be observable.
    """

    def test_a_raising_recall_is_logged_at_warning(self, monkeypatch, caplog):
        import logging

        import yadgar.core.server.tools.dispatch_helper as dh

        # NOT ``import ... as`` — ``tools/__init__`` re-exports the ``recall``
        # FUNCTION under the module's own name, so attribute-style import
        # resolves to the function and setattr lands on it.
        recall_mod = importlib.import_module("yadgar.core.server.tools.recall")
        wiki_mod = importlib.import_module("yadgar.core.server.tools.wiki")

        def _boom(**_kw):
            raise RuntimeError("backend unreachable")

        monkeypatch.setattr(recall_mod, "recall", _boom)
        monkeypatch.setattr(wiki_mod, "wiki_query", lambda **_kw: [])

        with caplog.at_level(logging.WARNING, logger=dh.logger.name):
            dh._build_context_block(
                task_topic="topic",
                directory="/home/max/git/yadgar",
                subagent_type="Explore",
                storage=None,
                project="m-agahi/yadgar",
            )

        assert any(
            r.levelno >= logging.WARNING and "recall failed" in r.getMessage()
            for r in caplog.records
        ), "a raising recall was swallowed below WARNING — the failure is invisible"

    def test_a_raising_wiki_query_is_logged_at_warning(self, monkeypatch, caplog):
        import logging

        import yadgar.core.server.tools.dispatch_helper as dh

        # NOT ``import ... as`` — ``tools/__init__`` re-exports the ``recall``
        # FUNCTION under the module's own name, so attribute-style import
        # resolves to the function and setattr lands on it.
        recall_mod = importlib.import_module("yadgar.core.server.tools.recall")
        wiki_mod = importlib.import_module("yadgar.core.server.tools.wiki")

        def _boom(**_kw):
            raise RuntimeError("backend unreachable")

        monkeypatch.setattr(recall_mod, "recall", lambda **_kw: [])
        monkeypatch.setattr(wiki_mod, "wiki_query", _boom)

        with caplog.at_level(logging.WARNING, logger=dh.logger.name):
            dh._build_context_block(
                task_topic="topic",
                directory="/home/max/git/yadgar",
                subagent_type="Explore",
                storage=None,
                project="m-agahi/yadgar",
            )

        assert any(
            r.levelno >= logging.WARNING and "wiki_query failed" in r.getMessage()
            for r in caplog.records
        ), "a raising wiki_query was swallowed below WARNING — the failure is invisible"

    def test_an_unresolved_project_does_not_break_prelude_assembly(self, monkeypatch):
        """The broad backstop is kept for exactly this case.

        Narrowing the excepts to specific storage errors would let
        ``UnresolvedProjectError`` — an EXPECTED outcome once no identity is
        named — escape and fail every ``include_context=True`` dispatch.
        """
        import yadgar.core.server.tools.dispatch_helper as dh
        from yadgar._shared.errors import UnresolvedProjectError

        # NOT ``import ... as`` — ``tools/__init__`` re-exports the ``recall``
        # FUNCTION under the module's own name, so attribute-style import
        # resolves to the function and setattr lands on it.
        recall_mod = importlib.import_module("yadgar.core.server.tools.recall")
        wiki_mod = importlib.import_module("yadgar.core.server.tools.wiki")

        def _unresolved(**_kw):
            raise UnresolvedProjectError("recall")

        monkeypatch.setattr(recall_mod, "recall", _unresolved)
        monkeypatch.setattr(wiki_mod, "wiki_query", _unresolved)

        assert (
            dh._build_context_block(
                task_topic="topic",
                directory="/home/max/git/yadgar",
                subagent_type="Explore",
                storage=None,
                project=None,
            )
            == ""
        )


@pytest.mark.parametrize("dead", ["_block_dir_clause"])
def test_dead_block_directory_clause_is_gone(dead):
    """``_block_dir_clause`` had zero production callers.

    Superseded by ``_block_project_clause``; its only remaining mention was an
    allowlist entry in the directory-residue lint, which hard-fails on a stale
    subject — so the method and the entry come out together.
    """
    from yadgar._shared.storage.blocks import _BlocksMixin

    assert not hasattr(_BlocksMixin, dead)
