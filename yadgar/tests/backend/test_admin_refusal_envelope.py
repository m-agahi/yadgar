"""A DELIBERATE refusal must not be reported as a crash (ledger tasks 80 + 294).

Before this car the ``/admin`` route caught ``KeyError`` and nothing else, so
every op that refused BY DESIGN — the restore-verification gate
(``RestoreVerificationError``), Car J's wiki mutability lock (a bare
``PermissionError``) — left FastAPI to render it as a bare HTTP 500 with a
traceback. Downstream, ``_forward_admin`` called ``raise_for_status()`` before
it ever looked at the body, so the caller received an untyped
``httpx.HTTPStatusError`` reading "500 Internal Server Error".

Two consequences, and the second is the defect these tests pin:

* ``quiesce.py``'s ``if verification.get("status") != "ok"`` was DEAD — the
  forward raised before the check could run;
* a correct refusal and a genuine backend fault were byte-identical to every
  automated caller, so a working gate paged as a yadgar bug.

The tests therefore assert on the SHAPE and the REASON, never merely on a
non-2xx status. The crash arm is the control: without it nothing proves the two
outcomes are DISTINGUISHABLE, which is the whole defect.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from yadgar._shared.refusal import REFUSAL_STATUS, AdminRefusal, refusal_envelope


class _StubRefusal(AdminRefusal, RuntimeError):
    """A refusal type that exists only for these tests (opt-in is per-type)."""

    reason = "stub_refused"

    def __init__(self, message: str, report: dict) -> None:
        super().__init__(message)
        self._report = report

    def refusal_report(self) -> dict:
        return self._report


_TRI_STATE = {
    "status": "violation",
    "checks": {"row_identity": {"status": "violation", "reason": "rows_diverged"}},
    "violations": ["restore[row_identity]: 3 rows diverged"],
    "unavailable": [],
}


@pytest.fixture
def _client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A /admin client whose dispatcher raises whatever the test registers.

    ``raise_server_exceptions=False`` so an UNhandled exception renders as a real
    500 response instead of propagating into the test — the crash arm needs to
    observe the status code the way a caller would.
    """
    from yadgar.backend.embed_service.embed_service import app

    return TestClient(app, raise_server_exceptions=False)


def _raise(exc: BaseException):
    async def _op(op: str, payload: dict) -> dict:
        raise exc

    return _op


# ---------------------------------------------------------------------------
# Arm 1 — a refusal is structured, machine-coded, and keeps its tri-state.
# ---------------------------------------------------------------------------
def test_refusal_returns_structured_envelope_not_500(
    _client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "yadgar.backend.admin_exec.run_admin_op_async",
        _raise(_StubRefusal("the gate refused", _TRI_STATE)),
    )

    resp = _client.post("/admin", json={"op": "mariadb_restore_verify", "payload": {}})

    assert resp.status_code == REFUSAL_STATUS, (
        f"a deliberate refusal must not be rendered as a server fault — got {resp.status_code}"
    )
    body = resp.json()["detail"]
    assert body["refused"] is True, "the envelope must carry the machine refusal flag"
    assert body["ok"] is False, "a refusal is not a success; ok must be False"
    assert body["reason"] == "stub_refused", "the envelope must name WHY it refused"
    assert body["op"] == "mariadb_restore_verify"
    assert "the gate refused" in body["error"]
    # The tri-state survives verbatim at top level — same shape the 200 path
    # splices (ADR-0195/0196: ``unavailable`` is a refusal, not a crash).
    for key, expected in _TRI_STATE.items():
        assert body[key] == expected, f"tri-state key {key!r} was lost or rewritten"


def test_unavailable_is_a_refusal_not_a_crash(
    _client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``unavailable`` stays in the PAYLOAD; it must not leak into the status code.

    The restore gate deliberately collapses ``violation`` and ``unavailable``
    into one refusal (restore_sql's "FAIL CLOSED" docstring). Splitting them by
    HTTP status would re-file "the check could not run" as a backend fault.
    """
    report = {"status": "unavailable", "checks": {}, "violations": [], "unavailable": ["x(y)"]}
    monkeypatch.setattr(
        "yadgar.backend.admin_exec.run_admin_op_async",
        _raise(_StubRefusal("could not run", report)),
    )

    resp = _client.post("/admin", json={"op": "mariadb_restore_verify", "payload": {}})

    assert resp.status_code == REFUSAL_STATUS
    assert resp.json()["detail"]["status"] == "unavailable"


# ---------------------------------------------------------------------------
# Arm 2 (the control) — an UNexpected exception is still a 500.
# ---------------------------------------------------------------------------
def test_unexpected_exception_is_still_a_server_fault(
    _client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opt-in is per-type: a non-refusal exception must keep crashing loudly.

    Without this arm the refusal test proves only "returns 4xx", not
    "distinguishable from a crash" — and blanket-classifying every exception as
    a refusal is the same defect pointed the other way.
    """
    monkeypatch.setattr(
        "yadgar.backend.admin_exec.run_admin_op_async",
        _raise(ValueError("something actually broke")),
    )

    resp = _client.post("/admin", json={"op": "bookmark_add", "payload": {}})

    assert resp.status_code == 500, (
        "a genuine fault must remain a 500 — only explicitly opted-in refusal "
        "types are re-filed as structured rejections"
    )


def test_unknown_op_still_400(_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pre-existing KeyError → 400 contract is untouched."""
    monkeypatch.setattr(
        "yadgar.backend.admin_exec.run_admin_op_async",
        _raise(KeyError("no such op: 'nope'")),
    )

    resp = _client.post("/admin", json={"op": "nope", "payload": {}})

    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# The two real refusal families the tasks name — both opted in at the TYPE.
# ---------------------------------------------------------------------------
def test_restore_verification_error_is_a_refusal() -> None:
    """Task 80: the restore gate's refusal carries its tri-state report."""
    from yadgar.backend.admin_exec.restore_sql import RestoreVerificationError

    with pytest.raises(RestoreVerificationError) as excinfo:
        # A path (not a basename) is rejected by the gate before any IO.
        from yadgar.backend.admin_exec.restore_sql import mariadb_restore_verify

        mariadb_restore_verify({"filename": "../escape.sql"})

    exc = excinfo.value
    assert isinstance(exc, AdminRefusal), (
        "RestoreVerificationError must be opted into the refusal base, else the "
        "/admin route cannot tell it from a crash"
    )
    assert isinstance(exc, RuntimeError), (
        "existing `except RuntimeError` catchers must keep working"
    )
    envelope = refusal_envelope(exc, "mariadb_restore_verify")
    assert envelope["reason"] == exc.reason
    assert envelope["status"] in {"violation", "unavailable"}
    # The never-collapsed per-check vocabulary is preserved rather than
    # replaced by a parallel dialect.
    assert any(
        check.get("reason") == "artifact_rejected" for check in envelope["checks"].values()
    ), "the per-check REASON_* vocabulary must survive into the envelope"


def test_wiki_mutability_refusal_is_typed_and_still_a_permissionerror() -> None:
    """Task 294: Car J's lock refuses with a typed, machine-coded rejection."""
    from yadgar._shared.storage.mutability_gate import WikiImmutableError, enforce_mutability

    page = {"id": 42, "slug": "m-agahi_yadgar_adr-0154", "page_type": "adr"}

    with pytest.raises(WikiImmutableError) as excinfo:
        enforce_mutability(page, op="update_wiki_page", sanctioned=False)

    exc = excinfo.value
    assert isinstance(exc, PermissionError), (
        "subclassing PermissionError is load-bearing — existing catchers and the "
        "builtin-vs-typed distinction both depend on it"
    )
    assert isinstance(exc, AdminRefusal)
    assert exc.reason == "wiki_page_locked"
    envelope = refusal_envelope(exc, "wiki_append_section")
    assert envelope["reason"] == "wiki_page_locked"
    assert envelope["mutability"] == "locked"
    assert envelope["slug"] == "m-agahi_yadgar_adr-0154"
    assert envelope["page_id"] == 42


def test_derived_pages_get_their_own_reason_code() -> None:
    """``derived`` is not collapsed into ``locked`` — the fix differs per value."""
    from yadgar._shared.storage.mutability_gate import WikiImmutableError, enforce_mutability

    page = {"id": 7, "slug": "rollup", "page_type": "x", "mutability_override": "derived"}

    with pytest.raises(WikiImmutableError) as excinfo:
        enforce_mutability(page, op="update_wiki_page", sanctioned=False)

    assert excinfo.value.reason == "wiki_page_derived"


def test_sanctioned_writes_are_still_never_gated() -> None:
    """The bypass Car G / Car K depend on is untouched."""
    from yadgar._shared.storage.mutability_gate import enforce_mutability

    enforce_mutability({"id": 1, "page_type": "adr"}, op="update_wiki_page", sanctioned=True)
