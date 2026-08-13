"""Car 6 — the discard-the-result family beyond ``task_write`` (bug-train 2026-08-13).

Same bug CLASS Car 4 fixed on ``task_write``: a ``_forward_admin`` result is
fetched and then either partially or fully discarded without checking ``ok``.

KEY INVARIANT (re-verified for this car, do not assume it generalises):
backend SUCCESS envelopes carry NO ``ok`` key at all (see
``yadgar/backend/admin_exec/ledger.py`` — ``{"rows": [...]}`` / ``{"row": ...}``
on success); only ``{"ok": False, "error": ...}`` signals rejection. So the
correct test everywhere is ``result.get("ok") is False``, never
``not result.get("ok")`` (which would invert every success case, since
success envelopes have no ``ok`` key to be truthy).

Sites fixed here:

  1. ``task_list`` / ``task_get`` (``yadgar/core/server/tools/task.py``) — on
     a backend ``{"ok": False, "error": ...}`` they used to return ``[]`` /
     ``None`` via ``result.get("rows", [])`` / ``result.get("row")``, making a
     backend REJECTION indistinguishable from "the table is genuinely empty"
     / "the row is genuinely absent". DECISION (see module docstrings on
     ``task_list``/``task_get``): raise, rather than silently fail-open or
     widen the return type. Rationale below the test classes.

  2. ``anchor_renew`` (``yadgar/core/server/tools/admin_other.py``) — called
     ``_forward_admin("anchor_renew", ...)``, discarded ``ok``, and
     unconditionally returned ``{"ok": True, ...}``. NOTE: as of this car the
     concrete backend body (``yadgar/backend/admin_exec/memory.py``'s
     ``anchor_renew``) never actually returns ``{"ok": False, ...}`` — it
     raises ``ValueError`` instead, and nothing in the ``/admin`` route
     converts that into an ``ok: False`` envelope (only ``KeyError`` is
     caught there). So this fix is CONTRACT HYGIENE against the general
     ``_forward_admin`` contract (any admin op MAY return ``ok: False``), not
     a fix for a presently-reachable live bug — the only way to exercise it
     today is to mock ``_forward_admin`` directly, which is exactly what the
     test below does.

DECISION RATIONALE for task_list/task_get (raise, not widen-return-type):
``task_list`` has a real, non-test production caller —
``yadgar/core/server/http.py``'s ``_task_list_restore_nudge`` — which does
``list(_rows or [])`` on the ``task_list(...)`` return value with NO type
check, then ``_format_task_list_nudge_rows`` calls ``.get(...)`` on each
element. Widening ``task_list``'s return type to sometimes hand back a dict
envelope (``{"ok": False, "error": ...}``) would make ``list(_rows or [])``
iterate the envelope's KEYS as fake "rows" (bare strings), which
``_format_task_list_nudge_rows`` would then choke on — the exact silent
corruption the fix is supposed to prevent, just moved one level up. Raising
sidesteps this: the one production caller already wraps its
``_ledger.task_list(...)`` call in its own ``try/except Exception`` (see
``http.py``'s ``_task_list_restore_nudge``), so raising is caught RIGHT
THERE and degrades to the existing fail-open ``[]`` nudge behaviour with zero
change for that caller — while any other caller (including a human/agent
invoking the MCP tool directly) now gets a loud, unambiguous error instead of
a silently empty list. ``task_get`` is made to match (same shape) for
consistency even though its ``-> dict | None`` signature could technically
carry the envelope through without a type-contract violation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

# ── task_list / task_get ─────────────────────────────────────────────────────


@pytest.fixture
def _forward_rejects():
    """Patch ``_forward_admin`` in the task module to always report a backend
    rejection, distinguishing it from a genuine HTTP/connection failure."""
    from yadgar.core.server.tools import task as task_mod

    def fake_forward(op: str, payload: dict, timeout_s: float = 30.0):  # noqa: ARG001
        return {"ok": False, "error": "simulated backend rejection"}

    with patch.object(task_mod, "_forward_admin", side_effect=fake_forward):
        yield


class TestTaskListDiscardedOk:
    def test_backend_rejection_raises_not_silently_empty(self, _forward_rejects) -> None:
        """Before the fix: ``task_list`` returned ``[]`` on a backend rejection,
        indistinguishable from "the table is empty"."""
        from yadgar.core.server.tools.task import task_list

        with pytest.raises(Exception, match="simulated backend rejection"):
            task_list(project_id="yadgar")

    def test_connection_failure_still_fails_open_to_empty_list(self) -> None:
        """The EXISTING fail-open contract for a raised exception (backend down,
        network error) must be unchanged — only an explicit ``ok: False``
        envelope becomes loud."""
        from yadgar.core.server.tools import task as task_mod

        def fake_forward(op, payload, timeout_s=30.0):  # noqa: ARG001
            raise ConnectionError("backend unreachable")

        with patch.object(task_mod, "_forward_admin", side_effect=fake_forward):
            result = task_mod.task_list(project_id="yadgar")
        assert result == []

    def test_success_envelope_without_ok_key_still_returns_rows(self) -> None:
        """Guards the KEY INVARIANT: a success envelope carries no ``ok`` key
        at all — must not be misread as a rejection."""
        from yadgar.core.server.tools import task as task_mod

        def fake_forward(op, payload, timeout_s=30.0):  # noqa: ARG001
            return {"rows": [{"id": 1, "title": "a"}]}

        with patch.object(task_mod, "_forward_admin", side_effect=fake_forward):
            result = task_mod.task_list(project_id="yadgar")
        assert result == [{"id": 1, "title": "a"}]


class TestTaskGetDiscardedOk:
    def test_backend_rejection_raises_not_silently_none(self, _forward_rejects) -> None:
        """Before the fix: ``task_get`` returned ``None`` on a backend rejection,
        indistinguishable from "the row does not exist"."""
        from yadgar.core.server.tools.task import task_get

        with pytest.raises(Exception, match="simulated backend rejection"):
            task_get(project_id="yadgar", id=231)

    def test_connection_failure_still_fails_open_to_none(self) -> None:
        from yadgar.core.server.tools import task as task_mod

        def fake_forward(op, payload, timeout_s=30.0):  # noqa: ARG001
            raise ConnectionError("backend unreachable")

        with patch.object(task_mod, "_forward_admin", side_effect=fake_forward):
            result = task_mod.task_get(project_id="yadgar", id=231)
        assert result is None

    def test_success_envelope_without_ok_key_still_returns_row(self) -> None:
        from yadgar.core.server.tools import task as task_mod

        def fake_forward(op, payload, timeout_s=30.0):  # noqa: ARG001
            return {"row": {"id": 231, "title": "x"}}

        with patch.object(task_mod, "_forward_admin", side_effect=fake_forward):
            result = task_mod.task_get(project_id="yadgar", id=231)
        assert result == {"id": 231, "title": "x"}

    def test_absent_row_still_returns_none(self) -> None:
        """Unchanged: a genuinely absent row (``{"row": None}``, no ``ok`` key)
        still returns ``None``, not an exception."""
        from yadgar.core.server.tools import task as task_mod

        with patch.object(task_mod, "_forward_admin", return_value={"row": None}):
            result = task_mod.task_get(project_id="yadgar", id=999)
        assert result is None


# ── anchor_renew ──────────────────────────────────────────────────────────────


from yadgar.core import server  # noqa: E402
from yadgar.tests.core.conftest import TEST_PROJECT_ID  # noqa: E402


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    server.init_engines(
        db_path=str(tmp_path / "car6_anchor_renew.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


def _insert_anchor() -> int:
    storage = server._get_storage()
    embeddings = server._get_embeddings()
    content = "car6 anchor_renew discarded-ok regression fixture"
    row: dict = {
        "content": content,
        "embedding": embeddings.encode(content),
        "tags": ["_anchor", "yadgar"],
        "store_type": "episodic",
        "directory_context": "/home/user/project",
        "project_id": TEST_PROJECT_ID,
        "heat": 1.0,
        "importance": 1.0,
        "is_protected": True,
        "is_stale": False,
        "file_hash": None,
        "embedding_model": embeddings.get_model_name(),
        "tier": "conditional",
        "valid_until": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    }
    return storage.insert_memory(row)


class TestAnchorRenewDiscardedOk:
    def test_backend_rejection_propagates_instead_of_unconditional_ok_true(self) -> None:
        """Before the fix: ``anchor_renew`` discarded ``_forward_admin``'s
        result and unconditionally returned ``{"ok": True, ...}`` — even if
        the backend reported ``ok: False``.

        The concrete backend body raises rather than returning ``ok: False``
        today (see module docstring), so this is exercised via a direct mock
        of ``_forward_admin`` — contract hygiene, not a live-bug repro.
        Deliberately does NOT use the ``admin_backend_bypass`` fixture: that
        fixture monkeypatches ``_forward_admin`` in this exact module and
        would clobber the mock below.
        """
        from yadgar.core.server.tools import admin_other as admin_other_mod

        mid = _insert_anchor()

        with patch.object(
            admin_other_mod,
            "_forward_admin",
            return_value={"ok": False, "error": "simulated backend rejection"},
        ):
            result = admin_other_mod.anchor_renew(mid, ttl_days=30, reason="r")

        assert result.get("ok") is False, (
            f"a backend rejection must propagate, not be discarded in favor of an "
            f"unconditional ok:True envelope, got {result!r}"
        )
        assert "simulated backend rejection" in str(result.get("error", ""))

    def test_success_envelope_without_ok_key_still_returns_ok_true(self) -> None:
        """Guards the KEY INVARIANT for this site too: a success envelope
        (the updated memory dict, no ``ok`` key) must still produce
        ``ok: True``."""
        from yadgar.core.server.tools import admin_other as admin_other_mod

        mid = _insert_anchor()
        result = admin_other_mod.anchor_renew(mid, ttl_days=30, reason="r")

        assert result.get("ok") is True
        assert result.get("memory_id") == mid
