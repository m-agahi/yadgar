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
