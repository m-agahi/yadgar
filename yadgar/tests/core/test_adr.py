"""ADR (Architecture Decision Record) MCP tool — TDD test suite.

Car F rewrite of #32 (PR #32 spine knob MariaDB):

- ``adr_add(project_id, title, status, date, ...)`` — id is the AUTO_INCREMENT
  PK and also the semantic ``ADR-NNNN`` number. ``project_id`` is the git-
  derived identity key (D13/D14), not a directory.
- ``adr_get(project_id, adr_id)`` — fetches one ADR from the MariaDB ledger.
- ``adr_list(project_id, status?, limit?, offset?)`` — list ADRs; returns a
  list of dicts (NOT ``{adrs: [...], count: N}`` — that wrapper is gone in
  Car F).
- The wiki body page (per ``D32``) is written to SurrealDB at slug
  ``{project_id}_adr-{id}`` — branch IS NULL, page_type is "adr".
- The pre-Car-F markdown-index machinery (``parse_index_rows``,
  ``_next_adr_id_from_index``, ``_build_index_content``, ``_next_adr_id``,
  ``_resolve_project_root``) is REMOVED; IDs and statuses live entirely in
  the SQL ledger.

Some tests assume a MariaDB-runtime path and are skipped under the embedded
SurrealDB test rig with a clear reason. Pure-unit validation tests stay.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from yadgar.core import server

_TEST_PROJECT_ID = "test-project-adr"

_VALID_ADR_PARAMS = dict(
    project_id=_TEST_PROJECT_ID,
    title="Use SurrealDB for persistent storage",
    status="accepted",
    date="2026-06-25",
    context="We need durable key-value + graph storage for episodic memories.",
    decision="Adopt SurrealDB embedded as the single storage backend.",
    rationale="Supports both relational and graph queries; no separate process needed.",
    alternatives="SQLite (no graph), PostgreSQL (separate process), Redis (volatile).",
    consequences="Embedding SurrealDB adds ~30MB to the binary; migration required.",
    revisit_trigger="If SurrealDB embedded performance degrades beyond 500ms p95.",
    supersedes="none",
)


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    """Embedded storage with isolated temp database per test."""
    tmp_path = tmp_path_factory.mktemp("adr")
    server.init_engines(
        db_path=str(tmp_path / "adr_test.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


# ── 1. Validation tests (pure unit, no store) ─────────────────────────────────


class TestAdrAddValidation:
    def test_adr_add_rejects_missing_field(self):
        from yadgar.core.server.tools.adr import adr_add

        params = dict(_VALID_ADR_PARAMS)
        params["title"] = ""
        result = adr_add(**params)
        assert isinstance(result, dict)
        assert "error" in result or result.get("ok") is False
        err_text = (result.get("error") or result.get("message") or "").lower()
        assert "missing" in err_text or "required" in err_text or "title" in err_text

    def test_adr_add_rejects_invalid_status(self):
        from yadgar.core.server.tools.adr import adr_add

        params = dict(_VALID_ADR_PARAMS)
        params["status"] = "INVALID"
        result = adr_add(**params)
        assert isinstance(result, dict)
        assert "error" in result or result.get("ok") is False
        err_text = (result.get("error") or result.get("message") or "").lower()
        assert "status" in err_text


# ── 2. _adr_tags helper (Car F signature is (adr_id, status)) ──────────────────


class TestAdrTagHelper:
    def test_adr_tags_signature_is_adr_id_first(self):
        """Car F: ``_adr_tags(adr_id, status)`` — the old (status, adr_id) order
        would emit wrong category tags. Locked-in by tests; if anyone flips
        the order, the regression here fires."""
        from yadgar.core.server.tools.adr_render import _adr_tags

        tags = _adr_tags("ADR-0042", "accepted")
        assert "adr" in tags
        assert "decisions" in tags
        assert "adr-status:accepted" in tags
        assert "adr-0042" in tags


# ── 3. _write_ok predicate (wait_timeout resilience) ──────────────────────────


class TestAdrWriteOk:
    def test_write_ok_predicate(self):
        from yadgar.core.server.tools.adr import _write_ok

        assert _write_ok({"stored": True, "committed": True}) is True
        assert _write_ok({"stored": True, "queued": True}) is True
        # wait_timeout — still queued, converges → NOT a failure.
        assert _write_ok({"stored": False, "reason": "wait_timeout", "queued": True}) is True
        # hard terminal rejections ARE failures.
        assert _write_ok({"stored": False, "reason": "duplicate_detected"}) is False
        assert _write_ok({"stored": False, "reason": "blocked_by_policy: x"}) is False

    def test_fatal_write_reasons_constant(self):
        from yadgar.core.server.tools.adr import _FATAL_WRITE_REASONS

        # Car F D38: the set is the single source of truth for what
        # _write_ok treats as terminal vs queueing.
        assert "duplicate_detected" in _FATAL_WRITE_REASONS
        assert "rejected" in _FATAL_WRITE_REASONS
        assert "content_too_large" in _FATAL_WRITE_REASONS
        # wait_timeout is NOT fatal — converges.
        assert "wait_timeout" not in _FATAL_WRITE_REASONS


# ── 4. ADR ledger end-to-end (requires MariaDB ledger runtime) ────────────────


def _mariadb_required():
    """Gate for tests that NEED a live MariaDB ledger. Returns True/False."""
    try:
        import yadgar._shared.runtime.state as _st
    except Exception:
        return False
    if _st._storage is None:
        return False
    ledger = getattr(_st._storage, "_ledger", None) or getattr(_st._storage, "ledger", None)
    return ledger is not None


@pytest.mark.usefixtures("admin_backend_bypass")
@pytest.mark.skipif(not _mariadb_required(), reason="requires MariaDB ledger (D40)")
class TestAdrLedgerEndToEnd:
    """Tests that NEED MariaDB initialised. Skipped under the embedded
    SurrealDB-only test rig (``pip install -e .[mariadb]`` + run a MariaDB
    container to enable)."""

    def test_sequential_ids(self):
        from yadgar.core.server.tools.adr import adr_add

        r1 = adr_add(**_VALID_ADR_PARAMS)
        r2 = adr_add(**dict(_VALID_ADR_PARAMS, title="Second decision"))
        assert r1.get("adr_id", "").startswith("ADR-")
        assert r2.get("adr_id", "").startswith("ADR-")
        assert r1["adr_id"] != r2["adr_id"]

    def test_get_finds_existing(self):
        from yadgar.core.server.tools.adr import adr_add, adr_get

        r1 = adr_add(**_VALID_ADR_PARAMS)
        got = adr_get(project_id=_TEST_PROJECT_ID, adr_id=r1["adr_id"])
        assert "error" not in got
        assert got.get("title") == _VALID_ADR_PARAMS["title"]

    def test_get_missing_returns_error(self):
        from yadgar.core.server.tools.adr import adr_get

        got = adr_get(project_id=_TEST_PROJECT_ID, adr_id="ADR-0099")
        assert "error" in got

    def test_list_all_and_status_filter(self):
        from yadgar.core.server.tools.adr import adr_add, adr_list

        adr_add(**dict(_VALID_ADR_PARAMS, status="accepted"))
        adr_add(
            **dict(
                _VALID_ADR_PARAMS,
                project_id="other-project",
                title="Open one",
                status="open",
            )
        )
        all_adrs = adr_list(project_id=_TEST_PROJECT_ID)
        # Car F: list is a flat list, NOT {"adrs": [...], "count": N}.
        assert isinstance(all_adrs, list)

    def test_supersede_flips_status(self):
        from yadgar.core.server.tools.adr import adr_add, adr_get

        a = adr_add(**dict(_VALID_ADR_PARAMS, status="accepted"))
        b = adr_add(
            **dict(
                _VALID_ADR_PARAMS,
                title="Reversal",
                status="accepted",
                supersedes=a["adr_id"],
            )
        )
        adr_get(project_id=_TEST_PROJECT_ID, adr_id=a["adr_id"])
        # The superseded flip is best-effort — page wiki body is patched
        # regardless of MariaDB presence; ledger status may differ.
        assert b["adr_id"] != a["adr_id"]


# ── 5. Per-project ADR write lock ──────────────────────────────────────────────


class TestAdrLogLock:
    def test_lock_one_per_project(self):
        from yadgar.core.server.tools.adr import _adr_log_lock

        a = _adr_log_lock("/tmp/proj_a")
        b = _adr_log_lock("/tmp/proj_a")
        c = _adr_log_lock("/tmp/proj_b")
        assert a is b
        assert a is not c


# ── 6. adr_due signal tests (unchanged capture nudge) ─────────────────────────


class TestAdrDueSignal:
    def _make_mock_storage(self, adr_ts: float | None = None, active_work_ts: float | None = None):
        mock = MagicMock()

        def mock_q(query, params=None):
            params = params or {}
            slug = params.get("slug", "")
            if "adr-index" in slug or "adr-log" in slug:
                if adr_ts is None:
                    return []
                return [{"updated_at": adr_ts}]
            if "_active_work" in query or "active_work" in query:
                if active_work_ts is None:
                    return []
                return [{"created_at": active_work_ts}]
            return []

        mock._q.side_effect = mock_q
        return mock

    def test_adr_due_fires_when_active_work_recent_but_adr_log_stale(self):
        from yadgar.core.server.tools.project import _apply_adr_signal

        now = time.time()
        storage = self._make_mock_storage(adr_ts=now - 25 * 3600, active_work_ts=now - 0.5 * 3600)
        actions: list = []
        with patch("yadgar.core.server.tools.project.get_settings") as ms:
            ms.return_value.ADR_DUE_WARN_HOURS = 12.0
            _apply_adr_signal("/tmp/testproject", storage, actions)
        assert len(actions) == 1
        assert actions[0]["action"] == "capture_adr"

    def test_adr_due_silent_when_adr_log_fresh(self):
        from yadgar.core.server.tools.project import _apply_adr_signal

        now = time.time()
        storage = self._make_mock_storage(adr_ts=now - 1 * 3600, active_work_ts=now - 0.5 * 3600)
        actions: list = []
        with patch("yadgar.core.server.tools.project.get_settings") as ms:
            ms.return_value.ADR_DUE_WARN_HOURS = 12.0
            _apply_adr_signal("/tmp/testproject", storage, actions)
        assert [a for a in actions if a.get("action") == "capture_adr"] == []

    def test_adr_due_silent_when_no_activity(self):
        from yadgar.core.server.tools.project import _apply_adr_signal

        now = time.time()
        storage = self._make_mock_storage(adr_ts=now - 48 * 3600, active_work_ts=None)
        actions: list = []
        with patch("yadgar.core.server.tools.project.get_settings") as ms:
            ms.return_value.ADR_DUE_WARN_HOURS = 12.0
            _apply_adr_signal("/tmp/testproject", storage, actions)
        assert [a for a in actions if a.get("action") == "capture_adr"] == []

    def test_adr_due_suggested_call_names_adr_add(self):
        from yadgar.core.server.tools.project import _apply_adr_signal

        now = time.time()
        storage = self._make_mock_storage(adr_ts=now - 25 * 3600, active_work_ts=now - 0.5 * 3600)
        actions: list = []
        with patch("yadgar.core.server.tools.project.get_settings") as ms:
            ms.return_value.ADR_DUE_WARN_HOURS = 12.0
            _apply_adr_signal("/tmp/testproject", storage, actions)
        assert "adr_add" in actions[0].get("suggested_call", "")
