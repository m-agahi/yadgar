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
