"""Task #346 — three admin_exec ops silently swallow registry refusals.

The buggy sites on master before this car:

  ``yadgar/backend/admin_exec/ledger.py``
    :307  ``create_task_row``      — wraps ``UnknownProjectError`` in
          ``except Exception`` and returns ``{"ok": False, "error": ...}``
    :678  ``create_adr_row``       — same swallow
    :799  ``create_project_row``   — wraps ``DuplicateProjectError`` (the
          docstring at :783-787 EXPLICITLY admits the swallow)

Both errors subclass ``RuntimeError`` — NOT ``AdminRefusal``. A bare
``except Exception`` swallow therefore produces a 500-ish "ok: False" with
a stringified traceback instead of a structured 409 with the typed
``reason``. The fix is BOTH halves:

  (1) the errors must subclass ``AdminRefusal`` so the ``/admin`` route's
      ``except AdminRefusal`` arm in embed_service_routes.py catches them;
  (2) the wrappers must NOT swallow — they must let ``AdminRefusal``
      propagate to the route.

This file pins BOTH invariants for all three sites. The pattern follows
``TaskEdgePartialStateError`` / ``WikiSizeCollapseError`` /
``RestoreVerificationError`` — the typed-error base whose reason rides
through ``refusal_envelope``.

We use a fake storage layer that raises the registry error; we assert the
exception propagates (not swallowed to ``{"ok": False, "error": ...}``)
and that it carries the ``AdminRefusal`` marker.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from yadgar._shared.storage.sql.errors import (
    AdminRefusal,
    DuplicateProjectError,
    UnknownProjectError,
)
from yadgar.backend import admin_exec


def _fake_sql_storage(*, create_raises: BaseException | None = None) -> MagicMock:
    """Storage whose create_* methods raise *create_raises* (or return a row)."""
    storage = MagicMock()
    storage.create_task_row = AsyncMock(
        side_effect=create_raises if create_raises else {"id": 1, "title": "x"}
    )
    storage.create_adr_row = AsyncMock(
        side_effect=create_raises if create_raises else {"id": 1, "title": "x"}
    )
    storage.create_project_row = AsyncMock(
        side_effect=create_raises if create_raises else {"key": "k", "kind": "git"}
    )
    return storage


@pytest.fixture
def _patched_storage(monkeypatch: pytest.MonkeyPatch):
    """Return a helper that monkeypatches ``_get_sql_storage`` to a chosen fake."""

    def _patch(storage: MagicMock) -> None:
        monkeypatch.setattr(
            "yadgar.backend.admin_exec.ledger._get_sql_storage",
            lambda: storage,
        )

    return _patch


# ---------------------------------------------------------------------------
# (1) The errors themselves are typed as refusals, not bare RuntimeErrors.
# ---------------------------------------------------------------------------


def test_unknown_project_error_is_an_admin_refusal() -> None:
    """UnknownProjectError must be opted into the refusal base so the /admin
    route can render it as a structured 409, not a generic 500."""
    err = UnknownProjectError("m-agahi/ghost")
    assert isinstance(err, AdminRefusal), (
        "UnknownProjectError no longer inherits AdminRefusal — the /admin "
        "route cannot distinguish a refused write from a server fault"
    )
    assert err.project_id == "m-agahi/ghost", (
        "project_id must be carried so the structured envelope can name "
        "which key the registry rejected"
    )


def test_duplicate_project_error_is_an_admin_refusal() -> None:
    """DuplicateProjectError must also be an AdminRefusal — same reason."""
    err = DuplicateProjectError("m-agahi/yadgar")
    assert isinstance(err, AdminRefusal)
    assert err.project_id == "m-agahi/yadgar"


def test_registry_unavailable_stays_a_server_fault() -> None:
    """ProjectRegistryUnavailableError must NOT be an AdminRefusal.

    The refusal module's docstring argues the two are deliberately
    non-collapsible: "cannot check" and "checked and rejected" are
    different operator responses. Conflating them would let an offline
    deployment read as a deliberate refusal.
    """
    from yadgar._shared.storage.sql.errors import ProjectRegistryUnavailableError

    err = ProjectRegistryUnavailableError("m-agahi/yadgar")
    assert not isinstance(err, AdminRefusal), (
        "ProjectRegistryUnavailableError was re-typed as a refusal — "
        "this collapses 'cannot check' into 'checked and rejected', the "
        "exact defect the module docstring warns against"
    )


# ---------------------------------------------------------------------------
# (2) The three op wrappers propagate AdminRefusal instead of swallowing.
# ---------------------------------------------------------------------------


class TestCreateTaskRowPropagatesRefusal:
    async def test_unknown_project_propagates_as_admin_refusal(self, _patched_storage) -> None:
        err = UnknownProjectError("m-agahi/ghost")
        _patched_storage(_fake_sql_storage(create_raises=err))

        # Bind the concrete subclass — the base ``AdminRefusal`` does not
        # carry ``project_id`` (it's the registry-error half that does).
        # The test asserts BOTH halves of the propagation:
        #   1. the exception escapes the wrapper (not swallowed to
        #      ``{"ok": False, "error": ...}``), AND
        #   2. it carries the offending ``project_id`` so the structured
        #      envelope can name it.
        with pytest.raises(UnknownProjectError) as excinfo:
            await admin_exec.run_admin_op_async(
                "create_task_row",
                {"project_id": "m-agahi/ghost", "title": "x"},
            )
        assert isinstance(excinfo.value, AdminRefusal), (
            "UnknownProjectError must remain an AdminRefusal after the wrapper "
            "lets it propagate — otherwise the /admin route would 500 it"
        )
        assert excinfo.value.project_id == "m-agahi/ghost"

    async def test_swallowed_error_does_not_return_ok_false_envelope(
        self, _patched_storage
    ) -> None:
        """The pre-car return shape was ``{"ok": False, "error": str(exc)}``.

        That shape is the silent-swallow signal — an ``AdminRefusal`` op
        must reach the route's refusal arm and become a 409, never a
        bare ``{"ok": False, ...}`` JSON body.
        """
        err = UnknownProjectError("m-agahi/ghost")
        _patched_storage(_fake_sql_storage(create_raises=err))

        with pytest.raises(AdminRefusal):
            await admin_exec.run_admin_op_async(
                "create_task_row",
                {"project_id": "m-agahi/ghost", "title": "x"},
            )
        # If the wrapper swallowed, this line would be reachable with
        # ``{"ok": False, "error": "unknown project_id: 'm-agahi/ghost'"}``.
        # The test framework has already proven the exception escaped.


class TestCreateAdrRowPropagatesRefusal:
    async def test_unknown_project_propagates_as_admin_refusal(self, _patched_storage) -> None:
        err = UnknownProjectError("m-agahi/ghost")
        _patched_storage(_fake_sql_storage(create_raises=err))

        with pytest.raises(UnknownProjectError) as excinfo:
            await admin_exec.run_admin_op_async(
                "create_adr_row",
                {"project_id": "m-agahi/ghost", "title": "x"},
            )
        assert isinstance(excinfo.value, AdminRefusal)
        assert excinfo.value.project_id == "m-agahi/ghost"


class TestCreateProjectRowPropagatesRefusal:
    async def test_duplicate_key_propagates_as_admin_refusal(self, _patched_storage) -> None:
        """The duplicate case is the one whose pre-car docstring explicitly
        ADMITTED the swallow — the fix must remove the swallow AND retype."""
        err = DuplicateProjectError("m-agahi/yadgar")
        _patched_storage(_fake_sql_storage(create_raises=err))

        with pytest.raises(DuplicateProjectError) as excinfo:
            await admin_exec.run_admin_op_async(
                "create_project_row",
                {"key": "m-agahi/yadgar", "kind": "git"},
            )
        assert isinstance(excinfo.value, AdminRefusal)
        assert excinfo.value.project_id == "m-agahi/yadgar"


# ---------------------------------------------------------------------------
# (3) The /admin route renders the propagated refusal as a structured 409.
# ---------------------------------------------------------------------------


def test_admin_route_renders_unknown_project_as_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a refusal raised by ``create_task_row`` reaches the
    route's ``except AdminRefusal`` arm and returns REFUSAL_STATUS, not
    a 500 and not a swallowed ``{"ok": False, ...}`` envelope.

    Without (1) — the AdminRefusal base on UnknownProjectError — this test
    would observe a 500. Without (2) — the wrapper not swallowing — this
    test would observe a 200 with ``{"ok": False, ...}``. Both halves of
    the fix are load-bearing.
    """
    from fastapi.testclient import TestClient

    from yadgar._shared.refusal import REFUSAL_STATUS
    from yadgar.backend.embed_service.embed_service import app

    err = UnknownProjectError("m-agahi/ghost")
    storage = _fake_sql_storage(create_raises=err)
    monkeypatch.setattr(
        "yadgar.backend.admin_exec.ledger._get_sql_storage",
        lambda: storage,
    )

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/admin",
        json={
            "op": "create_task_row",
            "payload": {"project_id": "m-agahi/ghost", "title": "x"},
        },
    )

    assert resp.status_code == REFUSAL_STATUS, (
        f"a refused write must render as {REFUSAL_STATUS} (Conflict), not "
        f"a server fault — got {resp.status_code}. Either the registry "
        f"error was re-typed away from AdminRefusal or the wrapper still "
        f"swallows it."
    )
    detail = resp.json().get("detail", {})
    assert detail.get("refused") is True
    assert detail.get("ok") is False


# ---------------------------------------------------------------------------
# (4) Car Q — the guard is UNIFORM, not three hand-picked ops.
#
# Car C-source added ``except AdminRefusal: raise`` to exactly the three ops
# that could raise a refusal on the day it was written. Car B-d20's NEW op
# (``update_adr_tier_subsystem``) shipped without it, because "which ops can
# refuse today" is a fact that changes every time someone adds a registry
# check to an engine method. These two tests replace that per-op judgement
# with a structural invariant over the whole module.
# ---------------------------------------------------------------------------


# Handlers deliberately left WITHOUT the re-raise, each with the reason it is
# not an oversight. Anything else added to this set needs the same standard:
# the swallow must not be building an operator-facing error envelope.
_REFUSAL_SWEEP_EXEMPT: dict[str, str] = {
    "_attach_supersedes": (
        "best-effort enrichment — its swallow degrades ADR rows to empty edge "
        "lists and lets the CALLER's op succeed; it builds no error envelope, "
        "so re-raising would hand list_adr_rows the failure the degrade exists "
        "to prevent"
    ),
}


def _ledger_handlers_missing_refusal_guard() -> list[tuple[str, int]]:
    """``(function_name, lineno)`` for each unguarded ``except Exception``.

    Structural, not behavioural: a behavioural test can only cover the ops
    someone remembered to write a case for, which is the exact failure mode
    this is here to catch.
    """
    import ast
    from pathlib import Path

    import yadgar.backend.admin_exec.ledger as ledger_mod

    tree = ast.parse(Path(ledger_mod.__file__).read_text(encoding="utf-8"))
    missing: list[tuple[str, int]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if fn.name in _REFUSAL_SWEEP_EXEMPT:
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Try):
                continue
            names = [h.type.id if isinstance(h.type, ast.Name) else None for h in node.handlers]
            if "Exception" not in names:
                continue
            broad_at = names.index("Exception")
            if "AdminRefusal" not in names[:broad_at]:
                missing.append((fn.name, node.handlers[broad_at].lineno))
    return missing


def test_every_ledger_handler_reraises_admin_refusal() -> None:
    """No ``except Exception`` in ledger.py may sit above a missing refusal arm.

    A refusal swallowed into ``{"ok": False}`` at HTTP 200 is
    indistinguishable from a transport fault at the caller — that is the whole
    point of ADR-0423's 409 seam. ``update_adr_tier_subsystem`` is the op that
    proved a per-op audit does not hold: it was added AFTER the three ops were
    fixed and inherited none of the fix.
    """
    missing = _ledger_handlers_missing_refusal_guard()
    assert missing == [], (
        "these ledger.py handlers swallow AdminRefusal into an {'ok': False} "
        f"envelope instead of letting the /admin route render a 409: {missing}. "
        "Add `except AdminRefusal: raise` above the broad handler, or — if the "
        "swallow genuinely builds no error envelope — name it in "
        "_REFUSAL_SWEEP_EXEMPT with the reason."
    )


def test_refusal_sweep_exemptions_still_name_real_functions() -> None:
    """An exemption for a function that no longer exists is a silent hole."""
    import yadgar.backend.admin_exec.ledger as ledger_mod

    for name in _REFUSAL_SWEEP_EXEMPT:
        assert hasattr(ledger_mod, name), (
            f"_REFUSAL_SWEEP_EXEMPT names {name!r}, which ledger.py no longer "
            "defines — drop the stale entry rather than leaving it to exempt "
            "a future function that happens to reuse the name"
        )


class TestUpdateAdrTierSubsystemPropagatesRefusal:
    """The specific op car B-d20 shipped without the guard (its train siblings
    ``create_task_row`` / ``create_adr_row`` / ``create_project_row`` got it)."""

    async def test_refusal_is_not_swallowed_into_ok_false(self, _patched_storage) -> None:
        from yadgar.backend.admin_exec import ledger

        class _RefusingStorage:
            async def update_adr_tier_subsystem(self, *_a, **_kw):
                raise UnknownProjectError("m-agahi/ghost")

        _patched_storage(_RefusingStorage())

        with pytest.raises(UnknownProjectError) as excinfo:
            await ledger.update_adr_tier_subsystem(
                {"id": 1, "tier": "binding", "subsystem": "storage"}
            )
        assert isinstance(excinfo.value, AdminRefusal)

    async def test_ordinary_fault_still_returns_ok_false(self, _patched_storage) -> None:
        """The guard must narrow the swallow, not remove it — a genuine
        backend fault still becomes an ``{"ok": False}`` envelope."""
        from yadgar.backend.admin_exec import ledger

        class _BrokenStorage:
            async def update_adr_tier_subsystem(self, *_a, **_kw):
                raise RuntimeError("connection reset")

        _patched_storage(_BrokenStorage())

        result = await ledger.update_adr_tier_subsystem(
            {"id": 1, "tier": "binding", "subsystem": "storage"}
        )
        assert result["ok"] is False
        assert "connection reset" in result["error"]
