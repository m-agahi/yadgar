"""Tests for yadgar/cli/stats.py — stats command.

Coverage targets:
- cmd_stats HTTP path: daemon running → print summary (table + JSON)
- cmd_stats HTTP path: daemon not running → fallback (mocked to avoid Surreal init)
- _one helper (internal)
- URL scheme validation (§8)

Note: The direct DB path (lines 57-390) requires a live SurrealDB connection
and is excluded here. Coverage floor: ~25% (HTTP path + helpers only).
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _make_args(**kwargs):
    defaults = {"project": None, "format": "table", "db_path": None}
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ── HTTP path — daemon running ─────────────────────────────────────────────────


def test_cmd_stats_http_table_format(capsys):
    from yadgar.core.cli.stats import cmd_stats

    mock_data = {
        "total_memories": 42,
        "active_count": 30,
        "archived_count": 5,
        "stale_count": 7,
        "avg_heat": 0.5123,
        "last_consolidation": "2026-06-09T10:00:00",
    }

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(mock_data).encode()

    with patch("urllib.request.urlopen", return_value=mock_resp):
        cmd_stats(_make_args())

    captured = capsys.readouterr()
    assert "42" in captured.out
    assert "Yadgar Stats" in captured.out
    mock_resp.close.assert_called_once()


def test_try_http_path_closes_http_error():
    """HTTPError is a response object holding a file wrapper (a
    tempfile._TemporaryFileWrapper via addbase on py3.14); an unclosed instance
    fires a spurious ResourceWarning at GC that pytest-xdist mis-attributes to
    an unrelated test. _try_http_path must close it deterministically."""
    import urllib.error

    from yadgar.core.cli.stats import _try_http_path

    http_err = urllib.error.HTTPError(url="", code=500, msg="Error", hdrs={}, fp=None)
    with patch("urllib.request.urlopen", side_effect=http_err):
        result = _try_http_path(_make_args())
    assert result is False
    assert http_err.fp is None or http_err.fp.closed, "the hook must close the caught HTTPError"


def test_cmd_stats_http_json_format(capsys):
    from yadgar.core.cli.stats import cmd_stats

    mock_data = {"total_memories": 10}
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(mock_data).encode()

    with patch("urllib.request.urlopen", return_value=mock_resp):
        cmd_stats(_make_args(format="json"))

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["total_memories"] == 10


def test_cmd_stats_http_with_project(capsys):
    from yadgar.core.cli.stats import cmd_stats

    mock_data = {"total_memories": 5, "active_count": 4, "avg_heat": 0.8}
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(mock_data).encode()
    captured_urls = []

    def mock_urlopen(req, timeout=None):
        captured_urls.append(req.full_url if hasattr(req, "full_url") else str(req))
        return mock_resp

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        cmd_stats(_make_args(project="/myproject"))

    assert len(captured_urls) == 1
    assert "project" in captured_urls[0]


def test_cmd_stats_http_daemon_not_running_falls_through(monkeypatch):
    """When HTTP fails, falls back to surrealdb import."""
    from yadgar.core.cli.stats import cmd_stats

    # Make HTTP fail
    monkeypatch.setattr("urllib.request.urlopen", MagicMock(side_effect=OSError("refused")))

    # Mock surrealdb as not importable → sys.exit(1)
    with patch.dict(sys.modules, {"surrealdb": None}):
        with pytest.raises(SystemExit) as exc_info:
            cmd_stats(_make_args())
    assert exc_info.value.code == 1


def test_run_db_path_locked_datastore_gets_actionable_message(monkeypatch, capsys):
    """Car 5 item 3: the observed fresh-install failure was

        Failed to query database: Failed to create datastore: ... IO error:
        kind=unexpected end of file, message=failed to fill whole buffer

    — the host CLI opening the SurrealKV file directly while a running
    container holds it. That raw datastore error must NOT reach the user;
    _run_db_path must recognise this failure signature and print a clear,
    actionable message instead (still exits 1 — this is still a failure,
    just not an unexplained one)."""
    from yadgar.core.cli.stats import cmd_stats

    monkeypatch.setattr("urllib.request.urlopen", MagicMock(side_effect=OSError("refused")))

    class _LockedSurreal:
        def __init__(self, *a, **k):
            raise RuntimeError(
                "Failed to create datastore: Failed to load index: IO error: "
                "kind=unexpected end of file, message=failed to fill whole buffer"
            )

    fake_module = MagicMock()
    fake_module.Surreal = _LockedSurreal
    with patch.dict(sys.modules, {"surrealdb": fake_module}):
        with pytest.raises(SystemExit) as exc_info:
            cmd_stats(_make_args())

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "unexpected end of file" not in captured.err
    assert "failed to fill whole buffer" not in captured.err
    # Must name the actual problem (another process holding the DB) rather
    # than surface the raw driver error verbatim.
    assert (
        "another process" in captured.err or "container" in captured.err or "daemon" in captured.err
    )


def test_cmd_stats_http_avg_heat_displayed(capsys):
    from yadgar.core.cli.stats import cmd_stats

    mock_data = {
        "total_memories": 3,
        "active_count": 2,
        "archived_count": 1,
        "stale_count": 0,
        "avg_heat": 0.6789,
    }
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(mock_data).encode()

    with patch("urllib.request.urlopen", return_value=mock_resp):
        cmd_stats(_make_args())

    captured = capsys.readouterr()
    assert "0.6789" in captured.out


def test_cmd_stats_project_in_header(capsys):
    from yadgar.core.cli.stats import cmd_stats

    mock_data = {"total_memories": 1}
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(mock_data).encode()

    with patch("urllib.request.urlopen", return_value=mock_resp):
        cmd_stats(_make_args(project="/my/project"))

    captured = capsys.readouterr()
    assert "/my/project" in captured.out


# ── Split-container install guard (Car K, 2026-08-14 train) ──────────────────


def test_is_split_container_install_true_for_remote_db_url(monkeypatch):
    """Car K: when YADGAR_DB_URL points at a non-loopback host (the
    container-internal hostname `yadgar-backend`), the CLI must detect the
    split-container install and refuse the embedded path."""
    from yadgar.core.cli.stats import _is_split_container_install

    monkeypatch.setenv("YADGAR_DB_URL", "http://yadgar-backend:8000")
    assert _is_split_container_install() is True


def test_is_split_container_install_false_for_loopback_db_url(monkeypatch):
    """Loopback DB URL (the default) means a co-located install — the
    embedded SurrealKV path is fine, no split-container detection."""
    from yadgar.core.cli.stats import _is_split_container_install

    monkeypatch.setenv("YADGAR_DB_URL", "http://127.0.0.1:8000")
    assert _is_split_container_install() is False


def test_is_split_container_install_false_when_unset(monkeypatch):
    """No YADGAR_DB_URL set = use default (loopback) = local install,
    not split-container. _is_split_container_install must be False so
    the existing flow runs."""
    from yadgar.core.cli.stats import _is_split_container_install

    monkeypatch.delenv("YADGAR_DB_URL", raising=False)
    assert _is_split_container_install() is False


def test_cmd_stats_split_container_exits_with_actionable_message(monkeypatch, capsys):
    """End-to-end: with YADGAR_DB_URL pointing at a container hostname,
    cmd_stats must exit 1 WITHOUT attempting the embedded SurrealKV
    open (which would surface a raw driver error) and WITHOUT touching
    the HTTP path (which has no /api/stats yet). The stderr must name
    the actual fix."""
    from yadgar.core.cli.stats import cmd_stats

    monkeypatch.setenv("YADGAR_DB_URL", "http://yadgar-backend:8000")

    # SurrealKV open is the failure mode the guard prevents — if the
    # guard fails to fire, this raises a MagicMock AttributeError,
    # which is a different (and worse) failure than the SystemExit
    # we assert here.
    class _ShouldNotOpen:
        def __init__(self, *a, **k):
            raise AssertionError(
                "split-container guard failed: cmd_stats reached the "
                "embedded SurrealKV path despite YADGAR_DB_URL pointing "
                "at a container hostname"
            )

    fake_module = MagicMock()
    fake_module.Surreal = _ShouldNotOpen
    monkeypatch.setattr(
        "urllib.request.urlopen", MagicMock(side_effect=AssertionError("HTTP must not be tried"))
    )

    with patch.dict(sys.modules, {"surrealdb": fake_module}):
        with pytest.raises(SystemExit) as exc_info:
            cmd_stats(_make_args())

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "split-container" in captured.err or "split container" in captured.err
    # Must point at the actual fix (podman exec / curl /api/stats).
    assert "podman exec yadgar-backend yadgar stats" in captured.err
    assert "/api/stats" in captured.err
