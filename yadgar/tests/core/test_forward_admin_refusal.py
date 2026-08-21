"""``_forward_admin`` must hand a refusal BACK, and still raise on a crash.

The core half of ledger tasks 80 + 294. ``_forward_admin`` called
``resp.raise_for_status()`` before it ever read the body, so a structured
refusal from the backend was flattened into an untyped ``httpx.HTTPStatusError``
— and ``quiesce.py``'s ``if verification.get("status") != "ok"`` never ran.
Returning the envelope (rather than raising a second typed exception) is what
makes that pre-existing check live, and it is why the two call sites need no
edit.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from yadgar._shared.refusal import REFUSAL_STATUS

_ENV = {"YADGAR_EMBED_URL": "http://backend:8001", "YADGAR_MCP_AUTH_TOKEN": "tok"}


def _response(status: int, body: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body
    if status >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"{status}", request=MagicMock(), response=resp
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


def _forward_with(resp: MagicMock, op: str = "mariadb_restore_verify") -> dict:
    from yadgar.core.forward import _forward_admin

    with (
        patch("httpx.post", lambda *a, **k: resp),
        patch.dict("os.environ", _ENV),
    ):
        return _forward_admin(op, {})


def test_refusal_envelope_is_returned_not_raised() -> None:
    envelope = {
        "ok": False,
        "refused": True,
        "op": "mariadb_restore_verify",
        "reason": "restore_not_verified",
        "error": "restore verification did NOT pass",
        "status": "violation",
        "checks": {},
        "violations": ["restore[row_identity]: diverged"],
        "unavailable": [],
    }

    result = _forward_with(_response(REFUSAL_STATUS, {"detail": envelope}))

    assert result == envelope, (
        "the caller must receive the structured refusal — quiesce.py's "
        "status check depends on getting the report back, not an exception"
    )
    # The property the whole car exists for, stated as the caller would test it.
    assert result.get("status") != "ok"
    assert result["reason"] == "restore_not_verified"


def test_server_fault_still_raises() -> None:
    """A 500 is still a 500 — refusal handling must not swallow real faults."""
    with pytest.raises(httpx.HTTPStatusError):
        _forward_with(_response(500, {"detail": "Internal Server Error"}))


def test_non_refusal_4xx_still_raises() -> None:
    """A 4xx that is NOT our envelope (e.g. the 400 unknown-op) keeps raising."""
    with pytest.raises(httpx.HTTPStatusError):
        _forward_with(_response(400, {"detail": "unknown op"}))


def test_refusal_status_without_the_envelope_still_raises() -> None:
    """Detection keys on the ``refused`` marker, not on the status code alone."""
    with pytest.raises(httpx.HTTPStatusError):
        _forward_with(_response(REFUSAL_STATUS, {"detail": {"error": "some other conflict"}}))


def test_success_path_unchanged() -> None:
    result = _forward_with(_response(200, {"result": {"added": True}}), op="bookmark_add")
    assert result == {"added": True}


def test_bare_mock_response_behaves_exactly_as_before() -> None:
    """A loosely-mocked response must walk the byte-identical pre-car path.

    Dozens of existing tests hand ``_forward_admin`` a bare ``MagicMock`` whose
    ``.json()`` is itself a MagicMock, not a dict. Reading the body
    unconditionally and then guarding the unwrap with ``isinstance(body, dict)``
    turned those into ``{}`` — a silent SUCCESS-path change to serve a refusal
    path they never touch. Gating the peek on the status code is what keeps them
    honest, and this pins it.
    """
    from yadgar.core.forward import _forward_admin

    bare = MagicMock()  # status_code is a Mock; .json() returns a Mock
    with (
        patch("httpx.post", lambda *a, **k: bare),
        patch.dict("os.environ", _ENV),
    ):
        result = _forward_admin("bookmark_add", {})

    assert result is bare.json.return_value.get.return_value, (
        "the unwrap must still be `resp.json().get('result', {})` verbatim"
    )


# ---------------------------------------------------------------------------
# The one tool that POST-PROCESSES the forward's result rather than returning
# it verbatim. Every other wiki-edit tool is `return _forward_admin(...)`, so
# the envelope reaches the caller untouched; wiki_delete branches on a
# ``deleted`` key the envelope does not carry, and its else-branch says "not
# found". Left alone, this car would have swapped a bare 500 for a WRONG
# ANSWER — the same class of defect, and strictly harder to notice.
# ---------------------------------------------------------------------------
_LOCK_REFUSAL = {
    "ok": False,
    "refused": True,
    "op": "wiki_delete",
    "reason": "wiki_page_locked",
    "error": "wiki page mutability='locked' forbids delete_wiki_page (...)",
    "mutability": "locked",
    "slug": "m-agahi_yadgar_adr-0154",
}


def test_wiki_delete_reports_a_refusal_as_a_refusal() -> None:
    from unittest.mock import patch as _patch

    import yadgar.core.server.tools.wiki as _w

    with _patch.object(_w, "_forward_admin", lambda op, payload: _LOCK_REFUSAL):
        result = _w.wiki_delete("m-agahi_yadgar_adr-0154")

    assert result["refused"] is True
    assert result["reason"] == "wiki_page_locked"
    assert "not found" not in result.get("error", ""), (
        "a locked page EXISTS — reporting the refusal as a missing page is the "
        "lie this train removes"
    )


def test_wiki_delete_not_found_is_unchanged() -> None:
    """The genuine miss keeps its own message; only the refusal branch is new."""
    from unittest.mock import patch as _patch

    import yadgar.core.server.tools.wiki as _w

    with _patch.object(_w, "_forward_admin", lambda op, payload: {"deleted": False}):
        result = _w.wiki_delete("nope")

    assert result == {"deleted": False, "error": "Wiki page 'nope' not found"}
