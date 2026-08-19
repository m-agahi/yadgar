"""Car B1 — ledger task 188: the ``get_adr_row`` admin op must carry project_id.

``adr.id`` is ONE GLOBAL ``AUTO_INCREMENT`` shared across every project, so an
unscoped by-id lookup returns FOREIGN rows routinely.  Core's ``adr_get``
already puts ``project_id`` on the forwarded payload (Car M); the op body
dropped it on the floor, calling ``storage.get_adr_row(int(payload["id"]))``.
This file pins the PLUMBING half of the fix — that the op reaches the
chokepoint with the scope it was handed, and that it refuses to run unscoped.

The SQL half (does the query actually filter?) is pinned separately in
``yadgar/tests/_shared/test_adr_car_b1_ledger_sql.py``, which executes the real
statement against a two-project fixture.  Neither test alone is sufficient: a
scoped query reached without the scope returns foreign rows, and a scope
forwarded into an unscoped query does too.  The fake storage here honours the
``project_id`` kwarg it is given precisely BECAUSE the SQL it stands in for is
pinned over there — so this file measures forwarding, not filtering.

REQUIRED, not optional-defaulting-to-None: an op that silently degrades to a
corpus-wide lookup when the caller forgets the scope reproduces exactly this
bug for the next caller.  Core's ``adr_get`` always has a project_id
post-ADR-0227 (unresolved → structured raise), and it is the only caller.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from yadgar.backend import admin_exec

_YADGAR = "m-agahi/yadgar"
_FLUX = "quinyx/flux"

#: One id space, two projects — the shape that makes an unscoped lookup wrong.
_ROWS: tuple[dict[str, Any], ...] = (
    {"id": 16, "project_id": _FLUX, "title": "flux ADR 16", "status": "accepted"},
    {"id": 23, "project_id": _YADGAR, "title": "yadgar ADR 23", "status": "accepted"},
)


class _ScopeHonouringStorage:
    """Stands in for ``MariaStorageEngine`` — filters on the scope it RECEIVES.

    If the op forwards no ``project_id`` the lookup is corpus-wide and the
    foreign row comes back, which is the defect verbatim.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def get_adr_row(self, adr_id: int, *, project_id: str | None = None) -> dict | None:
        self.calls.append({"adr_id": adr_id, "project_id": project_id})
        for row in _ROWS:
            if row["id"] != adr_id:
                continue
            if project_id is not None and row["project_id"] != project_id:
                continue
            return dict(row)
        return None


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> _ScopeHonouringStorage:
    fake = _ScopeHonouringStorage()
    holder = MagicMock()
    holder.get_adr_row = fake.get_adr_row
    monkeypatch.setattr(admin_exec.ledger, "_get_sql_storage", lambda: holder)
    return fake


class TestGetAdrRowOpIsProjectScoped:
    async def test_foreign_project_row_is_not_returned(
        self, storage: _ScopeHonouringStorage
    ) -> None:
        result = await admin_exec.run_admin_op_async(
            "get_adr_row", {"id": 16, "project_id": _YADGAR}
        )
        assert result.get("row") is None, (
            "ADR id 16 belongs to quinyx/flux; the op must not hand a "
            f"m-agahi/yadgar caller that row — got {result!r}"
        )

    async def test_own_project_row_is_still_returned(self, storage: _ScopeHonouringStorage) -> None:
        result = await admin_exec.run_admin_op_async(
            "get_adr_row", {"id": 23, "project_id": _YADGAR}
        )
        assert (result.get("row") or {}).get("id") == 23

    async def test_project_id_reaches_the_chokepoint(self, storage: _ScopeHonouringStorage) -> None:
        await admin_exec.run_admin_op_async("get_adr_row", {"id": 23, "project_id": _YADGAR})
        assert storage.calls == [{"adr_id": 23, "project_id": _YADGAR}]

    async def test_missing_project_id_is_refused(self, storage: _ScopeHonouringStorage) -> None:
        """No scope → refuse. Degrading to a corpus-wide lookup IS the bug."""
        result = await admin_exec.run_admin_op_async("get_adr_row", {"id": 16})
        assert result.get("ok") is False, f"unscoped lookup must be refused; got {result!r}"
        assert "project_id" in str(result.get("error", ""))
        assert storage.calls == [], "a refused op must not reach the chokepoint at all"
