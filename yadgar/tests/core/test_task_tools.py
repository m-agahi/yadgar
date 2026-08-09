"""Car D — task tools MCP tests (0047 spine train, §7 row D).

The three task tools (``task_write`` / ``task_list`` / ``task_get``) replace
the markdown `{project}-task-list` wiki page as the **source of truth** for
task tracking (ADR-0133). They forward over HTTP to the backend
``yadgar.backend.admin_exec.ledger`` op bodies — core NEVER touches the DB
directly (ADR-0078 §15).

Contract pinned here:
  * §15 — core calls ``_forward_admin`` (HTTP), NOT ``_get_storage()``.
  * D37 — ``task_list`` defaults to open-only (``status IN (pending, in_progress)``).
  * D11 — ``_format_task_id`` emits ``[<id>]`` for the harness-render prefix.
  * D12 — title ≤ 200 chars (reject-on-write).
  * §14.1 — no ``origin`` parameter (column dropped).
  * §16.10 — ``state`` cleared to NULL when ``status`` → ``completed``/``archived``.
  * D39 — ``blocked_by`` / ``blocks`` manage ``task_blocked_by`` join edges.
  * §13.2 blocker 2 — payload keys use ``id``, never ``number``.
  * ADR-0202 — ``project_id`` arrives from the caller; the tool never derives it.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

# ── Module-level fixtures ───────────────────────────────────────────────────


@pytest.fixture
def _forward_capture():
    """Patch _forward_admin in the task module so tests can inspect forwarded
    payloads without an HTTP round-trip."""
    from yadgar.core.server.tools import task as task_mod

    captured: dict = {}

    def fake_forward(op: str, payload: dict, timeout_s: float = 30.0) -> dict:  # noqa: ARG001
        captured["op"] = op
        captured["payload"] = payload
        # Backend inserts / updates return the row dict (incl. id).
        if op == "create_task_row":
            return {"id": 231, **payload}
        if op == "update_task_row":
            return {"id": payload.get("id", 231), **payload}
        if op == "list_task_rows":
            return {"rows": []}
        if op == "get_task_row":
            return {"row": None}
        return {}

    with patch.object(task_mod, "_forward_admin", side_effect=fake_forward):
        yield captured


# ── _format_task_id (D11) ───────────────────────────────────────────────────


class TestFormatTaskId:
    def test_local_id_emits_bracket_prefix(self) -> None:
        """D11: ``[231]`` form for a local task id."""
        from yadgar.core.server.tools.task import _format_task_id

        assert _format_task_id(231) == "[231]"

    def test_id_1_emits_one_bracket(self) -> None:
        from yadgar.core.server.tools.task import _format_task_id

        assert _format_task_id(1) == "[1]"


# ── _validate_title / _validate_project_id (D12, strict types) ──────────────


class TestValidateTitle:
    def test_rejects_non_string(self) -> None:
        from yadgar.core.server.tools.task import _validate_title

        with pytest.raises(ValueError):
            _validate_title(123)  # type: ignore[arg-type]

    def test_rejects_empty(self) -> None:
        from yadgar.core.server.tools.task import _validate_title

        with pytest.raises(ValueError):
            _validate_title("")

    def test_rejects_over_200_chars(self) -> None:
        """D12: title ≤ 200 chars, reject-on-write."""
        from yadgar.core.server.tools.task import _validate_title

        with pytest.raises(ValueError):
            _validate_title("a" * 201)

    def test_accepts_200_chars(self) -> None:
        from yadgar.core.server.tools.task import _validate_title

        assert _validate_title("a" * 200) == "a" * 200


class TestValidateProjectId:
    def test_rejects_non_string(self) -> None:
        from yadgar.core.server.tools.task import _validate_project_id

        with pytest.raises(ValueError):
            _validate_project_id(123)  # type: ignore[arg-type]

    def test_rejects_empty(self) -> None:
        from yadgar.core.server.tools.task import _validate_project_id

        with pytest.raises(ValueError):
            _validate_project_id("")

    def test_accepts_normal_string(self) -> None:
        from yadgar.core.server.tools.task import _validate_project_id

        assert _validate_project_id("yadgar") == "yadgar"


# ── task_write ──────────────────────────────────────────────────────────────


class TestTaskWriteCreate:
    def test_create_forwards_create_op_without_id(self, _forward_capture: dict) -> None:
        """Create: forwarded op is ``create_task_row``; payload has NO ``id``."""
        from yadgar.core.server.tools.task import task_write

        result = task_write(project_id="yadgar", title="ship car D")

        assert result == {"ok": True, "id": 231}
        assert _forward_capture["op"] == "create_task_row"
        payload = _forward_capture["payload"]
        assert payload["project_id"] == "yadgar"
        assert payload["title"] == "ship car D"
        assert "id" not in payload  # create never carries id

    def test_create_does_not_call_storage_directly(self) -> None:
        """§15: core MUST NOT call ``_get_storage().create_task_row()`` directly."""
        from yadgar.core.server.tools import task as task_mod

        storage_mock = patch(
            "yadgar._shared.runtime.lifecycle._get_storage",
            autospec=True,
        )
        with storage_mock as mock_storage, patch.object(task_mod, "_forward_admin") as fake_fwd:
            fake_fwd.return_value = {"id": 1}
            task_mod.task_write(project_id="yadgar", title="x")

        # The storage accessor must NEVER be invoked on the forward path.
        mock_storage.assert_not_called()

    def test_create_returns_ok_envelope_with_int_id(self, _forward_capture: dict) -> None:
        from yadgar.core.server.tools.task import task_write

        result = task_write(project_id="yadgar", title="ship car D")
        assert result.get("ok") is True
        assert isinstance(result.get("id"), int)

    def test_create_does_not_pass_origin(self, _forward_capture: dict) -> None:
        """§14.1: ``origin`` column dropped; do NOT pass it."""
        from yadgar.core.server.tools.task import task_write

        task_write(project_id="yadgar", title="x")
        assert "origin" not in _forward_capture["payload"]

    def test_create_rejects_title_over_200_chars(self) -> None:
        """D12."""
        from yadgar.core.server.tools.task import task_write

        result = task_write(project_id="yadgar", title="a" * 201)
        assert result.get("ok") is False
        assert "title" in result.get("error", "").lower()

    def test_create_rejects_non_string_project_id(self) -> None:
        from yadgar.core.server.tools.task import task_write

        result = task_write(project_id=123, title="x")  # type: ignore[arg-type]
        assert result.get("ok") is False
        assert "project_id" in result.get("error", "").lower()


class TestTaskWriteUpdate:
    def test_update_clears_state_on_completion(self, _forward_capture: dict) -> None:
        """§16.10: ``state`` is NULL once ``status`` is completed/archived."""
        from yadgar.core.server.tools.task import task_write

        result = task_write(
            project_id="yadgar",
            title="ignored on update",
            id=231,
            status="completed",
            state="planned",
        )

        assert result.get("ok") is True
        assert _forward_capture["op"] == "update_task_row"
        payload = _forward_capture["payload"]
        assert payload["id"] == 231
        assert payload["status"] == "completed"
        assert payload["state"] is None  # cleared by the tool layer

    def test_update_clears_state_on_archive(self, _forward_capture: dict) -> None:
        from yadgar.core.server.tools.task import task_write

        task_write(
            project_id="yadgar",
            title="ignored on update",
            id=231,
            status="archived",
            state="open",
        )
        assert _forward_capture["payload"]["state"] is None

    def test_update_partial_update_omits_title_when_not_given(self, _forward_capture: dict) -> None:
        """Update path: ``title=None`` means "leave unchanged"."""
        from yadgar.core.server.tools.task import task_write

        task_write(project_id="yadgar", title="ignored", id=231)
        payload = _forward_capture["payload"]
        # ``title`` is the create-time primary — on update it should NOT be in
        # the forwarded UPDATE payload unless the caller passed it explicitly.
        assert "title" not in payload

    def test_update_does_not_pass_origin(self, _forward_capture: dict) -> None:
        from yadgar.core.server.tools.task import task_write

        task_write(project_id="yadgar", title="ignored", id=231)
        assert "origin" not in _forward_capture["payload"]


class TestTaskWriteBlockedBy:
    def test_update_with_blocked_by_forwards_join_edge_sync(self, _forward_capture: dict) -> None:
        """D39: ``blocked_by=[5, 7]`` drives ``task_blocked_by`` join-edge sync."""
        from yadgar.core.server.tools.task import task_write

        task_write(
            project_id="yadgar",
            title="ignored",
            id=231,
            blocked_by=[5, 7],
        )
        payload = _forward_capture["payload"]
        assert payload["blocked_by"] == [5, 7]


# ── task_list (D37) ─────────────────────────────────────────────────────────


class TestTaskList:
    def test_defaults_open_only(self, _forward_capture: dict) -> None:
        """D37: default ``status IN (pending, in_progress)``."""
        from yadgar.core.server.tools.task import task_list

        result = task_list(project_id="yadgar")

        assert _forward_capture["op"] == "list_task_rows"
        payload = _forward_capture["payload"]
        assert payload["project_id"] == "yadgar"
        assert payload["status"] == ["pending", "in_progress"]
        # Return value is the rows list extracted from the backend envelope.
        assert result == []

    def test_include_closed_forwards_status_none(self, _forward_capture: dict) -> None:
        from yadgar.core.server.tools.task import task_list

        task_list(project_id="yadgar", include_closed=True)
        payload = _forward_capture["payload"]
        # include_closed=True means no status filter — backend returns everything.
        assert payload["status"] is None

    def test_explicit_status_overrides_default(self, _forward_capture: dict) -> None:
        """Explicit ``status`` list wins over ``include_closed``."""
        from yadgar.core.server.tools.task import task_list

        task_list(project_id="yadgar", include_closed=True, status=["archived"])
        payload = _forward_capture["payload"]
        assert payload["status"] == ["archived"]

    def test_returns_rows_envelope_field(self) -> None:
        """``task_list`` returns ``{"rows": [...]}``'s ``rows`` value (the list)."""
        from yadgar.core.server.tools import task as task_mod

        def fake_forward(op, payload, timeout_s=30.0):  # noqa: ARG001
            if op == "list_task_rows":
                return {"rows": [{"id": 1, "title": "a"}, {"id": 2, "title": "b"}]}
            return {}

        with patch.object(task_mod, "_forward_admin", side_effect=fake_forward):
            rows = task_mod.task_list(project_id="yadgar", include_closed=True)
        assert rows == [{"id": 1, "title": "a"}, {"id": 2, "title": "b"}]


# ── task_get ────────────────────────────────────────────────────────────────


class TestTaskGet:
    def test_forwards_id_not_number(self, _forward_capture: dict) -> None:
        """§13.2 blocker 2: payload key is ``id``, NOT ``number``."""
        from yadgar.core.server.tools.task import task_get

        task_get(project_id="yadgar", id=231)

        assert _forward_capture["op"] == "get_task_row"
        payload = _forward_capture["payload"]
        assert payload == {"id": 231}
        assert "number" not in payload

    def test_returns_row_envelope_field(self) -> None:
        """``task_get`` returns the ``row`` dict (or ``None``) from the backend envelope."""
        from yadgar.core.server.tools import task as task_mod

        with patch.object(
            task_mod,
            "_forward_admin",
            return_value={"row": {"id": 231, "title": "x"}},
        ):
            result = task_mod.task_get(project_id="yadgar", id=231)
        assert result == {"id": 231, "title": "x"}

    def test_absent_row_returns_none(self) -> None:
        from yadgar.core.server.tools import task as task_mod

        with patch.object(task_mod, "_forward_admin", return_value={"row": None}):
            result = task_mod.task_get(project_id="yadgar", id=999)
        assert result is None
