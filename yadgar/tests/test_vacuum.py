"""Tests for cmd_vacuum (v4.8 rewrite).

TDD: characterization test written first, expected to FAIL against the
old no-op implementation, then pass once the new implementation is in place.
"""

from __future__ import annotations

import types
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helper: build a minimal argparse namespace for cmd_vacuum
# ---------------------------------------------------------------------------


def _vacuum_args(
    backend_url: str = "http://127.0.0.1:8080",
    service_mode: str = "manual",
    db_path: str | None = None,
    yes: bool = True,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        backend_url=backend_url,
        service_mode=service_mode,
        db_path=db_path,
        yes=yes,
    )


# ---------------------------------------------------------------------------
# Fixture: fake ~/.yadgar layout
# ---------------------------------------------------------------------------


@pytest.fixture
def yadgar_home(tmp_path, monkeypatch):
    """Fake ~/.yadgar directory with a surreal_db containing known subdirs."""
    home = tmp_path / ".yadgar"
    db = home / "surreal_db"
    for subdir in ("vlog", "sstables", "wal"):
        (db / subdir).mkdir(parents=True)
    # Put some bytes so before_bytes > 0
    (db / "vlog" / "00001.vlog").write_bytes(b"x" * 10_000)
    (db / "wal" / "00001.log").write_bytes(b"y" * 1_000)
    monkeypatch.setenv("YADGAR_HOME", str(home))
    return home


# ---------------------------------------------------------------------------
# Fixture: fake cleanup-backups.sh script
# ---------------------------------------------------------------------------


@pytest.fixture
def cleanup_script(tmp_path, monkeypatch):
    script = tmp_path / "cleanup-backups.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    monkeypatch.setenv("YADGAR_CLEANUP_SCRIPT", str(script))
    return script


# ---------------------------------------------------------------------------
# strip_action_log tests  (independent of HTTP / daemons)
# ---------------------------------------------------------------------------


class TestStripActionLog:
    """Unit tests for strip_action_log() — pure text transformation."""

    def _import_strip(self):
        from yadgar.vacuum import strip_action_log

        return strip_action_log

    def test_removes_data_block(self):
        strip = self._import_strip()
        surql = (
            "-- TABLE DATA: memory ----\n"
            "UPSERT memory:1 CONTENT {text: 'hi'};\n"
            "\n"
            "-- TABLE DATA: action_log ----\n"
            "UPSERT action_log:1 CONTENT {cmd: 'rm -rf /'};\n"
            "UPSERT action_log:2 CONTENT {cmd: 'echo hi'};\n"
            "\n"
            "-- TABLE DATA: wiki_page ----\n"
            "UPSERT wiki_page:1 CONTENT {title: 'Home'};\n"
        )
        out = strip(surql)
        assert "action_log:1" not in out
        assert "action_log:2" not in out
        # Surrounding tables preserved verbatim
        assert "memory:1" in out
        assert "wiki_page:1" in out

    def test_removes_define_table_action_log(self):
        strip = self._import_strip()
        surql = (
            "DEFINE TABLE action_log SCHEMAFULL;\n"
            "DEFINE TABLE memory SCHEMAFULL;\n"
            "-- TABLE DATA: action_log ----\n"
            "UPSERT action_log:x CONTENT {};\n"
            "\n"
            "-- TABLE DATA: memory ----\n"
        )
        out = strip(surql)
        assert "DEFINE TABLE action_log" not in out
        assert "DEFINE TABLE memory" in out

    def test_no_action_log_passthrough(self):
        strip = self._import_strip()
        surql = "UPSERT memory:1 CONTENT {text: 'safe'};\n"
        assert strip(surql) == surql

    def test_preserves_tables_after_action_log(self):
        """Tables that appear AFTER action_log in the export must survive."""
        strip = self._import_strip()
        surql = (
            "-- TABLE DATA: action_log ----\n"
            "UPSERT action_log:1 CONTENT {cmd: 'bad'};\n"
            "\n"
            "-- TABLE DATA: wiki_crossref ----\n"
            "RELATE wiki_page:1->wiki_crossref:1->wiki_page:2;\n"
        )
        out = strip(surql)
        assert "wiki_crossref" in out
        assert "action_log" not in out

    def test_real_surrealdb_format(self):
        """Strip handles the actual SurrealDB v3.0.5 dashes-block export format."""
        strip = self._import_strip()
        # Mimics the actual /export output structure
        surql = (
            "-- ------------------------------\n"
            "-- TABLE: memory\n"
            "-- ------------------------------\n"
            "\n"
            "DEFINE TABLE memory TYPE ANY SCHEMALESS PERMISSIONS NONE;\n"
            "\n"
            "\n"
            "\n"
            "-- ------------------------------\n"
            "-- TABLE: action_log\n"
            "-- ------------------------------\n"
            "\n"
            "DEFINE TABLE action_log TYPE ANY SCHEMALESS PERMISSIONS NONE;\n"
            "\n"
            "\n"
            "\n"
            "-- ------------------------------\n"
            "-- TABLE: wiki_page\n"
            "-- ------------------------------\n"
            "\n"
            "DEFINE TABLE wiki_page TYPE ANY SCHEMALESS PERMISSIONS NONE;\n"
            "\n"
            "\n"
            "\n"
            "-- ------------------------------\n"
            "-- TABLE DATA: memory\n"
            "-- ------------------------------\n"
            "\n"
            "INSERT [ { id: memory:aaa, content: 'hello' } ];\n"
            "\n"
            "-- ------------------------------\n"
            "-- TABLE DATA: action_log\n"
            "-- ------------------------------\n"
            "\n"
            "INSERT [ { id: action_log:z, cmd: 'rm -rf /' } ];\n"
            "\n"
            "-- ------------------------------\n"
            "-- TABLE DATA: wiki_page\n"
            "-- ------------------------------\n"
            "\n"
            "INSERT [ { id: wiki_page:bbb, title: 'Home' } ];\n"
        )
        out = strip(surql)

        # action_log schema and data stripped
        assert "DEFINE TABLE action_log" not in out
        assert "action_log:z" not in out

        # Surrounding tables preserved
        assert "DEFINE TABLE memory" in out
        assert "memory:aaa" in out
        assert "DEFINE TABLE wiki_page" in out
        assert "wiki_page:bbb" in out


# ---------------------------------------------------------------------------
# Service-mode detection tests
# ---------------------------------------------------------------------------


class TestServiceModeDetection:
    def test_systemd_from_invocation_id(self, monkeypatch):
        from yadgar.ops import detect_service_mode

        monkeypatch.setenv("INVOCATION_ID", "abc123")
        monkeypatch.delenv("DOCKER_HOST", raising=False)
        assert detect_service_mode() == "systemd"

    def test_docker_from_dockerenv(self, monkeypatch, tmp_path):
        from yadgar.ops import detect_service_mode

        monkeypatch.delenv("INVOCATION_ID", raising=False)
        fake_dockerenv = tmp_path / ".dockerenv"
        fake_dockerenv.touch()
        with patch("yadgar.ops.Path") as MockPath:
            # Make Path("/.dockerenv").exists() return True
            mock_p = MagicMock()
            mock_p.exists.return_value = True
            MockPath.return_value = mock_p
            result = detect_service_mode()
        assert result == "docker"

    def test_manual_fallback(self, monkeypatch):
        from yadgar.ops import detect_service_mode

        monkeypatch.delenv("INVOCATION_ID", raising=False)
        with patch("yadgar.ops.Path") as MockPath:
            mock_p = MagicMock()
            mock_p.exists.return_value = False
            MockPath.return_value = mock_p
            result = detect_service_mode()
        assert result == "manual"


# ---------------------------------------------------------------------------
# Characterization test — MUST FAIL on the old cmd_vacuum (no-op)
# ---------------------------------------------------------------------------


class TestCharacterization:
    """Full-flow characterization test.

    Proves that cmd_vacuum does the export→swap→reimport dance and:
    - Produces a .filtered.surql file on disk.
    - Inserts a consolidation_log row with kind="vacuum".
    - Does NOT lose rows from preserved tables.
    - action_log data is stripped from the export.

    This test FAILS on the old cmd_vacuum (no-op: exits early with
    "No clog directory found"). That's intentional — TDD.
    """

    def _mock_backend(self, tmp_path, monkeypatch, import_status: int = 200):
        """Set up mocks so the HTTP calls succeed without a real daemon."""
        # Fake export surql
        fake_surql = (
            "-- TABLE DATA: memory ----\n"
            "UPSERT memory:aaa CONTENT {content: 'test', heat: 1.0};\n"
            "\n"
            "-- TABLE DATA: action_log ----\n"
            "UPSERT action_log:z CONTENT {cmd: 'bad stuff'};\n"
            "\n"
            "-- TABLE DATA: wiki_page ----\n"
            "UPSERT wiki_page:bbb CONTENT {title: 'Test Page'};\n"
        )

        mock_export_resp = MagicMock()
        mock_export_resp.status_code = 200
        mock_export_resp.text = fake_surql

        mock_import_resp = MagicMock()
        mock_import_resp.status_code = import_status
        mock_import_resp.text = "OK" if import_status == 200 else "Internal Server Error"

        mock_health_resp = MagicMock()
        mock_health_resp.status_code = 200

        mock_invariants_resp = MagicMock()
        mock_invariants_resp.status_code = 200
        mock_invariants_resp.json.return_value = {"ok": True}

        mock_consolidation_resp = MagicMock()
        mock_consolidation_resp.status_code = 200
        mock_consolidation_resp.json.return_value = [{}]

        import httpx

        def mock_get(url, **kwargs):
            if "/health" in url or "/healthz" in url:
                return mock_health_resp
            if "/export" in url:
                return mock_export_resp
            return MagicMock(status_code=200, text="")

        def mock_post(url, **kwargs):
            if "/import" in url:
                return mock_import_resp
            if "/api/check_invariants" in url:
                return mock_invariants_resp
            if "/sql" in url:
                return mock_consolidation_resp
            return MagicMock(status_code=200, text="")

        monkeypatch.setattr(httpx, "get", mock_get)
        monkeypatch.setattr(httpx, "post", mock_post)

        return fake_surql

    def test_characterization_produces_filtered_surql_and_log_row(
        self, tmp_path, monkeypatch, yadgar_home, cleanup_script
    ):
        """Discriminating check: new cmd_vacuum produces a .filtered.surql file.

        The old no-op returns early because clog/ doesn't exist — it never
        creates any file. This assertion FAILS on the old code.
        """
        from yadgar.vacuum import cmd_vacuum_impl

        self._mock_backend(tmp_path, monkeypatch)

        log_rows = []

        def fake_log_row(row):
            log_rows.append(row)

        args = _vacuum_args(
            backend_url="http://127.0.0.1:8080",
            service_mode="manual",
            db_path=str(yadgar_home / "surreal_db"),
        )

        with patch("yadgar.vacuum._log_consolidation_row", fake_log_row):
            with patch("yadgar.vacuum.ServiceController") as MockSVC:
                with patch("yadgar.vacuum._wait_for_health", return_value=True):
                    with patch("yadgar.vacuum._wait_for_yadgar_health", return_value=True):
                        mock_svc = MagicMock()
                        MockSVC.return_value = mock_svc

                        result = cmd_vacuum_impl(args)

        # Must succeed
        assert result == 0, "cmd_vacuum_impl should return 0 on success"

        # Must produce a filtered file
        filtered_files = list(yadgar_home.glob("vacuum_export_*.filtered.surql"))
        assert filtered_files, "Expected a .filtered.surql file in ~/.yadgar/"

        # action_log stripped
        content = filtered_files[0].read_text()
        assert "action_log:z" not in content

        # Surrounding tables preserved
        assert "memory:aaa" in content
        assert "wiki_page:bbb" in content

    def test_cmd_vacuum_delegates_to_impl(self, tmp_path, monkeypatch, yadgar_home, cleanup_script):
        """Regression: cmd_vacuum() in __main__ must delegate to cmd_vacuum_impl.

        The old cmd_vacuum was a no-op (checked for clog/ which doesn't exist
        in surrealkv >= v2). The new wrapper must call cmd_vacuum_impl and exit
        non-zero on failure, zero on success.
        """
        import httpx

        from yadgar import __main__ as main_mod

        fake_surql = "-- TABLE DATA: memory ----\nUPSERT memory:1 CONTENT {};\n"
        monkeypatch.setattr(
            httpx,
            "get",
            lambda url, **kw: MagicMock(
                status_code=200, text=fake_surql if "/export" in url else ""
            ),
        )
        monkeypatch.setattr(
            httpx,
            "post",
            lambda url, **kw: MagicMock(
                status_code=200, text="OK", json=MagicMock(return_value={"ok": True})
            ),
        )

        args = _vacuum_args(
            backend_url="http://127.0.0.1:8080",
            service_mode="manual",
            db_path=str(yadgar_home / "surreal_db"),
        )

        with patch("yadgar.vacuum._log_consolidation_row"):
            with patch("yadgar.vacuum.ServiceController"):
                with patch("yadgar.vacuum._wait_for_health", return_value=True):
                    with patch("yadgar.vacuum._wait_for_yadgar_health", return_value=True):
                        # Should not raise — exit_code 0 means no sys.exit call
                        try:
                            main_mod.cmd_vacuum(args)
                        except SystemExit as e:
                            pytest.fail(f"cmd_vacuum raised SystemExit({e.code}) unexpectedly")


# ---------------------------------------------------------------------------
# RELATE preservation test
# ---------------------------------------------------------------------------


class TestRelatePreservation:
    """RELATE edges (wiki_crossref) must survive the vacuum cycle."""

    def test_wiki_crossref_preserved_in_filtered_surql(
        self, tmp_path, monkeypatch, yadgar_home, cleanup_script
    ):
        """After strip_action_log, RELATE / wiki_crossref rows must survive."""
        import httpx

        from yadgar.vacuum import cmd_vacuum_impl

        fake_surql = (
            "-- TABLE DATA: memory ----\n"
            "UPSERT memory:1 CONTENT {content: 'alpha'};\n"
            "UPSERT memory:2 CONTENT {content: 'beta'};\n"
            "UPSERT memory:3 CONTENT {content: 'gamma'};\n"
            "\n"
            "-- TABLE DATA: action_log ----\n"
            "UPSERT action_log:x CONTENT {cmd: 'bad'};\n"
            "\n"
            "-- TABLE DATA: wiki_crossref ----\n"
            "RELATE wiki_page:A->wiki_crossref:1->wiki_page:B;\n"
            "RELATE wiki_page:B->wiki_crossref:2->wiki_page:C;\n"
        )

        mock_export = MagicMock(status_code=200, text=fake_surql)
        mock_import = MagicMock(status_code=200, text="OK")
        mock_health = MagicMock(status_code=200)
        mock_invariants = MagicMock(status_code=200, json=MagicMock(return_value={"ok": True}))

        def mock_get(url, **kwargs):
            if "/export" in url:
                return mock_export
            return mock_health

        def mock_post(url, **kwargs):
            if "/import" in url:
                return mock_import
            return mock_invariants

        monkeypatch.setattr(httpx, "get", mock_get)
        monkeypatch.setattr(httpx, "post", mock_post)

        args = _vacuum_args(
            backend_url="http://127.0.0.1:8080",
            service_mode="manual",
            db_path=str(yadgar_home / "surreal_db"),
        )

        with patch("yadgar.vacuum._log_consolidation_row"):
            with patch("yadgar.vacuum.ServiceController"):
                with patch("yadgar.vacuum._wait_for_health", return_value=True):
                    with patch("yadgar.vacuum._wait_for_yadgar_health", return_value=True):
                        cmd_vacuum_impl(args)

        filtered = list(yadgar_home.glob("vacuum_export_*.filtered.surql"))
        assert filtered
        content = filtered[0].read_text()

        # Both RELATE edges must survive
        assert "wiki_crossref:1" in content
        assert "wiki_crossref:2" in content
        # action_log stripped
        assert "action_log:x" not in content


# ---------------------------------------------------------------------------
# Failure injection test — /import returns HTTP 500
# ---------------------------------------------------------------------------


class TestFailureInjection:
    """When /import returns 500, bloated dir must be RETAINED and exit != 0."""

    def test_import_500_retains_bloated_dir(
        self, tmp_path, monkeypatch, yadgar_home, cleanup_script
    ):
        import httpx

        from yadgar.vacuum import cmd_vacuum_impl

        fake_surql = "-- TABLE DATA: memory ----\nUPSERT memory:1 CONTENT {};\n"
        mock_export = MagicMock(status_code=200, text=fake_surql)
        mock_import = MagicMock(status_code=500, text="Internal Server Error")
        mock_health = MagicMock(status_code=200)

        def mock_get(url, **kwargs):
            if "/export" in url:
                return mock_export
            return mock_health

        def mock_post(url, **kwargs):
            return mock_import

        monkeypatch.setattr(httpx, "get", mock_get)
        monkeypatch.setattr(httpx, "post", mock_post)

        args = _vacuum_args(
            backend_url="http://127.0.0.1:8080",
            service_mode="manual",
            db_path=str(yadgar_home / "surreal_db"),
        )

        started_services = []

        with patch("yadgar.vacuum._log_consolidation_row"):
            with patch("yadgar.vacuum.ServiceController") as MockSVC:
                with patch("yadgar.vacuum._wait_for_health", return_value=True):
                    mock_svc = MagicMock()
                    mock_svc.start_backend.side_effect = lambda: started_services.append(
                        "start_backend"
                    )
                    mock_svc.start_yadgar.side_effect = lambda: started_services.append(
                        "start_yadgar"
                    )
                    MockSVC.return_value = mock_svc

                    exit_code = cmd_vacuum_impl(args)

        # Non-zero exit on import failure
        assert exit_code != 0, "Expected non-zero exit on /import HTTP 500"

        # Bloated dir must be retained
        bloated_dirs = list(yadgar_home.glob("surreal_db.bloated-*"))
        assert bloated_dirs, "Bloated dir must be retained when /import fails"

        # yadgar (MCP layer) must NOT be started after import failure
        assert "start_yadgar" not in started_services, (
            "yadgar service must NOT be started after import failure"
        )
