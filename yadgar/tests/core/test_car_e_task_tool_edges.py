"""Car E — the core-side half of the edge wiring.

Four seams, each of which dropped edges silently before this car:

* ``task_list(with_edges=...)`` forwards the flag, ALWAYS and both ways round.
  The decision to pay for the join belongs to the tool, not to a backend
  default — same doctrine as ``summary`` (Car A).
* ``task_write(..., blocked_by=[...])`` on a CREATE puts the key on the wire.
  It did not: ``_build_create_payload`` never carried edge keys, so the
  backend's create-side reconciler was unreachable and every dependency stated
  at create time was lost under an ``ok: true``.
* ``_task_list_payload`` (the SessionStart seeder's input) carries the edges
  through instead of projecting them away.
* ``_task_list_restore_nudge`` asks for them — it is the one list read that
  needs the join.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def _forward_capture():
    """Patch ``_forward_admin`` so the forwarded payload is inspectable."""
    from yadgar.core.server.tools import task as task_mod

    captured: dict = {}

    def fake_forward(op: str, payload: dict, timeout_s: float = 30.0) -> dict:  # noqa: ARG001
        captured["op"] = op
        captured["payload"] = payload
        if op == "create_task_row":
            return {"id": 231, **payload}
        if op == "update_task_row":
            return {"id": payload.get("id", 231), **payload}
        if op == "list_task_rows":
            return {"rows": [{"id": 1, "title": "a", "status": "pending"}]}
        return {"row": None}

    with patch.object(task_mod, "_forward_admin", side_effect=fake_forward):
        yield captured


# ── task_list(with_edges=...) ───────────────────────────────────────────────


class TestTaskListForwardsWithEdges:
    def test_default_is_false_on_the_wire(self, _forward_capture: dict) -> None:
        """Not merely absent — explicitly False, so no backend default decides it."""
        from yadgar.core.server.tools.task import task_list

        task_list(project_id="m-agahi/yadgar")

        assert _forward_capture["payload"]["with_edges"] is False

    def test_opt_in_reaches_the_backend(self, _forward_capture: dict) -> None:
        from yadgar.core.server.tools.task import task_list

        task_list(project_id="m-agahi/yadgar", with_edges=True)

        assert _forward_capture["payload"]["with_edges"] is True

    def test_it_is_independent_of_verbose(self, _forward_capture: dict) -> None:
        """Width and edges are separate decisions — the lean shape can carry edges."""
        from yadgar.core.server.tools.task import task_list

        task_list(project_id="m-agahi/yadgar", with_edges=True)

        payload = _forward_capture["payload"]
        assert payload["summary"] is True
        assert payload["with_edges"] is True


# ── task_write CREATE carries the edge keys ─────────────────────────────────


class TestCreateCarriesEdgeKeys:
    def test_blocked_by_reaches_the_create_payload(self, _forward_capture: dict) -> None:
        """The defect: accepted by the signature, absent from the payload."""
        from yadgar.core.server.tools.task import task_write

        task_write(project_id="m-agahi/yadgar", title="t", blocked_by=[3, 4])

        assert _forward_capture["op"] == "create_task_row"
        assert _forward_capture["payload"]["blocked_by"] == [3, 4]

    def test_blocks_reaches_the_create_payload(self, _forward_capture: dict) -> None:
        from yadgar.core.server.tools.task import task_write

        task_write(project_id="m-agahi/yadgar", title="t", blocks=[9])

        assert _forward_capture["payload"]["blocks"] == [9]

    def test_absent_edges_put_no_key_on_the_wire(self, _forward_capture: dict) -> None:
        """Absent must stay absent — the backend reads it as "not mentioned"."""
        from yadgar.core.server.tools.task import task_write

        task_write(project_id="m-agahi/yadgar", title="t")

        payload = _forward_capture["payload"]
        assert "blocked_by" not in payload
        assert "blocks" not in payload

    def test_an_empty_list_is_still_stated(self, _forward_capture: dict) -> None:
        from yadgar.core.server.tools.task import task_write

        task_write(project_id="m-agahi/yadgar", title="t", blocked_by=[])

        assert _forward_capture["payload"]["blocked_by"] == []

    def test_update_still_carries_them(self, _forward_capture: dict) -> None:
        """The one path that already worked must not regress on the shared helper."""
        from yadgar.core.server.tools.task import task_write

        task_write(project_id="m-agahi/yadgar", title="t", id=7, blocked_by=[5])

        assert _forward_capture["op"] == "update_task_row"
        assert _forward_capture["payload"]["blocked_by"] == [5]


# ── the SessionStart seeder's input payload ─────────────────────────────────


class TestTaskListPayloadCarriesEdges:
    def test_edges_survive_the_projection(self) -> None:
        from yadgar.core.server.http import _task_list_payload

        out = _task_list_payload(
            [{"id": 1, "title": "a", "status": "pending", "blocked_by": [2], "blocks": [3]}]
        )

        assert out == [
            {"id": 1, "title": "a", "status": "pending", "blocked_by": [2], "blocks": [3]}
        ]

    def test_absent_edges_are_omitted_not_emptied(self) -> None:
        """A backend that predates the edge read must not assert "no dependencies"."""
        from yadgar.core.server.http import _task_list_payload

        out = _task_list_payload([{"id": 1, "title": "a", "status": "pending"}])

        assert out == [{"id": 1, "title": "a", "status": "pending"}]

    def test_rows_without_an_id_are_still_dropped(self) -> None:
        from yadgar.core.server.http import _task_list_payload

        assert _task_list_payload([{"title": "a", "status": "pending"}]) == []


class TestRestoreNudgeAsksForEdges:
    async def test_the_seeder_read_opts_in(self) -> None:
        """The one list read that needs the join, because the seeder writes it."""
        from yadgar.core.server import http as http_mod
        from yadgar.core.server.tools import task as task_mod

        captured: dict = {}

        def fake_task_list(**kwargs):
            captured.update(kwargs)
            return []

        with patch.object(task_mod, "task_list", side_effect=fake_task_list):
            await http_mod._task_list_restore_nudge("/repo", project="m-agahi/yadgar")

        assert captured["with_edges"] is True
