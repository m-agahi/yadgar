"""Ledger task 285 — the three wiki-search HTTP handlers must not render failure as success.

All three carried the identical swallow::

    except Exception as exc:
        logger.debug(...)
        return JSONResponse([], status_code=500, ...)

— an HTTP 500 whose body is a well-formed empty result list, with the real cause
logged BELOW production level. That is the ADR-0420 class: failure rendered as
well-formed success.

Two distinct defects live under one shape, and the tests below keep them apart:

  * the LIVE failure — ``api_wiki_search`` and ``api_wiki_query(mode=semantic)``
    call ``WikiStore.query`` without a ``scope``. Car H1 made a falsy
    ``project_id`` RAISE ``UnresolvedProjectError`` unless ``unscoped`` is set,
    so every one of those calls died and the 500 hid it. Measured live on the
    running daemon 2026-08-21: ``/api/wiki/search`` and
    ``/api/wiki_query?mode=semantic`` returned HTTP 500 with body ``[]``;
    ``mode=slug``/``mode=keyword`` and ``/api/wiki/list`` returned 200.
  * the LATENT swallow — ``api_wiki_list`` takes no ``scope`` and was NOT
    failing. It gets the visibility fix only.

Run::

    OTEL_SDK_DISABLED=true uv run pytest \\
        yadgar/tests/core/test_wiki_search_failure_is_visible.py -p no:xdist -q
"""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from yadgar.core import server


def _make_request(params: dict, path: str = "/api/wiki_query"):
    """Build a minimal fake Starlette Request carrying *params* as a query string."""
    from starlette.requests import Request  # noqa: PLC0415

    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": b"&".join(f"{k}={v}".encode() for k, v in params.items()),
            "headers": [],
        }
    )


def _body(resp) -> object:
    return json.loads(resp.body)


class _Boom(RuntimeError):
    """Sentinel exception — its text is what the handler must surface."""


# ---------------------------------------------------------------------------
# 1. The live failure: WikiStore.query is called with an explicit scope
# ---------------------------------------------------------------------------


class TestSemanticSearchPassesAnExplicitScope:
    """Car H1 raises on a falsy project_id unless ``unscoped`` is set.

    These handlers are the whole-corpus Bookmarks UI — they have no project
    identity, so ``unscoped=True`` is the honest declaration. Passing no scope
    at all is what made them raise. The fix must NOT be to relax
    ``WikiStore.query``'s default: that would launder past H1's guard for every
    caller instead of stating this caller's intent.
    """

    def test_api_wiki_search_passes_unscoped_scope(self):
        from yadgar.core.server import http_bookmarks  # noqa: PLC0415

        wiki_mock = MagicMock()
        wiki_mock.query.return_value = []

        with patch.object(server._state_mod, "_wiki", wiki_mock):
            resp = asyncio.run(
                http_bookmarks.api_wiki_search(_make_request({"q": "adr"}, "/api/wiki/search"))
            )

        assert resp.status_code == 200
        scope = wiki_mock.query.call_args.kwargs.get("scope")
        assert scope is not None, "api_wiki_search must pass an explicit RecallScope"
        assert scope.unscoped is True, "the bookmarks UI reads the whole corpus"

    def test_api_wiki_query_semantic_passes_unscoped_scope(self):
        from yadgar.core.server import http_wiki_versioning  # noqa: PLC0415

        wiki_mock = MagicMock()
        wiki_mock.query.return_value = []
        storage_mock = MagicMock()

        with (
            patch.object(server._state_mod, "_wiki", wiki_mock),
            patch.object(server._state_mod, "_storage", storage_mock),
        ):
            resp = asyncio.run(
                http_wiki_versioning.api_wiki_query(_make_request({"q": "adr", "mode": "semantic"}))
            )

        assert resp.status_code == 200
        scope = wiki_mock.query.call_args.kwargs.get("scope")
        assert scope is not None, "api_wiki_query semantic mode must pass an explicit RecallScope"
        assert scope.unscoped is True

    def test_unscoped_scope_builds_a_clause_without_raising(self):
        """The regression itself: an unscoped RecallScope must not raise."""
        from yadgar._shared.storage.directory import RecallScope  # noqa: PLC0415

        sql, params = RecallScope(unscoped=True).with_default_opt_in(None).clause()
        assert isinstance(sql, str)
        assert isinstance(params, dict)

        with pytest.raises(Exception):  # noqa: B017 - the pre-fix behaviour, any raise
            RecallScope().with_default_opt_in(None).clause()


# ---------------------------------------------------------------------------
# 2. The swallow: a failure must be audible and must not read as success
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("module_name", "handler_name", "params", "path", "slot", "attr"),
    [
        ("http_bookmarks", "api_wiki_search", {"q": "x"}, "/api/wiki/search", "_wiki", "query"),
        (
            "http_bookmarks",
            "api_wiki_list",
            {"slug_prefix": "x"},
            "/api/wiki/list",
            "_storage",
            "list_wiki_pages",
        ),
        (
            "http_wiki_versioning",
            "api_wiki_query",
            {"q": "x", "mode": "semantic"},
            "/api/wiki_query",
            "_wiki",
            "query",
        ),
    ],
)
class TestFailureIsAudibleAndNotSuccessShaped:
    @staticmethod
    def _run(module_name, handler_name, params, path, slot, attr):
        import importlib  # noqa: PLC0415

        mod = importlib.import_module(f"yadgar.core.server.{module_name}")
        failing = MagicMock()
        getattr(failing, attr).side_effect = _Boom("underlying cause")

        other = MagicMock()
        other.list_wiki_pages.return_value = []
        other.query.return_value = []

        patches = {"_wiki": other, "_storage": other, slot: failing}
        with (
            patch.object(server._state_mod, "_wiki", patches["_wiki"]),
            patch.object(server._state_mod, "_storage", patches["_storage"]),
        ):
            return asyncio.run(getattr(mod, handler_name)(_make_request(params, path)))

    def test_logs_at_warning_or_above_with_traceback(
        self, module_name, handler_name, params, path, slot, attr, caplog
    ):
        with caplog.at_level(logging.WARNING, logger=f"yadgar.core.server.{module_name}"):
            self._run(module_name, handler_name, params, path, slot, attr)

        records = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert records, f"{handler_name} swallowed the failure below WARNING"
        assert any(r.exc_info for r in records), (
            f"{handler_name} must log the exception (exc_info), not just its str()"
        )

    def test_body_does_not_read_as_an_empty_success(
        self, module_name, handler_name, params, path, slot, attr
    ):
        resp = self._run(module_name, handler_name, params, path, slot, attr)

        assert resp.status_code >= 500
        body = _body(resp)
        assert body != [], (
            f"{handler_name} returned HTTP {resp.status_code} with an empty result list — "
            "a caller cannot tell that apart from 'no matches'"
        )
        assert isinstance(body, dict) and body.get("error"), (
            f"{handler_name} must name the failure in its body"
        )


# ---------------------------------------------------------------------------
# 3. The 503 guards carry the same shape and must be named too
# ---------------------------------------------------------------------------


class TestEngineUnavailableIsNotAnEmptySuccess:
    @pytest.mark.parametrize(
        ("module_name", "handler_name", "params", "path"),
        [
            ("http_bookmarks", "api_wiki_search", {"q": "x"}, "/api/wiki/search"),
            ("http_bookmarks", "api_wiki_list", {"slug_prefix": "x"}, "/api/wiki/list"),
            (
                "http_wiki_versioning",
                "api_wiki_query",
                {"q": "x", "mode": "semantic"},
                "/api/wiki_query",
            ),
        ],
    )
    def test_503_body_names_the_failure(self, module_name, handler_name, params, path):
        import importlib  # noqa: PLC0415

        mod = importlib.import_module(f"yadgar.core.server.{module_name}")
        with (
            patch.object(server._state_mod, "_wiki", None),
            patch.object(server._state_mod, "_storage", None),
        ):
            resp = asyncio.run(getattr(mod, handler_name)(_make_request(params, path)))

        assert resp.status_code == 503
        body = _body(resp)
        assert body != [], f"{handler_name} returned HTTP 503 with an empty result list"
        assert isinstance(body, dict) and body.get("error")
