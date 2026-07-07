"""TDD tests for v5.49.0 Phase 1: purge_expired_archives() storage helper.

Scope:
  - Rows older than retention threshold are purged.
  - is_protected=true rows are skipped.
  - Rows tagged _anchor or anchor (legacy) are skipped.
  - Thrash-guard: rows with recent created_at are skipped even if archived_at is old.
  - migration_grace=true + future valid_until skips; past valid_until purges.
  - Circuit-breaker caps purge count at MEMORY_ARCHIVE_RETENTION_CIRCUIT_BREAKER;
    CRITICAL log fired when hit.
  - dry_run=True reports candidates without deleting.
  - MEMORY_ARCHIVE_RETENTION_DAYS=0 disables purge entirely (all-zero return).

Written BEFORE implementation — all tests start red.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from yadgar.core import server

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("archive_retention")
    server.init_engines(
        db_path=str(tmp_path / "test_archive_retention.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


@pytest.fixture()
def storage(_engines):
    from yadgar._shared.runtime.lifecycle import _get_storage

    return _get_storage()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ago(days: float) -> str:
    """ISO timestamp `days` ago."""
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _future(days: float = 30) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def _insert_archive(storage, **kw) -> int:
    """Insert a memory_archive row with flexible field set.

    Defaults:
      archived_at  — 1d ago
      created_at   — 100d ago (well outside thrash-guard window)
      is_protected — False
      tags         — []
      content      — "test archive content"
    """
    aid = storage._next_id("memory_archive")
    params: dict = {
        "id": aid,
        "content": kw.get("content", "test archive content"),
        "archived_at": kw.get("archived_at", _ago(1)),
        "created_at": kw.get("created_at", _ago(100)),
        "is_protected": kw.get("is_protected", False),
        "tags": kw.get("tags", []),
        "original_memory_id": kw.get("original_memory_id", 0),
        "mismatch_score": kw.get("mismatch_score", 0.0),
        "archive_reason": kw.get("archive_reason", "test"),
    }
    sql = (
        "CREATE type::record('memory_archive', $id) SET "
        "content = $content, archived_at = $archived_at, created_at = $created_at, "
        "is_protected = $is_protected, tags = $tags, "
        "original_memory_id = $original_memory_id, "
        "mismatch_score = $mismatch_score, archive_reason = $archive_reason"
    )
    if "migration_grace" in kw:
        params["migration_grace"] = kw["migration_grace"]
        sql += ", migration_grace = $migration_grace"
    if "valid_until" in kw:
        params["valid_until"] = kw["valid_until"]
        sql += ", valid_until = $valid_until"
    storage._q(sql, params)
    return aid


def _count_archives(storage) -> int:
    rows = storage._q("SELECT count() AS c FROM memory_archive GROUP ALL")
    return int(rows[0]["c"]) if rows else 0


# ---------------------------------------------------------------------------
# 1. Retention age filter
# ---------------------------------------------------------------------------


def test_purge_respects_retention_age(storage):
    """Insert 3 archives at ages 30d/91d/180d; default 90d → only 91d+180d removed."""
    from yadgar._shared.storage.ops import purge_expired_archives

    _insert_archive(storage, archived_at=_ago(30))  # younger — kept
    _insert_archive(storage, archived_at=_ago(91))  # older — purged
    _insert_archive(storage, archived_at=_ago(180))  # older — purged

    result = purge_expired_archives(storage)

    assert result["purged"] == 2, f"expected 2 purged, got {result}"
    assert _count_archives(storage) == 1


# ---------------------------------------------------------------------------
# 2. is_protected=true skipped
# ---------------------------------------------------------------------------


def test_purge_skips_protected(storage):
    """is_protected=true archive at 180d must NOT be purged."""
    from yadgar._shared.storage.ops import purge_expired_archives

    _insert_archive(storage, archived_at=_ago(180), is_protected=True)

    result = purge_expired_archives(storage)

    assert result["purged"] == 0
    assert result["skipped_protected"] == 1
    assert _count_archives(storage) == 1


# ---------------------------------------------------------------------------
# 3. _anchor tag skipped
# ---------------------------------------------------------------------------


def test_purge_skips_anchor_tag(storage):
    """Archive tagged _anchor at 180d must NOT be purged."""
    from yadgar._shared.storage.ops import purge_expired_archives

    _insert_archive(storage, archived_at=_ago(180), tags=["_anchor"])

    result = purge_expired_archives(storage)

    assert result["purged"] == 0
    assert result["skipped_anchor"] == 1
    assert _count_archives(storage) == 1


# ---------------------------------------------------------------------------
# 4. Legacy anchor tag (no underscore) skipped
# ---------------------------------------------------------------------------


def test_purge_skips_legacy_anchor_no_underscore(storage):
    """Archive tagged anchor (no underscore, pre-v5.8) at 180d must NOT be purged."""
    from yadgar._shared.storage.ops import purge_expired_archives

    _insert_archive(storage, archived_at=_ago(180), tags=["anchor"])

    result = purge_expired_archives(storage)

    assert result["purged"] == 0
    assert result["skipped_anchor"] == 1
    assert _count_archives(storage) == 1


# ---------------------------------------------------------------------------
# 5. Thrash-guard: recent created_at blocks purge
# ---------------------------------------------------------------------------


def test_purge_skips_recent_creation(storage):
    """archived_at=91d ago BUT created_at=3d ago → thrash-guard skip."""
    from yadgar._shared.storage.ops import purge_expired_archives

    _insert_archive(storage, archived_at=_ago(91), created_at=_ago(3))

    result = purge_expired_archives(storage)

    assert result["purged"] == 0
    assert result["skipped_recent"] == 1
    assert _count_archives(storage) == 1


# ---------------------------------------------------------------------------
# 6. migration_grace + future valid_until skips
# ---------------------------------------------------------------------------


def test_purge_skips_migration_grace(storage):
    """migration_grace=true + valid_until future → skip."""
    from yadgar._shared.storage.ops import purge_expired_archives

    _insert_archive(
        storage,
        archived_at=_ago(180),
        migration_grace=True,
        valid_until=_future(30),
    )

    result = purge_expired_archives(storage)

    assert result["purged"] == 0
    assert _count_archives(storage) == 1


# ---------------------------------------------------------------------------
# 7. migration_grace + past valid_until → purge
# ---------------------------------------------------------------------------


def test_purge_migration_grace_after_expiry(storage):
    """migration_grace=true + valid_until past → PURGED (grace expired)."""
    from yadgar._shared.storage.ops import purge_expired_archives

    _insert_archive(
        storage,
        archived_at=_ago(180),
        migration_grace=True,
        valid_until=_ago(5),
    )

    result = purge_expired_archives(storage)

    assert result["purged"] == 1
    assert _count_archives(storage) == 0


# ---------------------------------------------------------------------------
# 8. Circuit-breaker caps purge + CRITICAL log
# ---------------------------------------------------------------------------


def test_circuit_breaker_caps_purge_count(storage, caplog):
    """600 eligible archives → only 500 purged + circuit_breaker_hit=True + CRITICAL."""
    from yadgar._shared.storage.ops import purge_expired_archives

    # Batch insert 600 eligible archives
    _BATCH = 600
    records = [
        {
            "id": storage._next_id("memory_archive"),
            "content": f"archive {i}",
            "archived_at": _ago(180),
            "created_at": _ago(365),
            "is_protected": False,
            "tags": [],
            "original_memory_id": i,
            "mismatch_score": 0.0,
            "archive_reason": "bulk_test",
        }
        for i in range(_BATCH)
    ]
    _CHUNK = 100
    for start in range(0, len(records), _CHUNK):
        storage._q("INSERT INTO memory_archive $data", {"data": records[start : start + _CHUNK]})

    with caplog.at_level(logging.CRITICAL):
        result = purge_expired_archives(storage)

    assert result["circuit_breaker_hit"] is True
    assert result["purged"] == 500
    assert any(record.levelno == logging.CRITICAL for record in caplog.records), (
        "Expected CRITICAL log when circuit-breaker fires"
    )


# ---------------------------------------------------------------------------
# 9. dry_run=True reports candidates, no delete
# ---------------------------------------------------------------------------


def test_dry_run_no_delete(storage):
    """Eligible archives with dry_run=True → candidates reported, purged==0, rows intact."""
    from yadgar._shared.storage.ops import purge_expired_archives

    _insert_archive(storage, archived_at=_ago(91))
    _insert_archive(storage, archived_at=_ago(180))

    result = purge_expired_archives(storage, dry_run=True)

    assert result["purged"] == 0
    assert result["candidates"] >= 2
    assert _count_archives(storage) == 2


# ---------------------------------------------------------------------------
# 10. MEMORY_ARCHIVE_RETENTION_DAYS=0 disables purge
# ---------------------------------------------------------------------------


def test_retention_disabled(storage, monkeypatch):
    """MEMORY_ARCHIVE_RETENTION_DAYS=0 → early return, all-zero dict."""
    import yadgar._shared.config as _cfg
    from yadgar._shared.storage.ops import purge_expired_archives

    _insert_archive(storage, archived_at=_ago(365))

    # Bypass the lru_cache by injecting a fresh Settings instance with DAYS=0.
    original_get = _cfg.get_settings

    def _patched_settings():
        s = original_get()
        # Return a new instance overriding only the retention knob.
        return s.model_copy(update={"MEMORY_ARCHIVE_RETENTION_DAYS": 0})

    monkeypatch.setattr(_cfg, "get_settings", _patched_settings)

    result = purge_expired_archives(storage)

    assert result == {
        "candidates": 0,
        "purged": 0,
        "skipped_protected": 0,
        "skipped_anchor": 0,
        "skipped_recent": 0,
        "circuit_breaker_hit": False,
    }
    assert _count_archives(storage) == 1


# ---------------------------------------------------------------------------
# 11. I25 three-way config registration (Phase 2)
# ---------------------------------------------------------------------------


def test_three_config_knobs_registered_three_way():
    """All 3 MEMORY_ARCHIVE_RETENTION_* knobs appear in Settings, registry, and FIELD_META."""
    from yadgar._shared.config import Settings
    from yadgar._shared.config_registry import list_config
    from yadgar._shared.config_yaml import FIELD_META

    # Settings (Phase 1 added these)
    fields = Settings.model_fields
    assert "MEMORY_ARCHIVE_RETENTION_DAYS" in fields, (
        "MEMORY_ARCHIVE_RETENTION_DAYS missing from Settings"
    )
    assert "MEMORY_ARCHIVE_RETENTION_CIRCUIT_BREAKER" in fields, (
        "MEMORY_ARCHIVE_RETENTION_CIRCUIT_BREAKER missing from Settings"
    )
    assert "MEMORY_ARCHIVE_RETENTION_THRASH_GUARD_DAYS" in fields, (
        "MEMORY_ARCHIVE_RETENTION_THRASH_GUARD_DAYS missing from Settings"
    )

    # _REGISTRY
    registry_names = {e.name for e in list_config()}
    assert "YADGAR_MEMORY_ARCHIVE_RETENTION_DAYS" in registry_names, (
        "YADGAR_MEMORY_ARCHIVE_RETENTION_DAYS missing from _REGISTRY"
    )
    assert "YADGAR_MEMORY_ARCHIVE_RETENTION_CIRCUIT_BREAKER" in registry_names, (
        "YADGAR_MEMORY_ARCHIVE_RETENTION_CIRCUIT_BREAKER missing from _REGISTRY"
    )
    assert "YADGAR_MEMORY_ARCHIVE_RETENTION_THRASH_GUARD_DAYS" in registry_names, (
        "YADGAR_MEMORY_ARCHIVE_RETENTION_THRASH_GUARD_DAYS missing from _REGISTRY"
    )

    # FIELD_META
    assert "memory_archive_retention_days" in FIELD_META, (
        "memory_archive_retention_days missing from FIELD_META"
    )
    assert "memory_archive_retention_circuit_breaker" in FIELD_META, (
        "memory_archive_retention_circuit_breaker missing from FIELD_META"
    )
    assert "memory_archive_retention_thrash_guard_days" in FIELD_META, (
        "memory_archive_retention_thrash_guard_days missing from FIELD_META"
    )


# ---------------------------------------------------------------------------
# Helpers (metric reads)
# ---------------------------------------------------------------------------


def _counter_total(counter) -> float:
    """Sum _value across all labeled/unlabeled children of a counter."""
    # Labeled counter: children live in _metrics dict
    if hasattr(counter, "_metrics") and counter._metrics:
        return sum(c._value.get() for c in counter._metrics.values())
    # Unlabeled counter: single _value
    if hasattr(counter, "_value"):
        return counter._value.get()
    return 0.0


def _labeled_counter_value(counter, **labels) -> float:
    """Current _value for a labeled counter child (0.0 if not yet incremented)."""
    key = tuple(labels[k] for k in counter._labelnames)
    child = counter._metrics.get(key)
    return child._value.get() if child is not None else 0.0


# ---------------------------------------------------------------------------
# 12. Nightly cycle invokes purge_expired_archives
# ---------------------------------------------------------------------------


def test_nightly_cycle_invokes_purge(storage, monkeypatch):
    """_run_retention_tasks() calls purge_expired_archives when retention enabled.

    Seeds 3 eligible archives (archived 180d ago), enables retention=90d,
    drives _run_retention_tasks(), then asserts:
    - purge_expired_archives was called exactly once.
    - yadgar_archive_purged_total incremented by >=3.
    """
    import yadgar._shared.config as _cfg
    from yadgar._shared.metrics import yadgar_archive_purged_total

    # Seed eligible archives
    _insert_archive(storage, archived_at=_ago(180))
    _insert_archive(storage, archived_at=_ago(200))
    _insert_archive(storage, archived_at=_ago(365))

    # Patch settings: retention=90d (enabled)
    original_get = _cfg.get_settings

    def _patched():
        return original_get().model_copy(update={"MEMORY_ARCHIVE_RETENTION_DAYS": 90})

    monkeypatch.setattr(_cfg, "get_settings", _patched)

    # Spy on the storage method
    call_count = []
    original_method = storage.purge_expired_archives

    def _spy(dry_run=False):
        call_count.append(1)
        return original_method(dry_run=dry_run)

    monkeypatch.setattr(storage, "purge_expired_archives", _spy)

    # Build minimal consolidator with our storage
    from yadgar.core.consolidation.cleanup import _CleanupMixin  # noqa: PLC0415

    class _FakeConsolidator(_CleanupMixin):
        def __init__(self, st, settings):
            self._storage = st
            self._settings = settings

    settings = _patched()
    consolidator = _FakeConsolidator(storage, settings)

    before = _counter_total(yadgar_archive_purged_total)
    consolidator._run_retention_tasks()
    after = _counter_total(yadgar_archive_purged_total)

    assert len(call_count) == 1, f"expected purge called once, got {len(call_count)}"
    assert (after - before) >= 3, (
        f"yadgar_archive_purged_total expected +3, got delta={after - before}"
    )


# ---------------------------------------------------------------------------
# 13. Prometheus metrics emitted correctly after purge
# ---------------------------------------------------------------------------


def test_metrics_emitted(storage, monkeypatch):
    """Verify all 4 metric increments after a purge run via _run_retention_tasks().

    Seeds:
    - 2 eligible archives (purged)
    - 1 protected (skipped_protected)
    - 1 anchor-tagged (skipped_anchor)
    - 1 recently created (skipped_recent)

    Asserts counter deltas match result dict.
    """
    import yadgar._shared.config as _cfg
    from yadgar._shared.metrics import (
        yadgar_archive_purged_total,
        yadgar_archive_retention_skipped_total,
    )

    # Eligible — will be purged
    _insert_archive(storage, archived_at=_ago(180))
    _insert_archive(storage, archived_at=_ago(200))

    # Protected
    _insert_archive(storage, archived_at=_ago(180), is_protected=True)

    # Anchor-tagged
    _insert_archive(storage, archived_at=_ago(180), tags=["_anchor"])

    # Thrash-guard (archived_at old, created_at recent)
    _insert_archive(storage, archived_at=_ago(180), created_at=_ago(3))

    original_get = _cfg.get_settings

    def _patched():
        return original_get().model_copy(update={"MEMORY_ARCHIVE_RETENTION_DAYS": 90})

    monkeypatch.setattr(_cfg, "get_settings", _patched)

    from yadgar.core.consolidation.cleanup import _CleanupMixin  # noqa: PLC0415

    class _FakeConsolidator(_CleanupMixin):
        def __init__(self, st, settings):
            self._storage = st
            self._settings = settings

    settings = _patched()
    consolidator = _FakeConsolidator(storage, settings)

    before_purged = _counter_total(yadgar_archive_purged_total)
    before_protected = _labeled_counter_value(
        yadgar_archive_retention_skipped_total, reason="protected"
    )
    before_anchor = _labeled_counter_value(yadgar_archive_retention_skipped_total, reason="anchor")
    before_recent = _labeled_counter_value(yadgar_archive_retention_skipped_total, reason="recent")

    consolidator._run_retention_tasks()

    after_purged = _counter_total(yadgar_archive_purged_total)
    after_protected = _labeled_counter_value(
        yadgar_archive_retention_skipped_total, reason="protected"
    )
    after_anchor = _labeled_counter_value(yadgar_archive_retention_skipped_total, reason="anchor")
    after_recent = _labeled_counter_value(yadgar_archive_retention_skipped_total, reason="recent")

    assert (after_purged - before_purged) == 2, (
        f"purged counter: expected +2, got {after_purged - before_purged}"
    )
    assert (after_protected - before_protected) >= 1, (
        f"skipped_protected: expected >=1, got {after_protected - before_protected}"
    )
    assert (after_anchor - before_anchor) >= 1, (
        f"skipped_anchor: expected >=1, got {after_anchor - before_anchor}"
    )
    assert (after_recent - before_recent) >= 1, (
        f"skipped_recent: expected >=1, got {after_recent - before_recent}"
    )


# ---------------------------------------------------------------------------
# Phase 4 tests — archive_purge MCP tool
# ---------------------------------------------------------------------------


# 14. archive_purge() default dry_run=True
def test_archive_purge_dry_run_default(storage):
    """archive_purge() with no args: dry_run=True in result, no deletion, sample populated."""
    from yadgar.core.server.tools.admin_archive import archive_purge

    _insert_archive(storage, archived_at=_ago(180))
    _insert_archive(storage, archived_at=_ago(200))

    result = archive_purge()

    assert result["dry_run"] is True
    assert result["purged"] == 0
    assert result["candidates"] >= 2
    assert isinstance(result["sample"], list)
    assert len(result["sample"]) <= 10
    assert _count_archives(storage) == 2


# 15. archive_purge(dry_run=False) performs deletion
def test_archive_purge_explicit_run(storage):
    """archive_purge(dry_run=False): deletion occurs, purged>0, dry_run=False in result."""
    from yadgar.core.server.tools.admin_archive import archive_purge

    _insert_archive(storage, archived_at=_ago(180))
    _insert_archive(storage, archived_at=_ago(200))

    result = archive_purge(dry_run=False)

    assert result["dry_run"] is False
    assert result["purged"] >= 2
    assert _count_archives(storage) == 0


# 16. retention_days override
def test_archive_purge_retention_override(storage):
    """retention_days=40 → 45d + 91d archives purged; 30d untouched."""
    from yadgar.core.server.tools.admin_archive import archive_purge

    aid_30 = _insert_archive(storage, archived_at=_ago(30))  # kept
    _insert_archive(storage, archived_at=_ago(45))  # purged (>40d)
    _insert_archive(storage, archived_at=_ago(91))  # purged (>40d)

    result = archive_purge(dry_run=False, retention_days=40)

    assert result["purged"] == 2, f"expected 2 purged, got {result}"
    assert result["retention_days"] == 40
    # 30d archive still present
    rows = storage._q(
        "SELECT meta::id(id) AS rid FROM memory_archive WHERE meta::id(id) = $id",
        {"id": aid_30},
    )
    assert rows, "30d archive should remain after retention_days=40 override"


# 17. Power gate — tool exported from server module
def test_archive_purge_power_gated():
    """archive_purge must be accessible from the server module (power-gated pattern)."""
    from yadgar.core import server

    assert hasattr(server, "archive_purge"), (
        "archive_purge must be exported from yadgar.server (power tool registration)"
    )


# 18. Secret gate — gate_or_reject called
def test_archive_purge_secret_gated(monkeypatch):
    """gate_or_reject must be called when archive_purge is invoked."""
    import importlib

    _mod = importlib.import_module("yadgar.core.server.tools.admin_archive")

    captured_calls = []

    def fake_gate(*args, tags=None, source=None):
        captured_calls.append({"args": args, "tags": tags})
        return None  # allow through

    monkeypatch.setattr(_mod, "gate_or_reject", fake_gate)

    _mod.archive_purge()

    assert captured_calls, "gate_or_reject was never called in archive_purge()"
