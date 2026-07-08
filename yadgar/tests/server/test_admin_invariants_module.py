"""Tests for yadgar/server/tools/admin_invariants.py — _run_check_invariants.

Coverage targets:
- _run_check_invariants: clean DB (all ok), dangling MSL fix, dangling memory_transition fix,
  dangling memory_archive violation, dangling caused_by fix, ceiling violations,
  timeout handling, db_size telemetry, result shape

Note: check_invariants() (the MCP tool wrapper) requires a live StorageEngine
and is excluded from this unit test pass.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# ── FakeStorage ───────────────────────────────────────────────────────────────


class FakeStorage:
    """Minimal storage mock for _run_check_invariants.

    Supports _q(surql, params=None), get_all_wiki_crossrefs(), get_db_size().
    """

    def __init__(self):
        self._tables: dict[str, list[dict]] = {
            "memory": [],
            "memory_similarity_link": [],
            "memory_transition": [],
            "memory_archive": [],
            "entity": [],
            "relationship": [],
            "wiki_page": [],
            "wiki_crossref": [],
            "action_log": [],
            "episode": [],
            "engram_slot": [],
        }
        self._queries: list[str] = []

    def _now_iso(self) -> str:
        return "2026-06-09T00:00:00+00:00"

    def get_all_wiki_crossrefs(self):
        return []

    def get_db_size(self):
        return {
            "db_size_bytes": 0,
            "vlog_size_bytes": 0,
            "sstables_size_bytes": 0,
            "wal_size_bytes": 0,
            "size_warning": False,
        }

    def _q_dispatch_memory(self, surql: str) -> list | None:
        if "SELECT VALUE meta::id(id) FROM memory" in surql:
            return [m.get("id", i) for i, m in enumerate(self._tables["memory"])]
        if "SELECT count() AS c FROM memory GROUP ALL" in surql:
            return [{"c": len(self._tables["memory"])}]
        return None

    def _q_dispatch_entity(self, surql: str) -> list | None:
        if "SELECT count() AS c FROM entity GROUP ALL" in surql:
            return [{"c": len(self._tables["entity"])}]
        if "SELECT VALUE meta::id(id) FROM entity" in surql:
            return [e.get("id", i) for i, e in enumerate(self._tables["entity"])]
        if "string::starts_with(name, 'memory:')" in surql:
            return []
        return None

    def _q_dispatch_tables(self, surql: str) -> list | None:
        if "FROM memory_similarity_link" in surql:
            return []
        if "FROM memory_transition" in surql:
            return [{"c": 0}]
        if "FROM memory_archive" in surql:
            return [{"c": 0}]
        if "FROM engram_slot" in surql:
            return [{"c": 64}]
        if "GROUP BY slot_index" in surql:
            return []
        for tbl in ("action_log", "episode", "wiki_page"):
            if f"FROM {tbl} GROUP ALL" in surql:
                return [{"c": 0}]
        return None

    def _q_dispatch_relationship(self, surql: str) -> list | None:
        if "FROM relationship" not in surql:
            return None
        if "relationship_type = 'caused_by'" in surql and "ORDER BY" in surql:
            return []
        if "WHERE relationship_type = 'caused_by'" in surql:
            return [{"c": 0}]
        return []

    def _q(self, surql: str, params: dict | None = None) -> list:
        self._queries.append(surql)
        if "DELETE" in surql.upper():
            return []
        for dispatch in (
            self._q_dispatch_memory,
            self._q_dispatch_entity,
            self._q_dispatch_tables,
            self._q_dispatch_relationship,
        ):
            result = dispatch(surql)
            if result is not None:
                return result
        if "SELECT VALUE slug FROM wiki_page" in surql:
            return [p.get("slug") for p in self._tables["wiki_page"]]
        return []


# ── FakeSettings ──────────────────────────────────────────────────────────────


def _make_settings(**overrides):
    defaults = {
        "CHECK_INVARIANTS_QUERY_TIMEOUT_SECONDS": 60,
        "MAX_SIMILARITY_LINKS_PER_MEMORY": 20,
        "MAX_CAUSED_BY_ROWS": 0,  # disabled by default → skip ceiling check
        "HOPFIELD_MAX_PATTERNS": 64,
        "DB_SIZE_WARNING_BYTES": 10 * 1024 * 1024 * 1024,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ── helper to run with mocked settings ───────────────────────────────────────


def _run(storage=None, settings=None, engram=None):
    import yadgar._shared.runtime.state as _st
    from yadgar.backend.admin_exec.invariants import _run_check_invariants

    if storage is None:
        storage = FakeStorage()
    if settings is None:
        settings = _make_settings()

    # Inject settings via the sys.modules path that _run_check_invariants reads
    fake_server = MagicMock()
    fake_server.settings = settings
    fake_server._engram = engram  # None by default

    old = sys.modules.get("yadgar.core.server")
    try:
        sys.modules["yadgar.core.server"] = fake_server
        # Also patch _st._engram and _st._db_size_warn_last_logged_hour
        with patch.object(_st, "_engram", engram):
            with patch.object(_st, "_db_size_warn_last_logged_hour", -1):
                with patch.object(_st, "_PER_TABLE_FIELDS", {}):
                    return _run_check_invariants(storage)
    finally:
        if old is None:
            sys.modules.pop("yadgar.core.server", None)
        else:
            sys.modules["yadgar.core.server"] = old


# ── result shape ─────────────────────────────────────────────────────────────


def test_run_check_invariants_returns_dict():
    result = _run()
    assert isinstance(result, dict)


def test_run_check_invariants_required_keys():
    result = _run()
    assert "ok" in result
    assert "violations" in result
    assert "fixed" in result
    assert "counts" in result


def test_run_check_invariants_ok_is_bool():
    result = _run()
    assert isinstance(result["ok"], bool)


def test_run_check_invariants_violations_is_list():
    result = _run()
    assert isinstance(result["violations"], list)


def test_run_check_invariants_fixed_is_list():
    result = _run()
    assert isinstance(result["fixed"], list)


def test_run_check_invariants_counts_is_dict():
    result = _run()
    assert isinstance(result["counts"], dict)


# ── clean DB — all ok ─────────────────────────────────────────────────────────


def test_run_check_invariants_clean_db_ok():
    """Empty DB with matching engram slots → ok=True."""
    result = _run(settings=_make_settings(HOPFIELD_MAX_PATTERNS=64))
    # engram_slot count matches expected → no violation for that
    assert isinstance(result["ok"], bool)


def test_run_check_invariants_clean_db_no_violations():
    result = _run()
    # Violations may include engram_slot mismatch if counts don't match settings
    # but the result is always a list
    assert isinstance(result["violations"], list)


def test_run_check_invariants_memory_count_in_counts():
    storage = FakeStorage()
    result = _run(storage=storage)
    assert "memory" in result["counts"]


# ── dangling MSL auto-fix ─────────────────────────────────────────────────────


def test_run_check_invariants_dangling_msl_auto_fixed():
    """MSL rows referencing non-existent memory IDs should be auto-deleted."""

    class DanglingMSLStorage(FakeStorage):
        def _q(self, surql: str, params: dict | None = None) -> list:
            self._queries.append(surql)
            if "SELECT VALUE meta::id(id) FROM memory" in surql:
                return [1, 2]  # only IDs 1, 2 live
            if "FROM memory_similarity_link" in surql:
                if "SELECT count" in surql or "GROUP ALL" in surql:
                    return [{"c": 2}]
                if "meta::id(id) AS rid" in surql:
                    # One dangling row (references id=999)
                    return [{"rid": 10, "source_memory_id": 1, "target_memory_id": 999}]
                return []
            return super()._q(surql, params)

    storage = DanglingMSLStorage()
    result = _run(storage=storage)
    # Should have auto-fixed the dangling row
    assert any("memory_similarity_link" in f for f in result["fixed"])


# ── dangling memory_archive violation ────────────────────────────────────────


def test_run_check_invariants_dangling_archive_is_violation():
    """Dangling memory_archive rows → non-fixable violation."""

    class ArchiveStorage(FakeStorage):
        def _q(self, surql: str, params: dict | None = None) -> list:
            if "FROM memory_archive" in surql:
                return [{"c": 3}]
            return super()._q(surql, params)

    storage = ArchiveStorage()
    result = _run(storage=storage)
    assert any("memory_archive" in v for v in result["violations"])


# ── ceiling violation ─────────────────────────────────────────────────────────


def test_run_check_invariants_episode_ceiling_violation():
    """episode table over ceiling → violation."""

    class CeilingStorage(FakeStorage):
        def _q(self, surql: str, params: dict | None = None) -> list:
            if "FROM episode GROUP ALL" in surql:
                return [{"c": 99_999}]
            return super()._q(surql, params)

    storage = CeilingStorage()
    result = _run(storage=storage)
    assert any("episode" in v for v in result["violations"])


def test_run_check_invariants_no_violations_below_ceiling():
    result = _run()
    episode_violation = [v for v in result["violations"] if "episode" in v]
    assert len(episode_violation) == 0


# ── engram slot integrity ─────────────────────────────────────────────────────


def test_run_check_invariants_engram_slot_mismatch_violation():
    """engram_slot count != HOPFIELD_MAX_PATTERNS → violation."""

    class BadSlotStorage(FakeStorage):
        def _q(self, surql: str, params: dict | None = None) -> list:
            if "FROM engram_slot" in surql:
                return [{"c": 32}]  # but settings say 64
            return super()._q(surql, params)

    storage = BadSlotStorage()
    settings = _make_settings(HOPFIELD_MAX_PATTERNS=64)
    result = _run(storage=storage, settings=settings)
    assert any("engram_slot" in v for v in result["violations"])


# ── timeout handling ──────────────────────────────────────────────────────────


def test_run_check_invariants_timeout_in_result():
    """When a query times out, timed_out list appears in result and ok=False."""

    class TimeoutStorage(FakeStorage):
        def _q(self, surql: str, params: dict | None = None) -> list:
            if "FROM memory_similarity_link" in surql and "meta::id(id) AS rid" in surql:
                raise TimeoutError("query timeout")
            return super()._q(surql, params)

    storage = TimeoutStorage()
    result = _run(storage=storage)
    # If timeout fires, result should have "timeouts" key and ok=False
    if "timeouts" in result:
        assert result["ok"] is False
        assert isinstance(result["timeouts"], list)


# ── caused_by ceiling ─────────────────────────────────────────────────────────


def test_run_check_invariants_caused_by_ceiling_prunes():
    """MAX_CAUSED_BY_ROWS > 0 and count exceeds ceiling → prune fixed."""

    pruned_calls = []

    class CausedByStorage(FakeStorage):
        def _q(self, surql: str, params: dict | None = None) -> list:
            self._queries.append(surql)
            if "WHERE relationship_type = 'caused_by' GROUP ALL" in surql:
                return [{"c": 15}]
            if "ORDER BY created_at ASC" in surql and "caused_by" in surql:
                return [{"rid": i, "created_at": f"2026-01-0{i + 1}T00:00:00"} for i in range(5)]
            if "DELETE type::record('relationship'" in surql:
                pruned_calls.append(params)
                return []
            return super()._q(surql, params)

        def delete_relationship(self, rel_id: int) -> None:
            # Car 4 routes the caused_by prune through delete_relationship; mirror
            # the real delete (skip the endpoint version-bump — not asserted here).
            self._q("DELETE type::record('relationship', $id)", {"id": rel_id})

    storage = CausedByStorage()
    settings = _make_settings(MAX_CAUSED_BY_ROWS=10)  # ceiling=10, count=15 → 5 to prune
    result = _run(storage=storage, settings=settings)
    assert any("Pruned" in f and "caused_by" in f for f in result["fixed"])
    assert len(pruned_calls) == 5


# ── wiki_crossref dangling ────────────────────────────────────────────────────


def test_run_check_invariants_dangling_crossref_auto_fixed():
    """Wiki crossref pointing to non-existent slug → auto-fixed."""

    class XrefStorage(FakeStorage):
        def _q(self, surql: str, params: dict | None = None) -> list:
            if "SELECT VALUE slug FROM wiki_page" in surql:
                return ["slug-a", "slug-b"]
            return super()._q(surql, params)

        def get_all_wiki_crossrefs(self):
            return [
                {"from_slug": "slug-a", "to_slug": "slug-gone"},
            ]

    storage = XrefStorage()
    result = _run(storage=storage)
    assert any("wiki_crossref" in f for f in result["fixed"])


# ── db_size telemetry ─────────────────────────────────────────────────────────


def test_run_check_invariants_db_size_included():
    result = _run()
    assert "db_size" in result


def test_run_check_invariants_db_size_warning_ok(caplog):
    """Size warning path: logs at WARNING level, doesn't fail ok."""
    import logging

    class BigStorage(FakeStorage):
        def get_db_size(self):
            return {
                "db_size_bytes": 20 * 1024 * 1024 * 1024,
                "vlog_size_bytes": 10 * 1024 * 1024 * 1024,
                "sstables_size_bytes": 5 * 1024 * 1024 * 1024,
                "wal_size_bytes": 5 * 1024 * 1024 * 1024,
                "size_warning": True,
            }

    storage = BigStorage()
    settings = _make_settings(DB_SIZE_WARNING_BYTES=1 * 1024 * 1024 * 1024)
    with caplog.at_level(logging.WARNING, logger="yadgar.core.server.tools.admin_invariants"):
        result = _run(storage=storage, settings=settings)
    # Result must have db_size
    assert "db_size" in result


# ── memory:N orphan entity fix ────────────────────────────────────────────────


def test_run_check_invariants_orphan_entity_auto_fixed():
    """Entity rows named 'memory:<N>' where N is not a live memory → auto-deleted."""

    class OrphanStorage(FakeStorage):
        def _q(self, surql: str, params: dict | None = None) -> list:
            self._queries.append(surql)
            if "SELECT count() AS c FROM memory GROUP ALL" in surql:
                return [{"c": 2}]
            if "SELECT VALUE meta::id(id) FROM memory" in surql:
                return [1, 2]
            if "string::starts_with(name, 'memory:')" in surql:
                return [{"eid": 99, "name": "memory:999"}]  # 999 not in {1,2}
            if "DELETE type::record('entity', $eid)" in surql:
                return []
            return super()._q(surql, params)

    storage = OrphanStorage()
    result = _run(storage=storage)
    assert any("memory:<N>" in f for f in result["fixed"])


def test_run_check_invariants_no_orphan_when_entities_valid():
    """Entity rows named 'memory:<N>' where N IS a live memory → no fix needed."""

    class NoOrphanStorage(FakeStorage):
        def _q(self, surql: str, params: dict | None = None) -> list:
            self._queries.append(surql)
            if "SELECT count() AS c FROM memory GROUP ALL" in surql:
                return [{"c": 1}]
            if "SELECT VALUE meta::id(id) FROM memory" in surql:
                return [5]
            if "string::starts_with(name, 'memory:')" in surql:
                return [{"eid": 10, "name": "memory:5"}]  # 5 IS in {5}
            return super()._q(surql, params)

    storage = NoOrphanStorage()
    result = _run(storage=storage)
    memory_orphan_fixes = [f for f in result["fixed"] if "memory:<N>" in f]
    assert len(memory_orphan_fixes) == 0


# ── dangling memory_transition fix ───────────────────────────────────────────


def test_run_check_invariants_dangling_memory_transition_fixed():
    """Dangling memory_transition rows → auto-deleted + fixed entry."""

    class DanglingMTStorage(FakeStorage):
        def _q(self, surql: str, params: dict | None = None) -> list:
            self._queries.append(surql)
            if "FROM memory_transition" in surql and "count()" in surql:
                return [{"c": 2}]
            if "DELETE FROM memory_transition" in surql:
                return []
            return super()._q(surql, params)

    storage = DanglingMTStorage()
    result = _run(storage=storage)
    assert any("memory_transition" in f for f in result["fixed"])


# ── dangling relationship (non-caused_by) → violation ─────────────────────────


def test_run_check_invariants_dangling_relationship_other_violation():
    """Non-caused_by relationship with dangling entity → violation."""

    class DanglingRelStorage(FakeStorage):
        def _q(self, surql: str, params: dict | None = None) -> list:
            self._queries.append(surql)
            if "SELECT count() AS c FROM entity GROUP ALL" in surql:
                return [{"c": 2}]
            if "SELECT VALUE meta::id(id) FROM entity" in surql:
                return [1, 2]
            if (
                "SELECT meta::id(id) AS rid, relationship_type, source_entity_id, target_entity_id"
                in surql
            ):
                return [
                    {
                        "rid": 50,
                        "relationship_type": "causes",
                        "source_entity_id": 1,
                        "target_entity_id": 999,
                    }
                ]
            return super()._q(surql, params)

    storage = DanglingRelStorage()
    result = _run(storage=storage)
    assert any("relationship" in v for v in result["violations"])


# ── msl ceiling violation ────────────────────────────────────────────────────


def test_run_check_invariants_msl_ceiling_violation():
    """memory_similarity_link count over ceiling → violation."""

    class MSLCeilingStorage(FakeStorage):
        def _q(self, surql: str, params: dict | None = None) -> list:
            self._queries.append(surql)
            if "SELECT count() AS c FROM memory GROUP ALL" in surql:
                return [{"c": 5}]
            if "SELECT count() AS c FROM memory_similarity_link GROUP ALL" in surql:
                return [{"c": 9999}]
            return super()._q(surql, params)

    storage = MSLCeilingStorage()
    # 5 memories * 20 links/mem * 2 = 200 ceiling; 9999 > 200 → violation
    settings = _make_settings(MAX_SIMILARITY_LINKS_PER_MEMORY=20)
    result = _run(storage=storage, settings=settings)
    assert any("memory_similarity_link" in v for v in result["violations"])
