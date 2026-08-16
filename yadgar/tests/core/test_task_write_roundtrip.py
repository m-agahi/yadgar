"""``task_write`` UPDATE, verified by RE-READING the row — not by trusting ``ok``.

Car C part 2. The defect this module pins returned ``{"ok": True}`` while
writing nothing: **``title`` was discarded on UPDATE** (ledger task 111).
``title`` is a REQUIRED argument of ``task_write`` and is run through
``_validate_write_inputs`` — and ``_build_update_payload`` then never put it
in the forwarded payload. Storage accepted it the whole time
(``update_task_row``'s allowlist has ``"title"``). Found by a user who noticed
two titles had not changed after the tool said they had.

WHY A ROUND-TRIP AND NOT A PAYLOAD ASSERTION
--------------------------------------------
A payload assertion is what the old guard did
(``test_update_partial_update_omits_title_when_not_given``): it inspected the
dict the tool built and passed for the bug's entire life because the bug WAS
the dict. These tests drive the real core tool through the real backend admin
op and read the row back through the real ``get_task_row`` op. Only the
storage engine is a double, and it is deliberately dumb — it applies
``**fields`` onto a dict and enforces the same column allowlist the SQL
engine does, nothing more. A double authored to model the outcome under test
would be the same vacuous pass in a new costume.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

# The real storage allowlist (``MariaStorageEngine.update_task_row``). Kept
# here verbatim rather than imported: ``sqlalchemy`` is gated behind the
# ``sql`` extra and this module must run in the yadgar-ci image.
_ALLOWED_COLUMNS = {
    "project_id",
    "title",
    "status",
    "state",
    "active_form",
    "plan_path",
    "body_slug",
    "completed_at",
}


class _DictStorage:
    """Dumb dict-backed stand-in for ``MariaStorageEngine``'s task methods."""

    def __init__(self) -> None:
        self.rows: dict[int, dict] = {}
        self._next_id = 1

    async def create_task_row(self, **fields: Any) -> dict:
        row_id = self._next_id
        self._next_id += 1
        row = {"id": row_id, **fields}
        self.rows[row_id] = row
        return dict(row)

    async def get_task_row(self, task_id: int) -> dict | None:
        row = self.rows.get(int(task_id))
        return None if row is None else dict(row)

    async def update_task_row(self, task_id: int, **fields: Any) -> None:
        if not fields:
            return
        unknown = set(fields) - _ALLOWED_COLUMNS
        if unknown:
            raise ValueError(f"unknown task columns: {sorted(unknown)}")
        self.rows[int(task_id)].update(fields)


@pytest.fixture
def ledger_roundtrip():
    """Wire the core task tool onto the real backend ops over a dict storage.

    Yields the ``_DictStorage`` so a test can inspect the stored row directly
    when it needs the raw value rather than the read-back envelope.
    """
    from yadgar.backend.admin_exec import ledger as ledger_mod
    from yadgar.core.server.tools import task as task_mod

    storage = _DictStorage()

    def fake_forward(op: str, payload: dict, timeout_s: float = 30.0) -> dict:  # noqa: ARG001
        op_fn = getattr(ledger_mod, op)
        return asyncio.run(op_fn(payload))

    with (
        patch.object(ledger_mod, "_get_sql_storage", return_value=storage),
        patch.object(task_mod, "_forward_admin", side_effect=fake_forward),
    ):
        yield storage


def _create(project_id: str = "m-agahi/yadgar", title: str = "original title", **kw) -> int:
    from yadgar.core.server.tools.task import task_write

    result = task_write(project_id=project_id, title=title, **kw)
    assert result.get("ok") is True, result
    return int(result["id"])


def _read(task_id: int) -> dict:
    from yadgar.core.server.tools.task import task_get

    row = task_get(project_id="m-agahi/yadgar", id=task_id)
    assert row is not None
    return row


class TestUpdateStoresTitle:
    """Ledger task 111 — the update path must actually carry ``title``."""

    def test_updated_title_is_readable_back(self, ledger_roundtrip) -> None:
        from yadgar.core.server.tools.task import task_write

        task_id = _create(title="original title")

        result = task_write(
            project_id="m-agahi/yadgar",
            title="the title the caller meant",
            id=task_id,
            status="in_progress",
        )

        assert result.get("ok") is True
        assert _read(task_id)["title"] == "the title the caller meant"

    def test_title_update_alone_is_enough(self, ledger_roundtrip) -> None:
        """No status/state change — a title-only edit must still land."""
        from yadgar.core.server.tools.task import task_write

        task_id = _create(title="41: stale wording")
        task_write(project_id="m-agahi/yadgar", title="41: current wording", id=task_id)

        assert _read(task_id)["title"] == "41: current wording"

    def test_update_does_not_touch_unmentioned_columns(self, ledger_roundtrip) -> None:
        """Partial update stays partial — forwarding title must not widen it."""
        from yadgar.core.server.tools.task import task_write

        task_id = _create(title="t", active_form="doing t", plan_path="docs/plans/t.md")
        task_write(project_id="m-agahi/yadgar", title="t2", id=task_id)

        row = _read(task_id)
        assert row["title"] == "t2"
        assert row["active_form"] == "doing t"
        assert row["plan_path"] == "docs/plans/t.md"

    def test_over_long_title_is_rejected_on_update(self, ledger_roundtrip) -> None:
        """D12 applies to the update path too — the row keeps its old title."""
        from yadgar.core.server.tools.task import task_write

        task_id = _create(title="short")
        result = task_write(project_id="m-agahi/yadgar", title="a" * 201, id=task_id)

        assert result.get("ok") is False
        assert _read(task_id)["title"] == "short"
