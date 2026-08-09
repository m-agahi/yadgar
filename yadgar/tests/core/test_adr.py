"""ADR (Architecture Decision Record) MCP tool — TDD test suite.

Car 2 (ADR-consultable, v5.141.0) rewrote adr_add to write recall-native records:
  * one CANONICAL wiki page per ADR (`<project>-adr-NNNN`)
  * one thin CANONICAL index (`<project>-adr-index`) — the ID source of truth

Car F (0047 §7, ADR-tools re-pointed) moves metadata/index rows to the MariaDB
ledger while keeping body pages in SurrealDB (D4). The legacy wiki-index path
is gone (Car G deletes the index parser/renderer/lock); body pages still live
in the wiki store. The integration tests below mock `_forward_admin` so the
ledger writes dispatch to an in-memory ledger fixture — see
``_LedgerStub`` (below). The 194-page ADR re-slug is Car L scope; the test
fixtures use the new D32 ③ slug scheme (`{project_id}_adr-NNNN`) so the
post-Car-F read path is exercised.

Tests cover:
  1. Validation (pure unit, no store)
  2. Canonical round-trip: sequential IDs, readable from any caller, ledger rows
  3. adr_get / adr_list (post-re-point shape)
  4. Supersede: ledger join row + status flip on target row
  5. Concurrent ID-assignment (per-project lock + ledger AUTO_INCREMENT)
  6. adr_due signal (unchanged — still nudges on capture)

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


# ── Car F: ledger stub for the integration tests ─────────────────────────────
# ``_forward_admin`` is the seam Car F introduces. The autouse harness in
# ``tests/conftest.py`` (``_unit_backend_harness``) routes it to
# ``run_admin_op_blocking`` (real backend). For these tests we want a
# deterministic in-process ledger so the assertions can pin ids + rows
# without composing MariaDB engine #2 — that engine is what the admin ops
# wrap. ``_ledger_stub`` keeps a counter + row dict and is wired into the
# relevant test classes via ``_patch_ledger``.


class _LedgerStub:
    """In-memory ledger mimicking ``list_adr_rows`` / ``create_adr_row`` /
    ``set_adr_body_slug`` / ``get_adr_row`` / ``add_adr_supersedes`` for the
    Car F integration tests. NOT a replacement for the real MariaStorageEngine
    — the engine-level CRUD lives in ``yadgar/_shared/storage/sql/mariadb.py``;
    this stub exists only to keep the wiki-index integration tests running
    without an engine #2 fixture."""

    def __init__(self) -> None:
        self._rows: dict[int, dict] = {}
        self._next_id: int = 1

    def _allocate_id(self) -> int:
        new_id = self._next_id
        self._next_id += 1
        return new_id

    def create(self, payload: dict) -> dict:
        new_id = self._allocate_id()
        row = {
            "id": new_id,
            "project_id": payload["project_id"],
            "title": payload["title"],
            "status": payload.get("status", "open"),
            "decided_on": payload.get("decided_on"),
            "subsystem": payload.get("subsystem"),
            "tier": payload.get("tier"),
            "body_slug": payload.get("body_slug"),
        }
        self._rows[new_id] = row
        return {"row": row}

    def list(self, payload: dict) -> dict:
        rows = sorted(
            (r for r in self._rows.values() if r["project_id"] == payload["project_id"]),
            key=lambda r: r["id"],
        )
        if payload.get("status") is not None:
            rows = [r for r in rows if r["status"] == payload["status"]]
        return {"rows": rows}

    def get(self, payload: dict) -> dict:
        row = self._rows.get(int(payload["id"]))
        return {"row": row}

    def set_body_slug(self, payload: dict) -> dict:
        row = self._rows.get(int(payload["id"]))
        if row is not None:
            row["body_slug"] = payload["body_slug"]
        return {"ok": True}

    def add_supersedes(self, payload: dict) -> dict:
        target = self._rows.get(int(payload["supersedes_id"]))
        if target is not None:
            target["status"] = "superseded"
        return {"ok": True}


@pytest.fixture
def _ledger_stub() -> _LedgerStub:
    """Per-test in-memory ledger for the integration tests."""
    return _LedgerStub()


@pytest.fixture
def _patch_ledger(_ledger_stub, monkeypatch, admin_backend_bypass):
    """Patch ``_forward_admin`` on the ``adr`` module to dispatch into
    ``_ledger_stub`` for the Car F admin ops. Body-page writes still go through
    the real ``_wiki_write_canonical`` seam (D4).

    Uses ``monkeypatch.setattr`` (not ``unittest.mock.patch``) so the patch ties
    into the SAME monkeypatch session that ``admin_backend_bypass`` uses, and
    therefore overrides the bypass patch on the same attribute. Without this,
    the admin_backend_bypass (opt-in via usefixtures) wins the patch race and
    routes the call to ``run_admin_op_blocking`` instead of the ledger stub —
    which fails with ``engine #2 not composed`` because the unit-test engine
    stack doesn't wire the MariaStorageEngine."""

    def _forward(op: str, payload: dict, **kwargs) -> dict:
        if op == "list_adr_rows":
            return _ledger_stub.list(payload)
        if op == "create_adr_row":
            return _ledger_stub.create(payload)
        if op == "get_adr_row":
            return _ledger_stub.get(payload)
        if op == "set_adr_body_slug":
            return _ledger_stub.set_body_slug(payload)
        if op == "add_adr_supersedes":
            return _ledger_stub.add_supersedes(payload)
        return {"ok": True}

    monkeypatch.setattr("yadgar.core.server.tools.adr._forward_admin", _forward)
    yield _ledger_stub


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


@pytest.mark.usefixtures("_patch_ledger", "admin_backend_bypass")
class TestAdrAddCanonicalRoundTrip:
    """End-to-end against the real embedded wiki store + the in-memory ledger stub.

    Car F: the body-page write is unchanged (D4 — wiki layer is real); the ID
    source is the ledger AUTO_INCREMENT, mocked here via ``_patch_ledger``.
    """

    def test_sequential_ids_and_per_adr_pages(self, tmp_path, _ledger_stub):
        """Two adr_add calls → ADR-0001, ADR-0002; each has its own canonical page
        at the new D32 ③ slug ``{project_id}_adr-NNNN`` (Car L re-slugs the
        194 pre-existing pages)."""
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
        # Body pages use the new D32 ③ slug scheme. Car L re-slugs the legacy
        # `<project>-adr-NNNN` pages; F writes new pages under the new slug.
        assert r1.get("slug") and r1["slug"].endswith("_adr-0001"), (
            f"body slug must use the new scheme: {r1['slug']!r}"
        )
        assert r2.get("slug") and r2["slug"].endswith("_adr-0002"), (
            f"body slug must use the new scheme: {r2['slug']!r}"
        )

        # Each per-ADR page resolves CANONICALLY from the caller directory.
        p1 = wiki_read(r1["slug"], directory=project_dir)
        p2 = wiki_read(r2["slug"], directory=project_dir)
        assert "error" not in p1, f"ADR-0001 page not found canonically: {p1}"
        assert "error" not in p2, f"ADR-0002 page not found canonically: {p2}"
        assert "SurrealDB" in p1.get("content", "")
        assert "SQLite" in p2.get("content", "")
        # page_type + tags
        assert p1.get("page_type") == "adr"
        assert "adr" in (p1.get("tags") or [])
        assert "adr-status:accepted" in (p1.get("tags") or [])

    def test_ledger_rows_track_all_adrs(self, tmp_path, _ledger_stub):
        """Car F: the ledger holds one row per ADR (replacing the wiki-index)."""
        from yadgar.core.server.tools.adr import adr_add, adr_list

        project_dir = str(tmp_path / "idxproj")
        __import__("os").makedirs(project_dir, exist_ok=True)
        params = dict(_VALID_ADR_PARAMS, directory=project_dir)

        with patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=project_dir):
            adr_add(**params)
            adr_add(**dict(params, title="Second decision", status="open"))

        listing = adr_list(directory=project_dir)
        rows = listing["adrs"]
        assert [r["adr_id"] for r in rows] == ["ADR-0001", "ADR-0002"]
        assert rows[0]["status"] == "accepted"
        assert rows[1]["status"] == "open"

    def test_body_header_does_not_poison_id_scan(self, tmp_path, _ledger_stub):
        """A col-0 ``## ADR-NNNN`` line inside a body field must not poison ID assignment.

        Car F: IDs come from the ledger AUTO_INCREMENT, so a body ## ADR-9999
        cannot poison the sequence (the next id is purely ``last_id + 1``).
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


@pytest.mark.usefixtures("_patch_ledger", "admin_backend_bypass")
class TestAdrGetList:
    def test_adr_get_fetches_page(self, tmp_path, _ledger_stub):
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

    def test_adr_list_all_and_status_filter(self, tmp_path, _ledger_stub):
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

    def test_adr_list_empty_when_absent(self, tmp_path, _ledger_stub):
        from yadgar.core.server.tools.adr import adr_list

        project_dir = str(tmp_path / "listempty")
        __import__("os").makedirs(project_dir, exist_ok=True)
        with patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=project_dir):
            result = adr_list(directory=project_dir)
        assert result == {"adrs": [], "count": 0}


# ── 4. Supersede: ledger join + status flip ───────────────────────────────────


@pytest.mark.usefixtures("_patch_ledger", "admin_backend_bypass")
class TestAdrSupersede:
    def test_supersede_flips_status_and_links_ledger(self, tmp_path, _ledger_stub):
        """ADR-0002 supersedes ADR-0001 → target row's status flipped to 'superseded';
        ``adr_list(status='open')`` excludes the superseded target."""
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

        # Target page's status tag flipped to superseded (wiki side — unchanged).
        assert "adr-status:superseded" in (target.get("tags") or []), (
            f"target status tag not flipped: {target.get('tags')}"
        )
        # Ledger reflects the supersede: ADR-0001 status superseded.
        by_id = {r["adr_id"]: r for r in listing["adrs"]}
        assert by_id["ADR-0001"]["status"] == "superseded"


# ── 5. Concurrent ID-assignment race (per-project lock) ───────────────────────


@pytest.mark.usefixtures("_patch_ledger", "admin_backend_bypass")
class TestAdrAddConcurrentIdAssignment:
    def test_concurrent_calls_produce_distinct_ids(self, tmp_path, _ledger_stub):
        """Two simultaneous adr_add on a fresh project → ADR-0001 and ADR-0002 (no dup).
        Car F: AUTO_INCREMENT serialises IDs backend-side; the per-project lock
        orders body write → row → slug link so two callers never observe a
        partial commit."""
        import os

        from yadgar.core.server.tools.adr import adr_add, adr_list

        project_dir = str(tmp_path / "racetest")
        os.makedirs(project_dir, exist_ok=True)

        results: list[dict] = []
        errors: list[Exception] = []
        entry_barrier = threading.Barrier(2)

        params = dict(_VALID_ADR_PARAMS, directory=project_dir)

        def _call(title: str) -> None:
            try:
                try:
                    entry_barrier.wait(timeout=10)
                except threading.BrokenBarrierError:
                    pass
                # Car F: the per-project lock orders body write → ledger row
                # → slug link so two callers never observe a partial commit.
                # The stub's _next_id mirrors AUTO_INCREMENT (per-stub counter,
                # serialised by GIL on the increment), so distinct ids are
                # guaranteed without any test-side pre-allocation.
                result = adr_add(**dict(params, title=title))
                results.append(result)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        with patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=project_dir):
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
        assert len(ids) == 2, f"Ledger must hold 2 distinct ids: {ids}"
        # The ids must be sequential (AUTO_INCREMENT semantics on the stub).
        sorted_ids = sorted(ids)
        assert sorted_ids[0].endswith("0001") or sorted_ids[0].endswith("0002"), (
            f"Expected sequential ids starting at 0001, got: {sorted_ids}"
        )

    def test_different_project_roots_do_not_block_each_other(self, tmp_path, _ledger_stub):
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


@pytest.mark.usefixtures("_patch_ledger", "admin_backend_bypass")
class TestAdrWaitTimeoutResilience:
    """Car F: the body-page write (wait=True) returning ``wait_timeout`` is
    non-fatal — the row + body_slug link live in the ledger, not the wiki."""

    def test_write_ok_predicate(self):
        from yadgar.core.server.tools.adr import _write_ok

        assert _write_ok({"stored": True, "committed": True}) is True
        assert _write_ok({"stored": True, "queued": True}) is True
        # wait_timeout — still queued, converges → NOT a failure.
        assert _write_ok({"stored": False, "reason": "wait_timeout", "queued": True}) is True
        # hard terminal rejections ARE failures.
        assert _write_ok({"stored": False, "reason": "duplicate_detected"}) is False
        assert _write_ok({"stored": False, "reason": "blocked_by_policy: x"}) is False

    def test_ledger_autoincrement_assigns_distinct_ids(self, tmp_path, _ledger_stub):
        """Car F: the AUTO_INCREMENT ``id`` is the canonical id source (ADR-0197).
        Two sequential ``create_adr_row`` calls yield ADR-0001 + ADR-0002 — the
        wiki-index path's lagging-index race is gone."""
        from yadgar.core.server.tools.adr import adr_add

        project_dir = str(tmp_path / "lagproj")
        __import__("os").makedirs(project_dir, exist_ok=True)
        with patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=project_dir):
            r1 = adr_add(**dict(_VALID_ADR_PARAMS, directory=project_dir))
        assert r1.get("adr_id") == "ADR-0001"

        with patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=project_dir):
            r2 = adr_add(**dict(_VALID_ADR_PARAMS, directory=project_dir, title="Second"))
        assert r2.get("adr_id") == "ADR-0002", (
            f"AUTO_INCREMENT must yield the next id even with a lagging wiki: {r2}"
        )

    def test_adr_add_ok_when_body_write_times_out(self, tmp_path, _ledger_stub):
        """Car F: the body-page write returning ``wait_timeout`` is non-fatal —
        the row + body_slug link commit in the ledger and the page converges on
        the next drain."""
        import yadgar.core.server.tools.adr as _adr_mod
        from yadgar.core.server.tools.adr import adr_add

        project_dir = str(tmp_path / "toproj")
        __import__("os").makedirs(project_dir, exist_ok=True)

        real_canonical = _adr_mod._wiki_write_canonical

        def _canonical_with_timeout(payload, wait=False):
            # Queue the write so it converges, but report wait_timeout.
            real_canonical(payload, wait=False)
            return {"stored": False, "reason": "wait_timeout", "queued": True}

        with (
            patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=project_dir),
            patch.object(_adr_mod, "_wiki_write_canonical", _canonical_with_timeout),
        ):
            result = adr_add(**dict(_VALID_ADR_PARAMS, directory=project_dir))

        assert "error" not in result, f"body wait_timeout must NOT fail adr_add: {result}"
        assert result.get("adr_id") == "ADR-0001"
        assert result.get("slug"), f"slug set despite body wait_timeout: {result}"


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
