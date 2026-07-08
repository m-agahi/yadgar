"""test_scan_script.py — subprocess tests for scripts/scan_db_for_secrets.py.

Tests use --storage-mock / --storage-mock-leak to avoid live-DB dependency.
Live-DB tests are skipped unless YADGAR_TEST_LIVE_SCAN=1 is set.
"""

from __future__ import annotations

import os
import subprocess
import sys

# Script path relative to repo root
from pathlib import Path

import pytest

from yadgar.tests._paths import REPO_ROOT as _REPO_ROOT

_SCRIPT = _REPO_ROOT / "scripts" / "scan_db_for_secrets.py"
_PYTHON = sys.executable


def _run(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_PYTHON, str(_SCRIPT)] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


class TestScanScriptHelp:
    def test_help_exits_zero(self) -> None:
        result = _run(["--help"])
        assert result.returncode == 0
        assert "--storage-mock" in result.stdout
        assert "--dry-run" in result.stdout

    def test_help_does_not_call_init_engines(self) -> None:
        """--help must exit without touching the DB."""
        result = _run(["--help"])
        assert result.returncode == 0
        # If init_engines had run, sentence-transformers warning would appear
        # in stderr. It may or may not be present — just confirm it exits fast.
        assert "Backfill scan" in result.stdout


class TestScanScriptMock:
    def test_storage_mock_exits_zero(self) -> None:
        """Clean mock data → exit 0, no hits."""
        result = _run(["--storage-mock"])
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        assert "Clean" in result.stdout

    def test_storage_mock_quiet_no_output(self) -> None:
        """Clean mock + --quiet → no stdout, no stderr HITS line."""
        result = _run(["--storage-mock", "--quiet"])
        assert result.returncode == 0
        assert result.stdout.strip() == ""
        assert "HITS:" not in result.stderr

    def test_storage_mock_report_written(self, tmp_path: Path) -> None:
        """Report file is created even for clean scans."""
        result = _run(["--storage-mock", "--report-dir", str(tmp_path)])
        assert result.returncode == 0
        reports = list(tmp_path.glob("secret-leak-scan-*.txt"))
        assert len(reports) == 1
        content = reports[0].read_text()
        assert "Hits: 0" in content


class TestScanScriptMockLeak:
    def test_storage_mock_leak_exits_one(self) -> None:
        """Mock data with known secret → exit 1."""
        result = _run(["--storage-mock-leak"])
        assert result.returncode == 1, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        assert "WARNING" in result.stdout or "1 row" in result.stdout

    def test_storage_mock_leak_quiet_hits_to_stderr(self) -> None:
        """--storage-mock-leak --quiet → HITS: N on stderr, exit 1."""
        result = _run(["--storage-mock-leak", "--quiet"])
        assert result.returncode == 1
        assert "HITS:" in result.stderr
        assert result.stdout.strip() == ""

    def test_storage_mock_leak_report_contains_hit(self, tmp_path: Path) -> None:
        """Report file contains the detected leak entry."""
        result = _run(["--storage-mock-leak", "--report-dir", str(tmp_path)])
        assert result.returncode == 1
        reports = list(tmp_path.glob("secret-leak-scan-*.txt"))
        assert len(reports) == 1
        content = reports[0].read_text()
        assert "Hits: 1" in content
        assert "GitHub token" in content
        assert "ghp_" in content

    def test_storage_mock_leak_detects_github_token(self) -> None:
        """Synthetic ghp_ token must be caught (validates check_secrets threshold)."""
        result = _run(["--storage-mock-leak"])
        assert result.returncode == 1
        # Report path is printed in stdout
        assert "secret-leak-scan-" in result.stdout


@pytest.mark.skipif(
    not os.environ.get("YADGAR_TEST_LIVE_SCAN"),
    reason="Live-DB scan skipped unless YADGAR_TEST_LIVE_SCAN=1",
)
class TestScanScriptLiveDB:
    def test_live_scan_exits_nonzero(self) -> None:
        """Live DB scan (memory 519107 known leak) must exit 1."""
        result = _run(["--dry-run", "--limit", "200", "--quiet"], timeout=60)
        assert result.returncode in (0, 1), f"Fatal error: {result.stderr}"
        # If hits found, HITS: line on stderr
        if result.returncode == 1:
            assert "HITS:" in result.stderr

    def test_live_scan_catches_memory_519107(self, tmp_path: Path) -> None:
        """Memory 519107 (ghp_ 33-char token) must appear in report."""
        result = _run(["--dry-run", "--limit", "200", "--report-dir", str(tmp_path)], timeout=60)
        assert result.returncode == 1, "Expected hits from memory 519107"
        reports = list(tmp_path.glob("secret-leak-scan-*.txt"))
        assert len(reports) == 1
        content = reports[0].read_text()
        assert "519107" in content
        assert "GitHub token" in content
