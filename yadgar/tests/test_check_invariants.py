"""Tests for check_invariants enhancements — Part A (timeout) and Part B (DB-size).

TDD: these tests are written before implementation and should fail initially.
"""

from pathlib import Path

import pytest

from yadgar import server
from yadgar.server import _run_check_invariants


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    """Initialize global engines with a temp database for each test."""
    db_path = str(tmp_path / "test.db")
    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


# ── Part A: similarity-link check uses Python-side set-difference ────────────


def test_similarity_link_check_does_not_use_record_exists():
    """The MSL dangling-edge check must NOT use record::exists (O(N) per-row lookup).

    The Python-side approach fetches memory IDs into a set and does the diff
    in-memory — no subquery that SurrealDB might re-evaluate per MSL row.
    We verify this by recording all SQL issued to _q and asserting no query
    against memory_similarity_link contains 'record::exists'.
    """
    storage = server._get_storage()
    issued: list[str] = []
    original_q = storage._q

    def recording_q(surql, params=None):
        issued.append(surql)
        return original_q(surql, params)

    storage._q = recording_q
    try:
        _run_check_invariants(storage)
    finally:
        storage._q = original_q

    msl_queries = [q for q in issued if "memory_similarity_link" in q.lower()]
    for q in msl_queries:
        assert "record::exists" not in q, (
            f"memory_similarity_link query used record::exists (O(N) per-row): {q!r}"
        )


def test_similarity_link_check_uses_python_set_difference():
    """After the rewrite the MSL check should issue at most one targeted DELETE
    (when dangling rows exist) rather than a correlated subquery DELETE.

    With no data the dangling count should be 0 and no DELETE is issued.
    """
    result = _run_check_invariants(server._get_storage())
    assert "memory_similarity_link_dangling" in result["counts"]
    assert result["counts"]["memory_similarity_link_dangling"] == 0


# ── Part A: per-table timeout — partial failure continues ────────────────────


def test_check_invariants_partial_timeout_continues():
    """When the MSL check times out, the response must:
    - still contain counts for other tables (partial result, not empty)
    - have ok=False (a check failed)
    - include a 'timeouts' field listing 'memory_similarity_link'

    We stub _q to raise TimeoutError when the query touches memory_similarity_link.
    """
    storage = server._get_storage()
    original_q = storage._q

    def stubbed_q(surql, params=None):
        if "memory_similarity_link" in surql and "SELECT VALUE" not in surql:
            # Simulate a timeout on the dangling check (not the ID fetch)
            raise TimeoutError("simulated timeout")
        return original_q(surql, params)

    storage._q = stubbed_q
    try:
        result = _run_check_invariants(storage)
    finally:
        storage._q = original_q

    # Other table counts must still be present
    assert "memory" in result["counts"], "memory count missing after MSL timeout"

    # ok must be False because a check didn't complete
    assert result["ok"] is False, "ok should be False when a check timed out"

    # timeouts field must name the failing table
    assert "timeouts" in result, "timeouts field missing from result"
    assert any("memory_similarity_link" in t for t in result["timeouts"]), (
        f"expected 'memory_similarity_link' in timeouts, got: {result['timeouts']}"
    )


# ── Part B: DB-size block ────────────────────────────────────────────────────


def _make_sparse_file(path: Path, size: int) -> None:
    """Create a sparse file of *size* bytes without allocating disk space."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.seek(size - 1)
        f.write(b"\0")


def test_db_size_block_populated(tmp_path, monkeypatch):
    """check_invariants must return a db_size block with correct per-subdir sizes.

    We create a fixture DB dir with known sizes under vlog/, sstables/, wal/.
    """
    db_dir = tmp_path / "surreal_db"
    vlog_bytes = 100 * 1024 * 1024  # 100 MiB
    sstables_bytes = 30 * 1024 * 1024  # 30 MiB
    wal_bytes = 4 * 1024 * 1024  # 4 MiB

    _make_sparse_file(db_dir / "vlog" / "data.vlog", vlog_bytes)
    _make_sparse_file(db_dir / "sstables" / "data.sst", sstables_bytes)
    _make_sparse_file(db_dir / "wal" / "data.wal", wal_bytes)
    # extra file that goes into other_size_bytes
    _make_sparse_file(db_dir / "LOCK", 4096)

    from yadgar import config as _cfg

    monkeypatch.setenv("YADGAR_DB_PATH", str(db_dir))
    _cfg.get_settings.cache_clear()
    monkeypatch.setattr(_cfg.get_settings(), "DB_PATH", str(db_dir), raising=False)

    # Also patch the settings object used in server module
    from yadgar import server as _s

    monkeypatch.setattr(_s.settings, "DB_PATH", str(db_dir), raising=False)

    result = _run_check_invariants(server._get_storage())

    assert "db_size" in result, f"db_size block missing. Got keys: {list(result.keys())}"
    ds = result["db_size"]
    assert ds["vlog_size_bytes"] == vlog_bytes, (
        f"vlog_size_bytes: expected {vlog_bytes}, got {ds['vlog_size_bytes']}"
    )
    assert ds["sstables_size_bytes"] == sstables_bytes, (
        f"sstables_size_bytes: expected {sstables_bytes}, got {ds['sstables_size_bytes']}"
    )
    assert ds["wal_size_bytes"] == wal_bytes, (
        f"wal_size_bytes: expected {wal_bytes}, got {ds['wal_size_bytes']}"
    )
    total = ds["db_size_bytes"]
    assert total >= vlog_bytes + sstables_bytes + wal_bytes, (
        f"db_size_bytes {total} < sum of known subdirs"
    )
    assert "vlog_pct_of_total" in ds
    assert "size_warning" in ds
    assert isinstance(ds["size_warning"], bool)


def test_db_size_warning_false_below_threshold(tmp_path, monkeypatch):
    """size_warning must be False when total is below DB_SIZE_WARNING_BYTES."""
    db_dir = tmp_path / "surreal_db"
    _make_sparse_file(db_dir / "vlog" / "x", 1024)  # tiny

    from yadgar import config as _cfg
    from yadgar import server as _s

    monkeypatch.setenv("YADGAR_DB_PATH", str(db_dir))
    _cfg.get_settings.cache_clear()
    monkeypatch.setattr(_s.settings, "DB_PATH", str(db_dir), raising=False)

    result = _run_check_invariants(server._get_storage())
    assert result["db_size"]["size_warning"] is False


def test_db_size_warning_true_above_threshold(tmp_path, monkeypatch):
    """size_warning must be True when total exceeds DB_SIZE_WARNING_BYTES (1 GiB)."""
    db_dir = tmp_path / "surreal_db"
    # Create a file > 1 GiB via sparse file (no actual disk usage)
    _make_sparse_file(db_dir / "vlog" / "big.vlog", 2 * 1024 * 1024 * 1024)  # 2 GiB

    from yadgar import config as _cfg
    from yadgar import server as _s

    monkeypatch.setenv("YADGAR_DB_PATH", str(db_dir))
    _cfg.get_settings.cache_clear()
    monkeypatch.setattr(_s.settings, "DB_PATH", str(db_dir), raising=False)

    result = _run_check_invariants(server._get_storage())
    assert result["db_size"]["size_warning"] is True


def test_db_size_warning_logged_once_per_hour(tmp_path, monkeypatch, caplog):
    """When size_warning is True, WARN should be logged only once per hour across
    multiple check_invariants calls.

    We call _run_check_invariants twice in quick succession and assert the warning
    appears at most once.
    """
    import logging

    db_dir = tmp_path / "surreal_db"
    _make_sparse_file(db_dir / "vlog" / "big.vlog", 2 * 1024 * 1024 * 1024)

    from yadgar import config as _cfg
    from yadgar import server as _s

    monkeypatch.setenv("YADGAR_DB_PATH", str(db_dir))
    _cfg.get_settings.cache_clear()
    monkeypatch.setattr(_s.settings, "DB_PATH", str(db_dir), raising=False)

    # Reset the throttle state so this test starts clean
    import yadgar.server._state as _st

    monkeypatch.setattr(_st, "_db_size_warn_last_logged_hour", -1)

    storage = server._get_storage()
    with caplog.at_level(logging.WARNING, logger="yadgar.server"):
        _run_check_invariants(storage)
        _run_check_invariants(storage)

    warn_lines = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "db_size" in r.getMessage().lower()
    ]
    assert len(warn_lines) == 1, (
        f"Expected exactly 1 DB size WARN per hour, got {len(warn_lines)}: "
        + "\n".join(r.getMessage() for r in warn_lines)
    )


# ── Part B: memory_stats surfaces db_size ───────────────────────────────────


def test_memory_stats_includes_db_size(tmp_path, monkeypatch):
    """memory_stats() should include the db_size block from check_invariants."""
    db_dir = tmp_path / "surreal_db"
    _make_sparse_file(db_dir / "vlog" / "x.vlog", 50 * 1024 * 1024)

    from yadgar import server as _s

    monkeypatch.setattr(_s.settings, "DB_PATH", str(db_dir), raising=False)

    stats = server.memory_stats()
    assert "db_size" in stats, f"db_size missing from memory_stats. Got: {list(stats.keys())}"
    assert "db_size_bytes" in stats["db_size"]
