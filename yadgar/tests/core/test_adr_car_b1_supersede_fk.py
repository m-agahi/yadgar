"""Car B1 — ledger task 190: ``adr_add`` writes the prose number as an FK.

``_link_adr_supersede_targets`` turned ``supersedes="ADR-0016"`` into
``int("0016")`` and forwarded that straight through as ``add_adr_supersedes(
supersedes_id=16)`` — no check that a row 16 exists, and no check that it
belongs to the writing project.

``adr.id`` is ONE GLOBAL ``AUTO_INCREMENT`` shared across projects
(``quinyx/flux`` owns 7–22 and 257–332; ``m-agahi/yadgar`` owns 23–252), so
that number routinely names ANOTHER project's ADR.  The consequence is not
merely a dangling link: the backend ``add_adr_supersedes`` op ALSO flips the
target row's ``status`` to ``'superseded'`` (Car F, D23), so a yadgar ADR
declaring ``supersedes: ADR-0016`` retires a live ``quinyx/flux`` decision.
Ledger task 195 (the ``supersedes`` COLUMN never being stamped) is downstream
of this and is a separate car — the flip demonstrably works, which is exactly
why an unvalidated id is dangerous rather than inert.

Under ADR-0197 the id IS the number, so a correctly-remapped call has
``number == id`` and the defect is masked; it bites the caller who passes a
legacy (pre-re-landing) number, and any caller whose number lands in another
project's slice.

SCOPE, stated honestly: this closes the CROSS-PROJECT FK.  It does NOT recover
"legacy number 16, which now names a different ADR inside the same project" —
the legacy numbering is not recoverable from any stored data, so such a call
resolves to the same-project row that now wears that number.  The seed path's
rule applies here too: a wrong FK is unrepairable, a gap is not.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from unittest.mock import patch

import pytest

_YADGAR = "m-agahi/yadgar"
_FLUX = "quinyx/flux"

#: One id space, two projects.
_ROWS: dict[int, dict[str, Any]] = {
    16: {"id": 16, "project_id": _FLUX, "title": "flux ADR 16", "status": "accepted"},
    23: {"id": 23, "project_id": _YADGAR, "title": "yadgar ADR 23", "status": "accepted"},
    24: {"id": 24, "project_id": _YADGAR, "title": "yadgar ADR 24", "status": "accepted"},
}


def _adr_add_params(project_dir: str, supersedes: str, **overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = dict(
        directory=project_dir,
        project=_YADGAR,
        title="Car B1 ADR",
        status="accepted",
        date="2026-08-19",
        context="Car B1.",
        decision="Validate supersede FKs.",
        rationale="Task 190.",
        alternatives="Keep forwarding the prose number — rejected.",
        consequences="No cross-project FK.",
        revisit_trigger="ADR numbering stops being the row id.",
        supersedes=supersedes,
    )
    params.update(overrides)
    return params


class _Forwarder:
    """Records every admin forward and answers ``get_adr_row`` from ``_ROWS``.

    ``get_adr_row`` honours the ``project_id`` in the payload — its scoping is
    pinned in ``tests/backend/test_adr_car_b1_admin_ops`` and
    ``tests/_shared/test_adr_car_b1_ledger_sql``.  A caller that omits the
    scope here gets the foreign row, exactly as the real op would refuse to.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, op: str, payload: dict, **kwargs: Any) -> dict:
        self.calls.append((op, dict(payload)))
        if op == "create_adr_row":
            return {"row": {"id": 99, **payload}}
        if op == "get_adr_row":
            row = _ROWS.get(int(payload.get("id", 0)))
            scope = payload.get("project_id")
            if row is None or (scope is not None and row["project_id"] != scope):
                return {"row": None}
            return {"row": dict(row)}
        return {"ok": True}

    @property
    def supersede_ids(self) -> list[int]:
        return [int(p["supersedes_id"]) for op, p in self.calls if op == "add_adr_supersedes"]


def _run_adr_add(tmp_path, supersedes: str, name: str) -> _Forwarder:
    from yadgar.core.server.tools.adr import adr_add

    project_dir = str(tmp_path / name)
    os.makedirs(project_dir, exist_ok=True)
    forwarder = _Forwarder()
    with (
        patch(
            "yadgar.core.server.tools.adr._resolve_project_root",
            return_value=project_dir,
        ),
        patch("yadgar.core.server.tools.adr._forward_admin", side_effect=forwarder),
        patch(
            "yadgar.core.server.tools.adr._wiki_write_canonical",
            return_value={"stored": True, "committed": True},
        ),
    ):
        result = adr_add(**_adr_add_params(project_dir, supersedes))
    assert "error" not in result, f"unexpected error: {result.get('error')}"
    return forwarder


class TestSupersedeFkIsProjectScoped:
    def test_cross_project_target_is_not_linked(self, tmp_path) -> None:
        """ADR-0016 belongs to quinyx/flux — a yadgar ADR must not FK to it.

        ASSERTS THE VALUE WRITTEN, not that the call returned ok: the write
        tools in this corpus have a documented history of reporting success for
        writes they dropped.
        """
        forwarder = _run_adr_add(tmp_path, "ADR-0016", "cross")
        assert forwarder.supersede_ids == [], (
            "id 16 belongs to quinyx/flux; linking it also flips that row's "
            f"status to 'superseded'. Wrote FKs: {forwarder.supersede_ids!r}"
        )

    def test_same_project_target_is_still_linked(self, tmp_path) -> None:
        """The working direction must keep working — a one-direction test stays
        green through the whole defect lifetime."""
        forwarder = _run_adr_add(tmp_path, "ADR-0023", "same")
        assert forwarder.supersede_ids == [23]

    def test_nonexistent_target_is_not_linked(self, tmp_path) -> None:
        """A number naming no row at all is a gap, never a guessed FK."""
        forwarder = _run_adr_add(tmp_path, "ADR-0777", "missing")
        assert forwarder.supersede_ids == []

    def test_mixed_targets_link_only_the_resolvable_ones(self, tmp_path) -> None:
        forwarder = _run_adr_add(tmp_path, "ADR-0016, ADR-0023, ADR-0024", "mixed")
        assert forwarder.supersede_ids == [23, 24]

    def test_target_is_resolved_before_the_link_is_written(self, tmp_path) -> None:
        """Resolution must be a real project-scoped lookup, not a parse.

        Pins the mechanism: a ``get_adr_row`` carrying THIS project's id must
        precede the link. Without it, "no FK written" could equally come from a
        blanket refusal to link anything.
        """
        forwarder = _run_adr_add(tmp_path, "ADR-0023", "mechanism")
        ops = [op for op, _ in forwarder.calls]
        assert "get_adr_row" in ops, f"target must be resolved via the ledger; ops={ops!r}"
        lookup = next(p for op, p in forwarder.calls if op == "get_adr_row")
        assert lookup.get("project_id") == _YADGAR, (
            f"the resolution lookup must be project-scoped; got {lookup!r}"
        )
        assert ops.index("get_adr_row") < ops.index("add_adr_supersedes")

    def test_skipped_target_is_logged(self, tmp_path, caplog: pytest.LogCaptureFixture) -> None:
        """A dropped link must be visible — a silent skip is how a supersede
        that never happened looks identical to one that did."""
        with caplog.at_level(logging.WARNING, logger="yadgar.core.server.tools.adr"):
            _run_adr_add(tmp_path, "ADR-0016", "logged")
        assert any("ADR-0016" in r.getMessage() for r in caplog.records), (
            f"expected a warning naming the skipped target; got {[r.getMessage() for r in caplog.records]!r}"
        )

    def test_foreign_row_is_rejected_even_when_the_backend_returns_it(self, tmp_path) -> None:
        """Defence in depth: the row's own ``project_id`` is re-compared here.

        The scoped lookup is one layer (fixed in the same car, ledger task 188);
        this pins the OTHER layer.  Modelled by a forwarder that ignores the
        scope it is handed — a backend that regresses, an older backend, or a
        future caller reaching the row by some other path.
        """
        from yadgar.core.server.tools.adr import adr_add

        project_dir = str(tmp_path / "unscoped-backend")
        os.makedirs(project_dir, exist_ok=True)
        forwarder = _Forwarder()
        unscoped = _Forwarder()

        def _ignore_scope(op: str, payload: dict, **kwargs: Any) -> dict:
            if op == "get_adr_row":
                # The scope is dropped — the corpus-wide row comes back.
                return unscoped("get_adr_row", {"id": payload["id"]})
            return forwarder(op, payload, **kwargs)

        with (
            patch(
                "yadgar.core.server.tools.adr._resolve_project_root",
                return_value=project_dir,
            ),
            patch("yadgar.core.server.tools.adr._forward_admin", side_effect=_ignore_scope),
            patch(
                "yadgar.core.server.tools.adr._wiki_write_canonical",
                return_value={"stored": True, "committed": True},
            ),
        ):
            adr_add(**_adr_add_params(project_dir, "ADR-0016"))

        assert forwarder.supersede_ids == [], (
            "the returned row declares project_id=quinyx/flux; core must reject "
            f"it regardless of what the lookup was asked for. Wrote {forwarder.supersede_ids!r}"
        )

    def test_none_supersedes_does_no_lookups(self, tmp_path) -> None:
        """``supersedes="none"`` must not cost a round-trip."""
        forwarder = _run_adr_add(tmp_path, "none", "nonecase")
        assert [op for op, _ in forwarder.calls if op == "get_adr_row"] == []
