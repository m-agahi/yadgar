"""ADR (Architecture Decision Record) MCP tool — TDD test suite.

Car 2 (ADR-consultable, v5.141.0) rewrote adr_add to write recall-native records:
  * one CANONICAL wiki page per ADR (`<project>-adr-NNNN`, branch IS NULL)
  * one thin CANONICAL index (`<project>-adr-index`) — the ID source of truth

The OLD contract (branch_hint=default-branch pin on the `<project>-adr-log`
monolith) is REVERSED: ADR pages must resolve from any caller branch AND in
non-git dirs, WITHOUT a branch_hint (the memory-531352 bug fix).

Tests cover:
  1. Validation (pure unit, no store)
  2. Canonical round-trip: sequential IDs, readable without branch_hint, index rows
  3. adr_get / adr_list
  4. Supersede: status tag flip + index back-link
  5. Concurrent ID assignment (per-project lock)
  6. adr_due signal (unchanged — still nudges on capture)
  7. Migration-parse helpers (monolith → index) — see test_migrate_adr_monolith.py

RED before implementation; GREEN after.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import UTC
from unittest.mock import MagicMock, patch

import pytest

from yadgar._shared.storage.migrations import _migration_013_wiki_page_version
from yadgar.core import server

UTC = UTC

_TEST_DIR = "/tmp/test-project-adr"


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    """Embedded storage with isolated temp database per test."""
    tmp_path = tmp_path_factory.mktemp("adr")
    server.init_engines(
        db_path=str(tmp_path / "adr_test.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    _migration_013_wiki_page_version(server._get_storage())
    yield
    server.shutdown()


# Minimal valid ADR call params (excludes directory which is passed separately)
_VALID_ADR_PARAMS = dict(
    directory=_TEST_DIR,
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


# ── 2. Canonical round-trip (real embedded store) ─────────────────────────────


@pytest.mark.usefixtures("admin_backend_bypass")
class TestAdrAddCanonicalRoundTrip:
    """End-to-end against the real embedded wiki store.

    Patches ONLY _resolve_project_root. The wiki layer is real — proves the
    canonical write + read-your-writes ID assignment + branch-NULL resolution.
    """

    def test_sequential_ids_and_per_adr_pages(self, tmp_path):
        """Two adr_add calls → ADR-0001, ADR-0002; each has its own canonical page."""
        from yadgar.core.server.tools.adr import adr_add
        from yadgar.core.server.tools.wiki import wiki_read

        project_dir = str(tmp_path / "myproj")
        __import__("os").makedirs(project_dir, exist_ok=True)
        params = dict(_VALID_ADR_PARAMS, directory=project_dir)

        with patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=project_dir):
            r1 = adr_add(**params)
            r2 = adr_add(**dict(params, title="Adopt SQLite for embedding cache"))

        assert r1.get("adr_id") == "ADR-0001", f"First: {r1}"
        assert r2.get("adr_id") == "ADR-0002", f"Second: {r2}"
        assert r1.get("slug") == "myproj-adr-0001"
        assert r2.get("slug") == "myproj-adr-0002"

        # Each per-ADR page resolves CANONICALLY — WITHOUT a branch_hint.
        p1 = wiki_read("myproj-adr-0001", directory=project_dir)
        p2 = wiki_read("myproj-adr-0002", directory=project_dir)
        assert "error" not in p1, f"ADR-0001 page not found canonically: {p1}"
        assert "error" not in p2, f"ADR-0002 page not found canonically: {p2}"
        assert p1.get("branch") is None, (
            f"ADR page must be canonical (branch NULL): {p1.get('branch')!r}"
        )
        assert "SurrealDB" in p1.get("content", "")
        assert "SQLite" in p2.get("content", "")
        # page_type + tags
        assert p1.get("page_type") == "adr"
        assert "adr" in (p1.get("tags") or [])
        assert "adr-status:accepted" in (p1.get("tags") or [])

    def test_index_rows_track_all_adrs(self, tmp_path):
        """The canonical index carries one row per ADR, readable without branch_hint."""
        from yadgar.core.server.tools.adr import adr_add, parse_index_rows
        from yadgar.core.server.tools.wiki import wiki_read

        project_dir = str(tmp_path / "idxproj")
        __import__("os").makedirs(project_dir, exist_ok=True)
        params = dict(_VALID_ADR_PARAMS, directory=project_dir)

        with patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=project_dir):
            adr_add(**params)
            adr_add(**dict(params, title="Second decision", status="open"))

        index = wiki_read("idxproj-adr-index", directory=project_dir)
        assert "error" not in index, f"index not found canonically: {index}"
        assert index.get("branch") is None, "index must be canonical"
        rows = parse_index_rows(index["content"])
        assert [r["adr_id"] for r in rows] == ["ADR-0001", "ADR-0002"]
        assert rows[0]["slug"] == "idxproj-adr-0001"
        assert rows[1]["status"] == "open"

    def test_body_header_does_not_poison_id_scan(self, tmp_path):
        """A col-0 ``## ADR-NNNN`` line inside a body field must not poison ID assignment.

        Canonical model: IDs come from the INDEX table, so a body ## ADR-9999
        cannot poison the sequence (index rows, not headers, drive next-id).
        """
        from yadgar.core.server.tools.adr import adr_add

        project_dir = str(tmp_path / "poisonproj")
        __import__("os").makedirs(project_dir, exist_ok=True)
        poison = dict(
            _VALID_ADR_PARAMS,
            directory=project_dir,
            context="Considered:\n## ADR-9999: a referenced decision\ninline.",
        )
        with patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=project_dir):
            r1 = adr_add(**poison)
            r2 = adr_add(**dict(_VALID_ADR_PARAMS, directory=project_dir, title="Second"))
        assert r1.get("adr_id") == "ADR-0001", f"First: {r1}"
        assert r2.get("adr_id") == "ADR-0002", f"Body ## ADR-9999 poisoned id scan: {r2}"


# ── 3. adr_get / adr_list ─────────────────────────────────────────────────────


@pytest.mark.usefixtures("admin_backend_bypass")
class TestAdrGetList:
    def test_adr_get_fetches_page(self, tmp_path):
        from yadgar.core.server.tools.adr import adr_add, adr_get

        project_dir = str(tmp_path / "getproj")
        __import__("os").makedirs(project_dir, exist_ok=True)
        with patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=project_dir):
            adr_add(**dict(_VALID_ADR_PARAMS, directory=project_dir))
            got = adr_get(directory=project_dir, adr_id="ADR-0001")
            # accepts loose forms too
            got_loose = adr_get(directory=project_dir, adr_id="1")
        assert "error" not in got, f"adr_get failed: {got}"
        assert "SurrealDB" in got.get("content", "")
        assert "error" not in got_loose, f"loose adr_id failed: {got_loose}"

    def test_adr_get_missing_returns_error(self, tmp_path):
        from yadgar.core.server.tools.adr import adr_get

        project_dir = str(tmp_path / "getempty")
        __import__("os").makedirs(project_dir, exist_ok=True)
        with patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=project_dir):
            got = adr_get(directory=project_dir, adr_id="ADR-0099")
        assert "error" in got

    def test_adr_list_all_and_status_filter(self, tmp_path):
        from yadgar.core.server.tools.adr import adr_add, adr_list

        project_dir = str(tmp_path / "listproj")
        __import__("os").makedirs(project_dir, exist_ok=True)
        with patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=project_dir):
            adr_add(**dict(_VALID_ADR_PARAMS, directory=project_dir, status="accepted"))
            adr_add(
                **dict(_VALID_ADR_PARAMS, directory=project_dir, title="Open one", status="open")
            )
            all_adrs = adr_list(directory=project_dir)
            open_only = adr_list(directory=project_dir, status="open")

        assert all_adrs["count"] == 2, f"expected 2 ADRs, got {all_adrs}"
        assert open_only["count"] == 1, f"expected 1 open ADR, got {open_only}"
        assert open_only["adrs"][0]["status"] == "open"

    def test_adr_list_empty_when_absent(self, tmp_path):
        from yadgar.core.server.tools.adr import adr_list

        project_dir = str(tmp_path / "listempty")
        __import__("os").makedirs(project_dir, exist_ok=True)
        with patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=project_dir):
            result = adr_list(directory=project_dir)
        assert result == {"adrs": [], "count": 0}


# ── 4. Supersede: status tag flip + index back-link ───────────────────────────


@pytest.mark.usefixtures("admin_backend_bypass")
class TestAdrSupersede:
    def test_supersede_flips_status_and_backlinks_index(self, tmp_path):
        """ADR-0002 supersedes ADR-0001 → target status 'superseded' + index back-link;
        adr_list(status='open') excludes the superseded target."""
        from yadgar.core.server.tools.adr import adr_add, adr_get, adr_list

        project_dir = str(tmp_path / "supproj")
        __import__("os").makedirs(project_dir, exist_ok=True)
        with patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=project_dir):
            adr_add(**dict(_VALID_ADR_PARAMS, directory=project_dir, status="accepted"))
            adr_add(
                **dict(
                    _VALID_ADR_PARAMS,
                    directory=project_dir,
                    title="Reversal decision",
                    status="accepted",
                    supersedes="ADR-0001",
                )
            )
            target = adr_get(directory=project_dir, adr_id="ADR-0001")
            listing = adr_list(directory=project_dir)

        # Target page's status tag flipped to superseded.
        assert "adr-status:superseded" in (target.get("tags") or []), (
            f"target status tag not flipped: {target.get('tags')}"
        )
        # Index reflects the supersede: ADR-0001 status superseded, superseded_by names 0002.
        by_id = {r["adr_id"]: r for r in listing["adrs"]}
        assert by_id["ADR-0001"]["status"] == "superseded"
        assert "0002" in by_id["ADR-0001"]["superseded_by"]


# ── 5. Concurrent ID-assignment race (per-project lock) ───────────────────────


@pytest.mark.usefixtures("admin_backend_bypass")
class TestAdrAddConcurrentIdAssignment:
    def test_concurrent_calls_produce_distinct_ids(self, tmp_path):
        """Two simultaneous adr_add on a fresh project → ADR-0001 and ADR-0002 (no dup)."""
        import os

        import yadgar.core.server.tools.adr as _adr_mod
        from yadgar.core.server.tools.adr import adr_add, adr_list

        project_dir = str(tmp_path / "racetest")
        os.makedirs(project_dir, exist_ok=True)

        results: list[dict] = []
        errors: list[Exception] = []
        entry_barrier = threading.Barrier(2)
        id_barrier = threading.Barrier(2)
        _real_next = _adr_mod._next_adr_id_from_index

        def _slow_next(content: str) -> str:
            rid = _real_next(content)
            try:
                id_barrier.wait(timeout=0.5)
            except threading.BrokenBarrierError:
                pass
            time.sleep(0.05)
            return rid

        params = dict(_VALID_ADR_PARAMS, directory=project_dir)

        def _call(title: str) -> None:
            try:
                try:
                    entry_barrier.wait(timeout=10)
                except threading.BrokenBarrierError:
                    pass
                results.append(adr_add(**dict(params, title=title)))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        with (
            patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=project_dir),
            patch("yadgar.core.server.tools.adr._next_adr_id_from_index", side_effect=_slow_next),
        ):
            threads = [threading.Thread(target=_call, args=(f"Concurrent {i}",)) for i in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

        assert not errors, f"Thread(s) raised: {errors}"
        assert len(results) == 2, f"Expected 2 results: {results}"
        returned = [r.get("adr_id") for r in results]
        assert len(set(returned)) == 2, f"Duplicate ADR IDs (race not fixed): {returned}"

        with patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=project_dir):
            listing = adr_list(directory=project_dir)
        ids = {r["adr_id"] for r in listing["adrs"]}
        assert ids == {"ADR-0001", "ADR-0002"}, f"Index must hold both: {ids}"

    def test_different_project_roots_do_not_block_each_other(self, tmp_path):
        import os

        from yadgar.core.server.tools.adr import adr_add

        proj_a = str(tmp_path / "proj_a")
        proj_b = str(tmp_path / "proj_b")
        os.makedirs(proj_a, exist_ok=True)
        os.makedirs(proj_b, exist_ok=True)

        timings: dict[str, float] = {}
        barrier = threading.Barrier(2)

        def _call(project_dir: str, key: str) -> None:
            with patch(
                "yadgar.core.server.tools.adr._resolve_project_root", return_value=project_dir
            ):
                barrier.wait()
                t0 = time.monotonic()
                adr_add(**dict(_VALID_ADR_PARAMS, directory=project_dir))
                timings[key] = time.monotonic() - t0

        threads = [
            threading.Thread(target=_call, args=(proj_a, "a")),
            threading.Thread(target=_call, args=(proj_b, "b")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert "a" in timings and "b" in timings, f"deadlock? timings={timings}"


# ── 5b. wait_timeout resilience (the RYW-on-timeout race fix) ─────────────────


@pytest.mark.usefixtures("admin_backend_bypass")
class TestAdrWaitTimeoutResilience:
    """A wait=True index write that only QUEUES (wait_timeout) must not fail adr_add,
    and next-ID correctness must survive a lagging index (committed page-slug scan)."""

    def test_write_ok_predicate(self):
        from yadgar.core.server.tools.adr import _write_ok

        assert _write_ok({"stored": True, "committed": True}) is True
        assert _write_ok({"stored": True, "queued": True}) is True
        # wait_timeout — still queued, converges → NOT a failure.
        assert _write_ok({"stored": False, "reason": "wait_timeout", "queued": True}) is True
        # hard terminal rejections ARE failures.
        assert _write_ok({"stored": False, "reason": "duplicate_detected"}) is False
        assert _write_ok({"stored": False, "reason": "blocked_by_policy: x"}) is False

    def test_next_id_uses_committed_page_slug_when_index_lags(self, tmp_path):
        """With ADR-0001's page committed but the index EMPTY (lagging), the next id
        is ADR-0002 — the committed page slug scan prevents a duplicate ID."""
        from yadgar.core.server.tools.adr import _next_adr_id, adr_add

        project_dir = str(tmp_path / "lagproj")
        __import__("os").makedirs(project_dir, exist_ok=True)
        with patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=project_dir):
            r1 = adr_add(**dict(_VALID_ADR_PARAMS, directory=project_dir))
            assert r1.get("adr_id") == "ADR-0001"
            # Simulate a fully-lagged index: next-id computed from an EMPTY index
            # must still skip ADR-0001 because the committed page slug is scanned.
            nxt = _next_adr_id(project_dir, "")
        assert nxt == "ADR-0002", f"committed page slug must bump next id: {nxt}"

    def test_adr_add_ok_when_index_write_times_out(self, tmp_path):
        """adr_add returns ok (not an error) when the INDEX write returns wait_timeout —
        the page committed and the index converges on the next drain."""
        import yadgar.core.server.tools.adr as _adr_mod
        from yadgar.core.server.tools.adr import adr_add

        project_dir = str(tmp_path / "toproj")
        __import__("os").makedirs(project_dir, exist_ok=True)

        real_canonical = _adr_mod._wiki_write_canonical
        calls = {"n": 0}

        def _canonical_with_index_timeout(payload, wait=False):
            calls["n"] += 1
            # First call = per-ADR page (commit it for real); second = index (timeout).
            if payload.get("tags") == ["adr", "adr-index"]:
                # Still enqueue so it converges, then report a wait_timeout.
                real_canonical(payload, wait=False)
                return {"stored": False, "reason": "wait_timeout", "queued": True}
            return real_canonical(payload, wait=wait)

        with (
            patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=project_dir),
            patch.object(_adr_mod, "_wiki_write_canonical", _canonical_with_index_timeout),
        ):
            result = adr_add(**dict(_VALID_ADR_PARAMS, directory=project_dir))

        assert "error" not in result, f"index wait_timeout must NOT fail adr_add: {result}"
        assert result.get("adr_id") == "ADR-0001"
        assert result.get("slug") == "toproj-adr-0001"


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


# ── 7. Monolith-parse helpers (migration source) ──────────────────────────────


class TestMonolithParseHelpers:
    def test_parse_adr_ids_from_monolith(self):
        from yadgar.core.server.tools.adr import parse_adr_ids

        content = (
            "## ADR-0001: First\n- status: accepted\n"
            "## ADR-0003: Third\nbody refs ADR-0099 (ignored)\n"
            "## ADR-0002: Second\n- status: open\n"
        )
        assert parse_adr_ids(content) == ["ADR-0003", "ADR-0002", "ADR-0001"]

    def test_index_next_id_ignores_body(self):
        from yadgar.core.server.tools.adr import _build_index_content, _next_adr_id_from_index

        content = _build_index_content(
            "proj",
            [
                {
                    "adr_id": "ADR-0005",
                    "status": "open",
                    "date": "d",
                    "title": "t",
                    "supersedes": "none",
                    "superseded_by": "-",
                    "slug": "proj-adr-0005",
                }
            ],
        )
        assert _next_adr_id_from_index(content) == "ADR-0006"


# Guard: index round-trip survives pipes / markdown in the title.
def test_index_row_sanitises_pipes():
    from yadgar.core.server.tools.adr import _build_index_content, parse_index_rows

    content = _build_index_content(
        "proj",
        [
            {
                "adr_id": "ADR-0001",
                "status": "open",
                "date": "2026-01-01",
                "title": "A | B | C table title",
                "supersedes": "none",
                "superseded_by": "-",
                "slug": "proj-adr-0001",
            }
        ],
    )
    rows = parse_index_rows(content)
    assert len(rows) == 1
    assert rows[0]["adr_id"] == "ADR-0001"
    # No stray table columns injected by the pipe.
    header_rows = re.findall(r"^\| ADR-\d{4} \|", content, re.MULTILINE)
    assert len(header_rows) == 1
