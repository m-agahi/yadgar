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

from yadgar import server

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    server.init_engines(
        db_path=str(tmp_path / "test_archive_retention.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


@pytest.fixture()
def storage(_engines):
    from yadgar.server.lifecycle import _get_storage

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
    from yadgar.storage.ops import purge_expired_archives

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
    from yadgar.storage.ops import purge_expired_archives

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
    from yadgar.storage.ops import purge_expired_archives

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
    from yadgar.storage.ops import purge_expired_archives

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
    from yadgar.storage.ops import purge_expired_archives

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
    from yadgar.storage.ops import purge_expired_archives

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
    from yadgar.storage.ops import purge_expired_archives

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
    from yadgar.storage.ops import purge_expired_archives

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
    from yadgar.storage.ops import purge_expired_archives

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
    import yadgar.config as _cfg
    from yadgar.storage.ops import purge_expired_archives

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
